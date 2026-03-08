"""
Peripheral Bridge — Orchestrates peripheral forwarding across HARTOS transport.

Wraps platform tools for USB, Bluetooth, and Gamepad forwarding:
  USB: usbip (Linux kernel module)
  BT: HID event relay via dbus/evdev
  Gamepad: evdev → transport → remote

HARTOS doesn't reimplement device drivers — it orchestrates existing system
tools the same way it orchestrates RustDesk/Sunshine for remote desktop.

Reuses:
  - transport.py: TransportChannel for event relay
  - security.py: audit_session_event() for peripheral audit
  - service_manager.py: lifecycle pattern (singleton + NodeWatchdog)
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

logger = logging.getLogger('hevolve.remote_desktop')


class PeripheralType(Enum):
    USB = 'usb'
    BLUETOOTH = 'bluetooth'
    GAMEPAD = 'gamepad'
    GENERIC_HID = 'generic_hid'


@dataclass
class PeripheralInfo:
    """Discovered peripheral device."""
    peripheral_id: str          # Unique ID (USB busid, BT MAC, evdev path)
    name: str
    peripheral_type: PeripheralType
    vendor_id: Optional[str] = None
    product_id: Optional[str] = None
    connected: bool = False
    forwarded: bool = False

    def to_dict(self) -> dict:
        return {
            'peripheral_id': self.peripheral_id,
            'name': self.name,
            'type': self.peripheral_type.value,
            'vendor_id': self.vendor_id,
            'product_id': self.product_id,
            'connected': self.connected,
            'forwarded': self.forwarded,
        }


class PeripheralBridge:
    """Orchestrates peripheral forwarding across HARTOS transport.

    Discovers local peripherals (USB, BT, gamepad) and forwards selected
    devices to the remote host over the HARTOS transport channel.
    """

    def __init__(self):
        self._forwarded: Dict[str, PeripheralInfo] = {}
        self._backends: Dict[str, object] = {}
        self._lock = threading.Lock()
        self._init_backends()

    def _init_backends(self) -> None:
        """Initialize available backends."""
        try:
            from integrations.remote_desktop.peripheral_backends.usbip_backend import (
                USBIPBackend,
            )
            backend = USBIPBackend()
            if backend.available:
                self._backends['usb'] = backend
        except Exception:
            pass

        try:
            from integrations.remote_desktop.peripheral_backends.bluetooth_backend import (
                BluetoothBackend,
            )
            backend = BluetoothBackend()
            if backend.available:
                self._backends['bluetooth'] = backend
        except Exception:
            pass

        try:
            from integrations.remote_desktop.peripheral_backends.gamepad_backend import (
                GamepadBackend,
            )
            backend = GamepadBackend()
            if backend.available:
                self._backends['gamepad'] = backend
        except Exception:
            pass

    def discover_peripherals(self,
                              types: Optional[List[str]] = None) -> List[PeripheralInfo]:
        """Enumerate connected peripherals.

        Args:
            types: Filter by type names (e.g., ['usb', 'gamepad']).
                   None = discover all types.

        Returns:
            List of PeripheralInfo for discovered devices.
        """
        results = []
        for type_name, backend in self._backends.items():
            if types and type_name not in types:
                continue
            try:
                discovered = backend.discover()
                # Mark forwarded status
                for p in discovered:
                    if p.peripheral_id in self._forwarded:
                        p.forwarded = True
                results.extend(discovered)
            except Exception as e:
                logger.debug(f"Discovery failed for {type_name}: {e}")
        return results

    def forward_peripheral(self, peripheral_id: str,
                           transport, session_id: str = '') -> dict:
        """Start forwarding a peripheral to the remote device.

        Args:
            peripheral_id: ID of the peripheral to forward.
            transport: TransportChannel to send events on.
            session_id: Remote desktop session ID (for audit).

        Returns:
            {success, peripheral_id, name, type}
        """
        # Find the peripheral
        all_peripherals = self.discover_peripherals()
        target = None
        for p in all_peripherals:
            if p.peripheral_id == peripheral_id:
                target = p
                break

        if not target:
            return {'success': False, 'error': f'Peripheral not found: {peripheral_id}'}

        if target.forwarded:
            return {'success': True, 'note': 'Already forwarding'}

        # Get backend for this type
        backend = self._backends.get(target.peripheral_type.value)
        if not backend:
            return {'success': False,
                    'error': f'No backend for {target.peripheral_type.value}'}

        # Forward
        ok = backend.forward(target, transport)
        if ok:
            with self._lock:
                target.forwarded = True
                self._forwarded[peripheral_id] = target

            # Audit
            self._audit('peripheral_forward', session_id,
                        f'{target.peripheral_type.value}: {target.name}')

            return {
                'success': True,
                'peripheral_id': peripheral_id,
                'name': target.name,
                'type': target.peripheral_type.value,
            }

        return {'success': False, 'error': 'Backend forward failed'}

    def stop_forwarding(self, peripheral_id: str) -> bool:
        """Stop forwarding a specific peripheral."""
        with self._lock:
            info = self._forwarded.pop(peripheral_id, None)
        if not info:
            return False

        backend = self._backends.get(info.peripheral_type.value)
        if backend:
            return backend.stop(peripheral_id)
        return False

    def stop_all(self) -> None:
        """Stop all peripheral forwarding."""
        with self._lock:
            ids = list(self._forwarded.keys())
        for pid in ids:
            self.stop_forwarding(pid)

    def get_status(self) -> dict:
        """Get peripheral bridge status."""
        with self._lock:
            forwarded = [p.to_dict() for p in self._forwarded.values()]
        return {
            'backends_available': list(self._backends.keys()),
            'forwarded_count': len(forwarded),
            'forwarded': forwarded,
        }

    def get_available_backends(self) -> List[str]:
        """Get list of available backend types."""
        return list(self._backends.keys())

    def _audit(self, event_type: str, session_id: str, detail: str) -> None:
        """Audit log peripheral events."""
        try:
            from integrations.remote_desktop.security import audit_session_event
            audit_session_event(event_type, session_id, 'peripheral_bridge', detail)
        except Exception:
            pass


# ── Singleton ────────────────────────────────────────────────

_peripheral_bridge: Optional[PeripheralBridge] = None


def get_peripheral_bridge() -> PeripheralBridge:
    """Get or create the singleton PeripheralBridge."""
    global _peripheral_bridge
    if _peripheral_bridge is None:
        _peripheral_bridge = PeripheralBridge()
    return _peripheral_bridge
