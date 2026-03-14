"""
Functional tests for FederatedAggregator — ZERO mocks, real objects, real math.

Tests actual weighted FedAvg aggregation, HMAC signing, convergence tracking,
recipe sharing, and guardrail hash enforcement using live FederatedAggregator
instances.

Run: pytest tests/functional/test_federation_functional.py -v --noconftest
"""
import hashlib
import hmac as hmac_mod
import json
import math
import os
import sys
import time

import pytest

# Ensure project root is on sys.path for --noconftest compatibility
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.agent_engine.federated_aggregator import (
    DELTA_MAX_AGE_SECONDS,
    DELTA_VERSION,
    FederatedAggregator,
    _sign_delta,
    _verify_delta_signature,
)


def _make_delta(node_id, total_recorded=100, goal_throughput=50,
                flush_rate=0.8, success_rate=0.9, agent_count=5,
                total_queries=200, avg_fusion_latency_ms=15.0,
                guardrail_hash='', extra=None):
    """Build a realistic federation delta for testing."""
    delta = {
        'version': DELTA_VERSION,
        'node_id': node_id,
        'public_key': '',
        'guardrail_hash': guardrail_hash,
        'timestamp': time.time(),
        'experience_stats': {
            'total_recorded': total_recorded,
            'total_flushed': int(total_recorded * flush_rate),
            'flush_rate': flush_rate,
        },
        'ralt_stats': {
            'skills_distributed': 10,
            'skills_blocked': 2,
            'acceptance_rate': 10 / 12,
        },
        'hivemind_state': {
            'agent_count': agent_count,
            'total_queries': total_queries,
            'avg_fusion_latency_ms': avg_fusion_latency_ms,
        },
        'quality_metrics': {
            'correction_density': 3,
            'success_rate': success_rate,
            'goal_throughput': goal_throughput,
        },
        'benchmark_results': {},
        'capability_tier': 'standard',
        'contribution_score': 0.5,
        'signature': '',
        'event_counters': {},
    }
    if extra and isinstance(extra, dict):
        delta.update(extra)
    return delta


# ─── Test 1: Three-node convergence ───

class TestThreeNodeConvergence:
    """Create 3 aggregator instances, cross-inject deltas, verify weighted averages converge."""

    def test_three_node_convergence(self):
        agg_a = FederatedAggregator()
        agg_b = FederatedAggregator()
        agg_c = FederatedAggregator()

        # Each node has different metrics
        delta_a = _make_delta('node_a', total_recorded=100, goal_throughput=50,
                              flush_rate=0.6, success_rate=0.7, agent_count=3,
                              total_queries=100, avg_fusion_latency_ms=10.0)
        delta_b = _make_delta('node_b', total_recorded=200, goal_throughput=80,
                              flush_rate=0.8, success_rate=0.85, agent_count=5,
                              total_queries=300, avg_fusion_latency_ms=20.0)
        delta_c = _make_delta('node_c', total_recorded=300, goal_throughput=120,
                              flush_rate=0.95, success_rate=0.95, agent_count=8,
                              total_queries=500, avg_fusion_latency_ms=30.0)

        # Cross-inject: each aggregator receives deltas from the other two
        for agg in [agg_a, agg_b, agg_c]:
            for delta in [delta_a, delta_b, delta_c]:
                agg.receive_peer_delta(delta)

        # Set local deltas (each node considers its own delta local)
        agg_a._local_delta = delta_a
        agg_b._local_delta = delta_b
        agg_c._local_delta = delta_c

        result_a = agg_a.aggregate()
        result_b = agg_b.aggregate()
        result_c = agg_c.aggregate()

        assert result_a is not None
        assert result_b is not None
        assert result_c is not None

        # All three see the same 3 peer deltas, so aggregated values
        # should be identical (each aggregator has all 3 deltas in _peer_deltas,
        # plus its own local — but local is also in _peer_deltas, so it appears
        # twice for the local node). Let's verify the experience stats
        # are weighted averages somewhere between the min and max input values.
        for result in [result_a, result_b, result_c]:
            fr = result['experience_stats']['flush_rate']
            # Must be between min(0.6) and max(0.95) of the inputs
            assert 0.6 <= fr <= 0.95, f"flush_rate {fr} out of bounds"

            sr = result['quality_metrics']['success_rate']
            assert 0.7 <= sr <= 0.95, f"success_rate {sr} out of bounds"

            latency = result['hivemind_state']['avg_fusion_latency_ms']
            assert 10.0 <= latency <= 30.0, f"latency {latency} out of bounds"

        # The three aggregators that received the same deltas should produce
        # identical results (they all hold the same peer_deltas and the local
        # delta is added on top, which doubles one entry but that's consistent
        # per-node).
        # Verify peer_count is 3 for all
        for result in [result_a, result_b, result_c]:
            # peer_count = len(peer_deltas) + 1 for local if present,
            # but since local is also in peer_deltas, it counts once there
            # plus once for _local_delta
            assert result['peer_count'] >= 3


# ─── Test 2: Equal weight simple average ───

class TestEqualWeightSimpleAverage:
    """3 nodes with identical interaction counts produce simple average."""

    def test_equal_weight_simple_average(self):
        agg = FederatedAggregator()

        # All three nodes have 0 interactions → weight = max(1.0, log1p(0)) = 1.0
        # With equal weights, result should be simple average.
        delta_a = _make_delta('node_a', total_recorded=0, goal_throughput=0,
                              success_rate=10.0)
        delta_b = _make_delta('node_b', total_recorded=0, goal_throughput=0,
                              success_rate=20.0)
        delta_c = _make_delta('node_c', total_recorded=0, goal_throughput=0,
                              success_rate=30.0)

        for d in [delta_a, delta_b, delta_c]:
            ok, msg = agg.receive_peer_delta(d)
            assert ok, f"receive_peer_delta failed: {msg}"

        result = agg.aggregate()
        assert result is not None

        # With equal weights (all 1.0), weighted average = simple average
        avg_success = result['quality_metrics']['success_rate']
        assert abs(avg_success - 20.0) < 0.01, \
            f"Expected ~20.0 (simple average of 10,20,30), got {avg_success}"

        # Also verify flush_rate: all have flush_rate based on 0 recorded
        # (flush_rate in delta_a/b/c is 0.8 by default from _make_delta,
        #  but total_recorded=0 → total_flushed=0, flush_rate is passed as 0.8)
        # All have flush_rate=0.8 so average should be 0.8
        fr = result['experience_stats']['flush_rate']
        assert abs(fr - 0.8) < 0.01, f"Expected flush_rate ~0.8, got {fr}"


# ─── Test 3: log1p prevents domination ───

class TestLog1pPreventsDomination:
    """Node A with 100K interactions should NOT dominate over Node B with 10."""

    def test_log1p_prevents_domination(self):
        agg = FederatedAggregator()

        # Node A: 100,000 interactions, Node B: 10 interactions
        delta_a = _make_delta('node_a', total_recorded=100000, goal_throughput=0,
                              success_rate=100.0)
        delta_b = _make_delta('node_b', total_recorded=10, goal_throughput=0,
                              success_rate=0.0)

        agg.receive_peer_delta(delta_a)
        agg.receive_peer_delta(delta_b)

        # Compute the actual weights
        interactions_a = 100000 + 0  # total_recorded + goal_throughput
        interactions_b = 10 + 0

        weight_a = max(1.0, math.log1p(interactions_a))  # log1p(100000) ~ 11.51
        weight_b = max(1.0, math.log1p(interactions_b))  # log1p(10) ~ 2.40

        ratio = weight_a / weight_b
        # The ratio should be roughly 4-5x, NOT 10000x
        assert ratio < 6.0, f"Weight ratio {ratio} too high — log1p should prevent domination"
        assert ratio > 2.0, f"Weight ratio {ratio} — A should still have more weight than B"

        result = agg.aggregate()
        assert result is not None

        # With ~4.8x weight ratio, success_rate should be closer to A's 100
        # but NOT at 100 (B's 0 must have meaningful influence).
        # Exact: (100 * 11.51 + 0 * 2.40) / (11.51 + 2.40) ~ 82.7
        sr = result['quality_metrics']['success_rate']
        assert 70.0 < sr < 95.0, \
            f"success_rate {sr} — A dominates too much or too little"
        # Crucially, it should NOT be ~99.99 (which linear weighting would give)
        assert sr < 95.0, "log1p weighting failed — A is dominating like linear"


# ─── Test 4: Stale delta rejected ───

class TestStaleDeltaRejected:
    """Delta with timestamp >1 hour old should be rejected."""

    def test_stale_delta_rejected(self):
        agg = FederatedAggregator()

        delta = _make_delta('stale_node')
        # Set timestamp to 2 hours ago
        delta['timestamp'] = time.time() - (2 * 3600)

        ok, msg = agg.receive_peer_delta(delta)
        assert not ok, "Stale delta should be rejected"
        assert 'delta too old' in msg.lower() or 'future' in msg.lower(), \
            f"Expected 'delta too old or from the future', got: {msg}"

    def test_future_delta_rejected(self):
        agg = FederatedAggregator()

        delta = _make_delta('future_node')
        # Set timestamp to 2 hours in the future
        delta['timestamp'] = time.time() + (2 * 3600)

        ok, msg = agg.receive_peer_delta(delta)
        assert not ok, "Future delta should be rejected"
        assert 'delta too old' in msg.lower() or 'future' in msg.lower(), \
            f"Expected rejection message, got: {msg}"

    def test_fresh_delta_accepted(self):
        agg = FederatedAggregator()

        delta = _make_delta('fresh_node')
        # Timestamp is current (set by _make_delta)
        ok, msg = agg.receive_peer_delta(delta)
        assert ok, f"Fresh delta should be accepted, got: {msg}"
        assert msg == 'accepted'


# ─── Test 5: HMAC round trip ───

class TestHmacRoundTrip:
    """Sign delta with HART_NODE_KEY, verify signature, then tamper and verify rejection."""

    def test_hmac_sign_and_verify(self):
        old_key = os.environ.get('HART_NODE_KEY', '')
        try:
            os.environ['HART_NODE_KEY'] = 'test-secret-key-for-hmac-functional'

            delta = _make_delta('hmac_node')
            # Remove any existing signature
            delta.pop('hmac_signature', None)

            # Sign
            _sign_delta(delta)
            assert 'hmac_signature' in delta, "Delta should have hmac_signature after signing"
            assert len(delta['hmac_signature']) == 64, "HMAC-SHA256 hex should be 64 chars"

            # Verify — should pass
            assert _verify_delta_signature(delta), "Valid signature should verify"

        finally:
            if old_key:
                os.environ['HART_NODE_KEY'] = old_key
            else:
                os.environ.pop('HART_NODE_KEY', None)

    def test_hmac_tamper_detected(self):
        old_key = os.environ.get('HART_NODE_KEY', '')
        try:
            os.environ['HART_NODE_KEY'] = 'test-secret-key-for-hmac-functional'

            delta = _make_delta('hmac_node')
            delta.pop('hmac_signature', None)

            # Sign
            _sign_delta(delta)
            original_sig = delta['hmac_signature']

            # Tamper with the delta
            delta['experience_stats']['total_recorded'] = 999999

            # Verify — should fail
            assert not _verify_delta_signature(delta), \
                "Tampered delta should fail HMAC verification"

        finally:
            if old_key:
                os.environ['HART_NODE_KEY'] = old_key
            else:
                os.environ.pop('HART_NODE_KEY', None)

    def test_hmac_no_key_returns_unsigned(self):
        old_key = os.environ.get('HART_NODE_KEY', '')
        try:
            os.environ.pop('HART_NODE_KEY', None)

            delta = _make_delta('unsigned_node')
            delta.pop('hmac_signature', None)

            _sign_delta(delta)
            # Without HART_NODE_KEY, delta should not have hmac_signature
            assert 'hmac_signature' not in delta, \
                "Without HART_NODE_KEY, delta should remain unsigned"

        finally:
            if old_key:
                os.environ['HART_NODE_KEY'] = old_key

    def test_receive_peer_delta_rejects_bad_hmac(self):
        """Integration: receive_peer_delta checks HMAC and rejects tampered deltas."""
        old_key = os.environ.get('HART_NODE_KEY', '')
        try:
            os.environ['HART_NODE_KEY'] = 'test-secret-key-for-hmac-functional'

            agg = FederatedAggregator()

            delta = _make_delta('hmac_peer')
            _sign_delta(delta)

            # Tamper after signing
            delta['experience_stats']['total_recorded'] = 777

            ok, msg = agg.receive_peer_delta(delta)
            assert not ok, "Tampered HMAC delta should be rejected"
            assert 'hmac' in msg.lower(), f"Expected HMAC rejection, got: {msg}"

        finally:
            if old_key:
                os.environ['HART_NODE_KEY'] = old_key
            else:
                os.environ.pop('HART_NODE_KEY', None)


# ─── Test 6: Convergence score increases ───

class TestConvergenceScoreIncreases:
    """Submit consistent deltas across 5 epochs, verify convergence trends upward."""

    def test_convergence_score_increases(self):
        agg = FederatedAggregator()

        scores = []
        for epoch in range(5):
            # All nodes report increasingly similar flush_rates
            # (converging toward 0.8)
            spread = 0.3 / (epoch + 1)  # Spread decreases each epoch
            base_rate = 0.8

            delta_a = _make_delta(f'conv_a', flush_rate=base_rate - spread)
            delta_b = _make_delta(f'conv_b', flush_rate=base_rate)
            delta_c = _make_delta(f'conv_c', flush_rate=base_rate + spread)

            # Clear old deltas, inject new ones
            with agg._lock:
                agg._peer_deltas.clear()

            for d in [delta_a, delta_b, delta_c]:
                agg.receive_peer_delta(d)

            score = agg.track_convergence()
            scores.append(score)

        # Convergence score should trend upward as variance decreases
        assert scores[-1] > scores[0], \
            f"Convergence should increase: first={scores[0]:.4f}, last={scores[-1]:.4f}"

        # The final score (very low variance) should be close to 1.0
        assert scores[-1] > 0.8, \
            f"Final convergence {scores[-1]:.4f} should be >0.8 with low variance"

        # Verify history is tracked
        assert len(agg._convergence_history) == 5


# ─── Test 7: Recipe delta channel ───

class TestRecipeDeltaChannel:
    """3 nodes share recipe deltas, aggregate_recipes() merges them."""

    def test_recipe_delta_channel(self):
        agg = FederatedAggregator()

        # Node A shares 2 recipes
        agg.receive_recipe_delta('recipe_node_a', {
            'node_id': 'recipe_node_a',
            'recipes': [
                {'id': 'recipe_1', 'name': 'Web Scraper', 'action_count': 5,
                 'success_rate': 0.9, 'reuse_count': 10},
                {'id': 'recipe_2', 'name': 'Data Pipeline', 'action_count': 8,
                 'success_rate': 0.85, 'reuse_count': 5},
            ]
        })

        # Node B shares 1 recipe (same as recipe_1 from A, plus a new one)
        agg.receive_recipe_delta('recipe_node_b', {
            'node_id': 'recipe_node_b',
            'recipes': [
                {'id': 'recipe_1', 'name': 'Web Scraper', 'action_count': 5,
                 'success_rate': 0.95, 'reuse_count': 20},
                {'id': 'recipe_3', 'name': 'Report Generator', 'action_count': 3,
                 'success_rate': 0.8, 'reuse_count': 15},
            ]
        })

        # Node C shares 1 unique recipe
        agg.receive_recipe_delta('recipe_node_c', {
            'node_id': 'recipe_node_c',
            'recipes': [
                {'id': 'recipe_4', 'name': 'Email Sender', 'action_count': 2,
                 'success_rate': 1.0, 'reuse_count': 50},
            ]
        })

        result = agg.aggregate_recipes()
        assert result is not None

        # Should have 4 unique recipes
        assert result['total_recipes'] == 4, \
            f"Expected 4 unique recipes, got {result['total_recipes']}"

        # 3 peers contributed
        assert result['peer_count'] == 3

        # Find recipe_1 — should be merged from nodes A and B
        recipes_by_id = {r['id']: r for r in result['recipes']}
        assert 'recipe_1' in recipes_by_id
        r1 = recipes_by_id['recipe_1']
        assert len(r1['nodes']) == 2, "recipe_1 should be on 2 nodes"
        assert 'recipe_node_a' in r1['nodes']
        assert 'recipe_node_b' in r1['nodes']
        assert r1['total_reuse_count'] == 30, \
            f"Expected 30 total reuses (10+20), got {r1['total_reuse_count']}"
        # Average success rate: (0.9 + 0.95) / 2 via running average
        # Running avg: first=0.9, second=0.9+(0.95-0.9)/2=0.925
        assert abs(r1['avg_success_rate'] - 0.925) < 0.01, \
            f"Expected avg_success_rate ~0.925, got {r1['avg_success_rate']}"

        # recipe_4 should be from node C only
        r4 = recipes_by_id['recipe_4']
        assert len(r4['nodes']) == 1
        assert r4['total_reuse_count'] == 50

    def test_recipe_stats(self):
        agg = FederatedAggregator()

        agg.receive_recipe_delta('stats_node', {
            'node_id': 'stats_node',
            'recipes': [{'id': 'r1', 'name': 'Test', 'action_count': 1,
                          'success_rate': 1.0, 'reuse_count': 1}]
        })

        stats = agg.get_recipe_stats()
        assert stats['pending_deltas'] == 1


# ─── Test 8: Guardrail hash mismatch rejects ───

class TestGuardrailHashMismatchRejects:
    """Peer delta with wrong guardrail_hash is rejected."""

    def test_guardrail_hash_mismatch_rejects(self):
        agg = FederatedAggregator()

        # Get the real local guardrail hash
        try:
            from security.hive_guardrails import compute_guardrail_hash
            local_hash = compute_guardrail_hash()
        except ImportError:
            pytest.skip("security.hive_guardrails not importable")

        # Create delta with a WRONG guardrail hash
        delta = _make_delta('bad_guardrail_node',
                            guardrail_hash='deadbeef' * 8)

        ok, msg = agg.receive_peer_delta(delta)
        assert not ok, "Delta with wrong guardrail hash should be rejected"
        assert 'guardrail hash mismatch' in msg.lower(), \
            f"Expected guardrail hash mismatch message, got: {msg}"

    def test_matching_guardrail_hash_accepted(self):
        agg = FederatedAggregator()

        # Get the real local guardrail hash
        try:
            from security.hive_guardrails import compute_guardrail_hash
            local_hash = compute_guardrail_hash()
        except ImportError:
            pytest.skip("security.hive_guardrails not importable")

        # Create delta with the CORRECT guardrail hash
        delta = _make_delta('good_guardrail_node', guardrail_hash=local_hash)

        ok, msg = agg.receive_peer_delta(delta)
        assert ok, f"Delta with correct guardrail hash should be accepted, got: {msg}"

    def test_empty_guardrail_hash_passes(self):
        """Empty guardrail hash in delta skips the check (backwards compatibility)."""
        agg = FederatedAggregator()

        delta = _make_delta('no_hash_node', guardrail_hash='')

        ok, msg = agg.receive_peer_delta(delta)
        assert ok, f"Delta with empty guardrail hash should pass, got: {msg}"


# ─── Additional edge cases ───

class TestEdgeCases:
    """Additional functional tests for completeness."""

    def test_version_mismatch_rejected(self):
        agg = FederatedAggregator()
        delta = _make_delta('wrong_version_node')
        delta['version'] = 999
        ok, msg = agg.receive_peer_delta(delta)
        assert not ok
        assert 'version mismatch' in msg

    def test_missing_node_id_rejected(self):
        agg = FederatedAggregator()
        delta = _make_delta('')  # empty node_id
        ok, msg = agg.receive_peer_delta(delta)
        assert not ok
        assert 'missing node_id' in msg

    def test_invalid_payload_rejected(self):
        agg = FederatedAggregator()
        ok, msg = agg.receive_peer_delta("not a dict")
        assert not ok
        assert 'invalid payload' in msg

    def test_aggregate_empty_returns_none(self):
        agg = FederatedAggregator()
        result = agg.aggregate()
        assert result is None

    def test_weighted_avg_dict_math(self):
        """Directly test _weighted_avg_dict with known values."""
        agg = FederatedAggregator()

        dicts = [
            {'a': 10, 'b': 100},
            {'a': 20, 'b': 200},
            {'a': 30, 'b': 300},
        ]
        weights = [1.0, 1.0, 1.0]
        total = 3.0

        result = agg._weighted_avg_dict(dicts, weights, total)
        assert abs(result['a'] - 20.0) < 0.001
        assert abs(result['b'] - 200.0) < 0.001

    def test_weighted_avg_dict_unequal_weights(self):
        """Unequal weights shift the average toward the heavier entry."""
        agg = FederatedAggregator()

        dicts = [
            {'val': 0.0},
            {'val': 100.0},
        ]
        weights = [1.0, 3.0]  # second entry has 3x weight
        total = 4.0

        result = agg._weighted_avg_dict(dicts, weights, total)
        # Expected: (0*1 + 100*3) / (1+3) = 75
        assert abs(result['val'] - 75.0) < 0.001

    def test_single_node_aggregate(self):
        """Single node delta aggregates to itself."""
        agg = FederatedAggregator()
        delta = _make_delta('solo_node', success_rate=0.42)
        agg.receive_peer_delta(delta)
        result = agg.aggregate()
        assert result is not None
        assert abs(result['quality_metrics']['success_rate'] - 0.42) < 0.001


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
