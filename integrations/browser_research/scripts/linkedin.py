"""LinkedIn — T2 platform script (read).

NOTE on LinkedIn TOS: LinkedIn aggressively detects automation, even with
real cookies.  Using the user's CDP-attached Chrome session (B2 mode) is the
safest path — LinkedIn sees a normal browser the user is already in.  B1
fallback uses Obscura's stealth profile but TOS risk is higher.

Domain-locked to linkedin.com by domain_allowlist.
"""
from urllib.parse import quote_plus
from typing import Optional

from ._base import fetch_with_session

PLATFORM = 'linkedin'
BASE = 'https://www.linkedin.com'


def search(query: str, handle: Optional[str] = None, limit: int = 25) -> dict:
    url = f"{BASE}/search/results/all/?keywords={quote_plus(query)}"
    result = fetch_with_session(url, PLATFORM, handle=handle)
    result['action'] = 'search'
    result['query'] = query
    if result.get('markdown'):
        result['markdown'] = result['markdown'][:12000]
    return result


def timeline(target_handle: str, viewer_handle: Optional[str] = None) -> dict:
    """Read a public profile by handle (e.g. 'sathishbabu')."""
    h = target_handle.lstrip('@').lstrip('in/')
    url = f"{BASE}/in/{quote_plus(h)}/"
    result = fetch_with_session(url, PLATFORM, handle=viewer_handle)
    result['action'] = 'timeline'
    result['target_handle'] = target_handle
    if result.get('markdown'):
        result['markdown'] = result['markdown'][:12000]
    return result
