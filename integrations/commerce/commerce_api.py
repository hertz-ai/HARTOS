"""
/api/commerce/* -- the McGroce <-> HARTOS session exchange and status reads.

  POST /api/commerce/session        server-to-server from McGroce's Tomcat
                                    (SpaAgentTokenEndpoint).  Header
                                    X-Commerce-Secret must equal
                                    COMMERCE_SESSION_SECRET (constant-time).
                                    Body {customerId, username, role,
                                    storeId}.  Writes the commerce binding and
                                    returns a HARTOS JWT (tenant 'mcgroce')
                                    the SPA then sends as Bearer to POST /chat
                                    and /api/agent/approval.  The McGroce
                                    service account never reaches a browser.
  GET  /api/commerce/mandates/<id>  the caller's own mandate status (JWT).
  GET  /api/commerce/stream         where the caller's live cards arrive
                                    (no new stream: the per-user topic).
  GET  /api/commerce/health         configured? breaker state.

The agentic chat itself is the existing POST /chat -- no second chat route.
"""

import hmac
import logging
import time

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

commerce_bp = Blueprint('commerce', __name__)


def _json_error(message: str, code: int):
    return jsonify({'success': False, 'error': message}), code


@commerce_bp.route('/api/commerce/session', methods=['POST'])
def commerce_session():
    from core.config_cache import get_secret
    expected = get_secret('COMMERCE_SESSION_SECRET', '')
    if not expected:
        return _json_error('commerce session exchange is not configured', 503)
    given = request.headers.get('X-Commerce-Secret', '')
    if not hmac.compare_digest(given.encode('utf-8'), expected.encode('utf-8')):
        logger.warning('commerce session: bad secret from %s', request.remote_addr)
        return _json_error('forbidden', 403)
    data = request.get_json(silent=True) or {}
    customer_id = data.get('customerId')
    username = str(data.get('username') or '').strip()
    role = str(data.get('role') or 'customer').strip().lower()
    if customer_id in (None, '') or not username:
        return _json_error('customerId and username are required', 400)
    from integrations.commerce.bindings import TENANT, get_bindings
    try:
        binding = get_bindings().upsert(customer_id, username, role,
                                        data.get('storeId'))
    except ValueError as e:
        return _json_error(str(e), 400)
    from integrations.social.auth import decode_jwt, generate_jwt
    token = generate_jwt(binding['user_id'], username, role, tenant_id=TENANT)
    claims = decode_jwt(token) or {}
    expires_in = int(claims['exp'] - time.time()) if claims.get('exp') else None
    return jsonify({'success': True, 'token': token, 'expiresIn': expires_in,
                    'userId': binding['user_id']})


@commerce_bp.route('/api/commerce/mandates/<mandate_id>', methods=['GET'])
def commerce_mandate(mandate_id):
    from integrations.commerce.approvals import approver_from_request
    user_id = approver_from_request(request)
    if not user_id:
        return _json_error('sign in first', 401)
    from integrations.ap2.ap2_mandate import get_mandate_store
    m = get_mandate_store().get(mandate_id)
    # Someone else's mandate answers exactly like a missing one.
    if m is None or m.user_id != user_id:
        return _json_error('mandate not found', 404)
    return jsonify({'success': True, 'mandate': {
        'mandate_id': m.mandate_id, 'status': m.status,
        'payment_id': m.payment_id, 'amount': m.amount,
        'currency': m.currency, 'merchant': m.merchant,
        'expires_at': m.expires_at, 'approved_at': m.approved_at}})


@commerce_bp.route('/api/commerce/stream', methods=['GET'])
def commerce_stream():
    """Where the embed listens.  Commerce cards go out through
    liquid_ui_service.push_agent_ui, which publishes each on the user's own
    'chat.social' topic -- WAMP com.hertzai.hevolve.social.<user_id>, per-user
    SSE event chat.social in bundled Nunba -- as type agent_ui_update."""
    from integrations.commerce.approvals import approver_from_request
    user_id = approver_from_request(request)
    if not user_id:
        return _json_error('sign in first', 401)
    return jsonify({'user_id': user_id,
                    'wamp_topic': f'com.hertzai.hevolve.social.{user_id}',
                    'sse_event': 'chat.social',
                    'message_type': 'agent_ui_update'})


@commerce_bp.route('/api/commerce/health', methods=['GET'])
def commerce_health():
    from integrations.commerce.mcgroce_client import get_client
    client = get_client()
    return jsonify({'status': 'ok',
                    'configured': bool(client.base_url),
                    'admin_configured': bool(client.admin_url and client.admin_key),
                    'breaker': client.breaker.get_stats()['state']})


# The credentials this module reads from the environment; a vault value is
# delivered for these names (hartos.ai_key_vault.reads_from_env).
ENV_SECRETS = (
    'COMMERCE_SESSION_SECRET',
)
