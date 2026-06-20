"""
Behavioural tests for the regional-initiated, sub-fleet-scoped OTA publish
(integrations.social.api_fleet_update — gap #58) and the push_broadcast
node_ids allowlist + HierarchyService.region_member_node_ids it relies on.

These import the REAL fleet_update blueprint + FleetCommandService +
HierarchyService, mock the BOUNDARIES (the fleet command service, the audit
log, the account gate, the region-member query), call the REAL route, and
assert observable side-effects:

  - POST /api/ota/publish-regional is gated by the REAL require_regional
    (401/403 for a caller with no token) — a non-regional cannot fan out.
  - a regional publish scopes push_broadcast to EXACTLY its region's members
    (node_ids allowlist), never a global broadcast.
  - region is derived server-side from THIS node's id, NEVER from the body —
    a region key in the request body is ignored.
  - an empty member set returns node_count=0 and NEVER falls through to a
    global all-nodes broadcast.
  - push_broadcast(node_ids=[]) returns [] (scopes to nobody); node_ids with
    entries fans out only to active peers in the allowlist.
  - region_member_node_ids reads the active RegionAssignment set for the
    regional and nothing else.

No grep/source-shape assertions.
"""
import json
import os
import sys
import unittest
from functools import wraps
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')


def _app():
    from flask import Flask
    from integrations.social.api_fleet_update import fleet_update_bp
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(fleet_update_bp)
    return app


class TestRegionalPublishGate(unittest.TestCase):
    """The route is gated by the REAL require_regional (no bypass)."""

    def test_publish_regional_requires_regional_role(self):
        app = _app()
        fleet = MagicMock()
        # A request with no Bearer token must be rejected BEFORE any fan-out.
        with patch('integrations.social.fleet_command.FleetCommandService', fleet):
            r = app.test_client().post('/api/ota/publish-regional',
                                       json={'channel': 'stable', 'commit': 'x'})
        self.assertIn(r.status_code, (401, 403))
        fleet.push_broadcast.assert_not_called()


class _RegionalPublishCase(unittest.TestCase):
    """Harness: patch require_regional to a pass-through that sets g.db/g.user_id
    (the REAL gate is covered by TestRegionalPublishGate)."""

    def setUp(self):
        from flask import g

        def _pass_through(f):
            @wraps(f)
            def inner(*a, **k):
                g.user_id = 'regional_steward'
                g.db = self.fake_db
                return f(*a, **k)
            return inner

        self.fake_db = MagicMock()
        self._gate = patch('integrations.social.auth.require_regional', _pass_through)
        self._gate.start()
        self.app = _app()

    def tearDown(self):
        self._gate.stop()


class TestRegionalPublishScopes(_RegionalPublishCase):

    def test_publish_scopes_to_region_members(self):
        """push_broadcast is called with node_ids = THIS region's member set."""
        fleet = MagicMock()
        fleet.push_broadcast.return_value = [{'id': 11}, {'id': 12}]
        members = ['local-a', 'local-b', 'local-c']
        with patch('integrations.social.fleet_command.FleetCommandService', fleet), \
             patch('integrations.social.fleet_command._get_self_node_id',
                   return_value='regional-self-001'), \
             patch('integrations.social.hierarchy_service.HierarchyService.region_member_node_ids',
                   return_value=members), \
             patch('security.immutable_audit_log.get_audit_log', return_value=MagicMock()):
            r = self.app.test_client().post('/api/ota/publish-regional', json={
                'channel': 'stable', 'commit': 'cafef00d',
                'flake_ref': 'github:hertz-ai/HARTOS/cafef00d'})

        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['success'])

        fleet.push_broadcast.assert_called_once()
        # firmware_update, scoped to the region members via node_ids allowlist.
        pb_args = fleet.push_broadcast.call_args
        self.assertEqual(pb_args[0][1], 'firmware_update')
        self.assertEqual(pb_args.kwargs['node_ids'], members)
        # Region is reported from the server-resolved self id, not the body.
        self.assertEqual(data['region_node_id'], 'regional-self-001')
        self.assertEqual(data['member_count'], 3)
        self.assertEqual(data['node_count'], 2)
        # A regional publish does NOT kick the central pipeline.
        self.assertFalse(data['pipeline_started'])

    def test_region_is_server_derived_not_from_body(self):
        """A 'region'/'node_ids' key in the body must be IGNORED — scope comes
        only from the server-side self id + RegionAssignment table."""
        fleet = MagicMock()
        fleet.push_broadcast.return_value = [{'id': 1}]
        members = ['only-my-local']
        with patch('integrations.social.fleet_command.FleetCommandService', fleet), \
             patch('integrations.social.fleet_command._get_self_node_id',
                   return_value='regional-self-002'), \
             patch('integrations.social.hierarchy_service.HierarchyService.region_member_node_ids',
                   return_value=members) as m_members, \
             patch('security.immutable_audit_log.get_audit_log', return_value=MagicMock()):
            r = self.app.test_client().post('/api/ota/publish-regional', json={
                'channel': 'stable', 'commit': 'beef',
                # Attacker-supplied scope-widening attempts — all ignored:
                'region_node_id': 'some-other-region',
                'node_ids': ['victim-node-in-another-region'],
                'tier': ''})

        self.assertEqual(r.status_code, 200)
        # region_member_node_ids was queried with the SERVER self id only.
        m_members.assert_called_once()
        self.assertEqual(m_members.call_args[0][1], 'regional-self-002')
        # The fan-out used the resolved members, NOT the body's node_ids.
        self.assertEqual(fleet.push_broadcast.call_args.kwargs['node_ids'], members)

    def test_empty_member_set_fans_out_to_nobody(self):
        """A regional with no locals returns node_count=0 — NEVER a global
        broadcast."""
        fleet = MagicMock()
        fleet.push_broadcast.return_value = []  # empty allowlist → nobody
        with patch('integrations.social.fleet_command.FleetCommandService', fleet), \
             patch('integrations.social.fleet_command._get_self_node_id',
                   return_value='regional-empty'), \
             patch('integrations.social.hierarchy_service.HierarchyService.region_member_node_ids',
                   return_value=[]), \
             patch('security.immutable_audit_log.get_audit_log', return_value=MagicMock()):
            r = self.app.test_client().post('/api/ota/publish-regional',
                                            json={'channel': 'stable', 'commit': 'abc'})

        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['member_count'], 0)
        self.assertEqual(data['node_count'], 0)
        # Still scoped: node_ids was the empty allowlist, not None (= global).
        self.assertEqual(fleet.push_broadcast.call_args.kwargs['node_ids'], [])

    def test_rejects_missing_commit(self):
        r = self.app.test_client().post('/api/ota/publish-regional',
                                        json={'channel': 'stable'})
        self.assertEqual(r.status_code, 400)

    def test_rejects_invalid_channel(self):
        r = self.app.test_client().post('/api/ota/publish-regional',
                                        json={'channel': 'bogus', 'commit': 'x'})
        self.assertEqual(r.status_code, 400)


class TestPushBroadcastNodeIdsAllowlist(unittest.TestCase):
    """The fan-out unit: node_ids scopes push_broadcast; empty = nobody."""

    @classmethod
    def setUpClass(cls):
        from integrations.social.models import Base, get_engine
        Base.metadata.create_all(get_engine())

    def _db(self):
        from integrations.social.models import get_db
        return get_db()

    def _peers(self, db, *specs):
        from integrations.social.models import PeerNode
        for node_id, status in specs:
            db.add(PeerNode(node_id=node_id, url=f'http://{node_id}:6777',
                            status=status, tier='local'))
        db.flush()

    def test_empty_allowlist_returns_nobody(self):
        from integrations.social.fleet_command import FleetCommandService
        db = self._db()
        try:
            with patch('integrations.social.fleet_command._sign_command', return_value='sig'):
                out = FleetCommandService.push_broadcast(
                    db, 'firmware_update', {'release_hash': 'x'},
                    node_ids=[], issued_by='reg')
            self.assertEqual(out, [])
        finally:
            db.rollback(); db.close()

    def test_allowlist_targets_only_active_listed_nodes(self):
        from integrations.social.fleet_command import FleetCommandService
        db = self._db()
        try:
            self._peers(db, ('al-a', 'active'), ('al-b', 'active'),
                        ('al-dead', 'dead'), ('al-other', 'active'))
            with patch('integrations.social.fleet_command._sign_command', return_value='sig'):
                out = FleetCommandService.push_broadcast(
                    db, 'firmware_update', {'release_hash': 'x'},
                    node_ids=['al-a', 'al-b', 'al-dead'], issued_by='reg')
            targets = {c['target_node_id'] for c in out}
            # al-a + al-b are active & listed; al-dead is listed but not active;
            # al-other is active but NOT listed.
            self.assertEqual(targets, {'al-a', 'al-b'})
        finally:
            db.rollback(); db.close()

    def test_none_allowlist_is_unscoped_global(self):
        """node_ids=None keeps the original global-broadcast behaviour."""
        from integrations.social.fleet_command import FleetCommandService
        db = self._db()
        try:
            self._peers(db, ('gl-a', 'active'), ('gl-b', 'active'))
            with patch('integrations.social.fleet_command._sign_command', return_value='sig'):
                out = FleetCommandService.push_broadcast(
                    db, 'firmware_update', {'release_hash': 'x'},
                    node_ids=None, issued_by='reg')
            targets = {c['target_node_id'] for c in out}
            self.assertTrue({'gl-a', 'gl-b'}.issubset(targets))
        finally:
            db.rollback(); db.close()


class TestRegionMemberResolution(unittest.TestCase):
    """region_member_node_ids reads only the active RegionAssignment set."""

    @classmethod
    def setUpClass(cls):
        from integrations.social.models import Base, get_engine
        Base.metadata.create_all(get_engine())

    def test_returns_active_members_for_the_regional(self):
        from integrations.social.models import get_db, RegionAssignment
        from integrations.social.hierarchy_service import HierarchyService
        db = get_db()
        try:
            db.add(RegionAssignment(local_node_id='loc-1', regional_node_id='reg-X', status='active'))
            db.add(RegionAssignment(local_node_id='loc-2', regional_node_id='reg-X', status='active'))
            # Not this regional:
            db.add(RegionAssignment(local_node_id='loc-3', regional_node_id='reg-OTHER', status='active'))
            # This regional but not active:
            db.add(RegionAssignment(local_node_id='loc-4', regional_node_id='reg-X', status='revoked'))
            db.flush()
            members = HierarchyService.region_member_node_ids(db, 'reg-X')
            self.assertEqual(set(members), {'loc-1', 'loc-2'})
        finally:
            db.rollback(); db.close()


if __name__ == '__main__':
    unittest.main()
