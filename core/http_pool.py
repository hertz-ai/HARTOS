"""
Connection-pooled HTTP session.

Replaces 81+ bare `requests.post()` / `requests.get()` calls across the codebase
with a shared session that reuses TCP connections via keep-alive.

Before: Each HTTP call opens a new TCP connection + TLS handshake.
After:  Connections are pooled and reused (10 pool connections, 20 max per host).

Typical improvement: 40-60% latency reduction on repeated calls to same host.

Retry policy:
  - localhost: 0 retries (dead local services should fail instantly, not block 15s)
  - remote:    2 retries with 0.5s backoff (network can be flaky)
"""

import logging
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger('hevolve_core')

_session = None
_session_lock = threading.Lock()

# Default timeout for all requests (connect, read) in seconds
DEFAULT_TIMEOUT = (3, 15)

# Shared httpx.Client for the autogen/openai LLM path.  Separate from the
# requests Session above (openai uses httpx, not requests).
_llm_httpx_client = None
_llm_httpx_lock = threading.Lock()


def _shared_llm_http_client_class():
    """Build the shared-client class lazily (httpx imported on first use).

    autogen/openai DEEPCOPIES the llm_config — ``ConversableAgent`` copies its
    ``config_list`` per agent, and the REUSE path copies the config when
    rebuilding trained agents.  A plain ``httpx.Client`` is not deep-copyable, so
    once we put the shared client into the config (the GIL fix below) every such
    copy raised:

        Some ERROR IN REUSE RECIPE Please implement __deepcopy__ method for each
        value class in llm_config to support deepcopy

    which crashed REUSE → fell back to (expensive) CREATE → defeated the
    flywheel.  The client is a PROCESS-WIDE SINGLETON (one SSL context + one
    connection pool — the whole point of the fix, and httpx.Client is documented
    thread-safe), so the correct copy semantics are to return the SAME instance,
    never duplicate the pool.  This is the autogen-recommended remedy named in
    the error message itself.
    """
    import httpx

    class _SharedLLMHttpClient(httpx.Client):
        def __deepcopy__(self, memo):
            return self

        def __copy__(self):
            return self

    return _SharedLLMHttpClient


def get_llm_http_client():
    """Return a process-wide shared ``httpx.Client`` for the autogen/openai LLM
    path.  Thread-safe singleton; never closed (lives for the process).

    WHY THIS EXISTS (py-spy diagnosis 2026-06-01): autogen rebuilds the OpenAI
    client on EVERY ``register_for_llm`` (i.e. once per tool registration, via
    ``ConversableAgent.update_tool_signature``).  Each fresh ``openai.OpenAI`` →
    ``httpx.Client`` calls ``httpx.create_ssl_context`` → ``ssl.create_default_
    context(cafile=certifi.where())``, which re-reads + re-parses the entire
    system CA bundle (~150 root certs).  With ~40 core tools per agent and the
    flywheel/coding agents spinning up agents continuously, that CA-bundle reload
    storm was the #1 GIL hog — 56% of GIL-active samples, main process pinned at
    >1 core, and a bare ``hi`` taking ~2m27s before the chat thread could win the
    GIL.  (The LLM endpoint is the LOCAL llama-server over plain HTTP, so the TLS
    setup is pure waste.)

    Passing this ONE client as ``http_client`` in the autogen config_list (see
    ``core.autogen_config.get_autogen_config_list``) makes openai reuse it, so
    the SSL context is built exactly ONCE for the whole process.  ``httpx.Client``
    is safe for concurrent use across the agent worker threads, and a shared
    transport works across different ``base_url``s (openai applies base_url at
    request-build time, not on the client).
    """
    global _llm_httpx_client
    if _llm_httpx_client is not None:
        return _llm_httpx_client
    with _llm_httpx_lock:
        if _llm_httpx_client is not None:
            return _llm_httpx_client
        import httpx
        # Generous transport timeout: openai sets its own per-request timeout on
        # top of this, so it is only a fallback — it must never be tighter than a
        # long local generation, or it would cut streams off.
        # Deepcopy-safe subclass so the config it lives in stays copyable (see
        # _shared_llm_http_client_class for why REUSE deepcopies the config).
        _llm_httpx_client = _shared_llm_http_client_class()(
            timeout=httpx.Timeout(600.0, connect=10.0))
        logger.info(
            "Shared LLM httpx.Client initialized — SSL context built once, "
            "reused across all autogen client rebuilds")
        return _llm_httpx_client


# ── Background-only LLM client (autonomous daemon calls, request_id 'daemon_*')
# ──────────────────────────────────────────────────────────────────────────
# A SEPARATE httpx.Client from the foreground shared client above, used only for
# autonomous background-agent LLM calls.  Its whole reason to exist: it can be
# closed mid-flight — dropping every in-flight background connection — the
# instant a user chat arrives, so llama-server sees the client disconnect, aborts
# those generations, and frees the single local model slot for the user.  The
# foreground shared client is NEVER touched, so a live user turn is byte-for-byte
# unchanged (the GIL fix above stays intact).
#
# verify=False: the only endpoint is the local plain-HTTP llama-server, so there
# is no TLS — and it also skips ssl.create_default_context's CA-bundle parse, the
# very GIL hog get_llm_http_client exists to avoid.  Built lazily, rebuilt fresh
# after each close (the next background call gets a usable client again).
_bg_llm_httpx_client = None
_bg_llm_httpx_lock = threading.Lock()
_bg_cancel_registered = False


def get_bg_llm_http_client():
    """Return the process-wide background-only ``httpx.Client`` for autonomous
    daemon LLM calls.  Closable mid-flight via ``close_bg_llm_http_client`` (wired
    to ``core.foreground``'s cancel registry, fired when a user chat starts).
    Thread-safe; rebuilds a fresh client if the previous one was closed."""
    global _bg_llm_httpx_client, _bg_cancel_registered
    with _bg_llm_httpx_lock:
        if not _bg_cancel_registered:
            # Register the closer ONCE so the 0->1 foreground edge aborts any
            # in-flight background call.  Idempotent + harmless if no client is
            # open, so it never needs unregistering.
            try:
                from core.foreground import register_cancellable
                register_cancellable(close_bg_llm_http_client)
                _bg_cancel_registered = True
            except Exception:
                pass
        if _bg_llm_httpx_client is not None and not _bg_llm_httpx_client.is_closed:
            return _bg_llm_httpx_client
        import httpx
        _bg_llm_httpx_client = _shared_llm_http_client_class()(
            timeout=httpx.Timeout(600.0, connect=10.0), verify=False)
        logger.info("Background LLM httpx.Client initialized (closable for "
                    "foreground preemption)")
        return _bg_llm_httpx_client


def close_bg_llm_http_client() -> None:
    """Close the background LLM client, dropping all its in-flight connections so
    llama-server aborts those generations and frees the slot for the user.

    Idempotent + best-effort: a closed/absent client is a no-op.  Clears the
    singleton so the next background call rebuilds a fresh client (which only
    happens AFTER the foreground turn clears — see the wrapper's yield gate)."""
    global _bg_llm_httpx_client
    with _bg_llm_httpx_lock:
        c = _bg_llm_httpx_client
        _bg_llm_httpx_client = None
    if c is not None:
        try:
            c.close()
        except Exception:
            pass


def get_http_session() -> requests.Session:
    """
    Get or create a connection-pooled requests.Session.
    Thread-safe singleton.
    """
    global _session
    if _session is not None:
        return _session

    with _session_lock:
        if _session is not None:
            return _session

        session = requests.Session()

        # Localhost: zero retries — dead local services should fail instantly.
        # This prevents the retry storm (36 failed TCP connects/min) that kills
        # the system when optional sidecars (MiniCPM:9891, etc.) aren't running.
        local_adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=Retry(total=0),
        )

        # Remote: modest retries with backoff (network can be flaky)
        remote_retry = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=[502, 503, 504],
            allowed_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        )
        remote_adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=remote_retry,
        )

        session.mount('http://localhost', local_adapter)
        session.mount('http://127.0.0.1', local_adapter)
        session.mount('http://', remote_adapter)
        session.mount('https://', remote_adapter)

        # Default headers
        session.headers.update({
            'Content-Type': 'application/json',
        })

        _session = session
        logger.info("HTTP pool initialized (localhost=0 retries, remote=2 retries)")
        return _session


def pooled_get(url: str, timeout=DEFAULT_TIMEOUT, **kwargs) -> requests.Response:
    """Connection-pooled GET request."""
    return get_http_session().get(url, timeout=timeout, **kwargs)


def pooled_post(url: str, timeout=DEFAULT_TIMEOUT, **kwargs) -> requests.Response:
    """Connection-pooled POST request."""
    resp = get_http_session().post(url, timeout=timeout, **kwargs)
    # Log LLM input/output for observability
    if '/chat/completions' in url:
        try:
            import json as _json
            body = kwargs.get('json', {})
            msgs = body.get('messages', [])
            prompt_preview = msgs[-1].get('content', '')[:200] if msgs else ''
            rj = resp.json()
            content = rj.get('choices', [{}])[0].get('message', {}).get('content', '')
            reasoning = rj.get('choices', [{}])[0].get('message', {}).get('reasoning_content', '')
            usage = rj.get('usage', {})
            logger.info(
                f"[LLM] IN: {prompt_preview}... | "
                f"OUT({usage.get('completion_tokens',0)}tok): {content[:200]}... | "
                f"THINK: {len(reasoning)}chars")
        except Exception:
            pass
    return resp


def pooled_put(url: str, timeout=DEFAULT_TIMEOUT, **kwargs) -> requests.Response:
    """Connection-pooled PUT request."""
    return get_http_session().put(url, timeout=timeout, **kwargs)


def pooled_patch(url: str, timeout=DEFAULT_TIMEOUT, **kwargs) -> requests.Response:
    """Connection-pooled PATCH request."""
    return get_http_session().patch(url, timeout=timeout, **kwargs)


def pooled_delete(url: str, timeout=DEFAULT_TIMEOUT, **kwargs) -> requests.Response:
    """Connection-pooled DELETE request."""
    return get_http_session().delete(url, timeout=timeout, **kwargs)


def pooled_request(method: str, url: str, timeout=DEFAULT_TIMEOUT, **kwargs) -> requests.Response:
    """Connection-pooled generic request."""
    return get_http_session().request(method, url, timeout=timeout, **kwargs)
