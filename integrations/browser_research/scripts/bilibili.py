"""哔哩哔哩 (Bilibili) — T2 platform script (read).

No Nunba T1 adapter exists for Bilibili — this is greenfield.  Cookie-based
because the search API is behind a session check; public anonymous access
returns rate-limited stubs.

Domain-locked to bilibili.com.
"""
from urllib.parse import quote_plus
from typing import Optional

from ._base import fetch_with_session, post_preview

PLATFORM = 'bilibili'
BASE = 'https://search.bilibili.com'


def search(query: str, handle: Optional[str] = None, limit: int = 25) -> dict:
    url = f"{BASE}/all?keyword={quote_plus(query)}"
    result = fetch_with_session(url, PLATFORM, handle=handle)
    result['action'] = 'search'
    result['query'] = query
    if result.get('markdown'):
        result['markdown'] = result['markdown'][:14000]
    return result


def timeline(target_handle: str, viewer_handle: Optional[str] = None) -> dict:
    """Read a UP主 (uploader) page by uid or username."""
    h = target_handle.lstrip('@')
    url = f"https://space.bilibili.com/{quote_plus(h)}"
    result = fetch_with_session(url, PLATFORM, handle=viewer_handle)
    result['action'] = 'timeline'
    result['target_handle'] = target_handle
    if result.get('markdown'):
        result['markdown'] = result['markdown'][:14000]
    return result


def post(content, handle=None, dry_run=True):
    return post_preview(PLATFORM, content, handle=handle, dry_run=dry_run)

