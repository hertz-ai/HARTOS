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


def post_preview(platform: str, content: str, handle: Optional[str] = None,
                 dry_run: bool = True) -> dict:
    """Canonical preview-or-post for write-side T2 actions.

    dry_run=True (default) → returns a `liquid_ui: post_preview` card with the
    proposed content; the calling agent must re-invoke with dry_run=False
    AFTER the user explicitly confirms via tap.  Same shape as Invite_Friend
    and channel_send confirms.

    dry_run=False → not yet implemented per-platform; explicit guard at this
    layer keeps the canonical wiring + audit log honest until per-platform
    POST endpoints land.  Returns success=False with a clear "needs platform
    post path" message so the agent surfaces this to the user as a UI state
    not a silent failure.
    """
    if not content or not content.strip():
        return {'success': False, 'platform': platform,
                'error': 'post content empty'}

    if dry_run:
        # Canonical preview card — single source of truth schema (mirrors
        # the meet_copilot + Invite_Friend preview cards).
        return {
            'success': True,
            'platform': platform,
            'handle': handle,
            'dry_run': True,
            'connection_mechanism': 'preview_only',
            'liquid_ui': {
                'type': 'post_preview',
                'platform': platform,
                'handle': handle or '(first vault entry)',
                'content': content,
                'confirm_tool': 'Post_As_User',
                'confirm_args': {
                    'platform': platform,
                    'content': content,
                    'handle': handle,
                    'dry_run': False,
                },
                'cancel_label': 'Cancel',
                'confirm_label': f'Post to {platform}',
            },
        }

    # dry_run=False — per-platform POST path is not yet implemented.  We
    # ship the preview-confirm CONTRACT but require explicit per-platform
    # work before any real post happens.  This guard is intentional: it
    # prevents an LLM accident from posting before per-platform code is
    # reviewed against the platform's TOS + form encoding + auth nuance.
    return {
        'success': False,
        'platform': platform,
        'handle': handle,
        'dry_run': False,
        'connection_mechanism': 'unimplemented_write',
        'error': (
            f'post action for {platform!r} is preview-only in this build. '
            'Per-platform POST implementation pending — preview-confirm '
            'contract is in place but real write is intentionally gated. '
            'Track in memory/project_browser_research_subsystem.md.'
        ),
    }


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
