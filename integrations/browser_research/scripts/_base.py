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

from core.tool_refusal import NOTE_KEY, RETRY_NEVER, refusal_fields

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
    # The sentence is built by core.tool_refusal, not by hand: a hand-written
    # 'error' leaves status unset, so core.constants.tool_reply_failed reads
    # this reply as a SUCCESSFUL call and the CREATE trace banker and the
    # REUSE credit gates bank a post that was never sent.  refusal_fields
    # supplies status='refused', the code, the retry mode and the sentence;
    # 'error' is kept, carrying that same sentence, so nothing that already
    # reads 'error' breaks.
    refusal = refusal_fields(
        'Post_As_User', 'preview_only',
        f'nothing was posted to {platform}: posting is preview-only in this '
        'build, which has no per-platform write path yet',
        'the text was not sent anywhere and comes back unchanged under '
        '"content", so the person can post it themselves now; for the agent '
        'to post it, a reviewed per-platform write path has to land first',
        retry=RETRY_NEVER)
    return {
        'success': False,
        'platform': platform,
        'handle': handle,
        'dry_run': False,
        'connection_mechanism': 'unimplemented_write',
        'error': refusal[NOTE_KEY],
        'content': content,
        **refusal,
    }


def fetch_with_session(url: str, platform: str, handle: Optional[str] = None,
                       timeout: int = 30) -> dict:
    """Canonical 'fetch with this user's cookies' entry for T2 scripts.

    Looks up the AccountVault for the (platform, handle) cookie jar, picks
    the B2/B1 mode from env, and calls the canonical crawler.

    Returns the crawler dict augmented with `platform`, `handle` and
    `authenticated`.  `handle` is the handle whose cookies were ACTUALLY
    sent, or None when the crawl went out logged out — it is never the
    handle that was merely asked for.  When no cookies resolve, the result
    is relabelled `logged_out_public` and carries a `notice` the agent must
    repeat, because a logged-out fetch returns the public view or a login
    wall, not the person's own signed-in session.

    The one exception is real: under HEVOLVE_BROWSER_USE_B2 the crawler
    attaches over CDP to the person's OWN Chrome, so an empty vault still
    reads their signed-in profile.  That is reported as it happened --
    `obscura_b2_cdp_user_chrome`, `authenticated` True, no notice -- and the
    crawler's own `public_http` is likewise left alone.
    """
    from ..vault import get_vault
    from integrations.web_crawler import crawl_url_with_cookies

    # An entry with an empty cookie list is not a session, so it must not
    # count as one — `authenticated` is about cookies on the wire, not about
    # a row existing in the vault.
    cookies: list = []
    session_handle: Optional[str] = None
    v = get_vault()
    if handle:
        acc = v.get(platform, handle)
        if acc and acc.cookies:
            cookies = acc.cookies
            session_handle = handle
    else:
        # No handle given — first vault entry for the platform that has cookies
        for candidate in v.list_handles(platform):
            acc = v.get(platform, candidate)
            if acc and acc.cookies:
                cookies = acc.cookies
                session_handle = candidate
                break

    cdp = get_cdp_endpoint()
    result = crawl_url_with_cookies(url, cookies=cookies, timeout=timeout,
                                    cdp_endpoint=cdp)
    # Only the crawler knows HOW it reached the page, so only it can label
    # that.  Under HEVOLVE_BROWSER_USE_B2 it attaches over CDP to the person's
    # OWN Chrome and reads their own profile, which IS their signed-in session
    # even when the vault is empty.  Keying this off "no cookies" made the
    # function overwrite that with logged_out_public in the result, the notice
    # AND the audit log, so the audit understated the one path that drives the
    # person's browser.  Key it off how access actually happened instead.
    mechanism = result.get('connection_mechanism')
    person_session = mechanism == 'obscura_b2_cdp_user_chrome'
    result['platform'] = platform
    # Whose session was really used: the handle whose cookies went on the
    # wire, or -- with no cookies but their own browser attached -- the
    # handle that was asked for, because that browser is theirs.
    result['handle'] = handle if (person_session and not cookies) else session_handle
    result['authenticated'] = bool(cookies) or person_session
    if not result['authenticated']:
        # 'public_http' already says plain HTTP, no browser, no auth.
        # Relabelling it loses which path ran and tells the audit log less
        # than the crawler already knew.
        if mechanism != 'public_http':
            result['connection_mechanism'] = 'logged_out_public'
        result['notice'] = (
            f'This is NOT the person\'s signed-in {platform} session. No '
            f'stored {platform} session was found, so the page was fetched '
            f'logged out and may be the public view or a login wall. Say that '
            f'when using it; do not present it as their own feed, account or '
            f'private data. To fix: a {platform} account has to be in the '
            f'vault, and this build can only list stored sessions (GET '
            f'/api/web-research/vault) and revoke them (DELETE '
            f'/api/web-research/vault/<platform>) — no sign-in path writes '
            f'one yet, so this needs the owner.'
        )
    return result
