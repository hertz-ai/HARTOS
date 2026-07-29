"""
User-facing Channel Bindings API — /api/social/channels

Provides endpoints for:
- Channel catalog (metadata, capabilities)
- User channel bindings (CRUD, preferred)
- Pairing (code generation + QR, verification)
- Presence (adapter status, heartbeat)
- Unified conversation history
"""

import base64
import io
import logging
import re
from datetime import datetime

from flask import Blueprint, request, jsonify, g

from .auth import require_auth
from .models import (
    db_session, UserChannelBinding, ConversationEntry, ChannelPresence,
)

logger = logging.getLogger(__name__)

channel_user_bp = Blueprint('channel_user', __name__, url_prefix='/api/social/channels')


# ── Catalog ────────────────────────────────────────────────────

@channel_user_bp.route('/catalog', methods=['GET'])
def get_catalog():
    """Return the full channel metadata catalog (public, no auth)."""
    from integrations.channels.metadata import list_all_channels
    catalog = list_all_channels()
    return jsonify({'success': True, 'data': catalog})


@channel_user_bp.route('/catalog/<channel_type>', methods=['GET'])
def get_catalog_channel(channel_type):
    """Return metadata for a single channel."""
    from integrations.channels.metadata import get_channel_metadata
    meta = get_channel_metadata(channel_type)
    if not meta:
        return jsonify({'success': False, 'error': f'Unknown channel: {channel_type}'}), 404
    return jsonify({'success': True, 'data': meta})


# ── Bindings ───────────────────────────────────────────────────

@channel_user_bp.route('/bindings', methods=['GET'])
@require_auth
def list_bindings():
    """List current user's channel bindings."""
    bindings = g.db.query(UserChannelBinding).filter_by(
        user_id=g.user_id,
    ).order_by(UserChannelBinding.created_at.desc()).all()
    return jsonify({'success': True, 'data': [b.to_dict() for b in bindings]})


def _extract_credential(data: dict, meta: dict):
    """Pull the connect-form credential (bot_token, api_key, ...) out of
    the raw request body.

    ChannelSetupScreen.js sends each metadata.setup_fields key at the TOP
    LEVEL of the body (e.g. {'channel_type':'telegram','bot_token':'123:ABC'}),
    not nested under 'metadata' — so it must be looked up by the channel's
    own declared field name, not a fixed key. Returns (cred_key, value) or
    (None, None) if this channel has no non-auto setup field (e.g. 'web',
    or gateway_qr channels like WhatsApp which use a separate pairing flow).
    """
    fields = [f for f in (meta.get('setup_fields') or []) if not f.get('auto')]
    if not fields:
        return None, None
    cred_key = fields[0]['key']
    value = data.get(cred_key) or (data.get('metadata') or {}).get(cred_key)
    return cred_key, value


def _wire_live_adapter(channel_type: str, credential) -> dict:
    """Construct + register a live channel adapter so inbound messages
    from a freshly-connected token-based channel (Telegram/Discord/Slack/
    ...) actually reach the agent.

    Same bug class as WhatsApp's gateway_qr flow (see
    hart_intelligence_entry._ensure_whatsapp_live_adapter): this endpoint
    used to only ever write the UserChannelBinding row — "connected" in
    the UI, but nothing was ever registered into the running
    ChannelRegistry, so FlaskChannelIntegration._handle_message (the
    function that calls /chat and sends the agent's reply back) never
    saw a single inbound message. FlaskChannelIntegration.register_channel()
    adds the adapter to the registry and wires on_message, but — being a
    plain sync call made long after the one-time boot-time start_all() —
    never actually calls adapter.start(), so the adapter would sit
    registered but never connect. This schedules that start explicitly,
    same as the WhatsApp fix. Never raises — binding creation must
    succeed even if live wiring fails.
    """
    if not credential:
        return {'success': True, 'message': 'no credential to wire (binding-only channel)'}
    try:
        import asyncio as _aio
        import time as _time
        from integrations.channels.flask_integration import get_channel_integration
        integration = get_channel_integration()

        if integration.registry.get(channel_type) is not None:
            return {'success': True, 'message': 'adapter already registered'}

        if not integration.register_channel(channel_type, token=credential):
            return {'success': False, 'error': 'register_channel returned False (see server log)'}

        adapter = integration.registry.get(channel_type)
        loop = integration._loop
        if not (loop and loop.is_running()):
            # Same gap as WhatsApp's _ensure_whatsapp_live_adapter (see
            # hart_intelligence_entry.py): entrypoints that skip
            # hartos_bootstrap's full sequence never call
            # FlaskChannelIntegration.start(), so _loop stays None and
            # register_channel() above sits registered-but-never-started
            # forever — confirmed live 2026-07-28 for Telegram, same as
            # WhatsApp the night before. start() is idempotent and cheap,
            # safe to trigger on-demand here.
            integration.start()
            for _ in range(50):  # ~5s for the background thread to spin up
                loop = integration._loop
                if loop and loop.is_running():
                    break
                _time.sleep(0.1)
            else:
                return {'success': False, 'error': 'channel event loop failed to start'}
        if adapter is None:
            return {'success': False, 'error': 'adapter missing after registration'}
        _aio.run_coroutine_threadsafe(adapter.start(), loop)
        return {'success': True, 'message': f'{channel_type} adapter registration scheduled'}
    except Exception as e:
        logger.warning("live adapter wiring failed for %s: %s", channel_type, e)
        return {'success': False, 'error': repr(e)[:300]}


@channel_user_bp.route('/bindings', methods=['POST'])
@require_auth
def create_binding():
    """Create a new channel binding for the current user."""
    data = request.get_json(silent=True) or {}
    channel_type = data.get('channel_type')
    if not channel_type:
        return jsonify({'success': False, 'error': 'channel_type is required'}), 400

    from integrations.channels.metadata import get_channel_metadata
    meta = get_channel_metadata(channel_type)
    if not meta:
        return jsonify({'success': False, 'error': f'Unknown channel: {channel_type}'}), 400

    sender_id = data.get('channel_sender_id', '')
    chat_id = data.get('channel_chat_id', '')

    # The connect form's credential (bot_token, api_key, ...) arrives at
    # the top level of the body, not nested under 'metadata' — fold it
    # into metadata_json here so it's actually persisted (previously
    # silently dropped: only channel_sender_id/channel_chat_id/
    # auth_method/metadata were ever read).
    cred_key, credential = _extract_credential(data, meta)
    metadata_payload = dict(data.get('metadata') or {})
    if cred_key and credential:
        metadata_payload[cred_key] = credential

    # Check for existing binding
    existing = g.db.query(UserChannelBinding).filter_by(
        user_id=g.user_id,
        channel_type=channel_type,
        channel_sender_id=sender_id,
    ).first()

    if existing:
        existing.is_active = True
        existing.channel_chat_id = chat_id or existing.channel_chat_id
        existing.metadata_json = metadata_payload or existing.metadata_json
        existing.auth_method = data.get('auth_method', existing.auth_method)
        binding = existing
    else:
        binding = UserChannelBinding(
            user_id=g.user_id,
            channel_type=channel_type,
            channel_sender_id=sender_id,
            channel_chat_id=chat_id,
            auth_method=data.get('auth_method'),
            metadata_json=metadata_payload or None,
            is_active=True,
            is_preferred=False,
        )
        g.db.add(binding)

    g.db.flush()

    adapter_result = _wire_live_adapter(channel_type, credential)
    if not adapter_result.get('success'):
        logger.warning(
            "create_binding: %s bound but live adapter not wired: %s",
            channel_type, adapter_result.get('error'),
        )

    return jsonify({'success': True, 'data': binding.to_dict()}), 201


@channel_user_bp.route('/bindings/<int:binding_id>', methods=['DELETE'])
@require_auth
def remove_binding(binding_id):
    """Remove a channel binding (soft-delete: set is_active=False)."""
    binding = g.db.query(UserChannelBinding).filter_by(
        id=binding_id, user_id=g.user_id,
    ).first()
    if not binding:
        return jsonify({'success': False, 'error': 'Binding not found'}), 404

    g.db.delete(binding)
    return jsonify({'success': True, 'data': {'deleted': binding_id}})


@channel_user_bp.route('/bindings/<int:binding_id>/preferred', methods=['PUT'])
@require_auth
def set_preferred(binding_id):
    """Set a binding as the preferred reply channel (unsets others)."""
    binding = g.db.query(UserChannelBinding).filter_by(
        id=binding_id, user_id=g.user_id,
    ).first()
    if not binding:
        return jsonify({'success': False, 'error': 'Binding not found'}), 404

    # Unset all other preferred for this user
    g.db.query(UserChannelBinding).filter(
        UserChannelBinding.user_id == g.user_id,
        UserChannelBinding.id != binding_id,
    ).update({'is_preferred': False})

    binding.is_preferred = True
    return jsonify({'success': True, 'data': binding.to_dict()})


# ── Pairing ────────────────────────────────────────────────────

@channel_user_bp.route('/pair/generate', methods=['POST'])
@require_auth
def generate_pair_code():
    """Generate a pairing code + QR code for cross-device linking."""
    try:
        from integrations.channels.security import PairingManager
        pm = PairingManager()
        code = pm.generate_pairing_code(
            user_id=int(g.user_id) if g.user_id.isdigit() else hash(g.user_id) % 100000,
            prompt_id=0,
            expiry_minutes=15,
        )

        # Generate QR as base64 PNG
        qr_data_url = _generate_qr_data_url(f'hevolve://pair?code={code}')

        return jsonify({
            'success': True,
            'data': {
                'code': code,
                'qr_data_url': qr_data_url,
                'expires_in_seconds': 900,
            },
        })
    except Exception as e:
        logger.error("Failed to generate pairing code: %s", e)
        return jsonify({'success': False, 'error': 'Failed to generate pairing code'}), 500


@channel_user_bp.route('/pair/verify', methods=['POST'])
@require_auth
def verify_pair_code():
    """Verify a pairing code and create a UserChannelBinding."""
    data = request.get_json(silent=True) or {}
    code = data.get('code')
    channel_type = data.get('channel', 'mobile')
    sender_id = data.get('sender_id', '')

    if not code:
        return jsonify({'success': False, 'error': 'code is required'}), 400

    try:
        from integrations.channels.security import PairingManager
        pm = PairingManager()
        result = pm.verify_pairing(channel_type, sender_id, code)

        if result is None:
            return jsonify({'success': False, 'error': 'Invalid or expired pairing code'}), 400

        # Find-or-update: re-pairing an already-connected channel_type+sender_id
        # (expired token, retry, device switch) must not 500 on the unique
        # constraint (user_id, channel_type, channel_sender_id).
        existing = g.db.query(UserChannelBinding).filter_by(
            user_id=g.user_id,
            channel_type=channel_type,
            channel_sender_id=sender_id,
        ).first()

        if existing:
            existing.is_active = True
            existing.auth_method = 'pairing'
            binding = existing
        else:
            binding = UserChannelBinding(
                user_id=g.user_id,
                channel_type=channel_type,
                channel_sender_id=sender_id,
                auth_method='pairing',
                is_active=True,
                is_preferred=False,
            )
            g.db.add(binding)
        g.db.flush()

        return jsonify({'success': True, 'data': binding.to_dict()})
    except Exception as e:
        logger.error("Pairing verification failed: %s", e)
        return jsonify({'success': False, 'error': 'Verification failed'}), 500


# ── Gateway-backed channel pairing (Baileys-style) ─────────────
#
# Single seam between the wizard and any channel whose pairing is
# served by an embedded subprocess gateway (currently WhatsApp via
# integrations/social/whatsapp_supervisor.py).  The wizard fetches
# the QR string + the optional 8-char "Link with phone number"
# code from here; this file owns the env-driven base-URL resolution
# so the frontend never has to know whether the gateway is local
# (embedded Baileys) or remote (operator-managed WAHA).

_GATEWAY_HTTP_TIMEOUT = 8.0


def _whatsapp_gateway_base() -> str:
    """Resolve the WhatsApp gateway base URL.  Single source of truth —
    same env the adapter reads (integrations/channels/whatsapp_adapter
    .py:509) so override behaviour stays consistent across surfaces."""
    import os as _os
    return (
        _os.environ.get('WHATSAPP_API_URL')
        or f'http://127.0.0.1:{_os.environ.get("WHATSAPP_GATEWAY_PORT", "3000")}'
    )


def _whatsapp_account_id() -> str:
    """One Baileys session per authenticated user — the gateway stores
    creds under ~/.hevolve/whatsapp/auth/<account_id>/ so this id is
    also the persistence key (g.user_id is opaque enough)."""
    return f'user_{g.user_id}'


def _proxy_gateway(method: str, path: str, **kwargs):
    """Thin HTTP proxy to the gateway.  Returns (json_or_none, status).
    Never raises — connection / timeout errors map to (None, 503)."""
    import json as _json
    import urllib.error
    import urllib.request
    url = _whatsapp_gateway_base().rstrip('/') + path
    body = None
    headers = {'Accept': 'application/json'}
    if 'json' in kwargs and kwargs['json'] is not None:
        body = _json.dumps(kwargs['json']).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_GATEWAY_HTTP_TIMEOUT) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
            try:
                return _json.loads(raw), resp.status
            except _json.JSONDecodeError:
                return {'raw': raw}, resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', errors='replace') if e.fp else ''
        try:
            return _json.loads(raw), e.code
        except _json.JSONDecodeError:
            return {'error': raw or e.reason}, e.code
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.warning("whatsapp gateway unreachable at %s: %s", url, e)
        return None, 503


@channel_user_bp.route('/whatsapp/_debug_adapter', methods=['GET'])
@require_auth
def whatsapp_debug_adapter():
    """TEMP DEBUG (2026-07-27) — introspect the registered live WhatsApp
    adapter's extra config to confirm owner_phone/owner_lid actually got
    set. Remove once the self-chat LID fix is confirmed working."""
    from integrations.channels.flask_integration import get_channel_integration
    integration = get_channel_integration()
    adapter = integration.registry.get('whatsapp')
    loop = integration._loop
    loop_info = {
        'loop_exists': loop is not None,
        'loop_running': bool(loop and loop.is_running()),
    }
    if adapter is None:
        return jsonify({'success': True, 'data': {'registered': False, **loop_info}})
    extra = getattr(adapter.config, 'extra', None) or {}
    return jsonify({'success': True, 'data': {
        'registered': True, 'extra': extra,
        'adapter_running': adapter.is_running(),
        'adapter_status': str(adapter.get_status()),
        **loop_info,
    }})


@channel_user_bp.route('/_debug_adapter/<channel_type>', methods=['GET'])
@require_auth
def generic_debug_adapter(channel_type):
    """TEMP DEBUG (2026-07-28) — same as whatsapp_debug_adapter above but
    for any channel type, to confirm the live-adapter-wiring fix applied
    to _wire_live_adapter() (Telegram/Discord/Slack/...) actually worked."""
    from integrations.channels.flask_integration import get_channel_integration
    integration = get_channel_integration()
    adapter = integration.registry.get(channel_type)
    loop = integration._loop
    loop_info = {
        'loop_exists': loop is not None,
        'loop_running': bool(loop and loop.is_running()),
    }
    if adapter is None:
        return jsonify({'success': True, 'data': {'registered': False, **loop_info}})
    return jsonify({'success': True, 'data': {
        'registered': True,
        'adapter_running': adapter.is_running(),
        'adapter_status': str(adapter.get_status()),
        **loop_info,
    }})


@channel_user_bp.route('/whatsapp/qr', methods=['GET'])
@require_auth
def whatsapp_get_qr():
    """Return the WhatsApp-Web QR string for the current user's session.

    On first call this also starts the session (idempotent).  Frontend
    polls this endpoint every ~2-3s and renders ``qr`` (a WhatsApp-Web
    pairing string — feed it into any QR generator).  When the user
    completes the scan, subsequent calls return ``{authenticated:true,
    qr:null}``.
    """
    account_id = _whatsapp_account_id()
    # Idempotent start — supervisor's session map short-circuits if
    # already running for this account.
    _proxy_gateway('POST', f'/api/sessions/{account_id}/start')
    body, status = _proxy_gateway('GET', f'/api/sessions/{account_id}/status')
    if body is None:
        return jsonify({
            'success': False,
            'error': (
                'WhatsApp gateway unreachable.  If you set WHATSAPP_API_URL '
                'to a remote endpoint, verify it is reachable; otherwise the '
                'embedded Baileys gateway is still starting (first run '
                'installs Node deps, ~30s).'),
        }), 503

    # This flow (real Baileys gateway) never went through /pair/verify or
    # /bindings, so nothing else ever wrote a UserChannelBinding for it —
    # the app's own channel list would show WhatsApp as unconnected even
    # though the gateway is genuinely authenticated. Upsert here, same
    # find-or-update pattern as create_binding()/verify_pair_code().
    if body.get('authenticated'):
        existing = g.db.query(UserChannelBinding).filter_by(
            user_id=g.user_id, channel_type='whatsapp', channel_sender_id='',
        ).first()
        if existing:
            existing.is_active = True
            existing.auth_method = 'gateway_qr'
        else:
            g.db.add(UserChannelBinding(
                user_id=g.user_id,
                channel_type='whatsapp',
                channel_sender_id='',
                auth_method='gateway_qr',
                is_active=True,
                is_preferred=False,
            ))
        g.db.flush()

        # Binding above only makes the Channels list show "connected" —
        # it does not wire a live WhatsAppAdapter into the running
        # ChannelRegistry, so without this, no inbound message from this
        # session ever reaches the agent (same gap that existed on the
        # chat-based Connect_Channel path; see
        # hart_intelligence_entry._ensure_whatsapp_live_adapter). This is
        # the endpoint the mobile app's QRScannerScreen actually polls,
        # so this is the real fix for the mobile connect flow.
        try:
            from hart_intelligence_entry import (
                _ensure_whatsapp_live_adapter,
            )
            _ensure_whatsapp_live_adapter(
                g.user_id, sid=account_id, base=_whatsapp_gateway_base(),
            )
        except Exception as _adapter_err:
            logger.warning(
                "whatsapp_get_qr: live adapter registration failed: %s",
                _adapter_err,
            )

    qr = body.get('qr')
    return jsonify({
        'success': True,
        'data': {
            'qr': qr,
            # Real scannable image, same helper /pair/generate already
            'qr_data_url': _generate_qr_data_url(qr) if qr else None,
            'authenticated': bool(body.get('authenticated')),
            'state': body.get('state', 'unknown'),
            'account_id': account_id,
        },
    }), 200 if status < 400 else status


@channel_user_bp.route('/whatsapp/messages', methods=['GET'])
@require_auth
def whatsapp_get_messages():
    """Poll for messages on the current user's real WhatsApp session —
    both received AND sent (the gateway's buffer includes our own
    echoed sends, so the app renders one thread). ``?since_ts=<unix
    seconds>`` returns only newer messages for incremental polling."""
    account_id = _whatsapp_account_id()
    since_ts = request.args.get('since_ts', '')
    path = f'/api/sessions/{account_id}/messages'
    if since_ts:
        path += f'?since_ts={since_ts}'
    body, status = _proxy_gateway('GET', path)
    if body is None:
        return jsonify({'success': False, 'error': 'WhatsApp gateway unreachable'}), 503
    return jsonify({'success': True, 'data': {'messages': body.get('messages', [])}}), 200 if status < 400 else status


@channel_user_bp.route('/whatsapp/send', methods=['POST'])
@require_auth
def whatsapp_send_message():
    """Send a real WhatsApp message through the current user's session.

    Body: ``{"to": "<phone digits>", "text": "..."}``. ``to`` accepts a
    bare phone number (any format, digits extracted) — the JID suffix
    is added server-side so the app never needs to know Baileys' JID
    format. Pass ``to`` empty/omitted to send to yourself (self-chat)."""
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'success': False, 'error': 'text is required'}), 400

    to_raw = (data.get('to') or '').strip()
    account_id = _whatsapp_account_id()
    if to_raw:
        digits = re.sub(r'\D', '', to_raw)
        if not digits:
            return jsonify({'success': False, 'error': 'to must contain a phone number'}), 400
        jid = f'{digits}@s.whatsapp.net'
    else:
        # Self-chat: same account_id derivation used to start the
        # session, so the accompanying own phone/JID is already known
        # to the gateway from the authenticated socket — ask it for
        # its own JID via /status rather than duplicating that lookup.
        status_body, _ = _proxy_gateway('GET', f'/api/sessions/{account_id}/status')
        own_jid = status_body.get('own_jid') if status_body else None
        if not own_jid:
            return jsonify({'success': False, 'error': 'not connected yet — cannot resolve self chat'}), 409
        jid = own_jid

    body, status = _proxy_gateway(
        'POST', f'/api/sessions/{account_id}/messages/send', json={'to': jid, 'text': text},
    )
    if body is None:
        return jsonify({'success': False, 'error': 'WhatsApp gateway unreachable'}), 503
    if status >= 400:
        return jsonify({'success': False, 'error': body.get('error', 'send failed')}), status
    return jsonify({'success': True, 'data': body})


@channel_user_bp.route('/whatsapp/pair-code', methods=['POST'])
@require_auth
def whatsapp_request_pair_code():
    """Mint an 8-char "Link with phone number" code via Baileys'
    requestPairingCode().  User types this in WhatsApp → Linked Devices
    → Link with phone number instead of scanning a QR.

    Body: ``{"phone": "+91 90030 54371"}`` — accepts any phone format;
    gateway strips non-digits."""
    data = request.get_json(silent=True) or {}
    phone = (data.get('phone') or '').strip()
    if not phone:
        return jsonify({'success': False, 'error': 'phone is required'}), 400
    account_id = _whatsapp_account_id()
    # Ensure session exists before requesting a pair code.
    _proxy_gateway('POST', f'/api/sessions/{account_id}/start')

    # Requesting a NEW pairing code against an already-authenticated
    # session re-triggers Baileys' login handshake mid-flight and can
    # tear down the live connection (observed: a mistyped/wrong number
    # submitted while already linked knocked out a working session,
    # requiring a full gateway restart to recover from saved creds).
    # If we're already connected, there is nothing to pair — say so
    # instead of touching the socket.
    status_body, _ = _proxy_gateway('GET', f'/api/sessions/{account_id}/status')
    if status_body and status_body.get('authenticated'):
        return jsonify({
            'success': False,
            'error': 'This WhatsApp account is already connected. Disconnect it first if you want to re-pair.',
        }), 409

    body, status = _proxy_gateway(
        'POST',
        f'/api/sessions/{account_id}/request-pair-code',
        json={'phone': phone},
    )
    if body is None:
        return jsonify({'success': False, 'error': 'WhatsApp gateway unreachable'}), 503
    if status >= 400:
        return jsonify({'success': False, 'error': body.get('error', 'pair-code failed')}), status
    return jsonify({
        'success': True,
        'data': {'code': body.get('code'), 'account_id': account_id},
    })


# ── Conversational onboarding form-submit re-entry ─────────────
#
# Closes the loop on _start_gateway_qr_pair_push's phone-collection
# form (hart_intelligence_entry.py).  When the agent's gateway_qr
# branch can't find a phone in env / user profile, it emits a one-
# field form whose action= points here.  This route receives the
# submitted phone, sets the env var so the helper picks it up, and
# re-enters _start_gateway_qr_pair_push synchronously — same code
# path the chat agent would take if phone were known upfront.
#
# Auth required: only the authenticated user can connect their
# own WhatsApp account.  Per-user binding gating happens inside
# register_channel via thread_local_data.

@channel_user_bp.route('/<channel_type>/connect-pair-code',
                       methods=['POST'])
@require_auth
def channel_connect_pair_code_submit(channel_type: str):
    """Re-enter gateway_qr pair-code flow with the submitted phone."""
    import os as _os
    data = request.get_json(silent=True) or {}
    phone_raw = (data.get('phone') or '').strip()
    if not phone_raw:
        return jsonify({'success': False, 'error': 'phone required'}), 400
    phone = ''.join(ch for ch in phone_raw if ch.isdigit())
    if not phone:
        return jsonify({
            'success': False,
            'error': 'phone must contain digits (E.164 without +).',
        }), 400

    # Env override is the same knob _start_gateway_qr_pair_push reads
    # at line 1, so setting it here makes the helper find a value on
    # re-entry.  Process-wide so subsequent users on the same node
    # would inherit it — for multi-user nodes the proper fix is a
    # per-user kv store, but the typical Nunba deploy is single-user
    # and this matches the existing HEVOLVE_WHATSAPP_PHONE override
    # discipline.
    _os.environ['HEVOLVE_WHATSAPP_PHONE'] = phone

    try:
        from integrations.channels.metadata import get_channel_metadata
        meta = get_channel_metadata(channel_type) or {}
        # Local import to avoid circular at module load.
        from hart_intelligence_entry import _start_gateway_qr_pair_push
        _start_gateway_qr_pair_push(channel_type, meta)
        return jsonify({
            'success': True,
            'message': (
                f"Generating pair-code for {meta.get('display_name') or channel_type}. "
                f"Watch chat for the code card."
            ),
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f"connect-pair-code re-entry failed: {e}",
        }), 500


# ── Presence ───────────────────────────────────────────────────

@channel_user_bp.route('/presence', methods=['GET'])
def get_presence():
    """Get all channel adapter statuses (public)."""
    with db_session(commit=False) as db:
        presences = db.query(ChannelPresence).all()
        return jsonify({'success': True, 'data': [p.to_dict() for p in presences]})


@channel_user_bp.route('/presence/heartbeat', methods=['POST'])
def post_heartbeat():
    """Adapter reports heartbeat — upserts ChannelPresence row."""
    data = request.get_json(silent=True) or {}
    channel_type = data.get('channel_type')
    status = data.get('status', 'online')

    if not channel_type:
        return jsonify({'success': False, 'error': 'channel_type required'}), 400

    try:
        with db_session() as db:
            existing = db.query(ChannelPresence).filter_by(channel_type=channel_type).first()
            if existing:
                existing.status = status
                existing.last_heartbeat = datetime.utcnow()
                existing.error_message = data.get('error_message')
            else:
                presence = ChannelPresence(
                    channel_type=channel_type,
                    status=status,
                    last_heartbeat=datetime.utcnow(),
                    error_message=data.get('error_message'),
                )
                db.add(presence)
            return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Conversation History ───────────────────────────────────────

@channel_user_bp.route('/conversations', methods=['GET'])
@require_auth
def get_conversations():
    """Paginated unified conversation history for the current user."""
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 100)
    channel_filter = request.args.get('channel_type')

    query = g.db.query(ConversationEntry).filter_by(user_id=g.user_id)
    if channel_filter:
        query = query.filter_by(channel_type=channel_filter)

    total = query.count()
    entries = query.order_by(
        ConversationEntry.created_at.desc()
    ).offset((page - 1) * per_page).limit(per_page).all()

    return jsonify({
        'success': True,
        'data': [e.to_dict() for e in entries],
        'pagination': {
            'page': page,
            'per_page': per_page,
            'total': total,
            'pages': (total + per_page - 1) // per_page,
        },
    })


# ── Helpers ────────────────────────────────────────────────────

def _generate_qr_data_url(data: str) -> str:
    """Generate a QR code as a base64 data URL (PNG)."""
    try:
        import qrcode
        qr = qrcode.QRCode(version=1, box_size=8, border=2)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        return f'data:image/png;base64,{b64}'
    except ImportError:
        logger.warning("qrcode package not installed — QR generation unavailable")
        return ''
    except Exception as e:
        logger.warning("QR generation failed: %s", e)
        return ''
