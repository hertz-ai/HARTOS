"""Canonical runtime-state probes.

Single source of truth for "is the daemon actually running?", "is the
LLM server actually reachable?", "is Flask up?".  Replaces the
duplicated, drift-prone probes that previously lived inline in BOTH
`integrations/mcp/mcp_server.py` and
`integrations/mcp/mcp_http_bridge.py`.

Why this module exists (root-cause notes from 2026-05-01 incident):

1. The old `daemon_enabled` probe read
   ``os.environ.get('HEVOLVE_AGENT_ENGINE_ENABLED', 'false')`` — a
   *config snapshot*, not the actual thread state.  It returned
   ``'false'`` even when the daemon thread was alive, because the
   env-var auto-setter at ``integrations/social/__init__.py:348``
   only runs `if env is None`, leaving any other unset/empty value
   to default to `'false'`.  Probes must read the actual
   ``agent_daemon._running`` singleton state.

2. The old `llm_server` probe hit
   ``http://localhost:{get_port('llm')}/health`` (default 8080).  On
   installs where llama-server binds to a non-default port (set via
   ``HEVOLVE_LOCAL_LLM_URL``, ``LLAMA_CPP_PORT``, or written into
   ``~/.nunba/llama_config.json:server_port``), this hardcoded URL
   misses entirely.  The canonical resolver
   ``core.port_registry.get_local_llm_url()`` already walks 7
   candidate sources and probes each — both MCP probes must route
   through it instead of duplicating a worse version of the same
   logic.

Public API: each `probe_*` function returns a plain dict that the
MCP tools serialize to JSON.  Side-effect free, fast (≤200 ms total
on a healthy host).

Per CLAUDE.md DRY gate — no parallel implementations of these probes
are allowed elsewhere.  If you find yourself writing
``os.environ.get('HEVOLVE_AGENT_ENGINE_ENABLED', ...)`` to figure out
"is the daemon on?", you are in the wrong place; call
``probe_agent_daemon()`` instead.
"""
from __future__ import annotations
import os
from typing import Dict, Any


def probe_agent_daemon() -> Dict[str, Any]:
    """Return the actual agent daemon thread state plus config.

    Reads ``agent_daemon._running`` and ``agent_daemon._thread`` —
    NOT the ``HEVOLVE_AGENT_ENGINE_ENABLED`` env var (which is the
    pre-boot intent, not the live state).  Falls back to env var if
    the daemon module cannot be imported (extreme degraded boot).
    """
    out: Dict[str, Any] = {
        'poll_interval': int(os.environ.get('HEVOLVE_AGENT_POLL_INTERVAL', '30')),
        'max_concurrent': int(os.environ.get('HEVOLVE_AGENT_MAX_CONCURRENT', '10')),
        'speculative_enabled': (
            os.environ.get('HEVOLVE_SPECULATIVE_ENABLED', 'false').lower() == 'true'
        ),
    }
    try:
        from integrations.agent_engine.agent_daemon import agent_daemon
        out['daemon_enabled'] = bool(agent_daemon._running)
        out['daemon_thread_alive'] = bool(
            agent_daemon._thread and agent_daemon._thread.is_alive()
        )
        out['daemon_tick_count'] = int(getattr(agent_daemon, '_tick_count', 0))
    except Exception as e:
        # Degraded fallback — couldn't reach the daemon module at all.
        # Default ON to match integrations.agent_engine.__init__ default.
        out['daemon_enabled'] = (
            os.environ.get('HEVOLVE_AGENT_ENGINE_ENABLED', 'true').lower() != 'false'
        )
        out['daemon_thread_alive'] = False
        out['daemon_probe_error'] = str(e)
    return out


def probe_llm(include_models: bool = False) -> Dict[str, Any]:
    """Return live LLM server state via an HTTP-fidelity probe.

    Issues an actual HTTP GET to ``<url>/models`` and checks for a 200
    response.  Distinct from the TCP-only ``_probe_llm_endpoint`` in
    ``core.port_registry`` which is the cheap candidate-filter for
    ``get_local_llm_url`` — that one stays TCP-only on purpose
    (sub-1ms per candidate).  This probe upgrades to HTTP fidelity
    so a half-loaded llama-server (port bound but model not ready)
    is correctly reported as ``down`` (#459).

    SRP (#458): the default response is a single HTTP request — no
    second-call side effect.  Pass ``include_models=True`` when you
    actually need the model-list payload; otherwise the response body
    is discarded.

    Always returns the URL we tried so debugging is one log line
    instead of "down" with no clue.
    """
    out: Dict[str, Any] = {}
    try:
        from core.port_registry import get_local_llm_url
        url = get_local_llm_url()
        out['url'] = url
    except Exception as e:
        out['status'] = 'probe_error'
        out['error'] = str(e)
        return out
    try:
        from core.http_pool import pooled_get
        # ``get_local_llm_url`` returns the ".../v1" suffix so /models
        # is the OpenAI-compatible models endpoint.  A 200 here proves
        # the LLM is actually serving — port-bound-but-stuck processes
        # return 5xx / connection-error / timeout.
        models_url = url.rstrip('/') + '/models'
        resp = pooled_get(models_url, timeout=2)
        if resp.status_code == 200:
            out['status'] = 'up'
            if include_models:
                try:
                    data = resp.json()
                    out['models'] = [
                        m.get('id', 'unknown')
                        for m in data.get('data', [])
                    ]
                except Exception:
                    pass
        else:
            out['status'] = 'down'
            out['code'] = resp.status_code
    except Exception as e:
        out['status'] = 'down'
        out['error'] = str(e)
    return out


def probe_nunba_flask() -> Dict[str, Any]:
    """Return Nunba Flask server state.

    Resolves the port via the canonical ``core.port_registry.get_port
    ('flask')`` resolver instead of the previously-hardcoded :5000
    literal (#460) — env override ``HART_FLASK_PORT`` is honored
    automatically.
    """
    out: Dict[str, Any] = {}
    try:
        from core.port_registry import get_port
        port = get_port('flask')
        out['port'] = port
    except Exception as e:
        out['status'] = 'probe_error'
        out['error'] = str(e)
        return out
    try:
        from core.http_pool import pooled_get
        resp = pooled_get(f'http://localhost:{port}/health', timeout=2)
        out['status'] = ('up' if resp.status_code == 200
                         else f'status_{resp.status_code}')
        out['code'] = resp.status_code
    except Exception as e:
        out['status'] = 'down'
        out['error'] = str(e)
    return out


def probe_langchain() -> Dict[str, Any]:
    """Return langchain state for the topology we are actually in.

    Resolves the port via the canonical ``core.port_registry.get_port
    ('langchain')`` resolver instead of the previously-hardcoded :6778
    literal (#460) — env override ``HART_LANGCHAIN_PORT`` is honored
    automatically.

    #460 (second half): langchain is only a SIDECAR in standalone/Docker
    mode.  In bundled mode Nunba imports it in-process
    (``hart_intelligence_entry``) and exposes :5000 only, so nothing
    listens on a langchain port at all.  Dialing one there asks a
    question with no correct answer: the connect is always refused, so
    the probe reported ``down`` about a perfectly healthy engine.

    The bundled branch therefore reads the canonical in-process
    readiness signal instead of a socket, and — importantly — does NOT
    collapse "I can't see it from here" into ``down``.  A ``sys.modules``
    read only describes THIS process; the stdio MCP server runs in a
    separate one where the answer would be a fresh-zero false negative.
    That is the same shadow-module trap that made a 14h agent-engine
    outage undiagnosable, so the unknown case says ``unknown`` and names
    why.
    """
    out: Dict[str, Any] = {}
    try:
        from core.config_cache import is_bundled
        bundled = is_bundled()
    except Exception:
        bundled = False          # unknowable -> treat as sidecar, dial as before
    if bundled:
        out['mode'] = 'in_process'
        try:
            from core.safe_hartos_attr import hartos_loaded
            loaded = hartos_loaded()
        except Exception as e:
            out['status'] = 'probe_error'
            out['error'] = str(e)
            return out
        if loaded:
            out['status'] = 'up'
            out['source'] = 'sys.modules'
        else:
            out['status'] = 'unknown'
            out['reason'] = ('bundled mode: langchain is in-process and has '
                             'no port; it is not loaded in THIS process, and '
                             'only the hosting process can answer. NOT down.')
        return out

    out['mode'] = 'sidecar'
    try:
        from core.port_registry import get_port
        port = get_port('langchain')
        out['port'] = port
    except Exception as e:
        out['status'] = 'probe_error'
        out['error'] = str(e)
        return out
    try:
        from core.http_pool import pooled_get
        resp = pooled_get(f'http://localhost:{port}/health', timeout=2)
        out['status'] = 'up' if resp.status_code == 200 else 'error'
        out['code'] = resp.status_code
    except Exception:
        out['status'] = 'down'
    return out


__all__ = [
    'probe_agent_daemon',
    'probe_llm',
    'probe_nunba_flask',
    'probe_langchain',
]
