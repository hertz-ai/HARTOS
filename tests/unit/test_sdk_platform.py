"""
Tests for WS4: Architecture Expansion — platform detection, port registry,
capability detection, and graceful fallbacks.

Covers:
  - detect_platform() returns correct values for known architectures
  - port_registry has model_bus entry in all three dicts
  - capability detection works with and without the service registry
  - graceful fallbacks when platform is not bootstrapped
"""

import os
import platform
import sys
import unittest
from unittest.mock import patch

from core.port_registry import (
    APP_PORTS, OS_PORTS, ENV_OVERRIDES,
    get_port, get_all_ports, is_os_mode, check_port_available,
)


# ── Minimal detect_platform helper (tested here, usable by SDK) ──

def detect_platform() -> dict:
    """Detect the current platform/architecture.

    Returns a dict with:
        os: 'linux', 'windows', 'darwin', etc.
        arch: 'x86_64', 'aarch64', 'riscv64', etc.
        is_os_mode: True if running as HART OS
        python: Python version string
    """
    machine = platform.machine().lower()
    # Normalize common aliases
    arch_map = {
        'amd64': 'x86_64',
        'x86_64': 'x86_64',
        'aarch64': 'aarch64',
        'arm64': 'aarch64',
        'riscv64': 'riscv64',
    }
    arch = arch_map.get(machine, machine)

    return {
        'os': sys.platform,
        'arch': arch,
        'is_os_mode': is_os_mode(),
        'python': platform.python_version(),
    }


def get_capabilities() -> dict:
    """Detect platform capabilities (GPU, services, etc.).

    Gracefully handles missing registry — returns defaults when
    platform is not bootstrapped.
    """
    caps = {
        'gpu_available': False,
        'services_registered': 0,
        'event_bus': False,
        'apps_registered': 0,
    }

    # GPU detection via environment (RISC-V boards force CPU-only)
    if os.environ.get('HART_FORCE_CPU', '').lower() in ('true', '1'):
        caps['gpu_available'] = False
    else:
        try:
            import torch
            caps['gpu_available'] = torch.cuda.is_available()
        except ImportError:
            caps['gpu_available'] = False

    # Service registry — may not be bootstrapped
    try:
        from core.platform.registry import get_registry
        reg = get_registry()
        caps['services_registered'] = len(reg.names())
        caps['event_bus'] = reg.has('events')
        if reg.has('apps'):
            apps = reg.get('apps')
            caps['apps_registered'] = apps.count()
    except Exception:
        pass

    return caps


# ═════════════════════════════════════════════════════════════════
# Tests
# ═════════════════════════════════════════════════════════════════


class TestDetectPlatform(unittest.TestCase):
    """detect_platform() returns correct values."""

    def test_returns_dict_with_required_keys(self):
        result = detect_platform()
        self.assertIn('os', result)
        self.assertIn('arch', result)
        self.assertIn('is_os_mode', result)
        self.assertIn('python', result)

    def test_os_matches_sys_platform(self):
        result = detect_platform()
        self.assertEqual(result['os'], sys.platform)

    def test_python_version_is_string(self):
        result = detect_platform()
        self.assertIsInstance(result['python'], str)
        # Should contain at least major.minor
        parts = result['python'].split('.')
        self.assertGreaterEqual(len(parts), 2)

    @patch('platform.machine', return_value='x86_64')
    def test_x86_64_arch(self, _mock):
        result = detect_platform()
        self.assertEqual(result['arch'], 'x86_64')

    @patch('platform.machine', return_value='aarch64')
    def test_aarch64_arch(self, _mock):
        result = detect_platform()
        self.assertEqual(result['arch'], 'aarch64')

    @patch('platform.machine', return_value='arm64')
    def test_arm64_normalized_to_aarch64(self, _mock):
        result = detect_platform()
        self.assertEqual(result['arch'], 'aarch64')

    @patch('platform.machine', return_value='riscv64')
    def test_riscv64_arch(self, _mock):
        result = detect_platform()
        self.assertEqual(result['arch'], 'riscv64')

    @patch('platform.machine', return_value='amd64')
    def test_amd64_normalized_to_x86_64(self, _mock):
        result = detect_platform()
        self.assertEqual(result['arch'], 'x86_64')


class TestPortRegistryModelBus(unittest.TestCase):
    """port_registry has model_bus entry in all three dicts."""

    def test_app_ports_has_model_bus(self):
        self.assertIn('model_bus', APP_PORTS)
        self.assertEqual(APP_PORTS['model_bus'], 6790)

    def test_os_ports_has_model_bus(self):
        self.assertIn('model_bus', OS_PORTS)

    def test_env_overrides_has_model_bus(self):
        self.assertIn('model_bus', ENV_OVERRIDES)
        self.assertEqual(ENV_OVERRIDES['model_bus'], 'HART_MODEL_BUS_PORT')

    def test_get_port_model_bus_app_mode(self):
        """get_port('model_bus') returns 6790 in app mode."""
        import core.port_registry as pr
        old_cached = pr._os_mode_cached
        try:
            pr._os_mode_cached = False
            port = get_port('model_bus')
            self.assertEqual(port, 6790)
        finally:
            pr._os_mode_cached = old_cached

    @patch.dict(os.environ, {'HART_MODEL_BUS_PORT': '9999'})
    def test_get_port_model_bus_env_override(self):
        """Environment variable overrides model_bus port."""
        port = get_port('model_bus')
        self.assertEqual(port, 9999)

    def test_get_all_ports_includes_model_bus(self):
        """get_all_ports() includes model_bus."""
        ports = get_all_ports()
        self.assertIn('model_bus', ports)


class TestCapabilityDetection(unittest.TestCase):
    """get_capabilities() works with and without registry."""

    def test_returns_dict_with_required_keys(self):
        caps = get_capabilities()
        self.assertIn('gpu_available', caps)
        self.assertIn('services_registered', caps)
        self.assertIn('event_bus', caps)
        self.assertIn('apps_registered', caps)

    @patch.dict(os.environ, {'HART_FORCE_CPU': 'true'})
    def test_force_cpu_disables_gpu(self):
        caps = get_capabilities()
        self.assertFalse(caps['gpu_available'])

    def test_capabilities_without_registry(self):
        """Capabilities degrade gracefully if registry is empty."""
        caps = get_capabilities()
        # Should not raise — returns defaults
        self.assertIsInstance(caps['services_registered'], int)
        self.assertIsInstance(caps['gpu_available'], bool)

    def test_capabilities_with_bootstrapped_registry(self):
        """After bootstrap, capabilities reflect registered services."""
        from core.platform.registry import reset_registry
        from core.platform.bootstrap import bootstrap_platform

        old_registry_cache = None
        try:
            reset_registry()
            bootstrap_platform()
            caps = get_capabilities()
            self.assertTrue(caps['event_bus'])
            self.assertGreater(caps['services_registered'], 0)
        finally:
            reset_registry()


if __name__ == '__main__':
    unittest.main()
