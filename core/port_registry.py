"""
Port Registry — Single source of truth for HART OS service ports.

Two modes:
  APP MODE (default):  User-space ports (6777+) for running alongside other apps
  OS MODE  (NixOS):    Privileged ports (<1024) for running as the operating system

OS mode is activated when:
  - HART_OS_MODE=true environment variable is set, OR
  - /etc/os-release contains ID=hart-os (NixOS deployment)

Privileged ports (<1024) require root/systemd, which is correct for OS daemons.
This frees ports 1024-65535 for user applications.

Usage:
    from core.port_registry import get_port, get_all_ports, is_os_mode

    port = get_port('backend')        # 677 (OS mode) or 6777 (app mode)
    port = get_port('backend', 9999)  # Override with specific port
"""

import logging
import os
import socket

logger = logging.getLogger('hevolve.ports')

# ── Port Definitions ──────────────────────────────────────────

# App mode: user-space ports (coexist with other software)
APP_PORTS = {
    'backend':      6777,
    'discovery':    6780,
    'vision':       9891,
    'llm':          8080,
    'websocket':    5460,
    'diarization':  8004,
    'dlna_stream':  8554,
    'mesh_wg':      6795,
    'mesh_relay':   6796,
    'model_bus':    6790,
}

# OS mode: privileged ports (HART OS is the operating system)
OS_PORTS = {
    'backend':      677,
    'discovery':    678,
    'vision':       989,
    'llm':          808,
    'websocket':    546,
    'diarization':  800,
    'dlna_stream':  855,
    'mesh_wg':      679,
    'mesh_relay':   680,
    'model_bus':    681,
}

# Environment variable overrides (takes precedence over both modes)
ENV_OVERRIDES = {
    'backend':      'HARTOS_BACKEND_PORT',
    'discovery':    'HART_DISCOVERY_PORT',
    'vision':       'HART_VISION_PORT',
    'llm':          'HART_LLM_PORT',
    'websocket':    'HART_WS_PORT',
    'diarization':  'HEVOLVE_DIARIZATION_PORT',
    'dlna_stream':  'HART_DLNA_PORT',
    'mesh_wg':      'HART_MESH_WG_PORT',
    'mesh_relay':   'HART_MESH_RELAY_PORT',
    'model_bus':    'HART_MODEL_BUS_PORT',
}


# ── Detection ─────────────────────────────────────────────────

_os_mode_cached = None


def is_os_mode() -> bool:
    """Detect if running as HART OS (the operating system).

    True when:
      - HART_OS_MODE=true env var, OR
      - /etc/os-release contains ID=hart-os (NixOS deployment)
    """
    global _os_mode_cached
    if _os_mode_cached is not None:
        return _os_mode_cached

    # Explicit env var
    if os.environ.get('HART_OS_MODE', '').lower() in ('true', '1', 'yes'):
        _os_mode_cached = True
        return True

    # NixOS detection: check /etc/os-release
    try:
        with open('/etc/os-release', 'r') as f:
            for line in f:
                if line.strip().startswith('ID=') and 'hart-os' in line:
                    _os_mode_cached = True
                    return True
    except (FileNotFoundError, PermissionError):
        pass

    _os_mode_cached = False
    return False


# ── Port Resolution ───────────────────────────────────────────

def get_port(service: str, override: int = None) -> int:
    """Get the port for a HART OS service.

    Resolution order:
      1. Explicit override parameter
      2. Environment variable (HARTOS_BACKEND_PORT, etc.)
      3. OS-mode port (if running as HART OS)
      4. App-mode port (default)

    Args:
        service: Service name ('backend', 'discovery', 'vision', etc.)
        override: Explicit port override (highest priority).

    Returns:
        Port number.
    """
    # 1. Explicit override
    if override is not None:
        return override

    # 2. Environment variable
    env_var = ENV_OVERRIDES.get(service)
    if env_var:
        env_val = os.environ.get(env_var)
        if env_val:
            try:
                return int(env_val)
            except ValueError:
                logger.warning(f"Invalid port in {env_var}={env_val}, using default")

    # 3. OS mode vs App mode
    if is_os_mode():
        return OS_PORTS.get(service, APP_PORTS.get(service, 0))

    return APP_PORTS.get(service, 0)


def get_all_ports() -> dict:
    """Get all service ports as a dict."""
    return {service: get_port(service) for service in APP_PORTS}


def check_port_available(port: int, host: str = '0.0.0.0') -> bool:
    """Check if a port is available for binding.

    Args:
        port: Port number to check.
        host: Host to check on.

    Returns:
        True if port is available.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.bind((host, port))
        s.close()
        return True
    except OSError:
        return False


def get_mode_label() -> str:
    """Return 'OS' or 'APP' for display."""
    return 'OS' if is_os_mode() else 'APP'
