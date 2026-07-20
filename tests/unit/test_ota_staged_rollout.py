"""
Behavioural tests for the REAL staged-rollout / canary gate
(integrations.social.api_fleet_update — gap #59), replacing the approve-all
stub at /api/social/fleet/update-approved.

These import the REAL fleet_update blueprint + decision helpers, mock the
BOUNDARIES (the channel-pointer lookup, the fleet command service, the audit
log, the account gate), call the REAL routes/functions, and assert observable
side-effects:

  - the canary bucket is deterministic + distributes ~N% of a population at
    canary_pct=N and is MONOTONIC (a node in the 25% cohort is in the 50%)
  - _rollout_decision precedence: deny > approve > canary_pct > full-rollout
  - GET /api/social/fleet/update-approved returns per-node yes/no from the
    pointer policy (canary hold vs approve), passes through when no policy /
    version-mismatch (standalone auto-approve preserved)
  - POST /api/ota/rollout re-rolls the CURRENT pointer's policy without a new
    commit, scoped global (central) vs region (regional), and rejects when no
    pointer / no canary fields
  - publish carries the canary fields into the firmware_update params and
    rejects a malformed canary_pct

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

from integrations.social.api_fleet_update import (  # noqa: E402
    fleet_update_bp, _canary_bucket, _rollout_decision, _canary_params_from_body,
)


def _app():
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(fleet_update_bp)
    return app


class TestCanaryBucketProperties(unittest.TestCase):
    """The deterministic-percentage core of a real staged rollout."""

    def test_bucket_is_deterministic(self):
        self.assertEqual(_canary_bucket('node-7', 'v1'), _canary_bucket('node-7', 'v1'))

    def test_bucket_reshuffles_per_version(self):
        """Each release reshuffles which nodes are early — no permanent guinea
        pig.  (Statistically near-certain to differ across many nodes.)"""
        diffs = sum(1 for i in range(500)
                    if _canary_bucket(f'n{i}', 'vA') != _canary_bucket(f'n{i}', 'vB'))
        self.assertGreater(diffs, 400)  # the vast majority move

    def test_distribution_matches_percentage(self):
        n = 5000
        for pct in (5, 25, 50):
            approved = sum(1 for i in range(n)
                           if _rollout_decision(f'node-{i}', 'v1', {'canary_pct': pct}))
            frac = approved / n * 100
            self.assertAlmostEqual(frac, pct, delta=3.0)

    def test_ramp_is_monotonic(self):
        """A node approved at 25% must STILL be approved at 50% — ramping never
        revokes a node mid-rollout."""
        n = 5000
        c25 = {i for i in range(n) if _rollout_decision(f'node-{i}', 'v9', {'canary_pct': 25})}
        c50 = {i for i in range(n) if _rollout_decision(f'node-{i}', 'v9', {'canary_pct': 50})}
        self.assertTrue(c25.issubset(c50))


class TestRolloutDecisionPrecedence(unittest.TestCase):

    def test_deny_wins_over_everything(self):
        policy = {'deny': ['n1'], 'approve': ['n1'], 'canary_pct': 100}
        self.assertFalse(_rollout_decision('n1', 'v', policy))

    def test_approve_wins_over_canary(self):
        self.assertTrue(_rollout_decision('n1', 'v', {'approve': ['n1'], 'canary_pct': 0}))

    def test_no_policy_approves(self):
        self.assertTrue(_rollout_decision('n1', 'v', {}))

    def test_canary_zero_holds_unlisted_node(self):
        self.assertFalse(_rollout_decision('n1', 'v', {'canary_pct': 0}))

    def test_canary_hundred_approves_all(self):
        self.assertTrue(_rollout_decision('n1', 'v', {'canary_pct': 100}))

    def test_malformed_pct_does_not_block(self):
        self.assertTrue(_rollout_decision('n1', 'v', {'canary_pct': 'oops'}))


class TestCanaryParamValidation(unittest.TestCase):

    def test_extracts_only_supplied_fields(self):
        self.assertEqual(_canary_params_from_body({'canary_pct': 25}), {'canary_pct': 25})
        self.assertEqual(_canary_params_from_body({}), {})

    def test_rejects_out_of_range_pct(self):
        with self.assertRaises(ValueError):
            _canary_params_from_body({'canary_pct': 150})

    def test_rejects_non_list_approve(self):
        with self.assertRaises(ValueError):
            _canary_params_from_body({'approve': 'not-a-list'})


class TestUpdateApprovedGate(unittest.TestCase):
    """GET /api/social/fleet/update-approved — the REAL per-node gate."""

    def setUp(self):
        self.app = _app()

    def _ptr(self, commit='abc', policy=None):
        return {'channel': 'stable', 'flake_ref': 'github:x/y/abc',
                'commit': commit, 'published_at': 1.0, 'policy': policy or {}}

    def test_no_pointer_passes_through(self):
        """A standalone node (no central pointer) keeps auto-approving."""
        with patch('integrations.social.api_fleet_update._latest_pointer_for_channel',
                   return_value=None), \
             patch('integrations.social.models.get_db', return_value=MagicMock()):
            r = self.app.test_client().get(
                '/api/social/fleet/update-approved?v=abc&node_id=n1&channel=stable')
        data = json.loads(r.data)
        self.assertTrue(data['approved'])
        self.assertEqual(data['reason'], 'no-fleet-policy')

    def test_version_mismatch_passes_through(self):
        """A node asking about a different commit than the pointer isn't gated
        on this pointer."""
        with patch('integrations.social.api_fleet_update._latest_pointer_for_channel',
                   return_value=self._ptr(commit='NEWcommit', policy={'canary_pct': 0})), \
             patch('integrations.social.models.get_db', return_value=MagicMock()):
            r = self.app.test_client().get(
                '/api/social/fleet/update-approved?v=OLDcommit&node_id=n1')
        data = json.loads(r.data)
        self.assertTrue(data['approved'])
        self.assertEqual(data['reason'], 'version-mismatch-passthrough')

    def test_canary_zero_holds_node(self):
        with patch('integrations.social.api_fleet_update._latest_pointer_for_channel',
                   return_value=self._ptr(commit='abc', policy={'canary_pct': 0})), \
             patch('integrations.social.models.get_db', return_value=MagicMock()):
            r = self.app.test_client().get(
                '/api/social/fleet/update-approved?v=abc&node_id=n1')
        data = json.loads(r.data)
        self.assertFalse(data['approved'])
        self.assertEqual(data['reason'], 'staged-rollout-hold')

    def test_deny_list_blocks_node(self):
        with patch('integrations.social.api_fleet_update._latest_pointer_for_channel',
                   return_value=self._ptr(commit='abc',
                                          policy={'canary_pct': 100, 'deny': ['n1']})), \
             patch('integrations.social.models.get_db', return_value=MagicMock()):
            r = self.app.test_client().get(
                '/api/social/fleet/update-approved?v=abc&node_id=n1')
        self.assertFalse(json.loads(r.data)['approved'])

    def test_approve_list_admits_node(self):
        with patch('integrations.social.api_fleet_update._latest_pointer_for_channel',
                   return_value=self._ptr(commit='abc',
                                          policy={'canary_pct': 0, 'approve': ['n1']})), \
             patch('integrations.social.models.get_db', return_value=MagicMock()):
            r = self.app.test_client().get(
                '/api/social/fleet/update-approved?v=abc&node_id=n1')
        self.assertTrue(json.loads(r.data)['approved'])


class TestUpdateApprovedDerivesPolicyFromCommandLog(unittest.TestCase):
    """End-to-end through the REAL pointer derivation: a published
    firmware_update command's canary params drive the gate (one source of
    truth — no separate rollout table)."""

    @classmethod
    def setUpClass(cls):
        from integrations.social.models import Base, get_engine
        Base.metadata.create_all(get_engine())

    def _publish_pointer(self, db, commit, canary_pct, target='n1'):
        # The per-node policy pointer is the firmware_update command ADDRESSED
        # TO the polling node (the region-scoped gate reads the node's OWN
        # command so a foreign regional can't override it).
        from integrations.social.models import FleetCommand
        db.add(FleetCommand(
            target_node_id=target, cmd_type='firmware_update',
            params_json=json.dumps({'update_url': 'github:x/y/' + commit,
                                    'release_hash': commit, 'channel': 'stable',
                                    'canary_pct': canary_pct}),
            issued_by='central', signature='sig', status='pending'))
        db.flush()

    def test_gate_reads_canary_from_published_command(self):
        from integrations.social.models import get_db
        app = _app()
        db = get_db()
        try:
            # paused rollout for THIS node (the command targets n1).
            self._publish_pointer(db, 'cafe', canary_pct=0, target='n1')
            # The gate opens its OWN db via get_db — point it at this session.
            with patch('integrations.social.models.get_db', return_value=db):
                with app.test_client() as c:
                    r = c.get('/api/social/fleet/update-approved?v=cafe&node_id=n1')
            self.assertFalse(json.loads(r.data)['approved'])
        finally:
            db.rollback(); db.close()


class TestCrossRegionRolloutOverrideBlocked(unittest.TestCase):
    """PRIVILEGE-ESCALATION GUARD (cross-region rollout-policy override): a
    regional's re-roll writes firmware_update commands targeting only ITS OWN
    members.  A node in another region polling the gate must NOT pick up that
    foreign regional's canary_pct/deny policy — the verdict is read from the
    command ADDRESSED TO the polling node, not whichever regional wrote last."""

    @classmethod
    def setUpClass(cls):
        from integrations.social.models import Base, get_engine
        Base.metadata.create_all(get_engine())

    def _cmd(self, db, target, commit, canary_pct, issued_by):
        from integrations.social.models import FleetCommand
        db.add(FleetCommand(
            target_node_id=target, cmd_type='firmware_update',
            params_json=json.dumps({'update_url': 'github:x/y/' + commit,
                                    'release_hash': commit, 'channel': 'stable',
                                    'canary_pct': canary_pct}),
            issued_by=issued_by, signature='sig', status='pending'))
        db.flush()

    def test_foreign_regional_reroll_does_not_gate_other_region_node(self):
        from integrations.social.models import get_db
        app = _app()
        db = get_db()
        try:
            # Central admitted victim_node at 100%.
            self._cmd(db, target='victim_node', commit='v1',
                      canary_pct=100, issued_by='central_node')
            # A FOREIGN regional re-rolls a HOLD (canary_pct=0) but its command
            # targets only ITS OWN member, not victim_node.
            self._cmd(db, target='foreign_regional_member', commit='v1',
                      canary_pct=0, issued_by='foreign_regional')
            with patch('integrations.social.models.get_db', return_value=db):
                with app.test_client() as c:
                    r = c.get(
                        '/api/social/fleet/update-approved?v=v1&node_id=victim_node')
            # victim_node reads its OWN (central, 100%) command — still approved,
            # the foreign regional's hold did NOT leak across the region boundary.
            self.assertTrue(json.loads(r.data)['approved'])
        finally:
            db.rollback(); db.close()


class _RolloutGateCase(unittest.TestCase):
    """Harness patching require_regional to a pass-through that sets g (with a
    settable role).  The REAL gate is covered separately."""

    ROLE = 'regional'
    IS_ADMIN = False

    def setUp(self):
        from flask import g

        case = self

        def _pass_through(f):
            @wraps(f)
            def inner(*a, **k):
                g.user_id = 'roller'
                g.db = case.fake_db
                g.user = MagicMock(role=case.ROLE, is_admin=case.IS_ADMIN)
                return f(*a, **k)
            return inner

        self.fake_db = MagicMock()
        self._gate = patch('integrations.social.auth.require_regional', _pass_through)
        self._gate.start()
        self.app = _app()

    def tearDown(self):
        self._gate.stop()


class TestRolloutRouteGate(unittest.TestCase):
    def test_rollout_requires_regional(self):
        app = _app()
        fleet = MagicMock()
        with patch('integrations.social.fleet_command.FleetCommandService', fleet):
            r = app.test_client().post('/api/ota/rollout',
                                       json={'channel': 'stable', 'canary_pct': 50})
        self.assertIn(r.status_code, (401, 403))
        fleet.push_broadcast.assert_not_called()


class TestRolloutRegionalScoped(_RolloutGateCase):
    ROLE = 'regional'

    def test_regional_reroll_scopes_to_members_no_new_commit(self):
        fleet = MagicMock()
        fleet.push_broadcast.return_value = [{'id': 1}, {'id': 2}]
        members = ['loc-a', 'loc-b']
        with patch('integrations.social.fleet_command.FleetCommandService', fleet), \
             patch('integrations.social.fleet_command._get_self_node_id',
                   return_value='reg-self'), \
             patch('integrations.social.hierarchy_service.HierarchyService.region_member_node_ids',
                   return_value=members), \
             patch('integrations.social.api_fleet_update._latest_pointer_for_channel',
                   return_value={'commit': 'existing-commit', 'flake_ref': 'github:x/y/e',
                                 'channel': 'stable', 'policy': {'canary_pct': 10}}), \
             patch('security.immutable_audit_log.get_audit_log', return_value=MagicMock()):
            r = self.app.test_client().post('/api/ota/rollout',
                                            json={'channel': 'stable', 'canary_pct': 50})
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['scope'], 'region')
        self.assertEqual(data['commit'], 'existing-commit')  # re-rolls, no new commit
        self.assertEqual(data['canary_pct'], 50)
        pb = fleet.push_broadcast.call_args
        self.assertEqual(pb.kwargs['node_ids'], members)
        # The written params carry the SAME commit + the NEW canary_pct.
        self.assertEqual(pb[0][2]['release_hash'], 'existing-commit')
        self.assertEqual(pb[0][2]['canary_pct'], 50)

    def test_reroll_merges_with_existing_policy(self):
        """A partial update (just canary_pct) keeps the existing approve/deny."""
        fleet = MagicMock()
        fleet.push_broadcast.return_value = [{'id': 1}]
        with patch('integrations.social.fleet_command.FleetCommandService', fleet), \
             patch('integrations.social.fleet_command._get_self_node_id', return_value='reg'), \
             patch('integrations.social.hierarchy_service.HierarchyService.region_member_node_ids',
                   return_value=['m']), \
             patch('integrations.social.api_fleet_update._latest_pointer_for_channel',
                   return_value={'commit': 'c', 'flake_ref': 'f', 'channel': 'stable',
                                 'policy': {'canary_pct': 10, 'deny': ['banned-node']}}), \
             patch('security.immutable_audit_log.get_audit_log', return_value=MagicMock()):
            r = self.app.test_client().post('/api/ota/rollout',
                                            json={'channel': 'stable', 'canary_pct': 75})
        params = fleet.push_broadcast.call_args[0][2]
        self.assertEqual(params['canary_pct'], 75)        # updated
        self.assertEqual(params['deny'], ['banned-node'])  # preserved

    def test_reroll_rejects_when_no_pointer(self):
        fleet = MagicMock()
        with patch('integrations.social.fleet_command.FleetCommandService', fleet), \
             patch('integrations.social.api_fleet_update._latest_pointer_for_channel',
                   return_value=None):
            r = self.app.test_client().post('/api/ota/rollout',
                                            json={'channel': 'stable', 'canary_pct': 50})
        self.assertEqual(r.status_code, 400)
        fleet.push_broadcast.assert_not_called()

    def test_reroll_rejects_when_no_canary_fields(self):
        r = self.app.test_client().post('/api/ota/rollout', json={'channel': 'stable'})
        self.assertEqual(r.status_code, 400)


class TestRolloutCentralGlobal(_RolloutGateCase):
    ROLE = 'central'

    def test_central_reroll_is_global_not_scoped(self):
        fleet = MagicMock()
        fleet.push_broadcast.return_value = [{'id': 1}, {'id': 2}, {'id': 3}]
        with patch('integrations.social.fleet_command.FleetCommandService', fleet), \
             patch('integrations.social.api_fleet_update._latest_pointer_for_channel',
                   return_value={'commit': 'c', 'flake_ref': 'f', 'channel': 'stable',
                                 'policy': {}}), \
             patch('security.immutable_audit_log.get_audit_log', return_value=MagicMock()):
            r = self.app.test_client().post('/api/ota/rollout',
                                            json={'channel': 'stable', 'canary_pct': 100})
        data = json.loads(r.data)
        self.assertEqual(data['scope'], 'global')
        # Global = push_broadcast with NO node_ids scope.
        self.assertIsNone(fleet.push_broadcast.call_args.kwargs.get('node_ids'))


class TestPublishCarriesCanary(unittest.TestCase):
    """Publishing seeds the rollout policy into the firmware_update params."""

    def setUp(self):
        from flask import g

        def _pass_through(f):
            @wraps(f)
            def inner(*a, **k):
                g.user_id = 'central_steward'
                g.db = MagicMock()
                return f(*a, **k)
            return inner

        self._gate = patch('integrations.social.auth.require_central', _pass_through)
        self._gate.start()
        self.app = _app()

    def tearDown(self):
        self._gate.stop()

    def test_publish_writes_canary_into_params(self):
        fleet = MagicMock()
        fleet.push_broadcast.return_value = [{'id': 1}]
        with patch('integrations.social.fleet_command.FleetCommandService', fleet), \
             patch('integrations.agent_engine.upgrade_orchestrator.get_upgrade_orchestrator',
                   side_effect=Exception('skip')), \
             patch('security.immutable_audit_log.get_audit_log', side_effect=Exception('skip')):
            r = self.app.test_client().post('/api/ota/publish', json={
                'channel': 'stable', 'commit': 'abc',
                'canary_pct': 10, 'approve': ['vip-node'], 'deny': ['bad-node']})
        self.assertEqual(r.status_code, 200)
        params = fleet.push_broadcast.call_args[0][2]
        self.assertEqual(params['canary_pct'], 10)
        self.assertEqual(params['approve'], ['vip-node'])
        self.assertEqual(params['deny'], ['bad-node'])

    def test_publish_rejects_bad_canary_pct(self):
        fleet = MagicMock()
        with patch('integrations.social.fleet_command.FleetCommandService', fleet):
            r = self.app.test_client().post('/api/ota/publish',
                                            json={'channel': 'stable', 'commit': 'abc',
                                                  'canary_pct': 999})
        self.assertEqual(r.status_code, 400)
        fleet.push_broadcast.assert_not_called()


if __name__ == '__main__':
    unittest.main()
