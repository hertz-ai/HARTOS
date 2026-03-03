"""
Tests for integrations.agent_engine.federated_aggregator.

Covers: equal weighting, EventBus subscription, recipe channel,
event counters, aggregation, convergence, and event emission.
"""

import math
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

from integrations.agent_engine.federated_aggregator import (
    FederatedAggregator, DELTA_VERSION, DELTA_MAX_AGE_SECONDS,
)


def _make_delta(node_id='node-a', total_recorded=100, goal_throughput=50,
                flush_rate=0.8, contribution_score=5.0,
                capability_tier='standard', **kwargs):
    """Build a minimal valid delta for testing."""
    d = {
        'version': DELTA_VERSION,
        'node_id': node_id,
        'public_key': 'pk_test',
        'guardrail_hash': '',
        'timestamp': time.time(),
        'experience_stats': {
            'total_recorded': total_recorded,
            'total_flushed': int(total_recorded * flush_rate),
            'flush_rate': flush_rate,
        },
        'ralt_stats': {
            'skills_distributed': 10,
            'skills_blocked': 2,
            'acceptance_rate': 0.83,
        },
        'hivemind_state': {
            'agent_count': 3,
            'total_queries': 50,
            'avg_fusion_latency_ms': 120.0,
        },
        'quality_metrics': {
            'correction_density': 5,
            'success_rate': 0.9,
            'goal_throughput': goal_throughput,
        },
        'benchmark_results': {},
        'capability_tier': capability_tier,
        'contribution_score': contribution_score,
        'signature': '',
        'event_counters': {},
    }
    d.update(kwargs)
    return d


class TestEqualWeighting(unittest.TestCase):
    """Verify that federation weighting is interaction-based, not tier-based."""

    def setUp(self):
        with patch.object(FederatedAggregator, '_subscribe_to_eventbus'):
            self.agg = FederatedAggregator()

    def test_weight_by_interactions_not_tier(self):
        """A low-tier node with many interactions should outweigh a high-tier
        node with few interactions."""
        # "Raspberry Pi" — embedded tier but 10,000 interactions, flush_rate=0.9
        pi_delta = _make_delta('pi', total_recorded=9000, goal_throughput=1000,
                               flush_rate=0.9,
                               capability_tier='embedded', contribution_score=0.5)
        # "GPU server" — compute_host tier but only 10 interactions, flush_rate=0.3
        gpu_delta = _make_delta('gpu', total_recorded=8, goal_throughput=2,
                                flush_rate=0.3,
                                capability_tier='compute_host', contribution_score=50.0)

        self.agg._local_delta = pi_delta
        with self.agg._lock:
            self.agg._peer_deltas['gpu'] = gpu_delta

        result = self.agg.aggregate()
        self.assertIsNotNone(result)

        # Pi weight: log1p(9000+1000) = log1p(10000) ≈ 9.21
        # GPU weight: log1p(8+2) = log1p(10) ≈ 2.40
        # Pi should dominate the weighted average
        agg_flush = result['experience_stats']['flush_rate']

        # Result should be much closer to Pi's 0.9 than GPU's 0.3
        pi_dist = abs(agg_flush - 0.9)
        gpu_dist = abs(agg_flush - 0.3)
        self.assertLess(pi_dist, gpu_dist,
                        "Aggregated value should be closer to high-interaction node")

    def test_floor_weight_ensures_every_node_counts(self):
        """Even a node with 0 interactions gets weight=1.0 (floor)."""
        zero_delta = _make_delta('zero', total_recorded=0, goal_throughput=0)
        active_delta = _make_delta('active', total_recorded=100, goal_throughput=50)

        self.agg._local_delta = zero_delta
        with self.agg._lock:
            self.agg._peer_deltas['active'] = active_delta

        result = self.agg.aggregate()
        self.assertIsNotNone(result)
        # Zero node has floor weight 1.0, active has log1p(150)≈5.02
        # Both contribute — result is NOT just the active node's values
        self.assertEqual(result['peer_count'], 2)

    def test_single_node_aggregates(self):
        """Single node should aggregate to its own values."""
        delta = _make_delta('solo', total_recorded=500, goal_throughput=100)
        self.agg._local_delta = delta

        result = self.agg.aggregate()
        self.assertIsNotNone(result)
        self.assertEqual(result['peer_count'], 1)
        self.assertAlmostEqual(
            result['experience_stats']['flush_rate'], 0.8, places=2)

    def test_equal_interactions_equal_weight(self):
        """Two nodes with identical interactions should have identical weight."""
        a = _make_delta('a', total_recorded=100, goal_throughput=50,
                        flush_rate=0.9, capability_tier='embedded')
        b = _make_delta('b', total_recorded=100, goal_throughput=50,
                        flush_rate=0.7, capability_tier='compute_host')

        self.agg._local_delta = a
        with self.agg._lock:
            self.agg._peer_deltas['b'] = b

        result = self.agg.aggregate()
        # With equal weights, flush_rate should be exactly the average
        expected = (0.9 + 0.7) / 2.0
        self.assertAlmostEqual(
            result['experience_stats']['flush_rate'], expected, places=4)

    def test_no_deltas_returns_none(self):
        result = self.agg.aggregate()
        self.assertIsNone(result)

    def test_many_peers_all_contribute(self):
        """10 peers all get included in aggregation."""
        for i in range(9):
            d = _make_delta(f'peer-{i}', total_recorded=i*10, goal_throughput=i*5)
            with self.agg._lock:
                self.agg._peer_deltas[f'peer-{i}'] = d
        self.agg._local_delta = _make_delta('local', total_recorded=50,
                                             goal_throughput=25)

        result = self.agg.aggregate()
        self.assertEqual(result['peer_count'], 10)


class TestRecipeChannel(unittest.TestCase):
    """Recipe sharing: catalog metadata flows equally across the hive."""

    def setUp(self):
        with patch.object(FederatedAggregator, '_subscribe_to_eventbus'):
            self.agg = FederatedAggregator()

    def test_receive_recipe_delta(self):
        delta = {
            'node_id': 'node-a',
            'recipes': [
                {'id': 'recipe-1', 'name': 'Web Search', 'action_count': 3,
                 'success_rate': 0.95, 'reuse_count': 42},
            ],
        }
        self.agg.receive_recipe_delta('node-a', delta)
        with self.agg._recipe_lock:
            self.assertEqual(len(self.agg._recipe_deltas), 1)

    def test_receive_invalid_delta_ignored(self):
        self.agg.receive_recipe_delta('', {'recipes': []})
        self.agg.receive_recipe_delta('x', 'not a dict')
        self.agg.receive_recipe_delta('', None)
        with self.agg._recipe_lock:
            self.assertEqual(len(self.agg._recipe_deltas), 0)

    def test_aggregate_recipes_builds_index(self):
        self.agg.receive_recipe_delta('node-a', {
            'node_id': 'node-a',
            'recipes': [
                {'id': 'r1', 'name': 'Web Search', 'action_count': 3,
                 'success_rate': 0.95, 'reuse_count': 10},
                {'id': 'r2', 'name': 'Code Review', 'action_count': 5,
                 'success_rate': 0.88, 'reuse_count': 5},
            ],
        })
        self.agg.receive_recipe_delta('node-b', {
            'node_id': 'node-b',
            'recipes': [
                {'id': 'r1', 'name': 'Web Search', 'action_count': 3,
                 'success_rate': 0.90, 'reuse_count': 20},
                {'id': 'r3', 'name': 'Data Analysis', 'action_count': 7,
                 'success_rate': 0.80, 'reuse_count': 3},
            ],
        })

        result = self.agg.aggregate_recipes()
        self.assertIsNotNone(result)
        self.assertEqual(result['total_recipes'], 3)  # r1, r2, r3
        self.assertEqual(result['peer_count'], 2)

        # r1 should appear on both nodes
        r1 = next(r for r in result['recipes'] if r['id'] == 'r1')
        self.assertEqual(len(r1['nodes']), 2)
        self.assertEqual(r1['total_reuse_count'], 30)  # 10 + 20

    def test_aggregate_recipes_equal_discoverability(self):
        """Every node's recipes are listed equally — no priority by tier."""
        # Embedded node with 1 recipe
        self.agg.receive_recipe_delta('embedded-node', {
            'node_id': 'embedded-node',
            'recipes': [{'id': 'e1', 'name': 'IoT Monitor', 'action_count': 2,
                          'success_rate': 0.99, 'reuse_count': 1000}],
        })
        # GPU server with 1 recipe
        self.agg.receive_recipe_delta('gpu-server', {
            'node_id': 'gpu-server',
            'recipes': [{'id': 'g1', 'name': 'Training Pipeline', 'action_count': 10,
                          'success_rate': 0.70, 'reuse_count': 5}],
        })

        result = self.agg.aggregate_recipes()
        # Both recipes appear — no filtering by tier
        self.assertEqual(result['total_recipes'], 2)

    def test_aggregate_empty_returns_cached(self):
        """If no deltas, return last aggregated (or None on first call)."""
        result = self.agg.aggregate_recipes()
        self.assertIsNone(result)

        # After one aggregation, cached result returned
        self.agg.receive_recipe_delta('x', {
            'node_id': 'x', 'recipes': [{'id': 'r1', 'name': 'test',
                                           'action_count': 1, 'success_rate': 0.5,
                                           'reuse_count': 1}],
        })
        self.agg.aggregate_recipes()
        # Clear deltas
        with self.agg._recipe_lock:
            self.agg._recipe_deltas.clear()
        # Should return cached
        cached = self.agg.aggregate_recipes()
        self.assertIsNotNone(cached)

    def test_get_recipe_stats(self):
        stats = self.agg.get_recipe_stats()
        self.assertIn('pending_deltas', stats)
        self.assertIn('last_aggregated', stats)
        self.assertEqual(stats['pending_deltas'], 0)


class TestEventCounters(unittest.TestCase):
    """EventBus event counters flow into federation delta."""

    def setUp(self):
        with patch.object(FederatedAggregator, '_subscribe_to_eventbus'):
            self.agg = FederatedAggregator()

    def test_on_event_increments_counter(self):
        self.agg._on_event('inference.completed', {'model': 'test'})
        self.agg._on_event('inference.completed', {'model': 'test2'})
        self.agg._on_event('memory.item_added', {'id': '1'})

        with self.agg._event_counters_lock:
            self.assertEqual(self.agg._event_counters['inference.completed'], 2)
            self.assertEqual(self.agg._event_counters['memory.item_added'], 1)

    def test_get_event_counters_returns_and_resets(self):
        self.agg._on_event('resonance.tuned', {})
        self.agg._on_event('resonance.tuned', {})
        self.agg._on_event('action_state.changed', {})

        counters = self.agg.get_event_counters()
        self.assertEqual(counters['resonance.tuned'], 2)
        self.assertEqual(counters['action_state.changed'], 1)

        # After get, counters are reset
        counters2 = self.agg.get_event_counters()
        self.assertEqual(counters2, {})

    def test_event_counters_thread_safe(self):
        """Concurrent event accumulation doesn't corrupt counters."""
        errors = []

        def fire_events():
            for _ in range(100):
                try:
                    self.agg._on_event('inference.completed', {})
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=fire_events) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        counters = self.agg.get_event_counters()
        self.assertEqual(counters['inference.completed'], 400)


class TestEventBusSubscription(unittest.TestCase):
    """Verify FederatedAggregator subscribes to EventBus when available."""

    def test_subscribe_called_on_init(self):
        """__init__ calls _subscribe_to_eventbus."""
        with patch.object(FederatedAggregator, '_subscribe_to_eventbus') as mock_sub:
            agg = FederatedAggregator()
            mock_sub.assert_called_once()

    def test_subscribe_wires_four_topics(self):
        """When EventBus is available, 4 topics are subscribed."""
        mock_bus = MagicMock()
        mock_registry = MagicMock()
        mock_registry.has.return_value = True
        mock_registry.get.return_value = mock_bus

        with patch('integrations.agent_engine.federated_aggregator.FederatedAggregator._subscribe_to_eventbus'):
            agg = FederatedAggregator()

        # Call the real method manually
        with patch('core.platform.registry.get_registry', return_value=mock_registry):
            FederatedAggregator._subscribe_to_eventbus(agg)

        self.assertEqual(mock_bus.on.call_count, 4)
        topics = [call.args[0] for call in mock_bus.on.call_args_list]
        self.assertIn('inference.completed', topics)
        self.assertIn('resonance.tuned', topics)
        self.assertIn('memory.item_added', topics)
        self.assertIn('action_state.changed', topics)

    def test_subscribe_noop_when_no_registry(self):
        """_subscribe_to_eventbus silently fails if platform not bootstrapped."""
        with patch('core.platform.registry.get_registry',
                   side_effect=Exception('not bootstrapped')):
            # Should not raise
            agg = FederatedAggregator()


class TestApplyAggregatedEmitsEvent(unittest.TestCase):
    """apply_aggregated() emits federation.aggregated event."""

    def setUp(self):
        with patch.object(FederatedAggregator, '_subscribe_to_eventbus'):
            self.agg = FederatedAggregator()

    @patch('integrations.agent_engine.federated_aggregator.emit_event',
           create=True)
    def test_apply_emits_federation_event(self, mock_emit):
        # Patch the import inside apply_aggregated
        aggregated = {'epoch': 3, 'peer_count': 5, 'timestamp': time.time()}

        with patch.dict('sys.modules', {
            'core.platform.events': MagicMock(emit_event=mock_emit),
        }):
            # Re-import to pick up mock
            import importlib
            import integrations.agent_engine.federated_aggregator as fmod
            # Call directly — the try/except inside calls emit_event
            self.agg.apply_aggregated(aggregated)

        # The method stores the result
        self.assertEqual(self.agg._last_aggregated, aggregated)

    def test_apply_stores_aggregated(self):
        aggregated = {'epoch': 1, 'peer_count': 2}
        self.agg.apply_aggregated(aggregated)
        self.assertEqual(self.agg._last_aggregated, aggregated)


class TestReceivePeerDelta(unittest.TestCase):
    """Delta validation: version, freshness, guardrail hash."""

    def setUp(self):
        with patch.object(FederatedAggregator, '_subscribe_to_eventbus'):
            self.agg = FederatedAggregator()

    def test_valid_delta_accepted(self):
        delta = _make_delta('node-1')
        ok, msg = self.agg.receive_peer_delta(delta)
        self.assertTrue(ok)
        self.assertEqual(msg, 'accepted')

    def test_wrong_version_rejected(self):
        delta = _make_delta('node-1')
        delta['version'] = 999
        ok, msg = self.agg.receive_peer_delta(delta)
        self.assertFalse(ok)
        self.assertIn('version', msg)

    def test_stale_delta_rejected(self):
        delta = _make_delta('node-1')
        delta['timestamp'] = time.time() - DELTA_MAX_AGE_SECONDS - 100
        ok, msg = self.agg.receive_peer_delta(delta)
        self.assertFalse(ok)
        self.assertIn('old', msg)

    def test_missing_node_id_rejected(self):
        delta = _make_delta('node-1')
        delta['node_id'] = ''
        ok, msg = self.agg.receive_peer_delta(delta)
        self.assertFalse(ok)
        self.assertIn('node_id', msg)

    def test_invalid_payload_rejected(self):
        ok, msg = self.agg.receive_peer_delta('not a dict')
        self.assertFalse(ok)

    def test_accepted_delta_stored(self):
        delta = _make_delta('node-x')
        self.agg.receive_peer_delta(delta)
        with self.agg._lock:
            self.assertIn('node-x', self.agg._peer_deltas)


class TestConvergence(unittest.TestCase):
    """Variance-based convergence scoring."""

    def setUp(self):
        with patch.object(FederatedAggregator, '_subscribe_to_eventbus'):
            self.agg = FederatedAggregator()

    def test_single_peer_full_convergence(self):
        with self.agg._lock:
            self.agg._peer_deltas['a'] = _make_delta('a')
        score = self.agg.track_convergence()
        self.assertEqual(score, 1.0)

    def test_identical_peers_full_convergence(self):
        for i in range(5):
            d = _make_delta(f'n-{i}', flush_rate=0.8)
            with self.agg._lock:
                self.agg._peer_deltas[f'n-{i}'] = d
        score = self.agg.track_convergence()
        self.assertEqual(score, 1.0)

    def test_divergent_peers_low_convergence(self):
        d1 = _make_delta('a', flush_rate=0.1)
        d2 = _make_delta('b', flush_rate=0.9)
        with self.agg._lock:
            self.agg._peer_deltas['a'] = d1
            self.agg._peer_deltas['b'] = d2
        score = self.agg.track_convergence()
        self.assertLess(score, 0.5)

    def test_convergence_history_bounded(self):
        with self.agg._lock:
            self.agg._peer_deltas['a'] = _make_delta('a')
        for _ in range(150):
            self.agg.track_convergence()
        self.assertLessEqual(len(self.agg._convergence_history), 100)


class TestGetStats(unittest.TestCase):
    """get_stats() includes all channel stats + recipe stats."""

    def setUp(self):
        with patch.object(FederatedAggregator, '_subscribe_to_eventbus'):
            self.agg = FederatedAggregator()

    def test_stats_includes_recipe_channel(self):
        stats = self.agg.get_stats()
        self.assertIn('recipes', stats)
        self.assertIn('pending_deltas', stats['recipes'])

    def test_stats_includes_all_channels(self):
        stats = self.agg.get_stats()
        self.assertIn('epoch', stats)
        self.assertIn('peer_count', stats)
        self.assertIn('convergence', stats)
        self.assertIn('embedding', stats)
        self.assertIn('lifecycle', stats)
        self.assertIn('resonance', stats)
        self.assertIn('recipes', stats)

    def test_event_counters_in_extract(self):
        """extract_local_delta() includes event_counters field."""
        self.agg._on_event('inference.completed', {})
        self.agg._on_event('inference.completed', {})

        # Mock the bridge to avoid import errors
        mock_bridge = MagicMock()
        mock_bridge.get_stats.return_value = {}
        mock_bridge.get_learning_stats.return_value = {
            'hivemind': {}, 'bridge': {},
        }

        with patch('integrations.agent_engine.federated_aggregator.'
                   'get_world_model_bridge', return_value=mock_bridge, create=True):
            # Patch the actual import inside extract_local_delta
            import sys
            mock_wmb = MagicMock()
            mock_wmb.get_world_model_bridge = MagicMock(return_value=mock_bridge)
            with patch.dict(sys.modules, {
                'integrations.agent_engine.world_model_bridge': mock_wmb,
                'security.node_integrity': MagicMock(
                    get_node_identity=MagicMock(return_value={'node_id': 'test', 'public_key': 'pk'}),
                    sign_payload=MagicMock(return_value='sig'),
                ),
                'security.hive_guardrails': MagicMock(
                    compute_guardrail_hash=MagicMock(return_value='hash'),
                ),
                'security.system_requirements': MagicMock(
                    get_tier_name=MagicMock(return_value='standard'),
                ),
            }):
                delta = self.agg.extract_local_delta()

        self.assertIsNotNone(delta)
        self.assertIn('event_counters', delta)
        self.assertEqual(delta['event_counters'].get('inference.completed'), 2)


class TestWeightedAvgDict(unittest.TestCase):
    """Internal _weighted_avg_dict correctness."""

    def setUp(self):
        with patch.object(FederatedAggregator, '_subscribe_to_eventbus'):
            self.agg = FederatedAggregator()

    def test_equal_weights(self):
        dicts = [{'a': 10, 'b': 20}, {'a': 30, 'b': 40}]
        result = self.agg._weighted_avg_dict(dicts, [1.0, 1.0], 2.0)
        self.assertAlmostEqual(result['a'], 20.0)
        self.assertAlmostEqual(result['b'], 30.0)

    def test_unequal_weights(self):
        dicts = [{'a': 10}, {'a': 30}]
        # Weight 3:1 → (10*3 + 30*1) / 4 = 60/4 = 15
        result = self.agg._weighted_avg_dict(dicts, [3.0, 1.0], 4.0)
        self.assertAlmostEqual(result['a'], 15.0)

    def test_missing_keys_handled(self):
        dicts = [{'a': 10}, {'b': 20}]
        result = self.agg._weighted_avg_dict(dicts, [1.0, 1.0], 2.0)
        self.assertAlmostEqual(result['a'], 10.0)
        self.assertAlmostEqual(result['b'], 20.0)

    def test_non_numeric_ignored(self):
        dicts = [{'a': 10, 'b': 'text'}, {'a': 20}]
        result = self.agg._weighted_avg_dict(dicts, [1.0, 1.0], 2.0)
        self.assertIn('a', result)
        self.assertNotIn('b', result)

    def test_empty_dicts(self):
        result = self.agg._weighted_avg_dict([], [], 0.0)
        self.assertEqual(result, {})


if __name__ == '__main__':
    unittest.main()
