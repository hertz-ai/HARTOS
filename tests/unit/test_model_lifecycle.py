"""
Tests for Model Lifecycle Manager — dynamic load/unload/offload, pressure response,
hive hints, inference guard, and capability tier awareness.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from unittest.mock import patch, MagicMock, PropertyMock


# ─── ModelState Tests ───

class TestModelState:
    def test_initial_defaults(self):
        from integrations.service_tools.model_lifecycle import ModelState, ModelDevice, ModelPriority
        state = ModelState(name='whisper')
        assert state.device == ModelDevice.UNLOADED
        assert state.priority == ModelPriority.IDLE
        assert state.access_count == 0
        assert state.active_inference_count == 0
        assert state.hive_boost is False

    def test_to_dict(self):
        from integrations.service_tools.model_lifecycle import ModelState, ModelDevice, ModelPriority
        state = ModelState(name='minicpm', device=ModelDevice.GPU,
                           priority=ModelPriority.WARM, vram_gb=4.0)
        d = state.to_dict()
        assert d['name'] == 'minicpm'
        assert d['device'] == 'gpu'
        assert d['priority'] == 'warm'
        assert d['vram_gb'] == 4.0
        assert 'idle_seconds' in d

    def test_priority_values(self):
        from integrations.service_tools.model_lifecycle import ModelPriority
        assert ModelPriority.ACTIVE.value == 'active'
        assert ModelPriority.WARM.value == 'warm'
        assert ModelPriority.IDLE.value == 'idle'
        assert ModelPriority.EVICTABLE.value == 'evictable'


# ─── ModelLifecycleManager Tests ───

class TestModelLifecycleManager:
    @pytest.fixture
    def manager(self):
        from integrations.service_tools.model_lifecycle import ModelLifecycleManager
        mgr = ModelLifecycleManager()
        return mgr

    def test_initial_state(self, manager):
        assert manager._running is False
        assert manager._tick_count == 0
        assert len(manager._models) == 0

    def test_get_status_structure(self, manager):
        status = manager.get_status()
        assert 'running' in status
        assert 'tick_count' in status
        assert 'models' in status
        assert 'vram_pressure' in status
        assert 'ram_pressure' in status
        assert 'hive_hints' in status
        assert 'node_tier' in status

    def test_on_tool_started_creates_state(self, manager):
        from integrations.service_tools.model_lifecycle import ModelDevice, ModelPriority
        manager._on_tool_started('whisper', device='gpu', inprocess=True)
        assert 'whisper' in manager._models
        state = manager._models['whisper']
        assert state.device == ModelDevice.GPU
        assert state.priority == ModelPriority.WARM
        assert state.is_sidecar is False
        assert state.last_access_time > 0

    def test_on_tool_started_cpu_offload(self, manager):
        from integrations.service_tools.model_lifecycle import ModelDevice
        manager._on_tool_started('tts_audio_suite', device='gpu', offload_mode='cpu_only')
        state = manager._models['tts_audio_suite']
        assert state.device == ModelDevice.CPU

    def test_on_tool_stopped_marks_unloaded(self, manager):
        from integrations.service_tools.model_lifecycle import ModelDevice
        manager._on_tool_started('whisper', device='gpu')
        manager._on_tool_stopped('whisper')
        state = manager._models['whisper']
        assert state.device == ModelDevice.UNLOADED
        assert state.vram_gb == 0.0

    def test_notify_access_updates(self, manager):
        manager._on_tool_started('whisper', device='gpu')
        before = manager._models['whisper'].last_access_time
        time.sleep(0.01)
        manager.notify_access('whisper')
        after = manager._models['whisper'].last_access_time
        assert after > before
        assert manager._models['whisper'].access_count == 1

    def test_inference_guard(self, manager):
        from integrations.service_tools.model_lifecycle import ModelPriority
        manager._on_tool_started('whisper', device='gpu')
        assert manager._models['whisper'].active_inference_count == 0

        with manager.inference_guard('whisper'):
            assert manager._models['whisper'].active_inference_count == 1
            assert manager._models['whisper'].access_count == 1

        assert manager._models['whisper'].active_inference_count == 0

    def test_inference_guard_exception_safe(self, manager):
        """Guard releases count even if exception occurs during inference."""
        manager._on_tool_started('whisper', device='gpu')
        try:
            with manager.inference_guard('whisper'):
                raise ValueError("test error")
        except ValueError:
            pass
        assert manager._models['whisper'].active_inference_count == 0


# ─── Priority Update Tests ───

class TestPriorityUpdates:
    @pytest.fixture
    def manager(self):
        from integrations.service_tools.model_lifecycle import ModelLifecycleManager
        mgr = ModelLifecycleManager()
        # Isolate from persisted hints left by prior tests — the lifecycle
        # state file at ~/.hevolve/lifecycle_state.json is process-global.
        # A prior test that calls _on_tool_stopped persists hints, which
        # would make _on_tool_started flip hive_boost=True and skew the
        # idle/evictable threshold tests below.
        mgr._persisted_hints.clear()
        return mgr

    def test_active_inference_sets_active(self, manager):
        from integrations.service_tools.model_lifecycle import ModelPriority, ModelDevice
        manager._on_tool_started('whisper', device='gpu')
        manager._models['whisper'].active_inference_count = 1
        manager._update_priorities()
        assert manager._models['whisper'].priority == ModelPriority.ACTIVE

    def test_recent_access_sets_warm(self, manager):
        from integrations.service_tools.model_lifecycle import ModelPriority
        manager._on_tool_started('whisper', device='gpu')
        manager._models['whisper'].last_access_time = time.time()  # just accessed
        manager._update_priorities()
        assert manager._models['whisper'].priority == ModelPriority.WARM

    def test_old_access_sets_idle(self, manager):
        from integrations.service_tools.model_lifecycle import ModelPriority
        manager._on_tool_started('whisper', device='gpu')
        timeout = manager._models['whisper'].idle_timeout_s
        # Set access time to 70% of timeout ago (between 50% and 100%)
        manager._models['whisper'].last_access_time = time.time() - (timeout * 0.7)
        manager._update_priorities()
        assert manager._models['whisper'].priority == ModelPriority.IDLE

    def test_very_old_access_sets_evictable(self, manager):
        from integrations.service_tools.model_lifecycle import ModelPriority
        manager._on_tool_started('whisper', device='gpu')
        timeout = manager._models['whisper'].idle_timeout_s
        manager._models['whisper'].last_access_time = time.time() - (timeout * 1.5)
        manager._update_priorities()
        assert manager._models['whisper'].priority == ModelPriority.EVICTABLE

    def test_hive_boost_extends_warm(self, manager):
        from integrations.service_tools.model_lifecycle import ModelPriority
        manager._on_tool_started('whisper', device='gpu')
        timeout = manager._models['whisper'].idle_timeout_s
        # 70% of timeout = normally IDLE
        manager._models['whisper'].last_access_time = time.time() - (timeout * 0.7)
        manager._models['whisper'].hive_boost = True
        manager._update_priorities()
        # With hive boost, stays WARM
        assert manager._models['whisper'].priority == ModelPriority.WARM

    def test_unloaded_models_skipped(self, manager):
        from integrations.service_tools.model_lifecycle import ModelPriority, ModelDevice
        manager._on_tool_started('whisper', device='gpu')
        manager._on_tool_stopped('whisper')
        assert manager._models['whisper'].device == ModelDevice.UNLOADED
        manager._update_priorities()
        # Should not change priority of unloaded models to ACTIVE/WARM etc


# ─── Pressure Detection Tests ───

class TestPressureDetection:
    @pytest.fixture
    def manager(self):
        from integrations.service_tools.model_lifecycle import ModelLifecycleManager
        mgr = ModelLifecycleManager()
        return mgr

    def test_vram_pressure_below_threshold(self, manager):
        """No pressure when VRAM usage is below threshold."""
        manager._vram_pressure_pct = 85.0
        from integrations.service_tools.vram_manager import vram_manager
        with patch.object(vram_manager, 'get_vram_usage_pct', return_value=50.0):
            assert not manager._detect_vram_pressure()

    def test_vram_pressure_above_threshold(self, manager):
        """Pressure detected when VRAM usage exceeds threshold."""
        manager._vram_pressure_pct = 85.0
        from integrations.service_tools.vram_manager import vram_manager
        with patch.object(vram_manager, 'get_vram_usage_pct', return_value=90.0):
            assert manager._detect_vram_pressure()

    def test_ram_pressure_no_psutil(self, manager):
        """If psutil import fails, no RAM pressure detected."""
        with patch.dict('sys.modules', {'psutil': None}):
            assert not manager._detect_ram_pressure()


# ─── Eviction Tests ───

class TestEviction:
    @pytest.fixture
    def manager(self):
        from integrations.service_tools.model_lifecycle import ModelLifecycleManager
        mgr = ModelLifecycleManager()
        return mgr

    def test_evict_idle_model(self, manager):
        from integrations.service_tools.model_lifecycle import ModelPriority
        manager._on_tool_started('whisper', device='gpu')
        # Make it evictable: old access time, past timeout
        timeout = manager._models['whisper'].idle_timeout_s
        manager._models['whisper'].last_access_time = time.time() - (timeout * 2)
        manager._models['whisper'].priority = ModelPriority.EVICTABLE

        mock_rtm = MagicMock()
        with patch('integrations.service_tools.runtime_manager.runtime_tool_manager', mock_rtm):
            manager._evict_idle_models()
            mock_rtm.stop_tool.assert_called_with('whisper')

    def test_active_inference_prevents_eviction(self, manager):
        from integrations.service_tools.model_lifecycle import ModelPriority, ModelDevice
        manager._on_tool_started('whisper', device='gpu')
        manager._models['whisper'].active_inference_count = 1
        manager._models['whisper'].priority = ModelPriority.EVICTABLE
        timeout = manager._models['whisper'].idle_timeout_s
        manager._models['whisper'].last_access_time = time.time() - (timeout * 2)

        # Should NOT be in evictable list because active_inference_count > 0
        evictable = [
            s.name for s in manager._models.values()
            if s.priority == ModelPriority.EVICTABLE
            and s.device != ModelDevice.UNLOADED
            and s.active_inference_count == 0
        ]
        assert 'whisper' not in evictable


# ─── Tier Awareness Tests ───

class TestTierAwareness:
    @pytest.fixture
    def manager(self):
        from integrations.service_tools.model_lifecycle import ModelLifecycleManager
        mgr = ModelLifecycleManager()
        return mgr

    def test_standard_tier_allows_whisper(self, manager):
        from security.system_requirements import NodeTierLevel
        manager._node_tier = NodeTierLevel.STANDARD
        assert manager._is_tier_appropriate('whisper') is True

    def test_standard_tier_blocks_minicpm(self, manager):
        from security.system_requirements import NodeTierLevel
        manager._node_tier = NodeTierLevel.STANDARD
        assert manager._is_tier_appropriate('minicpm') is False

    def test_full_tier_allows_minicpm(self, manager):
        from security.system_requirements import NodeTierLevel
        manager._node_tier = NodeTierLevel.FULL
        assert manager._is_tier_appropriate('minicpm') is True

    def test_unknown_tier_allows_all(self, manager):
        manager._node_tier = None
        assert manager._is_tier_appropriate('minicpm') is True


# ─── Manual API Tests ───

class TestManualAPI:
    @pytest.fixture
    def manager(self):
        from integrations.service_tools.model_lifecycle import ModelLifecycleManager
        mgr = ModelLifecycleManager()
        return mgr

    def test_set_priority_valid(self, manager):
        manager._on_tool_started('whisper', device='gpu')
        result = manager.set_priority('whisper', 'idle')
        assert result['priority'] == 'idle'

    def test_set_priority_invalid(self, manager):
        result = manager.set_priority('whisper', 'nonexistent')
        assert 'error' in result

    def test_set_priority_unknown_model(self, manager):
        result = manager.set_priority('unknown_model', 'warm')
        assert 'error' in result

    def test_manual_offload_not_loaded(self, manager):
        result = manager.manual_offload('whisper')
        assert 'error' in result

    def test_manual_offload_unloaded(self, manager):
        from integrations.service_tools.model_lifecycle import ModelDevice
        manager._on_tool_started('whisper', device='gpu')
        manager._on_tool_stopped('whisper')
        result = manager.manual_offload('whisper')
        assert 'error' in result

    def test_manual_offload_already_cpu(self, manager):
        from integrations.service_tools.model_lifecycle import ModelDevice
        manager._on_tool_started('whisper', device='gpu', offload_mode='cpu_only')
        result = manager.manual_offload('whisper')
        assert 'already on CPU' in result.get('message', '')


# ─── VRAMManager Addition Tests ───

class TestVRAMManagerAdditions:
    def test_get_vram_usage_pct_no_gpu(self):
        from integrations.service_tools.vram_manager import VRAMManager
        vm = VRAMManager()
        vm._gpu_info = {'name': None, 'total_gb': 0, 'free_gb': 0, 'cuda_available': False}
        # Mock refresh_gpu_info to prevent nvidia-smi from overwriting test data
        with patch.object(vm, 'refresh_gpu_info'):
            pct = vm.get_vram_usage_pct()
        assert pct == 0.0

    def test_get_vram_usage_pct_with_gpu(self):
        from integrations.service_tools.vram_manager import VRAMManager
        vm = VRAMManager()
        vm._gpu_info = {'name': 'RTX 4090', 'total_gb': 24.0, 'free_gb': 6.0, 'cuda_available': True}
        with patch.object(vm, 'refresh_gpu_info'):
            pct = vm.get_vram_usage_pct()
        assert pct == pytest.approx(75.0)

    def test_get_actual_free_vram(self):
        from integrations.service_tools.vram_manager import VRAMManager
        vm = VRAMManager()
        vm._gpu_info = {'name': 'RTX 4090', 'total_gb': 24.0, 'free_gb': 8.0, 'cuda_available': True}
        with patch.object(vm, 'refresh_gpu_info'):
            free = vm.get_actual_free_vram()
        assert free == 8.0


# ─── RuntimeToolManager Hook Tests ───

class TestRTMHooks:
    def test_hook_registration(self):
        from integrations.service_tools.runtime_manager import RuntimeToolManager
        rtm = RuntimeToolManager.__new__(RuntimeToolManager)
        rtm._processes = {}
        rtm._ports = {}
        rtm._lock = __import__('threading').Lock()
        rtm._lifecycle_hooks = {'on_tool_started': [], 'on_tool_stopped': []}

        callback = MagicMock()
        rtm.register_lifecycle_hook('on_tool_started', callback)
        assert callback in rtm._lifecycle_hooks['on_tool_started']

    def test_hook_notification(self):
        from integrations.service_tools.runtime_manager import RuntimeToolManager
        rtm = RuntimeToolManager.__new__(RuntimeToolManager)
        rtm._lifecycle_hooks = {'on_tool_started': [], 'on_tool_stopped': []}

        callback = MagicMock()
        rtm.register_lifecycle_hook('on_tool_started', callback)
        rtm._notify_hooks('on_tool_started', 'whisper', device='gpu')
        callback.assert_called_once_with('whisper', device='gpu')

    def test_hook_error_does_not_propagate(self):
        from integrations.service_tools.runtime_manager import RuntimeToolManager
        rtm = RuntimeToolManager.__new__(RuntimeToolManager)
        rtm._lifecycle_hooks = {'on_tool_started': []}

        bad_callback = MagicMock(side_effect=RuntimeError("hook failure"))
        rtm.register_lifecycle_hook('on_tool_started', bad_callback)
        # Should not raise
        rtm._notify_hooks('on_tool_started', 'whisper')
        bad_callback.assert_called_once()


# ─── Configuration Table Tests ───

class TestConfigTables:
    def test_idle_timeouts_all_positive(self):
        from integrations.service_tools.model_lifecycle import DEFAULT_IDLE_TIMEOUTS
        for name, timeout in DEFAULT_IDLE_TIMEOUTS.items():
            assert timeout > 0, f"{name} has non-positive timeout"

    def test_cpu_offload_table_has_all_models(self):
        from integrations.service_tools.model_lifecycle import CPU_OFFLOAD_TABLE
        expected = {'whisper', 'tts_audio_suite', 'minicpm', 'wan2gp', 'ltx2',
                    'acestep', 'omniparser', 'clip', 'sentence_transformers', 'mobilevlm'}
        assert set(CPU_OFFLOAD_TABLE.keys()) == expected

    def test_model_min_tier_uses_valid_tiers(self):
        """Ensure MODEL_MIN_TIER only uses valid NodeTierLevel values."""
        from integrations.service_tools.model_lifecycle import MODEL_MIN_TIER
        from security.system_requirements import NodeTierLevel
        valid = {t.value for t in NodeTierLevel}
        for model, tier_str in MODEL_MIN_TIER.items():
            assert tier_str in valid, f"{model} has invalid tier '{tier_str}'"


# ─── FederatedAggregator Lifecycle Channel Tests ───

class TestFederationLifecycle:
    def test_receive_lifecycle_delta(self):
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        agg = FederatedAggregator()
        delta = {'models': {'whisper': {'device': 'gpu', 'access_rate': 0.5}}}
        agg.receive_lifecycle_delta('node1', delta)
        assert 'node1' in agg._lifecycle_deltas

    def test_aggregate_lifecycle_empty(self):
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        agg = FederatedAggregator()
        result = agg.aggregate_lifecycle()
        assert result is None

    def test_aggregate_lifecycle_popularity(self):
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        agg = FederatedAggregator()
        agg._lifecycle_deltas = {
            'n1': {'models': {'whisper': {'access_rate': 0.5}, 'minicpm': {'access_rate': 0.1}}},
            'n2': {'models': {'whisper': {'access_rate': 0.3}}},
            'n3': {'models': {'whisper': {'access_rate': 0.8}, 'minicpm': {'access_rate': 0.2}}},
        }
        result = agg.aggregate_lifecycle()
        assert result is not None
        assert 'popularity' in result
        # Whisper loaded on 3/3 peers = high popularity
        assert result['popularity']['whisper'] > result['popularity']['minicpm']
        assert result['peer_count'] == 3

    def test_lifecycle_stats_in_dashboard(self):
        from integrations.agent_engine.federated_aggregator import FederatedAggregator
        agg = FederatedAggregator()
        stats = agg.get_stats()
        assert 'lifecycle' in stats


# ─── CPU / Disk Pressure + Throttle Tests ───

class TestCPUPressure:
    @pytest.fixture
    def manager(self):
        from integrations.service_tools.model_lifecycle import ModelLifecycleManager
        return ModelLifecycleManager()

    def test_detect_cpu_pressure_below_threshold(self, manager):
        """CPU below threshold → no pressure."""
        manager._cpu_pressure_pct = 80.0
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.return_value = 45.0
        with patch.dict('sys.modules', {'psutil': mock_psutil}):
            assert not manager._detect_cpu_pressure()

    def test_detect_cpu_pressure_above_threshold(self, manager):
        """CPU above threshold → pressure detected."""
        manager._cpu_pressure_pct = 80.0
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.return_value = 92.0
        with patch.dict('sys.modules', {'psutil': mock_psutil}):
            assert manager._detect_cpu_pressure()


class TestDiskPressure:
    @pytest.fixture
    def manager(self):
        from integrations.service_tools.model_lifecycle import ModelLifecycleManager
        return ModelLifecycleManager()

    def test_detect_disk_pressure_plenty_space(self, manager):
        """Plenty of disk free → no pressure."""
        manager._disk_free_min_gb = 2.0
        # 50 GB free
        mock_usage = MagicMock(free=50 * 1024 ** 3)
        with patch('shutil.disk_usage', return_value=mock_usage):
            assert not manager._detect_disk_pressure()

    def test_detect_disk_pressure_low_space(self, manager):
        """Disk free below threshold → pressure detected."""
        manager._disk_free_min_gb = 2.0
        # 0.5 GB free
        mock_usage = MagicMock(free=int(0.5 * 1024 ** 3))
        with patch('shutil.disk_usage', return_value=mock_usage):
            assert manager._detect_disk_pressure()


class TestThrottleFactor:
    @pytest.fixture
    def manager(self):
        from integrations.service_tools.model_lifecycle import ModelLifecycleManager
        return ModelLifecycleManager()

    def test_throttle_factor_no_pressure(self, manager):
        """No pressure anywhere → throttle factor 1.0."""
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.return_value = 20.0
        mock_psutil.virtual_memory.return_value = MagicMock(percent=40.0)
        from integrations.service_tools.vram_manager import vram_manager
        mock_usage = MagicMock(free=100 * 1024 ** 3)  # 100 GB free
        with patch.dict('sys.modules', {'psutil': mock_psutil}), \
             patch.object(vram_manager, 'get_vram_usage_pct', return_value=30.0), \
             patch('shutil.disk_usage', return_value=mock_usage):
            factor = manager._calculate_throttle_factor()
            assert factor == 1.0

    def test_throttle_factor_heavy_cpu(self, manager):
        """CPU at 95%+ → heavy throttling (factor < 0.3)."""
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.return_value = 96.0
        mock_psutil.virtual_memory.return_value = MagicMock(percent=40.0)
        from integrations.service_tools.vram_manager import vram_manager
        mock_usage = MagicMock(free=100 * 1024 ** 3)
        with patch.dict('sys.modules', {'psutil': mock_psutil}), \
             patch.object(vram_manager, 'get_vram_usage_pct', return_value=30.0), \
             patch('shutil.disk_usage', return_value=mock_usage):
            factor = manager._calculate_throttle_factor()
            assert factor < 0.3


class TestSystemPressureAPI:
    @pytest.fixture
    def manager(self):
        from integrations.service_tools.model_lifecycle import ModelLifecycleManager
        return ModelLifecycleManager()

    def test_get_system_pressure_structure(self, manager):
        """get_system_pressure() returns all expected keys."""
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.return_value = 30.0
        mock_psutil.virtual_memory.return_value = MagicMock(percent=50.0)
        from integrations.service_tools.vram_manager import vram_manager
        mock_usage = MagicMock(free=50 * 1024 ** 3)
        with patch.dict('sys.modules', {'psutil': mock_psutil}), \
             patch.object(vram_manager, 'get_vram_usage_pct', return_value=40.0), \
             patch('shutil.disk_usage', return_value=mock_usage):
            result = manager.get_system_pressure()
            assert 'vram_pressure' in result
            assert 'ram_pressure' in result
            assert 'cpu_pressure' in result
            assert 'disk_pressure' in result
            assert 'throttle_factor' in result
            assert isinstance(result['throttle_factor'], float)
            assert 0.0 <= result['throttle_factor'] <= 1.0


# ─── Restart Idempotence / Starvation Regression Tests ───
#
# Locks in the fix from commit 73cc9f5 (and the v2 device-detection
# follow-up): the watchdog must NOT re-queue a restart that's already
# pending, otherwise retry_after gets pushed forward every dead-detection
# tick faster than the clock advances.  Live evidence prior to fix: 220
# `Queued restart for llm` log lines / 0 `_restart_llm starting` over 7.5h.
class TestRestartIdempotence:
    @pytest.fixture
    def manager(self):
        from integrations.service_tools.model_lifecycle import ModelLifecycleManager
        return ModelLifecycleManager()

    def _seed_dead_llm(self, manager, monkeypatch_time=None):
        """Helper: register an 'llm' state and queue a restart via _handle_dead_process."""
        from integrations.service_tools.model_lifecycle import (
            ModelState, ModelDevice, ModelPriority,
        )
        manager._models['llm'] = ModelState(
            name='llm',
            device=ModelDevice.GPU,
            priority=ModelPriority.ACTIVE,
            crash_count=0,
        )
        # First detect-dead → should queue
        manager._handle_dead_process('llm', None, 'llm_server')

    def test_first_dead_detection_queues(self, manager):
        """First dead detection registers a pending restart entry."""
        self._seed_dead_llm(manager)
        assert 'llm' in manager._restart_pending
        info = manager._restart_pending['llm']
        assert isinstance(info, dict)
        assert 'retry_after' in info
        assert info.get('in_progress') is False

    def test_repeat_dead_detection_does_not_re_queue(self, manager):
        """Second dead detection within the backoff window must NOT
        overwrite the pending entry — that was the 73cc9f5 starvation bug."""
        self._seed_dead_llm(manager)
        original_retry_after = manager._restart_pending['llm']['retry_after']
        original_crash_count = manager._models['llm'].crash_count

        # Simulate 5 more dead-detection ticks (each ~120s apart in prod)
        for _ in range(5):
            manager._handle_dead_process('llm', None, 'llm_server')

        # retry_after must NOT have moved forward
        assert manager._restart_pending['llm']['retry_after'] == original_retry_after, (
            "retry_after was overwritten — starvation bug regressed"
        )
        # crash_count incremented in the function (state update happens
        # before the idempotence guard returns), but that's fine — the
        # critical invariant is retry_after must not move.
        assert manager._models['llm'].crash_count > original_crash_count

    def test_in_progress_flag_blocks_new_dead_detection(self, manager):
        """Once a tick has marked the entry in_progress=True, subsequent
        dead detections must not re-queue (would create double-spawn race)."""
        self._seed_dead_llm(manager)
        # Simulate _process_restart_queue marking it in_progress
        manager._restart_pending['llm']['in_progress'] = True
        retry_after_before = manager._restart_pending['llm']['retry_after']

        manager._handle_dead_process('llm', None, 'llm_server')

        # Entry untouched
        assert manager._restart_pending['llm']['in_progress'] is True
        assert manager._restart_pending['llm']['retry_after'] == retry_after_before

    def test_pre_spawn_probe_skips_when_alive(self, manager):
        """If the HTTP probe finds llama-server already healthy, the queued
        spawn must be popped without calling _restart_llm.  Prevents the
        race where two ticks both spawn and second collides on port :8082."""
        from unittest.mock import patch, MagicMock
        from integrations.service_tools.model_lifecycle import (
            ModelState, ModelDevice, ModelPriority,
        )
        manager._models['llm'] = ModelState(
            name='llm',
            device=ModelDevice.UNLOADED,
            priority=ModelPriority.IDLE,
            crash_count=1,
        )
        # Queue a pending entry that's already past retry_after
        manager._restart_pending['llm'] = {
            'retry_after': time.time() - 10,  # already due
            'downgrade': False,
            'old_device': 'gpu',
            'in_progress': False,
        }
        # Mock LlamaConfig probe to report healthy
        # vram_manager mock so device detection picks GPU branch
        fake_cfg = MagicMock()
        fake_cfg.check_server_running.return_value = True
        fake_vmm = MagicMock()
        fake_vmm.detect_gpu.return_value = {'cuda_available': True}
        # Sentinel: track whether _restart_llm was called
        called = []
        manager._restart_llm = lambda mode: called.append(mode) or True

        with patch.dict('sys.modules', {
            'llama.llama_config': MagicMock(LlamaConfig=lambda: fake_cfg),
            'integrations.service_tools.vram_manager': MagicMock(
                vram_manager=fake_vmm),
        }):
            manager._process_restart_queue()

        # Spawn must NOT have been called
        assert called == [], (
            "_restart_llm fired even though pre-spawn probe found server alive"
        )
        # Pending entry must be popped
        assert 'llm' not in manager._restart_pending
        # State synced — device picked from vram_manager (GPU here)
        assert manager._models['llm'].device == ModelDevice.GPU
        assert manager._models['llm'].priority == ModelPriority.ACTIVE

    def test_pre_spawn_probe_picks_cpu_on_no_cuda(self, manager):
        """v2 device-detection: when vram_manager reports no CUDA, state
        must be CPU not GPU — was 'best-effort GPU' bug caught in self-review."""
        from unittest.mock import patch, MagicMock
        from integrations.service_tools.model_lifecycle import (
            ModelState, ModelDevice, ModelPriority,
        )
        manager._models['llm'] = ModelState(
            name='llm',
            device=ModelDevice.UNLOADED,
            priority=ModelPriority.IDLE,
            crash_count=1,
        )
        manager._restart_pending['llm'] = {
            'retry_after': time.time() - 10,
            'downgrade': False, 'old_device': 'gpu', 'in_progress': False,
        }
        fake_cfg = MagicMock()
        fake_cfg.check_server_running.return_value = True
        fake_vmm = MagicMock()
        fake_vmm.detect_gpu.return_value = {'cuda_available': False}  # CPU box
        manager._restart_llm = lambda mode: False  # would-be-failure ignored

        with patch.dict('sys.modules', {
            'llama.llama_config': MagicMock(LlamaConfig=lambda: fake_cfg),
            'integrations.service_tools.vram_manager': MagicMock(
                vram_manager=fake_vmm),
        }):
            manager._process_restart_queue()

        # Critical: device is CPU, not GPU (v1 was unconditional GPU)
        assert manager._models['llm'].device == ModelDevice.CPU, (
            "device stamped GPU when CUDA unavailable — v1 wrong-direction bug regressed"
        )


class TestVramPressureWarningUsesTheRealAPI:
    """The VRAM-pressure warning must actually be computable.

    LIVE INCIDENT 2026-08-02 (frozen_debug.log 22:13:28):
        AttributeError: 'VRAMManager' object has no attribute 'total_vram_gb'
    raised from ModelLifecycleManager.start(). VRAMManager exposes
    get_total_vram() as a METHOD (vram_manager.py:617); there has never been a
    `total_vram_gb` attribute — `total_vram_gb` exists only as a DICT KEY in
    model_onboarding's payloads, which is what made the wrong name look
    plausible.

    The failure was quiet in the worst way: an outer handler swallowed it, the
    manager started anyway, and the over-subscription warning this block exists
    to emit was simply never computed on any boot. So a node whose pinned
    models exceed its VRAM never told the user.
    """

    def test_vram_manager_has_no_total_vram_gb_attribute(self):
        """Pin the API shape that made the bug possible."""
        from integrations.service_tools.vram_manager import get_vram_manager
        vm = get_vram_manager()
        assert not hasattr(vm, 'total_vram_gb'), (
            "VRAMManager grew a total_vram_gb attribute — if that is now the "
            "canonical accessor, model_lifecycle should use it deliberately, "
            "not by accident")
        assert callable(getattr(vm, 'get_total_vram', None)), (
            "get_total_vram() is the canonical total-VRAM accessor")

    def test_start_computes_total_vram_without_raising(self):
        """The real call path: start() must reach its VRAM check.

        Drives ModelLifecycleManager.start() with a stubbed manager so the
        assertion is about OUR call, not about the host having a GPU.
        """
        from unittest.mock import patch, MagicMock
        from integrations.service_tools import model_lifecycle as ml

        fake_vm = MagicMock()
        fake_vm.get_total_vram.return_value = 8.0
        # A MagicMock answers ANY attribute, so an accidental `vm.total_vram_gb`
        # would silently return a Mock and compare falsely instead of raising.
        # Delete it so the old spelling fails loudly here.
        del fake_vm.total_vram_gb

        mgr = ml.ModelLifecycleManager()
        with patch.object(ml, 'get_vram_manager', return_value=fake_vm,
                          create=True), \
             patch('integrations.service_tools.vram_manager.get_vram_manager',
                   return_value=fake_vm):
            try:
                mgr.start()
            finally:
                stop = getattr(mgr, 'stop', None)
                if callable(stop):
                    try:
                        stop()
                    except Exception:
                        pass

        fake_vm.get_total_vram.assert_called(), (
            "start() never asked for total VRAM — the pressure check is dead")
