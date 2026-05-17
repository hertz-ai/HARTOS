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
        out['daemon_enabled'] = (
            os.environ.get('HEVOLVE_AGENT_ENGINE_ENABLED', 'false').lower() == 'true'
        )
        out['daemon_thread_alive'] = False
        out['daemon_probe_error'] = str(e)
    return out


def probe_llm() -> Dict[str, Any]:
    """Return live LLM server state via the canonical URL resolver.

    Uses ``core.port_registry.get_local_llm_url()`` which walks 7
    candidate sources (env vars, ``~/.nunba/llama_config.json``, the
    port-registry default) and ``_probe_llm_endpoint`` for the actual
    reachability check.  Always returns the URL we tried so debugging
    is one log line instead of "down" with no clue.
    """
    out: Dict[str, Any] = {}
    try:
        from core.port_registry import get_local_llm_url, _probe_llm_endpoint
        url = get_local_llm_url()
        out['url'] = url
        if _probe_llm_endpoint(url):
            out['status'] = 'up'
            # Best-effort model list — no failure if it 404s.
            try:
                from core.http_pool import pooled_get
                # get_local_llm_url returns ".../v1" suffix; models endpoint is /v1/models
                models_url = url.rstrip('/') + '/models'
                resp = pooled_get(models_url, timeout=2)
                if resp.status_code == 200:
                    data = resp.json()
                    out['models'] = [
                        m.get('id', 'unknown') for m in data.get('data', [])
                    ]
            except Exception:
                pass
        else:
            out['status'] = 'down'
    except Exception as e:
        out['status'] = 'probe_error'
        out['error'] = str(e)
    return out


def probe_nunba_flask() -> Dict[str, Any]:
    """Return Nunba Flask server state (the in-process app at :5000)."""
    out: Dict[str, Any] = {}
    try:
        from core.http_pool import pooled_get
        resp = pooled_get('http://localhost:5000/health', timeout=2)
        out['status'] = 'up' if resp.status_code == 200 else f'status_{resp.status_code}'
        out['code'] = resp.status_code
    except Exception as e:
        out['status'] = 'down'
        out['error'] = str(e)
    return out


def probe_langchain() -> Dict[str, Any]:
    """Return langchain GPT API sidecar state (port 6778)."""
    out: Dict[str, Any] = {}
    try:
        from core.http_pool import pooled_get
        resp = pooled_get('http://localhost:6778/health', timeout=2)
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
