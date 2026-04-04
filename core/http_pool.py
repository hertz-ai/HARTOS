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
