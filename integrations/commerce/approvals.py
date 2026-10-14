"""
Commerce answers on POST /api/agent/approval.

The approval card an agent raises for a payment or a merchant/SKU draft
carries one of these actions:

    ap2_pay:<payment_id>          approve / decline an AP2 CartMandate
    merchant_onboard:<draft_id>   create the drafted McGroce store
    merchant_sku:<draft_id>       create the drafted McGroce product

The approver is the VERIFIED JWT identity of the caller -- never a body
field -- and must be the mandate's / draft's owner, else 403.  A capability
consent is a different noun, so these actions are answered here and never
reach ConsentService.

hart_intelligence_entry.agent_approval calls ``answer_commerce_approval``
for any action ``is_commerce_action`` accepts; this module is the one
implementation.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from integrations.commerce import COMMERCE_ACTION_PREFIXES  # one definition

logger = logging.getLogger(__name__)

_DRAFT_KIND = {'merchant_onboard': 'merchant', 'merchant_sku': 'sku'}

Reply = Tuple[Dict[str, Any], int]


def is_commerce_action(action) -> bool:
    return str(action or '').strip().lower().startswith(COMMERCE_ACTION_PREFIXES)


def approver_from_request(req) -> Optional[str]:
    """Whose Bearer token this is, or None -- never a body field.

    A HARTOS JWT (what /api/commerce/session issues and /chat accepts) is
    decoded locally; anything else goes through
    integrations.social.auth.user_id_for_token (a hive JWT or a stored
    api_token, e.g. a cloud login on a desktop).  A node API key names no
    person, so it answers None.
    """
    auth = req.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    token = auth[7:].strip()
    try:
        from integrations.social.auth import decode_jwt
        uid = (decode_jwt(token) or {}).get('user_id')
        if uid:
            return str(uid)
    except Exception as e:
        logger.debug(f'commerce approval: token not decodable: {e}')
    try:
        from integrations.social.auth import user_id_for_token
        return user_id_for_token(token)
    except Exception as e:
        logger.debug(f'commerce approval: token lookup failed: {e}')
        return None


def answer_commerce_approval(action: str, approved: bool,
                             approver_id: Optional[str]) -> Reply:
    action = str(action or '').strip()
    prefix, _, ref = action.partition(':')
    prefix = prefix.lower()
    ref = ref.strip()
    if not approver_id:
        return {'status': 'error', 'action': action,
                'reason': 'sign in to answer this request'}, 401
    if not ref:
        return {'status': 'error', 'action': action,
                'reason': 'malformed action'}, 400
    if prefix == 'ap2_pay':
        return _answer_payment(action, ref, approved, approver_id)
    if prefix in _DRAFT_KIND:
        return _answer_draft(action, prefix, ref, approved, approver_id)
    return {'status': 'error', 'action': action,
            'reason': 'not a commerce action'}, 400


def _answer_payment(action, payment_id, approved, approver_id) -> Reply:
    # Importing commerce_tools registers the McGroce checkout settler, so an
    # approval here pays AND places the order.
    import integrations.commerce.commerce_tools  # noqa: F401
    from integrations.ap2.ap2_mandate import decide_payment
    payload, code = decide_payment(payment_id, approver_id, approved)
    payload['action'] = action
    return payload, code


def _answer_draft(action, prefix, draft_id, approved, approver_id) -> Reply:
    from integrations.commerce.drafts import get_draft_store
    kind = _DRAFT_KIND[prefix]
    drafts = get_draft_store()
    d = drafts.get(draft_id)
    if d is None or d['kind'] != kind:
        return {'status': 'error', 'action': action,
                'reason': 'draft not found'}, 404
    if d['user_id'] != str(approver_id):
        return {'status': 'error', 'action': action,
                'reason': 'this request belongs to someone else'}, 403
    if not approved:
        ok, reason = drafts.reject(draft_id, kind, approver_id)
        return {'status': 'denied', 'action': action, 'applied': ok,
                'reason': reason}, 200
    claimed, reason = drafts.claim(draft_id, kind, approver_id)
    if claimed is None:
        return {'status': 'error', 'action': action, 'reason': reason}, 409
    from integrations.commerce.mcgroce_client import get_client
    client = get_client()
    res = (client.onboard_merchant(claimed['payload']) if kind == 'merchant'
           else client.create_product(claimed['payload']))
    if not res['success']:
        drafts.finish(draft_id, 'failed', res['error'])
        return {'status': 'error', 'action': action,
                'reason': f"McGroce refused it: {res['error']}"}, 502
    drafts.finish(draft_id, 'submitted', res.get('data'))
    name = claimed['payload'].get('displayName') or claimed['payload'].get('name')
    _push(approver_id, {'type': 'notification',
                        'title': 'Store created' if kind == 'merchant' else 'Product added',
                        'message': (f'“{name}” is live. Check your email for '
                                    'the link to set your password.'
                                    if kind == 'merchant'
                                    else f'“{name}” is now in your store.')})
    return {'status': 'approved', 'action': action, 'applied': True,
            'result': res.get('data')}, 200


def _push(user_id, component) -> None:
    from integrations.commerce.commerce_tools import push_fragment
    push_fragment(user_id, component)
