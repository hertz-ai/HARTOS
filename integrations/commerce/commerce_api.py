"""
Commerce HTTP surface: /api/commerce/*.

POST /api/commerce/session
    Server-to-server, from the McGroce site (SpaAgentTokenEndpoint).  The
    caller proves itself with ``X-Commerce-Secret`` (compared with
    hmac.compare_digest against COMMERCE_SESSION_SECRET) and names the
    logged-in customer; the answer is ``{"token": ..., "expiresIn": seconds}``.
    The token is an HS256 JWT signed with a key DERIVED from that secret, so it
    can never be mistaken for, or used as, a HARTOS login token.

Embed routes (Bearer = that commerce token, or a HARTOS login token):
    GET  /api/commerce/products?q=...&store_id=...
    GET  /api/commerce/cart
    POST /api/commerce/cart/items        {product_id, quantity?, store_id?}
    DELETE /api/commerce/cart/items/<product_id>
    POST /api/commerce/checkout          {store_id?}
    GET  /api/commerce/orders/<order_id>
    GET  /api/commerce/stream            where this user's live cards arrive

The routes call the same functions the agent's tools are (commerce_tools), so
there is one implementation of each verb.  Approvals (``ap2_pay:<id>``,
``merchant_onboard:<id>``) are answered on /api/agent/approval, which calls
``handle_commerce_approval`` here.
"""
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, jsonify, request

from integrations.commerce import bindings, commerce_tools

logger = logging.getLogger('hevolve.commerce')

commerce_bp = Blueprint('commerce', __name__, url_prefix='/api/commerce')

SESSION_SCOPE = 'commerce'
_KEY_CONTEXT = b'hartos-commerce-session-v1'
_CUSTOMER_FIELDS = ('customerId', 'customer_id', 'userId', 'user_id',
                    'username', 'email')


# ─── Session tokens ────────────────────────────────────────────────

def _signing_key(secret: str) -> bytes:
    return hmac.new(secret.encode('utf-8'), _KEY_CONTEXT,
                    hashlib.sha256).digest()


def issue_session_token(customer_id, secret: Optional[str] = None,
                        ttl: Optional[int] = None) -> Dict[str, Any]:
    """``{token, expiresIn}`` for a McGroce customer."""
    import jwt
    secret = secret if secret is not None else bindings.commerce_session_secret()
    ttl = ttl if ttl is not None else bindings.commerce_session_ttl()
    identity = bindings.mcgroce_identity(customer_id)
    if not secret or not identity:
        raise ValueError('secret and customer id are required')
    now = int(time.time())
    token = jwt.encode({'sub': identity, 'scope': SESSION_SCOPE,
                        'cid': str(customer_id), 'iat': now, 'exp': now + ttl},
                       _signing_key(secret), algorithm='HS256')
    return {'token': token, 'expiresIn': ttl}


def verify_session_token(token: str,
                         secret: Optional[str] = None) -> Optional[str]:
    """The identity a commerce session token names, or None."""
    secret = secret if secret is not None else bindings.commerce_session_secret()
    if not token or not secret:
        return None
    import jwt
    try:
        claims = jwt.decode(token, _signing_key(secret), algorithms=['HS256'])
    except jwt.PyJWTError:
        return None
    if claims.get('scope') != SESSION_SCOPE:
        return None
    sub = claims.get('sub')
    return sub if isinstance(sub, str) and sub.startswith(
        bindings.MCGROCE_IDENTITY_PREFIX) else None


def resolve_identity(req=None) -> Optional[str]:
    """Who is calling: the Bearer token's user, never a body field.

    A commerce session token (McGroce shopper) is tried first because it needs
    no database; otherwise the token is a HARTOS login, resolved by
    integrations.social.auth.user_id_for_token (local JWT, hive JWT, or stored
    api_token).
    """
    req = req or request
    header = req.headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        return None
    token = header[7:].strip()
    identity = verify_session_token(token)
    if identity:
        return identity
    try:
        from integrations.social.auth import user_id_for_token
        return user_id_for_token(token)
    except Exception:
        logger.exception("commerce: token lookup failed")
        return None


# ─── Approvals (called from /api/agent/approval) ───────────────────

def is_commerce_action(action: str) -> bool:
    from integrations.ap2.ap2_mandate import parse_approval_action
    return bool(parse_approval_action(action)
                or commerce_tools.parse_merchant_action(action))


def handle_commerce_approval(action: str, approved: bool,
                             req=None) -> Tuple[Dict[str, Any], int]:
    """Decide an ``ap2_pay:<id>`` or ``merchant_onboard:<id>`` card.

    The approver is ``resolve_identity(req)``: whoever the caller's token
    says.  A ``user_id`` or ``approver_id`` in the body is ignored.
    """
    from integrations.ap2.ap2_mandate import decide_payment, parse_approval_action
    approver = resolve_identity(req)
    payment_id = parse_approval_action(action)
    if payment_id:
        payload, status = decide_payment(payment_id, approver, approved)
    else:
        request_id = commerce_tools.parse_merchant_action(action)
        if not request_id:
            return {'status': 'error', 'reason': 'not a commerce action'}, 400
        payload, status = commerce_tools.decide_merchant_onboarding(
            request_id, approver, approved)
    payload.setdefault('action', action)
    return payload, status


# ─── Routes ────────────────────────────────────────────────────────

@commerce_bp.route('/session', methods=['POST'])
def create_session():
    secret = bindings.commerce_session_secret()
    if not secret:
        return jsonify({'error': 'commerce sessions are not configured'}), 503
    presented = request.headers.get('X-Commerce-Secret', '')
    if not hmac.compare_digest(presented.encode('utf-8'),
                               secret.encode('utf-8')):
        return jsonify({'error': 'forbidden'}), 403
    body = request.get_json(silent=True) or {}
    customer = next((body[f] for f in _CUSTOMER_FIELDS
                     if body.get(f) not in (None, '')), None)
    if customer is None:
        return jsonify({'error': 'customer id required'}), 400
    return jsonify(issue_session_token(customer, secret=secret)), 200


def _identity_or_401():
    identity = resolve_identity()
    if not identity:
        return None, (jsonify({'error': 'authentication required'}), 401)
    return identity, None


def _tool_response(raw: str):
    data = json.loads(raw)
    return jsonify(data), (200 if data.get('success') else 502)


@commerce_bp.route('/products', methods=['GET'])
def products():
    uid, err = _identity_or_401()
    if err:
        return err
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'error': 'q required'}), 400
    return _tool_response(commerce_tools.search_products(
        uid, q, request.args.get('store_id', '')))


@commerce_bp.route('/cart', methods=['GET'])
def cart():
    uid, err = _identity_or_401()
    if err:
        return err
    return _tool_response(commerce_tools.view_cart(uid))


@commerce_bp.route('/cart/items', methods=['POST'])
def cart_add():
    uid, err = _identity_or_401()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if not body.get('product_id'):
        return jsonify({'error': 'product_id required'}), 400
    try:
        qty = int(body.get('quantity', 1))
    except (TypeError, ValueError):
        return jsonify({'error': 'quantity must be an integer'}), 400
    return _tool_response(commerce_tools.add_to_cart(
        uid, str(body['product_id']), qty, str(body.get('store_id') or '')))


@commerce_bp.route('/cart/items/<product_id>', methods=['DELETE'])
def cart_remove(product_id):
    uid, err = _identity_or_401()
    if err:
        return err
    return _tool_response(commerce_tools.remove_from_cart(uid, product_id))


@commerce_bp.route('/checkout', methods=['POST'])
def checkout():
    uid, err = _identity_or_401()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    return _tool_response(commerce_tools.checkout(
        uid, str(body.get('store_id') or '')))


@commerce_bp.route('/orders/<order_id>', methods=['GET'])
def order(order_id):
    uid, err = _identity_or_401()
    if err:
        return err
    return _tool_response(commerce_tools.track_order(uid, order_id))


@commerce_bp.route('/stream', methods=['GET'])
def stream():
    uid, err = _identity_or_401()
    if err:
        return err
    return jsonify(dict(bindings.event_stream_for(uid), user_id=uid)), 200
