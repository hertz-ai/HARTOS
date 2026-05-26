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
    'stt_stream':   8005,
    'dlna_stream':  8554,
    'mesh_wg':      6795,
    'mesh_relay':   6796,
    'model_bus':    6790,
    'mcp':          6791,
    'vlm_caption':  8081,
}

# OS mode: privileged ports (HART OS is the operating system)
OS_PORTS = {
    'backend':      677,
    'discovery':    678,
    'vision':       989,
    'llm':          808,
    'websocket':    546,
    'diarization':  800,
    'stt_stream':   801,
    'dlna_stream':  855,
    'mesh_wg':      679,
    'mesh_relay':   680,
    'model_bus':    681,
    'mcp':          682,
    'vlm_caption':  809,
}

# Environment variable overrides (takes precedence over both modes)
ENV_OVERRIDES = {
    'backend':      'HARTOS_BACKEND_PORT',
    'discovery':    'HART_DISCOVERY_PORT',
    'vision':       'HART_VISION_PORT',
    'llm':          'HART_LLM_PORT',
    'websocket':    'HART_WS_PORT',
    'diarization':  'HEVOLVE_DIARIZATION_PORT',
    'stt_stream':   'HART_STT_STREAM_PORT',
    'dlna_stream':  'HART_DLNA_PORT',
    'mesh_wg':      'HART_MESH_WG_PORT',
    'mesh_relay':   'HART_MESH_RELAY_PORT',
    'model_bus':    'HART_MODEL_BUS_PORT',
    'mcp':          'HART_MCP_PORT',
    'vlm_caption':  'HEVOLVE_VLM_CAPTION_PORT',
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
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def get_mode_label() -> str:
    """Return 'OS' or 'APP' for display."""
    return 'OS' if is_os_mode() else 'APP'


# ── LLM URL Resolution ──────────────────────────────────────

_llm_url_cache: str = ''


def get_local_draft_url() -> str:
    """Single source of truth for the local DRAFT LLM endpoint URL.

    The draft model is the Qwen3.5-0.8B instance that answers
    dispatch_draft_first calls and generates continuous video captions.

    On ≥8GB VRAM, draft runs on a SEPARATE port (8081) from the main
    model (8080) so both stay resident simultaneously.

    On ≤6GB VRAM (no separate draft server), the draft URL points to
    the MAIN model's port so the same model serves both roles — draft
    classification AND agentic responses. This avoids the "draft offline
    → fall through → slow main" latency penalty by letting the speculative
    dispatcher talk to whatever model IS running.

    Resolution order:
      1. HEVOLVE_DRAFT_LLM_URL    — full URL override
      2. HEVOLVE_VLM_CAPTION_PORT — port override (separate draft server)
      3. If draft server is running on default port → use it
      4. Otherwise → fall back to main LLM URL (same model, dual role)

    Returns full URL with /v1 suffix (OpenAI-compatible).
    """
    url = os.environ.get('HEVOLVE_DRAFT_LLM_URL', '').strip()
    if not url:
        port = os.environ.get('HEVOLVE_VLM_CAPTION_PORT', '').strip()
        if not port:
            port = str(get_port('vlm_caption'))
        url = f'http://127.0.0.1:{port}/v1'

    url = url.rstrip('/')
    if not url.endswith('/v1'):
        url += '/v1'

    # If draft port has no server, use the main LLM instead (single-model mode).
    # This makes the main model serve BOTH draft and agentic roles on low VRAM.
    try:
        import socket
        _body = url.split('://', 1)[-1].split('/', 1)[0]
        host, _, port_s = _body.partition(':')
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        result = s.connect_ex((host or '127.0.0.1', int(port_s or 8081)))
        s.close()
        if result != 0:
            # Draft port not listening → use main model as draft
            return get_local_llm_url()
    except Exception:
        pass

    return url


def get_local_llm_url() -> str:
    """Single source of truth for the local LLM endpoint URL.

    Resolution order (first non-empty wins):
      1. HEVOLVE_LOCAL_LLM_URL  — canonical, full URL (set by Nunba/HARTOS)
      2. CUSTOM_LLM_BASE_URL    — user-provided custom endpoint (backwards compat)
      3. LLAMA_CPP_PORT          — deprecated port-only var (backwards compat)
      4. port_registry default   — construct from get_port('llm')

    The result always includes /v1 suffix for OpenAI-compatible endpoints.
    Caches the resolved URL; call invalidate_llm_url() to clear after
    port changes (warm start, conflict reassignment).

    Returns:
        Full URL string, e.g. 'http://127.0.0.1:8081/v1'
    """
    global _llm_url_cache
    if _llm_url_cache:
        return _llm_url_cache

    url = ''

    # 1. Canonical env var
    url = os.environ.get('HEVOLVE_LOCAL_LLM_URL', '')

    # 2. Custom LLM base URL (user-provided via wizard)
    if not url:
        url = os.environ.get('CUSTOM_LLM_BASE_URL', '')

    # 3. Deprecated: reconstruct from port-only var
    if not url:
        port = os.environ.get('LLAMA_CPP_PORT', '')
        if port:
            url = f'http://127.0.0.1:{port}/v1'

    # 4. Read from Nunba's llama_config.json (wizard-configured port)
    if not url:
        try:
            import json as _json
            _cfg_path = os.path.join(os.path.expanduser('~'), '.nunba', 'llama_config.json')
            if os.path.isfile(_cfg_path):
                with open(_cfg_path) as _f:
                    _cfg = _json.load(_f)
                _port = _cfg.get('server_port')
                if _port:
                    url = f'http://127.0.0.1:{_port}/v1'
        except Exception:
            pass

    # 5. Fallback: port registry default
    if not url:
        url = f'http://127.0.0.1:{get_port("llm")}/v1'

    # Normalize: ensure /v1 suffix
    url = url.rstrip('/')
    if not url.endswith('/v1'):
        url += '/v1'

    # Validate URL format
    if not _validate_llm_url(url):
        logger.warning(f"Invalid LLM URL '{url}', falling back to port registry default")
        url = f'http://127.0.0.1:{get_port("llm")}/v1'

    _llm_url_cache = url
    return url


def set_local_llm_url(url: str) -> None:
    """Set the local LLM URL and propagate to env.

    Called by Nunba when:
    - start_server() detects/starts a server on a port
    - Port conflict causes reassignment to a new port
    - User provides a custom endpoint via the wizard

    Validates the URL, sets HEVOLVE_LOCAL_LLM_URL, and invalidates cache.
    """
    url = url.rstrip('/')
    if not url.endswith('/v1'):
        url += '/v1'

    if not _validate_llm_url(url):
        logger.error(f"Refusing to set invalid LLM URL: {url}")
        return

    os.environ['HEVOLVE_LOCAL_LLM_URL'] = url
    invalidate_llm_url()
    logger.info(f"LLM URL set: {url}")


def invalidate_llm_url() -> None:
    """Clear the cached LLM URL. Call after port changes."""
    global _llm_url_cache
    _llm_url_cache = ''


def is_local_llm() -> bool:
    """Check if the configured LLM is a local endpoint (zero cost).

    Returns True if the resolved URL points to localhost/127.0.0.1,
    or if a local LLM model name is configured.
    """
    if os.environ.get('HEVOLVE_LOCAL_LLM_MODEL'):
        return True
    url = get_local_llm_url()
    return any(h in url for h in ('localhost', '127.0.0.1', '0.0.0.0', '[::1]'))


def _validate_llm_url(url: str) -> bool:
    """Validate that a URL is well-formed for an LLM endpoint.

    Checks: has scheme (http/https), has host, port is numeric if present.
    Does NOT check connectivity — that's a runtime concern.
    """
    if not url:
        return False
    if not url.startswith(('http://', 'https://')):
        return False
    # Extract host:port portion
    try:
        after_scheme = url.split('://', 1)[1]
        host_port = after_scheme.split('/')[0]
        if ':' in host_port:
            host, port_str = host_port.rsplit(':', 1)
            if not host or not port_str.isdigit():
                return False
            port = int(port_str)
            if port < 1 or port > 65535:
                return False
        elif not host_port:
            return False
    except (IndexError, ValueError):
        return False
    return True
