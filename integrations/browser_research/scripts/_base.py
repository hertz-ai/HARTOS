"""Common per-platform script helpers.

Every T2 platform script (twitter, reddit, linkedin, bilibili, ...) routes
the actual fetch through `integrations.web_crawler.crawl_url_with_cookies` —
the canonical primitive.  Per-platform scripts ONLY own:

  1. URL construction (build search / timeline / post URLs)
  2. Cookie lookup from the AccountVault
  3. CDP endpoint resolution (B2 vs B1)
  4. Response parsing (extract structured items from the crawled HTML)

No script has its own crawler instance.  No script bypasses the vault.
No script writes directly to platforms — write operations go through
`Post_As_User` which requires dry_run=True on the first call (preview).
"""
import logging
import os
from typing import Optional

logger = logging.getLogger('browser_research.scripts')


def get_cdp_endpoint() -> Optional[str]:
    """Resolve the B2 CDP endpoint to attach to, or None for B1 fallback.

    Reads HEVOLVE_BROWSER_CDP_ENDPOINT env var; default "http://127.0.0.1:9222"
    when HEVOLVE_BROWSER_USE_B2=1.
    """
    use_b2 = os.environ.get('HEVOLVE_BROWSER_USE_B2', '').lower() in ('1', 'true', 'yes', 'on')
    if not use_b2:
        return None
    return os.environ.get('HEVOLVE_BROWSER_CDP_ENDPOINT', 'http://127.0.0.1:9222')


def fetch_with_session(url: str, platform: str, handle: Optional[str] = None,
                       timeout: int = 30) -> dict:
    """Canonical 'fetch with this user's cookies' entry for T2 scripts.

    Looks up the AccountVault for the (platform, handle) cookie jar, picks
    the B2/B1 mode from env, and calls the canonical crawler.

    Returns the crawler dict augmented with `platform`/`handle` so the audit
    log can attribute the call.
    """
    from ..vault import get_vault
    from integrations.web_crawler import crawl_url_with_cookies

    cookies: list = []
    if handle:
        acc = get_vault().get(platform, handle)
        if acc:
            cookies = acc.cookies
    else:
        # No handle given — pick the first vault entry for the platform
        v = get_vault()
        handles = v.list_handles(platform)
        if handles:
            handle = handles[0]
            acc = v.get(platform, handle)
            if acc:
                cookies = acc.cookies

    cdp = get_cdp_endpoint()
    result = crawl_url_with_cookies(url, cookies=cookies, timeout=timeout,
                                    cdp_endpoint=cdp)
    result['platform'] = platform
    result['handle'] = handle
    return result
