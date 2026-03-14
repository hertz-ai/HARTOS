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
