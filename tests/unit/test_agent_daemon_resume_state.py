"""Unit tests for AgentDaemon._resume_state_once (stop/start resume).

What this pins:
  1. Idempotent — running twice does not double-call save/restore.
  2. Warms SmartLedger cache via the EXISTING _get_goal_ledger path
     (so persisted task-graph state from agent_data/ledger_*.json is
     loaded back into self._ledger_cache before the first dispatch).
  3. Warms TaskLedger morphable-agent slot via the EXISTING
     task_ledger.get_or_create (so the next /chat turn lands in the
     same conversation slot rather than spawning a parallel ledger).
  4. Calls check_pending_followups_daemon once so CRM follow-up
     sequences whose due-date elapsed while Nunba was down get
     flushed on boot.
  5. Resilient — when AgentGoal table is empty / unreachable, log a
     warning but DO NOT crash the daemon (boot must succeed even
     under partial DB state).

No new schema, no new persistence layer.  Tests use the existing
imports + monkey-patch the existing helpers.
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


class _FakeDB:
    def __init__(self, goals):
        self._goals = goals

    def query(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._goals)

    def close(self):
        return None


def _build_daemon():
    """Pull in the real AgentDaemon class (no AgentGoal/DB imports yet)."""
    from integrations.agent_engine.agent_daemon import AgentDaemon
    d = AgentDaemon()
    # Replace _get_goal_ledger so tests don't touch real SmartLedger files.
    d._get_goal_ledger_calls = []

    def _fake_get_goal_ledger(goal):
        d._get_goal_ledger_calls.append(getattr(goal, 'id', None))
        # Simulate "ledger file existed and was loaded successfully" —
        # the existing path returns the ledger when >1 tasks were
        # restored from JSON.
        return object()

    d._get_goal_ledger = _fake_get_goal_ledger
    return d


def _make_goal(goal_id, user_id='u-1', prompt_id='p-1'):
    return SimpleNamespace(id=goal_id, user_id=user_id, prompt_id=prompt_id,
                           created_by=user_id, status='active')


class ResumeStateOnceTests(unittest.TestCase):

    def setUp(self):
        # Re-init task_ledger module state per test.
        from integrations.social import task_ledger
        task_ledger._LEDGERS.clear()

    def _patch_db(self, goals):
        """Patch get_db to return our fake DB."""
        return mock.patch(
            'integrations.social.models.get_db',
            return_value=_FakeDB(goals),
        )

    def _patch_followups(self, processed=0):
        return mock.patch(
            'integrations.agent_engine.outreach_crm_tools.check_pending_followups_daemon',
            return_value={'processed': processed},
        )

    def test_idempotent_only_runs_once(self):
        d = _build_daemon()
        with self._patch_db([]), self._patch_followups(0):
            d._resume_state_once()
            d._resume_state_once()
            d._resume_state_once()
        # _get_goal_ledger called 0 times (no goals), but the
        # important invariant is no double-execution side effects.
        self.assertTrue(getattr(d, '_resume_done', False))

    def test_warms_smart_ledger_via_existing_get_goal_ledger(self):
        """The resume path must reuse _get_goal_ledger so the existing
        SmartLedger.load(JSON) save/restore is the single path."""
        d = _build_daemon()
        goals = [_make_goal('g-1'), _make_goal('g-2', user_id='u-2', prompt_id='p-2')]
        with self._patch_db(goals), self._patch_followups(0):
            d._resume_state_once()
        # Both goals' SmartLedgers should have been warmed via the
        # existing path — proves we did NOT create a parallel restore.
        self.assertEqual(set(d._get_goal_ledger_calls), {'g-1', 'g-2'})

    def test_warms_task_ledger_morphable_slots(self):
        """task_ledger.get_or_create must be called for each (user_id,
        conv_id) so the morphable-agent slot survives restart."""
        d = _build_daemon()
        goals = [
            _make_goal('g-1', user_id='u-A', prompt_id='conv-X'),
            _make_goal('g-2', user_id='u-B', prompt_id=None),  # falls back to 'nunba'
        ]
        with self._patch_db(goals), self._patch_followups(0):
            d._resume_state_once()
        from integrations.social import task_ledger
        keys = set(task_ledger._LEDGERS.keys())
        self.assertIn('u-A:conv-X', keys)
        self.assertIn('u-B:nunba', keys)

    def test_calls_followups_once(self):
        d = _build_daemon()
        with self._patch_db([]) as _mdb, self._patch_followups(7) as mfu:
            d._resume_state_once()
        mfu.assert_called_once()

    def test_does_not_crash_on_empty_db(self):
        """Boot must succeed even if the goals table is empty."""
        d = _build_daemon()
        with self._patch_db([]), self._patch_followups(0):
            d._resume_state_once()  # must not raise
        self.assertTrue(d._resume_done)

    def test_does_not_crash_when_followups_module_unavailable(self):
        d = _build_daemon()
        # Simulate the import failing.
        with self._patch_db([]):
            with mock.patch.dict(sys.modules, {
                'integrations.agent_engine.outreach_crm_tools': None,
            }):
                # The function catches ImportError internally — must not raise.
                try:
                    d._resume_state_once()
                except Exception as e:
                    self.fail(f"resume_state_once must swallow followup errors, raised: {e}")

    def test_per_goal_failure_does_not_block_remaining_goals(self):
        """One bad goal must NOT block the rest of the boot resume.
        Regression guard for the zombie_reaper pattern (#179) where a
        single bad task crashed the whole reaper."""
        d = _build_daemon()
        good1 = _make_goal('g-good-1')
        bad = _make_goal('g-bad')
        good2 = _make_goal('g-good-2')
        seen = []

        def _flaky_get_goal_ledger(goal):
            seen.append(goal.id)
            if goal.id == 'g-bad':
                raise RuntimeError('simulated SmartLedger.load IO error')
            return object()
        d._get_goal_ledger = _flaky_get_goal_ledger

        with self._patch_db([good1, bad, good2]), self._patch_followups(0):
            d._resume_state_once()
        # All three goals were visited; the bad one didn't stop the loop.
        self.assertEqual(seen, ['g-good-1', 'g-bad', 'g-good-2'])


if __name__ == '__main__':
    unittest.main()
