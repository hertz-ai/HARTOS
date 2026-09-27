"""
AP2 mandates: a person's consent to one specific payment.

An agent may ASK for money; only the person who owns the payment may say yes.
Before this module, the LLM was handed an ``authorize_payment(payment_id,
approver_id="system")`` tool and could approve its own request in the next
turn.  A mandate closes that structurally, not by prompt:

  * The agent's ``request_payment`` stamps a MANDATE on the payment: the owner,
    the amount, the currency, the items, hashed (sha256 over canonical JSON).
  * ``PaymentLedger.authorize_payment`` refuses a mandate-bearing payment unless
    ``verify_approval`` holds: an approval record signed with this node's HMAC
    secret (core.node_secret), over the mandate hash, the payment id and the
    approver, where the approver IS the mandate's owner.
  * The only writer of that record is ``decide_payment``, which the
    /api/agent/approval route calls with the identity resolved from the
    caller's Bearer token.  No tool, prompt or body field can produce it.

So an approval cannot be forged by the agent, replayed onto another payment,
or stretched to a different amount, and the approver is whoever the token says,
never a string an agent wrote.

Post-approval work is plugged in by payment ``kind`` (``register_payment_hook``),
so the McGroce order flow can confirm its order without this module importing
commerce code.
"""

import hashlib
import hmac
import json
import logging
import threading
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MANDATE_KEY = 'mandate'
APPROVAL_KEY = 'mandate_approval'
APPROVAL_ACTION_PREFIX = 'ap2_pay:'
AP2_AGENT_ID = 'ap2_payments'

#: Approver ids that are never a person.  The ledger refuses them outright.
NON_HUMAN_APPROVERS = frozenset({'', 'system', 'assistant', 'agent', 'helper',
                                 'executor', 'llm'})

_hooks: Dict[str, List[Callable]] = {}
_hooks_lock = threading.Lock()


# ─── Mandate content ───────────────────────────────────────────────

def canonical_hash(content: Dict[str, Any]) -> str:
    """sha256 over canonical JSON (sorted keys, no whitespace)."""
    raw = json.dumps(content, sort_keys=True, separators=(',', ':'),
                     default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def build_mandate(user_id: Optional[str], amount, currency: str,
                  description: str, kind: str = 'generic',
                  items: Optional[List[Dict[str, Any]]] = None,
                  merchant: Optional[str] = None,
                  refs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The cart/intent a person is asked to approve, with its hash."""
    content = {
        'user_id': str(user_id) if user_id not in (None, '') else None,
        'amount': str(Decimal(str(amount))),
        'currency': str(currency).upper(),
        'description': description,
        'kind': kind,
        'items': items or [],
        'merchant': merchant,
        'refs': refs or {},
        'created_at': datetime.now().isoformat(),
    }
    content['hash'] = canonical_hash(content)
    return content


def mandate_is_intact(mandate: Optional[Dict[str, Any]]) -> bool:
    """True if the mandate's content still hashes to its recorded hash."""
    if not isinstance(mandate, dict) or not mandate.get('hash'):
        return False
    body = {k: v for k, v in mandate.items() if k != 'hash'}
    return hmac.compare_digest(canonical_hash(body), str(mandate['hash']))


def _node_key() -> bytes:
    from core.node_secret import get_hmac_secret
    return get_hmac_secret().encode('utf-8')


def _signature(mandate_hash: str, payment_id: str, approver_id: str,
               approved_at: str) -> str:
    msg = '|'.join(('ap2-mandate-v1', mandate_hash, payment_id,
                    approver_id, approved_at))
    return hmac.new(_node_key(), msg.encode('utf-8'),
                    hashlib.sha256).hexdigest()


def _approval_record(payment, approver_id: str) -> Dict[str, Any]:
    mandate = payment.metadata[MANDATE_KEY]
    approved_at = datetime.now().isoformat()
    return {
        'approver_id': approver_id,
        'approved_at': approved_at,
        'mandate_hash': mandate['hash'],
        'signature': _signature(mandate['hash'], payment.payment_id,
                                approver_id, approved_at),
    }


def verify_approval(payment, approver_id: str) -> bool:
    """May ``approver_id`` authorize this mandate-bearing payment?

    Every condition must hold: the mandate is intact and still describes this
    payment's amount and currency, its owner is ``approver_id``, and the
    approval record for it carries this node's signature.
    """
    meta = getattr(payment, 'metadata', None) or {}
    mandate = meta.get(MANDATE_KEY)
    approval = meta.get(APPROVAL_KEY)
    if not mandate_is_intact(mandate) or not isinstance(approval, dict):
        return False
    if not mandate.get('user_id') or str(mandate['user_id']) != str(approver_id):
        return False
    if (Decimal(mandate['amount']) != Decimal(str(payment.amount))
            or mandate['currency'] != payment.currency):
        return False
    if (approval.get('approver_id') != approver_id
            or approval.get('mandate_hash') != mandate['hash']):
        return False
    try:
        expected = _signature(mandate['hash'], payment.payment_id,
                              approver_id, str(approval.get('approved_at')))
    except Exception:
        logger.exception("verify_approval: node secret unavailable")
        return False
    return hmac.compare_digest(expected, str(approval.get('signature', '')))


# ─── Approval action vocabulary (the card <-> /api/agent/approval) ─

def approval_action(payment_id: str) -> str:
    return f'{APPROVAL_ACTION_PREFIX}{payment_id}'


def parse_approval_action(action: str) -> Optional[str]:
    """The payment id an ``ap2_pay:<id>`` action names, else None."""
    action = str(action or '').strip()
    if not action.lower().startswith(APPROVAL_ACTION_PREFIX):
        return None
    pid = action[len(APPROVAL_ACTION_PREFIX):].strip()
    return pid or None


def approval_component(payment) -> Dict[str, Any]:
    """The ``approval`` card the person answers (AgentOverlayBridge props)."""
    mandate = (payment.metadata or {}).get(MANDATE_KEY) or {}
    return {
        'type': 'approval',
        'agent_id': AP2_AGENT_ID,
        'action': approval_action(payment.payment_id),
        'description': (f'Pay {payment.amount} {payment.currency}: '
                        f'{payment.description}'),
        'options': ['Approve', 'Deny'],
        'amount': str(payment.amount),
        'currency': payment.currency,
        'items': mandate.get('items') or [],
        'payment_id': payment.payment_id,
    }


def _push(component: Dict[str, Any], user_id: Optional[str]) -> bool:
    try:
        from integrations.agent_engine.liquid_ui_service import push_agent_ui
        return push_agent_ui(AP2_AGENT_ID, component, user_id=user_id)
    except Exception:
        logger.exception("ap2_mandate: UI push failed")
        return False


def request_human_approval(ledger, payment_id: str) -> Dict[str, Any]:
    """Move a payment to APPROVAL_REQUIRED and put the card in front of its
    owner.  What the agent's ``authorize_payment`` tool does now."""
    payment = ledger.get_payment(payment_id)
    if payment is None:
        return {'success': False, 'error': 'Payment not found'}
    if not ledger.request_approval(payment_id):
        return {'success': False, 'payment_id': payment_id,
                'error': f'Payment is {payment.status.value}, not awaiting approval'}
    owner = ((payment.metadata or {}).get(MANDATE_KEY) or {}).get('user_id')
    shown = _push(approval_component(payment), owner)
    return {
        'success': True,
        'payment_id': payment_id,
        'status': 'approval_required',
        'approval_card_shown': shown,
        'message': ('Waiting for the user to approve this payment. Agents '
                    'cannot authorize payments; do not retry authorization.'),
    }


# ─── Deciding ──────────────────────────────────────────────────────

def register_payment_hook(kind: str, fn: Callable) -> None:
    """Run ``fn(payment, outcome)`` after a person decides a payment of
    ``kind``; ``outcome`` is 'completed', 'failed' or 'denied'.  Idempotent."""
    with _hooks_lock:
        fns = _hooks.setdefault(kind, [])
        if fn not in fns:
            fns.append(fn)


def _run_hooks(payment, outcome: str) -> List[Any]:
    kind = ((payment.metadata or {}).get(MANDATE_KEY) or {}).get('kind') \
        or (payment.metadata or {}).get('kind') or 'generic'
    with _hooks_lock:
        fns = list(_hooks.get(kind, []))
    results = []
    for fn in fns:
        try:
            results.append(fn(payment, outcome))
        except Exception as e:
            logger.exception("ap2 payment hook %s failed", kind)
            results.append({'error': str(e)})
    return results


def decide_payment(payment_id: str, approver_id: Optional[str],
                   approved: bool, ledger=None) -> Tuple[Dict[str, Any], int]:
    """Apply a person's answer to a payment.  The ONE writer of approvals.

    ``approver_id`` must come from the caller's verified token (the route
    resolves it); it is never taken from the request body.  Returns
    ``(payload, http_status)``.
    """
    if ledger is None:
        from integrations.ap2.ap2_protocol import payment_ledger as ledger
    if not approver_id:
        return {'status': 'error', 'reason': 'authentication required'}, 401
    payment = ledger.get_payment(payment_id)
    if payment is None:
        return {'status': 'error', 'reason': 'payment not found'}, 404
    mandate = (payment.metadata or {}).get(MANDATE_KEY)
    if not mandate_is_intact(mandate):
        return {'status': 'error', 'reason': 'payment has no valid mandate'}, 409
    owner = mandate.get('user_id')
    if not owner:
        return {'status': 'error', 'reason': 'payment has no owner'}, 403
    if str(owner) != str(approver_id):
        logger.warning("ap2: %s tried to decide %s owned by %s", approver_id,
                       payment_id, owner)
        return {'status': 'error', 'reason': 'not your payment'}, 403

    from integrations.ap2.ap2_protocol import PaymentStatus
    if not approved:
        if not ledger.cancel_payment(payment_id, approver_id,
                                     'Denied by the user'):
            return {'status': 'error', 'payment_id': payment_id,
                    'reason': f'payment is {payment.status.value}'}, 409
        _push({'type': 'payment_status', 'status': 'cancelled',
               'amount': str(payment.amount), 'currency': payment.currency,
               'transaction_id': payment_id}, owner)
        _run_hooks(payment, 'denied')
        return {'status': 'denied', 'payment_id': payment_id}, 200

    if payment.status not in (PaymentStatus.PENDING,
                              PaymentStatus.APPROVAL_REQUIRED):
        return {'status': 'error', 'payment_id': payment_id,
                'reason': f'payment is {payment.status.value}'}, 409
    ledger.set_metadata(payment_id, APPROVAL_KEY,
                        _approval_record(payment, approver_id))
    if not ledger.authorize_payment(payment_id, approver_id):
        return {'status': 'error', 'payment_id': payment_id,
                'reason': 'authorization refused'}, 409
    result = ledger.process_payment(payment_id)
    payment = ledger.get_payment(payment_id)
    outcome = 'completed' if payment.status == PaymentStatus.COMPLETED \
        else 'failed'
    _push({'type': 'payment_status', 'status': outcome,
           'amount': str(payment.amount), 'currency': payment.currency,
           'method': payment.gateway.value if payment.gateway else None,
           'transaction_id': payment.gateway_transaction_id or payment_id},
          owner)
    hook_results = _run_hooks(payment, outcome)
    return {'status': 'approved', 'payment_id': payment_id,
            'payment_status': payment.status.value,
            'success': bool(result.get('success')),
            'error': result.get('error'),
            'hooks': hook_results}, 200
