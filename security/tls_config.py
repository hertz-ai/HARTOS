"""
TLS Configuration
Enforces HTTPS for all outbound HTTP calls and provides secure request sessions.
Defends against man-in-the-middle attacks on internal service communication.
"""

import os
import logging
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger('hevolve_security')

_LOCALHOST_HOSTS = frozenset(['localhost', '127.0.0.1', '::1', '0.0.0.0'])

# Singleton session
_secure_session = None


def get_secure_session() -> requests.Session:
    """
    Create or return a requests.Session with TLS verification enabled.
    Uses CA bundle from HEVOLVE_CA_BUNDLE env var if set.
    """
    global _secure_session
    if _secure_session is not None:
        return _secure_session

    session = requests.Session()

    ca_bundle = os.environ.get('HEVOLVE_CA_BUNDLE')
    if ca_bundle and os.path.exists(ca_bundle):
        session.verify = ca_bundle
    else:
        session.verify = True

    # Connection pooling with retry
    adapter = HTTPAdapter(
        pool_connections=20,
        pool_maxsize=20,
        max_retries=3,
    )
    session.mount('https://', adapter)
    session.mount('http://', adapter)

    _secure_session = session
    return session


def upgrade_url(url: str) -> str:
    """
    Upgrade http:// to https:// for non-localhost URLs in production.
    In development mode (HEVOLVE_ENV=development), allows HTTP.
    """
    if os.environ.get('HEVOLVE_ENV') == 'development':
        return url

    parsed = urlparse(url)
    if parsed.scheme == 'http' and parsed.hostname not in _LOCALHOST_HOSTS:
        upgraded = url.replace('http://', 'https://', 1)
        logger.debug(f"Upgraded URL to HTTPS: {parsed.hostname}")
        return upgraded
    return url


def secure_request(method: str, url: str, **kwargs) -> requests.Response:
    """
    Make an HTTP request through the secure session with URL upgrade.
    Drop-in replacement for requests.get/post/put/delete.
    """
    session = get_secure_session()
    safe_url = upgrade_url(url)

    # Set reasonable timeout if not provided
    if 'timeout' not in kwargs:
        kwargs['timeout'] = 30

    return session.request(method, safe_url, **kwargs)


def secure_get(url: str, **kwargs) -> requests.Response:
    """Secure replacement for requests.get()."""
    return secure_request('GET', url, **kwargs)


def secure_post(url: str, **kwargs) -> requests.Response:
    """Secure replacement for requests.post()."""
    return secure_request('POST', url, **kwargs)


def secure_put(url: str, **kwargs) -> requests.Response:
    """Secure replacement for requests.put()."""
    return secure_request('PUT', url, **kwargs)


def secure_delete(url: str, **kwargs) -> requests.Response:
    """Secure replacement for requests.delete()."""
    return secure_request('DELETE', url, **kwargs)
