"""Regression test for HiveBenchmarkProver._collect_results
claim-grace short-circuit (2026-05-23).

Before the fix: when no peer claimed a dispatched benchmark shard,
_collect_results polled dispatcher.get_task(task_id) every 1s for the
full `timeout` (300s default) waiting for a status transition that
never came, then fell through to local execution with zero remaining
time budget. Result: 128/128 production benchmark runs scored 0.0 in
agent_data/benchmark_leaderboard.json — making every "hive beats X"
claim in marketing materials unbacked.

After the fix: probe for `HEVOLVE_BENCHMARK_CLAIM_GRACE_SECONDS`
(default 5s).  If the task stays at PENDING the whole time, skip the
long poll and let local execution use the full remaining budget.

Tests:
  1. Pending-forever task short-circuits within grace + epsilon
     (not the full 300s timeout).
  2. Task that flips to 'assigned' during grace falls through to the
     normal polling loop (peer is working — wait for it).
  3. Task that's already 'completed' at first probe returns result
     without entering polling.
  4. _execute_shard_locally is called when no peer claims (proves the
     fallback path actually fires under the new short-circuit).

Uses ONLY mocks over the existing dispatcher — no new abstractions.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


class _FakeDispatcher:
    """Stand-in for hive_task_protocol.HiveTaskDispatcher.

    Each call to get_task() walks through self.status_sequence one
    element at a time (clamped at the last entry once exhausted).  Lets
    tests drive the exact status timeline the prover sees.
    """

    def __init__(self, status_sequence, result_payload=None):
        self.status_sequence = list(status_sequence)
        self.result_payload = result_payload
        self.get_calls = 0

    def get_task(self, task_id):
        self.get_calls += 1
        idx = min(self.get_calls - 1, len(self.status_sequence) - 1)
        status = self.status_sequence[idx]
        return SimpleNamespace(
            task_id=task_id,
            status=status,
            result=self.result_payload if status in ('completed', 'validated') else None,
        )


def _make_dispatched(task_id='t-1', shard_index=0):
    return [{
        'task_id': task_id,
        'node_id': 'peer-x',
        'shard_index': shard_index,
        'shard': {
            'shard_index': shard_index,
            'problem_count': 10,
            'problems': [],
            'shared_context': {'benchmark': 'ensemble_mmlu'},
        },
        'node_type': 'peer',
    }]


def _patch_dispatcher(fake_dispatcher):
    """Inject fake_dispatcher in the same place the prover imports it
    via integrations.coding_agent.hive_task_protocol.get_dispatcher."""
    fake_mod = SimpleNamespace(get_dispatcher=lambda: fake_dispatcher)
    return mock.patch.dict(sys.modules, {
        'integrations.coding_agent.hive_task_protocol': fake_mod,
    })


def _build_prover_with_local_stub(local_result):
    """Build a HiveBenchmarkProver-like object minimal enough to drive
    _collect_results in isolation — no DB, no ledger writes."""
    from integrations.agent_engine import hive_benchmark_prover as hbp

    prover = hbp.HiveBenchmarkProver.__new__(hbp.HiveBenchmarkProver)
    prover._ledger = SimpleNamespace(record_result=lambda **kw: None)
    prover._execute_shard_locally = mock.Mock(return_value=local_result)
    return prover


class ClaimGraceTests(unittest.TestCase):

    def setUp(self):
        # 2s grace — long enough that the inner sleep(0.5) in the
        # source's grace-probe loop fires at least 3-4 times (so a
        # peer claim mid-grace is actually observable), short enough
        # that tests stay fast.
        os.environ['HEVOLVE_BENCHMARK_CLAIM_GRACE_SECONDS'] = '2'

    def tearDown(self):
        os.environ.pop('HEVOLVE_BENCHMARK_CLAIM_GRACE_SECONDS', None)

    def test_pending_forever_short_circuits(self):
        """Task stays PENDING — must NOT poll for the full timeout."""
        dispatcher = _FakeDispatcher(['pending'] * 999)
        prover = _build_prover_with_local_stub(
            {'problems_solved': 7, 'score': 0.7})

        with _patch_dispatcher(dispatcher):
            start = time.time()
            results = prover._collect_results(
                run_id='r-1',
                dispatched=_make_dispatched(),
                timeout=300.0,   # the broken-historic default
            )
            elapsed = time.time() - start

        # Must finish well under the historic 300s timeout — proves
        # the short-circuit actually engaged.  With grace=2s the
        # expected total is ~2.5s (4 probes × 0.5s sleep); bound
        # generously against CI jitter but still well below 300.
        self.assertLess(
            elapsed, 15.0,
            f"Took {elapsed:.1f}s — short-circuit did not engage. "
            f"Before the fix this loop took the full 300s timeout."
        )
        # Local execution stub fired with non-zero problems_solved.
        self.assertEqual(results[0]['problems_solved'], 7)
        prover._execute_shard_locally.assert_called_once()

    def test_peer_claim_during_grace_falls_through_to_polling(self):
        """When a peer claims the task during grace, the long poll
        should engage (waiting for the peer to finish) rather than
        immediately going to local."""
        # PENDING for the first probe, then 'assigned', then
        # immediately 'completed' so the polling loop terminates fast.
        dispatcher = _FakeDispatcher(
            status_sequence=['pending', 'assigned', 'completed'],
            result_payload={'problems_solved': 9, 'score': 0.9},
        )
        prover = _build_prover_with_local_stub(
            {'problems_solved': 0, 'score': 0.0})

        with _patch_dispatcher(dispatcher):
            results = prover._collect_results(
                run_id='r-2',
                dispatched=_make_dispatched(),
                timeout=30.0,
            )

        # Peer result was used — local fallback NOT called.
        prover._execute_shard_locally.assert_not_called()
        self.assertEqual(results[0]['problems_solved'], 9)

    def test_completed_at_first_probe_returns_immediately(self):
        """If the dispatcher returns 'completed' immediately, we
        should not waste any time polling."""
        dispatcher = _FakeDispatcher(
            status_sequence=['completed'] * 5,
            result_payload={'problems_solved': 10, 'score': 1.0},
        )
        prover = _build_prover_with_local_stub(
            {'problems_solved': 0, 'score': 0.0})

        with _patch_dispatcher(dispatcher):
            start = time.time()
            results = prover._collect_results(
                run_id='r-3',
                dispatched=_make_dispatched(),
                timeout=30.0,
            )
            elapsed = time.time() - start

        # Sub-second since we never wait between probes when the
        # status is already terminal.
        self.assertLess(elapsed, 2.0)
        self.assertEqual(results[0]['problems_solved'], 10)
        prover._execute_shard_locally.assert_not_called()

    def test_unreachable_dispatcher_still_falls_back_to_local(self):
        """When get_dispatcher itself raises (e.g. import error in a
        partial install), we must still fall through to local
        execution rather than returning all-zero shard results."""
        prover = _build_prover_with_local_stub(
            {'problems_solved': 4, 'score': 0.4})

        # Inject a fake module whose get_dispatcher raises — same as
        # the historic source-mode HARTOS deploy where the protocol
        # module is missing.
        broken_mod = SimpleNamespace(
            get_dispatcher=mock.Mock(side_effect=RuntimeError('no dispatcher')))
        with mock.patch.dict(sys.modules, {
            'integrations.coding_agent.hive_task_protocol': broken_mod,
        }):
            results = prover._collect_results(
                run_id='r-4',
                dispatched=_make_dispatched(),
                timeout=30.0,
            )

        prover._execute_shard_locally.assert_called_once()
        self.assertEqual(results[0]['problems_solved'], 4)


if __name__ == '__main__':
    unittest.main()
