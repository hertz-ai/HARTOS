"""小红书 (Xiaohongshu / RED) — T2 platform script (read).

Search results page is the only practical entry — direct note URLs are
hashed and short-lived.  Domain-locked to xiaohongshu.com / xhslink.com.
"""
from urllib.parse import quote_plus
from typing import Optional

from ._base import fetch_with_session

PLATFORM = 'xiaohongshu'
BASE = 'https://www.xiaohongshu.com'


def search(query: str, handle: Optional[str] = None, limit: int = 25) -> dict:
    url = f"{BASE}/search_result?keyword={quote_plus(query)}"
    result = fetch_with_session(url, PLATFORM, handle=handle)
    result['action'] = 'search'
    result['query'] = query
    if result.get('markdown'):
        result['markdown'] = result['markdown'][:14000]
    return result


def timeline(target_handle: str, viewer_handle: Optional[str] = None) -> dict:
    """Read a user's profile page by uid."""
    h = target_handle.lstrip('@')
    url = f"{BASE}/user/profile/{quote_plus(h)}"
    result = fetch_with_session(url, PLATFORM, handle=viewer_handle)
    result['action'] = 'timeline'
    result['target_handle'] = target_handle
    if result.get('markdown'):
        result['markdown'] = result['markdown'][:14000]
    return result
