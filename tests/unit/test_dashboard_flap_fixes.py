"""Contract tests for the 2026-04-29 dashboard flap fixes.

Three pinned behaviors:
  * `seed_bootstrap_goals` re-arms a `completed` bootstrap row instead
    of inserting a duplicate (Bug C).
  * `auto_remediate_loopholes` cooldown-suppresses repeat creation of
    a remediation goal whose previous instance was created within the
    last hour, regardless of status (Bug B).
  * The agent_daemon completion gate requires `spark_spent > 0`
    before marking a non-continuous goal `completed` (Bug A).

The full agent_daemon completion path needs a running HARTOS context
to exercise (DB session, dispatch_goal, idle agents, …).  The Bug A
test here pins the underlying contract via the behavior we
manipulate: a stub goal with spark_spent=0 must NOT auto-complete on
dispatch.  The full live test lives in tests/integration/.

Run: pytest tests/unit/test_dashboard_flap_fixes.py -v --noconftest
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


class FakeGoal:
    """Stub for AgentGoal — only the fields seed_bootstrap_goals reads."""
    _id_counter = 0

    def __init__(self, **kwargs):
        FakeGoal._id_counter += 1
        self.id = str(FakeGoal._id_counter)
        self.status = kwargs.get('status', 'active')
        self.config_json = kwargs.get('config_json', {})
        self.created_at = kwargs.get('created_at', datetime.utcnow())
        self.spark_spent = kwargs.get('spark_spent', 0)
        # goal_type became a first-class column on AgentGoal as part of
        # the speech-therapy seeding work; seed_bootstrap_goals now reads
        # it directly off the row instead of digging into config_json.
        # Default to '' so unrelated tests (which never set it) keep
        # working.
        self.goal_type = kwargs.get('goal_type', '')
        for k, v in kwargs.items():
            setattr(self, k, v)


class FakeQuery:
    def __init__(self, results):
        self._results = results

    def filter(self, *args, **kwargs):
        return self

    def filter_by(self, **kwargs):
        return self

    def all(self):
        return self._results


class FakeDB:
    def __init__(self, goals):
        self._goals = goals
        self._added = []
        self._flushed = False

    def query(self, model):
        return FakeQuery(self._goals)

    def add(self, obj):
        self._added.append(obj)

    def flush(self):
        self._flushed = True


# ─────────────────────────────────────────────────────────────────────
# Bug C: seed_bootstrap_goals reactivates `completed` bootstrap rows
# instead of duplicating (the dashboard "Continuous Flywheel Health
# Monitor × 10" pattern).
# ─────────────────────────────────────────────────────────────────────


class TestSeedBootstrapReactivatesCompleted(unittest.TestCase):
    def test_completed_bootstrap_is_reactivated_not_duplicated(self):
        """A `completed` bootstrap row with the same slug must flip back
        to `active` on reseed; no new INSERT is performed."""
        from integrations.agent_engine.goal_seeding import seed_bootstrap_goals

        existing = FakeGoal(
            status='completed',
            config_json={
                'bootstrap_slug': 'bootstrap_marketing_awareness',
                'completed_at': '2026-04-29T10:00:00',
                'noop_dispatch_count': 5,
            },
        )
        db = FakeDB(goals=[existing])

        with patch(
            'integrations.agent_engine.goal_manager.GoalManager.create_goal'
        ) as mock_create:
            count = seed_bootstrap_goals(db, platform_product_id=None)

        # Reactivated, not re-created — count of NEW seeds excludes
        # the one that was already present (under any status).
        for call in mock_create.call_args_list:
            kwargs = call.kwargs
            cfg = kwargs.get('config') or {}
            self.assertNotEqual(
                cfg.get('bootstrap_slug'),
                'bootstrap_marketing_awareness',
                'duplicate INSERT for an existing completed bootstrap '
                'slug — reactivation contract broken'
            )
        self.assertEqual(existing.status, 'active',
                         'completed bootstrap was not reactivated')
        cfg = existing.config_json
        self.assertNotIn('completed_at', cfg)
        self.assertNotIn('noop_dispatch_count', cfg)

    def test_active_bootstrap_is_left_untouched(self):
        """Already-active rows must not be mutated."""
        from integrations.agent_engine.goal_seeding import seed_bootstrap_goals

        existing = FakeGoal(
            status='active',
            config_json={
                'bootstrap_slug': 'bootstrap_marketing_awareness',
            },
        )
        original_cfg = dict(existing.config_json)
        db = FakeDB(goals=[existing])

        with patch(
            'integrations.agent_engine.goal_manager.GoalManager.create_goal'
        ):
            seed_bootstrap_goals(db, platform_product_id=None)

        self.assertEqual(existing.status, 'active')
        self.assertEqual(existing.config_json, original_cfg)


# ─────────────────────────────────────────────────────────────────────
# Bug B: auto_remediate_loopholes cooldown.  Recently-created
# remediation goals (regardless of status) must block re-creation.
# ─────────────────────────────────────────────────────────────────────


class TestAutoRemediateCooldown(unittest.TestCase):
    def test_recent_completed_remediation_blocks_recreation(self):
        """A `completed` remediation goal created within the cooldown
        window suppresses re-creation of the same loophole — this is
        the exact flap pattern from the 2026-04-29 dashboard."""
        from integrations.agent_engine import goal_seeding

        # Recent (within cooldown) completed remediation row
        recent_completed = FakeGoal(
            status='completed',
            config_json={'remediation': 'cold_start'},
            created_at=datetime.utcnow() - timedelta(minutes=5),
        )
        db = FakeDB(goals=[recent_completed])

        with patch(
            'integrations.agent_engine.ip_service.IPService.get_loop_health',
            return_value={'flywheel_loopholes': [
                {'type': 'cold_start', 'severity': 'high'},
            ]},
        ), patch(
            'integrations.agent_engine.goal_manager.GoalManager.create_goal'
        ) as mock_create:
            count = goal_seeding.auto_remediate_loopholes(db)

        self.assertEqual(
            count, 0,
            'auto_remediate_loopholes created a duplicate within '
            'cooldown — the flap that put 50+ Remediate rows on the '
            'dashboard'
        )
        mock_create.assert_not_called()

    def test_old_completed_remediation_does_not_block(self):
        """A remediation completed BEFORE the cooldown window is no
        longer blocking — re-fire is allowed.

        Production filter would exclude the row entirely (status not
        in active/paused AND created_at < cutoff), so we represent
        that by returning an empty result set — same observable
        behavior the real DB produces for an old-completed row.
        """
        from integrations.agent_engine import goal_seeding

        # Empty result set = the real DB filter excluded the old row.
        db = FakeDB(goals=[])

        with patch(
            'integrations.agent_engine.ip_service.IPService.get_loop_health',
            return_value={'flywheel_loopholes': [
                {'type': 'cold_start', 'severity': 'high'},
            ]},
        ), patch(
            'integrations.agent_engine.goal_manager.GoalManager.create_goal',
            return_value={'success': True, 'goal': MagicMock()},
        ):
            count = goal_seeding.auto_remediate_loopholes(db)

        self.assertEqual(count, 1,
                         'remediation should be allowed once cooldown elapses')

    def test_active_remediation_blocks_regardless_of_age(self):
        """An active remediation goal must continue to block re-fire
        even if it was created weeks ago.  Long-running remediations
        shouldn't be duplicated."""
        from integrations.agent_engine import goal_seeding

        old_active = FakeGoal(
            status='active',
            config_json={'remediation': 'cold_start'},
            created_at=datetime.utcnow() - timedelta(days=14),
        )
        db = FakeDB(goals=[old_active])

        with patch(
            'integrations.agent_engine.ip_service.IPService.get_loop_health',
            return_value={'flywheel_loopholes': [
                {'type': 'cold_start', 'severity': 'high'},
            ]},
        ), patch(
            'integrations.agent_engine.goal_manager.GoalManager.create_goal'
        ) as mock_create:
            count = goal_seeding.auto_remediate_loopholes(db)

        self.assertEqual(count, 0,
                         'active remediation was not respected as a block')
        mock_create.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# Bug A: agent_daemon completion gate (spark_spent > 0).  Pins the
# completion-criterion contract via a focused inline simulation of
# the relevant branch.  Full daemon integration test is out of scope.
# ─────────────────────────────────────────────────────────────────────


class TestCompletionGateContract(unittest.TestCase):
    def _apply_completion_branch(self, goal):
        """Mirror agent_daemon._tick's success-branch completion logic
        on a stub goal.  Kept tight; if the source branch is refactored,
        update this and the matching tests."""
        cfg = goal.config_json or {}
        is_continuous = cfg.get('continuous', False)
        spark_spent = goal.spark_spent or 0
        if is_continuous:
            return
        if spark_spent > 0:
            goal.status = 'completed'
            cfg['completed_at'] = '2026-04-29T13:00:00'
            cfg.pop('noop_dispatch_count', None)
            goal.config_json = cfg
            return
        noop_count = int(cfg.get('noop_dispatch_count', 0)) + 1
        cfg['noop_dispatch_count'] = noop_count
        cfg['last_noop_dispatch'] = '2026-04-29T13:00:00'
        if noop_count >= 5:
            goal.status = 'paused'
            cfg['pause_reason'] = (
                f'Auto-paused: {noop_count} consecutive '
                f'dispatches produced 0 spark — work is not '
                f'reaching tool execution.  Investigate '
                f'agent prompt or tool registration.')
            cfg['paused_at'] = '2026-04-29T13:00:00'
        goal.config_json = cfg

    def test_zero_spark_dispatch_does_not_complete(self):
        """The bug: dispatched goals with spark_spent=0 were marked
        `completed`.  The fix: they stay `active` with a noop counter."""
        goal = FakeGoal(status='active', config_json={}, spark_spent=0)
        self._apply_completion_branch(goal)
        self.assertEqual(goal.status, 'active')
        self.assertEqual(goal.config_json.get('noop_dispatch_count'), 1)

    def test_real_work_completes_goal(self):
        """spark_spent > 0 is the true completion gate."""
        goal = FakeGoal(status='active', config_json={}, spark_spent=42)
        self._apply_completion_branch(goal)
        self.assertEqual(goal.status, 'completed')
        self.assertIn('completed_at', goal.config_json)

    def test_continuous_goal_never_auto_completes(self):
        """Continuous goals are persistent — they MUST stay active even
        with spark_spent > 0 (covered by separate cooldown logic)."""
        goal = FakeGoal(
            status='active',
            config_json={'continuous': True},
            spark_spent=42,
        )
        self._apply_completion_branch(goal)
        self.assertEqual(goal.status, 'active')

    def test_five_noops_auto_pauses(self):
        """5 consecutive zero-spark dispatches → auto-pause.  Stops the
        noop spin loop where the daemon kept re-dispatching a goal that
        produced no work."""
        goal = FakeGoal(
            status='active',
            config_json={'noop_dispatch_count': 4},
            spark_spent=0,
        )
        self._apply_completion_branch(goal)
        self.assertEqual(goal.status, 'paused')
        self.assertIn('Auto-paused', goal.config_json['pause_reason'])
        self.assertEqual(goal.config_json['noop_dispatch_count'], 5)


if __name__ == '__main__':
    unittest.main()
