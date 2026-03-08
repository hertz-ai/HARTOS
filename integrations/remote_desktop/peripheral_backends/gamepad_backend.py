"""
Gamepad Backend — Forward gamepad input events over HARTOS transport.

Sunshine/Moonlight already forward gamepads natively. This backend provides
the same for the native HARTOS transport fallback.

Linux: evdev → read input events → transport → remote receives
Windows: Not yet supported (would need XInput/DirectInput)

Poll interval: ~4ms (250Hz) for responsive gamepad input.
"""

import logging
import platform
import struct
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve.remote_desktop')

# Optional: evdev for Linux gamepad
_evdev = None
try:
    import evdev as _evdev_module
    _evdev = _evdev_module
except ImportError:
    pass


class GamepadBackend:
    """Gamepad forwarding via evdev (Linux) or XInput (Windows)."""

    POLL_INTERVAL = 0.004  # ~250Hz

    def __init__(self):
        self._forwarded: Dict[str, dict] = {}
        self._relay_threads: Dict[str, threading.Thread] = {}
        self._running: Dict[str, bool] = {}

    def discover(self) -> list:
        """Enumerate connected gamepads."""
        if not self.available:
            return []

        if platform.system() == 'Linux' and _evdev:
            return self._discover_evdev()
        return []

    def forward(self, peripheral_info, transport) -> bool:
        """Start forwarding gamepad events over transport.

        Args:
            peripheral_info: PeripheralInfo for the gamepad.
            transport: TransportChannel to send events on.

        Returns:
            True if forwarding started.
        """
        device_id = peripheral_info.peripheral_id
        if device_id in self._forwarded:
            return True

        self._running[device_id] = True
        self._forwarded[device_id] = {
            'info': peripheral_info,
            'started_at': time.time(),
        }

        thread = threading.Thread(
            target=self._relay_loop,
            args=(device_id, transport),
            daemon=True,
            name=f'gamepad-{device_id[:12]}',
        )
        self._relay_threads[device_id] = thread
        thread.start()

        logger.info(f"Gamepad relay started: {peripheral_info.name}")
        return True

    def stop(self, peripheral_id: str) -> bool:
        """Stop forwarding a gamepad."""
        self._running[peripheral_id] = False
        thread = self._relay_threads.pop(peripheral_id, None)
        if thread:
            thread.join(timeout=3)
        self._forwarded.pop(peripheral_id, None)
        logger.info(f"Gamepad relay stopped: {peripheral_id}")
        return True

    def stop_all(self) -> None:
        """Stop all gamepad relays."""
        for device_id in list(self._forwarded.keys()):
            self.stop(device_id)

    @property
    def available(self) -> bool:
        """Check if gamepad support is available."""
        if platform.system() == 'Linux' and _evdev:
            return True
        return False

    @property
    def peripheral_type_name(self) -> str:
        return 'gamepad'

    # ── Linux evdev ──────────────────────────────────────────

    def _discover_evdev(self) -> list:
        """Discover gamepads via evdev."""
        from integrations.remote_desktop.peripheral_bridge import (
            PeripheralInfo, PeripheralType,
        )
        results = []
        for path in _evdev.list_devices():
            try:
                dev = _evdev.InputDevice(path)
                caps = dev.capabilities(verbose=False)

                # Check for gamepad-like capabilities:
                # EV_ABS (3) for analog sticks, EV_KEY (1) for buttons
                has_abs = 3 in caps  # EV_ABS
                has_key = 1 in caps  # EV_KEY
                if has_abs and has_key:
                    # Check for gamepad buttons (BTN_GAMEPAD range: 0x130-0x13f)
                    buttons = caps.get(1, [])
                    has_gamepad_btn = any(0x130 <= b <= 0x13f for b in buttons)
                    # Also check for joystick buttons (BTN_JOYSTICK: 0x120-0x12f)
                    has_joystick_btn = any(0x120 <= b <= 0x12f for b in buttons)

                    if has_gamepad_btn or has_joystick_btn:
                        results.append(PeripheralInfo(
                            peripheral_id=path,
                            name=dev.name,
                            peripheral_type=PeripheralType.GAMEPAD,
                            vendor_id=f'{dev.info.vendor:04x}' if dev.info else None,
                            product_id=f'{dev.info.product:04x}' if dev.info else None,
                            connected=True,
                        ))
            except Exception:
                continue
        return results

    def _relay_loop(self, device_id: str, transport) -> None:
        """Relay gamepad events to remote via transport."""
        if not _evdev:
            return

        try:
            dev = _evdev.InputDevice(device_id)
        except Exception as e:
            logger.debug(f"Cannot open gamepad {device_id}: {e}")
            return

        try:
            for ev in dev.read_loop():
                if not self._running.get(device_id, False):
                    break
                if ev.type == 0:  # EV_SYN
                    continue

                # Send gamepad event over transport
                if transport and hasattr(transport, 'send_event'):
                    transport.send_event({
                        'type': 'peripheral',
                        'subtype': 'gamepad',
                        'device_id': device_id,
                        'ev_type': ev.type,
                        'ev_code': ev.code,
                        'ev_value': ev.value,
                        'timestamp': ev.timestamp(),
                    })
        except Exception as e:
            logger.debug(f"Gamepad relay error: {e}")
