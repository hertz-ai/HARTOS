"""Browser Research — single dispatch surface.

ALL tool invocations enter through `dispatch()`.  No parallel path.

Responsibilities (in order):
  1. Resolve the right per-platform script.
  2. Domain allowlist check (fail-closed).
  3. Consent gate (T2 only; T3 is public read, no consent needed).
  4. Invoke the script.
  5. Audit log (always — success and failure).
  6. Annotate response with connection_mechanism for agent transparency.

C1 ships YouTube_Transcript + Read_Webpage (T3 only).  T2 dispatch paths land
with their respective scripts in C4+.
"""
import logging
from typing import Any, Optional

from . import audit, domain_allowlist

logger = logging.getLogger('browser_research.tools')


# Tool → (script_module, action) wiring.
# YouTube_Transcript is the only T3 tool that belongs here — it has a distinct
# data source (captions/cc endpoint), not generic URL scraping.
# Generic URL fetch is intentionally NOT in this dispatcher: the canonical tool
# is `data_extraction_from_url` in core/agent_tools.py:1008, which already
# delegates to Crawl4AI → integrations/web_crawler.py.  Re-routing through
# browser_research would create a parallel path.
_TOOL_ROUTES: dict[str, tuple[str, str]] = {
    'YouTube_Transcript':  ('youtube',     'transcript'),
    # T2 routes — dispatched by `platform` arg.  Tool name → script wires the
    # CANONICAL action (search/timeline/post); the script module is resolved
    # at dispatch time from the `platform` kwarg.  See `dispatch()` below.
    'Search_Platform':     ('__per_platform__', 'search'),
    'Read_Timeline':       ('__per_platform__', 'timeline'),
}


# Allowed platforms for the per-platform routes.  Drift-guarded against
# domain_allowlist so a new script must declare both its allowed domains AND
# its tool-route eligibility (fail-closed default).
_PER_PLATFORM_ALLOWED = {
    'twitter', 'reddit', 'linkedin',
    'bilibili', 'xiaohongshu', 'weibo', 'douyin',
}


def list_tools() -> list[dict]:
    """Public introspection — what's wired in this build."""
    return [
        {'name': name, 'script': route[0], 'action': route[1]}
        for name, route in sorted(_TOOL_ROUTES.items())
    ]


def dispatch(
    tool: str,
    user_id: str,
    *,
    url: Optional[str] = None,
    language: str = 'en',
    platform: Optional[str] = None,
    query: Optional[str] = None,
    target_handle: Optional[str] = None,
    handle: Optional[str] = None,
    consent_check=None,
    **extra: Any,
) -> dict:
    """Run a tool by name.  Returns dict response; never raises.

    `consent_check(user_id, scope) -> bool` injected to keep this module
    independent of consent_service at import time (and trivially testable).
    For T2 tools (Search_Platform / Read_Timeline / Post_As_User), the
    `platform` kwarg picks the script module.
    """
    route = _TOOL_ROUTES.get(tool)
    if route is None:
        return {
            'success': False,
            'error': f'unknown tool: {tool!r}. known: {sorted(_TOOL_ROUTES)}',
        }
    script_name, action = route

    # T2 per-platform dispatch — resolve script_name from `platform` kwarg.
    if script_name == '__per_platform__':
        if not platform or platform not in _PER_PLATFORM_ALLOWED:
            return {
                'success': False,
                'error': f'tool {tool!r} requires platform kwarg from '
                         f'{sorted(_PER_PLATFORM_ALLOWED)}; got {platform!r}',
            }
        # Consent gate — T2 platforms need explicit user grant.
        if consent_check is None:
            try:
                from integrations.social.consent_service import ConsentService
                def _default_check(uid, scope):
                    try:
                        return ConsentService.has_capability(uid, scope)
                    except Exception:
                        return False
                consent_check = _default_check
            except Exception:
                consent_check = lambda uid, scope: False
        scope = f'web_research:{platform}'
        if not consent_check(user_id, scope):
            audit.append(
                user_id=user_id, tool=tool, platform=platform,
                connection_mechanism='consent_denied', success=False,
                error=f'consent missing for scope {scope!r}',
            )
            return {
                'success': False,
                'error': f'consent required: grant {scope!r} via /admin/web-research',
                'liquid_ui': {
                    'type': 'consent_prompt',
                    'scope': scope,
                    'platform': platform,
                },
            }
        script_name = platform

    # Domain allowlist gate (T3 web_generic accepts any URL by allowlist design).
    if url is not None and not domain_allowlist.host_allowed(script_name, url):
        audit.append(
            user_id=user_id,
            tool=tool,
            platform=script_name,
            connection_mechanism='blocked',
            success=False,
            error=f'url not on allowlist: {url}',
        )
        return {
            'success': False,
            'error': f'url not allowed for script {script_name!r}: {url}',
        }

    # T3 tools — public reads, no consent needed.  T2 routes will add a
    # consent_check call here when they land (C4+).

    # Lazy script import — keeps cx_Freeze tracer happy and avoids
    # circular imports if a script ever needs to import back into the package.
    try:
        if script_name == 'youtube':
            from .scripts import youtube as script_mod
        else:
            # Per-platform scripts: twitter / reddit / linkedin / bilibili / ...
            from importlib import import_module
            script_mod = import_module(f'{__package__}.scripts.{script_name}')
    except ImportError as exc:
        audit.append(
            user_id=user_id, tool=tool, platform=script_name,
            connection_mechanism='unavailable', success=False, error=str(exc),
        )
        return {'success': False, 'error': f'script not available: {exc}'}

    handler = getattr(script_mod, action, None)
    if handler is None:
        return {'success': False, 'error': f'script {script_name!r} has no action {action!r}'}

    # Build kwargs — only pass what the script's action accepts.
    kwargs: dict[str, Any] = {}
    if action == 'transcript':
        if url is not None:
            kwargs['url'] = url
        kwargs['language'] = language
    elif action == 'search':
        if query is None:
            return {'success': False, 'error': 'search requires `query` kwarg'}
        kwargs['query'] = query
        if handle is not None:
            kwargs['handle'] = handle
    elif action == 'timeline':
        if target_handle is None:
            return {'success': False, 'error': 'timeline requires `target_handle` kwarg'}
        kwargs['target_handle'] = target_handle
        if handle is not None:
            kwargs['viewer_handle'] = handle
    else:
        if url is not None:
            kwargs['url'] = url
    kwargs.update(extra)

    try:
        result = handler(**kwargs)
    except Exception as exc:
        logger.exception('script %s.%s raised', script_name, action)
        audit.append(
            user_id=user_id, tool=tool, platform=script_name,
            connection_mechanism='error', success=False, error=str(exc),
        )
        return {
            'success': False,
            'error': f'script raised: {type(exc).__name__}: {exc}',
        }

    if not isinstance(result, dict):
        result = {'success': True, 'value': result}

    audit.append(
        user_id=user_id,
        tool=tool,
        platform=script_name,
        connection_mechanism=result.get('connection_mechanism', 'unknown'),
        success=bool(result.get('success', False)),
        details={'url': url} if url else None,
        error=result.get('error'),
    )
    return result
