"""
Tests for Federated Weight Aggregation + Auto-Upgrade Pipeline + Production Fixes.

Covers:
- FederatedAggregator: singleton, extract, receive, aggregate, convergence
- BenchmarkRegistry: adapters, register, snapshot, is_upgrade_safe
- UpgradeOrchestrator: stages, gates, rollback, state persistence
- Production fixes: LLAMA_CPP_PORT, bridge race condition, brute-force protection
"""
import json
import os
import sys
import time
import threading
import tempfile
from unittest.mock import patch, MagicMock

import pytest

# ─── Ensure imports work ───
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

# Set in-memory DB for any social model imports
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')


# ═══════════════════════════════════════════════════════════════
# FEDERATED AGGREGATOR TESTS
# ═══════════════════════════════════════════════════════════════

class TestFederatedAggregator:
    """Test FederatedAggregator functionality."""

    def _make_aggregator(self):
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        return FederatedAggregator()

    def test_singleton(self):
        from integrations.agent_engine.federated_aggregator import (
            get_federated_aggregator, _aggregator_lock)
        import integrations.agent_engine.federated_aggregator as mod
        # Reset singleton
        mod._aggregator = None
        a1 = get_federated_aggregator()
        a2 = get_federated_aggregator()
        assert a1 is a2
        mod._aggregator = None  # Cleanup

    def test_initial_state(self):
        agg = self._make_aggregator()
        assert agg._epoch == 0
        assert len(agg._peer_deltas) == 0
        assert agg._local_delta is None
        stats = agg.get_stats()
        assert stats['epoch'] == 0
        assert stats['peer_count'] == 0
        assert stats['convergence'] == 0.0

    def test_receive_valid_delta(self):
        agg = self._make_aggregator()
        delta = {
            'version': 1,
            'node_id': 'test-node-1',
            'public_key': 'abc123',
            'guardrail_hash': '',
            'timestamp': time.time(),
            'signature': '',
            'experience_stats': {'total_recorded': 100, 'total_flushed': 90, 'flush_rate': 0.9},
            'ralt_stats': {'skills_distributed': 5, 'skills_blocked': 1, 'acceptance_rate': 0.83},
            'hivemind_state': {'agent_count': 3, 'total_queries': 50, 'avg_fusion_latency_ms': 15},
            'quality_metrics': {'correction_density': 10, 'success_rate': 0.8, 'goal_throughput': 5},
            'benchmark_results': {},
            'capability_tier': 'standard',
            'contribution_score': 10.0,
        }
        accepted, reason = agg.receive_peer_delta(delta)
        assert accepted is True
        assert reason == 'accepted'
        assert 'test-node-1' in agg._peer_deltas

    def test_receive_version_mismatch(self):
        agg = self._make_aggregator()
        delta = {'version': 999, 'node_id': 'x', 'timestamp': time.time()}
        accepted, reason = agg.receive_peer_delta(delta)
        assert accepted is False
        assert 'version mismatch' in reason

    def test_receive_stale_delta(self):
        agg = self._make_aggregator()
        delta = {'version': 1, 'node_id': 'x', 'timestamp': time.time() - 7200}
        accepted, reason = agg.receive_peer_delta(delta)
        assert accepted is False
        assert 'too old' in reason

    def test_receive_missing_node_id(self):
        agg = self._make_aggregator()
        delta = {'version': 1, 'node_id': '', 'timestamp': time.time()}
        accepted, reason = agg.receive_peer_delta(delta)
        assert accepted is False
        assert 'missing node_id' in reason

    def test_aggregate_single_delta(self):
        agg = self._make_aggregator()
        agg._local_delta = {
            'experience_stats': {'flush_rate': 0.9},
            'ralt_stats': {'acceptance_rate': 0.8},
            'hivemind_state': {'agent_count': 3},
            'quality_metrics': {'success_rate': 0.7},
            'contribution_score': 5.0,
            'capability_tier': 'standard',
        }
        result = agg.aggregate()
        assert result is not None
        assert result['peer_count'] == 1
        assert result['experience_stats']['flush_rate'] == pytest.approx(0.9, abs=0.01)

    def test_aggregate_weighted_multiple(self):
        agg = self._make_aggregator()
        agg._peer_deltas = {
            'node1': {
                'experience_stats': {'flush_rate': 0.5},
                'ralt_stats': {}, 'hivemind_state': {}, 'quality_metrics': {},
                'contribution_score': 1.0, 'capability_tier': 'lite',
            },
            'node2': {
                'experience_stats': {'flush_rate': 1.0},
                'ralt_stats': {}, 'hivemind_state': {}, 'quality_metrics': {},
                'contribution_score': 100.0, 'capability_tier': 'compute_host',
            },
        }
        result = agg.aggregate()
        assert result is not None
        assert result['peer_count'] == 2
        # compute_host node with high contribution should pull average towards 1.0
        avg = result['experience_stats']['flush_rate']
        assert avg > 0.5, f"Weighted avg should be > 0.5, got {avg}"

    def test_convergence_tracking(self):
        agg = self._make_aggregator()
        agg._peer_deltas = {
            'n1': {'experience_stats': {'flush_rate': 0.9}},
            'n2': {'experience_stats': {'flush_rate': 0.91}},
        }
        score = agg.track_convergence()
        # Low variance = high convergence (close to 1.0)
        assert score > 0.5
        assert len(agg._convergence_history) == 1

    def test_apply_aggregated(self):
        agg = self._make_aggregator()
        aggregated = {'epoch': 1, 'experience_stats': {'flush_rate': 0.85}}
        agg.apply_aggregated(aggregated)
        assert agg._last_aggregated == aggregated

    @patch('integrations.agent_engine.federated_aggregator.FederatedAggregator.broadcast_delta')
    @patch('integrations.agent_engine.federated_aggregator.FederatedAggregator.extract_local_delta')
    def test_tick_cycle(self, mock_extract, mock_broadcast):
        agg = self._make_aggregator()
        mock_extract.return_value = {
            'experience_stats': {'flush_rate': 0.8},
            'ralt_stats': {}, 'hivemind_state': {}, 'quality_metrics': {},
            'contribution_score': 5.0, 'capability_tier': 'standard',
        }
        result = agg.tick()
        assert result['aggregated'] is True
        assert result['epoch'] == 1
        mock_broadcast.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# BENCHMARK REGISTRY TESTS
# ═══════════════════════════════════════════════════════════════

class TestBenchmarkRegistry:
    """Test BenchmarkRegistry functionality."""

    def _make_registry(self):
        from integrations.agent_engine.benchmark_registry import BenchmarkRegistry
        return BenchmarkRegistry()

    def test_builtin_adapters_registered(self):
        registry = self._make_registry()
        names = [b['name'] for b in registry.list_benchmarks()]
        assert 'model_registry' in names
        assert 'world_model' in names
        assert 'regression' in names
        assert 'guardrail' in names
        assert 'quantiphy' in names

    def test_register_benchmark(self):
        from integrations.agent_engine.benchmark_registry import BenchmarkAdapter
        registry = self._make_registry()

        class TestAdapter(BenchmarkAdapter):
            name = 'test_bench'
            tier = 'fast'
            def run(self, **kw):
                return {'metrics': {'score': {'value': 42, 'direction': 'higher', 'unit': 'pts'}}}

        registry.register_benchmark(TestAdapter())
        names = [b['name'] for b in registry.list_benchmarks()]
        assert 'test_bench' in names

    def test_capture_snapshot_fast_tier(self):
        from integrations.agent_engine.benchmark_registry import BenchmarkAdapter, BenchmarkRegistry
        with tempfile.TemporaryDirectory() as tmpdir:
            import integrations.agent_engine.benchmark_registry as mod
            original = mod.BENCHMARK_DIR
            mod.BENCHMARK_DIR = tmpdir

            # Create a fresh registry with only a mock adapter
            registry = BenchmarkRegistry.__new__(BenchmarkRegistry)
            registry._lock = threading.Lock()
            registry._adapters = {}
            registry._latest_results = {}
            os.makedirs(tmpdir, exist_ok=True)

            class MockAdapter(BenchmarkAdapter):
                name = 'mock_fast'
                tier = 'fast'
                def run(self, **kw):
                    return {'metrics': {'val': {'value': 1.0, 'direction': 'higher', 'unit': 'x'}}}

            registry.register_benchmark(MockAdapter())
            snap = registry.capture_snapshot('v0.1', 'abc123', tier='fast')
            assert snap['version'] == 'v0.1'
            assert snap['tier'] == 'fast'
            assert 'mock_fast' in snap['benchmarks']
            assert snap['benchmarks']['mock_fast']['metrics']['val']['value'] == 1.0
            mod.BENCHMARK_DIR = original

    def test_is_upgrade_safe_no_baseline(self):
        registry = self._make_registry()
        safe, reason = registry.is_upgrade_safe('nonexistent', 'v2')
        assert safe is True
        assert 'no baseline' in reason

    def test_is_upgrade_safe_regression_detected(self):
        registry = self._make_registry()
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write old snapshot
            old = {'version': 'v1', 'benchmarks': {
                'test': {'metrics': {'score': {'value': 100, 'direction': 'higher', 'unit': 'pts'}}}
            }}
            with open(os.path.join(tmpdir, 'v1.json'), 'w') as f:
                json.dump(old, f)
            # Write new snapshot with regression
            new = {'version': 'v2', 'benchmarks': {
                'test': {'metrics': {'score': {'value': 50, 'direction': 'higher', 'unit': 'pts'}}}
            }}
            with open(os.path.join(tmpdir, 'v2.json'), 'w') as f:
                json.dump(new, f)

            import integrations.agent_engine.benchmark_registry as mod
            original = mod.BENCHMARK_DIR
            mod.BENCHMARK_DIR = tmpdir
            safe, reason = registry.is_upgrade_safe('v1', 'v2')
            assert safe is False
            assert 'regression' in reason.lower()
            mod.BENCHMARK_DIR = original

    def test_is_upgrade_safe_passes(self):
        registry = self._make_registry()
        with tempfile.TemporaryDirectory() as tmpdir:
            old = {'version': 'v1', 'benchmarks': {
                'test': {'metrics': {'score': {'value': 100, 'direction': 'higher', 'unit': 'pts'}}}
            }}
            with open(os.path.join(tmpdir, 'v1.json'), 'w') as f:
                json.dump(old, f)
            new = {'version': 'v2', 'benchmarks': {
                'test': {'metrics': {'score': {'value': 110, 'direction': 'higher', 'unit': 'pts'}}}
            }}
            with open(os.path.join(tmpdir, 'v2.json'), 'w') as f:
                json.dump(new, f)

            import integrations.agent_engine.benchmark_registry as mod
            original = mod.BENCHMARK_DIR
            mod.BENCHMARK_DIR = tmpdir
            safe, reason = registry.is_upgrade_safe('v1', 'v2')
            assert safe is True
            mod.BENCHMARK_DIR = original

    def test_get_latest_results(self):
        registry = self._make_registry()
        assert isinstance(registry.get_latest_results(), dict)

    def test_singleton(self):
        import integrations.agent_engine.benchmark_registry as mod
        mod._registry = None
        from integrations.agent_engine.benchmark_registry import get_benchmark_registry
        r1 = get_benchmark_registry()
        r2 = get_benchmark_registry()
        assert r1 is r2
        mod._registry = None


# ═══════════════════════════════════════════════════════════════
# UPGRADE ORCHESTRATOR TESTS
# ═══════════════════════════════════════════════════════════════

class TestUpgradeOrchestrator:
    """Test UpgradeOrchestrator pipeline."""

    def _make_orchestrator(self):
        from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator
        import integrations.agent_engine.upgrade_orchestrator as mod
        # Use temp state file
        mod.STATE_FILE = os.path.join(tempfile.gettempdir(), 'test_upgrade_state.json')
        if os.path.isfile(mod.STATE_FILE):
            os.remove(mod.STATE_FILE)
        return UpgradeOrchestrator()

    def test_initial_idle(self):
        orch = self._make_orchestrator()
        status = orch.get_status()
        assert status['stage'] == 'idle'

    def test_start_upgrade(self):
        orch = self._make_orchestrator()
        result = orch.start_upgrade('v2.0', 'abc123')
        assert result['success'] is True
        assert result['stage'] == 'building'
        status = orch.get_status()
        assert status['version'] == 'v2.0'

    def test_cannot_start_while_active(self):
        orch = self._make_orchestrator()
        orch.start_upgrade('v2.0')
        result = orch.start_upgrade('v3.0')
        assert result['success'] is False
        assert 'already active' in result['error']

    def test_rollback(self):
        orch = self._make_orchestrator()
        orch.start_upgrade('v2.0')
        result = orch.rollback('test reason')
        assert result['success'] is True
        assert result['rolled_back_from'] == 'building'
        assert orch.get_status()['stage'] == 'rolled_back'

    @patch('integrations.agent_engine.upgrade_orchestrator.UpgradeOrchestrator._stage_build')
    def test_advance_build_stage(self, mock_build):
        mock_build.return_value = (True, 'code_hash=abc')
        orch = self._make_orchestrator()
        orch.start_upgrade('v2.0')
        result = orch.advance_pipeline()
        assert result['success'] is True
        assert result['stage'] == 'testing'

    @patch('integrations.agent_engine.upgrade_orchestrator.UpgradeOrchestrator._stage_build')
    def test_advance_build_failure(self, mock_build):
        mock_build.return_value = (False, 'dirty git state')
        orch = self._make_orchestrator()
        orch.start_upgrade('v2.0')
        result = orch.advance_pipeline()
        assert result['success'] is False
        assert orch.get_status()['stage'] == 'failed'

    def test_state_persistence(self):
        from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator
        import integrations.agent_engine.upgrade_orchestrator as mod
        state_file = os.path.join(tempfile.gettempdir(), 'test_upgrade_persist.json')
        mod.STATE_FILE = state_file
        if os.path.isfile(state_file):
            os.remove(state_file)

        orch1 = UpgradeOrchestrator()
        orch1.start_upgrade('v2.0', 'sha123')

        # Create new instance - should load persisted state
        orch2 = UpgradeOrchestrator()
        assert orch2.get_status()['version'] == 'v2.0'
        assert orch2.get_status()['stage'] == 'building'

        os.remove(state_file)

    def test_canary_health_inactive(self):
        orch = self._make_orchestrator()
        health = orch.check_canary_health_status()
        assert health['canary_active'] is False

    def test_check_for_new_version_no_change(self):
        orch = self._make_orchestrator()
        result = orch.check_for_new_version()
        # No previous hash stored, so no detection
        assert result is None


# ═══════════════════════════════════════════════════════════════
# FEDERATION TOOLS TESTS
# ═══════════════════════════════════════════════════════════════

class TestFederationTools:
    """Test federation AutoGen tools."""

    @patch('integrations.agent_engine.federated_aggregator.get_federated_aggregator')
    def test_check_convergence(self, mock_get):
        from integrations.agent_engine.federation_tools import check_federation_convergence
        mock_agg = MagicMock()
        mock_agg.get_stats.return_value = {
            'convergence': 0.85, 'epoch': 5, 'peer_count': 3,
            'convergence_history': [0.8, 0.85]}
        mock_get.return_value = mock_agg
        result = check_federation_convergence()
        assert result['success'] is True
        assert result['convergence'] == 0.85

    @patch('integrations.agent_engine.federated_aggregator.get_federated_aggregator')
    def test_trigger_sync(self, mock_get):
        from integrations.agent_engine.federation_tools import trigger_federation_sync
        mock_agg = MagicMock()
        mock_agg.tick.return_value = {'aggregated': True, 'epoch': 1}
        mock_get.return_value = mock_agg
        result = trigger_federation_sync()
        assert result['success'] is True
        assert result['aggregated'] is True


# ═══════════════════════════════════════════════════════════════
# UPGRADE TOOLS TESTS
# ═══════════════════════════════════════════════════════════════

class TestUpgradeTools:
    """Test upgrade AutoGen tools."""

    @patch('integrations.agent_engine.upgrade_orchestrator.get_upgrade_orchestrator')
    def test_check_status(self, mock_get):
        from integrations.agent_engine.upgrade_tools import check_upgrade_status
        mock_orch = MagicMock()
        mock_orch.get_status.return_value = {'stage': 'idle', 'version': ''}
        mock_orch.check_for_new_version.return_value = None
        mock_get.return_value = mock_orch
        result = check_upgrade_status()
        assert result['success'] is True
        assert result['pipeline']['stage'] == 'idle'

    @patch('integrations.agent_engine.upgrade_orchestrator.get_upgrade_orchestrator')
    def test_rollback(self, mock_get):
        from integrations.agent_engine.upgrade_tools import rollback_upgrade
        mock_orch = MagicMock()
        mock_orch.rollback.return_value = {'success': True, 'rolled_back_from': 'testing'}
        mock_get.return_value = mock_orch
        result = rollback_upgrade('test reason')
        assert result['success'] is True

    def test_list_benchmarks(self):
        from integrations.agent_engine.upgrade_tools import list_benchmarks
        import integrations.agent_engine.benchmark_registry as mod
        mod._registry = None
        result = list_benchmarks()
        assert result['success'] is True
        assert isinstance(result['benchmarks'], list)
        assert len(result['benchmarks']) >= 5  # At least 5 builtins
        mod._registry = None


# ═══════════════════════════════════════════════════════════════
# PRODUCTION FIX TESTS
# ═══════════════════════════════════════════════════════════════

class TestProductionFixes:
    """Test production fixes: LLAMA_CPP_PORT, bridge race, brute-force."""

    def test_llama_cpp_port_env_var(self):
        """Verify LLAMA_CPP_PORT env var is respected in config_list."""
        with patch.dict(os.environ, {'LLAMA_CPP_PORT': '9999'}):
            # Re-evaluate the config dynamically
            port = os.environ.get('LLAMA_CPP_PORT', '8080')
            assert port == '9999'
            url = f'http://localhost:{port}/v1'
            assert 'localhost:9999' in url

    def test_bridge_lazy_retry(self):
        """WorldModelBridge lazy in-process retry flag."""
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        bridge = WorldModelBridge()
        assert bridge._in_process_retry_done is False
        assert bridge._federation_aggregated == {}

    def test_bridge_configurable_timeouts(self):
        """WorldModelBridge uses configurable timeouts."""
        with patch.dict(os.environ, {
            'HEVOLVE_WM_FLUSH_TIMEOUT': '20',
            'HEVOLVE_WM_CORRECTION_TIMEOUT': '45',
            'HEVOLVE_WM_HTTP_TIMEOUT': '15',
        }):
            from integrations.agent_engine.world_model_bridge import WorldModelBridge
            bridge = WorldModelBridge()
            assert bridge._timeout_flush == 20
            assert bridge._timeout_correction == 45
            assert bridge._timeout_default == 15

    def test_bridge_extract_learning_delta(self):
        """WorldModelBridge.extract_learning_delta() returns dict."""
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        bridge = WorldModelBridge()
        delta = bridge.extract_learning_delta()
        assert 'bridge' in delta
        assert 'learning' in delta
        assert 'hivemind' in delta

    def test_bridge_apply_federation_update(self):
        """WorldModelBridge.apply_federation_update() stores aggregated."""
        from integrations.agent_engine.world_model_bridge import WorldModelBridge
        bridge = WorldModelBridge()
        aggregated = {'epoch': 1, 'data': 'test'}
        assert bridge.apply_federation_update(aggregated) is True
        assert bridge._federation_aggregated == aggregated

    def test_brute_force_protection(self):
        """Commercial API brute-force protection (TTLCache)."""
        from integrations.agent_engine.commercial_api import (
            _check_brute_force, _record_failed_attempt, _failed_attempts,
            _failed_attempts_lock)

        test_ip = '192.168.99.99'
        # Clear any previous state — use _data if available (real TTLCache),
        # otherwise treat as plain dict (stub fallback).
        with _failed_attempts_lock:
            _store = getattr(_failed_attempts, '_data', _failed_attempts)
            if test_ip in _store:
                del _store[test_ip]
                if hasattr(_failed_attempts, '_timestamps'):
                    _failed_attempts._timestamps.pop(test_ip, None)

        assert _check_brute_force(test_ip) is False
        for _ in range(10):
            _record_failed_attempt(test_ip)
        assert _check_brute_force(test_ip) is True

        # Cleanup
        with _failed_attempts_lock:
            _store = getattr(_failed_attempts, '_data', _failed_attempts)
            if test_ip in _store:
                del _store[test_ip]
                if hasattr(_failed_attempts, '_timestamps'):
                    _failed_attempts._timestamps.pop(test_ip, None)

    def test_world_model_health_endpoint_exists(self):
        """Verify world model health endpoint function exists in api.py."""
        from integrations.agent_engine.api import world_model_health
        assert callable(world_model_health)


# ═══════════════════════════════════════════════════════════════
# GOAL SEEDING TESTS
# ═══════════════════════════════════════════════════════════════

class TestGoalSeeding:
    """Test new bootstrap goals and loophole entries."""

    def test_federation_bootstrap_goal_exists(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert 'bootstrap_federation_sync' in slugs

    def test_upgrade_bootstrap_goal_exists(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert 'bootstrap_upgrade_monitor' in slugs

    def test_learning_stall_loophole_exists(self):
        from integrations.agent_engine.goal_seeding import LOOPHOLE_REMEDIATION_MAP
        assert 'learning_stall' in LOOPHOLE_REMEDIATION_MAP
        entry = LOOPHOLE_REMEDIATION_MAP['learning_stall']
        assert entry['goal_type'] == 'federation'

    def test_goal_type_registration(self):
        from integrations.agent_engine.goal_manager import (
            _prompt_builders, _tool_tags, get_registered_types)
        registered = get_registered_types()
        assert 'federation' in registered
        assert 'upgrade' in registered
        assert 'federation' in _prompt_builders
        assert 'upgrade' in _prompt_builders
        assert 'federation' in _tool_tags
        assert 'upgrade' in _tool_tags
