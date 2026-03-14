"""
Sunshine + Moonlight Bridge — Wraps Sunshine (host) and Moonlight (viewer) for
high-fidelity game-streaming-quality remote desktop.

Sunshine: GPL-3.0 host that captures screen with hardware encoding (NVENC/AMF/QSV/VAAPI).
Moonlight: GPL-3.0 viewer that decodes and renders at up to 4K@120fps with <10ms latency.

Both installed as native OS apps. HARTOS invokes via CLI + Sunshine REST API.
No code linking — separate processes.

Sunshine REST API (default: https://localhost:47990):
  GET  /api/apps          — List configured apps
  POST /api/apps          — Add app configuration
  GET  /api/config        — Get server config
  POST /api/config        — Set server config
  POST /api/pin           — Pair with PIN
  GET  /api/clients       — List paired clients

Use cases:
  - VLM agents needing high-FPS remote screen capture (60fps for computer-use)
  - Gaming across devices (same user, different rooms)
  - Creative work (video editing, 3D modeling remote)
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

# ── Optional: pooled HTTP for Sunshine REST API ─────────────────
_http_pool = None
try:
    from core.http_pool import pooled_get as _pooled_get, pooled_post as _pooled_post
    _http_pool = True
except ImportError:
    _pooled_get = None
    _pooled_post = None


class SunshineBridge:
    """Bridge between HARTOS and Sunshine host service."""

    BINARY_NAMES = {
        'Windows': ['sunshine.exe'],
        'Linux': ['sunshine'],
        'Darwin': ['sunshine'],
    }

    INSTALL_PATHS = {
        'Windows': [
            os.path.join(os.environ.get('PROGRAMFILES', 'C:\\Program Files'), 'Sunshine'),
            os.path.join(os.environ.get('PROGRAMFILES', 'C:\\Program Files'),
                         'Sunshine', 'sunshine.exe'),
        ],
        'Linux': [
            '/usr/bin',
            '/usr/local/bin',
            '/opt/sunshine',
        ],
        'Darwin': [
            '/Applications/Sunshine.app/Contents/MacOS',
            '/usr/local/bin',
        ],
    }

    DEFAULT_API_PORT = 47990
    DEFAULT_API_URL = f'https://localhost:{DEFAULT_API_PORT}'

    def __init__(self, binary_path: Optional[str] = None,
                 api_url: Optional[str] = None,
                 username: str = 'sunshine',
                 password: str = 'sunshine'):
        self._binary = binary_path or self._find_binary()
        self._api_url = api_url or os.environ.get(
            'SUNSHINE_API_URL', self.DEFAULT_API_URL)
        self._username = username
        self._password = password

    def _find_binary(self) -> Optional[str]:
        """Auto-detect Sunshine binary."""
        system = platform.system()
        for name in self.BINARY_NAMES.get(system, ['sunshine']):
            path = shutil.which(name)
            if path:
                return path
        for dir_path in self.INSTALL_PATHS.get(system, []):
            for name in self.BINARY_NAMES.get(system, ['sunshine']):
                full = os.path.join(dir_path, name)
                if os.path.isfile(full):
                    return full
        return None

    @property
    def available(self) -> bool:
        return self._binary is not None

    # ── REST API ────────────────────────────────────────────────

    def _api_get(self, endpoint: str) -> Optional[dict]:
        """GET request to Sunshine REST API."""
        if not _http_pool:
            return None
        try:
            resp = _pooled_get(
                f"{self._api_url}{endpoint}",
                auth=(self._username, self._password),
                verify=False,  # Sunshine uses self-signed cert
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.debug(f"Sunshine API GET {endpoint} failed: {e}")
        return None

    def _api_post(self, endpoint: str, data: dict) -> Optional[dict]:
        """POST request to Sunshine REST API."""
        if not _http_pool:
            return None
        try:
            resp = _pooled_post(
                f"{self._api_url}{endpoint}",
                json=data,
                auth=(self._username, self._password),
                verify=False,
                timeout=5,
            )
            if resp.status_code in (200, 201):
                return resp.json() if resp.text else {'success': True}
        except Exception as e:
            logger.debug(f"Sunshine API POST {endpoint} failed: {e}")
        return None

    def get_apps(self) -> Optional[list]:
        """List configured streaming apps."""
        result = self._api_get('/api/apps')
        return result.get('apps', []) if result else None

    def get_config(self) -> Optional[dict]:
        """Get Sunshine server configuration."""
        return self._api_get('/api/config')

    def set_config(self, config: dict) -> bool:
        """Update Sunshine configuration."""
        result = self._api_post('/api/config', config)
        return result is not None

    def pair_with_pin(self, pin: str) -> bool:
        """Pair a Moonlight client using PIN.

        The PIN is displayed on the Moonlight client.
        """
        result = self._api_post('/api/pin', {'pin': pin})
        return result is not None

    def get_paired_clients(self) -> Optional[list]:
        """List paired Moonlight clients."""
        result = self._api_get('/api/clients')
        return result.get('clients', []) if result else None

    # ── Service Management ──────────────────────────────────────

    def start_service(self) -> bool:
        """Start Sunshine streaming service."""
        if not self._binary:
            return False
        try:
            subprocess.Popen(
                [self._binary],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            time.sleep(2)  # Give it time to start
            return self.is_running()
        except Exception as e:
            logger.error(f"Failed to start Sunshine: {e}")
            return False

    def stop_service(self) -> bool:
        """Stop Sunshine service."""
        system = platform.system()
        try:
            if system == 'Windows':
                subprocess.run(['taskkill', '/f', '/im', 'sunshine.exe'],
                               capture_output=True, timeout=5)
            else:
                subprocess.run(['pkill', '-f', 'sunshine'],
                               capture_output=True, timeout=5)
            return True
        except Exception:
            return False

    def is_running(self) -> bool:
        """Check if Sunshine is running (try API ping)."""
        result = self._api_get('/api/config')
        return result is not None

    # ── Status ──────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            'engine': 'sunshine',
            'available': self.available,
            'binary_path': self._binary,
            'api_url': self._api_url,
            'running': self.is_running() if self.available else False,
            'paired_clients': len(self.get_paired_clients() or []) if self.available else 0,
        }

    def get_install_command(self) -> str:
        system = platform.system()
        if system == 'Linux':
            return (
                "# Debian/Ubuntu:\n"
                "wget https://github.com/LizardByte/Sunshine/releases/latest/download/"
                "sunshine-ubuntu-22.04-amd64.deb && sudo dpkg -i sunshine-*.deb\n"
                "# NixOS:\n"
                "nix-env -iA nixpkgs.sunshine\n"
                "# Flatpak:\n"
                "flatpak install flathub dev.lizardbyte.app.Sunshine"
            )
        elif system == 'Darwin':
            return "brew install --cask sunshine"
        elif system == 'Windows':
            return "winget install LizardByte.Sunshine"
        return "Visit https://github.com/LizardByte/Sunshine/releases"


class MoonlightBridge:
    """Bridge between HARTOS and Moonlight viewer client."""

    BINARY_NAMES = {
        'Windows': ['moonlight.exe', 'Moonlight.exe'],
        'Linux': ['moonlight', 'moonlight-qt'],
        'Darwin': ['moonlight', 'Moonlight'],
    }

    def __init__(self, binary_path: Optional[str] = None):
        self._binary = binary_path or self._find_binary()

    def _find_binary(self) -> Optional[str]:
        system = platform.system()
        for name in self.BINARY_NAMES.get(system, ['moonlight']):
            path = shutil.which(name)
            if path:
                return path
        return None

    @property
    def available(self) -> bool:
        return self._binary is not None

    def stream(self, host: str, app: str = 'Desktop',
               resolution: str = '1920x1080', fps: int = 60) -> Tuple[bool, str]:
        """Start streaming from a Sunshine host.

        Args:
            host: Sunshine host IP or hostname
            app: App name to stream (default: 'Desktop')
            resolution: Stream resolution (e.g., '1920x1080', '3840x2160')
            fps: Frame rate (30, 60, 120)
        """
        if not self._binary:
            return False, 'Moonlight not installed'

        width, height = resolution.split('x')
        args = [
            self._binary, 'stream',
            host, app,
            '--resolution', resolution,
            '--fps', str(fps),
        ]

        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True, f'Streaming {app} from {host} at {resolution}@{fps}fps (pid={proc.pid})'
        except Exception as e:
            return False, str(e)

    def pair(self, host: str) -> Tuple[bool, str]:
        """Pair with Sunshine host (will display PIN on Moonlight)."""
        if not self._binary:
            return False, 'Moonlight not installed'
        try:
            result = subprocess.run(
                [self._binary, 'pair', host],
                capture_output=True, text=True, timeout=30,
            )
            return result.returncode == 0, result.stdout.strip()
        except Exception as e:
            return False, str(e)

    def list_hosts(self) -> List[str]:
        """List discovered Sunshine hosts."""
        if not self._binary:
            return []
        try:
            result = subprocess.run(
                [self._binary, 'list'],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except Exception:
            pass
        return []

    def get_status(self) -> dict:
        return {
            'engine': 'moonlight',
            'available': self.available,
            'binary_path': self._binary,
        }

    def get_install_command(self) -> str:
        system = platform.system()
        if system == 'Linux':
            return (
                "# Debian/Ubuntu:\n"
                "sudo apt install moonlight-qt\n"
                "# NixOS:\n"
                "nix-env -iA nixpkgs.moonlight-qt\n"
                "# Flatpak:\n"
                "flatpak install flathub com.moonlight_stream.Moonlight"
            )
        elif system == 'Darwin':
            return "brew install --cask moonlight"
        elif system == 'Windows':
            return "winget install MoonlightGameStreamingProject.Moonlight"
        return "Visit https://moonlight-stream.org"


# ── Singletons ──────────────────────────────────────────────────

_sunshine: Optional[SunshineBridge] = None
_moonlight: Optional[MoonlightBridge] = None


def get_sunshine_bridge() -> SunshineBridge:
    global _sunshine
    if _sunshine is None:
        _sunshine = SunshineBridge()
    return _sunshine


def get_moonlight_bridge() -> MoonlightBridge:
    global _moonlight
    if _moonlight is None:
        _moonlight = MoonlightBridge()
    return _moonlight
