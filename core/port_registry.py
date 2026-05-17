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
_llm_url_cache_ts: float = 0.0
_LLM_URL_CACHE_TTL: float = 30.0   # seconds — re-resolve after this
_LLM_PROBE_TIMEOUT: float = 1.0    # seconds per candidate probe
_LLM_PROBE_NEG_TTL: float = 10.0   # seconds — cache "dead" verdicts
_llm_probe_cache: dict = {}         # url → (is_healthy, ts)
_llm_url_last_announced: str = ''  # for change-toast emission


def _probe_llm_endpoint(url: str) -> bool:
    """Cheap TCP-connect probe with short-lived result caching.

    True if something is listening on the URL's host:port.  No HTTP,
    no body — just confirms the port is open.  Result is cached for
    ``_LLM_PROBE_NEG_TTL`` seconds so repeated resolver calls don't
    re-probe dead candidates and burn 1s each time.

    Used by ``get_local_llm_url`` to walk candidate URLs and pick the
    first reachable one instead of returning the first non-empty config
    field and discovering it's dead at synth time.  Exception path
    returns False — we never raise from a probe.
    """
    import time
    cached = _llm_probe_cache.get(url)
    if cached is not None:
        ok, ts = cached
        if (time.time() - ts) < _LLM_PROBE_NEG_TTL:
            return ok
    try:
        import socket
        body = url.split('://', 1)[-1].split('/', 1)[0]
        host, _, port_s = body.partition(':')
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(_LLM_PROBE_TIMEOUT)
        try:
            ok = s.connect_ex(
                (host or '127.0.0.1', int(port_s or 0))) == 0
        finally:
            s.close()
    except Exception:
        ok = False
    _llm_probe_cache[url] = (ok, time.time())
    return ok


def _is_loopback_url(url: str) -> bool:
    """True iff the URL points at this machine.  Used to gate auto-
    correct of stale config fields — never rewrite a non-loopback URL
    (could be a real remote endpoint the user explicitly chose)."""
    try:
        body = url.split('://', 1)[-1].split('/', 1)[0]
        host = body.partition(':')[0].lower()
        return host in ('127.0.0.1', 'localhost', '0.0.0.0', '[::1]', '')
    except Exception:
        return False


def _autocorrect_stale_loopback_config(healthy_url: str) -> None:
    """Rewrite drifted loopback URLs in ``~/.nunba/llama_config.json``
    so non-resolver readers (chat path's direct read of
    ``external_llm_endpoint.base_url``) also see the live URL.

    Safety rails:
      * Only touches fields whose CURRENT value is loopback — a real
        remote endpoint the user typed (e.g. a cloud OpenAI-compat URL)
        is preserved verbatim.  The toast still fires; the user fixes
        externally.
      * Only writes if at least one field actually changed — no
        timestamp churn on the config file otherwise.
      * Never raises.

    This is the auto-correct the user asked for: when the resolver
    detects drift between two source-of-truth fields, the stale one
    gets healed in place so the next boot reads cleanly without an
    operator edit.
    """
    if not _is_loopback_url(healthy_url):
        return
    try:
        import json as _json
        cfg_path = os.path.join(
            os.path.expanduser('~'), '.nunba', 'llama_config.json')
        if not os.path.isfile(cfg_path):
            return
        with open(cfg_path) as _f:
            cfg = _json.load(_f)
        changed = False
        # external_llm_endpoint — only auto-fix if its current value is
        # loopback (i.e. it was *intended* to point at this machine).
        ext = cfg.get('external_llm_endpoint') or {}
        ext_base = ext.get('base_url') or ''
        if ext_base and _is_loopback_url(ext_base) and ext_base != healthy_url:
            ext['base_url'] = healthy_url
            ext['completions'] = (
                healthy_url.rstrip('/').removesuffix('/v1')
                + '/v1/chat/completions'
            )
            cfg['external_llm_endpoint'] = ext
            changed = True
        # custom_api_base — same shape, host-portion of healthy URL.
        cab = cfg.get('custom_api_base') or ''
        healthy_host = healthy_url.rstrip('/').removesuffix('/v1')
        if cab and _is_loopback_url(cab) and cab != healthy_host:
            cfg['custom_api_base'] = healthy_host
            changed = True
        if changed:
            with open(cfg_path, 'w') as _f:
                _json.dump(cfg, _f, indent=2)
            logger.info(
                "[LLM URL] Auto-corrected stale loopback config to %s",
                healthy_url)
    except Exception as e:
        logger.debug("[LLM URL] auto-correct skipped: %s", e)


def _emit_llm_url_change_toast(new_url: str) -> None:
    """Best-effort WAMP toast when the resolved LLM URL changes.

    Fires once per actual transition; subsequent resolver calls that
    return the same URL are silent.  Surfaces drift (config stale,
    llama-server moved port, external endpoint went down → fell back to
    bundled, etc.) to the user without log-spam.  Never raises.
    """
    global _llm_url_last_announced
    if new_url == _llm_url_last_announced:
        return
    _llm_url_last_announced = new_url
    try:
        from core.realtime import publish_async as _wamp_pub
        _wamp_pub(
            'com.hertzai.hevolve.llm.endpoint_changed',
            {'url': new_url, 'reason': 'resolver fall-through'},
            timeout=0.3,
        )
    except Exception:
        pass


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

    Resolution order — every non-empty candidate is PROBED, and the
    first reachable one wins.  Stale-but-configured URLs (e.g. wizard
    wrote 8080, llama-server later moved to 8082) no longer cause silent
    chat hangs.  Probe results are cached for a short TTL so the walk
    is cheap on the chat hot path.

    Candidate order:
      1. HEVOLVE_LOCAL_LLM_URL    — canonical env override
      2. CUSTOM_LLM_BASE_URL      — user-provided custom endpoint
      3. LLAMA_CPP_PORT           — deprecated port-only env var
      4. ~/.nunba/llama_config.json: external_llm_endpoint.base_url
                                  — wizard-recorded "external" (often
                                    actually a loopback, see drift bug
                                    2026-04-29)
      5. ~/.nunba/llama_config.json: server_port
                                  — Nunba's auto-managed bundled server
      6. ~/.nunba/llama_config.json: custom_api_base
                                  — wizard mirror of server_port
      7. port_registry default    — get_port('llm')

    On a successful resolve that DIFFERS from the previously-announced
    URL, the resolver:
      - Emits a WAMP toast so the user knows their LLM endpoint moved
      - Auto-corrects stale loopback fields in llama_config.json so
        non-resolver readers (chat path's raw external_llm_endpoint
        consumer) also see the live URL on next read
      - Updates HEVOLVE_LOCAL_LLM_URL env so other resolver-based
        callers in the same process see the fresh value immediately

    Cold-boot fallback: if no candidate is reachable (typical during
    the first ~30s of boot before llama-server has finished spawning),
    returns the highest-priority candidate URL anyway as a stable
    placeholder.  Callers handle the "configured but not yet listening"
    case via their existing connection-error paths; the placeholder is
    correct *and* the call site doesn't need to special-case None.

    Returns:
        Full URL string, e.g. 'http://127.0.0.1:8082/v1'
    """
    import time
    global _llm_url_cache, _llm_url_cache_ts

    now = time.time()
    if _llm_url_cache and (now - _llm_url_cache_ts) < _LLM_URL_CACHE_TTL:
        return _llm_url_cache

    # Build the ordered candidate list — same sources as before plus
    # the wizard-recorded external_llm_endpoint.base_url (the field that
    # caused the 2026-04-29 drift incident).  Empty strings are filtered
    # at the probe step.
    candidates: list = []

    candidates.append(os.environ.get('HEVOLVE_LOCAL_LLM_URL', ''))
    candidates.append(os.environ.get('CUSTOM_LLM_BASE_URL', ''))

    _port_env = os.environ.get('LLAMA_CPP_PORT', '')
    if _port_env:
        candidates.append(f'http://127.0.0.1:{_port_env}/v1')

    try:
        import json as _json
        _cfg_path = os.path.join(
            os.path.expanduser('~'), '.nunba', 'llama_config.json')
        if os.path.isfile(_cfg_path):
            with open(_cfg_path) as _f:
                _cfg = _json.load(_f)
            _ext = (_cfg.get('external_llm_endpoint') or {}).get('base_url') or ''
            if _ext:
                candidates.append(_ext)
            _port = _cfg.get('server_port')
            if _port:
                candidates.append(f'http://127.0.0.1:{_port}/v1')
            _cab = _cfg.get('custom_api_base') or ''
            if _cab:
                candidates.append(_cab)
    except Exception:
        pass

    candidates.append(f'http://127.0.0.1:{get_port("llm")}/v1')

    # Normalize, dedupe (preserving order), filter invalids.
    seen: set = set()
    normalized: list = []
    for raw in candidates:
        if not raw:
            continue
        u = raw.rstrip('/')
        if not u.endswith('/v1'):
            u += '/v1'
        if not _validate_llm_url(u):
            continue
        if u in seen:
            continue
        seen.add(u)
        normalized.append(u)

    if not normalized:
        # Should be unreachable — port_registry default is always present —
        # but stay defensive.
        normalized = [f'http://127.0.0.1:{get_port("llm")}/v1']

    # First reachable candidate wins.  If none are reachable (cold boot
    # before llama-server spawns), return the highest-priority candidate
    # anyway as a stable placeholder.
    chosen = None
    for url in normalized:
        if _probe_llm_endpoint(url):
            chosen = url
            break
    if chosen is None:
        chosen = normalized[0]

    _llm_url_cache = chosen
    _llm_url_cache_ts = now

    # On a real transition (different URL than previously announced),
    # heal the drift so non-resolver readers also pick up the live URL,
    # surface the change to the user, and update env so any caller in
    # this process that's still on `os.environ.get(HEVOLVE_LOCAL_LLM_URL)`
    # sees the same answer.
    if chosen != _llm_url_last_announced:
        _emit_llm_url_change_toast(chosen)
        _autocorrect_stale_loopback_config(chosen)
        try:
            os.environ['HEVOLVE_LOCAL_LLM_URL'] = chosen
        except Exception:
            pass

    return chosen


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
