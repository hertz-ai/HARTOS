"""
Tests for Embedded Tier Support (Phase 1 - system_requirements + code hash caching).

Tests: EMBEDDED tier classification, feature gating, force-tier override,
hardware I/O detection fields, embedded feature map entries.
"""
import os
from unittest.mock import patch, MagicMock

import pytest

from security.system_requirements import (
    NodeTierLevel, HardwareProfile, NodeCapabilities,
    classify_tier, resolve_features, apply_feature_gates,
    TIER_REQUIREMENTS, FEATURE_TIER_MAP, _TIER_RANK, _TIER_ORDER,
    get_tier, get_tier_name,
)


# ─── EMBEDDED Tier Exists ───

class TestEmbeddedTierEnum:
    """Verify EMBEDDED tier is present and correctly positioned."""

    def test_embedded_exists(self):
        assert hasattr(NodeTierLevel, 'EMBEDDED')
        assert NodeTierLevel.EMBEDDED.value == 'embedded'

    def test_embedded_is_lowest_rank(self):
        assert _TIER_RANK[NodeTierLevel.EMBEDDED] == 0

    def test_tier_order_has_embedded_last(self):
        assert _TIER_ORDER[-1] == NodeTierLevel.EMBEDDED

    def test_six_tiers_total(self):
        assert len(NodeTierLevel) == 6


# ─── Classification ───

class TestEmbeddedClassification:
    """Verify sub-OBSERVER hardware classifies as EMBEDDED."""

    def test_zero_resources(self):
        hw = HardwareProfile(cpu_cores=0, ram_gb=0, disk_free_gb=0)
        assert classify_tier(hw) == NodeTierLevel.EMBEDDED

    def test_pi_zero_512mb(self):
        hw = HardwareProfile(cpu_cores=1, ram_gb=0.5, disk_free_gb=2.0)
        assert classify_tier(hw) == NodeTierLevel.EMBEDDED

    def test_esp32_proxy(self):
        """Microcontroller proxy (protocol bridge only)."""
        hw = HardwareProfile(cpu_cores=1, ram_gb=0.25, disk_free_gb=0.1)
        assert classify_tier(hw) == NodeTierLevel.EMBEDDED

    def test_observer_boundary(self):
        """1 core + 2GB RAM → OBSERVER, not EMBEDDED."""
        hw = HardwareProfile(cpu_cores=1, ram_gb=2.0, disk_free_gb=0.5)
        assert classify_tier(hw) == NodeTierLevel.OBSERVER

    def test_force_tier_embedded(self):
        hw = HardwareProfile(cpu_cores=16, ram_gb=64.0, disk_free_gb=500.0)
        with patch.dict(os.environ, {'HEVOLVE_FORCE_TIER': 'embedded'}):
            assert classify_tier(hw) == NodeTierLevel.EMBEDDED

    def test_force_tier_is_case_insensitive(self):
        hw = HardwareProfile(cpu_cores=1, ram_gb=0.5, disk_free_gb=0.1)
        with patch.dict(os.environ, {'HEVOLVE_FORCE_TIER': 'EMBEDDED'}):
            # The code does .lower(), so this should match
            assert classify_tier(hw) == NodeTierLevel.EMBEDDED


# ─── Feature Gating ───

class TestEmbeddedFeatureGating:
    """Verify embedded-tier features are enabled and higher-tier features disabled."""

    def _resolve_embedded(self):
        hw = HardwareProfile(cpu_cores=1, ram_gb=0.5, disk_free_gb=0.1)
        tier = NodeTierLevel.EMBEDDED
        return resolve_features(tier, hw)

    def test_gossip_enabled(self):
        enabled, _ = self._resolve_embedded()
        assert 'gossip' in enabled

    def test_sensor_bridge_enabled(self):
        enabled, _ = self._resolve_embedded()
        assert 'sensor_bridge' in enabled

    def test_protocol_adapter_enabled(self):
        enabled, _ = self._resolve_embedded()
        assert 'protocol_adapter' in enabled

    def test_flask_disabled(self):
        _, disabled = self._resolve_embedded()
        assert 'flask_server' in disabled

    def test_agent_engine_disabled(self):
        _, disabled = self._resolve_embedded()
        assert 'agent_engine' in disabled

    def test_vision_lightweight_disabled(self):
        _, disabled = self._resolve_embedded()
        assert 'vision_lightweight' in disabled

    def test_coding_agent_disabled(self):
        _, disabled = self._resolve_embedded()
        assert 'coding_agent' in disabled


# ─── Feature Map Entries ───

class TestFeatureMapEntries:
    """Verify embedded-specific features are in FEATURE_TIER_MAP."""

    def test_gossip_in_map(self):
        assert 'gossip' in FEATURE_TIER_MAP
        assert FEATURE_TIER_MAP['gossip'][0] == NodeTierLevel.EMBEDDED

    def test_sensor_bridge_in_map(self):
        assert 'sensor_bridge' in FEATURE_TIER_MAP
        assert FEATURE_TIER_MAP['sensor_bridge'][0] == NodeTierLevel.EMBEDDED

    def test_protocol_adapter_in_map(self):
        assert 'protocol_adapter' in FEATURE_TIER_MAP
        assert FEATURE_TIER_MAP['protocol_adapter'][0] == NodeTierLevel.EMBEDDED

    def test_vision_lightweight_at_lite(self):
        assert 'vision_lightweight' in FEATURE_TIER_MAP
        assert FEATURE_TIER_MAP['vision_lightweight'][0] == NodeTierLevel.LITE


# ─── Hardware Profile Fields ───

class TestHardwareProfileEmbeddedFields:
    """Verify embedded I/O detection fields exist on HardwareProfile."""

    def test_is_read_only_fs(self):
        hp = HardwareProfile()
        assert hasattr(hp, 'is_read_only_fs')
        assert hp.is_read_only_fs is False

    def test_has_gpio(self):
        hp = HardwareProfile()
        assert hasattr(hp, 'has_gpio')
        assert hp.has_gpio is False

    def test_has_serial(self):
        hp = HardwareProfile()
        assert hasattr(hp, 'has_serial')
        assert hp.has_serial is False

    def test_has_camera_hw(self):
        hp = HardwareProfile()
        assert hasattr(hp, 'has_camera_hw')
        assert hp.has_camera_hw is False

    def test_to_dict_includes_embedded_fields(self):
        hp = HardwareProfile(has_gpio=True, has_serial=True)
        d = hp.to_dict()
        assert d['has_gpio'] is True
        assert d['has_serial'] is True
        assert d['is_read_only_fs'] is False
        assert d['has_camera_hw'] is False


# ─── EMBEDDED Has No Requirements ───

class TestEmbeddedNoRequirements:
    """EMBEDDED should not appear in TIER_REQUIREMENTS - it's the floor."""

    def test_not_in_requirements(self):
        for req in TIER_REQUIREMENTS:
            assert req.tier != NodeTierLevel.EMBEDDED, \
                "EMBEDDED should not appear in TIER_REQUIREMENTS"

    def test_observer_is_lowest_in_requirements(self):
        tiers = [req.tier for req in TIER_REQUIREMENTS]
        assert NodeTierLevel.OBSERVER in tiers


# ─── Tier Helpers ───

class TestTierHelpers:
    """Verify get_tier() / get_tier_name() default to EMBEDDED."""

    def test_get_tier_default_embedded(self):
        """Before run_system_check(), get_tier() returns EMBEDDED."""
        import security.system_requirements as sr
        original = sr._capabilities
        try:
            sr._capabilities = None
            assert sr.get_tier() == NodeTierLevel.EMBEDDED
        finally:
            sr._capabilities = original

    def test_get_tier_name_default(self):
        import security.system_requirements as sr
        original = sr._capabilities
        try:
            sr._capabilities = None
            assert sr.get_tier_name() == 'embedded'
        finally:
            sr._capabilities = original


# ─── Apply Feature Gates ───

class TestApplyFeatureGatesEmbedded:
    """Verify env vars are set for embedded features."""

    def test_gossip_enabled_env(self):
        enabled = ['gossip', 'sensor_bridge', 'protocol_adapter']
        disabled = {'flask_server': 'Requires observer', 'agent_engine': 'Requires standard'}
        with patch.dict(os.environ, {}, clear=True):
            env_set = apply_feature_gates(enabled, disabled)
            assert os.environ.get('HEVOLVE_GOSSIP_ENABLED') == 'true'
            assert os.environ.get('HEVOLVE_SENSOR_BRIDGE_ENABLED') == 'true'

    def test_flask_disabled_env(self):
        enabled = ['gossip']
        disabled = {'flask_server': 'Requires observer'}
        with patch.dict(os.environ, {}, clear=True):
            apply_feature_gates(enabled, disabled)
            assert os.environ.get('HEVOLVE_FLASK_ENABLED') == 'false'

    def test_user_override_respected(self):
        """If user explicitly enables a feature, we don't override."""
        enabled = ['gossip']
        disabled = {'agent_engine': 'Requires standard'}
        with patch.dict(os.environ, {'HEVOLVE_AGENT_ENGINE_ENABLED': 'true'}, clear=True):
            apply_feature_gates(enabled, disabled)
            # User's explicit 'true' is kept, even though tier says disable
            assert os.environ.get('HEVOLVE_AGENT_ENGINE_ENABLED') == 'true'
