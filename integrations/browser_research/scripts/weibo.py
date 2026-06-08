"""微博 (Weibo) — T2 platform script (read).

Mobile-web search is more scrapable than the desktop site.
Domain-locked to weibo.com / weibo.cn.
"""
from urllib.parse import quote_plus
from typing import Optional

from ._base import fetch_with_session

PLATFORM = 'weibo'
BASE_MOBILE = 'https://m.weibo.cn'


def search(query: str, handle: Optional[str] = None, limit: int = 25) -> dict:
    url = f"{BASE_MOBILE}/search?containerid=100103type%3D1%26q%3D{quote_plus(query)}"
    result = fetch_with_session(url, PLATFORM, handle=handle)
    result['action'] = 'search'
    result['query'] = query
    if result.get('markdown'):
        result['markdown'] = result['markdown'][:14000]
    return result


def timeline(target_handle: str, viewer_handle: Optional[str] = None) -> dict:
    h = target_handle.lstrip('@')
    url = f"{BASE_MOBILE}/u/{quote_plus(h)}"
    result = fetch_with_session(url, PLATFORM, handle=viewer_handle)
    result['action'] = 'timeline'
    result['target_handle'] = target_handle
    if result.get('markdown'):
        result['markdown'] = result['markdown'][:14000]
    return result
