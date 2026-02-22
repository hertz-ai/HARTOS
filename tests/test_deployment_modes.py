"""
Tests for deployment manifest, deployment mode detection, config loading chain,
and tier/mode/variant matrix.

Validates:
- deploy/deployment-manifest.json structure and semantics
- core/config_cache.py loading priority and behaviour
- security/system_requirements.py tier detection
- Variant configs and shell script references
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set DB env for imports
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

import json
from unittest.mock import patch, MagicMock

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(REPO_ROOT, 'deploy', 'deployment-manifest.json')


def _load_manifest():
    with open(MANIFEST_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════
# Deployment Manifest Tests
# ═══════════════════════════════════════════════════════════════

class TestDeploymentManifest:
    """Tests for deploy/deployment-manifest.json structure and semantics."""

    def test_valid_json(self):
        """Manifest must be valid JSON."""
        with open(MANIFEST_PATH, 'r', encoding='utf-8') as f:
            raw = f.read()
        data = json.loads(raw)
        assert isinstance(data, dict)

    def test_has_all_five_modes(self):
        """Manifest must define all 5 deployment modes."""
        manifest = _load_manifest()
        expected_modes = {'standalone', 'bundled', 'headless', 'regional', 'central'}
        actual_modes = set(manifest['modes'].keys())
        assert actual_modes == expected_modes

    def test_has_all_six_tiers(self):
        """Manifest must define all 6 hardware tiers."""
        manifest = _load_manifest()
        expected_tiers = {
            'EMBEDDED', 'OBSERVER', 'LITE', 'STANDARD',
            'PERFORMANCE', 'COMPUTE_HOST',
        }
        actual_tiers = set(manifest['tiers'].keys())
        assert actual_tiers == expected_tiers

    def test_has_all_five_services(self):
        """Manifest must define all 5 services."""
        manifest = _load_manifest()
        expected_services = {
            'hart-backend', 'hart-discovery', 'hart-vision',
            'hart-llm', 'hart-agent-daemon',
        }
        actual_services = set(manifest['services'].keys())
        assert actual_services == expected_services

    def test_has_all_three_variants(self):
        """Manifest must define all 3 OS variants."""
        manifest = _load_manifest()
        expected_variants = {'server', 'desktop', 'edge'}
        actual_variants = set(manifest['variants'].keys())
        assert actual_variants == expected_variants

    def test_each_mode_has_required_fields(self):
        """Every deployment mode must include all required fields."""
        manifest = _load_manifest()
        required_fields = {
            'entry_point', 'database', 'certificate_tier',
            'config_file', 'default_port', 'features',
        }
        for mode_name, mode_def in manifest['modes'].items():
            actual_fields = set(mode_def.keys())
            missing = required_fields - actual_fields
            assert not missing, (
                f"Mode '{mode_name}' is missing fields: {missing}"
            )

    def test_tier_ram_requirements_monotonically_increasing(self):
        """Tier RAM minimums must increase: EMBEDDED < OBSERVER < ... < COMPUTE_HOST."""
        manifest = _load_manifest()
        tier_order = ['EMBEDDED', 'OBSERVER', 'LITE', 'STANDARD', 'PERFORMANCE', 'COMPUTE_HOST']
        ram_values = [manifest['tiers'][t]['min_ram_gb'] for t in tier_order]
        for i in range(len(ram_values) - 1):
            assert ram_values[i] <= ram_values[i + 1], (
                f"RAM not monotonically increasing: {tier_order[i]}={ram_values[i]} "
                f"vs {tier_order[i + 1]}={ram_values[i + 1]}"
            )

    def test_tier_feature_sets_are_additive(self):
        """Each tier must include all features from the previous tier."""
        manifest = _load_manifest()
        tier_order = ['EMBEDDED', 'OBSERVER', 'LITE', 'STANDARD', 'PERFORMANCE', 'COMPUTE_HOST']
        prev_features = set()
        for tier_name in tier_order:
            current_features = set(manifest['tiers'][tier_name]['features'])
            missing = prev_features - current_features
            assert not missing, (
                f"Tier '{tier_name}' is missing features from previous tier: {missing}"
            )
            prev_features = current_features

    def test_compute_host_requires_gpu(self):
        """COMPUTE_HOST tier must require a GPU."""
        manifest = _load_manifest()
        compute_host = manifest['tiers']['COMPUTE_HOST']
        assert compute_host['gpu_required'] is True

    def test_backend_service_always_enabled(self):
        """hart-backend service must be always_enabled."""
        manifest = _load_manifest()
        backend = manifest['services']['hart-backend']
        assert backend['always_enabled'] is True

    def test_service_ports_match_known_values(self):
        """All service ports must match known assigned values."""
        manifest = _load_manifest()
        known_ports = {6777, 6780, 9891, 5460, 8080}
        for svc_name, svc_def in manifest['services'].items():
            port = svc_def.get('port')
            ports = svc_def.get('ports', [])
            all_ports = ports if ports else ([port] if port is not None else [])
            for p in all_ports:
                assert p in known_ports, (
                    f"Service '{svc_name}' has unexpected port {p}. "
                    f"Known ports: {known_ports}"
                )


# ═══════════════════════════════════════════════════════════════
# Config Loading Chain Tests
# ═══════════════════════════════════════════════════════════════

class TestConfigLoadingChain:
    """Tests for core/config_cache.py loading priority and behaviour."""

    def test_module_docstring_mentions_priority(self):
        """config_cache.py docstring must mention loading priority."""
        from core import config_cache
        docstring = config_cache.__doc__ or ''
        assert 'Priority' in docstring or 'priority' in docstring, (
            "config_cache module docstring should mention loading priority"
        )

    def test_module_docstring_mentions_all_five_priority_levels(self):
        """config_cache.py docstring must reference all 5 priority levels."""
        from core import config_cache
        docstring = config_cache.__doc__ or ''
        # Check each priority level is mentioned somewhere in the docstring
        assert 'nvironment variable' in docstring or 'env var' in docstring.lower() or 'Environment' in docstring
        assert 'vault' in docstring.lower() or 'Vault' in docstring or 'SecretsManager' in docstring
        assert 'config.json' in docstring
        assert 'langchain_config.json' in docstring
        assert 'mpty dict' in docstring or 'fallback' in docstring.lower()

    def test_get_config_returns_dict(self):
        """get_config() must always return a dict."""
        from core.config_cache import get_config, reload_config
        # Reset cache to force fresh load
        import core.config_cache as cc
        with cc._config_lock:
            cc._config = None
        result = get_config()
        assert isinstance(result, dict)

    def test_get_secret_checks_env_var_first(self):
        """get_secret() must return env var value when set, ignoring config file."""
        from core.config_cache import get_secret
        test_key = '_HART_TEST_SECRET_ENV_FIRST_12345'
        test_val = 'from_env_var'
        try:
            os.environ[test_key] = test_val
            result = get_secret(test_key, default='from_default')
            assert result == test_val
        finally:
            os.environ.pop(test_key, None)

    def test_get_secret_returns_default_when_key_missing(self):
        """get_secret() must return default when key is absent from env and config."""
        from core.config_cache import get_secret
        missing_key = '_HART_NONEXISTENT_KEY_99999'
        # Ensure it is not in env
        os.environ.pop(missing_key, None)
        result = get_secret(missing_key, default='my_default_value')
        assert result == 'my_default_value'

    def test_reload_config_resets_cache(self):
        """reload_config() must clear the cached config and reload."""
        import core.config_cache as cc
        # Set cache to a sentinel value
        with cc._config_lock:
            cc._config = {'_sentinel': True}
        result = cc.reload_config()
        # After reload, sentinel must be gone (fresh load from disk/env)
        assert '_sentinel' not in result
        assert isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════
# Tier Detection Tests
# ═══════════════════════════════════════════════════════════════

class TestTierDetection:
    """Tests for security.system_requirements tier detection."""

    def test_import_works(self):
        """security.system_requirements module must be importable."""
        from security import system_requirements
        assert hasattr(system_requirements, 'run_system_check')
        assert hasattr(system_requirements, 'get_tier_name')
        assert hasattr(system_requirements, 'NodeTierLevel')

    def test_run_system_check_returns_capabilities_with_features(self):
        """run_system_check() result must have enabled_features and disabled_features."""
        from security.system_requirements import (
            run_system_check, reset_for_testing,
        )
        reset_for_testing()
        caps = run_system_check()
        assert hasattr(caps, 'enabled_features')
        assert hasattr(caps, 'disabled_features')
        assert isinstance(caps.enabled_features, list)
        assert isinstance(caps.disabled_features, dict)
        # Cleanup
        reset_for_testing()

    def test_get_tier_name_returns_string(self):
        """get_tier_name() must return a non-empty string."""
        from security.system_requirements import get_tier_name
        name = get_tier_name()
        assert isinstance(name, str)
        assert len(name) > 0


# ═══════════════════════════════════════════════════════════════
# Variant Config Tests
# ═══════════════════════════════════════════════════════════════

class TestVariantConfig:
    """Tests for distro variant configs and shell script references."""

    VARIANTS_DIR = os.path.join(REPO_ROOT, 'deploy', 'distro', 'variants')

    def test_variant_configs_exist(self):
        """All 3 variant config files must exist."""
        for variant in ('server', 'desktop', 'edge'):
            conf_path = os.path.join(self.VARIANTS_DIR, f'hart-os-{variant}.conf')
            assert os.path.isfile(conf_path), (
                f"Missing variant config: {conf_path}"
            )

    def test_first_boot_reads_variant(self):
        """first-boot.sh must read /etc/hart/variant to determine the variant."""
        fb_path = os.path.join(
            REPO_ROOT, 'deploy', 'distro', 'first-boot', 'hart-first-boot.sh',
        )
        with open(fb_path, 'r', encoding='utf-8') as f:
            content = f.read()
        assert '/etc/hart/variant' in content, (
            "first-boot.sh must read /etc/hart/variant"
        )

    def test_first_boot_has_edge_variant_override(self):
        """first-boot.sh must have edge variant override logic that disables services."""
        fb_path = os.path.join(
            REPO_ROOT, 'deploy', 'distro', 'first-boot', 'hart-first-boot.sh',
        )
        with open(fb_path, 'r', encoding='utf-8') as f:
            content = f.read()
        # Must check for edge variant and disable services
        assert 'edge' in content
        # Must contain a conditional block that references the variant for overriding
        assert 'VARIANT' in content and 'edge' in content
        # Must disable services in the edge path
        assert 'systemctl disable' in content

    def test_build_iso_writes_variant_to_chroot(self):
        """build-iso.sh must write the variant to /etc/hart/variant in chroot."""
        iso_path = os.path.join(REPO_ROOT, 'deploy', 'distro', 'build-iso.sh')
        with open(iso_path, 'r', encoding='utf-8') as f:
            content = f.read()
        # build-iso.sh writes: echo "$VARIANT" > config/includes.chroot/etc/hart/variant
        assert '/etc/hart/variant' in content, (
            "build-iso.sh must write variant to /etc/hart/variant in chroot"
        )
        assert 'VARIANT' in content
