"""Generic web page fetch — T3 (no auth, no browser).

Strategy ladder (each step is no-op if dependency missing):
  1. Jina Reader (https://r.jina.ai/<url>) — clean-text proxy, no JS execution
  2. requests.get with timeout — raw HTML
  3. degrade gracefully

No browser launch.  Caller's responsibility to enforce its own URL policy
(this script's allowlist is empty == caller-managed in domain_allowlist.py).
"""
import logging
from typing import Optional

logger = logging.getLogger('browser_research.scripts.web_generic')

CONNECTION_MECHANISM = 'public_http'

DEFAULT_TIMEOUT_S = 10.0
MAX_BYTES = 1_000_000  # 1 MB cap — protect from runaway pages
JINA_READER_BASE = 'https://r.jina.ai/'


def fetch(url: str, prefer_clean_text: bool = True, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Fetch a URL.  Returns dict with content + metadata.

    On failure returns {'success': False, ...} — never raises.
    """
    if not url or not (url.startswith('http://') or url.startswith('https://')):
        return {
            'success': False,
            'connection_mechanism': CONNECTION_MECHANISM,
            'error': f'url must start with http:// or https://: {url!r}',
        }

    try:
        import requests
    except ImportError:
        return {
            'success': False,
            'connection_mechanism': CONNECTION_MECHANISM,
            'error': 'requests not installed',
        }

    # Step 1: Jina Reader (LLM-friendly text)
    if prefer_clean_text:
        try:
            resp = requests.get(JINA_READER_BASE + url, timeout=timeout, stream=True)
            if resp.status_code == 200:
                body = _read_capped(resp, MAX_BYTES)
                return {
                    'success': True,
                    'connection_mechanism': CONNECTION_MECHANISM,
                    'tool': 'jina_reader',
                    'url': url,
                    'content_type': 'text/markdown',
                    'text': body,
                    'bytes': len(body.encode('utf-8', errors='ignore')),
                }
        except requests.RequestException as exc:
            logger.debug('Jina Reader failed, falling through: %s', exc)

    # Step 2: raw HTML
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            stream=True,
            headers={'User-Agent': 'Mozilla/5.0 (Nunba; +https://hevolve.ai)'},
        )
        body = _read_capped(resp, MAX_BYTES)
        return {
            'success': resp.status_code == 200,
            'connection_mechanism': CONNECTION_MECHANISM,
            'tool': 'requests_raw',
            'url': url,
            'status_code': resp.status_code,
            'content_type': resp.headers.get('Content-Type', ''),
            'text': body,
            'bytes': len(body.encode('utf-8', errors='ignore')),
        }
    except requests.RequestException as exc:
        return {
            'success': False,
            'connection_mechanism': CONNECTION_MECHANISM,
            'url': url,
            'error': f'{type(exc).__name__}: {exc}',
        }


def _read_capped(resp, max_bytes: int) -> str:
    """Read response body but stop at `max_bytes` to avoid OOM on huge pages."""
    buf = bytearray()
    for chunk in resp.iter_content(chunk_size=8192, decode_unicode=False):
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) >= max_bytes:
            break
    try:
        return buf.decode('utf-8', errors='replace')
    except Exception:
        return buf.decode('latin-1', errors='replace')
