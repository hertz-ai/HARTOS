"""Reddit — T2 platform script (read).

Search uses the public web UI with session cookies; logged-in subs are
included because the cookies grant `reddit_session`.  Domain-locked to
reddit.com by domain_allowlist (subdomain match handles old.reddit.com etc.).

Distinct from any T1 Reddit DM adapter: this is read/search of feeds + posts,
not real-time-message ingestion.
"""
from urllib.parse import quote_plus
from typing import Optional

from ._base import fetch_with_session, post_preview

PLATFORM = 'reddit'
BASE = 'https://www.reddit.com'


def search(query: str, handle: Optional[str] = None, limit: int = 25) -> dict:
    url = f"{BASE}/search/?q={quote_plus(query)}&sort=new"
    result = fetch_with_session(url, PLATFORM, handle=handle)
    result['action'] = 'search'
    result['query'] = query
    if result.get('markdown'):
        result['markdown'] = result['markdown'][:14000]
    return result


def timeline(target_handle: str, viewer_handle: Optional[str] = None) -> dict:
    """Read u/<handle> profile (their posts + comments)."""
    h = target_handle.lstrip('u/').lstrip('@')
    url = f"{BASE}/user/{quote_plus(h)}/"
    result = fetch_with_session(url, PLATFORM, handle=viewer_handle)
    result['action'] = 'timeline'
    result['target_handle'] = target_handle
    if result.get('markdown'):
        result['markdown'] = result['markdown'][:14000]
    return result


def post(content, handle=None, dry_run=True):
    return post_preview(PLATFORM, content, handle=handle, dry_run=dry_run)

