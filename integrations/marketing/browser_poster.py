"""Generic browser-driven posting — Bridge Phase 1c (#62).

Post to ANY platform that has a web composer through the user's LOGGED-IN
browser, via the VLM agentic loop.  This GENERALIZES the credential-free publish
path: previously only a standalone LinkedIn script
(_drops_ready/post_linkedin_vlm.py) could drive a browser post.  Here the
SINGLE-SOURCE per-platform composer URLs (``marketing.intents``) are wired into
the SINGLE-SOURCE browser driver (``vlm.local_loop.run_local_agentic_loop``) so
"post content to channel X via the logged-in browser" works for
twitter / linkedin / reddit / hackernews / … with NO per-platform code.

Use when a channel has no API-adapter credentials but the user is logged in to it
in their desktop browser.  External posts must be operator-consent-gated by the
CALLER (the marketing goal), exactly like ``marketing_tools.post_to_channel``.
"""
import logging
import sys

logger = logging.getLogger('hevolve_social')


def _host_os() -> str:
    """The OS the VLM loop will control (the local desktop running HARTOS)."""
    if sys.platform.startswith('win'):
        return 'windows'
    if sys.platform == 'darwin':
        return 'macos'
    return 'linux'


def post_to_platform_via_browser(platform, body=None, code=None,
                                 user_id='system', prompt_id='browser_post',
                                 tier='inprocess', max_eta=600):
    """Open ``platform``'s web composer in the logged-in browser and publish.

    Resolves the composer URL + default body from ``marketing.intents`` (single
    source) and runs the post through ``vlm.local_loop.run_local_agentic_loop``.

    Returns ``{ok, platform, code, status, detail}`` — ``ok`` is True only when
    the loop reports success.  ``ok=False`` (with ``error``) for an unknown
    platform or one with no browser composer (e.g. whatsapp → use its adapter).
    """
    from integrations.marketing.intents import get_intents
    intents = get_intents(platform)
    if not intents:
        return {'ok': False, 'platform': platform,
                'error': f'no canonical intent for platform {platform!r} '
                         f'(see marketing/intents.py)'}

    intent = next((i for i in intents if code and i.code == code), intents[0])
    if not intent.intent_url:
        return {'ok': False, 'platform': platform, 'code': intent.code,
                'error': f'{platform} has no browser composer URL — use its '
                         f'channel adapter (e.g. whatsapp_adapter) instead'}

    text = (body or intent.body_text or '').strip()
    instruction = (
        f"Open this URL in the web browser: {intent.intent_url}\n"
        f"It opens the {platform} post composer; the user is already logged in.\n"
        + (f"If the composer text is empty, type EXACTLY this and change nothing:\n{text}\n"
           if text else "")
        + "Then click the Post / Tweet / Submit button to publish it. "
          "Stop as soon as the post is published."
    )
    message = {
        'instruction_to_vlm_agent': instruction,
        'enhanced_instruction': instruction,
        'user_id': str(user_id),
        'prompt_id': str(prompt_id),
        'os_to_control': _host_os(),
        'max_ETA_in_seconds': int(max_eta),
    }

    try:
        from integrations.vlm.local_loop import run_local_agentic_loop
        result = run_local_agentic_loop(message, tier=tier) or {}
    except Exception as e:
        logger.warning(f"browser post to {platform} failed: {e}")
        return {'ok': False, 'platform': platform, 'code': intent.code, 'error': str(e)}

    status = str(result.get('status', 'unknown')).lower()
    return {
        'ok': status in ('success', 'done', 'completed'),
        'platform': platform,
        'code': intent.code,
        'status': status,
        'detail': result.get('extracted_responses'),
    }
