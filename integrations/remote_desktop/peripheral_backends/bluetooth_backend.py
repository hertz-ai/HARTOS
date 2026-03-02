"""
Bluetooth HID Backend — Relay BT HID events over HARTOS transport.

Strategy: Enumerate paired BT devices, relay HID input events (not full BT stack).
Linux: dbus org.bluez for discovery, evdev for HID event reading.
Windows: Not yet supported (would need win32 Bluetooth APIs).

This is event-level forwarding, not full Bluetooth device redirection.
"""

import logging
import platform
import threading
from typing import List

logger = logging.getLogger('hevolve.remote_desktop')

# Optional: dbus for BlueZ discovery
_dbus = None
try:
    import dbus as _dbus_module
    _dbus = _dbus_module
except ImportError:
    pass

# Optional: evdev for Linux HID input
_evdev = None
try:
    import evdev as _evdev_module
    _evdev = _evdev_module
except ImportError:
    pass


class BluetoothBackend:
    """Bluetooth HID event relay over HARTOS transport.

    Discovers paired BT devices via BlueZ D-Bus, then relays HID
    events (keyboard/mouse/gamepad) from the BT device over the
    HARTOS transport channel to the remote host.
    """

    def __init__(self):
        self._forwarded = {}       # device_id → relay thread
        self._relay_threads = {}   # device_id → Thread
        self._running = {}         # device_id → bool

    def discover(self) -> list:
        """Enumerate paired Bluetooth devices.

        Linux: Uses D-Bus BlueZ interface.
        """
        if not self.available:
            return []

        if platform.system() == 'Linux' and _dbus:
            return self._discover_bluez()
        return []

    def forward(self, peripheral_info, transport) -> bool:
        """Start relaying HID events from a BT device.

        Args:
            peripheral_info: PeripheralInfo with peripheral_id = BT MAC.
            transport: TransportChannel to send events on.

        Returns:
            True if relay started.
        """
        device_id = peripheral_info.peripheral_id
        if device_id in self._forwarded:
            return True  # Already forwarding

        self._running[device_id] = True
        self._forwarded[device_id] = peripheral_info

        # Start relay thread
        thread = threading.Thread(
            target=self._relay_loop,
            args=(device_id, transport),
            daemon=True,
            name=f'bt-relay-{device_id[:8]}',
        )
        self._relay_threads[device_id] = thread
        thread.start()

        logger.info(f"BT HID relay started: {device_id}")
        return True

    def stop(self, peripheral_id: str) -> bool:
        """Stop relaying a BT device."""
        self._running[peripheral_id] = False
        thread = self._relay_threads.pop(peripheral_id, None)
        if thread:
            thread.join(timeout=3)
        self._forwarded.pop(peripheral_id, None)
        logger.info(f"BT HID relay stopped: {peripheral_id}")
        return True

    def stop_all(self) -> None:
        """Stop all BT relays."""
        for device_id in list(self._forwarded.keys()):
            self.stop(device_id)

    @property
    def available(self) -> bool:
        """Check if BT backend dependencies are available."""
        if platform.system() != 'Linux':
            return False
        return _dbus is not None

    @property
    def peripheral_type_name(self) -> str:
        return 'bluetooth'

    # ── BlueZ discovery ──────────────────────────────────────

    def _discover_bluez(self) -> list:
        """Discover BT devices via BlueZ D-Bus interface."""
        from integrations.remote_desktop.peripheral_bridge import (
            PeripheralInfo, PeripheralType,
        )
        results = []
        try:
            bus = _dbus.SystemBus()
            manager = _dbus.Interface(
                bus.get_object('org.bluez', '/'),
                'org.freedesktop.DBus.ObjectManager',
            )
            objects = manager.GetManagedObjects()

            for path, interfaces in objects.items():
                if 'org.bluez.Device1' not in interfaces:
                    continue
                props = interfaces['org.bluez.Device1']
                address = str(props.get('Address', ''))
                name = str(props.get('Name', 'Unknown BT Device'))
                paired = bool(props.get('Paired', False))
                connected = bool(props.get('Connected', False))

                if paired:
                    results.append(PeripheralInfo(
                        peripheral_id=address,
                        name=name,
                        peripheral_type=PeripheralType.BLUETOOTH,
                        connected=connected,
                    ))
        except Exception as e:
            logger.debug(f"BlueZ discovery failed: {e}")

        return results

    # ── HID relay ────────────────────────────────────────────

    def _relay_loop(self, device_id: str, transport) -> None:
        """Relay HID input events from a BT device over transport.

        Uses evdev to read from the corresponding /dev/input/event* device.
        """
        if not _evdev:
            logger.debug("evdev not available for BT HID relay")
            return

        # Find evdev device matching the BT address
        input_device = self._find_evdev_device(device_id)
        if not input_device:
            logger.debug(f"No evdev device found for BT {device_id}")
            return

        try:
            for ev in input_device.read_loop():
                if not self._running.get(device_id, False):
                    break
                if ev.type == 0:  # EV_SYN
                    continue

                # Send HID event over transport
                if transport and hasattr(transport, 'send_event'):
                    transport.send_event({
                        'type': 'peripheral',
                        'subtype': 'bt_hid',
                        'device_id': device_id,
                        'ev_type': ev.type,
                        'ev_code': ev.code,
                        'ev_value': ev.value,
                    })
        except Exception as e:
            logger.debug(f"BT HID relay error: {e}")

    def _find_evdev_device(self, bt_address: str):
        """Find the evdev input device for a BT address."""
        if not _evdev:
            return None

        # Normalize address for matching
        normalized = bt_address.upper().replace(':', '')

        for path in _evdev.list_devices():
            try:
                dev = _evdev.InputDevice(path)
                # Check if device's uniq matches BT address
                if dev.uniq and dev.uniq.upper().replace(':', '') == normalized:
                    return dev
            except Exception:
                continue
        return None
