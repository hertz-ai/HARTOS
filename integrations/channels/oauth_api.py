"""
OAuth click-through endpoints (PR O).

Two routes mounted at ``/api/oauth/<channel_type>/...``:

  POST  /api/oauth/<channel_type>/start    (Bearer auth required)
        Generates state + PKCE, builds the provider's authorize URL,
        returns it.  Caller (Connect_Channel agent tool, web overlay,
        admin page) opens the URL in the browser.

  GET   /api/oauth/<channel_type>/callback (PUBLIC — provider redirects)
        Verifies state, exchanges the auth code for tokens, writes the
        channel binding via the existing register_channel path, returns
        an HTML page that posts ``oauth_complete`` to the opener and
        closes itself.

Why a new blueprint instead of admin/api.py:

  /callback must be reachable by the OAuth provider's user-agent —
  the user is mid-redirect from discord.com / accounts.google.com /
  facebook.com and isn't carrying our Bearer header.  The admin
  blueprint's ``before_request`` hook would 401 every callback.  A
  separate blueprint lets /start keep the same auth pattern while
  /callback uses state-token-based identity recovery.

This file contains only the HTTP layer.  All policy lives in:
  - ``integrations.channels.metadata`` — per-channel OAuth params
  - ``integrations.channels.security`` — OAuthStateManager + PKCE
  - ``integrations.channels.agent_tools`` — register_channel (binding write)

So the OAuth flow is one populator into the same binding shape the
paste-form flow writes — no parallel infrastructure.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Any, Optional, Tuple
from urllib.parse import urlencode

import requests
from flask import Blueprint, request, jsonify, g, current_app, Response

from .metadata import (
    CHANNEL_CATALOG,
    is_oauth_capable,
    is_oauth_configured,
    get_channel_metadata,
)
from .security import (
    get_oauth_state_manager,
    generate_pkce_pair,
)

logger = logging.getLogger(__name__)

oauth_bp = Blueprint("channels_oauth", __name__, url_prefix="/api/oauth")


# ───────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────

def _public_base_url() -> str:
    """The externally-reachable URL the OAuth provider will redirect to.

    Order:
      1. HARTOS_PUBLIC_URL env (operator-set, e.g. https://hartos.example.com)
      2. request.url_root (works for localhost / direct access; will fail
         for cloud-hosted providers since they can't reach 127.0.0.1)
    """
    return (os.environ.get('HARTOS_PUBLIC_URL') or '').rstrip('/') \
        or request.url_root.rstrip('/')


def _redirect_uri(channel_type: str) -> str:
    return f"{_public_base_url()}/api/oauth/{channel_type}/callback"


def _client_credentials(channel_type: str) -> Tuple[Optional[str], Optional[str]]:
    """Read the operator-supplied OAuth app credentials from env.

    Single source of truth: ``HARTOS_OAUTH_CLIENT_<TYPE>`` and
    ``HARTOS_OAUTH_SECRET_<TYPE>``.  When unset, ``is_oauth_configured``
    returns False and Connect_Channel falls back to paste-form.
    """
    upper = channel_type.upper()
    return (
        os.environ.get(f'HARTOS_OAUTH_CLIENT_{upper}'),
        os.environ.get(f'HARTOS_OAUTH_SECRET_{upper}'),
    )


def _require_user() -> Optional[Tuple[Response, int]]:
    """Mini auth gate for /start.  Mirrors admin_bp.before_request but
    scoped to this blueprint so /callback can stay public.
    """
    from integrations.social.auth import _get_user_from_token
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'success': False, 'error': 'Authentication required'}), 401
    token = auth_header[7:]
    user, db = _get_user_from_token(token)
    if user is None:
        if db:
            db.close()
        return jsonify({'success': False, 'error': 'Invalid token'}), 401
    g.user = user
    # user.id is a UUID string, not an int. int(user.id) raised
    # ValueError on every real user and 500'd the connect flow -- the
    # exact bug that code review missed and only a live token exposed.
    # Match the working @require_auth path (social/auth.py), which stores
    # str(user.id). The value only has to round-trip through the OAuth
    # state store and back, so a string is correct.
    g.user_id = str(user.id)
    g.db = db
    return None


@oauth_bp.teardown_request
def _oauth_teardown(exc):
    db = getattr(g, 'db', None)
    if db:
        try:
            if exc:
                db.rollback()
            else:
                db.commit()
        finally:
            db.close()


# ───────────────────────────────────────────────────────────────────
# Authorize-URL builder (single source of truth)
# ───────────────────────────────────────────────────────────────────
#
# Two callers consume this:
#   1. POST /api/oauth/<channel_type>/start (HTTP layer below).
#   2. hart_intelligence_entry._handle_connect_channel_tool —
#      the agent emits ``oauth_link`` directly without going through
#      HTTP.  Both must produce identical URLs (identical params,
#      identical state-store entries) — keeping the builder in one
#      place is the structural fix for that invariant.
#
# Returns ``(authorize_url, state)`` so callers can echo state back to
# the client if they need correlation.

def build_authorize_url(
    user_id: str,
    channel_type: str,
    return_to: str = '',
) -> Tuple[str, str]:
    """Build the provider's authorize URL with state + PKCE.

    Caller-side preconditions (NOT re-checked here so the helper stays
    pure): channel_type must be OAuth-capable AND OAuth-configured.
    Use ``is_oauth_capable`` / ``is_oauth_configured`` before calling.

    Side effect: generates and stores a state record in the global
    OAuthStateManager.  The returned URL embeds that state token.
    """
    meta = get_channel_metadata(channel_type) or {}
    client_id, _ = _client_credentials(channel_type)
    redirect_uri = _redirect_uri(channel_type)

    # PKCE for providers that require it (Google, Microsoft, Twitter v2).
    code_verifier = None
    code_challenge = None
    if meta.get('oauth_uses_pkce'):
        code_verifier, code_challenge = generate_pkce_pair()

    state = get_oauth_state_manager().generate_state(
        user_id=user_id,
        channel_type=channel_type,
        code_verifier=code_verifier,
        return_to=return_to,
    )

    params: Dict[str, Any] = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': meta.get('oauth_scopes') or '',
        'state': state,
    }
    if code_challenge:
        params['code_challenge'] = code_challenge
        params['code_challenge_method'] = 'S256'

    # Per-provider extras (Discord permissions, Google access_type, etc).
    for k, v in (meta.get('oauth_extra_params') or {}).items():
        params[k] = v

    authorize_url = f"{meta['oauth_authorize_url']}?{urlencode(params)}"
    logger.info(
        "OAuth authorize URL built: user=%s channel=%s redirect=%s pkce=%s",
        user_id, channel_type, redirect_uri, bool(code_verifier),
    )
    return authorize_url, state


# ───────────────────────────────────────────────────────────────────
# /api/oauth/<channel_type>/start
# ───────────────────────────────────────────────────────────────────

@oauth_bp.route("/<channel_type>/start", methods=["POST"])
def oauth_start(channel_type: str):
    """Build the provider's authorize URL with state + PKCE.

    Body (optional): {"return_to": "<deep-link>"}  — the deep link the
    callback's close-page should ping back so the agent overlay can
    dismiss itself.  Stored alongside the state record; not validated
    here (validated by the client when it consumes the postMessage).
    """
    auth_err = _require_user()
    if auth_err is not None:
        return auth_err

    channel_type = (channel_type or '').lower().strip()
    meta = get_channel_metadata(channel_type) or {}
    nice = meta.get('display_name') or channel_type.title()

    # Every error a tenant can trigger here has to read like a sentence, not
    # a config note. This container is multitenant: the person hitting this
    # is an end user connecting THEIR account, not the operator of the box.
    # They cannot set an env var, do not know what one is, and may be reading
    # in a second language. The operator-facing detail (which env var, which
    # provider) goes to the log; the response carries a plain message plus a
    # machine-readable `reason` so the app/agent can decide what to show or
    # offer next (retry later, try a different channel, use paste-a-token).
    if not is_oauth_capable(channel_type):
        # Not OAuth-capable can mean two very different things, and telling a
        # layman the wrong one strands them: WhatsApp is not OAuth because it
        # connects by scanning a QR from the phone, while an unknown name is
        # just a typo. Point at the channel's REAL method (from its metadata)
        # rather than blanket-suggesting "paste a token", which is wrong for
        # WhatsApp and useless for a name that does not exist.
        if not meta:
            logger.info("oauth_start: unknown channel %r", channel_type)
            return jsonify({
                'success': False,
                'reason': 'unknown_channel',
                'channel': channel_type,
                'error': f'We don\'t recognise a channel called "{channel_type}".',
            }), 400

        auth_method = meta.get('auth_method')
        if auth_method == 'gateway_qr':
            how = f"Connect {nice} by scanning a QR code from your phone."
        else:
            visible = [f for f in (meta.get('setup_fields') or [])
                       if not f.get('auto')]
            if visible:
                how = (f"Add {nice} by pasting your "
                       f"{visible[0].get('label') or 'token'}.")
            else:
                how = f"{nice} isn't ready to connect this way yet."
        logger.info("oauth_start: %s is not OAuth-capable (auth_method=%s)",
                    channel_type, auth_method)
        return jsonify({
            'success': False,
            'reason': 'not_oauth',
            'channel': channel_type,
            'auth_method': auth_method,
            'error': how,
        }), 400
    if not is_oauth_configured(channel_type):
        # Not the tenant's fault and not the tenant's fix: the shared Hevolve
        # app for this provider has not been switched on yet. Say exactly
        # that, and keep the env-var names in the log for whoever runs the
        # box.
        logger.warning(
            "oauth_start: %s requested but the shared OAuth app is not "
            "configured (set HARTOS_OAUTH_CLIENT_%s and "
            "HARTOS_OAUTH_SECRET_%s in the deploy env)",
            channel_type, channel_type.upper(), channel_type.upper(),
        )
        return jsonify({
            'success': False,
            'reason': 'not_configured',
            'channel': channel_type,
            'error': (
                f"Connecting {nice} isn't switched on yet. The Hevolve team "
                f"still has to enable it. There's nothing you need to do, "
                f"and it isn't a problem with your account."
            ),
        }), 400

    body = request.get_json(silent=True) or {}
    return_to = body.get('return_to') or ''
    authorize_url, state = build_authorize_url(
        user_id=g.user_id,
        channel_type=channel_type,
        return_to=return_to,
    )
    return jsonify({
        'success': True,
        'authorize_url': authorize_url,
        'redirect_uri': _redirect_uri(channel_type),
        'state': state,  # exposed so client can correlate; provider
                         # echoes it back, we re-validate.
    })


# ───────────────────────────────────────────────────────────────────
# /api/oauth/<channel_type>/callback  (PUBLIC)
# ───────────────────────────────────────────────────────────────────

def _exchange_code(
    channel_type: str,
    code: str,
    code_verifier: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """POST to the provider's token endpoint, return (token_dict, error).

    The token_dict shape varies per provider — Slack v2 nests bot creds
    under ``bot``; Google flat-returns ``{access_token, refresh_token}``;
    Meta returns ``{access_token, expires_in}``.  Caller maps fields via
    ``oauth_token_response_map``.
    """
    meta = get_channel_metadata(channel_type) or {}
    client_id, client_secret = _client_credentials(channel_type)
    redirect_uri = _redirect_uri(channel_type)

    data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'client_secret': client_secret,
    }
    if code_verifier:
        data['code_verifier'] = code_verifier

    headers = {'Accept': 'application/json'}
    try:
        resp = requests.post(
            meta['oauth_token_url'],
            data=data,
            headers=headers,
            timeout=15,
        )
    except requests.RequestException as e:
        return None, f'Token exchange request failed: {e}'

    try:
        token = resp.json()
    except ValueError:
        return None, f'Token exchange returned non-JSON (status {resp.status_code}): {resp.text[:200]}'

    if resp.status_code >= 400 or token.get('error'):
        return None, (
            f'Token exchange rejected (status {resp.status_code}): '
            f'{token.get("error_description") or token.get("error") or token}'
        )
    return token, None


def _walk(d: Dict[str, Any], dotted: str) -> Any:
    """Resolve a dotted path like ``bot.access_token`` against ``d``."""
    cur: Any = d
    for part in dotted.split('.'):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _close_page_html(channel_type: str, ok: bool, message: str) -> str:
    """HTML the OAuth provider's redirect lands on.  Three dismissal
    paths so the same page works for web popups, RN WebView popups,
    and mobile system browsers:

      1. ``window.opener.postMessage`` — web (popup window).  Skipped
         when there is no opener (mobile system-browser case) so we
         don't trigger an unnecessary "Open in Hevolve?" prompt on
         desktop browsers that aren't popups.
      2. ``window.location = 'hevolve://oauth-complete?…'`` — mobile.
         deepLinkService picks the URI up and emits
         ``DeviceEventEmitter('onAgentOAuthComplete')`` to dismiss the
         OAuthLinkCard.
      3. ``window.close()`` — fallback for both surfaces.

    Escaping:
      - HTML body uses ``html.escape`` so the user-visible message
        can't break out of <p>.
      - JS payload is built via ``json.dumps`` so no string injected
        into ``message`` (including ``</script>`` or backslashes) can
        break out of the JSON literal in the script tag.
      - Deep-link query is built via ``urlencode`` so ``&``/``=``/`#``
        in the message don't corrupt the param parser on the RN side.
    """
    import html as _html
    import json as _json
    payload_obj = {
        'type': 'oauth_complete',
        'channel_type': channel_type,
        'ok': bool(ok),
        'message': message or '',
    }
    # ensure_ascii=True keeps the JSON safe in HTML (no </script>
    # surrogate escapes needed); the </ catastrophe is handled by the
    # extra replace below.
    payload_js = _json.dumps(payload_obj, ensure_ascii=True).replace(
        '</', '<\\/'
    )
    deep_link_qs = urlencode({
        'channel_type': channel_type,
        'ok': 'true' if ok else 'false',
        'message': message or '',
    })
    deep_link = f"hevolve://oauth-complete?{deep_link_qs}"
    body_msg = _html.escape(message or '')
    return f'''<!doctype html>
<html><head><meta charset="utf-8"><title>Channel connected</title>
<style>body{{font:14px system-ui;margin:48px;text-align:center}}
.ok{{color:#1b8a3a}}.err{{color:#c0392b}}</style></head>
<body>
<h2 class="{ 'ok' if ok else 'err' }">{ 'Connected.' if ok else 'Connection failed.' }</h2>
<p>{body_msg}</p>
<p><small>This window will close automatically.</small></p>
<script>
try {{
  if (window.opener) {{
    window.opener.postMessage({payload_js}, "*");
  }} else {{
    window.location = {_json.dumps(deep_link)};
  }}
}} catch(e){{}}
setTimeout(function(){{ try{{window.close();}}catch(e){{}} }}, 1500);
</script>
</body></html>'''


@oauth_bp.route("/<channel_type>/callback", methods=["GET"])
def oauth_callback(channel_type: str):
    """Provider-redirect target.  No Bearer auth — state token is the
    proof of identity (single-use, 10-min TTL, replay-protected).
    """
    channel_type = (channel_type or '').lower().strip()

    code = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')

    if error:
        return Response(
            _close_page_html(channel_type, False, f'Provider error: {error}'),
            mimetype='text/html', status=400,
        )

    if not code or not state:
        return Response(
            _close_page_html(channel_type, False, 'Missing code or state.'),
            mimetype='text/html', status=400,
        )

    ctx = get_oauth_state_manager().verify_state(state)
    if ctx is None:
        return Response(
            _close_page_html(channel_type, False, 'Invalid or expired state token.'),
            mimetype='text/html', status=400,
        )
    if ctx['channel_type'] != channel_type:
        # State token was for a different channel — possible CSRF.
        return Response(
            _close_page_html(channel_type, False, 'State / channel mismatch.'),
            mimetype='text/html', status=400,
        )

    token, err = _exchange_code(channel_type, code, ctx.get('code_verifier'))
    if token is None:
        return Response(
            _close_page_html(channel_type, False, err or 'Token exchange failed.'),
            mimetype='text/html', status=502,
        )

    # Map provider response → our binding-config shape via the metadata
    # response map.  Empty map (e.g. LINE) → bounce to paste form;
    # caller's overlay will receive ok=true but message indicates next step.
    meta = get_channel_metadata(channel_type) or {}
    response_map = meta.get('oauth_token_response_map') or {}
    config: Dict[str, Any] = {}
    for src_path, dst_key in response_map.items():
        val = _walk(token, src_path)
        if val is not None:
            config[dst_key] = val

    if not config and response_map:
        # Mapped fields all came back null — shouldn't happen on a 2xx
        # response; treat as failure so we don't write an empty binding.
        return Response(
            _close_page_html(channel_type, False, 'Provider response missing expected token fields.'),
            mimetype='text/html', status=502,
        )

    # Write binding via the existing register_channel path — same single
    # populator the paste form goes through.  No parallel write path.
    try:
        from integrations.channels.agent_tools import build_channel_tool_closures
        tool_ctx = {'user_id': ctx['user_id'], 'prompt_id': None}
        tools = build_channel_tool_closures(tool_ctx) or []
        register_fn = next(
            (t[2] for t in tools
             if isinstance(t, tuple) and len(t) >= 3 and t[0] == 'register_channel'),
            None,
        )
        if register_fn is None:
            return Response(
                _close_page_html(channel_type, False, 'Channel registration is unavailable.'),
                mimetype='text/html', status=500,
            )
        import json as _json
        result = register_fn(channel_type, _json.dumps(config))
        ok = isinstance(result, str) and 'registered and enabled' in result
        # On success emit a `channel_connected` card to the user's chat
        # so the Demopage AgentOverlay shows the success without the
        # user refreshing.  Same payload shape as the gateway_qr polling
        # thread's success branch — single canonical kind.  The close-
        # page also fires postMessage back to the opener for the popup
        # auto-close, but THIS is what populates the chat status.
        if ok:
            try:
                from core.platform.registry import get_registry
                _lui = get_registry().get('LiquidUIService')
                if _lui:
                    display_name = meta.get('display_name') or channel_type
                    _lui.agent_ui_update(
                        str(ctx['user_id']),
                        {
                            'type': 'channel_connected',
                            'channel': channel_type,
                            'channel_type': channel_type,
                            'display_name': display_name,
                            'color': meta.get('color') or '#00e89d',
                            'icon': meta.get('icon') or channel_type,
                            'message': f"✅ {display_name} connected.",
                        },
                    )
            except Exception as _emit_err:
                logger.debug(
                    "oauth_callback: channel_connected emit skipped: %s",
                    _emit_err,
                )
        return Response(
            _close_page_html(channel_type, ok, result if isinstance(result, str) else 'Connected.'),
            mimetype='text/html',
            status=200 if ok else 502,
        )
    except Exception as e:
        logger.exception("OAuth callback register_channel failed")
        return Response(
            _close_page_html(channel_type, False, f'Registration error: {e}'),
            mimetype='text/html', status=500,
        )
