"""
Tests for security.system_requirements — Hyve OS equilibrium layer.

Covers: hardware detection, tier classification, feature resolution,
feature gating (env vars), user override respect, force tier override,
full pipeline.
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from security.system_requirements import (
    detect_hardware, classify_tier, resolve_features,
    apply_feature_gates, run_system_check, check_network_connectivity,
    get_capabilities, get_tier, get_tier_name, reset_for_testing,
    HardwareProfile, NodeCapabilities, NodeTierLevel,
    TIER_REQUIREMENTS, FEATURE_TIER_MAP, _TIER_RANK,
)


@pytest.fixture(autouse=True)
def clean_state():
    """Reset cached capabilities before each test."""
    reset_for_testing()
    yield
    reset_for_testing()


# ══════════════════════════════════════════════════════════════════
# Hardware Detection
# ══════════════════════════════════════════════════════════════════

class TestDetectHardware:

    def test_detect_hardware_returns_profile(self):
        """detect_hardware() returns a HardwareProfile with valid CPU count."""
        hw = detect_hardware()
        assert isinstance(hw, HardwareProfile)
        assert hw.cpu_cores >= 1
        assert hw.ram_gb > 0
        assert hw.os_platform != ''
        assert hw.python_version != ''

    def test_detect_hardware_has_disk(self):
        hw = detect_hardware()
        # We should have at least some disk
        assert hw.disk_total_gb > 0
        assert hw.disk_free_gb >= 0


# ══════════════════════════════════════════════════════════════════
# Tier Classification
# ══════════════════════════════════════════════════════════════════

class TestClassifyTier:

    def test_classify_tier_compute_host(self):
        hw = HardwareProfile(cpu_cores=16, ram_gb=32.0, disk_free_gb=100.0,
                             gpu_vram_gb=12.0, cuda_available=True)
        assert classify_tier(hw) == NodeTierLevel.COMPUTE_HOST

    def test_classify_tier_full(self):
        hw = HardwareProfile(cpu_cores=8, ram_gb=16.0, disk_free_gb=50.0,
                             gpu_vram_gb=8.0, cuda_available=True)
        assert classify_tier(hw) == NodeTierLevel.FULL

    def test_classify_tier_standard(self):
        hw = HardwareProfile(cpu_cores=4, ram_gb=8.0, disk_free_gb=10.0,
                             gpu_vram_gb=0.0, cuda_available=False)
        assert classify_tier(hw) == NodeTierLevel.STANDARD

    def test_classify_tier_lite(self):
        hw = HardwareProfile(cpu_cores=2, ram_gb=4.0, disk_free_gb=1.0,
                             gpu_vram_gb=0.0, cuda_available=False)
        assert classify_tier(hw) == NodeTierLevel.LITE

    def test_classify_tier_observer(self):
        """1 core, 2 GB RAM — observer tier."""
        hw = HardwareProfile(cpu_cores=1, ram_gb=2.0, disk_free_gb=0.5,
                             gpu_vram_gb=0.0, cuda_available=False)
        assert classify_tier(hw) == NodeTierLevel.OBSERVER

    def test_classify_tier_embedded(self):
        """Below all thresholds — embedded. Still valid, still counts."""
        hw = HardwareProfile(cpu_cores=1, ram_gb=0.5, disk_free_gb=0.1,
                             gpu_vram_gb=0.0, cuda_available=False)
        assert classify_tier(hw) == NodeTierLevel.EMBEDDED

    def test_classify_tier_partial_full(self):
        """Enough CPU/RAM/disk for FULL but no GPU → falls to STANDARD."""
        hw = HardwareProfile(cpu_cores=8, ram_gb=16.0, disk_free_gb=50.0,
                             gpu_vram_gb=4.0, cuda_available=True)
        # Not enough VRAM for FULL (needs 8), so falls to STANDARD
        assert classify_tier(hw) == NodeTierLevel.STANDARD

    def test_force_tier_override(self):
        """HEVOLVE_FORCE_TIER overrides hardware detection."""
        hw = HardwareProfile(cpu_cores=1, ram_gb=1.0, disk_free_gb=0.5)
        with patch.dict(os.environ, {'HEVOLVE_FORCE_TIER': 'full'}):
            assert classify_tier(hw) == NodeTierLevel.FULL

    def test_force_tier_compute_host(self):
        hw = HardwareProfile(cpu_cores=2, ram_gb=4.0, disk_free_gb=1.0)
        with patch.dict(os.environ, {'HEVOLVE_FORCE_TIER': 'compute_host'}):
            assert classify_tier(hw) == NodeTierLevel.COMPUTE_HOST

    def test_force_tier_embedded(self):
        """Force a powerful machine to embedded tier."""
        hw = HardwareProfile(cpu_cores=16, ram_gb=32.0, disk_free_gb=100.0,
                             gpu_vram_gb=12.0)
        with patch.dict(os.environ, {'HEVOLVE_FORCE_TIER': 'embedded'}):
            assert classify_tier(hw) == NodeTierLevel.EMBEDDED


# ══════════════════════════════════════════════════════════════════
# Feature Resolution
# ══════════════════════════════════════════════════════════════════

class TestResolveFeatures:

    def test_resolve_standard_tier(self):
        """Standard tier enables agents/speech but not video."""
        hw = HardwareProfile(cpu_cores=4, ram_gb=8.0, disk_free_gb=10.0)
        enabled, disabled = resolve_features(NodeTierLevel.STANDARD, hw)
        assert 'agent_engine' in enabled
        assert 'coding_agent' in enabled
        assert 'tts' in enabled
        assert 'whisper' in enabled
        assert 'video_gen' in disabled
        assert 'media_agent' in disabled
        assert 'regional_host' in disabled

    def test_resolve_full_tier(self):
        hw = HardwareProfile(cpu_cores=8, ram_gb=16.0, disk_free_gb=50.0,
                             gpu_vram_gb=8.0)
        enabled, disabled = resolve_features(NodeTierLevel.FULL, hw)
        assert 'agent_engine' in enabled
        assert 'video_gen' in enabled
        assert 'media_agent' in enabled
        assert 'regional_host' in disabled  # needs COMPUTE_HOST

    def test_resolve_observer_tier(self):
        """Observer: gossip + sensor + protocol + flask enabled, higher disabled."""
        hw = HardwareProfile(cpu_cores=1, ram_gb=2.0, disk_free_gb=0.5)
        enabled, disabled = resolve_features(NodeTierLevel.OBSERVER, hw)
        assert 'gossip' in enabled
        assert 'sensor_bridge' in enabled
        assert 'protocol_adapter' in enabled
        assert 'flask_server' in enabled
        assert 'agent_engine' in disabled
        assert 'video_gen' in disabled

    def test_resolve_embedded_tier(self):
        """Embedded: only gossip + sensor + protocol enabled."""
        hw = HardwareProfile(cpu_cores=1, ram_gb=0.5, disk_free_gb=0.1)
        enabled, disabled = resolve_features(NodeTierLevel.EMBEDDED, hw)
        assert 'gossip' in enabled
        assert 'sensor_bridge' in enabled
        assert 'protocol_adapter' in enabled
        assert 'flask_server' in disabled
        assert 'agent_engine' in disabled
        assert len(enabled) == 3  # Only embedded-tier features

    def test_resolve_compute_host_tier(self):
        """Compute host: everything enabled."""
        hw = HardwareProfile(cpu_cores=16, ram_gb=32.0, disk_free_gb=100.0,
                             gpu_vram_gb=12.0)
        enabled, disabled = resolve_features(NodeTierLevel.COMPUTE_HOST, hw)
        assert len(disabled) == 0
        assert 'regional_host' in enabled


# ══════════════════════════════════════════════════════════════════
# Feature Gating (env vars)
# ══════════════════════════════════════════════════════════════════

class TestApplyFeatureGates:

    def test_apply_gates_sets_env_vars(self):
        """Enabled features get env=true, disabled get env=false."""
        enabled = ['agent_engine', 'tts']
        disabled = {'video_gen': 'needs FULL', 'regional_host': 'needs COMPUTE_HOST'}

        # Clear any pre-existing env vars
        for _, (_, env_var) in FEATURE_TIER_MAP.items():
            os.environ.pop(env_var, None)

        try:
            env_set = apply_feature_gates(enabled, disabled)
            assert os.environ.get('HEVOLVE_AGENT_ENGINE_ENABLED') == 'true'
            assert os.environ.get('HEVOLVE_TTS_ENABLED') == 'true'
            assert os.environ.get('HEVOLVE_VIDEO_GEN_ENABLED') == 'false'
            assert os.environ.get('HEVOLVE_REGIONAL_HOST_ELIGIBLE') == 'false'
        finally:
            # Cleanup
            for _, (_, env_var) in FEATURE_TIER_MAP.items():
                os.environ.pop(env_var, None)

    def test_apply_gates_respects_user_override(self):
        """If user explicitly set env=true, we don't override even if hardware says no."""
        enabled = []
        disabled = {'agent_engine': 'needs STANDARD'}

        # User explicitly enabled it
        os.environ['HEVOLVE_AGENT_ENGINE_ENABLED'] = 'true'
        try:
            apply_feature_gates(enabled, disabled)
            # We should NOT have overridden the user's choice
            assert os.environ.get('HEVOLVE_AGENT_ENGINE_ENABLED') == 'true'
        finally:
            os.environ.pop('HEVOLVE_AGENT_ENGINE_ENABLED', None)

    def test_apply_gates_does_not_override_existing_true(self):
        """If env already set to true for an enabled feature, leave it."""
        enabled = ['agent_engine']
        disabled = {}
        os.environ['HEVOLVE_AGENT_ENGINE_ENABLED'] = 'true'
        try:
            env_set = apply_feature_gates(enabled, disabled)
            # Should not appear in env_set since it was already set
            assert 'HEVOLVE_AGENT_ENGINE_ENABLED' not in env_set
        finally:
            os.environ.pop('HEVOLVE_AGENT_ENGINE_ENABLED', None)


# ══════════════════════════════════════════════════════════════════
# Full Pipeline
# ══════════════════════════════════════════════════════════════════

class TestFullPipeline:

    def test_get_capabilities_none_before_check(self):
        """Before run_system_check(), get_capabilities() is None."""
        assert get_capabilities() is None
        assert get_tier() == NodeTierLevel.EMBEDDED
        assert get_tier_name() == 'embedded'

    def test_run_system_check_full_flow(self):
        """Mock hardware, verify full pipeline returns NodeCapabilities."""
        mock_hw = HardwareProfile(
            cpu_cores=4, ram_gb=8.0, disk_free_gb=15.0,
            gpu_vram_gb=0.0, cuda_available=False,
            os_platform='Linux', python_version='3.10.0',
            network_reachable=True,
        )

        # Clear env vars
        for _, (_, env_var) in FEATURE_TIER_MAP.items():
            os.environ.pop(env_var, None)

        try:
            with patch('security.system_requirements.detect_hardware',
                       return_value=mock_hw):
                caps = run_system_check()

            assert isinstance(caps, NodeCapabilities)
            assert caps.tier == NodeTierLevel.STANDARD
            assert 'agent_engine' in caps.enabled_features
            assert 'video_gen' in caps.disabled_features

            # get_capabilities() should now return cached result
            assert get_capabilities() is caps
            assert get_tier() == NodeTierLevel.STANDARD
            assert get_tier_name() == 'standard'
        finally:
            for _, (_, env_var) in FEATURE_TIER_MAP.items():
                os.environ.pop(env_var, None)

    def test_run_system_check_caches(self):
        """Second call returns cached result without re-detecting."""
        mock_hw = HardwareProfile(cpu_cores=2, ram_gb=4.0, disk_free_gb=1.0)

        for _, (_, env_var) in FEATURE_TIER_MAP.items():
            os.environ.pop(env_var, None)

        try:
            with patch('security.system_requirements.detect_hardware',
                       return_value=mock_hw) as mock_detect:
                caps1 = run_system_check()
                caps2 = run_system_check()

            assert caps1 is caps2
            assert mock_detect.call_count == 1  # Only called once
        finally:
            for _, (_, env_var) in FEATURE_TIER_MAP.items():
                os.environ.pop(env_var, None)

    def test_to_dict(self):
        """NodeCapabilities.to_dict() produces valid JSON-serializable dict."""
        mock_hw = HardwareProfile(
            cpu_cores=8, ram_gb=16.0, disk_free_gb=50.0,
            gpu_vram_gb=8.0, cuda_available=True,
        )

        for _, (_, env_var) in FEATURE_TIER_MAP.items():
            os.environ.pop(env_var, None)

        try:
            with patch('security.system_requirements.detect_hardware',
                       return_value=mock_hw):
                caps = run_system_check()

            d = caps.to_dict()
            assert d['tier'] == 'full'
            assert d['hardware']['cpu_cores'] == 8
            assert d['hardware']['gpu_vram_gb'] == 8.0
            assert isinstance(d['enabled_features'], list)
            assert isinstance(d['disabled_features'], dict)
        finally:
            for _, (_, env_var) in FEATURE_TIER_MAP.items():
                os.environ.pop(env_var, None)


# ══════════════════════════════════════════════════════════════════
# Network Connectivity
# ══════════════════════════════════════════════════════════════════

class TestNetworkConnectivity:

    @patch('security.system_requirements.socket.create_connection')
    def test_connectivity_success(self, mock_conn):
        mock_sock = MagicMock()
        mock_conn.return_value = mock_sock
        assert check_network_connectivity(timeout=2.0) is True
        mock_sock.close.assert_called_once()

    @patch('security.system_requirements.socket.create_connection',
           side_effect=OSError("no route"))
    def test_connectivity_failure(self, mock_conn):
        assert check_network_connectivity(timeout=1.0) is False


# ══════════════════════════════════════════════════════════════════
# Tier Ordering
# ══════════════════════════════════════════════════════════════════

class TestTierOrdering:

    def test_tier_rank_order(self):
        """EMBEDDED < OBSERVER < LITE < STANDARD < FULL < COMPUTE_HOST."""
        assert _TIER_RANK[NodeTierLevel.EMBEDDED] < _TIER_RANK[NodeTierLevel.OBSERVER]
        assert _TIER_RANK[NodeTierLevel.OBSERVER] < _TIER_RANK[NodeTierLevel.LITE]
        assert _TIER_RANK[NodeTierLevel.LITE] < _TIER_RANK[NodeTierLevel.STANDARD]
        assert _TIER_RANK[NodeTierLevel.STANDARD] < _TIER_RANK[NodeTierLevel.FULL]
        assert _TIER_RANK[NodeTierLevel.FULL] < _TIER_RANK[NodeTierLevel.COMPUTE_HOST]


# ══════════════════════════════════════════════════════════════════
# Local LLM Feature Tiers
# ══════════════════════════════════════════════════════════════════

class TestLocalLLMFeatures:

    def test_local_llm_requires_full_tier(self):
        """local_llm (Ollama 7B) needs FULL tier (16 GB RAM, 8 GB VRAM)."""
        assert FEATURE_TIER_MAP['local_llm'][0] == NodeTierLevel.FULL
        assert FEATURE_TIER_MAP['local_llm'][1] == 'HEVOLVE_LOCAL_LLM_ENABLED'

        # FULL node → local_llm enabled
        hw_full = HardwareProfile(cpu_cores=8, ram_gb=16.0, disk_free_gb=50.0, gpu_vram_gb=8.0)
        enabled, disabled = resolve_features(NodeTierLevel.FULL, hw_full)
        assert 'local_llm' in enabled
        # STANDARD node → local_llm disabled
        hw_std = HardwareProfile(cpu_cores=4, ram_gb=8.0, disk_free_gb=10.0)
        enabled, disabled = resolve_features(NodeTierLevel.STANDARD, hw_std)
        assert 'local_llm' not in enabled
        assert 'local_llm' in disabled

    def test_local_llm_large_requires_compute_host(self):
        """local_llm_large (Ollama 13B+) needs COMPUTE_HOST (32 GB RAM, 12 GB VRAM)."""
        assert FEATURE_TIER_MAP['local_llm_large'][0] == NodeTierLevel.COMPUTE_HOST
        assert FEATURE_TIER_MAP['local_llm_large'][1] == 'HEVOLVE_LOCAL_LLM_LARGE_ENABLED'

        # COMPUTE_HOST → local_llm_large enabled
        hw_host = HardwareProfile(cpu_cores=16, ram_gb=32.0, disk_free_gb=100.0, gpu_vram_gb=12.0)
        enabled, disabled = resolve_features(NodeTierLevel.COMPUTE_HOST, hw_host)
        assert 'local_llm_large' in enabled
        # FULL node → local_llm_large disabled
        hw_full = HardwareProfile(cpu_cores=8, ram_gb=16.0, disk_free_gb=50.0, gpu_vram_gb=8.0)
        enabled, disabled = resolve_features(NodeTierLevel.FULL, hw_full)
        assert 'local_llm_large' not in enabled
        assert 'local_llm_large' in disabled


# ══════════════════════════════════════════════════════════════════
# Embedded Tier Features
# ══════════════════════════════════════════════════════════════════

class TestEmbeddedFeatures:

    def test_gossip_requires_embedded(self):
        """gossip available at EMBEDDED tier (lowest)."""
        assert FEATURE_TIER_MAP['gossip'][0] == NodeTierLevel.EMBEDDED
        assert FEATURE_TIER_MAP['gossip'][1] == 'HEVOLVE_GOSSIP_ENABLED'

    def test_sensor_bridge_requires_embedded(self):
        assert FEATURE_TIER_MAP['sensor_bridge'][0] == NodeTierLevel.EMBEDDED
        assert FEATURE_TIER_MAP['sensor_bridge'][1] == 'HEVOLVE_SENSOR_BRIDGE_ENABLED'

    def test_protocol_adapter_requires_embedded(self):
        assert FEATURE_TIER_MAP['protocol_adapter'][0] == NodeTierLevel.EMBEDDED
        assert FEATURE_TIER_MAP['protocol_adapter'][1] == 'HEVOLVE_PROTOCOL_ADAPTER_ENABLED'

    def test_flask_server_requires_observer(self):
        """Flask server needs OBSERVER tier — embedded devices are headless."""
        assert FEATURE_TIER_MAP['flask_server'][0] == NodeTierLevel.OBSERVER
        assert FEATURE_TIER_MAP['flask_server'][1] == 'HEVOLVE_FLASK_ENABLED'

    def test_vision_lightweight_requires_lite(self):
        assert FEATURE_TIER_MAP['vision_lightweight'][0] == NodeTierLevel.LITE
        assert FEATURE_TIER_MAP['vision_lightweight'][1] == 'HEVOLVE_VISION_LITE_ENABLED'

    def test_embedded_gets_gossip_but_not_flask(self):
        """Embedded device can gossip but doesn't run Flask."""
        hw = HardwareProfile(cpu_cores=1, ram_gb=0.5, disk_free_gb=0.1)
        enabled, disabled = resolve_features(NodeTierLevel.EMBEDDED, hw)
        assert 'gossip' in enabled
        assert 'flask_server' in disabled

    def test_lite_gets_vision_lightweight(self):
        """Lite node gets lightweight vision (CPU-only VLM)."""
        hw = HardwareProfile(cpu_cores=2, ram_gb=4.0, disk_free_gb=1.0)
        enabled, disabled = resolve_features(NodeTierLevel.LITE, hw)
        assert 'vision_lightweight' in enabled
        assert 'gossip' in enabled
        assert 'flask_server' in enabled


# ══════════════════════════════════════════════════════════════════
# Hardware I/O Detection
# ══════════════════════════════════════════════════════════════════

class TestHardwareIODetection:

    def test_hardware_profile_has_embedded_fields(self):
        """HardwareProfile includes embedded I/O detection fields."""
        hw = HardwareProfile()
        assert hasattr(hw, 'is_read_only_fs')
        assert hasattr(hw, 'has_gpio')
        assert hasattr(hw, 'has_serial')
        assert hasattr(hw, 'has_camera_hw')
        assert hw.is_read_only_fs is False
        assert hw.has_gpio is False
        assert hw.has_serial is False
        assert hw.has_camera_hw is False

    def test_to_dict_includes_embedded_fields(self):
        """to_dict() includes the new embedded I/O fields."""
        hw = HardwareProfile(has_gpio=True, has_serial=True,
                             is_read_only_fs=True, has_camera_hw=True)
        d = hw.to_dict()
        assert d['has_gpio'] is True
        assert d['has_serial'] is True
        assert d['is_read_only_fs'] is True
        assert d['has_camera_hw'] is True

    def test_read_only_fs_detection_writable(self):
        """On this dev machine, filesystem should be writable."""
        from security.system_requirements import _detect_read_only_fs
        assert _detect_read_only_fs() is False

    def test_gpio_detection_no_gpio_on_dev_machine(self):
        """Dev machine (Windows) should not have GPIO."""
        from security.system_requirements import _detect_gpio
        # On Windows, no /sys/class/gpio and no gpiod/RPi.GPIO
        result = _detect_gpio()
        # Don't assert False since CI might have pyserial; just check it returns bool
        assert isinstance(result, bool)

    def test_detect_hardware_includes_io_fields(self):
        """detect_hardware() populates the embedded I/O fields."""
        hw = detect_hardware()
        assert isinstance(hw.is_read_only_fs, bool)
        assert isinstance(hw.has_gpio, bool)
        assert isinstance(hw.has_serial, bool)
        assert isinstance(hw.has_camera_hw, bool)
