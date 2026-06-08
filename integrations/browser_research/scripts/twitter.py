"""Twitter / X — T2 platform script (read).

URL construction + cookie lookup + delegation to web_crawler.

DOM parsing is intentionally MINIMAL — Twitter's frontend ships markup that
churns weekly, so we return the raw crawled markdown alongside the URL and
let the calling LLM extract structure from text.  When the agent needs
strongly-structured output, it can iterate (re-query for screenshot OCR,
use a more specific URL, etc.).

Connection mechanism: inherited from `_base.fetch_with_session`.
Domain-locked to x.com / twitter.com by domain_allowlist.
"""
from urllib.parse import quote_plus
from typing import Optional

from ._base import fetch_with_session

PLATFORM = 'twitter'
BASE = 'https://x.com'


def search(query: str, handle: Optional[str] = None, limit: int = 20) -> dict:
    """Search Twitter via the public web UI with the user's session cookies."""
    url = f"{BASE}/search?q={quote_plus(query)}&src=typed_query&f=live"
    result = fetch_with_session(url, PLATFORM, handle=handle)
    result['action'] = 'search'
    result['query'] = query
    # Soft cap output for LLM context — Twitter's search result page is huge.
    if result.get('markdown'):
        result['markdown'] = result['markdown'][:12000]
    return result


def timeline(target_handle: str, viewer_handle: Optional[str] = None) -> dict:
    """Read another user's public timeline using `viewer_handle`'s session."""
    h = target_handle.lstrip('@')
    url = f"{BASE}/{quote_plus(h)}"
    result = fetch_with_session(url, PLATFORM, handle=viewer_handle)
    result['action'] = 'timeline'
    result['target_handle'] = target_handle
    if result.get('markdown'):
        result['markdown'] = result['markdown'][:12000]
    return result
