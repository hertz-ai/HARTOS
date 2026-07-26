"""
Behavioural tests for the consolidated central OTA backend
(integrations.social.api_fleet_update) — the SINGLE /api/ota/* HTTP surface.

These import the REAL fleet_update blueprint, mock the boundaries (the fleet
command service, the upgrade orchestrator, the audit log, the account gate),
call the REAL routes, and assert observable side-effects:

  - GET  /api/ota/latest serves the FleetCommand-derived pointer (public, no auth)
  - POST /api/ota/publish fans out a firmware_update broadcast, kicks the EXISTING
    upgrade pipeline (NOT a second loop), and writes an immutable audit entry
  - the account gate (require_central) is actually enforced (401/403 for non-central)
  - publish does NOT start a second pipeline when one is already active

This file replaces tests/unit/test_ota_api.py (which tested the removed,
shadowed integrations/agent_engine/ota_api.py). No grep/source-shape assertions.
"""
import json
import os
import sys
import tempfile
import unittest
from functools import wraps
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _app():
    """Flask test app with the REAL fleet_update blueprint registered."""
    from flask import Flask
    from integrations.social.api_fleet_update import fleet_update_bp
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(fleet_update_bp)
    return app


class _FakeCmd(dict):
    """A FleetCommand-row stand-in exposing the attributes /api/ota/latest reads."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.cmd_type = kw.get('cmd_type', 'firmware_update')
        self.params_json = kw.get('params_json', '{}')
        self.created_at = kw.get('created_at', 1700000000.0)
        self.issued_by = kw.get('issued_by', 'central_steward')


class TestOtaLatestPublic(unittest.TestCase):
    """GET /api/ota/latest is public and derives the pointer from the command log."""

    def setUp(self):
        self.app = _app()

    def test_empty_pointer_when_channel_unpublished(self):
        # get_db returns a session whose firmware_update query yields nothing.
        fake_db = MagicMock()
        fake_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        with patch('integrations.social.models.get_db', return_value=fake_db):
            r = self.app.test_client().get('/api/ota/latest?channel=stable')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['channel'], 'stable')
        # Empty pointer = "stay put" (the node's jq `.commit // ""` holds).
        self.assertEqual(data['commit'], '')

    def test_serves_pointer_from_newest_command(self):
        cmd = _FakeCmd(
            params_json=json.dumps({'update_url': 'github:hertz-ai/HARTOS/abc',
                                    'release_hash': 'abc123', 'channel': 'testing'}),
            issued_by='central_steward')
        fake_db = MagicMock()
        fake_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [cmd]
        with patch('integrations.social.models.get_db', return_value=fake_db):
            r = self.app.test_client().get('/api/ota/latest?channel=testing')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['commit'], 'abc123')
        self.assertEqual(data['flake_ref'], 'github:hertz-ai/HARTOS/abc')
        self.assertEqual(data['channel'], 'testing')


class TestOtaPublishGate(unittest.TestCase):
    """POST /api/ota/publish is gated by the central account role (real gate)."""

    def test_publish_requires_central_role(self):
        # Use the REAL require_central (no bypass): a request with no Bearer
        # token must be rejected before any fan-out side-effect.
        app = _app()
        fleet = MagicMock()
        with patch('integrations.social.fleet_command.FleetCommandService', fleet):
            r = app.test_client().post('/api/ota/publish',
                                       json={'channel': 'stable', 'commit': 'x'})
        self.assertIn(r.status_code, (401, 403))
        # No fan-out by an unauthorized caller.
        fleet.push_broadcast.assert_not_called()


class TestOtaPublishBehaviour(unittest.TestCase):
    """Publish fans out + kicks the EXISTING pipeline + audits (gate patched)."""

    def setUp(self):
        # Patch the account gate to a pass-through that populates the request
        # globals require_auth would set (g.db, g.user_id). The REAL gate is
        # covered by TestOtaPublishGate.
        from flask import g

        def _pass_through(f):
            @wraps(f)
            def inner(*a, **k):
                g.user_id = 'central_steward'
                g.db = self.fake_db
                return f(*a, **k)
            return inner

        self.fake_db = MagicMock()
        self._gate = patch('integrations.social.auth.require_central', _pass_through)
        self._gate.start()
        self.app = _app()

    def tearDown(self):
        self._gate.stop()

    def _orch(self, stage='idle'):
        orch = MagicMock()
        orch.get_status.return_value = {'stage': stage}
        orch.start_upgrade.return_value = {'success': True, 'stage': 'building'}
        return orch

    def test_publish_fans_out_kicks_pipeline_and_audits(self):
        orch = self._orch('idle')
        fleet = MagicMock()
        fleet.push_broadcast.return_value = [{'id': 1}, {'id': 2}]
        audit = MagicMock()
        with patch('integrations.social.fleet_command.FleetCommandService', fleet), \
             patch('integrations.agent_engine.upgrade_orchestrator.get_upgrade_orchestrator',
                   return_value=orch), \
             patch('security.immutable_audit_log.get_audit_log', return_value=audit):
            r = self.app.test_client().post('/api/ota/publish', json={
                'channel': 'stable', 'commit': 'cafebabe',
                'flake_ref': 'github:hertz-ai/HARTOS/cafebabe'})

        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['success'])

        # 1. Fanned out via the EXISTING signed fleet bus as firmware_update.
        fleet.push_broadcast.assert_called_once()
        pb_args = fleet.push_broadcast.call_args[0]
        self.assertEqual(pb_args[1], 'firmware_update')
        # The command params carry the channel so /api/ota/latest can recover it.
        self.assertEqual(pb_args[2]['channel'], 'stable')
        self.assertEqual(pb_args[2]['release_hash'], 'cafebabe')
        self.assertEqual(data['node_count'], 2)
        self.assertEqual(data['command_ids'], [1, 2])

        # 2. The EXISTING pipeline was kicked via start_upgrade (single entry).
        orch.start_upgrade.assert_called_once()
        self.assertTrue(data['pipeline_started'])

        # 3. Immutable audit entry written for the account action.
        audit.log_event.assert_called_once()
        self.assertEqual(audit.log_event.call_args[0][0], 'ota_publish')
        self.assertTrue(data['audited'])

    def test_publish_does_not_double_start_when_pipeline_active(self):
        orch = self._orch('canary')  # a pipeline is already running
        fleet = MagicMock()
        fleet.push_broadcast.return_value = [{'id': 7}]
        with patch('integrations.social.fleet_command.FleetCommandService', fleet), \
             patch('integrations.agent_engine.upgrade_orchestrator.get_upgrade_orchestrator',
                   return_value=orch), \
             patch('security.immutable_audit_log.get_audit_log', return_value=MagicMock()):
            r = self.app.test_client().post('/api/ota/publish',
                                            json={'channel': 'nightly', 'commit': 'feed0001'})

        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        # Fan-out still happened, but NO second pipeline started (no parallel loop).
        self.assertTrue(data['success'])
        self.assertEqual(data['node_count'], 1)
        orch.start_upgrade.assert_not_called()
        self.assertFalse(data['pipeline_started'])

    def test_publish_survives_orchestrator_and_audit_unavailable(self):
        """Pipeline-kick + audit are best-effort: a publish still fans out and
        returns success even if those subsystems are unavailable."""
        fleet = MagicMock()
        fleet.push_broadcast.return_value = [{'id': 9}]
        with patch('integrations.social.fleet_command.FleetCommandService', fleet), \
             patch('integrations.agent_engine.upgrade_orchestrator.get_upgrade_orchestrator',
                   side_effect=Exception('orchestrator down')), \
             patch('security.immutable_audit_log.get_audit_log',
                   side_effect=Exception('audit down')):
            r = self.app.test_client().post('/api/ota/publish',
                                            json={'channel': 'stable', 'commit': 'abc'})
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['success'])
        fleet.push_broadcast.assert_called_once()  # the load-bearing step ran
        self.assertFalse(data['pipeline_started'])
        self.assertFalse(data['audited'])

    def test_publish_rejects_missing_commit(self):
        r = self.app.test_client().post('/api/ota/publish', json={'channel': 'stable'})
        self.assertEqual(r.status_code, 400)

    def test_publish_rejects_invalid_channel(self):
        r = self.app.test_client().post('/api/ota/publish',
                                        json={'channel': 'bogus', 'commit': 'x'})
        self.assertEqual(r.status_code, 400)


if __name__ == '__main__':
    unittest.main()
