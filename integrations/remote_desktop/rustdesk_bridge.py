"""
RustDesk Bridge — Wraps RustDesk CLI/API for HARTOS agent integration.

RustDesk is installed as a native OS app. HARTOS invokes it via CLI commands
and manages sessions through its API. No code linking — separate process.

RustDesk CLI commands:
  rustdesk --get-id                    # Get this device's RustDesk ID
  rustdesk --password <pass>           # Set permanent password
  rustdesk --config <key> <value>      # Set configuration
  rustdesk --connect <id>              # Connect to remote device
  rustdesk --file-transfer <id>        # Open file transfer
  rustdesk --port-forward <id> ...     # Port forwarding

RustDesk capabilities:
  - Screen sharing + remote control (VP8/VP9/AV1)
  - File transfer (drag-and-drop)
  - Clipboard sync (text, images, files, HTML, RTF)
  - Audio streaming
  - Chat
  - P2P with NAT traversal (UDP/TCP hole punching)
  - Self-hosted relay server (hbbs + hbbr)
"""

import json
import logging
import os
import platform
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve.remote_desktop')


class RustDeskBridge:
    """Bridge between HARTOS and RustDesk native application."""

    # RustDesk binary names by platform
    BINARY_NAMES = {
        'Windows': ['rustdesk.exe', 'RustDesk.exe'],
        'Linux': ['rustdesk'],
        'Darwin': ['rustdesk', 'RustDesk'],
    }

    # Common installation paths
    INSTALL_PATHS = {
        'Windows': [
            os.path.join(os.environ.get('PROGRAMFILES', 'C:\\Program Files'), 'RustDesk'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'RustDesk'),
        ],
        'Linux': [
            '/usr/bin',
            '/usr/local/bin',
            '/opt/rustdesk',
            '/snap/bin',
            '/flatpak/exports/bin',
        ],
        'Darwin': [
            '/Applications/RustDesk.app/Contents/MacOS',
            '/usr/local/bin',
        ],
    }

    def __init__(self, binary_path: Optional[str] = None,
                 server_url: Optional[str] = None):
        """
        Args:
            binary_path: Explicit path to rustdesk binary. Auto-detected if None.
            server_url: Custom relay server URL (self-hosted). Uses default if None.
        """
        self._binary = binary_path or self._find_binary()
        self._server_url = server_url or os.environ.get('RUSTDESK_SERVER')
        self._device_id: Optional[str] = None

    # ── Detection ───────────────────────────────────────────────

    def _find_binary(self) -> Optional[str]:
        """Auto-detect RustDesk binary on this system."""
        system = platform.system()
        binary_names = self.BINARY_NAMES.get(system, ['rustdesk'])

        # Check PATH first
        for name in binary_names:
            path = shutil.which(name)
            if path:
                logger.info(f"RustDesk found in PATH: {path}")
                return path

        # Check common install paths
        install_paths = self.INSTALL_PATHS.get(system, [])
        for dir_path in install_paths:
            for name in binary_names:
                full_path = os.path.join(dir_path, name)
                if os.path.isfile(full_path):
                    logger.info(f"RustDesk found at: {full_path}")
                    return full_path

        logger.info("RustDesk not found on this system")
        return None

    @property
    def available(self) -> bool:
        """Whether RustDesk is installed and accessible."""
        return self._binary is not None

    @property
    def binary_path(self) -> Optional[str]:
        return self._binary

    # ── CLI Commands ────────────────────────────────────────────

    def _run(self, args: List[str], timeout: int = 10) -> Tuple[bool, str]:
        """Run RustDesk CLI command.

        Returns:
            (success, output)
        """
        if not self._binary:
            return False, 'RustDesk not installed'

        cmd = [self._binary] + args
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            output = (result.stdout or '').strip()
            if result.returncode == 0:
                return True, output
            error = (result.stderr or '').strip()
            return False, error or output or f'Exit code {result.returncode}'
        except subprocess.TimeoutExpired:
            return False, 'Command timed out'
        except FileNotFoundError:
            self._binary = None
            return False, 'RustDesk binary not found'
        except Exception as e:
            return False, str(e)

    def get_id(self) -> Optional[str]:
        """Get this device's RustDesk ID.

        Returns:
            RustDesk ID string (e.g., '123456789') or None.
        """
        if self._device_id:
            return self._device_id

        ok, output = self._run(['--get-id'])
        if ok and output:
            self._device_id = output.strip()
            logger.info(f"RustDesk ID: {self._device_id}")
            return self._device_id
        return None

    def set_password(self, password: str) -> bool:
        """Set permanent access password.

        Returns:
            True if password was set.
        """
        ok, _ = self._run(['--password', password])
        return ok

    def get_config(self, key: str) -> Optional[str]:
        """Get RustDesk configuration value."""
        ok, output = self._run(['--get-config', key])
        return output if ok else None

    def set_config(self, key: str, value: str) -> bool:
        """Set RustDesk configuration value."""
        ok, _ = self._run(['--config', key, value])
        return ok

    def configure_server(self, relay_server: str,
                         api_server: Optional[str] = None,
                         key: Optional[str] = None) -> bool:
        """Configure custom relay server (self-hosted).

        Args:
            relay_server: Relay server address (e.g., 'relay.example.com')
            api_server: API server address (optional)
            key: Server public key (optional)
        """
        results = [self.set_config('relay-server', relay_server)]
        if api_server:
            results.append(self.set_config('api-server', api_server))
        if key:
            results.append(self.set_config('key', key))
        return all(results)

    # ── Session Control ─────────────────────────────────────────

    def connect(self, remote_id: str, password: Optional[str] = None,
                file_transfer: bool = False) -> Tuple[bool, str]:
        """Connect to remote device.

        Args:
            remote_id: Target RustDesk ID
            password: Access password (optional for same-user)
            file_transfer: Open file transfer mode instead of remote control

        Returns:
            (success, message)
        """
        args = ['--file-transfer' if file_transfer else '--connect', remote_id]
        if password:
            args.extend(['--password', password])

        # Launch as background process (non-blocking)
        if not self._binary:
            return False, 'RustDesk not installed'

        try:
            cmd = [self._binary] + args
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            logger.info(f"RustDesk connecting to {remote_id} (pid={proc.pid})")
            return True, f'Connecting to {remote_id}'
        except Exception as e:
            return False, str(e)

    def disconnect_all(self) -> bool:
        """Close all RustDesk connections."""
        # RustDesk doesn't have a direct disconnect CLI; close the process
        system = platform.system()
        try:
            if system == 'Windows':
                subprocess.run(['taskkill', '/f', '/im', 'rustdesk.exe'],
                               capture_output=True, timeout=5)
            else:
                subprocess.run(['pkill', '-f', 'rustdesk'],
                               capture_output=True, timeout=5)
            return True
        except Exception:
            return False

    # ── Service Management ──────────────────────────────────────

    def start_service(self) -> bool:
        """Start RustDesk background service (enables incoming connections)."""
        ok, _ = self._run(['--service', 'start'], timeout=15)
        if not ok:
            # Alternative: launch in service mode
            try:
                if self._binary:
                    subprocess.Popen(
                        [self._binary, '--service'],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return True
            except Exception:
                pass
        return ok

    def stop_service(self) -> bool:
        """Stop RustDesk background service."""
        ok, _ = self._run(['--service', 'stop'], timeout=15)
        return ok

    def is_service_running(self) -> bool:
        """Check if RustDesk service is running."""
        system = platform.system()
        try:
            if system == 'Windows':
                result = subprocess.run(
                    ['tasklist', '/fi', 'imagename eq rustdesk.exe'],
                    capture_output=True, text=True, timeout=5,
                )
                return 'rustdesk.exe' in result.stdout.lower()
            else:
                result = subprocess.run(
                    ['pgrep', '-f', 'rustdesk'],
                    capture_output=True, timeout=5,
                )
                return result.returncode == 0
        except Exception:
            return False

    # ── Installation ────────────────────────────────────────────

    def get_install_command(self) -> str:
        """Get platform-specific install command for RustDesk."""
        system = platform.system()
        if system == 'Linux':
            return (
                "# Debian/Ubuntu:\n"
                "wget https://github.com/rustdesk/rustdesk/releases/latest/download/"
                "rustdesk-<version>-x86_64.deb && sudo dpkg -i rustdesk-*.deb\n"
                "# NixOS:\n"
                "nix-env -iA nixpkgs.rustdesk\n"
                "# Flatpak:\n"
                "flatpak install flathub com.rustdesk.RustDesk"
            )
        elif system == 'Darwin':
            return "brew install --cask rustdesk"
        elif system == 'Windows':
            return "winget install RustDesk.RustDesk"
        return "Visit https://rustdesk.com/download"

    # ── Status ──────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Get RustDesk status for HARTOS."""
        return {
            'engine': 'rustdesk',
            'available': self.available,
            'binary_path': self._binary,
            'device_id': self.get_id() if self.available else None,
            'service_running': self.is_service_running() if self.available else False,
            'server_url': self._server_url,
            'install_command': self.get_install_command() if not self.available else None,
        }


# ── Singleton ───────────────────────────────────────────────────

_rustdesk_bridge: Optional[RustDeskBridge] = None


def get_rustdesk_bridge() -> RustDeskBridge:
    """Get or create the singleton RustDeskBridge."""
    global _rustdesk_bridge
    if _rustdesk_bridge is None:
        _rustdesk_bridge = RustDeskBridge()
    return _rustdesk_bridge
