"""Regression tests for the 6 P1 reliability fixes.

Each fix has a dedicated section.  Tests are self-contained and mock
the Redis / network / HiveMind dependencies so the suite runs without
a live cluster.
"""
import os
import sys
import time
import threading
import unittest
from unittest.mock import MagicMock, patch

# Ensure HARTOS + agent-ledger are importable
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for path in (ROOT, os.path.join(ROOT, 'agent-ledger-opensource')):
    if path not in sys.path:
        sys.path.insert(0, path)


# ────────────────────────────────────────────────────────────────────
# Fix 1 — HiveMind 0x05 PRIVATE handler
# ────────────────────────────────────────────────────────────────────
class HiveMindHandlerTest(unittest.TestCase):
    def test_malformed_payload_returns_none(self):
        from core.peer_link.hivemind_handler import handle_hivemind_message
        self.assertIsNone(handle_hivemind_message('not a dict', 'peer_x'))
        self.assertIsNone(handle_hivemind_message(None, 'peer_x'))

    def test_unknown_type_returns_none(self):
        from core.peer_link.hivemind_handler import handle_hivemind_message
        self.assertIsNone(
            handle_hivemind_message({'type': 'nonsense'}, 'peer_x'))

    def test_query_returns_reply_shape(self):
        """Even when HiveMind isn't loaded, query must return a dict with
        'thought' so the requesting peer's collect() sees a structured
        response rather than hanging on timeout."""
        from core.peer_link.hivemind_handler import (
            handle_hivemind_message, _EMPTY_REPLY,
        )
        result = handle_hivemind_message(
            {'type': 'query', 'query': 'hello'}, 'peer_x')
        self.assertIsInstance(result, dict)
        self.assertIn('thought', result)

    def test_deliver_missing_target_returns_ack_invalid(self):
        from core.peer_link.hivemind_handler import handle_hivemind_message
        result = handle_hivemind_message(
            {'type': 'deliver', 'message': {'x': 1}}, 'peer_x')
        self.assertEqual(result.get('delivered'), False)
        self.assertEqual(result.get('reason'), 'invalid_payload')

    def test_deliver_unknown_agent_returns_ack_unknown(self):
        from core.peer_link.hivemind_handler import handle_hivemind_message
        result = handle_hivemind_message(
            {'type': 'deliver', 'target_agent_id': 'ghost',
             'message': {'kind': 'ping'}},
            'peer_x')
        self.assertIsInstance(result, dict)
        # Could be unknown_agent OR no_coordinator/no_ledger depending on
        # the test env — all are "delivered=False" with a reason.
        self.assertFalse(result.get('delivered'))
        self.assertIn(result.get('reason'),
                      {'unknown_agent', 'no_coordinator', 'no_ledger'})

    def test_bootstrap_is_idempotent(self):
        from core.peer_link.hivemind_handler import bootstrap_hivemind_handler
        first = bootstrap_hivemind_handler()
        second = bootstrap_hivemind_handler()
        # First boot may fail silently if peer_link is missing, but the
        # second call must never claim to have registered again.
        if first:
            self.assertFalse(second)


# ────────────────────────────────────────────────────────────────────
# Fix 2 — HIVE_DEPTH constant + enforcement
# ────────────────────────────────────────────────────────────────────
class HiveDepthTest(unittest.TestCase):
    def test_constant_is_three(self):
        from core.constants import HIVE_DEPTH
        self.assertEqual(HIVE_DEPTH, 3)

    def test_submit_goal_rejects_over_depth(self):
        from integrations.distributed_agent.task_coordinator import (
            DistributedTaskCoordinator, HiveDepthExceeded,
        )
        from agent_ledger.core import SmartLedger
        from agent_ledger.distributed import DistributedTaskLock

        ledger = SmartLedger(agent_id='test', session_id='s1')
        fake_redis = MagicMock()
        lock = DistributedTaskLock(fake_redis)
        coord = DistributedTaskCoordinator(ledger, lock)

        with self.assertRaises(HiveDepthExceeded):
            coord.submit_goal(
                objective='over-depth goal',
                decomposed_tasks=[{'description': 'x'}],
                context={'hop': 3},
            )

    def test_submit_goal_accepts_at_depth_minus_one(self):
        from integrations.distributed_agent.task_coordinator import (
            DistributedTaskCoordinator,
        )
        from agent_ledger.core import SmartLedger
        from agent_ledger.distributed import DistributedTaskLock

        ledger = SmartLedger(agent_id='test', session_id='s2')
        fake_redis = MagicMock()
        lock = DistributedTaskLock(fake_redis)
        coord = DistributedTaskCoordinator(ledger, lock)

        # hop=2 (one less than HIVE_DEPTH=3) is legal.
        goal_id = coord.submit_goal(
            objective='legal',
            decomposed_tasks=[{'description': 'x'}],
            context={'hop': 2},
        )
        self.assertIsNotNone(goal_id)
        task = ledger.get_task(goal_id)
        self.assertEqual(task.context.get('hop'), 2)


# ────────────────────────────────────────────────────────────────────
# Fix 3 — DistributedTaskLock TTL renewal heartbeat
# ────────────────────────────────────────────────────────────────────
class DistributedTaskLockHeartbeatTest(unittest.TestCase):
    def test_renew_calls_expire_via_lua(self):
        from agent_ledger.distributed import DistributedTaskLock
        fake_redis = MagicMock()
        fake_redis.eval.return_value = 1
        lock = DistributedTaskLock(fake_redis)
        ok = lock.renew('task_A', 'agent_1', ttl=120)
        self.assertTrue(ok)
        self.assertTrue(fake_redis.eval.called)

    def test_renew_returns_false_when_not_owner(self):
        from agent_ledger.distributed import DistributedTaskLock
        fake_redis = MagicMock()
        fake_redis.eval.return_value = 0  # Lua returns 0 = not owner
        lock = DistributedTaskLock(fake_redis)
        self.assertFalse(lock.renew('task_A', 'other_agent'))

    def test_start_stop_heartbeat(self):
        from agent_ledger.distributed import DistributedTaskLock
        fake_redis = MagicMock()
        fake_redis.eval.return_value = 1
        lock = DistributedTaskLock(fake_redis)
        # Drop the interval so the loop fires quickly under test.
        lock.HEARTBEAT_INTERVAL = 0.1
        self.assertTrue(lock.start_heartbeat('task_B', 'agent_2', ttl=30))
        # Second start for same pair is a no-op.
        self.assertFalse(lock.start_heartbeat('task_B', 'agent_2'))
        # Give the loop time to call renew at least once.
        time.sleep(0.3)
        self.assertTrue(fake_redis.eval.call_count >= 1)
        lock.stop_all_heartbeats()

    def test_release_stops_heartbeat(self):
        from agent_ledger.distributed import DistributedTaskLock
        fake_redis = MagicMock()
        fake_redis.eval.return_value = 1
        lock = DistributedTaskLock(fake_redis)
        lock.HEARTBEAT_INTERVAL = 0.1
        lock.start_heartbeat('task_C', 'agent_3', ttl=30)
        lock.release_task('task_C', 'agent_3')
        # After release, the heartbeat entry is gone.
        self.assertNotIn(('task_C', 'agent_3'), lock._heartbeats)
        lock.stop_all_heartbeats()


# ────────────────────────────────────────────────────────────────────
# Fix 4 — Redis backoff on ConnectionError
# ────────────────────────────────────────────────────────────────────
class WorkerLoopBackoffTest(unittest.TestCase):
    def test_backoff_starts_at_min_and_caps_at_max(self):
        from integrations.distributed_agent.worker_loop import (
            DistributedWorkerLoop,
        )
        w = DistributedWorkerLoop()
        self.assertEqual(w._redis_backoff, 0.0)
        w._bump_redis_backoff()
        self.assertEqual(w._redis_backoff, w._BACKOFF_MIN)
        # Bump repeatedly — must cap at MAX.
        for _ in range(20):
            w._bump_redis_backoff()
        self.assertEqual(w._redis_backoff, w._BACKOFF_MAX)


# ────────────────────────────────────────────────────────────────────
# Fix 5 — HiveBenchmarkProver idempotency
# ────────────────────────────────────────────────────────────────────
class HiveBenchmarkProverIdempotencyTest(unittest.TestCase):
    def setUp(self):
        from integrations.agent_engine.hive_benchmark_prover import (
            HiveBenchmarkProver,
        )
        self.prover = HiveBenchmarkProver()
        # Seed an active run so on_shard_result doesn't bail on
        # "unknown run_id".
        self.run_id = 'run_idem'
        self.prover._active_runs[self.run_id] = {
            'benchmark': 'mmlu',
            'shards': [],
            'dispatched': [],
            'results': {},
            'total_shards': 1,
            'completed_shards': 0,
            'start_time': 0,
            'config': {},
            'status': 'running',
        }

    def test_idempotent_replay_returns_marker(self):
        key = 'dup-key-1'
        r1 = self.prover.on_shard_result(
            self.run_id, 'task_1',
            {'score': 0.9, 'problems_solved': 10},
            idempotency_key=key,
        )
        r2 = self.prover.on_shard_result(
            self.run_id, 'task_1',
            {'score': 0.9, 'problems_solved': 10},
            idempotency_key=key,
        )
        self.assertIsInstance(r2, dict)
        self.assertTrue(r2.get('idempotent_replay'))

    def test_no_key_always_records(self):
        r1 = self.prover.on_shard_result(
            self.run_id, 'task_A',
            {'score': 0.9, 'problems_solved': 10},
        )
        # Second call with no key is NOT considered a replay.
        self.prover._active_runs[self.run_id]['total_shards'] = 2
        r2 = self.prover.on_shard_result(
            self.run_id, 'task_B',
            {'score': 0.9, 'problems_solved': 10},
        )
        if isinstance(r2, dict):
            self.assertFalse(r2.get('idempotent_replay', False))

    def test_cache_bounded(self):
        # Fill past the cap; oldest must be evicted.
        self.prover._IDEMPOTENCY_CACHE_MAX = 3
        for i in range(10):
            self.prover.on_shard_result(
                self.run_id, f'task_fill_{i}',
                {'score': 0.5, 'problems_solved': 1},
                idempotency_key=f'fill_{i}',
            )
        self.assertLessEqual(
            len(self.prover._idempotency_cache), 3)


# ────────────────────────────────────────────────────────────────────
# Fix 6 — Federation silent-fail logging + alarm
# ────────────────────────────────────────────────────────────────────
class FederatedAggregatorAlarmTest(unittest.TestCase):
    def test_consecutive_failures_flip_alarm_at_threshold(self):
        from integrations.agent_engine.federated_aggregator import (
            FederatedAggregator,
        )
        agg = FederatedAggregator()

        def boom(*_, **__):
            raise RuntimeError('simulated peer outage')

        # Force every tick to fail for 3 rounds.
        agg.extract_local_delta = boom  # type: ignore[method-assign]
        for _ in range(FederatedAggregator._CONSECUTIVE_FAILURE_ALARM - 1):
            result = agg.tick()
            self.assertIn('error', result)
            self.assertFalse(result.get('alarm'))
        # Threshold hit.
        result = agg.tick()
        self.assertTrue(result.get('alarm'))
        self.assertGreaterEqual(
            result.get('consecutive_failures', 0),
            FederatedAggregator._CONSECUTIVE_FAILURE_ALARM,
        )

    def test_success_resets_consecutive_counter(self):
        from integrations.agent_engine.federated_aggregator import (
            FederatedAggregator,
        )
        agg = FederatedAggregator()
        agg._consecutive_tick_failures = 5

        # Replace inner calls so tick succeeds without hitting real peers.
        agg.extract_local_delta = lambda: None  # type: ignore[method-assign]
        agg.aggregate = lambda: None  # type: ignore[method-assign]
        agg.embedding_tick = lambda: {'aggregated': False}  # type: ignore[method-assign]
        agg.resonance_tick = lambda: {'aggregated': False}  # type: ignore[method-assign]

        agg.tick()
        self.assertEqual(agg._consecutive_tick_failures, 0)


if __name__ == '__main__':
    unittest.main()
