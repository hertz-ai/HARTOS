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
The approver comes from the verified JWT on POST /api/agent/approval
(``ap2_pay:<payment_id>``), never from a tool argument.

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

# How long a person has to approve a cart, and then to check out with it.
# A stale approval must not authorize a payment an hour later.
DEFAULT_MANDATE_TTL_S = 15 * 60

_MANDATES_FILENAME = 'ap2_mandates.json'
_MANDATE_KEY_FILENAME = '.ap2_mandate_key'


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
                            description: str = '') -> CartMandate:
        """Create a pending CartMandate plus its APPROVAL_REQUIRED payment.

        Raises MandateError for an empty or malformed cart, a non-positive
        total, or a total over ``cap``.
        """
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
        payment = ledger.create_payment_request(
            amount=amount, currency=currency,
            description=description or f'{merchant} order',
            requester_agent_id=f'user:{user_id}',
            gateway=ledger.select_gateway(currency),
            require_approval=True,
            metadata={'kind': 'commerce_checkout', 'mandate_id': mandate_id,
                      'merchant': merchant, 'user_id': str(user_id),
                      'cart_hash': cart_hash},
        )
        m = CartMandate(
            mandate_id=mandate_id, user_id=str(user_id), merchant=merchant,
            cart_hash=cart_hash, amount=str(amount), currency=currency,
            payment_id=payment.payment_id, created_at=now,
            expires_at=now + ttl, intent=intent)
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
]
