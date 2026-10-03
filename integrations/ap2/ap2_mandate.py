"""
AP2 mandates -- human-in-the-loop authorization for agentic payments.

The PaymentLedger (ap2_protocol.py) moves money; it has no idea WHO agreed to
WHAT.  A mandate is that agreement, bound to the exact thing being paid for:

  IntentMandate  the person's stated scope for this purchase: merchant,
                 spend cap, currency, expiry.  Checked when the cart
                 mandate is created -- a cart over the cap is never offered
                 for approval.
  CartMandate    ONE exact cart (canonical_cart_hash), ONE amount, ONE
                 PaymentRequest created in APPROVAL_REQUIRED.  Only the
                 owning person can approve it (approve() refuses any other
                 approver), and checkout re-hashes the LIVE cart and refuses
                 on any drift (verify_for_checkout), so an agent cannot get
                 approval for one cart and then pay for another.

Lifecycle: pending -> approved -> consumed, or pending -> rejected / expired.
The approver comes from the verified token on POST /api/agent/approval
(``ap2_pay:<payment_id>``), never from a tool argument: ``decide_payment`` is
the ONE place a person's answer is applied.  An approval SETTLES the payment
right away, through the settler registered for the mandate's ``kind``
(``register_settler``; McGroce checkout registers its own, which re-checks the
live cart and places the order), so nobody has to come back and "finish".

This module is the single writer of ``ap2_mandates.json`` under
core.platform_paths.get_agent_data_dir().  Each record carries an HMAC so a
hand-edited file (flipping a mandate to approved) fails verification.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

MANDATE_STATUSES = ('pending', 'approved', 'rejected', 'expired', 'consumed')

#: Approver ids that never name a person.  PaymentLedger.authorize_payment
#: refuses them outright (and an agent approving its own request).
NON_HUMAN_APPROVERS = frozenset({'', 'system', 'assistant', 'agent', 'helper',
                                 'executor', 'llm'})

#: A ``user:<id>`` id names a logged-in PERSON (the API-tier upgrade and
#: ``hart pay`` paths request and authorize as the same person).
PERSON_ID_PREFIX = 'user:'

APPROVAL_ACTION_PREFIX = 'ap2_pay:'
AP2_AGENT_ID = 'ap2_payments'
KIND_GENERIC = 'generic'

# How long a person has to approve a cart, and then to check out with it.
# A stale approval must not authorize a payment an hour later.
DEFAULT_MANDATE_TTL_S = 15 * 60

_MANDATES_FILENAME = 'ap2_mandates.json'
_MANDATE_KEY_FILENAME = '.ap2_mandate_key'


def _own_gateway_urls() -> Dict[str, str]:
    """This node's redirect/callback URLs from its configured public base
    (integrations.channels.oauth_api._public_base_url: HARTOS_PUBLIC_URL, else
    the live request's root).  Empty when neither exists -- the gateway
    default then applies."""
    try:
        from integrations.channels.oauth_api import _public_base_url
        base = _public_base_url()
    except Exception as e:
        logger.debug(f'ap2: no public base for gateway urls: {e}')
        return {}
    if not base:
        return {}
    return {'redirect_url': base + '/',
            'callback_url': base + '/api/v1/intelligence/phonepe/callback'}


class MandateError(ValueError):
    """A mandate could not be created (bad cart, over the cap)."""


def _money(value) -> Decimal:
    """Parse a price: a number, a numeric string, or a Money bean
    ``{amount, currency}`` (how Broadleaf serialises Money)."""
    if isinstance(value, dict):
        value = value.get('amount')
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        raise MandateError(f'not a price: {value!r}')


def canonical_cart_hash(cart: Dict[str, Any]) -> str:
    """sha256 of the cart's payable content, independent of line order.

    ``cart`` is ``{'lines': [{'sku_id', 'qty', 'unit_price'}], 'total',
    'currency'}``.  Only what changes what is paid is hashed -- which SKU,
    how many, at what unit price, the total and the currency -- so display
    fields (names, images) never break a match, and any change that does
    alter the payment always does.
    """
    try:
        lines = sorted(
            (str(line['sku_id']), int(line['qty']),
             str(_money(line['unit_price'])))
            for line in (cart.get('lines') or []))
    except (KeyError, TypeError, ValueError) as e:
        raise MandateError(f'malformed cart line: {e}')
    payload = {
        'lines': lines,
        'total': str(_money(cart.get('total'))),
        'currency': str(cart.get('currency') or '').upper(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()


@dataclass
class IntentMandate:
    user_id: str
    merchant: str
    cap: Optional[str]
    currency: str
    expires_at: float


@dataclass
class CartMandate:
    mandate_id: str
    user_id: str
    merchant: str
    cart_hash: str
    amount: str
    currency: str
    payment_id: str
    status: str = 'pending'
    approver_id: Optional[str] = None
    approved_at: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    intent: Optional[Dict[str, Any]] = None
    kind: str = KIND_GENERIC
    description: str = ''
    sig: str = ''

    def signed_fields(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop('sig', None)
        return d


class MandateStore:
    """Single writer of the mandate file.  Thread-safe."""

    def __init__(self, path: Optional[str] = None, ledger=None,
                 key: Optional[bytes] = None):
        if path is None:
            from core.platform_paths import get_agent_data_dir
            path = os.path.join(get_agent_data_dir(), _MANDATES_FILENAME)
        self.path = path
        self._ledger = ledger
        self._key = key
        self._lock = threading.Lock()
        self._mandates: Dict[str, CartMandate] = {}
        self._load()

    # ── collaborators ────────────────────────────────────────────
    @property
    def ledger(self):
        if self._ledger is None:
            from integrations.ap2.ap2_protocol import get_payment_ledger
            self._ledger = get_payment_ledger()
        return self._ledger

    def _signing_key(self) -> bytes:
        if self._key is None:
            self._key = _load_or_create_key(
                os.path.join(os.path.dirname(self.path), _MANDATE_KEY_FILENAME))
        return self._key

    def _sign(self, m: CartMandate) -> str:
        body = json.dumps(m.signed_fields(), sort_keys=True, default=str)
        return hmac.new(self._signing_key(), body.encode('utf-8'),
                        hashlib.sha256).hexdigest()

    # ── persistence (caller holds the lock for _save) ────────────
    def _load(self) -> None:
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            logger.warning(f'ap2 mandates: could not read {self.path}: {e}')
            return
        for mid, rec in (raw.get('mandates') or {}).items():
            try:
                self._mandates[mid] = CartMandate(**rec)
            except TypeError as e:
                logger.warning(f'ap2 mandates: skipped malformed {mid}: {e}')

    def _save(self) -> None:
        from core.file_cache import atomic_json_write
        atomic_json_write(self.path, {
            'mandates': {k: asdict(v) for k, v in self._mandates.items()},
            'last_updated': time.time(),
        })

    def _expire_if_due(self, m: CartMandate, now: float) -> None:
        if m.status in ('pending', 'approved') and now >= m.expires_at:
            if self._money_in_flight(m.payment_id):
                # A redirect gateway (PhonePe) is holding or has taken the
                # money; its callback finishes this mandate, not the clock.
                return
            m.status = 'expired'
            m.sig = self._sign(m)
            try:
                self.ledger.cancel_payment(m.payment_id, 'ap2_mandate',
                                           'mandate expired')
            except Exception as e:
                logger.debug(f'ap2 mandates: cancel on expiry failed: {e}')

    def _money_in_flight(self, payment_id: str) -> bool:
        from integrations.ap2.ap2_protocol import PaymentStatus
        try:
            p = self.ledger.get_payment(payment_id)
        except Exception:
            return False
        return p is not None and p.status in (PaymentStatus.PROCESSING,
                                              PaymentStatus.COMPLETED)

    # ── public API ───────────────────────────────────────────────
    def create_cart_mandate(self, user_id: str, merchant: str,
                            cart: Dict[str, Any], cap=None,
                            ttl_s: Optional[int] = None,
                            description: str = '',
                            kind: str = KIND_GENERIC,
                            requester_agent_id: Optional[str] = None
                            ) -> CartMandate:
        """Create a pending CartMandate plus its APPROVAL_REQUIRED payment.

        ``requester_agent_id`` is the AGENT asking (the ledger refuses it as
        an approver); it defaults to ``ap2:<merchant>``, never the owner.

        Raises MandateError for an empty or malformed cart, a non-positive
        total, or a total over ``cap``.
        """
        # Expiry is lazy; each new checkout also expires the old ones, so a
        # mandate nobody reads again does not keep its payment AUTHORIZED.
        self.sweep_expired()
        if not user_id:
            raise MandateError('a mandate needs an owner')
        if not cart.get('lines'):
            raise MandateError('the cart is empty')
        amount = _money(cart.get('total'))
        if amount <= 0:
            raise MandateError('nothing to pay')
        currency = str(cart.get('currency') or 'INR').upper()
        ttl = int(ttl_s if ttl_s is not None else DEFAULT_MANDATE_TTL_S)
        now = time.time()
        intent = None
        if cap is not None:
            cap_d = _money(cap)
            if amount > cap_d:
                raise MandateError(
                    f'cart total {amount} {currency} is over the cap '
                    f'{cap_d} {currency}')
            intent = asdict(IntentMandate(
                user_id=str(user_id), merchant=merchant, cap=str(cap_d),
                currency=currency, expires_at=now + ttl))
        cart_hash = canonical_cart_hash(cart)
        mandate_id = f'mdt_{uuid.uuid4().hex}'
        ledger = self.ledger
        meta = {'kind': kind, 'mandate_id': mandate_id,
                'merchant': merchant, 'user_id': str(user_id),
                'cart_hash': cart_hash}
        # A redirect gateway (PhonePe) returns the buyer and posts its
        # confirmation to THIS node, which owns the checkout; without these
        # the gateway defaults to hevolve.ai and the order is never placed.
        meta.update(_own_gateway_urls())
        payment = ledger.create_payment_request(
            amount=amount, currency=currency,
            description=description or f'{merchant} order',
            requester_agent_id=requester_agent_id or f'ap2:{merchant}',
            gateway=ledger.select_gateway(currency),
            require_approval=True,
            metadata=meta,
        )
        m = CartMandate(
            mandate_id=mandate_id, user_id=str(user_id), merchant=merchant,
            cart_hash=cart_hash, amount=str(amount), currency=currency,
            payment_id=payment.payment_id, created_at=now,
            expires_at=now + ttl, intent=intent, kind=kind,
            description=description or f'{merchant} order')
        m.sig = self._sign(m)
        with self._lock:
            self._mandates[mandate_id] = m
            self._save()
        return m

    def get(self, mandate_id: str) -> Optional[CartMandate]:
        with self._lock:
            m = self._mandates.get(mandate_id)
            if m is not None:
                self._expire_if_due(m, time.time())
            return m

    def find_by_payment_id(self, payment_id: str) -> Optional[CartMandate]:
        with self._lock:
            for m in self._mandates.values():
                if m.payment_id == payment_id:
                    self._expire_if_due(m, time.time())
                    return m
        return None

    def approve(self, mandate_id: str, approver_id: str) -> Tuple[bool, str]:
        """The owner approves: authorize the payment, mark approved."""
        with self._lock:
            m = self._mandates.get(mandate_id)
            if m is None:
                return False, 'mandate not found'
            self._expire_if_due(m, time.time())
            if not hmac.compare_digest(m.sig, self._sign(m)):
                return False, 'mandate signature invalid'
            if str(approver_id) != m.user_id:
                return False, 'only the owner can approve this payment'
            if m.status != 'pending':
                return False, f'mandate is {m.status}'
            if not self.ledger.authorize_payment(m.payment_id,
                                                 f'user:{approver_id}'):
                return False, 'payment could not be authorized'
            m.status = 'approved'
            m.approver_id = str(approver_id)
            m.approved_at = time.time()
            m.sig = self._sign(m)
            self._save()
            return True, 'approved'

    def reject(self, mandate_id: str, approver_id: str) -> Tuple[bool, str]:
        with self._lock:
            m = self._mandates.get(mandate_id)
            if m is None:
                return False, 'mandate not found'
            if str(approver_id) != m.user_id:
                return False, 'only the owner can decline this payment'
            if m.status != 'pending':
                return False, f'mandate is {m.status}'
            m.status = 'rejected'
            m.approver_id = str(approver_id)
            m.sig = self._sign(m)
            self.ledger.cancel_payment(m.payment_id, f'user:{approver_id}',
                                       'declined by the owner')
            self._save()
            return True, 'rejected'

    def withdraw(self, mandate_id: str, reason: str) -> bool:
        """approved -> rejected, and the payment is cancelled.

        For an approval that can no longer be honoured (the cart changed
        after the person approved it).  Nothing was charged, so the payment
        must not stay AUTHORIZED for something else to take; the person
        approves a fresh mandate instead.
        """
        with self._lock:
            m = self._mandates.get(mandate_id)
            if m is None or m.status != 'approved':
                return False
            if self._money_in_flight(m.payment_id):
                return False  # the gateway owns it now
            m.status = 'rejected'
            m.sig = self._sign(m)
            self.ledger.cancel_payment(m.payment_id, 'ap2_mandate', reason)
            self._save()
            return True

    def sweep_expired(self) -> int:
        """Expire every due pending/approved mandate and cancel its payment.

        Expiry is otherwise lazy (it runs only when a mandate is read), so a
        mandate nobody reads again would leave its payment AUTHORIZED past
        its expiry.  Returns how many were expired.
        """
        now = time.time()
        expired = 0
        with self._lock:
            for m in self._mandates.values():
                was = m.status
                self._expire_if_due(m, now)
                if m.status != was:
                    expired += 1
            if expired:
                self._save()
        return expired

    def verify_for_checkout(self, mandate_id: str, user_id: str,
                            current_cart: Dict[str, Any]) -> Tuple[bool, str]:
        """(ok, reason).  Refuses unless the mandate exists, is the caller's,
        is approved, unexpired, untampered, its payment is AUTHORIZED, and the
        LIVE cart hashes to exactly what the person approved."""
        from integrations.ap2.ap2_protocol import PaymentStatus
        with self._lock:
            m = self._mandates.get(mandate_id)
            if m is None:
                return False, 'mandate not found'
            self._expire_if_due(m, time.time())
            if not hmac.compare_digest(m.sig, self._sign(m)):
                return False, 'mandate signature invalid'
            if m.user_id != str(user_id):
                return False, 'mandate belongs to another user'
            if m.status != 'approved':
                return False, f'mandate is {m.status}, not approved'
            try:
                live_hash = canonical_cart_hash(current_cart)
            except MandateError as e:
                return False, f'cart unreadable: {e}'
            if not hmac.compare_digest(live_hash, m.cart_hash):
                return False, 'cart changed since it was approved'
            payment_id = m.payment_id
        payment = self.ledger.get_payment(payment_id)
        if payment is None or payment.status != PaymentStatus.AUTHORIZED:
            return False, 'payment is not authorized'
        return True, 'ok'

    def consume(self, mandate_id: str) -> bool:
        """approved -> consumed.  A consumed mandate never pays again."""
        with self._lock:
            m = self._mandates.get(mandate_id)
            if m is None or m.status != 'approved':
                return False
            m.status = 'consumed'
            m.sig = self._sign(m)
            self._save()
            return True


# ── the approval card and the one decision point ─────────────────

def approval_action(payment_id: str) -> str:
    return f'{APPROVAL_ACTION_PREFIX}{payment_id}'


def parse_approval_action(action) -> Optional[str]:
    """The payment id an ``ap2_pay:<id>`` action names, else None."""
    action = str(action or '').strip()
    if not action.lower().startswith(APPROVAL_ACTION_PREFIX):
        return None
    return action[len(APPROVAL_ACTION_PREFIX):].strip() or None


def push_card(user_id, component: Dict[str, Any]) -> bool:
    """One Liquid UI fragment to ``user_id`` (best effort, never raises)."""
    try:
        from integrations.agent_engine.liquid_ui_service import push_agent_ui
        return bool(push_agent_ui(AP2_AGENT_ID, component,
                                  user_id=str(user_id) if user_id else None))
    except Exception as e:
        logger.debug(f'ap2: card {component.get("type")} not shown: {e}')
        return False


def approval_card(m: CartMandate, description: Optional[str] = None) -> Dict[str, Any]:
    return {'type': 'approval', 'agent_id': AP2_AGENT_ID,
            'action': approval_action(m.payment_id),
            'description': description or f'Pay {m.amount} {m.currency}: {m.description}',
            'options': ['Approve', 'Decline'],
            'amount': m.amount, 'currency': m.currency,
            'payment_id': m.payment_id}


def request_human_approval(payment_id: str, store: Optional[MandateStore] = None
                           ) -> Dict[str, Any]:
    """Put a payment's approval card in front of its owner.  What the
    agent-side ``authorize_payment`` tool does: it can ask, never approve."""
    store = store or get_mandate_store()
    m = store.find_by_payment_id(payment_id)
    if m is None:
        return {'success': False, 'payment_id': payment_id,
                'error': 'this payment has no owner to ask; it cannot be '
                         'approved by an agent'}
    if m.status != 'pending':
        return {'success': False, 'payment_id': payment_id,
                'error': f'mandate is {m.status}'}
    shown = push_card(m.user_id, approval_card(m))
    return {'success': True, 'payment_id': payment_id,
            'status': 'approval_required', 'approval_card_shown': shown,
            'message': 'Waiting for the person to approve this payment. '
                       'Agents cannot approve payments; do not retry.'}


_settlers: Dict[str, Any] = {}
_settlers_lock = threading.Lock()


def register_settler(kind: str, fn) -> None:
    """``fn(mandate) -> dict`` settles an APPROVED mandate of ``kind``.
    Without one, settling is ledger.process_payment."""
    with _settlers_lock:
        _settlers[kind] = fn


def settle(m: CartMandate, store: Optional[MandateStore] = None) -> Dict[str, Any]:
    """Take the money for an approved mandate (and whatever its kind does)."""
    with _settlers_lock:
        fn = _settlers.get(m.kind)
    if fn is not None:
        return fn(m)
    store = store or get_mandate_store()
    result = store.ledger.process_payment(m.payment_id, settler=True)
    if result.get('success'):
        store.consume(m.mandate_id)
    payment = store.ledger.get_payment(m.payment_id)
    push_card(m.user_id, {
        'type': 'payment_status',
        'status': ('completed' if result.get('success') else
                   'processing' if result.get('status') == 'redirect_required'
                   else 'failed'),
        'amount': m.amount, 'currency': m.currency,
        'method': payment.gateway.value if payment and payment.gateway else None,
        'transaction_id': result.get('transaction_id'),
        'redirect_url': result.get('redirect_url')})
    return result


def decide_payment(payment_id: str, approver_id: Optional[str], approved: bool,
                   store: Optional[MandateStore] = None) -> Tuple[Dict[str, Any], int]:
    """Apply a person's answer to ``ap2_pay:<payment_id>``.

    ``approver_id`` MUST come from the caller's verified token (the route
    resolves it), never the request body.  Only the mandate's owner may
    answer.  Approve -> authorize -> settle; decline -> reject + cancel.
    Returns ``(payload, http_status)``.
    """
    action = approval_action(payment_id)
    if not approver_id:
        return {'status': 'error', 'action': action,
                'reason': 'sign in to answer this request'}, 401
    store = store or get_mandate_store()
    m = store.find_by_payment_id(payment_id)
    if m is None:
        return {'status': 'error', 'action': action,
                'reason': 'payment not found'}, 404
    if m.user_id != str(approver_id):
        logger.warning(f'ap2_pay: {approver_id} tried to answer '
                       f'{m.user_id}\'s payment {payment_id}')
        return {'status': 'error', 'action': action,
                'reason': 'this payment belongs to someone else'}, 403
    if not approved:
        ok, reason = store.reject(m.mandate_id, approver_id)
        if ok:
            push_card(m.user_id, {'type': 'payment_status', 'status': 'cancelled',
                                  'amount': m.amount, 'currency': m.currency})
        return {'status': 'denied', 'action': action, 'applied': ok,
                'reason': reason, 'mandate_id': m.mandate_id}, 200
    ok, reason = store.approve(m.mandate_id, approver_id)
    if not ok:
        return {'status': 'error', 'action': action, 'reason': reason,
                'mandate_id': m.mandate_id}, 409
    try:
        result = settle(store.get(m.mandate_id), store)
    except Exception as e:
        logger.exception(f'ap2_pay: settling {payment_id} raised')
        result = {'success': False, 'error': str(e)}
    return {'status': 'approved', 'action': action, 'applied': True,
            'mandate_id': m.mandate_id, 'payment_id': payment_id,
            'result': result}, 200


def _load_or_create_key(path: str) -> bytes:
    """Node-local HMAC key for mandate records (0600 where supported)."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            key = f.read().strip()
        if len(key) >= 64:
            return bytes.fromhex(key)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f'ap2 mandates: key unreadable ({e}); regenerating')
    key = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(key)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except (OSError, NotImplementedError):
            pass
    except OSError as e:
        logger.warning(f'ap2 mandates: key not persisted ({e}); '
                       f'mandates will not survive a restart')
    return bytes.fromhex(key)


_store: Optional[MandateStore] = None
_store_lock = threading.Lock()


def get_mandate_store() -> MandateStore:
    """Process-wide MandateStore (lazy: no file I/O at import)."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = MandateStore()
    return _store


__all__ = [
    'MandateError', 'IntentMandate', 'CartMandate', 'MandateStore',
    'canonical_cart_hash', 'get_mandate_store', 'DEFAULT_MANDATE_TTL_S',
    'NON_HUMAN_APPROVERS', 'PERSON_ID_PREFIX', 'approval_action', 'parse_approval_action',
    'request_human_approval', 'register_settler', 'settle', 'decide_payment',
]
