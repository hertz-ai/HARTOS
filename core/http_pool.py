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

# urllib3.connectionpool emits a WARNING per retry attempt
# ("Retrying ... after connection broken by 'NameResolutionError'")
# which floods the log when central.hevolve.ai is unreachable
# (offline laptop, DNS down, captive portal).  Real connection
# failures still surface via caller try/except handlers (see
# core.superadmin_report._post_report's logger.debug, peer_discovery's
# PeerBackoff exponential backoff, etc.) - urllib3's per-retry
# WARNING is pure noise on top of those.
#
# Downgrading to ERROR keeps genuine connection-pool issues visible
# while silencing the per-retry retry chatter.
logging.getLogger('urllib3.connectionpool').setLevel(logging.ERROR)

# autobahn.asyncio.component emits a WARNING per connect-retry when a
# Crossbar WAMP router isn't running ("Connection failed with OS
# error: ConnectionRefusedError" + "trying transport 0 ws://localhost
# :8088/ws using connect delay 300").  The component's own retry loop
# already implements exponential backoff up to 300s; the per-attempt
# WARNING is informational noise on flat-mode installs that don't run
# Crossbar.  Real session failures still surface via core/platform/
# events.py:_run's "WAMP component exited: %s" warning, which carries
# context (which url, which realm) the autobahn line lacks.  Bumping
# the autobahn logger to ERROR keeps genuine connect-attempt errors
# (e.g. ssl handshake failure on a real Crossbar URL) visible.
logging.getLogger('autobahn.asyncio.component').setLevel(logging.ERROR)
# autobahn.wamp.component (parent module) ALSO emits the connect-error
# traceback at handle_connect_error - separate logger from the
# .asyncio.component child.  Plus txaio.aio formats the traceback at
# its own level.  Without silencing both, the traceback still surfaces
# even when .asyncio.component is at ERROR (the traceback reaches the
# root logger via stderr emit).  Bumping all three keeps the log clean
# on flat installs without Crossbar.
logging.getLogger('autobahn.wamp.component').setLevel(logging.ERROR)
logging.getLogger('autobahn').setLevel(logging.ERROR)
logging.getLogger('txaio').setLevel(logging.ERROR)
# Same logic for asyncio's "socket.send() raised exception" pairs that
# fire alongside the failed Crossbar connect attempts - the underlying
# transport-failure event is already covered by the autobahn warning
# we just silenced; asyncio's transport-level chatter is redundant.
logging.getLogger('asyncio').setLevel(logging.ERROR)

_session = None
_session_lock = threading.Lock()

# Default timeout for all requests (connect, read) in seconds
DEFAULT_TIMEOUT = (3, 15)

# Shared httpx.Client for the autogen/openai LLM path.  Separate from the
# requests Session above (openai uses httpx, not requests).
_llm_httpx_client = None
_llm_httpx_lock = threading.Lock()


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
        _llm_httpx_client = httpx.Client(
            timeout=httpx.Timeout(600.0, connect=10.0))
        logger.info(
            "Shared LLM httpx.Client initialized — SSL context built once, "
            "reused across all autogen client rebuilds")
        return _llm_httpx_client


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
