"""
Connection-pooled HTTP session.

Replaces 81+ bare `requests.post()` / `requests.get()` calls across the codebase
with a shared session that reuses TCP connections via keep-alive.

Before: Each HTTP call opens a new TCP connection + TLS handshake.
After:  Connections are pooled and reused (10 pool connections, 20 max per host).

Typical improvement: 40-60% latency reduction on repeated calls to same host.
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
DEFAULT_TIMEOUT = (5, 30)


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

        # Configure retry strategy
        retry = Retry(
            total=3,
            backoff_factor=0.3,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        )

        # Connection pooling adapter
        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=retry,
        )

        session.mount('http://', adapter)
        session.mount('https://', adapter)

        # Default headers
        session.headers.update({
            'Content-Type': 'application/json',
        })

        _session = session
        logger.info("HTTP connection pool initialized (10 connections, 20 max per host)")
        return _session


def pooled_get(url: str, timeout=DEFAULT_TIMEOUT, **kwargs) -> requests.Response:
    """Connection-pooled GET request."""
    return get_http_session().get(url, timeout=timeout, **kwargs)


def pooled_post(url: str, timeout=DEFAULT_TIMEOUT, **kwargs) -> requests.Response:
    """Connection-pooled POST request."""
    return get_http_session().post(url, timeout=timeout, **kwargs)


def pooled_put(url: str, timeout=DEFAULT_TIMEOUT, **kwargs) -> requests.Response:
    """Connection-pooled PUT request."""
    return get_http_session().put(url, timeout=timeout, **kwargs)


def pooled_patch(url: str, timeout=DEFAULT_TIMEOUT, **kwargs) -> requests.Response:
    """Connection-pooled PATCH request."""
    return get_http_session().patch(url, timeout=timeout, **kwargs)


def pooled_request(method: str, url: str, timeout=DEFAULT_TIMEOUT, **kwargs) -> requests.Response:
    """Connection-pooled generic request."""
    return get_http_session().request(method, url, timeout=timeout, **kwargs)
