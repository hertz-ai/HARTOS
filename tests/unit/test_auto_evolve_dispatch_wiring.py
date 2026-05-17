"""Tests for Fix 1 — AutoEvolveOrchestrator parallel_dispatch + super-majority.

Guards the PRODUCT_MAP §10 contract:
  - DISPATCH stage uses a bounded ThreadPoolExecutor (not a sequential loop).
  - VOTE stage requires a 2/3 super-majority of decisive (for/against) weight
    in addition to the caller-provided min_approval_score floor.

These tests do NOT touch the DB — they call the orchestrator's internal
ranking + dispatching methods directly with synthetic candidates/tallies.
"""
import os
import sys
import time
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


class TestSuperMajorityGate(unittest.TestCase):
    """Rank-by-votes must reject candidates below the 2/3 super-majority."""

    def _fake_tally(self, for_weight, against_weight, weighted_score=None):
        # weighted_score defaults to the signed-mean which is the real service's
        # formula when abstains are absent.
        if weighted_score is None:
            total = for_weight + against_weight
            weighted_score = (
                (for_weight - against_weight) / total if total > 0 else 0.0
            )
        return {
            'total_for': for_weight,
            'total_against': against_weight,
            'weighted_score': weighted_score,
        }

    def test_rejects_simple_majority(self):
        """55% for / 45% against was admitted pre-fix — now rejected."""
        from integrations.agent_engine.auto_evolve import (
            AutoEvolveOrchestrator, EvolveSession,
            AUTO_EVOLVE_SUPERMAJORITY_RATIO,
        )
        orch = AutoEvolveOrchestrator()
        session = EvolveSession()

        candidates = [{'id': 'simple', 'title': 't', 'hypothesis': 'h'}]

        tally_call = self._fake_tally(5.5, 4.5, weighted_score=0.5)
        with patch(
            'integrations.social.thought_experiment_service.'
            'ThoughtExperimentService.tally_votes',
            return_value=tally_call,
        ), patch('integrations.social.models.db_session'):
            ranked = orch._rank_by_votes(session, candidates, min_score=0.1)

        self.assertEqual(ranked, [],
            f'simple-majority candidate should be rejected; '
            f'ratio threshold is {AUTO_EVOLVE_SUPERMAJORITY_RATIO:.3f}')

    def test_accepts_supermajority(self):
        """67% for / 33% against clears the 2/3 gate."""
        from integrations.agent_engine.auto_evolve import (
            AutoEvolveOrchestrator, EvolveSession,
        )
        orch = AutoEvolveOrchestrator()
        session = EvolveSession()

        candidates = [{'id': 'super', 'title': 't', 'hypothesis': 'h'}]

        tally = self._fake_tally(6.7, 3.3, weighted_score=0.34)
        with patch(
            'integrations.social.thought_experiment_service.'
            'ThoughtExperimentService.tally_votes',
            return_value=tally,
        ), patch('integrations.social.models.db_session'):
            ranked = orch._rank_by_votes(session, candidates, min_score=0.1)

        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]['id'], 'super')
        self.assertGreaterEqual(ranked[0]['_super_majority'], 2.0 / 3.0)

    def test_all_abstain_is_rejected(self):
        """Zero for / zero against → ratio=0 → reject (no silent pass-through)."""
        from integrations.agent_engine.auto_evolve import (
            AutoEvolveOrchestrator, EvolveSession,
        )
        orch = AutoEvolveOrchestrator()
        session = EvolveSession()

        candidates = [{'id': 'abstain', 'title': 't', 'hypothesis': 'h'}]
        tally = self._fake_tally(0, 0, weighted_score=0.9)
        with patch(
            'integrations.social.thought_experiment_service.'
            'ThoughtExperimentService.tally_votes',
            return_value=tally,
        ), patch('integrations.social.models.db_session'):
            ranked = orch._rank_by_votes(session, candidates, min_score=0.1)
        self.assertEqual(ranked, [])


class TestParallelDispatch(unittest.TestCase):
    """DISPATCH must fan-out via ThreadPoolExecutor, not sequentially."""

    def test_uses_threadpool(self):
        """Threads spawned during dispatch prove fan-out.

        We capture `threading.current_thread().name` inside the patched
        _dispatch_experiment.  If dispatch is sequential, all calls run on
        the orchestrator thread (same name).  If parallel, the names are
        prefixed 'auto-evolve-<session>'.

        FIX-1.4a patches the dispatcher down to max_workers=1 when the
        DB backend is SQLite — which is the default in CI (no
        HEVOLVE_DB_URL override).  This test is explicitly covering
        the MySQL/Postgres path, so we patch _is_sqlite_backend to
        False to keep the parallel branch active regardless of
        whichever backend the test runner happens to see.
        """
        from integrations.agent_engine.auto_evolve import (
            AutoEvolveOrchestrator, EvolveSession,
            AUTO_EVOLVE_MAX_PARALLEL_DISPATCH,
        )
        orch = AutoEvolveOrchestrator()
        session = EvolveSession()

        recorded_threads = []
        barrier = threading.Barrier(3)

        def _fake_dispatch(session, exp, user_id):
            # Block until 3 callers arrive — impossible if sequential.
            try:
                barrier.wait(timeout=2.0)
            except threading.BrokenBarrierError:
                raise RuntimeError('parallel fan-out did not happen')
            recorded_threads.append(threading.current_thread().name)
            return {'success': True, 'goal_id': f"g-{exp['id']}"}

        winners = [
            {'id': 'e1', 'title': 'a', '_approval_score': 0.9},
            {'id': 'e2', 'title': 'b', '_approval_score': 0.8},
            {'id': 'e3', 'title': 'c', '_approval_score': 0.7},
        ]
        with patch('integrations.agent_engine.auto_evolve._is_sqlite_backend',
                   return_value=False), \
             patch.object(orch, '_dispatch_experiment',
                          side_effect=_fake_dispatch):
            orch._dispatch_winners_parallel(session, winners, 'u1')

        self.assertEqual(session.dispatched, 3)
        self.assertEqual(session.failed, 0)
        self.assertEqual(len(session.experiments), 3)
        # All recorded threads should carry the executor prefix
        for name in recorded_threads:
            self.assertIn('auto-evolve-', name,
                f'thread {name!r} not from ThreadPoolExecutor — '
                f'dispatch is still sequential')
        self.assertLessEqual(AUTO_EVOLVE_MAX_PARALLEL_DISPATCH, 8,
            'Cap must stay bounded per PRODUCT_MAP §10')

    def test_one_failure_does_not_block_others(self):
        from integrations.agent_engine.auto_evolve import (
            AutoEvolveOrchestrator, EvolveSession,
        )
        orch = AutoEvolveOrchestrator()
        session = EvolveSession()

        def _fake(session, exp, user_id):
            if exp['id'] == 'bad':
                raise RuntimeError('boom')
            return {'success': True, 'goal_id': f"g-{exp['id']}"}

        winners = [
            {'id': 'good1', 'title': 'g1'},
            {'id': 'bad', 'title': 'b'},
            {'id': 'good2', 'title': 'g2'},
        ]
        # Same rationale as test_uses_threadpool: pin max_workers
        # to the parallel path so the assertion doesn't drift when
        # the runner picks up a SQLite backend.
        with patch('integrations.agent_engine.auto_evolve._is_sqlite_backend',
                   return_value=False), \
             patch.object(orch, '_dispatch_experiment', side_effect=_fake):
            orch._dispatch_winners_parallel(session, winners, 'u1')

        self.assertEqual(session.dispatched, 2)
        self.assertEqual(session.failed, 1)
        self.assertTrue(any('bad' in e for e in session.errors))


if __name__ == '__main__':
    unittest.main()
