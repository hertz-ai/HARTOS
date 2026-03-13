"""
USB over IP Backend — Wraps Linux usbip kernel module for USB device forwarding.

Host side: `usbip bind -b <busid>` → exposes USB device
Viewer side: `usbip attach -r <host> -b <busid>` → attaches remote USB device

Requires: usbip kernel module loaded (usbip-core, usbip-host, vhci-hcd).
NixOS: nixos/modules/hart-peripheral-bridge.nix loads these automatically.
"""

import logging
import platform
import subprocess
from typing import List

logger = logging.getLogger('hevolve.remote_desktop')


class USBIPBackend:
    """USB over IP via Linux usbip kernel module."""

    def __init__(self):
        self._forwarded = {}  # busid → info

    def discover(self) -> list:
        """Discover locally connected USB devices via `usbip list -l`."""
        if not self.available:
            return []

        try:
            output = subprocess.check_output(
                ['usbip', 'list', '-l'],
                timeout=5,
                text=True,
                stderr=subprocess.DEVNULL,
            )
            return self._parse_usbip_list(output)
        except Exception as e:
            logger.debug(f"USB discovery failed: {e}")
            return []

    def forward(self, peripheral_info, transport) -> bool:
        """Bind a USB device for remote access via usbip.

        Args:
            peripheral_info: PeripheralInfo with peripheral_id = bus_id.
            transport: TransportChannel (used for signaling, not data).

        Returns:
            True if bind succeeded.
        """
        bus_id = peripheral_info.peripheral_id
        try:
            subprocess.check_call(
                ['usbip', 'bind', '-b', bus_id],
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._forwarded[bus_id] = peripheral_info
            logger.info(f"USB device bound: {bus_id}")

            # Notify remote via transport
            if transport and hasattr(transport, 'send_event'):
                transport.send_event({
                    'type': 'peripheral',
                    'action': 'USB_AVAILABLE',
                    'bus_id': bus_id,
                    'name': peripheral_info.name,
                })
            return True
        except Exception as e:
            logger.warning(f"USB bind failed for {bus_id}: {e}")
            return False

    def attach_remote(self, host_ip: str, bus_id: str) -> bool:
        """Attach a remote USB device (viewer side).

        Args:
            host_ip: IP address of the host sharing the USB device.
            bus_id: USB bus ID to attach.

        Returns:
            True if attach succeeded.
        """
        try:
            subprocess.check_call(
                ['usbip', 'attach', '-r', host_ip, '-b', bus_id],
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(f"USB device attached: {bus_id} from {host_ip}")
            return True
        except Exception as e:
            logger.warning(f"USB attach failed: {e}")
            return False

    def stop(self, peripheral_id: str) -> bool:
        """Unbind a USB device."""
        try:
            subprocess.check_call(
                ['usbip', 'unbind', '-b', peripheral_id],
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._forwarded.pop(peripheral_id, None)
            logger.info(f"USB device unbound: {peripheral_id}")
            return True
        except Exception as e:
            logger.debug(f"USB unbind failed: {e}")
            return False

    def stop_all(self) -> None:
        """Unbind all forwarded USB devices."""
        for bus_id in list(self._forwarded.keys()):
            self.stop(bus_id)

    @property
    def available(self) -> bool:
        """Check if usbip is available on this system."""
        if platform.system() != 'Linux':
            return False
        try:
            subprocess.check_call(
                ['which', 'usbip'],
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False

    @property
    def peripheral_type_name(self) -> str:
        return 'usb'

    def _parse_usbip_list(self, output: str) -> list:
        """Parse `usbip list -l` output into PeripheralInfo-like dicts."""
        from integrations.remote_desktop.peripheral_bridge import (
            PeripheralInfo, PeripheralType,
        )
        results = []
        current_busid = None
        current_name = ''

        for line in output.split('\n'):
            line = line.strip()
            if line.startswith('-'):
                # Bus ID line: " - busid 1-1 (0bda:8153)"
                parts = line.split()
                if len(parts) >= 3 and parts[1] == 'busid':
                    current_busid = parts[2]
                    # Vendor:product in parentheses
                    for p in parts:
                        if '(' in p:
                            vid_pid = p.strip('()')
                            break
            elif current_busid and ':' in line:
                # Description line
                current_name = line.strip()
                results.append(PeripheralInfo(
                    peripheral_id=current_busid,
                    name=current_name,
                    peripheral_type=PeripheralType.USB,
                    connected=True,
                ))
                current_busid = None

        return results
