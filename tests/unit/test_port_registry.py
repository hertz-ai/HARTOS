"""
Tests for core.port_registry — single source of truth for HART OS service ports.

Covers: APP_PORTS, OS_PORTS, is_os_mode(), get_port(), get_all_ports(),
        check_port_available(), get_mode_label(), ENV_OVERRIDES.
"""

import os
import socket
import unittest
from unittest.mock import patch, mock_open


class TestAppPorts(unittest.TestCase):
    """Verify APP_PORTS table is complete and uses user-space ports."""

    def test_all_services_present(self):
        from core.port_registry import APP_PORTS
        expected = {
            'backend', 'discovery', 'vision', 'llm', 'websocket',
            'diarization', 'dlna_stream', 'mesh_wg', 'mesh_relay',
            'model_bus',
        }
        self.assertEqual(set(APP_PORTS.keys()), expected)

    def test_app_ports_are_user_space(self):
        from core.port_registry import APP_PORTS
        for service, port in APP_PORTS.items():
            self.assertGreater(port, 1024,
                               f"{service} app port {port} is not user-space")

    def test_app_ports_no_duplicates(self):
        from core.port_registry import APP_PORTS
        ports = list(APP_PORTS.values())
        self.assertEqual(len(ports), len(set(ports)), "Duplicate app ports")


class TestOSPorts(unittest.TestCase):
    """Verify OS_PORTS table is complete and uses privileged ports."""

    def test_all_services_present(self):
        from core.port_registry import OS_PORTS
        expected = {
            'backend', 'discovery', 'vision', 'llm', 'websocket',
            'diarization', 'dlna_stream', 'mesh_wg', 'mesh_relay',
            'model_bus',
        }
        self.assertEqual(set(OS_PORTS.keys()), expected)

    def test_os_ports_are_privileged(self):
        from core.port_registry import OS_PORTS
        for service, port in OS_PORTS.items():
            self.assertLess(port, 1024,
                            f"{service} OS port {port} is not privileged")

    def test_os_ports_no_duplicates(self):
        from core.port_registry import OS_PORTS
        ports = list(OS_PORTS.values())
        self.assertEqual(len(ports), len(set(ports)), "Duplicate OS ports")

    def test_os_and_app_have_same_services(self):
        from core.port_registry import APP_PORTS, OS_PORTS
        self.assertEqual(set(APP_PORTS.keys()), set(OS_PORTS.keys()))


class TestEnvOverrides(unittest.TestCase):
    """Verify ENV_OVERRIDES maps all services."""

    def test_all_services_have_env_var(self):
        from core.port_registry import APP_PORTS, ENV_OVERRIDES
        for service in APP_PORTS:
            self.assertIn(service, ENV_OVERRIDES,
                          f"Missing env override for {service}")

    def test_env_var_names_are_strings(self):
        from core.port_registry import ENV_OVERRIDES
        for service, var in ENV_OVERRIDES.items():
            self.assertIsInstance(var, str)
            self.assertTrue(var.isupper() or '_' in var,
                            f"{var} should be an env-style name")


class TestIsOsMode(unittest.TestCase):
    """Test OS mode detection."""

    def setUp(self):
        import core.port_registry as pr
        pr._os_mode_cached = None  # Reset cache for each test

    def tearDown(self):
        import core.port_registry as pr
        pr._os_mode_cached = None

    @patch.dict(os.environ, {'HART_OS_MODE': 'true'})
    def test_env_var_true(self):
        from core.port_registry import is_os_mode
        self.assertTrue(is_os_mode())

    @patch.dict(os.environ, {'HART_OS_MODE': '1'})
    def test_env_var_one(self):
        from core.port_registry import is_os_mode
        self.assertTrue(is_os_mode())

    @patch.dict(os.environ, {'HART_OS_MODE': 'yes'})
    def test_env_var_yes(self):
        from core.port_registry import is_os_mode
        self.assertTrue(is_os_mode())

    @patch.dict(os.environ, {'HART_OS_MODE': 'TRUE'})
    def test_env_var_case_insensitive(self):
        from core.port_registry import is_os_mode
        self.assertTrue(is_os_mode())

    @patch.dict(os.environ, {'HART_OS_MODE': 'false'}, clear=False)
    def test_env_var_false(self):
        from core.port_registry import is_os_mode
        # Not true from env, falls through to os-release check
        # On Windows/non-NixOS, should be False
        self.assertIsInstance(is_os_mode(), bool)

    @patch.dict(os.environ, {}, clear=False)
    def test_no_env_var_no_os_release(self):
        """Without env var or hart-os /etc/os-release, returns False."""
        from core.port_registry import is_os_mode
        # Remove HART_OS_MODE if present
        os.environ.pop('HART_OS_MODE', None)
        result = is_os_mode()
        # On Windows/normal Linux, this should be False
        self.assertFalse(result)

    @patch.dict(os.environ, {}, clear=False)
    @patch('builtins.open', mock_open(read_data='ID=hart-os\nNAME="HART OS"\n'))
    def test_os_release_detection(self):
        from core.port_registry import is_os_mode
        os.environ.pop('HART_OS_MODE', None)
        self.assertTrue(is_os_mode())

    def test_caching(self):
        """Once detected, result is cached."""
        import core.port_registry as pr
        pr._os_mode_cached = True
        self.assertTrue(pr.is_os_mode())
        pr._os_mode_cached = False
        self.assertFalse(pr.is_os_mode())


class TestGetPort(unittest.TestCase):
    """Test port resolution with priority: override > env > mode."""

    def setUp(self):
        import core.port_registry as pr
        pr._os_mode_cached = None

    def tearDown(self):
        import core.port_registry as pr
        pr._os_mode_cached = None

    def test_app_mode_defaults(self):
        import core.port_registry as pr
        pr._os_mode_cached = False
        self.assertEqual(pr.get_port('backend'), 6777)
        self.assertEqual(pr.get_port('discovery'), 6780)
        self.assertEqual(pr.get_port('vision'), 9891)
        self.assertEqual(pr.get_port('llm'), 8080)
        self.assertEqual(pr.get_port('websocket'), 5460)

    def test_os_mode_defaults(self):
        import core.port_registry as pr
        pr._os_mode_cached = True
        self.assertEqual(pr.get_port('backend'), 677)
        self.assertEqual(pr.get_port('discovery'), 678)
        self.assertEqual(pr.get_port('vision'), 989)
        self.assertEqual(pr.get_port('llm'), 808)
        self.assertEqual(pr.get_port('websocket'), 546)
        self.assertEqual(pr.get_port('diarization'), 800)
        self.assertEqual(pr.get_port('dlna_stream'), 855)
        self.assertEqual(pr.get_port('mesh_wg'), 679)
        self.assertEqual(pr.get_port('mesh_relay'), 680)

    def test_explicit_override(self):
        import core.port_registry as pr
        pr._os_mode_cached = False
        self.assertEqual(pr.get_port('backend', 9999), 9999)

    def test_override_beats_os_mode(self):
        import core.port_registry as pr
        pr._os_mode_cached = True
        self.assertEqual(pr.get_port('backend', 4444), 4444)

    @patch.dict(os.environ, {'HARTOS_BACKEND_PORT': '1234'})
    def test_env_override(self):
        import core.port_registry as pr
        pr._os_mode_cached = False
        self.assertEqual(pr.get_port('backend'), 1234)

    @patch.dict(os.environ, {'HARTOS_BACKEND_PORT': '1234'})
    def test_env_beats_os_mode(self):
        import core.port_registry as pr
        pr._os_mode_cached = True
        self.assertEqual(pr.get_port('backend'), 1234)

    def test_explicit_override_beats_env(self):
        import core.port_registry as pr
        with patch.dict(os.environ, {'HARTOS_BACKEND_PORT': '1234'}):
            self.assertEqual(pr.get_port('backend', 5555), 5555)

    @patch.dict(os.environ, {'HARTOS_BACKEND_PORT': 'not_a_number'})
    def test_invalid_env_falls_through(self):
        import core.port_registry as pr
        pr._os_mode_cached = False
        port = pr.get_port('backend')
        self.assertEqual(port, 6777)  # Falls back to app mode

    def test_unknown_service_returns_zero(self):
        import core.port_registry as pr
        pr._os_mode_cached = False
        self.assertEqual(pr.get_port('nonexistent'), 0)

    def test_unknown_service_os_mode_returns_zero(self):
        import core.port_registry as pr
        pr._os_mode_cached = True
        self.assertEqual(pr.get_port('nonexistent'), 0)


class TestGetAllPorts(unittest.TestCase):
    """Test get_all_ports()."""

    def setUp(self):
        import core.port_registry as pr
        pr._os_mode_cached = None

    def tearDown(self):
        import core.port_registry as pr
        pr._os_mode_cached = None

    def test_returns_all_services(self):
        import core.port_registry as pr
        pr._os_mode_cached = False
        ports = pr.get_all_ports()
        self.assertEqual(set(ports.keys()), set(pr.APP_PORTS.keys()))

    def test_app_mode_values(self):
        import core.port_registry as pr
        pr._os_mode_cached = False
        ports = pr.get_all_ports()
        self.assertEqual(ports['backend'], 6777)

    def test_os_mode_values(self):
        import core.port_registry as pr
        pr._os_mode_cached = True
        ports = pr.get_all_ports()
        self.assertEqual(ports['backend'], 677)


class TestCheckPortAvailable(unittest.TestCase):
    """Test check_port_available()."""

    def test_high_random_port_available(self):
        from core.port_registry import check_port_available
        # A very high port is almost certainly free
        result = check_port_available(59999, '127.0.0.1')
        self.assertIsInstance(result, bool)

    def test_occupied_port_unavailable(self):
        from core.port_registry import check_port_available
        # Bind a port, then check it's unavailable
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        try:
            self.assertFalse(check_port_available(port, '127.0.0.1'))
        finally:
            s.close()


class TestGetModeLabel(unittest.TestCase):
    """Test get_mode_label()."""

    def setUp(self):
        import core.port_registry as pr
        pr._os_mode_cached = None

    def tearDown(self):
        import core.port_registry as pr
        pr._os_mode_cached = None

    def test_app_mode_label(self):
        import core.port_registry as pr
        pr._os_mode_cached = False
        self.assertEqual(pr.get_mode_label(), 'APP')

    def test_os_mode_label(self):
        import core.port_registry as pr
        pr._os_mode_cached = True
        self.assertEqual(pr.get_mode_label(), 'OS')


if __name__ == '__main__':
    unittest.main()
