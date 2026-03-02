"""
Tests for peripheral forwarding (USB, Bluetooth, Gamepad).

Covers: peripheral_bridge.py, peripheral_backends/
"""

import unittest
from unittest.mock import MagicMock, patch


class TestPeripheralType(unittest.TestCase):
    """Tests for PeripheralType enum."""

    def test_peripheral_types(self):
        from integrations.remote_desktop.peripheral_bridge import PeripheralType
        self.assertEqual(PeripheralType.USB.value, 'usb')
        self.assertEqual(PeripheralType.BLUETOOTH.value, 'bluetooth')
        self.assertEqual(PeripheralType.GAMEPAD.value, 'gamepad')
        self.assertEqual(PeripheralType.GENERIC_HID.value, 'generic_hid')


class TestPeripheralInfo(unittest.TestCase):
    """Tests for PeripheralInfo dataclass."""

    def test_info_creation(self):
        from integrations.remote_desktop.peripheral_bridge import (
            PeripheralInfo, PeripheralType,
        )
        p = PeripheralInfo(
            peripheral_id='usb-001',
            name='USB Keyboard',
            peripheral_type=PeripheralType.USB,
            vendor_id='046d',
            product_id='c52b',
            connected=True,
        )
        self.assertEqual(p.peripheral_id, 'usb-001')
        self.assertEqual(p.name, 'USB Keyboard')
        self.assertTrue(p.connected)
        self.assertFalse(p.forwarded)

    def test_info_to_dict(self):
        from integrations.remote_desktop.peripheral_bridge import (
            PeripheralInfo, PeripheralType,
        )
        p = PeripheralInfo(
            peripheral_id='bt-aa:bb:cc',
            name='BT Mouse',
            peripheral_type=PeripheralType.BLUETOOTH,
            connected=True,
            forwarded=True,
        )
        d = p.to_dict()
        self.assertEqual(d['peripheral_id'], 'bt-aa:bb:cc')
        self.assertEqual(d['type'], 'bluetooth')
        self.assertTrue(d['forwarded'])


class TestPeripheralBridge(unittest.TestCase):
    """Tests for PeripheralBridge orchestrator."""

    def test_bridge_creation(self):
        from integrations.remote_desktop.peripheral_bridge import PeripheralBridge
        bridge = PeripheralBridge()
        self.assertIsNotNone(bridge)

    def test_discover_returns_list(self):
        from integrations.remote_desktop.peripheral_bridge import PeripheralBridge
        bridge = PeripheralBridge()
        result = bridge.discover_peripherals()
        self.assertIsInstance(result, list)

    def test_discover_with_type_filter(self):
        from integrations.remote_desktop.peripheral_bridge import PeripheralBridge
        bridge = PeripheralBridge()
        result = bridge.discover_peripherals(types=['usb'])
        self.assertIsInstance(result, list)

    def test_forward_nonexistent(self):
        from integrations.remote_desktop.peripheral_bridge import PeripheralBridge
        bridge = PeripheralBridge()
        result = bridge.forward_peripheral('nonexistent-id', MagicMock())
        self.assertFalse(result['success'])

    def test_stop_forwarding_nonexistent(self):
        from integrations.remote_desktop.peripheral_bridge import PeripheralBridge
        bridge = PeripheralBridge()
        result = bridge.stop_forwarding('nonexistent-id')
        self.assertFalse(result)

    def test_stop_all_empty(self):
        from integrations.remote_desktop.peripheral_bridge import PeripheralBridge
        bridge = PeripheralBridge()
        bridge.stop_all()  # Should not raise

    def test_get_status(self):
        from integrations.remote_desktop.peripheral_bridge import PeripheralBridge
        bridge = PeripheralBridge()
        status = bridge.get_status()
        self.assertIn('backends_available', status)
        self.assertIn('forwarded_count', status)
        self.assertEqual(status['forwarded_count'], 0)

    def test_get_available_backends(self):
        from integrations.remote_desktop.peripheral_bridge import PeripheralBridge
        bridge = PeripheralBridge()
        backends = bridge.get_available_backends()
        self.assertIsInstance(backends, list)

    def test_singleton(self):
        import integrations.remote_desktop.peripheral_bridge as pb_mod
        pb_mod._peripheral_bridge = None
        b1 = pb_mod.get_peripheral_bridge()
        b2 = pb_mod.get_peripheral_bridge()
        self.assertIs(b1, b2)
        pb_mod._peripheral_bridge = None  # cleanup


class TestPeripheralBackendBase(unittest.TestCase):
    """Tests for PeripheralBackend ABC."""

    def test_abc_methods(self):
        from integrations.remote_desktop.peripheral_backends.base import (
            PeripheralBackend,
        )
        # Verify it's abstract — can't instantiate
        with self.assertRaises(TypeError):
            PeripheralBackend()

    def test_abc_has_required_methods(self):
        from integrations.remote_desktop.peripheral_backends.base import (
            PeripheralBackend,
        )
        import inspect
        methods = [m for m in dir(PeripheralBackend) if not m.startswith('_')]
        self.assertIn('discover', methods)
        self.assertIn('forward', methods)
        self.assertIn('stop', methods)


class TestUSBIPBackend(unittest.TestCase):
    """Tests for USB/IP backend."""

    def test_backend_creation(self):
        from integrations.remote_desktop.peripheral_backends.usbip_backend import (
            USBIPBackend,
        )
        backend = USBIPBackend()
        self.assertIsNotNone(backend)

    def test_available_property(self):
        from integrations.remote_desktop.peripheral_backends.usbip_backend import (
            USBIPBackend,
        )
        backend = USBIPBackend()
        # On Windows/non-Linux, should be False
        avail = backend.available
        self.assertIsInstance(avail, bool)

    def test_discover_returns_list(self):
        from integrations.remote_desktop.peripheral_backends.usbip_backend import (
            USBIPBackend,
        )
        backend = USBIPBackend()
        result = backend.discover()
        self.assertIsInstance(result, list)

    def test_peripheral_type_name(self):
        from integrations.remote_desktop.peripheral_backends.usbip_backend import (
            USBIPBackend,
        )
        backend = USBIPBackend()
        self.assertEqual(backend.peripheral_type_name, 'usb')


class TestBluetoothBackend(unittest.TestCase):
    """Tests for Bluetooth HID backend."""

    def test_backend_creation(self):
        from integrations.remote_desktop.peripheral_backends.bluetooth_backend import (
            BluetoothBackend,
        )
        backend = BluetoothBackend()
        self.assertIsNotNone(backend)

    def test_available_property(self):
        from integrations.remote_desktop.peripheral_backends.bluetooth_backend import (
            BluetoothBackend,
        )
        backend = BluetoothBackend()
        avail = backend.available
        self.assertIsInstance(avail, bool)

    def test_discover_returns_list(self):
        from integrations.remote_desktop.peripheral_backends.bluetooth_backend import (
            BluetoothBackend,
        )
        backend = BluetoothBackend()
        result = backend.discover()
        self.assertIsInstance(result, list)

    def test_peripheral_type_name(self):
        from integrations.remote_desktop.peripheral_backends.bluetooth_backend import (
            BluetoothBackend,
        )
        backend = BluetoothBackend()
        self.assertEqual(backend.peripheral_type_name, 'bluetooth')

    def test_stop_nonexistent(self):
        from integrations.remote_desktop.peripheral_backends.bluetooth_backend import (
            BluetoothBackend,
        )
        backend = BluetoothBackend()
        result = backend.stop('nonexistent')
        self.assertTrue(result)  # stop returns True even if not found


class TestGamepadBackend(unittest.TestCase):
    """Tests for Gamepad backend."""

    def test_backend_creation(self):
        from integrations.remote_desktop.peripheral_backends.gamepad_backend import (
            GamepadBackend,
        )
        backend = GamepadBackend()
        self.assertIsNotNone(backend)

    def test_available_property(self):
        from integrations.remote_desktop.peripheral_backends.gamepad_backend import (
            GamepadBackend,
        )
        backend = GamepadBackend()
        avail = backend.available
        self.assertIsInstance(avail, bool)

    def test_discover_returns_list(self):
        from integrations.remote_desktop.peripheral_backends.gamepad_backend import (
            GamepadBackend,
        )
        backend = GamepadBackend()
        result = backend.discover()
        self.assertIsInstance(result, list)

    def test_peripheral_type_name(self):
        from integrations.remote_desktop.peripheral_backends.gamepad_backend import (
            GamepadBackend,
        )
        backend = GamepadBackend()
        self.assertEqual(backend.peripheral_type_name, 'gamepad')

    def test_stop_all_empty(self):
        from integrations.remote_desktop.peripheral_backends.gamepad_backend import (
            GamepadBackend,
        )
        backend = GamepadBackend()
        backend.stop_all()  # Should not raise


class TestPeripheralOrchestratorIntegration(unittest.TestCase):
    """Tests for peripheral methods on orchestrator."""

    def test_list_peripherals(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        result = orch.list_peripherals()
        self.assertIsInstance(result, list)

    def test_forward_peripheral_no_session(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        result = orch.forward_peripheral('fake-session', 'fake-peripheral')
        self.assertIn('error', result)

    def test_stop_peripheral_nonexistent(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        result = orch.stop_peripheral_forwarding('fake-id')
        self.assertFalse(result)


class TestPeripheralEngineSelector(unittest.TestCase):
    """Tests for peripheral-related engine selection."""

    def test_peripheral_forward_use_case_exists(self):
        from integrations.remote_desktop.engine_selector import UseCase
        self.assertEqual(UseCase.PERIPHERAL_FORWARD.value, 'peripheral_forward')

    def test_screen_cast_use_case_exists(self):
        from integrations.remote_desktop.engine_selector import UseCase
        self.assertEqual(UseCase.SCREEN_CAST.value, 'screen_cast')


if __name__ == '__main__':
    unittest.main()
