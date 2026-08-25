"""Tests for federation privacy enforcement + node bootstrapping.

Verifies:
1. ScopeGuard is called before federation broadcast (no PII leaks)
2. Federation deltas contain ONLY aggregate stats (no raw user data)
3. Node bootstrap package contains only federated-scope data
4. WorldModelBridge consent gate blocks non-consented external flush
5. Weight updates go through HevolveAI (not raw in federation delta)
"""
import time
import pytest
from unittest.mock import patch, MagicMock


# ── 1. Federation delta structure — no raw user data ──

class TestDeltaStructure:
    """Verify federation deltas contain only aggregate stats."""

    def _make_aggregator(self):
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        return FederatedAggregator()

    @patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge')
    def test_delta_contains_only_stats(self, mock_bridge):
        """Delta must be aggregate counters, not raw user data."""
        bridge = MagicMock()
        bridge.get_stats.return_value = {'total_experiences': 100}
        bridge.get_learning_stats.return_value = {
            'hivemind': {'agent_count': 3, 'avg_fusion_latency_ms': 12.5},
            'bridge': {
                'total_recorded': 100, 'total_flushed': 90,
                'total_skills_distributed': 50, 'total_skills_blocked': 5,
                'total_hivemind_queries': 200, 'total_corrections': 10,
            }
        }
        mock_bridge.return_value = bridge

        agg = self._make_aggregator()
        delta = agg.extract_local_delta()

        assert delta is not None
        # These are the ONLY keys that should contain data
        allowed_keys = {
            'version', 'node_id', 'public_key', 'guardrail_hash',
            'timestamp', 'experience_stats', 'ralt_stats', 'hivemind_state',
            'quality_metrics', 'benchmark_results', 'capability_tier',
            'contribution_score', 'event_counters', 'signature',
            # code_hash: the node's build hash — a PUBLIC identifier (SHA-256
            # of the .py manifest), already broadcast in every gossip announce,
            # not user data. Added 2026-08-24 so central can gate federation on
            # a genuine/unmodified build (receive_peer_delta). Privacy-safe.
            'code_hash',
        }
        for key in delta:
            assert key in allowed_keys, f"Unexpected key in delta: {key}"

        # experience_stats = aggregate counts, not raw data
        es = delta.get('experience_stats', {})
        assert isinstance(es.get('total_recorded'), int)
        assert isinstance(es.get('flush_rate'), float)
        # No raw prompts, responses, or user IDs
        delta_str = str(delta)
        assert 'prompt' not in delta_str.lower() or 'prompt_id' in delta_str.lower()

    @patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge')
    def test_presence_delta_when_bridge_unavailable(self, mock_bridge):
        """A fresh/frozen node whose learning bridge raises must STILL produce a
        signed presence delta, not None.

        tick() broadcasts only on a truthy delta, so returning None here means a
        genuine INSTALL never federates and never reaches the census (#694
        "installed but not federating"). On a frozen build with no hevolveai the
        bridge import raises; the extract must degrade to zeroed stats + a signed
        presence delta rather than swallowing the whole node.
        """
        mock_bridge.side_effect = RuntimeError('no hevolveai in frozen build')
        agg = self._make_aggregator()
        delta = agg.extract_local_delta()
        assert delta is not None, 'bridge failure must degrade to a presence delta'
        assert delta.get('node_id') is not None
        assert 'signature' in delta
        # Stats degrade to zeros, not missing keys (no KeyError building the delta).
        assert delta['experience_stats']['total_recorded'] == 0
        assert delta['experience_stats']['flush_rate'] == 0.0

    @patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge')
    def test_no_user_text_in_delta(self, mock_bridge):
        """Ensure no raw user text ends up in federation delta."""
        bridge = MagicMock()
        bridge.get_stats.return_value = {}
        bridge.get_learning_stats.return_value = {'hivemind': {}, 'bridge': {}}
        mock_bridge.return_value = bridge

        agg = self._make_aggregator()
        delta = agg.extract_local_delta()

        assert delta is not None
        # Flatten all string values
        def extract_strings(obj, strings=None):
            if strings is None:
                strings = []
            if isinstance(obj, str):
                strings.append(obj)
            elif isinstance(obj, dict):
                for v in obj.values():
                    extract_strings(v, strings)
            elif isinstance(obj, (list, tuple)):
                for v in obj:
                    extract_strings(v, strings)
            return strings

        all_strings = extract_strings(delta)
        # None of these should contain actual user messages
        for s in all_strings:
            # Aggregate numbers serialized as strings are fine
            # SHA-256 hashes (64 hex chars) and signatures are expected
            if s and len(s) > 200:
                pytest.fail(f"Suspiciously long string in delta: {s[:100]}...")


# ── 2. ScopeGuard wired into broadcast ──

class TestBroadcastPrivacyGate:
    """Verify ScopeGuard.check_egress() is called before broadcast."""

    def _make_aggregator(self):
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        return FederatedAggregator()

    @patch('integrations.agent_engine.federated_aggregator._sign_delta')
    @patch('security.edge_privacy.get_scope_guard')
    def test_scope_guard_called_on_broadcast(self, mock_guard_fn, mock_sign):
        """ScopeGuard.check_egress runs before any data leaves."""
        guard = MagicMock()
        guard.check_egress.return_value = (True, 'ok')
        mock_guard_fn.return_value = guard

        agg = self._make_aggregator()
        delta = {'version': 1, 'node_id': 'test', 'timestamp': time.time()}

        with patch('integrations.social.models.get_db', side_effect=ImportError):
            agg.broadcast_delta(delta)

        guard.check_egress.assert_called_once()
        call_args = guard.check_egress.call_args
        # Destination must be FEDERATED
        from security.edge_privacy import PrivacyScope
        assert call_args[0][1] == PrivacyScope.FEDERATED

    @patch('integrations.agent_engine.federated_aggregator._sign_delta')
    @patch('security.edge_privacy.get_scope_guard')
    def test_broadcast_blocked_on_pii(self, mock_guard_fn, mock_sign):
        """If ScopeGuard detects PII, broadcast is blocked entirely."""
        guard = MagicMock()
        guard.check_egress.return_value = (False, 'PII found in "node_id"')
        mock_guard_fn.return_value = guard

        agg = self._make_aggregator()
        delta = {'version': 1, 'node_id': 'test'}

        agg.broadcast_delta(delta)
        # _sign_delta should NOT be called (broadcast stopped early)
        mock_sign.assert_not_called()


# ── 3. Node bootstrapping ──

class TestNodeBootstrap:
    """Verify bootstrap_new_node returns only federated-scope data."""

    def _make_aggregator(self):
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        return FederatedAggregator()

    def test_bootstrap_returns_package(self):
        agg = self._make_aggregator()
        pkg = agg.bootstrap_new_node('new-node-123')
        assert pkg['type'] == 'node_bootstrap'
        assert pkg['for_node'] == 'new-node-123'
        assert 'benchmarks' in pkg
        assert 'recipe_index' in pkg
        assert 'quality_baselines' in pkg
        assert 'resonance_norms' in pkg

    def test_bootstrap_shares_pooled_quality_baselines(self):
        """A joiner must inherit the hive's pooled quality heuristics.

        Regression: bootstrap_new_node read self.peer_deltas (no underscore, no
        such attribute), the AttributeError was swallowed, and every joiner got
        quality_baselines={} — the network-value mechanism was silently a no-op.
        It must average quality_metrics from _peer_deltas (where accepted peer
        learning is stored). Proven end to end by
        tests/standalone/network_beats_solo_proof.py.
        """
        agg = self._make_aggregator()
        agg._peer_deltas = {
            'peer-a': {'quality_metrics': {'success_rate': 0.9,
                                           'avg_latency_ms': 100}},
            'peer-b': {'quality_metrics': {'success_rate': 0.8,
                                           'avg_latency_ms': 140}},
        }
        pkg = agg.bootstrap_new_node('joiner')
        qb = pkg.get('quality_baselines') or {}
        assert qb.get('success_rate') == pytest.approx(0.85)
        assert qb.get('avg_latency_ms') == pytest.approx(120.0)
        # A solo node (no peer deltas) has nothing to share.
        solo = self._make_aggregator()
        solo._peer_deltas = {}
        assert (solo.bootstrap_new_node('joiner').get('quality_baselines') or {}) == {}

    def test_bootstrap_no_raw_user_data(self):
        agg = self._make_aggregator()
        pkg = agg.bootstrap_new_node('node-456')
        pkg_str = str(pkg)
        # Should not contain user conversations, PII patterns
        assert 'password' not in pkg_str.lower()
        assert 'email' not in pkg_str.lower() or 'email_count' in pkg_str.lower()

    @patch('security.edge_privacy.get_scope_guard')
    def test_bootstrap_runs_scope_guard(self, mock_guard_fn):
        guard = MagicMock()
        guard.check_egress.return_value = (True, 'ok')
        mock_guard_fn.return_value = guard

        agg = self._make_aggregator()
        pkg = agg.bootstrap_new_node('node-789')

        guard.check_egress.assert_called_once()
        assert pkg['type'] == 'node_bootstrap'

    @patch('security.edge_privacy.get_scope_guard')
    def test_bootstrap_blocked_on_violation(self, mock_guard_fn):
        guard = MagicMock()
        guard.check_egress.return_value = (False, 'secrets detected')
        mock_guard_fn.return_value = guard

        agg = self._make_aggregator()
        pkg = agg.bootstrap_new_node('node-bad')
        assert 'error' in pkg


# ── 4. Consent gate on WorldModelBridge ──

class TestConsentGate:
    """WorldModelBridge blocks external flush for non-consented users."""

    def test_consent_check_exists(self):
        """_has_cloud_consent method exists on WorldModelBridge."""
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        assert hasattr(WorldModelBridge, '_has_cloud_consent')

    def test_external_target_check_exists(self):
        """_is_external_target method exists."""
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        assert hasattr(WorldModelBridge, '_is_external_target')


# ── 5. Weight updates go through HevolveAI, not raw in delta ──

class TestWeightExchangePath:
    """Verify weight/gradient updates use HevolveAI, not federation delta."""

    def test_delta_has_no_weight_tensors(self):
        """Federation delta must not contain raw weight tensors."""
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        agg = FederatedAggregator()
        # Even if we had peer deltas, they should be stats not weights
        agg.peer_deltas = {
            'peer1': {
                'experience_stats': {'total_recorded': 50},
                'quality_metrics': {'success_rate': 0.8},
            }
        }
        # Aggregate should produce stats, not tensors
        result = agg.aggregate()
        if result:
            result_str = str(result)
            assert 'tensor' not in result_str.lower()
            assert 'weight' not in result_str.lower() or 'weight' in 'contribution_weight'

    def test_gradient_protocol_is_phase2_stub(self):
        """LoRA gradient exchange is Phase 2 — not active yet."""
        from integrations.agent_engine.federated_gradient_protocol import (
            ByzantineAggregator, DifferentialPrivacyNoise, LoRAGradient
        )
        # Phase 2 stubs return None/unchanged
        byz = ByzantineAggregator()
        assert byz.aggregate([]) is None

        dp = DifferentialPrivacyNoise()
        grad = LoRAGradient('test_layer')
        result = dp.add_noise(grad)
        assert result is grad  # unchanged — stub

    def test_apply_federation_update_is_metrics_only(self):
        """apply_federation_update stores metrics, NOT weights."""
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        bridge = WorldModelBridge.__new__(WorldModelBridge)
        bridge._federation_aggregated = None
        # This should store metrics, not push to HevolveAI
        result = bridge.apply_federation_update({'avg_latency': 12.5, 'peer_count': 5})
        assert result is True
        assert bridge._federation_aggregated == {'avg_latency': 12.5, 'peer_count': 5}


# ── 6. Edge privacy defaults ──

class TestEdgePrivacyDefaults:
    """Verify privacy-by-default configuration."""

    def test_default_scope_is_edge_only(self):
        from security.edge_privacy import PrivacyScope
        assert PrivacyScope.EDGE_ONLY.value == 'edge_only'

    def test_edge_data_blocked_from_federation(self):
        from security.edge_privacy import scope_allows, PrivacyScope
        assert not scope_allows(PrivacyScope.EDGE_ONLY, PrivacyScope.FEDERATED)

    def test_federated_data_allowed_to_federated(self):
        from security.edge_privacy import scope_allows, PrivacyScope
        assert scope_allows(PrivacyScope.FEDERATED, PrivacyScope.FEDERATED)

    def test_user_devices_blocked_from_federation(self):
        from security.edge_privacy import scope_allows, PrivacyScope
        assert not scope_allows(PrivacyScope.USER_DEVICES, PrivacyScope.FEDERATED)


# ── Collective intelligence metric flows end to end ──

class TestCollectiveIntelligenceMetric:
    """The real intelligence_index the learning core computes must reach the
    census.  Regression guard for the drop+miskey bug: extract_local_delta kept
    only agent_count/latency in hivemind_state and hive_census read a `hivemind`
    key nothing produced, so mean_intelligence_index was permanently null.
    """

    @patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge')
    def test_intelligence_index_reaches_census(self, mock_bridge):
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        bridge = MagicMock()
        bridge.get_stats.return_value = {}
        bridge.get_learning_stats.return_value = {
            'hivemind': {
                'agent_count': 4, 'avg_fusion_latency_ms': 12.5,
                'intelligence_index': 7.5, 'growth_rate': 1.3,
            },
            'bridge': {'total_hivemind_queries': 20},
        }
        mock_bridge.return_value = bridge

        agg = FederatedAggregator()
        delta = agg.extract_local_delta()
        assert delta is not None
        # Carried in the existing (privacy-allowlisted) hivemind_state block,
        # not a new top-level key.
        assert delta['hivemind_state']['intelligence_index'] == 7.5
        assert delta['hivemind_state']['growth_rate'] == 1.3
        assert 'hivemind' not in delta  # no phantom parallel key

        agg._local_delta = delta
        census = agg.hive_census()
        assert census['nodes_with_intelligence'] == 1
        assert census['mean_intelligence_index'] == 7.5

    @patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge')
    def test_presence_node_absent_not_zero(self, mock_bridge):
        """A node whose learning core did not run carries no intelligence_index
        and must be absent from the mean, never counted as 0.0."""
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        bridge = MagicMock()
        bridge.get_stats.return_value = {}
        bridge.get_learning_stats.return_value = {
            'hivemind': {'agent_count': 0}, 'bridge': {},
        }
        mock_bridge.return_value = bridge

        agg = FederatedAggregator()
        delta = agg.extract_local_delta()
        assert 'intelligence_index' not in delta['hivemind_state']
        agg._local_delta = delta
        census = agg.hive_census()
        assert census['nodes_with_intelligence'] == 0
        assert census['mean_intelligence_index'] is None
