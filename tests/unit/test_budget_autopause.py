"""A goal that can never afford its next dispatch must not stay 'active'.

spark_budget is a one-shot cap set at goal creation. Nothing in the tree
replenishes it -- there is no top-up, reset or refill path anywhere. So a goal
whose remaining budget is below the next dispatch's estimate is finished, and
leaving status='active' makes the row lie: the daemon keeps selecting it, the
gate keeps refusing it, and an operator reading status sees a healthy goal.

Measured on central 2026-09-01: goal 917cc152 sat 'active' with 2 spark against
an 11-spark estimate, re-blocked on every pass; self_heal, self_build and
code_evolution sat at exactly 0 remaining, all three still 'active'.

agent_daemon DID auto-pause, but only in its own read-only pre-check, and it
estimates cost from a different prompt than the real dispatch -- so goals in
the gap between the two estimates were logged by pre_dispatch_budget_gate and
never paused by anyone. One decision, one home, both callers.

Runs standalone (`python tests/unit/test_budget_autopause.py`).
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from integrations.social.models import Base, AgentGoal
from integrations.agent_engine import budget_gate


class _DBFixture:
    """Point budget_gate's get_db at a throwaway in-memory database."""

    def __init__(self):
        self.engine = create_engine('sqlite://', echo=False,
                                    connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self._session = self.Session()

    def get_db(self):
        # one long-lived session; .close() from the code under test is a no-op
        s = self._session
        s.close = lambda: None
        return s

    def add_goal(self, gid, budget, spent, status='active', config=None):
        g = AgentGoal(id=gid, goal_type='coding', title='t',
                      status=status, spark_budget=budget, spark_spent=spent,
                      config_json=config or {})
        self._session.add(g)
        self._session.commit()
        return g

    def status_of(self, gid):
        self._session.expire_all()
        return self._session.query(AgentGoal).filter_by(id=gid).first().status

    def config_of(self, gid):
        self._session.expire_all()
        return self._session.query(AgentGoal).filter_by(id=gid).first().config_json or {}


class BudgetAutoPauseTest(unittest.TestCase):

    def setUp(self):
        self.fx = _DBFixture()
        self.patcher = patch('integrations.social.models.get_db', self.fx.get_db)
        self.patcher.start()
        budget_gate._budget_cache.clear()

    def tearDown(self):
        self.patcher.stop()

    # ── the regression ─────────────────────────────────────────────────
    def test_exhausted_goal_is_paused(self):
        self.fx.add_goal('g-exhausted', budget=200, spent=198)
        paused = budget_gate.pause_goal_for_budget(
            'g-exhausted', 'insufficient_budget (2 < 11)')
        self.assertTrue(paused)
        self.assertEqual(self.fx.status_of('g-exhausted'), 'paused')

    def test_pause_records_a_readable_reason(self):
        self.fx.add_goal('g-reason', budget=100, spent=100)
        budget_gate.pause_goal_for_budget('g-reason', 'insufficient_budget (0 < 5)')
        cfg = self.fx.config_of('g-reason')
        self.assertIn('budget gate blocked', cfg.get('pause_reason', ''))
        self.assertIn('insufficient_budget (0 < 5)', cfg.get('pause_reason', ''))
        self.assertTrue(cfg.get('paused_at'))

    # ── never_pause must finally mean something ────────────────────────
    def test_never_pause_goal_is_left_active(self):
        """The flag was set on Guardian Convergence and read by NOTHING."""
        self.fx.add_goal('g-guardian', budget=1000, spent=1000,
                         config={'never_pause': True})
        paused = budget_gate.pause_goal_for_budget('g-guardian', 'insufficient_budget (0 < 9)')
        self.assertFalse(paused)
        self.assertEqual(self.fx.status_of('g-guardian'), 'active')

    # ── guards ─────────────────────────────────────────────────────────
    def test_already_paused_goal_is_not_touched_again(self):
        self.fx.add_goal('g-paused', budget=10, spent=10, status='paused')
        self.assertFalse(budget_gate.pause_goal_for_budget('g-paused', 'x'))

    def test_completed_goal_is_not_resurrected_as_paused(self):
        self.fx.add_goal('g-done', budget=10, spent=10, status='completed')
        self.assertFalse(budget_gate.pause_goal_for_budget('g-done', 'x'))
        self.assertEqual(self.fx.status_of('g-done'), 'completed')

    def test_missing_goal_is_a_noop(self):
        self.assertFalse(budget_gate.pause_goal_for_budget('nope', 'x'))

    def test_no_goal_id_is_a_noop(self):
        self.assertFalse(budget_gate.pause_goal_for_budget(None, 'x'))

    # ── the gate wires it up ───────────────────────────────────────────
    def test_pre_dispatch_gate_pauses_on_goal_budget_block(self):
        self.fx.add_goal('g-gate', budget=200, spent=200)
        with patch.object(budget_gate, 'check_goal_budget',
                          return_value=(False, 0, 'insufficient_budget (0 < 11)')), \
             patch.object(budget_gate, 'check_platform_affordability',
                          return_value=(True, {})):
            allowed, reason = budget_gate.pre_dispatch_budget_gate('g-gate', 'prompt')
        self.assertFalse(allowed)
        self.assertIn('goal_budget_exceeded', reason)
        self.assertEqual(self.fx.status_of('g-gate'), 'paused')

    def test_allowed_dispatch_does_not_pause(self):
        self.fx.add_goal('g-ok', budget=200, spent=10)
        with patch.object(budget_gate, 'check_goal_budget',
                          return_value=(True, 190, 'budget_reserved')), \
             patch.object(budget_gate, 'check_platform_affordability',
                          return_value=(True, {})):
            allowed, _ = budget_gate.pre_dispatch_budget_gate('g-ok', 'prompt')
        self.assertTrue(allowed)
        self.assertEqual(self.fx.status_of('g-ok'), 'active')


class SingleHomeTest(unittest.TestCase):
    """agent_daemon must call the helper, not re-implement the mutation."""

    def test_daemon_delegates_and_keeps_no_inline_copy(self):
        import inspect
        from integrations.agent_engine import agent_daemon
        src = inspect.getsource(agent_daemon)
        self.assertIn('apply_budget_pause', src,
                      'daemon no longer delegates to the canonical pause')
        # The daemon must not re-implement the BUDGET pause. Its other two
        # pause sites (5 consecutive dispatch failures, 5 noop dispatches) are
        # different triggers and legitimately stay.
        start = src.index('bg_allowed = ')
        end = src.index('# GUARDRAIL: full pre-dispatch gate', start)
        budget_block = src[start:end]
        self.assertIn('apply_budget_pause', budget_block)
        self.assertNotIn("cfg['pause_reason'] = (", budget_block,
                         'daemon still carries its own inline budget pause')

    def test_daemon_uses_the_session_owning_variant_not_a_second_transaction(self):
        """The daemon holds one session across the loop and commits once at the
        end. A helper that opened its own session would add a write txn inside
        a read txn on the same SQLite file."""
        import inspect
        from integrations.agent_engine import agent_daemon
        src = inspect.getsource(agent_daemon)
        self.assertIn('apply_budget_pause', src)
        self.assertNotIn('pause_goal_for_budget', src,
                         'daemon must use the caller-commits variant')


if __name__ == '__main__':
    unittest.main(verbosity=2)
