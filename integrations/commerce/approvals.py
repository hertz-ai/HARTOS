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

logger = logging.getLogger(__name__)

COMMERCE_ACTION_PREFIXES = ('ap2_pay:', 'merchant_onboard:', 'merchant_sku:')
_DRAFT_KIND = {'merchant_onboard': 'merchant', 'merchant_sku': 'sku'}

Reply = Tuple[Dict[str, Any], int]


def is_commerce_action(action) -> bool:
    return str(action or '').strip().lower().startswith(COMMERCE_ACTION_PREFIXES)


def approver_from_request(req) -> Optional[str]:
    """user_id from the request's Bearer JWT, or None (no/invalid token, or
    a node API key, which names no person)."""
    auth = req.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    try:
        from integrations.social.auth import decode_jwt
        payload = decode_jwt(auth[7:].strip()) or {}
    except Exception as e:
        logger.debug(f'commerce approval: token not decodable: {e}')
        return None
    uid = payload.get('user_id')
    return str(uid) if uid else None


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
    from integrations.ap2.ap2_mandate import get_mandate_store
    store = get_mandate_store()
    m = store.find_by_payment_id(payment_id)
    if m is None:
        return {'status': 'error', 'action': action,
                'reason': 'payment not found'}, 404
    if m.user_id != str(approver_id):
        logger.warning(f'ap2_pay: {approver_id} tried to answer '
                       f'{m.user_id}\'s payment {payment_id}')
        return {'status': 'error', 'action': action,
                'reason': 'this payment belongs to someone else'}, 403
    if approved:
        ok, reason = store.approve(m.mandate_id, approver_id)
        if not ok:
            return {'status': 'error', 'action': action, 'reason': reason,
                    'mandate_id': m.mandate_id}, 409
        _push(approver_id, {'type': 'notification',
                            'title': 'Payment approved',
                            'message': f'₹{m.amount} to McGroce. Placing your order…'})
        return {'status': 'approved', 'action': action, 'applied': True,
                'mandate_id': m.mandate_id, 'payment_id': payment_id}, 200
    ok, reason = store.reject(m.mandate_id, approver_id)
    return {'status': 'denied', 'action': action, 'applied': ok,
            'reason': reason, 'mandate_id': m.mandate_id}, 200


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
