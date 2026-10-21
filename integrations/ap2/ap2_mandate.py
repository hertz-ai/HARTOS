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

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import tempfile
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

# A redirect payment still PROCESSING this long after its mandate expired was
# abandoned or lost its callback (a PhonePe checkout session lasts minutes).
# The mandate is then released and a person is told; the payment itself is
# left as it is, since money may have moved.
PROCESSING_RECONCILE_AFTER_S = 24 * 3600

# begin_settle's answer when this same mandate is already being paid: the
# caller is a second settle of it and must leave it alone.
ALREADY_SETTLING = 'this payment is already being taken'

_MANDATES_FILENAME = 'ap2_mandates.json'
_MANDATE_KEY_FILENAME = '.ap2_mandate_key'


class MandateError(ValueError):
    """A mandate could not be created (bad cart, over the cap)."""


#: Why payments stop on a node whose mandate key cannot be used: the one
#: spelling the approval answer (503), the mandate route and the commerce
#: tools give the person.  The approval card stays open, so they can answer
#: again once the key reads.
PAYMENTS_PAUSED = ('payments are paused on this node because its payment key '
                   'cannot be read or saved; nothing was charged')


class MandateKeyError(MandateError):
    """The node's mandate key cannot be read or saved right now: nothing is
    signed until it can (see _load_or_create_key)."""

    def __init__(self, message: str = PAYMENTS_PAUSED):
        super().__init__(message)


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
    #: What the paid-for thing became (a McGroce order number), once known.
    order_ref: Optional[str] = None
    #: Which merchant-side cart this pays for (a McGroce cart's order id),
    #: fixed and signed when the mandate is created.
    cart_ref: Optional[str] = None

    def signed_fields(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop('sig', None)
        # Absent while unset, so a record written before these fields
        # existed still verifies.
        for optional in ('order_ref', 'cart_ref'):
            if d.get(optional) is None:
                d.pop(optional, None)
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
        # Mandates whose payment is being taken right now (begin_settle).
        self._settling: set = set()
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

    def _ready_to_sign(self) -> None:
        """Every change to a record is signed, so a node that cannot sign
        (MandateKeyError from the key file) must refuse BEFORE it cancels,
        creates or authorizes a payment or edits a record, in memory or on
        disk.  Called first by every state-changing method: the key is in
        hand, or the pause is answered with everything as it was."""
        self._signing_key()

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
            from integrations.ap2.ap2_protocol import PaymentStatus
            # A record edited outside this store keeps its broken signature
            # whatever it becomes: re-signing it would make the edit genuine.
            intact = self._intact(m)
            status = self._payment_status(m.payment_id)
            if status == PaymentStatus.COMPLETED:
                # The money was taken.  A generic mandate has no step that
                # consumes it, so it is used now; any other kind (a commerce
                # checkout) is consumed by its own order step, which may run
                # after the TTL and must still find it approved.
                if m.status == 'approved' and m.kind == KIND_GENERIC:
                    m.status = 'consumed'
                    if intact:
                        m.sig = self._sign(m)
                return
            if self._money_taken(m.payment_id):
                # FAILED, but the gateway took a different amount: kept as it
                # is, so its cart is never offered for payment again; a person
                # settles it.
                return
            if status == PaymentStatus.PROCESSING:
                # A redirect gateway (PhonePe) is holding the money; its
                # callback finishes this mandate, not the clock -- unless it
                # never came.
                if now < m.expires_at + PROCESSING_RECONCILE_AFTER_S:
                    return
                logger.error(f'MANUAL RECONCILIATION NEEDED: payment '
                             f'{m.payment_id} (mandate {m.mandate_id}) is still '
                             f'PROCESSING a day after the mandate expired; the '
                             f'mandate is released, the payment is left as is')
                m.status = 'expired'
                if intact:
                    m.sig = self._sign(m)
                return
            m.status = 'expired'
            if intact:
                m.sig = self._sign(m)
            try:
                self.ledger.cancel_payment(m.payment_id, 'ap2_mandate',
                                           'mandate expired')
            except Exception as e:
                logger.debug(f'ap2 mandates: cancel on expiry failed: {e}')

    def _payment_status(self, payment_id: str):
        try:
            p = self.ledger.get_payment(payment_id)
        except Exception:
            return None
        return p.status if p is not None else None

    def _money_taken(self, payment_id: str) -> bool:
        """The gateway took money for this payment (ap2_protocol.money_taken,
        the one reading)."""
        from integrations.ap2.ap2_protocol import money_taken
        try:
            p = self.ledger.get_payment(payment_id)
        except Exception:
            return False
        return money_taken(p)

    def _money_in_flight(self, payment_id: str) -> bool:
        from integrations.ap2.ap2_protocol import PaymentStatus
        return (self._payment_status(payment_id) == PaymentStatus.PROCESSING
                or self._money_taken(payment_id))

    # ── public API ───────────────────────────────────────────────
    def create_cart_mandate(self, user_id: str, merchant: str,
                            cart: Dict[str, Any], cap=None,
                            ttl_s: Optional[int] = None,
                            description: str = '',
                            kind: str = KIND_GENERIC,
                            requester_agent_id: Optional[str] = None,
                            cart_ref: Optional[str] = None
                            ) -> CartMandate:
        """Create a pending CartMandate plus its APPROVAL_REQUIRED payment.

        ``requester_agent_id`` is the AGENT asking (the ledger refuses it as
        an approver); it defaults to ``ap2:<merchant>``, never the owner.
        ``cart_ref`` names the merchant-side cart this pays for; it is signed
        with the mandate, so a paid mandate can never be spent on another.

        Raises MandateError for an empty or malformed cart, a non-positive
        total, or a total over ``cap``.
        """
        self._ready_to_sign()
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
        gateway = ledger.select_gateway(currency)
        # A redirect gateway (PhonePe) returns the buyer and posts its
        # confirmation to THIS node, which owns the checkout: its URLs are
        # stamped now, while the shopper's own request is in hand.  The
        # gateway resolves them again when it mints the checkout and refuses
        # without them (ap2_protocol.own_gateway_urls); nothing defaults to a
        # host that is not this node.  A synchronous gateway (Stripe, the dev
        # mock) needs none, so none is looked up and nothing is logged for it.
        from integrations.ap2.ap2_protocol import REDIRECT_GATEWAYS, own_gateway_urls
        if gateway in REDIRECT_GATEWAYS:
            meta.update(own_gateway_urls())
        payment = ledger.create_payment_request(
            amount=amount, currency=currency,
            description=description or f'{merchant} order',
            requester_agent_id=requester_agent_id or f'ap2:{merchant}',
            gateway=gateway,
            require_approval=True,
            metadata=meta,
        )
        m = CartMandate(
            mandate_id=mandate_id, user_id=str(user_id), merchant=merchant,
            cart_hash=cart_hash, amount=str(amount), currency=currency,
            payment_id=payment.payment_id, created_at=now,
            expires_at=now + ttl, intent=intent, kind=kind,
            description=description or f'{merchant} order',
            cart_ref=None if cart_ref in (None, '') else str(cart_ref))
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
            self._ready_to_sign()
            m = self._mandates.get(mandate_id)
            if m is None:
                return False, 'mandate not found'
            self._expire_if_due(m, time.time())
            if not self._intact(m):
                return False, 'mandate signature invalid'
            if str(approver_id) != m.user_id:
                return False, 'only the owner can approve this payment'
            if m.status != 'pending':
                return False, f'mandate is {m.status}'
            if not self._payment_matches(m, self.ledger.get_payment(m.payment_id)):
                return False, 'payment amount differs from what was shown'
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
            self._ready_to_sign()
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
            self._ready_to_sign()
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
            if not self._intact(m):
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
        if not self._payment_matches(m, payment):
            return False, 'payment amount differs from what was approved'
        return True, 'ok'

    @staticmethod
    def _payment_matches(m: 'CartMandate', payment) -> bool:
        """The ledger payment asks exactly what the signed mandate shows the
        person: same amount, same currency.  The ledger record is not signed,
        so this is what ties the two together."""
        try:
            return (_money(payment.amount) == _money(m.amount)
                    and str(payment.currency).upper() == str(m.currency).upper())
        except Exception:
            return False

    def _intact(self, m: CartMandate) -> bool:
        return hmac.compare_digest(m.sig, self._sign(m))

    def consume(self, mandate_id: str) -> bool:
        """approved -> consumed.  A consumed mandate never pays again.
        A record edited outside this store is never re-signed."""
        with self._lock:
            self._ready_to_sign()
            m = self._mandates.get(mandate_id)
            if m is None or m.status != 'approved' or not self._intact(m):
                return False
            m.status = 'consumed'
            m.sig = self._sign(m)
            self._save()
            return True

    def paid_as_signed(self, m: CartMandate, payment) -> bool:
        """``m`` is untampered and ``payment`` took exactly its amount and
        currency: all a signed word that it was paid for may claim."""
        with self._lock:
            return self._intact(m) and self._payment_matches(m, payment)

    def record_order(self, mandate_id: str, order_ref: str) -> bool:
        """Note what a consumed mandate's payment became (once)."""
        with self._lock:
            self._ready_to_sign()
            m = self._mandates.get(mandate_id)
            if m is None or m.status != 'consumed' or m.order_ref \
                    or not self._intact(m):
                return False
            m.order_ref = str(order_ref)
            m.sig = self._sign(m)
            self._save()
            return True

    def _paid_unordered(self, m: CartMandate, cart_ref: Optional[str]) -> bool:
        """``m`` took the money for ``cart_ref`` and its order is not placed:
        consumed with no order yet, or still approved because its order step
        never used it up (it failed midway, the record fails its signature,
        the gateway took another amount).  A mandate consumed before cart_ref
        existed is not counted: its order step ran under the old build, which
        kept no order_ref."""
        if m.cart_ref is None or m.cart_ref != cart_ref:
            return False
        if m.status == 'consumed':
            return not m.order_ref
        return m.status == 'approved' and self._money_taken(m.payment_id)

    def supersede(self, user_id: str, merchant: str, kind: str,
                  cart_ref: Optional[str]) -> Tuple[bool, str]:
        """Before a new checkout: void the owner's earlier live mandates of
        this merchant and kind, so two approval cards can never both be paid.
        (ok, reason).

        Refused when an earlier payment can no longer be stopped: one is
        being taken right now, one is with the gateway (PROCESSING), or this
        same cart (``cart_ref``, whatever it holds now) was already paid for
        and its order is not placed -- paying again would charge twice; that
        order is retried with the earlier mandate, or a person sorts it out.
        Money taken for ANOTHER cart holds up nothing: that mandate is left
        as it is, never voided.  A mandate is voided only when its payment
        was really cancelled, or had already ended with no money taken (a
        declined card).
        """
        from integrations.ap2.ap2_protocol import PaymentStatus
        with self._lock:
            self._ready_to_sign()
            now = time.time()
            mine = [m for m in self._mandates.values()
                    if m.user_id == str(user_id) and m.merchant == merchant
                    and m.kind == kind]
            for m in mine:
                self._expire_if_due(m, now)
            for m in mine:
                if m.mandate_id in self._settling:
                    return False, ('a payment for an earlier checkout is being '
                                   'taken right now')
                if self._paid_unordered(m, cart_ref):
                    return False, (f'this cart is already paid for (mandate '
                                   f'{m.mandate_id}) and its order is not placed '
                                   f'yet; commerce_checkout with that mandate_id '
                                   f'places it with that payment and never '
                                   f'charges again (the cart must hold what was '
                                   f'paid for), or a person sorts it out')
                if m.status in ('pending', 'approved') and self._payment_status(
                        m.payment_id) == PaymentStatus.PROCESSING:
                    return False, ('a payment for an earlier checkout is still '
                                   'in progress')
            voided, stuck = 0, 0
            for m in mine:
                if m.status not in ('pending', 'approved') \
                        or self._money_taken(m.payment_id):
                    continue
                if self.ledger.cancel_payment(m.payment_id, 'ap2_mandate',
                                              'replaced by a newer checkout') \
                        or self._payment_status(m.payment_id) in (
                            PaymentStatus.FAILED, PaymentStatus.CANCELLED,
                            PaymentStatus.REFUNDED):
                    m.status = 'rejected'
                    m.sig = self._sign(m)
                    voided += 1
                else:
                    stuck += 1   # the gateway has it now: leave it as it is
            if voided:
                self._save()
            if stuck:
                return False, ('a payment for an earlier checkout is still '
                               'in progress')
            return True, f'{voided} earlier checkout(s) withdrawn'

    def begin_settle(self, mandate_id: str) -> Tuple[bool, str]:
        """Claim the right to take the money for ``mandate_id``, or to place
        its order.  (ok, reason).

        At most one mandate per merchant-side cart is settled at a time, and
        none once another one for that cart took the money without its order
        being placed or is with the gateway: two approvals for one cart --
        two prepares that raced, an older card -- never both charge.  A paid
        mandate's order step claims it the same way, so a callback and a
        retry never send its order at once.  Pair with end_settle in a
        finally.
        """
        with self._lock:
            m = self._mandates.get(mandate_id)
            if m is None:
                return False, 'mandate not found'
            if mandate_id in self._settling:
                return False, ALREADY_SETTLING
            if m.cart_ref is not None:
                for other in self._mandates.values():
                    if (other is m or other.user_id != m.user_id
                            or other.merchant != m.merchant
                            or other.kind != m.kind
                            or other.cart_ref != m.cart_ref):
                        continue
                    if other.mandate_id in self._settling:
                        return False, ('another payment for this cart is being '
                                       'taken right now')
                    if self._paid_unordered(other, m.cart_ref):
                        return False, (f'this cart is already paid for (mandate '
                                       f'{other.mandate_id})')
                    if other.status == 'approved' \
                            and self._money_in_flight(other.payment_id):
                        return False, ('another payment for this cart is still '
                                       'in progress')
            self._settling.add(mandate_id)
            return True, 'ok'

    def end_settle(self, mandate_id: str) -> None:
        with self._lock:
            self._settling.discard(mandate_id)


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


def no_live_gateway_card(message: Optional[str] = None) -> Dict[str, Any]:
    """The card a person sees when a payment they approved cannot be taken
    because this node has no live gateway (ap2_protocol.mock_would_settle).
    One title for every settler; the body may say what else did not happen
    (the cart settler adds "and no order was placed")."""
    return {'type': 'notification', 'severity': 'warning',
            'title': 'Payment not available',
            'message': message or ('This node cannot take payments right now, '
                                   'so nothing was charged.')}


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
    try:
        m = store.find_by_payment_id(payment_id)
    except MandateKeyError as e:
        return {'success': False, 'payment_id': payment_id, 'error': str(e)}
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
    Without one, settling is the no-live-gateway gate
    (ap2_protocol.mock_would_settle) and then ledger.process_payment.  A
    registered settler owns that gate itself (commerce_tools.settle_checkout
    does): a kind registered without it would settle on the mock."""
    with _settlers_lock:
        _settlers[kind] = fn


def settle(m: CartMandate, store: Optional[MandateStore] = None) -> Dict[str, Any]:
    """Take the money for an approved mandate (and whatever its kind does)."""
    with _settlers_lock:
        fn = _settlers.get(m.kind)
    if fn is not None:
        return fn(m)
    store = store or get_mandate_store()
    from integrations.ap2.ap2_protocol import (
        NO_LIVE_GATEWAY, NO_LIVE_GATEWAY_REASON, mock_would_settle,
        refuse_no_live_gateway)
    payment = store.ledger.get_payment(m.payment_id)
    if payment is not None and mock_would_settle(payment.gateway):
        # The same gate the cart settler has (commerce_tools.settle_checkout):
        # the mock captures anything, so an approved "pay 10 USD for X" from
        # any agent's request_payment was marked paid, consumed and shown as
        # completed with nothing taken (review of abb2dcfaf, measured).
        # Refused BEFORE process_payment so no PROCESSING reservation is
        # left; the approval is void, so the payment is cancelled, not left
        # AUTHORIZED for anything else to take.
        store.withdraw(m.mandate_id, NO_LIVE_GATEWAY_REASON)
        refuse_no_live_gateway('ap2', m.user_id, m.payment_id)
        push_card(m.user_id, no_live_gateway_card())
        return {'success': False, 'payment_id': m.payment_id,
                'error': f'payment refused: {NO_LIVE_GATEWAY}; nothing was charged'}
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
    try:
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
    except MandateKeyError as e:
        # Nothing was read or written: the card stays open and the person
        # can answer again once the key reads.  503, not 500: the node is
        # pausing payments, not crashing, and the reason is stated.
        logger.error(f'ap2_pay: {payment_id} not answered: {e}')
        return {'status': 'error', 'action': action, 'reason': str(e),
                'retry_later': True}, 503
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


@contextlib.contextmanager
def _key_file_lock(lock_path: str):
    """One writer at a time for the mandate key, across threads AND
    processes: an OS lock on the sidecar ``lock_path``.  fcntl.flock on
    POSIX and msvcrt.locking on Windows both bind to the handle opened here,
    so every caller opens its own (a lockf/fcntl lock would belong to the
    process and let two threads through).  The lock file is never deleted:
    unlinking it while another caller waits would hand that caller a
    different file and reopen the race.  Windows gives up after its ten
    one-second retries with an OSError, which the caller reports as the
    payment pause."""
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    f = open(lock_path, 'a+')
    try:
        if os.name == 'nt':
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            # Unlocked before the close, as the Windows runtime asks.
            if os.name == 'nt':
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


def _parse_key(raw: bytes) -> Optional[bytes]:
    """The ONE reading of the key file, cold boot and re-read alike: utf-8
    with or without a BOM (a key re-saved in Notepad is still the key, and
    replacing it would orphan every record it signed), surrounding
    whitespace and line ends ignored, 64 or more hex digits.  None when the
    bytes hold no key."""
    try:
        text = raw.decode('utf-8-sig').strip()
    except UnicodeDecodeError:
        return None
    if len(text) < 64:
        return None
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None


def _load_or_create_key(path: str) -> bytes:
    """Node-local HMAC key for mandate records (0600 where supported).

    Every mandate is signed with it, so the rule is: one key per node, read
    as it is on disk, created once, never replaced.  Every step here is a
    check-then-act, so it runs under _key_file_lock: two processes at first
    boot end with ONE key that both hold.  Under the lock:
      - a readable key (_parse_key) is returned exactly as it is on disk;
      - absent, or an empty file (the old create path's crash window), gets
        a key: written to a temp file in the same directory, fsynced, moved
        into place with os.replace (atomic on every volume; no hard links,
        which a FAT or exFAT data dir lacks), the directory fsynced on POSIX
        so a power loss cannot lose the rename, then read back with the same
        parser, so what is cached is what is on disk;
      - unreadable (locked, denied) raises MandateKeyError and writes
        nothing: a new key would turn every record unverifiable, and a paid
        one could then never be ordered;
      - non-empty but not a key also raises and writes nothing.  Whether
        that should instead mint a fresh key (and lose whatever the file
        once signed) is the owner's open call; pausing for a person is the
        fail-closed default.
    A failure to lock, write, replace or read back is the same
    MandateKeyError.  On Windows os.replace raises PermissionError while
    another handle holds the key open; every reader takes the lock, so that
    is an antivirus scan, brief and rare, and it is reported as the pause
    rather than retried here: a retry loop would hide a real denial behind
    delay.
    """
    directory = os.path.dirname(path)
    try:
        with _key_file_lock(path + '.lock'):
            try:
                with open(path, 'rb') as f:
                    raw = f.read()
            except FileNotFoundError:
                raw = b''
            if raw:
                key = _parse_key(raw)
                if key is None:
                    logger.error(f'ap2 mandates: {path} does not hold a key; not '
                                 f'replaced: restore it, or move it away to start '
                                 f'with a new key (records signed with the old '
                                 f'one will not verify)')
                    raise MandateKeyError()
                return key
            fd, tmp = tempfile.mkstemp(prefix=_MANDATE_KEY_FILENAME + '.',
                                       dir=directory)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(secrets.token_hex(32))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
            if os.name != 'nt':
                try:
                    dfd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(dfd)
                    finally:
                        os.close(dfd)
                except OSError as e:
                    logger.warning(f'ap2 mandates: directory of {path} not '
                                   f'fsynced ({e}); the key is written, a '
                                   f'power loss now could lose its name')
            with contextlib.suppress(OSError, NotImplementedError):
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            with open(path, 'rb') as f:
                key = _parse_key(f.read())
            if key is None:
                raise MandateKeyError()
            return key
    except MandateKeyError:
        raise
    except OSError as e:
        logger.error(f'ap2 mandates: key {path} cannot be used ({e}); not '
                     f'replaced, nothing is signed until it can')
        raise MandateKeyError()


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
    'MandateError', 'MandateKeyError', 'PAYMENTS_PAUSED',
    'IntentMandate', 'CartMandate', 'MandateStore',
    'canonical_cart_hash', 'get_mandate_store', 'DEFAULT_MANDATE_TTL_S',
    'NON_HUMAN_APPROVERS', 'PERSON_ID_PREFIX', 'approval_action', 'parse_approval_action',
    'request_human_approval', 'register_settler', 'settle', 'decide_payment',
    'no_live_gateway_card',
]
