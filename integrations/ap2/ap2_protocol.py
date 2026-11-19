"""
AP2 (Agent Protocol 2) - Agentic Commerce Module

This module enables payment workflows for agents, allowing them to request,
authorize, and complete payment transactions as part of their task execution.

Features:
- Payment request creation and management
- Multi-agent payment coordination
- Payment gateway integration (Stripe, PayPal, etc.)
- Transaction ledger with audit trail
- Secure payment handling with PCI compliance patterns
- Integration with task_ledger for workflow tracking
- Integration with A2A for multi-agent coordination
"""

import base64
import hmac
import json
import logging
import threading
import time
import uuid
import os
from typing import Dict, List, Any, Optional, Callable
from datetime import date, datetime, timedelta
from enum import Enum
from decimal import Decimal, InvalidOperation
import hashlib

from core.public_url import PUBLIC_URL_VAR, internet_base_url

logger = logging.getLogger(__name__)

# Where a redirect gateway's server-to-server confirmations land on this
# node (integrations.agent_engine.commercial_api serves both paths).  The
# gateway composes its callback URLs from these; a test pins the blueprint's
# rules to them.
PHONEPE_CALLBACK_PATH = '/api/v1/intelligence/phonepe/callback'
PHONEPE_REFUND_CALLBACK_PATH = '/api/v1/intelligence/phonepe/refund-callback'
# Where the buyer's browser comes back to after the hosted checkout, beside
# the callbacks so the one public route that carries them carries it too
# (commercial_api.phonepe_return; the CSRF guard lets its form POST through).
# It was the bare root, which no node serves: on api.hevolve.ai the root is
# Kong's 404 (measured 2026-10-27), so a buyer who had just paid landed on an
# error page (task #145).
PHONEPE_RETURN_PATH = '/api/v1/intelligence/phonepe/return'
# Where Cashfree posts its signed payment-link events on this node
# (commercial_api.cashfree_webhook), on the same public route as PhonePe's.
# A link names it as its notify_url when this node has an https public
# address; the owner can also register it in Cashfree's dashboard.
CASHFREE_WEBHOOK_PATH = '/api/v1/intelligence/cashfree/webhook'


def own_gateway_urls() -> Dict[str, str]:
    """The URLs a redirect gateway (PhonePe) returns the buyer to and posts
    its confirmations to, on this node's internet-reachable base
    (core.public_url.internet_base_url: HARTOS_PUBLIC_URL, else the live
    request's root when its Host is trusted AND the internet can reach it;
    a loopback or LAN root is this node, but no gateway can post to it).
    {} when there is none, and a warning says so: the payment is then
    refused rather than sent with a host that is not this node or that
    nothing can reach.  Called only for a redirect gateway: a cart mandate
    on Stripe or the dev mock needs no such URLs."""
    base = internet_base_url()
    if not base:
        logger.warning('ap2: this node has no public address for a redirect '
                       'gateway\'s return and callback urls (set %s); a payment '
                       'through one cannot be started until it has one',
                       PUBLIC_URL_VAR)
        return {}
    return {'redirect_url': base + PHONEPE_RETURN_PATH,
            'callback_url': base + PHONEPE_CALLBACK_PATH,
            'refund_callback_url': base + PHONEPE_REFUND_CALLBACK_PATH}


class PaymentStatus(str, Enum):
    """Payment transaction status lifecycle"""
    # Initial states
    PENDING = "pending"                    # Payment request created
    AUTHORIZED = "authorized"              # Payment authorized but not captured

    # Processing states
    PROCESSING = "processing"              # Payment being processed
    APPROVAL_REQUIRED = "approval_required"  # Requires user/admin approval

    # Terminal states
    COMPLETED = "completed"                # Payment successfully completed
    FAILED = "failed"                      # Payment failed
    CANCELLED = "cancelled"                # Payment cancelled
    REFUNDED = "refunded"                  # Payment refunded
    EXPIRED = "expired"                    # Payment authorization expired


class PaymentMethod(str, Enum):
    """Supported payment methods"""
    CREDIT_CARD = "credit_card"
    DEBIT_CARD = "debit_card"
    BANK_TRANSFER = "bank_transfer"
    PAYPAL = "paypal"
    STRIPE = "stripe"
    CRYPTO = "cryptocurrency"
    INTERNAL_CREDITS = "internal_credits"  # For testing or internal workflows


class PaymentGateway(str, Enum):
    """Supported payment gateways"""
    STRIPE = "stripe"
    PAYPAL = "paypal"
    SQUARE = "square"
    BRAINTREE = "braintree"
    PHONEPE = "phonepe"  # India UPI / cards / netbanking via PhonePe PG
    CASHFREE = "cashfree"  # India payment links (UPI / cards / netbanking)
    MOCK = "mock"  # For testing


# Gateways whose create_payment returns a hosted-checkout redirect: the money
# moves later, when the person pays, and a server callback confirms it.
REDIRECT_GATEWAYS = frozenset({PaymentGateway.PHONEPE})

#: Payment metadata flag: the gateway took money for this payment although it
#: is FAILED (PhonePe captured an amount other than the one asked).  Such a
#: payment is never read as a declined one: its cart is not offered for
#: payment again, and a person settles it.
MONEY_TAKEN = 'money_taken'


def money_taken(payment) -> bool:
    """The gateway took money for ``payment``: it COMPLETED, or it is FAILED
    only because the amount taken was not the one asked (MONEY_TAKEN).  The
    ONE reading of "was this paid": the mandate store's predicates read it,
    and so does the commerce order step when it must decide, without the
    store, whether a shopper's money moved."""
    if payment is None:
        return False
    return payment.status == PaymentStatus.COMPLETED or (
        payment.status == PaymentStatus.FAILED
        and bool((payment.metadata or {}).get(MONEY_TAKEN)))


def mock_settlement_allowed(user=None) -> bool:
    """May a payment the ledger routed to the MockPaymentGateway settle for
    this caller?  The ONE rule for every settlement path.

    The mock authorizes any payment_method and captures anything, so for a
    customer it is a paid tier, or a placed order, for nothing: a plain user
    named ``gateway: mock`` or hit a node without Stripe and got enterprise
    with the $499 recorded as paid (review of 0cf610371, measured on the real
    route); a McGroce cart on a node with no gateway keys settled through the
    mock and the order was placed (claude-autostart-93, 2026-10-14, read at
    903cd58c0).  select_gateway answers MOCK whenever no live gateway is
    registered, so a missing key must read as "cannot take payments", never
    as "paid".

    Allowed only when the node is in the existing dev mode
    (security.master_key.is_dev_mode, HEVOLVE_DEV_MODE=true, which central
    forces to false at boot), or when the caller is a known operator
    (integrations.social.auth.holds_central_role).  A settler that acts for
    a person by id and holds no user object (the cart settler) passes none
    and gets the dev-mode answer alone.
    """
    from security.master_key import is_dev_mode     # the ONE dev-mode switch
    if is_dev_mode():
        return True
    if user is None:
        return False
    from integrations.social.auth import holds_central_role
    return holds_central_role(user)


#: The refusal every settlement path answers with when the mock is the only
#: gateway (the person hears that nothing was charged; the operator reads
#: which setting is missing in the WARNING each refusal logs).
NO_LIVE_GATEWAY = 'no live payment gateway is configured on this node'
LIVE_GATEWAY_SETTINGS = ('STRIPE_API_KEY, or PHONEPE_MERCHANT_ID with '
                         'PHONEPE_SALT_KEY for INR')


def mock_would_settle(gateway, user=None) -> bool:
    """Would a payment on ``gateway`` be "paid" with no money moving for this
    caller?  True when it is the Mock and mock_settlement_allowed says no.
    The ONE spelling of that question: the tier upgrade route
    (commercial_api.upgrade_key), the McGroce cart settler
    (commerce_tools.settle_checkout and its prepare step) and the generic
    mandate settler (ap2_mandate.settle) all read it (review of abb2dcfaf:
    two spellings, and the generic settler read neither)."""
    return gateway == PaymentGateway.MOCK and not mock_settlement_allowed(user)


#: The reason a withdrawn mandate's payment is cancelled with, the one
#: spelling for every settler (review of 3a07d3989, F5).
NO_LIVE_GATEWAY_REASON = 'no live payment gateway'


def refuse_no_live_gateway(where: str, user_id, payment_id=None,
                           settings: str = LIVE_GATEWAY_SETTINGS) -> None:
    """The operator's side of every no-live-gateway refusal: ONE WARNING
    shape, on this module's logger, naming the settings to set
    (LIVE_GATEWAY_SETTINGS, or ``settings`` for a rail of its own such as
    CASHFREE_SETTINGS) and never a value.  The person's reply or card
    names no setting.  ``where`` is the path that refused ('ap2' for the
    generic settler and the agent's request, 'commerce' for the cart,
    'upgrade' for the paid tier, 'cashfree' for a payment link)."""
    logger.warning('%s: payment%s for %s refused: %s (set %s)', where,
                   f' {payment_id}' if payment_id else '', user_id,
                   NO_LIVE_GATEWAY, settings)


#: Metadata keys that are credentials: kept in memory for the gateway call,
#: never persisted or returned (PaymentRequest.to_dict).
_SECRET_METADATA_KEYS = ('payment_method_token',)


class PaymentRequest:
    """Represents a payment request from an agent"""

    def __init__(
        self,
        amount: Decimal,
        currency: str,
        description: str,
        requester_agent_id: str,
        payment_method: PaymentMethod = PaymentMethod.INTERNAL_CREDITS,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize a payment request

        Args:
            amount: Payment amount
            currency: Currency code (USD, EUR, etc.)
            description: Human-readable description
            requester_agent_id: Agent requesting payment
            payment_method: Payment method to use
            metadata: Additional payment metadata
        """
        self.payment_id = str(uuid.uuid4())
        self.amount = Decimal(str(amount))
        self.currency = currency.upper()
        self.description = description
        self.requester_agent_id = requester_agent_id
        self.payment_method = payment_method
        self.metadata = metadata or {}

        self.status = PaymentStatus.PENDING
        self.created_at = datetime.now()
        self.updated_at = datetime.now()
        self.completed_at = None

        self.gateway = None
        self.gateway_transaction_id = None
        self.approval_chain = []  # Track who approved
        self.error_message = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert payment request to dictionary"""
        return {
            'payment_id': self.payment_id,
            'amount': str(self.amount),
            'currency': self.currency,
            'description': self.description,
            'requester_agent_id': self.requester_agent_id,
            'payment_method': self.payment_method.value,
            'status': self.status.value,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'gateway': self.gateway.value if self.gateway else None,
            'gateway_transaction_id': self.gateway_transaction_id,
            'approval_chain': self.approval_chain,
            'error_message': self.error_message,
            # A card/payment-method token is used once, in memory, by the
            # gateway call; it is never written to the ledger or returned.
            'metadata': {k: v for k, v in (self.metadata or {}).items()
                         if k not in _SECRET_METADATA_KEYS}
        }

    def update_status(self, new_status: PaymentStatus, message: Optional[str] = None):
        """Update payment status"""
        old_status = self.status
        self.status = new_status
        self.updated_at = datetime.now()

        if new_status in [PaymentStatus.COMPLETED, PaymentStatus.FAILED,
                          PaymentStatus.CANCELLED, PaymentStatus.REFUNDED]:
            self.completed_at = datetime.now()

        if message:
            self.error_message = message

        logger.info(f"Payment {self.payment_id} status: {old_status} -> {new_status}")


class PaymentGatewayConnector:
    """Base class for payment gateway connectors"""

    def __init__(self, gateway: PaymentGateway, api_key: Optional[str] = None, config: Optional[Dict] = None):
        """
        Initialize payment gateway connector

        Args:
            gateway: Payment gateway type
            api_key: Gateway API key
            config: Additional gateway configuration
        """
        self.gateway = gateway
        self.api_key = api_key
        self.config = config or {}
        self.connected = False

    def connect(self) -> bool:
        """Connect to payment gateway"""
        raise NotImplementedError("Subclasses must implement connect()")

    def create_payment(self, payment_request: PaymentRequest) -> Dict[str, Any]:
        """Create a payment transaction"""
        raise NotImplementedError("Subclasses must implement create_payment()")

    def capture_payment(self, payment_id: str, gateway_transaction_id: str) -> Dict[str, Any]:
        """Capture an authorized payment"""
        raise NotImplementedError("Subclasses must implement capture_payment()")

    def refund_payment(self, payment_id: str, gateway_transaction_id: str, amount: Optional[Decimal] = None) -> Dict[str, Any]:
        """Refund a payment"""
        raise NotImplementedError("Subclasses must implement refund_payment()")


class MockPaymentGateway(PaymentGatewayConnector):
    """Mock payment gateway for testing"""

    def __init__(self, **kwargs):
        super().__init__(PaymentGateway.MOCK, **kwargs)
        self.transactions = {}

    def connect(self) -> bool:
        """Mock connection always succeeds"""
        self.connected = True
        logger.info("Connected to mock payment gateway")
        return True

    def create_payment(self, payment_request: PaymentRequest) -> Dict[str, Any]:
        """Create a mock payment transaction"""
        transaction_id = f"mock_txn_{uuid.uuid4().hex[:12]}"

        self.transactions[transaction_id] = {
            'payment_id': payment_request.payment_id,
            'amount': str(payment_request.amount),
            'currency': payment_request.currency,
            'status': 'authorized',
            'created_at': datetime.now().isoformat()
        }

        logger.info(f"Mock payment created: {transaction_id} for ${payment_request.amount}")

        return {
            'success': True,
            'transaction_id': transaction_id,
            'status': 'authorized',
            'message': 'Mock payment authorized successfully'
        }

    def capture_payment(self, payment_id: str, gateway_transaction_id: str) -> Dict[str, Any]:
        """Capture a mock payment"""
        if gateway_transaction_id in self.transactions:
            self.transactions[gateway_transaction_id]['status'] = 'captured'
            logger.info(f"Mock payment captured: {gateway_transaction_id}")
            return {
                'success': True,
                'status': 'captured',
                'message': 'Mock payment captured successfully'
            }
        else:
            return {
                'success': False,
                'error': 'Transaction not found'
            }

    def refund_payment(self, payment_id: str, gateway_transaction_id: str, amount: Optional[Decimal] = None) -> Dict[str, Any]:
        """Refund a mock payment"""
        if gateway_transaction_id in self.transactions:
            self.transactions[gateway_transaction_id]['status'] = 'refunded'
            logger.info(f"Mock payment refunded: {gateway_transaction_id}")
            return {
                'success': True,
                'status': 'refunded',
                'message': 'Mock payment refunded successfully'
            }
        else:
            return {
                'success': False,
                'error': 'Transaction not found'
            }


class StripePaymentGateway(PaymentGatewayConnector):
    """Stripe payment gateway adapter.

    Activates when the `STRIPE_API_KEY` environment variable is set (live
    or test key — both work).  Falls back to mock semantics if the
    ``stripe`` package isn't installed so a missing dep never crashes
    boot.  Designed to be drop-in compatible with the same flow used by
    `MockPaymentGateway`: create_payment → capture_payment → optional
    refund_payment.

    Operator step to go live: set `STRIPE_API_KEY=sk_live_…` (or
    `sk_test_…` for sandbox) and restart.  The PaymentLedger
    auto-registers this gateway at init when the env var is present, so
    no code change is required to flip from mock to real charges.
    """

    def __init__(self, api_key: Optional[str] = None, **kwargs):
        import os as _os
        super().__init__(
            PaymentGateway.STRIPE,
            api_key=api_key or _os.environ.get('STRIPE_API_KEY', ''),
            **kwargs,
        )
        self._stripe = None  # lazy-imported in connect()
        # The signing secret of the Checkout webhook (Stripe dashboard ->
        # Developers -> Webhooks -> the endpoint's signing secret, whsec_...).
        # Without it no webhook event is trusted (commercial_api.stripe_webhook
        # answers 503), so a paid Checkout bumps its tier only through the
        # buyer's success page.
        self.webhook_secret = _os.environ.get('STRIPE_WEBHOOK_SECRET', '')

    def verify_webhook(self, payload: bytes, sig_header: str):
        """The event Stripe signed, or ValueError.

        ``payload`` is the request body exactly as received (the signature
        covers its bytes), ``sig_header`` the Stripe-Signature header.  The
        SDK's Webhook.construct_event checks the HMAC against
        STRIPE_WEBHOOK_SECRET and its timestamp tolerance; whatever it
        raises, and a missing secret or connection, is one ValueError here,
        so the route's only branches are "verified" and "refused".
        """
        if not self.connected or not self._stripe:
            raise ValueError('Stripe gateway not connected')
        if not self.webhook_secret:
            raise ValueError('STRIPE_WEBHOOK_SECRET not set')
        try:
            return self._stripe.Webhook.construct_event(payload, sig_header, self.webhook_secret)
        except Exception as e:
            raise ValueError(f'Stripe webhook rejected: {e}') from e

    def connect(self) -> bool:
        """Lazy-import the stripe SDK and configure with the API key.

        Failures (missing SDK, bad key) are reported via `connected=False`
        rather than raising, so PaymentLedger init never crashes the app.
        """
        if not self.api_key:
            logger.info("StripePaymentGateway: STRIPE_API_KEY not set, gateway disabled")
            self.connected = False
            return False
        try:
            import stripe as _stripe  # type: ignore
            _stripe.api_key = self.api_key
            self._stripe = _stripe
            self.connected = True
            mode = 'live' if self.api_key.startswith('sk_live_') else 'test'
            logger.info(f"Connected to Stripe payment gateway ({mode} mode)")
            return True
        except ImportError:
            logger.warning(
                "StripePaymentGateway: `stripe` package not installed — "
                "run `pip install stripe` to enable real charges")
            self.connected = False
            return False
        except Exception as e:
            logger.warning(f"StripePaymentGateway: connect failed: {e}")
            self.connected = False
            return False

    def create_payment(self, payment_request: PaymentRequest) -> Dict[str, Any]:
        """Create a Stripe PaymentIntent for the requested amount.

        Returns Stripe transaction id on success.  The payment_method
        token from the upgrade flow is passed via metadata so the
        commercial-API upgrade handler can attach it to the intent.
        """
        if not self.connected or not self._stripe:
            return {'success': False, 'error': 'Stripe gateway not connected'}
        try:
            # Stripe amounts are in smallest currency unit (cents for USD).
            amount_minor = int(round(float(payment_request.amount) * 100))
            currency = (payment_request.currency or 'USD').lower()
            method_token = (payment_request.metadata or {}).get('payment_method_token', '')
            kwargs = dict(
                amount=amount_minor,
                currency=currency,
                description=payment_request.description or 'API tier upgrade',
                metadata={
                    'payment_request_id': payment_request.payment_id,
                    **{k: str(v) for k, v in (payment_request.metadata or {}).items() if k != 'payment_method_token'},
                },
                confirm=bool(method_token),
            )
            if method_token:
                kwargs['payment_method'] = method_token
            intent = self._stripe.PaymentIntent.create(**kwargs)
            return {
                'success': intent.status in ('requires_capture', 'succeeded', 'processing'),
                'transaction_id': intent.id,
                'status': intent.status,
                'message': f'Stripe PaymentIntent {intent.id} status={intent.status}',
            }
        except Exception as e:
            return {'success': False, 'error': f'Stripe create_payment failed: {e}'}

    def capture_payment(self, payment_id: str, gateway_transaction_id: str) -> Dict[str, Any]:
        """Capture an authorized Stripe PaymentIntent.

        If the intent was created with `confirm=True` and auto-captured
        (Stripe default for new accounts), this is a no-op success.
        """
        if not self.connected or not self._stripe:
            return {'success': False, 'error': 'Stripe gateway not connected'}
        try:
            intent = self._stripe.PaymentIntent.retrieve(gateway_transaction_id)
            if intent.status == 'succeeded':
                return {'success': True, 'status': 'captured', 'message': 'Already captured'}
            if intent.status == 'requires_capture':
                intent = self._stripe.PaymentIntent.capture(gateway_transaction_id)
                return {'success': intent.status == 'succeeded', 'status': intent.status}
            return {'success': False, 'error': f'Cannot capture from status {intent.status}'}
        except Exception as e:
            return {'success': False, 'error': f'Stripe capture failed: {e}'}

    def refund_payment(self, payment_id: str, gateway_transaction_id: str,
                       amount: Optional[Decimal] = None) -> Dict[str, Any]:
        """Refund a captured Stripe payment."""
        if not self.connected or not self._stripe:
            return {'success': False, 'error': 'Stripe gateway not connected'}
        try:
            kwargs = {'payment_intent': gateway_transaction_id}
            if amount is not None:
                kwargs['amount'] = int(round(float(amount) * 100))
            refund = self._stripe.Refund.create(**kwargs)
            return {'success': refund.status == 'succeeded',
                    'status': refund.status, 'refund_id': refund.id}
        except Exception as e:
            return {'success': False, 'error': f'Stripe refund failed: {e}'}


#: The payment-metadata key under which PhonePePaymentGateway.create_payment
#: records the paise it asked PhonePe for; the one spelling the callback's
#: amount check (expected_paise) reads.
PHONEPE_SENT_AMOUNT_KEY = 'phonepe_amount_paise'

#: The operator's rupees per US dollar.  PhonePe prices a USD tier in rupees
#: with it, and the revenue sum (revenue_aggregator.query_revenue_streams)
#: states rupees in dollars with it, so the two can never use two rates.
USD_INR_RATE_VAR = 'PHONEPE_USD_INR_RATE'
DEFAULT_USD_INR_RATE = 84.0


def operator_usd_inr_rate() -> float:
    """The ONE reader of the operator's USD to INR rate (USD_INR_RATE_VAR,
    DEFAULT_USD_INR_RATE when unset or not a number)."""
    try:
        return float(os.environ.get(USD_INR_RATE_VAR, DEFAULT_USD_INR_RATE))
    except (TypeError, ValueError):
        return DEFAULT_USD_INR_RATE


class PhonePePaymentGateway(PaymentGatewayConnector):
    """PhonePe Payment Gateway (Standard Checkout / Hermes API).

    PhonePe is the dominant India payment rail (UPI + cards + netbanking).
    The hevolve.ai web side already has PhonePe configured; this adapter
    closes the loop on the HARTOS API surface so the same merchant
    credentials drive API-tier-upgrade revenue.

    Activates when both `PHONEPE_MERCHANT_ID` and `PHONEPE_SALT_KEY` are
    set in the environment.  `PHONEPE_SALT_INDEX` defaults to '1'.
    `PHONEPE_ENV` selects 'PROD' (default) or 'UAT' for sandbox.

    Auth scheme (X-VERIFY header):
        sha256( base64(payload) + endpoint + saltKey ) + "###" + saltIndex

    Flow:
        create_payment → returns redirect URL to PhonePe-hosted checkout.
        capture_payment → polls /pg/v1/status (PhonePe auto-captures on
                          successful UPI auth; this just confirms).
        refund_payment  → POST /pg/v1/refund.

    Currency note: PhonePe is INR-only.  When a payment_request has
    currency='USD' we convert via PHONEPE_USD_INR_RATE (default 84.0)
    so the integration works for USD-priced API tiers without forcing
    a schema change to upgrade flows.  The rate is the operator's (the
    env var); nothing in a payment's metadata prices it (a metadata
    inr_amount once did, and a buyer reached it through the upgrade
    route's body: the review of af987a962 bought the enterprise tier for
    one rupee).  The paise actually sent are recorded on the payment
    (PHONEPE_SENT_AMOUNT_KEY) when the request is minted, and the callback
    compares the captured amount with THAT, not with a recomputation at
    today's rate (sent_paise / expected_paise).
    """

    PROD_BASE = "https://api.phonepe.com/apis/hermes"
    UAT_BASE = "https://api-preprod.phonepe.com/apis/pg-sandbox"
    PAY_ENDPOINT = "/pg/v1/pay"
    STATUS_ENDPOINT_TPL = "/pg/v1/status/{merchant_id}/{txn_id}"
    REFUND_ENDPOINT = "/pg/v1/refund"

    def __init__(self, merchant_id: Optional[str] = None,
                 salt_key: Optional[str] = None,
                 salt_index: Optional[str] = None,
                 env: Optional[str] = None, **kwargs):
        import os as _os
        super().__init__(PaymentGateway.PHONEPE, api_key=salt_key or _os.environ.get('PHONEPE_SALT_KEY', ''), **kwargs)
        self.merchant_id = merchant_id or _os.environ.get('PHONEPE_MERCHANT_ID', '')
        self.salt_index = salt_index or _os.environ.get('PHONEPE_SALT_INDEX', '1')
        env_choice = (env or _os.environ.get('PHONEPE_ENV', 'PROD')).upper()
        self.base_url = self.PROD_BASE if env_choice == 'PROD' else self.UAT_BASE
        self.env = env_choice
        # USD→INR conversion (PhonePe is INR-only): the operator's rate.
        self.usd_inr_rate = operator_usd_inr_rate()

    def _sign(self, base64_payload: str, endpoint: str) -> str:
        """Compute X-VERIFY checksum per PhonePe docs."""
        raw = base64_payload + endpoint + self.api_key
        digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        return f"{digest}###{self.salt_index}"

    def _sign_status(self, endpoint: str) -> str:
        """X-VERIFY for status check has no payload — same scheme."""
        raw = endpoint + self.api_key
        digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        return f"{digest}###{self.salt_index}"

    def _to_paise(self, payment_request: 'PaymentRequest') -> int:
        """The payment's amount in paise (INR's smallest unit) at the
        operator's rate: INR passes through, USD converts via usd_inr_rate.
        Metadata prices nothing (a metadata inr_amount was the override a
        buyer reached through the upgrade route's body, the review of
        af987a962; then an internal override nothing set, whose None value
        raised on float(None) and answered 502: a stale key is ignored)."""
        amt = float(payment_request.amount)
        if payment_request.currency.upper() == 'INR':
            return int(round(amt * 100))
        return int(round(amt * self.usd_inr_rate * 100))

    def sent_paise(self, payment_request: 'PaymentRequest') -> Optional[int]:
        """The paise this gateway asked PhonePe for when it minted the
        request (recorded by create_payment), or None when no request has
        been minted for the payment."""
        value = (payment_request.metadata or {}).get(PHONEPE_SENT_AMOUNT_KEY)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def expected_paise(self, payment_request: 'PaymentRequest') -> int:
        """What PhonePe should have captured: the amount SENT when known.
        A capture is checked against that, never against a recomputation,
        because the operator's rate can move between the checkout and the
        confirmation and a correct capture then read 'amount mismatch',
        FAILED, after the money was taken (the review of f08ff3e4c).  A
        payment minted before the amount was recorded falls back to today's
        rate, said at WARNING."""
        sent = self.sent_paise(payment_request)
        if sent is not None:
            return sent
        logger.warning("ap2: PhonePe payment %s has no recorded sent amount; "
                       "checking its capture against today's rate",
                       payment_request.payment_id)
        return self._to_paise(payment_request)

    def connect(self) -> bool:
        """Validate that credentials are present; PhonePe has no per-session handshake."""
        if not self.merchant_id or not self.api_key:
            logger.info("PhonePePaymentGateway: PHONEPE_MERCHANT_ID/SALT_KEY not set, gateway disabled")
            self.connected = False
            return False
        self.connected = True
        logger.info(f"Connected to PhonePe payment gateway ({self.env} mode, merchant={self.merchant_id})")
        return True

    def create_payment(self, payment_request: PaymentRequest) -> Dict[str, Any]:
        """Create a PhonePe pay request.

        Returns the redirect URL the caller should send the user to.  Once
        the user completes UPI auth on PhonePe's hosted page, PhonePe
        redirects back to `redirect_url` and (optionally) posts to
        `callback_url` for server-side confirmation.
        """
        if not self.connected:
            return {'success': False, 'error': 'PhonePe gateway not connected'}
        try:
            import base64 as _b64
            import json as _json
            import requests as _requests

            amount_paise = self._to_paise(payment_request)
            # Recorded before the wire: the callback checks the capture
            # against what was sent (expected_paise), and a request that
            # was posted but whose reply was lost still has its amount.
            if payment_request.metadata is None:
                payment_request.metadata = {}
            payment_request.metadata[PHONEPE_SENT_AMOUNT_KEY] = amount_paise
            meta = payment_request.metadata
            # Where the buyer returns and where the signed confirmation is
            # posted: what the payment's metadata names (a cart mandate
            # stamps this node's URLs at creation; the upgrade route may
            # name the buyer's return page), else this node's public URLs
            # now.  Never a guessed host: these defaulted to the web app's
            # host, which answers the API paths with the web app's 404, so
            # the callback never arrived and the money was taken with no
            # tier bumped and no order placed (task #122).
            redirect_url = meta.get('redirect_url')
            callback_url = meta.get('callback_url')
            if not redirect_url or not callback_url:
                own = own_gateway_urls()
                redirect_url = redirect_url or own.get('redirect_url')
                callback_url = callback_url or own.get('callback_url')
            if not redirect_url or not callback_url:
                # The operator's detail goes to the log; the sentence below
                # reaches the shopper through the agent (settle_checkout
                # repeats a gateway error), and a tenant-facing message
                # names no environment variable.
                logger.error("ap2: PhonePe payment %s refused: this node has no "
                             "public address for the buyer's return and the "
                             "payment callback (set %s)",
                             payment_request.payment_id, PUBLIC_URL_VAR)
                return {'success': False,
                        'error': 'This node is not set up to take PhonePe payments '
                                 'yet (it has no public address for the payment '
                                 'callback)'}
            merchant_txn_id = f"hartos_{payment_request.payment_id.replace('-', '')[:24]}"
            payload = {
                "merchantId": self.merchant_id,
                "merchantTransactionId": merchant_txn_id,
                "merchantUserId": str(meta.get('user_id', payment_request.requester_agent_id))[:36],
                "amount": amount_paise,
                "redirectUrl": redirect_url,
                "redirectMode": "POST",
                "callbackUrl": callback_url,
                "paymentInstrument": {"type": "PAY_PAGE"},
            }
            mobile = meta.get('mobile_number')
            if mobile:
                payload["mobileNumber"] = str(mobile)

            b64 = _b64.b64encode(_json.dumps(payload).encode('utf-8')).decode('utf-8')
            x_verify = self._sign(b64, self.PAY_ENDPOINT)
            url = self.base_url + self.PAY_ENDPOINT
            resp = _requests.post(
                url,
                json={"request": b64},
                headers={
                    "Content-Type": "application/json",
                    "X-VERIFY": x_verify,
                    "accept": "application/json",
                },
                timeout=15,
            )
            data = resp.json()
            ok = bool(data.get('success'))
            redirect = (((data.get('data') or {}).get('instrumentResponse') or {})
                        .get('redirectInfo') or {}).get('url')
            return {
                'success': ok,
                'transaction_id': merchant_txn_id,
                'status': 'pending' if ok else 'failed',
                'redirect_url': redirect,
                'gateway_response': data,
                'message': data.get('message', ''),
            }
        except Exception as e:
            return {'success': False, 'error': f'PhonePe create_payment failed: {e}'}

    def capture_payment(self, payment_id: str, gateway_transaction_id: str) -> Dict[str, Any]:
        """Check the status of a PhonePe transaction.

        PhonePe captures funds immediately on successful UPI auth, so
        "capture" semantically reduces to "confirm".  Returns success
        when PhonePe reports PAYMENT_SUCCESS.
        """
        if not self.connected:
            return {'success': False, 'error': 'PhonePe gateway not connected'}
        try:
            import requests as _requests
            endpoint = self.STATUS_ENDPOINT_TPL.format(
                merchant_id=self.merchant_id, txn_id=gateway_transaction_id)
            x_verify = self._sign_status(endpoint)
            resp = _requests.get(
                self.base_url + endpoint,
                headers={
                    "Content-Type": "application/json",
                    "X-VERIFY": x_verify,
                    "X-MERCHANT-ID": self.merchant_id,
                    "accept": "application/json",
                },
                timeout=15,
            )
            data = resp.json()
            code = data.get('code', '')
            if data.get('success') and code == 'PAYMENT_SUCCESS':
                return {'success': True, 'status': 'captured', 'gateway_response': data}
            return {
                'success': False,
                'status': code or 'unknown',
                'gateway_response': data,
                'error': data.get('message', f'PhonePe status={code}'),
            }
        except Exception as e:
            return {'success': False, 'error': f'PhonePe status check failed: {e}'}

    def verify_callback(self, b64_response: str, x_verify_header: str) -> bool:
        """Verify a PhonePe S2S callback signature.

        PhonePe posts `{"response": "<base64>"}` with header
        `X-VERIFY: sha256(base64_response + salt_key) + "###" + salt_index`.
        Returns True if the signature matches, False otherwise.  Constant-
        time comparison guards against timing leaks.
        """
        if not self.connected or not b64_response or not x_verify_header:
            return False
        try:
            import hmac as _hmac
            expected_digest = hashlib.sha256(
                (b64_response + self.api_key).encode('utf-8')
            ).hexdigest()
            expected_header = f"{expected_digest}###{self.salt_index}"
            return _hmac.compare_digest(expected_header, x_verify_header)
        except Exception as e:
            logger.debug(f"PhonePe verify_callback failed: {e}")
            return False

    def refund_payment(self, payment_id: str, gateway_transaction_id: str,
                       amount: Optional[Decimal] = None) -> Dict[str, Any]:
        """Refund a captured PhonePe payment (full or partial)."""
        if not self.connected:
            return {'success': False, 'error': 'PhonePe gateway not connected'}
        try:
            import base64 as _b64
            import json as _json
            import requests as _requests

            refund_txn_id = f"refund_{uuid.uuid4().hex[:16]}"
            amount_paise = (int(round(float(amount) * 100))
                            if amount is not None else None)
            payload = {
                "merchantId": self.merchant_id,
                "merchantUserId": payment_id,
                "originalTransactionId": gateway_transaction_id,
                "merchantTransactionId": refund_txn_id,
            }
            # The refund's status lands on this node's acknowledger
            # (commercial_api.phonepe_refund_callback) when the node knows
            # its public URL; the key is left out otherwise, rather than
            # naming a host that is not this node (it named the web app's
            # host, and a route that existed nowhere).
            refund_callback = own_gateway_urls().get('refund_callback_url')
            if refund_callback:
                payload["callbackUrl"] = refund_callback
            if amount_paise is not None:
                payload["amount"] = amount_paise

            b64 = _b64.b64encode(_json.dumps(payload).encode('utf-8')).decode('utf-8')
            x_verify = self._sign(b64, self.REFUND_ENDPOINT)
            resp = _requests.post(
                self.base_url + self.REFUND_ENDPOINT,
                json={"request": b64},
                headers={
                    "Content-Type": "application/json",
                    "X-VERIFY": x_verify,
                    "accept": "application/json",
                },
                timeout=15,
            )
            data = resp.json()
            return {
                'success': bool(data.get('success')),
                'status': data.get('code', 'unknown'),
                'refund_id': refund_txn_id,
                'gateway_response': data,
            }
        except Exception as e:
            return {'success': False, 'error': f'PhonePe refund failed: {e}'}


# ── Cashfree payment links ───────────────────────────────────────────
# Every endpoint, header and field below was read from Cashfree's docs on
# 2026-11-17 (www.cashfree.com/docs: payment-links/create-payment-link,
# fetch-payment-link-details, get-orders-for-a-payment-link, payment-links/
# webhooks, webhooks/payloads-and-signatures, webhooks/reliability-and-
# operations), never from memory.

#: The credentials the owner enters (ENV_SECRETS below; the vault delivers
#: them to the environment) and the mode switch.  There is no separate
#: webhook secret: Cashfree signs its webhooks with the client secret.
CASHFREE_APP_ID = 'CASHFREE_APP_ID'
CASHFREE_SECRET_KEY = 'CASHFREE_SECRET_KEY'
CASHFREE_ENV = 'CASHFREE_ENV'
#: What the operator's WARNING names (refuse_no_live_gateway's settings).
CASHFREE_SETTINGS = (f'{CASHFREE_APP_ID} with {CASHFREE_SECRET_KEY}; '
                     f'{CASHFREE_ENV}=production for live money')
#: The x-api-version this node speaks: the default of Cashfree's current
#: OpenAPI (openapi/payments/v2026-01-01.yaml).
CASHFREE_API_VERSION = '2026-01-01'
#: Cashfree's webhook guidance: reject a request whose x-webhook-timestamp
#: (milliseconds since the epoch) is more than five minutes old.
CASHFREE_WEBHOOK_MAX_AGE_S = 300
#: The ledger 'kind' of a payment a payer makes on a link this node minted.
PAYMENT_LINK_KIND = 'payment_link'
#: The fields every payment-link record carries (the owner's billing model:
#: a pilot runs under a centre's client ID; later, families subscribe).
#: transaction_id is filled when Cashfree confirms the payment.
PAYMENT_LINK_RECORD_FIELDS = ('payer_name', 'payer_phone', 'payer_email',
                              'child', 'plan', 'period', 'next_due_date',
                              'transaction_id', 'client_id')
#: Ledger kinds that are money IN to this node's owner (PaymentLedger.
#: revenue): a payer's payment link and a bought API tier (task #210: the
#: canonical sum missed tier upgrades).  A McGroce cart (commerce_checkout)
#: and an agent's request_payment are not counted.
REVENUE_KINDS = frozenset({PAYMENT_LINK_KIND, 'tier_upgrade'})


def cashfree_link_id(payment_id: str) -> str:
    """The link_id this node gives Cashfree for a payment: unique per
    payment, 43 characters of letters, digits, '-' and '_' (Cashfree allows
    50 of those)."""
    return f'hartos_{payment_id}'


class CashfreePaymentGateway(PaymentGatewayConnector):
    """Cashfree Payment Gateway: hosted payment links (UPI, cards,
    netbanking) a payer opens and pays; the money moves then, and Cashfree
    confirms it with a signed webhook.

    The credentials are read when they are used (core.config_cache.
    get_secret: the environment, then the config or vault), never kept on
    the object, so a key the owner enters after boot works without a
    restart and a rotated key takes effect on the next call.
    ``CASHFREE_ENV`` 'production' (or 'prod') selects the live API; anything
    else, unset included, is the sandbox.

    Not in select_gateway's order: no cart mandate or request_payment is
    routed here.  Payments reach it through create_payment_link only.
    """

    SANDBOX_BASE = 'https://sandbox.cashfree.com/pg'
    PRODUCTION_BASE = 'https://api.cashfree.com/pg'
    LINKS_ENDPOINT = '/links'
    HTTP_TIMEOUT = (5, 20)

    def __init__(self, **kwargs):
        super().__init__(PaymentGateway.CASHFREE, **kwargs)

    @staticmethod
    def _setting(name: str) -> str:
        from core.config_cache import get_secret
        return str(get_secret(name, '') or '').strip()

    @property
    def app_id(self) -> str:
        return self._setting(CASHFREE_APP_ID)

    @property
    def secret_key(self) -> str:
        return self._setting(CASHFREE_SECRET_KEY)

    @property
    def env(self) -> str:
        raw = self._setting(CASHFREE_ENV).lower()
        if raw in ('production', 'prod'):
            return 'PRODUCTION'
        if raw and raw not in ('sandbox', 'test'):
            logger.warning('CashfreePaymentGateway: %s=%r is not production or '
                           'sandbox; using the sandbox', CASHFREE_ENV, raw)
        return 'SANDBOX'

    @property
    def base_url(self) -> str:
        return self.PRODUCTION_BASE if self.env == 'PRODUCTION' else self.SANDBOX_BASE

    def missing_settings(self) -> List[str]:
        """The credential names that are not set now."""
        return [n for n in (CASHFREE_APP_ID, CASHFREE_SECRET_KEY) if not self._setting(n)]

    def connect(self) -> bool:
        """Connected while both credentials are set; Cashfree has no session
        handshake."""
        self.connected = not self.missing_settings()
        if not self.connected:
            # DEBUG: every node builds the ledger, and most have no Cashfree
            # account; a refused link says it at WARNING (refuse_no_live_gateway).
            logger.debug('CashfreePaymentGateway: %s not set, gateway disabled',
                         CASHFREE_SETTINGS)
        return self.connected

    @staticmethod
    def _refusal(status, data) -> str:
        """Cashfree's refusal as its HTTP status and its machine codes
        (``code``, ``type``).  Never its ``message``: that free text may echo
        what was sent, a payer's phone or name, and this sentence reaches
        logs and the agent."""
        codes = [str(data.get(k)) for k in ('code', 'type')
                 if isinstance(data, dict) and data.get(k)]
        return f'HTTP {status}' + (f' ({", ".join(codes)})' if codes else '')

    def _request(self, method: str, path: str, body=None,
                 idempotency_key: Optional[str] = None):
        """``(http_status, json_body)``; ``(None, {'message': why})`` when the
        request could not be made.  The credentials travel in headers only."""
        from core.http_pool import pooled_get, pooled_post
        headers = {'x-api-version': CASHFREE_API_VERSION,
                   'x-client-id': self.app_id,
                   'x-client-secret': self.secret_key,
                   'Content-Type': 'application/json',
                   'Accept': 'application/json'}
        if idempotency_key:
            # A retry with the same key never makes a second link (Cashfree
            # x-idempotency-key); the pooled session retries a 502/503/504.
            headers['x-idempotency-key'] = idempotency_key
        url = self.base_url + path
        try:
            if method == 'POST':
                resp = pooled_post(url, json=body, headers=headers,
                                   timeout=self.HTTP_TIMEOUT)
            else:
                resp = pooled_get(url, headers=headers, timeout=self.HTTP_TIMEOUT)
        except Exception as e:
            return None, {'message': f'{type(e).__name__}: {e}'[:300]}
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return resp.status_code, data if isinstance(data, (dict, list)) else {}

    def create_payment(self, payment_request: PaymentRequest) -> Dict[str, Any]:
        """Create a payment link for ``payment_request`` (POST /pg/links).

        The payer's name, phone and email come from the payment's metadata
        (create_payment_link writes them).  Partial payments are off and
        Cashfree sends no SMS or email: the agent hands the link over in
        its own reply.  notify_url is this node's webhook when it has an
        https public address (Cashfree requires https there); otherwise the
        confirmation comes from a webhook registered in Cashfree's dashboard
        or from check_payment_link.  Returns ``transaction_id`` (the link_id)
        and ``redirect_url`` (the link) on success.
        """
        if not self.connect():
            return {'success': False, 'error': 'Cashfree gateway not connected'}
        meta = payment_request.metadata or {}
        link_id = cashfree_link_id(payment_request.payment_id)
        customer = {'customer_phone': str(meta.get('payer_phone') or '')}
        if meta.get('payer_name'):
            customer['customer_name'] = str(meta['payer_name'])[:100]
        if meta.get('payer_email'):
            customer['customer_email'] = str(meta['payer_email'])
        # Up to five string pairs; nothing about the payer or a child.
        notes = {'hartos_payment_id': payment_request.payment_id}
        for key in ('client_id', 'plan', 'period'):
            if meta.get(key):
                notes[key] = str(meta[key])[:100]
        body = {
            'link_id': link_id,
            'link_amount': float(payment_request.amount.quantize(Decimal('0.01'))),
            'link_currency': payment_request.currency,
            'link_purpose': str(payment_request.description or '')[:500],
            'customer_details': customer,
            'link_partial_payments': False,
            'link_notify': {'send_sms': False, 'send_email': False},
            'link_notes': notes,
        }
        base = internet_base_url()
        if base.startswith('https://'):
            body['link_meta'] = {'notify_url': base + CASHFREE_WEBHOOK_PATH}
        else:
            logger.warning('ap2: Cashfree link %s has no notify_url: this node has no '
                           'https public address (set %s); its payment is confirmed '
                           'by a webhook registered in the Cashfree dashboard or by '
                           'check_payment_link', link_id, PUBLIC_URL_VAR)
        status, data = self._request('POST', self.LINKS_ENDPOINT, body,
                                     idempotency_key=payment_request.payment_id)
        if status is None:
            return {'success': False,
                    'error': f'Cashfree could not be reached: {data["message"]}'}
        if status != 200 or not isinstance(data, dict) or not data.get('link_url'):
            return {'success': False, 'http_status': status,
                    'error': f'Cashfree refused the link: {self._refusal(status, data)}'}
        return {'success': True,
                'transaction_id': data.get('link_id') or link_id,
                'status': 'pending',
                'redirect_url': data['link_url'],
                'cf_link_id': data.get('cf_link_id'),
                'link_status': data.get('link_status'),
                'link_expiry_time': data.get('link_expiry_time')}

    def fetch_link(self, link_id: str) -> Dict[str, Any]:
        """Cashfree's own word on a link (GET /pg/links/{link_id}):
        ``{'success': True, 'link': {...}}``, or ``success`` False with
        ``reachable`` False when Cashfree could not be asked."""
        from urllib.parse import quote
        status, data = self._request('GET', f'{self.LINKS_ENDPOINT}/{quote(link_id, safe="")}')
        if status is None:
            return {'success': False, 'reachable': False, 'error': data['message']}
        if status != 200 or not isinstance(data, dict):
            return {'success': False, 'reachable': True, 'http_status': status,
                    'error': self._refusal(status, data)}
        return {'success': True, 'link': data}

    def paid_orders(self, link_id: str) -> List[Dict[str, Any]]:
        """The PAID orders of a link (GET /pg/links/{link_id}/orders, whose
        default is PAID); [] when they could not be read."""
        from urllib.parse import quote
        status, data = self._request(
            'GET', f'{self.LINKS_ENDPOINT}/{quote(link_id, safe="")}/orders')
        if status != 200 or not isinstance(data, list):
            logger.warning('ap2: Cashfree orders of link %s could not be read (HTTP %s)',
                           link_id, status)
            return []
        return [o for o in data if isinstance(o, dict)
                and str(o.get('order_status') or '').upper() == 'PAID']

    def capture_payment(self, payment_id: str, gateway_transaction_id: str) -> Dict[str, Any]:
        """Confirm a link is paid: the money moves when the payer pays, so
        "capture" is Cashfree's PAID status."""
        looked = self.fetch_link(gateway_transaction_id)
        if not looked['success']:
            return {'success': False, 'error': looked['error']}
        state = str(looked['link'].get('link_status') or '').upper()
        if state == 'PAID':
            return {'success': True, 'status': 'captured', 'gateway_response': looked['link']}
        return {'success': False, 'status': state or 'unknown',
                'error': f'Cashfree link status {state or "unknown"}'}

    def refund_payment(self, payment_id: str, gateway_transaction_id: str,
                       amount: Optional[Decimal] = None) -> Dict[str, Any]:
        """Not wired: a person refunds from the Cashfree merchant dashboard."""
        return {'success': False,
                'error': 'Cashfree refunds are made from the Cashfree merchant dashboard'}

    def webhook_refusal(self, raw_body: bytes, timestamp: str, signature: str,
                        now: Optional[float] = None) -> Optional[str]:
        """Why a webhook is not Cashfree's, or None when it is.

        Cashfree's scheme: base64(HMAC-SHA256(x-webhook-timestamp + raw body,
        client secret)) in x-webhook-signature, compared in constant time
        over the body exactly as received.  The timestamp (milliseconds) must
        be within CASHFREE_WEBHOOK_MAX_AGE_S of ``now`` either way, so a
        captured delivery cannot be replayed later."""
        secret = self.secret_key
        if not secret:
            return 'no secret key is set'
        ts = str(timestamp or '').strip()
        sig = str(signature or '').strip()
        if not raw_body or not ts or not sig:
            return 'the body, x-webhook-timestamp or x-webhook-signature is missing'
        if not ts.isdigit():
            return 'x-webhook-timestamp is not a number'
        expected = base64.b64encode(hmac.new(
            secret.encode('utf-8'), ts.encode('utf-8') + raw_body,
            hashlib.sha256).digest())
        if not hmac.compare_digest(expected, sig.encode('utf-8')):
            return 'the signature does not match'
        age_s = abs((time.time() if now is None else now) - int(ts) / 1000.0)
        if age_s > CASHFREE_WEBHOOK_MAX_AGE_S:
            return f'the timestamp is {int(age_s)} s away from now'
        return None


def cashfree_gateway(ledger: 'PaymentLedger') -> Optional[CashfreePaymentGateway]:
    """The ledger's Cashfree connector while its credentials are set, else
    None.  A key the owner entered after boot registers it here, so the
    first payment link after the entry works with no restart (task #216's
    rule, for this rail)."""
    with ledger.lock:
        gateway = ledger.gateways.get(PaymentGateway.CASHFREE)
    if gateway is None:
        gateway = CashfreePaymentGateway()
        if not gateway.connect():
            return None
        ledger.add_gateway(gateway)
        return gateway
    return gateway if gateway.connect() else None


class PaymentLedger:
    """
    Payment transaction ledger with audit trail

    Integrates with task_ledger to track payment workflows
    """

    def __init__(self, ledger_path: Optional[str] = None):
        """
        Initialize payment ledger

        Args:
            ledger_path: Path to persist payment ledger.  Defaults to
                ``payment_ledger.json`` under core.platform_paths'
                agent-data dir -- the old CWD-relative default made a
                frozen desktop install try to write beside the .exe
                (Program Files on Windows), Gate 7.
        """
        if ledger_path is None:
            from core.platform_paths import get_agent_data_dir
            ledger_path = os.path.join(get_agent_data_dir(), 'payment_ledger.json')
            # One-time move of the old CWD-relative default: copy (never
            # delete) so payments recorded before the path moved survive.
            legacy = os.path.join(os.getcwd(), 'agent_data', 'payment_ledger.json')
            self._legacy_path = legacy
            if (os.path.exists(legacy) and not os.path.exists(ledger_path)
                    and os.path.abspath(legacy) != os.path.abspath(ledger_path)):
                try:
                    import shutil
                    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
                    shutil.copy2(legacy, ledger_path)
                    logger.info(f"Payment ledger migrated from {legacy}")
                except OSError as e:
                    logger.error(f"Payment ledger migration failed: {e}")
        self.ledger_path = ledger_path
        if not hasattr(self, '_legacy_path'):
            self._legacy_path = None
        self.payments: Dict[str, PaymentRequest] = {}
        self.lock = threading.Lock()
        self.gateways: Dict[PaymentGateway, PaymentGatewayConnector] = {}

        # Default to mock gateway for testing — always present.
        self.add_gateway(MockPaymentGateway())

        # Auto-register Stripe if STRIPE_API_KEY is set at boot.  This is
        # the single env-var flip between "mock charges only" and "real
        # money flowing" — removes one of the explicit blockers between
        # the wired infrastructure and actual hive-API revenue.
        try:
            import os as _os
            if _os.environ.get('STRIPE_API_KEY'):
                _stripe_gateway = StripePaymentGateway()
                # Register only when connect() succeeds (SDK present, key set).
                # connect() is what sets ``connected``; reading the flag before
                # calling it meant NO node ever registered Stripe, so every
                # paid tier settled through the mock, or, since b6502ac00,
                # was refused with "set STRIPE_API_KEY" while it was set
                # (review of b6502ac00, measured).  add_gateway connects once
                # more; both connectors' connect() is idempotent.
                if _stripe_gateway.connect():
                    self.add_gateway(_stripe_gateway)
                    logger.info(
                        "PaymentLedger: Stripe gateway auto-registered "
                        "(real charges enabled via STRIPE_API_KEY)")
                else:
                    logger.info(
                        "PaymentLedger: STRIPE_API_KEY present but connect "
                        "failed — staying on MockPaymentGateway only")
        except Exception as _stripe_err:
            logger.debug(f"PaymentLedger: stripe auto-register skipped: {_stripe_err}")

        # Auto-register PhonePe when PHONEPE_MERCHANT_ID + PHONEPE_SALT_KEY
        # are present.  Same shape as Stripe: env-var flip from "mock"
        # to "real INR charges via UPI/cards/netbanking".  Opens the
        # India market — the largest payment surface for hevolve.ai.
        try:
            import os as _os
            if _os.environ.get('PHONEPE_MERCHANT_ID') and _os.environ.get('PHONEPE_SALT_KEY'):
                _phonepe_gateway = PhonePePaymentGateway()
                if _phonepe_gateway.connect():     # same rule as Stripe above
                    self.add_gateway(_phonepe_gateway)
                    logger.info(
                        "PaymentLedger: PhonePe gateway auto-registered "
                        f"({_phonepe_gateway.env} mode — real INR charges enabled)")
                else:
                    logger.info(
                        "PaymentLedger: PHONEPE_* env present but connect "
                        "failed — staying on existing gateways")
        except Exception as _phonepe_err:
            logger.debug(f"PaymentLedger: phonepe auto-register skipped: {_phonepe_err}")

        # Cashfree when its credentials are set at boot, the same rule as
        # above; a key entered later registers on first use (cashfree_gateway).
        try:
            _cashfree_gateway = CashfreePaymentGateway()
            if _cashfree_gateway.connect():
                self.add_gateway(_cashfree_gateway)
                logger.info("PaymentLedger: Cashfree gateway auto-registered "
                            f"({_cashfree_gateway.env} mode, payment links)")
        except Exception as _cashfree_err:
            logger.warning(f"PaymentLedger: cashfree auto-register skipped: {_cashfree_err}")

        self.load_ledger()
        self._merge_legacy()

    def add_gateway(self, gateway: PaymentGatewayConnector):
        """Add a payment gateway connector"""
        with self.lock:
            self.gateways[gateway.gateway] = gateway
            gateway.connect()
            logger.info(f"Added payment gateway: {gateway.gateway.value}")

    def select_gateway(self, currency: str) -> PaymentGateway:
        """The live gateway a new payment in ``currency`` should use.

        INR prefers PhonePe (the India rail), then Stripe; any other
        currency prefers Stripe.  Mock is the fallback when no real
        gateway is registered, so a dev node keeps working and a node with
        live credentials never silently routes real orders to Mock -- which
        is what the hard-coded ``PaymentGateway.MOCK`` in request_payment
        used to do whatever was configured.
        """
        with self.lock:
            live = set(self.gateways)
        order = ([PaymentGateway.PHONEPE, PaymentGateway.STRIPE]
                 if (currency or '').upper() == 'INR'
                 else [PaymentGateway.STRIPE])
        for gw in order:
            if gw in live:
                return gw
        return PaymentGateway.MOCK

    def create_payment_request(
        self,
        amount: Decimal,
        currency: str,
        description: str,
        requester_agent_id: str,
        payment_method: PaymentMethod = PaymentMethod.INTERNAL_CREDITS,
        gateway: PaymentGateway = PaymentGateway.MOCK,
        metadata: Optional[Dict[str, Any]] = None,
        require_approval: bool = False,
    ) -> PaymentRequest:
        """
        Create a new payment request

        Args:
            amount: Payment amount
            currency: Currency code
            description: Payment description
            requester_agent_id: Agent requesting payment
            payment_method: Payment method
            gateway: Payment gateway to use
            metadata: Additional metadata
            require_approval: Start in APPROVAL_REQUIRED instead of PENDING
                -- the payment waits for a human answer on an approval card
                (integrations.ap2.ap2_mandate) rather than for any caller.

        Returns:
            PaymentRequest object
        """
        with self.lock:
            payment = PaymentRequest(
                amount=amount,
                currency=currency,
                description=description,
                requester_agent_id=requester_agent_id,
                payment_method=payment_method,
                metadata=metadata
            )

            payment.gateway = gateway
            if require_approval:
                payment.status = PaymentStatus.APPROVAL_REQUIRED
            self.payments[payment.payment_id] = payment

            logger.info(f"Created payment request: {payment.payment_id} - ${amount} {currency}")

            self.save_ledger()
            return payment

    def authorize_payment(self, payment_id: str, approver_id: str) -> bool:
        """
        Authorize a payment request

        Args:
            payment_id: Payment ID to authorize
            approver_id: ID of the approving PERSON.  Refused when empty, a
                non-human id (ap2_mandate.NON_HUMAN_APPROVERS, e.g. 'system')
                or the requesting agent's own id -- an agent can never
                authorize its own request, whatever calls this.

        Returns:
            True if authorization successful
        """
        from integrations.ap2.ap2_mandate import (NON_HUMAN_APPROVERS,
                                                  PERSON_ID_PREFIX)
        approver_id = str(approver_id or '').strip()
        with self.lock:
            if payment_id not in self.payments:
                logger.error(f"Payment not found: {payment_id}")
                return False

            payment = self.payments[payment_id]

            if payment.status not in (PaymentStatus.PENDING,
                                      PaymentStatus.APPROVAL_REQUIRED):
                logger.warning(f"Payment {payment_id} not in pending state: {payment.status}")
                return False

            if (approver_id.lower() in NON_HUMAN_APPROVERS
                    or (approver_id == payment.requester_agent_id
                        and not approver_id.startswith(PERSON_ID_PREFIX))):
                logger.warning(f"Payment {payment_id}: approver {approver_id!r} "
                               f"is not a person; refused")
                return False

            # A payment that belongs to a mandate is authorized only by the
            # mandate's owner (MandateStore.approve passes exactly that).  Any
            # other 'user:<id>' -- the CLI operator, an LLM naming a victim --
            # is refused here, at the one gate every caller goes through.
            _meta = payment.metadata or {}
            if _meta.get('mandate_id') and approver_id != (
                    PERSON_ID_PREFIX + str(_meta.get('user_id') or '')):
                logger.warning(f"Payment {payment_id}: belongs to a mandate; "
                               f"only its owner may authorize it, not "
                               f"{approver_id!r}")
                return False

            # Add to approval chain
            payment.approval_chain.append({
                'approver_id': approver_id,
                'approved_at': datetime.now().isoformat(),
                'action': 'authorized'
            })

            payment.update_status(PaymentStatus.AUTHORIZED)

            logger.info(f"Payment {payment_id} authorized by {approver_id}")

            self.save_ledger()
            return True

    def cancel_payment(self, payment_id: str, actor_id: str,
                       reason: str = 'cancelled') -> bool:
        """Cancel a payment that has not reached the gateway yet.

        Allowed from PENDING, APPROVAL_REQUIRED or AUTHORIZED only: once a
        payment is PROCESSING or terminal the gateway owns it.
        """
        with self.lock:
            payment = self.payments.get(payment_id)
            if payment is None or payment.status not in (
                    PaymentStatus.PENDING, PaymentStatus.APPROVAL_REQUIRED,
                    PaymentStatus.AUTHORIZED):
                return False
            payment.approval_chain.append({
                'approver_id': actor_id,
                'approved_at': datetime.now().isoformat(),
                'action': 'cancelled',
            })
            payment.update_status(PaymentStatus.CANCELLED, reason)
            self.save_ledger()
            return True

    def process_payment(self, payment_id: str, *,
                        settler: bool = False) -> Dict[str, Any]:
        """
        Process an authorized payment through the gateway

        ``settler=True`` is passed only by the code that settles a mandate
        (ap2_mandate.settle and a kind's registered settler): a payment that
        belongs to a mandate is taken only after the owner's approval and a
        re-check of the live cart, so any other caller (the CLI, a generic
        tool) is refused rather than moving money with no order placed.

        The gateway calls are network I/O (Stripe SDK, PhonePe with a 15 s
        timeout), so they run OUTSIDE ``self.lock``: the payment is reserved
        as PROCESSING under the lock -- which is also what makes a concurrent
        or replayed call refuse, since it is no longer AUTHORIZED -- the
        gateway is called unlocked, and the outcome is committed under the
        lock.  Holding the lock across the call blocked every ledger
        operation behind one slow gateway.

        A redirect gateway (PhonePe) has no money yet when create_payment
        returns -- the person pays on the hosted page -- so capturing right
        away would mark it FAILED.  Such a payment stays PROCESSING and the
        result carries ``status='redirect_required'`` and the
        ``redirect_url``; ``success`` stays False because nothing was
        captured.

        Args:
            payment_id: Payment ID to process

        Returns:
            Processing result
        """
        with self.lock:
            if payment_id not in self.payments:
                return {'success': False, 'error': 'Payment not found'}

            payment = self.payments[payment_id]

            if (payment.metadata or {}).get('mandate_id') and not settler:
                return {
                    'success': False,
                    'error': 'this payment is settled only by the person who '
                             'approves it; it cannot be processed directly'
                }

            if payment.status != PaymentStatus.AUTHORIZED:
                return {
                    'success': False,
                    'error': f'Payment not authorized: {payment.status.value}'
                }

            # Get appropriate gateway
            gateway = self.gateways.get(payment.gateway)
            if not gateway:
                payment.update_status(PaymentStatus.FAILED, "Gateway not available")
                self.save_ledger()
                return {'success': False, 'error': 'Gateway not available'}

            # Reserve: from here no other caller can process this payment.
            payment.update_status(PaymentStatus.PROCESSING)
            self.save_ledger()

        try:
            result = gateway.create_payment(payment)
            capture_result = None
            if (result.get('success')
                    and payment.gateway not in REDIRECT_GATEWAYS):
                # Capture payment immediately for synchronous gateways
                capture_result = gateway.capture_payment(
                    payment.payment_id, result.get('transaction_id'))
        except Exception as e:
            logger.error(f"Error processing payment {payment_id}: {e}")
            with self.lock:
                payment.update_status(PaymentStatus.FAILED, str(e))
                self.save_ledger()
            return {'success': False, 'error': str(e)}

        with self.lock:
            if not result.get('success'):
                payment.update_status(
                    PaymentStatus.FAILED,
                    result.get('error', 'Gateway returned failure')
                )
                self.save_ledger()
                return result

            payment.gateway_transaction_id = result.get('transaction_id')

            if capture_result is None:
                # Redirect gateway: the person has not paid yet.
                payment.update_status(PaymentStatus.PROCESSING,
                                      'awaiting payment on the gateway page')
                self.save_ledger()
                return {
                    'success': False,
                    'status': 'redirect_required',
                    'redirect_url': result.get('redirect_url'),
                    'transaction_id': payment.gateway_transaction_id,
                }

            if capture_result.get('success'):
                payment.update_status(PaymentStatus.COMPLETED)
                logger.info(f"Payment {payment_id} completed successfully")
            else:
                payment.update_status(
                    PaymentStatus.FAILED,
                    capture_result.get('error', 'Capture failed')
                )
            self.save_ledger()
            return capture_result

    def get_payment(self, payment_id: str) -> Optional[PaymentRequest]:
        """Get payment request by ID"""
        with self.lock:
            return self.payments.get(payment_id)

    def find_by_gateway_transaction(self, gateway_transaction_id: str
                                    ) -> Optional[PaymentRequest]:
        """The payment a gateway transaction id belongs to, read under the
        lock (a callback must not iterate the dict while another thread
        writes it)."""
        if not gateway_transaction_id:
            return None
        with self.lock:
            for p in self.payments.values():
                if p.gateway_transaction_id == gateway_transaction_id:
                    return p
        return None

    def complete_once(self, payment_id: str, note: str) -> bool:
        """Mark a payment COMPLETED if, and only if, this call is the one that
        does it: True for the single caller that moved it from PROCESSING (or
        AUTHORIZED), False for every other -- already completed, terminal, or
        unknown.  A gateway delivers its confirmation more than once, and only
        one delivery may go on to place the order."""
        with self.lock:
            p = self.payments.get(payment_id)
            if p is None or p.status not in (PaymentStatus.PROCESSING,
                                             PaymentStatus.AUTHORIZED):
                return False
            p.update_status(PaymentStatus.COMPLETED, note)
            self.save_ledger()
            return True

    def revenue(self, period_days: int = 30) -> Dict[str, Decimal]:
        """The money this node's owner took in through the ledger in the last
        ``period_days``, by currency: COMPLETED payments of a REVENUE_KINDS
        kind on a live gateway (never the mock, which moves no money).
        The ONE reader of "ledger revenue"; revenue_aggregator.
        query_revenue_streams adds it to the canonical sum.  Counted from
        completed_at, on the ledger's own clock (datetime.now)."""
        cutoff = datetime.now() - timedelta(days=period_days)
        totals: Dict[str, Decimal] = {}
        with self.lock:
            for p in self.payments.values():
                if (p.status == PaymentStatus.COMPLETED
                        and p.gateway not in (None, PaymentGateway.MOCK)
                        and (p.metadata or {}).get('kind') in REVENUE_KINDS
                        and p.completed_at is not None and p.completed_at >= cutoff):
                    totals[p.currency] = totals.get(p.currency, Decimal('0')) + p.amount
        return totals

    def list_payments(
        self,
        agent_id: Optional[str] = None,
        status: Optional[PaymentStatus] = None
    ) -> List[PaymentRequest]:
        """
        List payment requests with optional filters

        Args:
            agent_id: Filter by agent ID
            status: Filter by status

        Returns:
            List of matching payment requests
        """
        with self.lock:
            results = list(self.payments.values())

            if agent_id:
                results = [p for p in results if p.requester_agent_id == agent_id]

            if status:
                results = [p for p in results if p.status == status]

            # Sort by creation time (newest first)
            results.sort(key=lambda p: p.created_at, reverse=True)

            return results

    def save_ledger(self):
        """Save payment ledger to disk"""
        try:
            os.makedirs(os.path.dirname(self.ledger_path), exist_ok=True)

            data = {
                'payments': {
                    pid: payment.to_dict()
                    for pid, payment in self.payments.items()
                },
                'last_updated': datetime.now().isoformat()
            }

            # Atomic (temp file + replace): a crash mid-write must not leave
            # a truncated ledger that the next load reads as empty.
            from core.file_cache import atomic_json_write
            atomic_json_write(self.ledger_path, data)

            logger.debug(f"Payment ledger saved to {self.ledger_path}")
        except Exception as e:
            logger.error(f"Failed to save payment ledger: {e}")

    @staticmethod
    def _payment_from_dict(pid: str, payment_data: Dict[str, Any]) -> 'PaymentRequest':
        payment = PaymentRequest(
            amount=Decimal(payment_data['amount']),
            currency=payment_data['currency'],
            description=payment_data['description'],
            requester_agent_id=payment_data['requester_agent_id'],
            payment_method=PaymentMethod(payment_data['payment_method']),
            metadata=payment_data.get('metadata', {})
        )
        payment.payment_id = pid
        payment.status = PaymentStatus(payment_data['status'])
        payment.created_at = datetime.fromisoformat(payment_data['created_at'])
        payment.updated_at = datetime.fromisoformat(payment_data['updated_at'])
        if payment_data.get('completed_at'):
            payment.completed_at = datetime.fromisoformat(payment_data['completed_at'])
        payment.gateway = PaymentGateway(payment_data['gateway']) if payment_data.get('gateway') else None
        payment.gateway_transaction_id = payment_data.get('gateway_transaction_id')
        payment.approval_chain = payment_data.get('approval_chain', [])
        payment.error_message = payment_data.get('error_message')
        return payment

    def _preserve_unreadable(self, reason: str) -> None:
        """Copy the ledger file aside before anything can overwrite it.

        A load that fails (or skips records) followed by the next save would
        otherwise replace real payment records with what little was read."""
        try:
            import shutil
            import time as _time
            backup = f"{self.ledger_path}.unreadable-{int(_time.time())}"
            shutil.copy2(self.ledger_path, backup)
            logger.error(f"Payment ledger {self.ledger_path}: {reason}; the "
                         f"original is kept at {backup}")
        except Exception as e:
            logger.error(f"Payment ledger {self.ledger_path}: {reason}, and it "
                         f"could not be copied aside: {e}")

    def load_ledger(self):
        """Load payment ledger from disk.  A record that cannot be read is
        skipped, and the file is copied aside first, so no save can lose it."""
        if not os.path.exists(self.ledger_path):
            return
        try:
            with open(self.ledger_path, 'r') as f:
                data = json.load(f)
        except Exception as e:
            self._preserve_unreadable(f'could not be read ({e})')
            return
        skipped = []
        for pid, payment_data in (data.get('payments') or {}).items():
            try:
                self.payments[pid] = self._payment_from_dict(pid, payment_data)
            except Exception as e:
                skipped.append(f'{pid}: {e}')
        if skipped:
            self._preserve_unreadable(f'{len(skipped)} record(s) unreadable '
                                      f'({skipped[0]})')
        logger.info(f"Loaded {len(self.payments)} payments from ledger")

    def _merge_legacy(self) -> None:
        """Bring in payments from the old CWD-relative ledger that the new one
        lacks, once.  The one-time copy ran only when the new file did not
        exist, so records written to the old path after that were invisible.
        Copy-never-delete, as before; a marker file stops repeat merges."""
        legacy = getattr(self, '_legacy_path', None)
        if (not legacy or not os.path.exists(legacy)
                or os.path.abspath(legacy) == os.path.abspath(self.ledger_path)):
            return
        marker = self.ledger_path + '.legacy_merged'
        if os.path.exists(marker):
            return
        try:
            with open(legacy, 'r') as f:
                old = json.load(f).get('payments') or {}
        except Exception as e:
            logger.warning(f"Payment ledger: legacy {legacy} unreadable: {e}")
            return
        added = 0
        with self.lock:
            for pid, d in old.items():
                if pid in self.payments:
                    continue
                try:
                    self.payments[pid] = self._payment_from_dict(pid, d)
                    added += 1
                except Exception as e:
                    logger.warning(f"Payment ledger: legacy record {pid} skipped: {e}")
            if added:
                self.save_ledger()
        try:
            with open(marker, 'w') as f:
                f.write(f'merged {added} from {legacy}\n')
        except OSError:
            pass
        if added:
            logger.info(f"Payment ledger: merged {added} legacy payment(s) from {legacy}")


# Global payment ledger instance
payment_ledger = PaymentLedger()


def get_payment_ledger() -> PaymentLedger:
    """The process-wide PaymentLedger -- the single writer of the payment
    ledger file.  ``hart pay`` (hartos/hart_cli.py) imports this name; it
    did not exist, so every ``hart pay`` command reported "AP2 not
    available"."""
    return payment_ledger


def _money(value) -> Optional[Decimal]:
    """A Cashfree amount (a JSON number or string) as rupees and paise, or
    None when it is not a number."""
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        return None


def settle_cashfree_link(link_id: str, ledger: 'PaymentLedger',
                         transaction_id: Optional[str] = None) -> Dict[str, Any]:
    """Record what Cashfree says of payment link ``link_id``: the ONE
    finalizer, shared by the signed webhook (commercial_api.cashfree_webhook)
    and the agents' check_payment_link.

    Nothing a webhook body says about the money is believed: Cashfree is
    asked (fetch_link), as the PhonePe callback asks PhonePe.  Paid in full,
    in the link's currency: the payment COMPLETES once (complete_once; a
    redelivery or a concurrent check answers already_completed and records
    nothing again) with its transaction id: ``transaction_id`` (the signed
    webhook's order.transaction_id) or else the paid order's id.  Paid
    another amount: FAILED with MONEY_TAKEN and a reconciliation line, never
    revenue.  Expired or cancelled: so marked.  Not paid yet: left waiting.
    ``retry`` True means Cashfree could not be asked, so the webhook answers
    non-2xx and Cashfree delivers again.
    """
    payment = ledger.find_by_gateway_transaction(link_id)
    if payment is None or payment.gateway != PaymentGateway.CASHFREE:
        return {'status': 'not_ours', 'retry': False}
    out = {'payment_id': payment.payment_id, 'retry': False}
    if payment.status == PaymentStatus.COMPLETED:
        return dict(out, status='already_completed')
    if payment.status != PaymentStatus.PROCESSING:
        return dict(out, status=payment.status.value)
    gateway = cashfree_gateway(ledger)
    if gateway is None:
        refuse_no_live_gateway('cashfree', (payment.metadata or {}).get('user_id'),
                               payment.payment_id, settings=CASHFREE_SETTINGS)
        return dict(out, status='not_configured', retry=True)
    looked = gateway.fetch_link(link_id)
    if not looked['success']:
        logger.warning('ap2: Cashfree link %s could not be checked (%s); payment %s '
                       'stays waiting', link_id, looked['error'], payment.payment_id)
        return dict(out, status='status_check_failed', retry=True)
    link = looked['link']
    state = str(link.get('link_status') or '').upper()
    if state == 'PAID':
        paid = _money(link.get('link_amount_paid'))
        currency = str(link.get('link_currency') or '').upper()
        expected = payment.amount.quantize(Decimal('0.01'))
        if paid != expected or currency != payment.currency:
            logger.error('MANUAL RECONCILIATION NEEDED: Cashfree link %s paid %s %s for '
                         'payment %s, which asked %s %s; not counted', link_id,
                         link.get('link_amount_paid'), currency, payment.payment_id,
                         expected, payment.currency)
            with ledger.lock:
                if payment.status == PaymentStatus.PROCESSING:
                    payment.update_status(
                        PaymentStatus.FAILED,
                        f'amount mismatch: paid {link.get("link_amount_paid")} {currency}, '
                        f'asked {expected} {payment.currency}')
                    payment.metadata = {**(payment.metadata or {}), MONEY_TAKEN: True}
                    ledger.save_ledger()
            return dict(out, status='amount_mismatch')
        orders = gateway.paid_orders(link_id)
        order_id = (orders[0].get('cf_order_id') or orders[0].get('order_id')) if orders else None
        txn = str(transaction_id) if transaction_id else (str(order_id) if order_id else None)
        if txn is None:
            logger.warning('ap2: Cashfree link %s is paid but no transaction id could be '
                           'read; payment %s is recorded without one', link_id,
                           payment.payment_id)
        with ledger.lock:
            if payment.status == PaymentStatus.PROCESSING:
                payment.metadata = {**(payment.metadata or {}), 'transaction_id': txn,
                                    'cashfree_order_id': str(order_id) if order_id else None}
        done = ledger.complete_once(payment.payment_id, 'Cashfree payment link paid')
        return dict(out, status='completed' if done else 'already_completed',
                    transaction_id=txn)
    if state in ('EXPIRED', 'CANCELLED'):
        with ledger.lock:
            if payment.status == PaymentStatus.PROCESSING:
                payment.update_status(PaymentStatus.EXPIRED if state == 'EXPIRED'
                                      else PaymentStatus.CANCELLED,
                                      f'Cashfree link {state.lower()}')
                ledger.save_ledger()
        return dict(out, status=state.lower())
    return dict(out, status='pending', link_status=state or 'unknown')


def create_payment_request_function(agent_name: str,
                                    user_id: Optional[str] = None) -> Callable:
    """
    Create a payment request function for an agent

    Args:
        agent_name: Name of the agent
        user_id: The person the agent works for.  When given, every payment
            gets an AP2 mandate owned by them (ap2_mandate), so only they can
            approve it -- on the approval card, never through a tool.

    Returns:
        Function that can be registered with autogen
    """
    def request_payment(
        amount: float,
        currency: str,
        description: str,
        payment_method: str = "internal_credits"
    ) -> str:
        """
        Request a payment as part of agent workflow

        Args:
            amount: Payment amount
            currency: Currency code (USD, EUR, etc.)
            description: Payment description
            payment_method: Payment method (credit_card, paypal, etc.)

        Returns:
            JSON string with payment details
        """
        try:
            method = PaymentMethod(payment_method.lower())
        except ValueError:
            method = PaymentMethod.INTERNAL_CREDITS
        # The currency as the mandate reads it (ap2_mandate.create_cart_mandate:
        # INR when the tool names none, upper-cased), decided ONCE so the gate
        # below and the mandate pick the same gateway: the gate read the raw
        # argument, so on a PhonePe-only node request_payment(10, '', ...)
        # was refused "no live payment gateway" where 'inr' minted on PhonePe
        # (the review of 3ec90cc58 measured it).
        currency = str(currency or 'INR').upper()

        if user_id:
            from integrations.ap2.ap2_mandate import (
                MandateError, get_mandate_store, request_human_approval)
            store = get_mandate_store()
            # Refused before the person is asked (the cart's prepare step
            # does the same): on a node with no gateway keys the mandate
            # would be minted on the mock and the Approve card shown, and
            # only the approval would be refused (review of 3a07d3989, F1).
            # settle()'s own gate stays for mandates minted earlier.
            if mock_would_settle(store.ledger.select_gateway(currency)):
                refuse_no_live_gateway('ap2', user_id)
                return json.dumps({
                    'success': False,
                    'error': f'{NO_LIVE_GATEWAY}; nothing was charged and the '
                             'person was not asked'}, indent=2)
            try:
                m = store.create_cart_mandate(
                    str(user_id), agent_name,
                    {'lines': [{'sku_id': 'payment', 'qty': 1,
                                'unit_price': amount}],
                     'total': amount, 'currency': currency},
                    description=description, requester_agent_id=agent_name)
            except MandateError as e:
                return json.dumps({'success': False, 'error': str(e)}, indent=2)
            asked = request_human_approval(m.payment_id, store)
            return json.dumps({
                'payment_id': m.payment_id,
                'amount': m.amount,
                'currency': m.currency,
                'status': 'approval_required',
                'approval_card_shown': asked.get('approval_card_shown'),
                'message': 'Payment request created. The person must approve '
                           'it on their screen; approving also completes it.'
            }, indent=2)

        payment = payment_ledger.create_payment_request(
            amount=Decimal(str(amount)),
            currency=currency,
            description=description,
            requester_agent_id=agent_name,
            payment_method=method,
            gateway=payment_ledger.select_gateway(currency)
        )

        return json.dumps({
            'payment_id': payment.payment_id,
            'amount': str(payment.amount),
            'currency': payment.currency,
            'status': payment.status.value,
            'message': f'Payment request created. Awaiting authorization.'
        }, indent=2)

    return request_payment


def create_payment_authorization_function(ask_only: bool = False) -> Callable:
    """
    Create the ``authorize_payment`` tool.

    ``ask_only`` (what agents get by default): the tool cannot authorize.  It
    shows the payment's owner the approval card and returns
    ``approval_required``; the person's answer reaches /api/agent/approval,
    which authorizes as the identity in their token
    (ap2_mandate.decide_payment).  The name is kept so saved recipes that
    call authorize_payment still resolve.

    Returns:
        Function that can be registered with autogen
    """
    if ask_only:
        def authorize_payment(payment_id: str, approver_id: str = "") -> str:
            """
            Ask the person to approve a pending payment.  Agents cannot approve.

            Args:
                payment_id: Payment ID awaiting approval
                approver_id: Ignored -- the approver is whoever the person's
                    login token says.

            Returns:
                JSON with status 'approval_required' on success
            """
            from integrations.ap2.ap2_mandate import request_human_approval
            return json.dumps(request_human_approval(payment_id), indent=2)

        return authorize_payment

    def authorize_payment(payment_id: str, approver_id: str = "") -> str:
        """
        Authorize a payment request

        Args:
            payment_id: Payment ID to authorize
            approver_id: ID of the approving person (never 'system')

        Returns:
            Authorization result
        """
        success = payment_ledger.authorize_payment(payment_id, approver_id)

        if success:
            return json.dumps({
                'success': True,
                'payment_id': payment_id,
                'message': 'Payment authorized successfully'
            }, indent=2)
        else:
            return json.dumps({
                'success': False,
                'payment_id': payment_id,
                'error': 'Authorization failed'
            }, indent=2)

    return authorize_payment


def create_payment_processing_function() -> Callable:
    """
    Create a payment processing function

    Returns:
        Function that can be used to process payments
    """
    def process_payment(payment_id: str) -> str:
        """
        Process an authorized payment

        Args:
            payment_id: Payment ID to process

        Returns:
            Processing result
        """
        payment = payment_ledger.get_payment(payment_id)
        if payment is not None and (payment.metadata or {}).get('mandate_id'):
            # A payment that belongs to a mandate is taken only by its
            # settler (ap2_mandate.settle / decide_payment), after the
            # person's approval and, for a checkout, a re-check of the live
            # cart.  This generic tool would charge it without either: money
            # moves and no order is placed.
            return json.dumps({
                'success': False,
                'payment_id': payment_id,
                'error': 'this payment is settled only by the person who '
                         'approves it; an agent cannot process it'},
                indent=2)
        result = payment_ledger.process_payment(payment_id)
        return json.dumps(result, indent=2)

    return process_payment


#: What the agent is told when no payment link can be made: no setting named
#: (the person reads the agent's reply), and the owner has been asked.
_NO_CASHFREE = ('payment links cannot be made yet: this node has no Cashfree '
                'account set up. The owner has been asked for the Cashfree keys; '
                'no link was made and nobody was charged. Tell the payer the link '
                'will follow, then try again later.')


def _ask_owner_for_cashfree(missing: List[str]) -> None:
    """Ask this computer's owner for each missing Cashfree credential through
    the one credential ask (hartos.ai_key_vault.request_credential: a
    consent card, the value kept in the vault).  tell_alias False: the
    gateway reads the value itself, so the agent is never handed an alias."""
    from hartos.ai_key_vault import request_credential
    labels = {CASHFREE_APP_ID: 'Cashfree App ID', CASHFREE_SECRET_KEY: 'Cashfree Secret Key'}
    for name in missing:
        try:
            request_credential(json.dumps({
                'resource_type': 'api_key', 'key_name': name,
                'label': labels.get(name, name),
                'service': 'Cashfree Payments (merchant.cashfree.com)',
                'used_by': 'payment links that sales and customer-care agents send',
                'description': 'so payers can pay through a Cashfree payment link',
                'where_to_get': 'Cashfree Merchant Dashboard, Payment Gateway, '
                                'Developers, API Keys'}), tell_alias=False)
        except Exception:
            logger.exception('ap2: the owner could not be asked for %s', name)


def _digits(text) -> str:
    return ''.join(ch for ch in str(text or '') if ch.isdigit())


def create_payment_link_function(agent_name: str,
                                 user_id: Optional[str] = None) -> Callable:
    """The ``create_payment_link`` tool: a Cashfree link a named payer opens
    to pay this node's owner, recorded in the ledger with the owner's billing
    fields (PAYMENT_LINK_RECORD_FIELDS)."""

    def create_payment_link(
        payer_name: str,
        payer_phone: str,
        amount_inr: float,
        purpose: str,
        plan: str = '',
        period: str = '',
        next_due_date: str = '',
        child_name: str = '',
        client_id: str = '',
        payer_email: str = '',
    ) -> str:
        """
        Make a payment link the payer opens to pay, and record it.

        Args:
            payer_name: Who pays (a centre's name, or a parent's name)
            payer_phone: The payer's phone number (Cashfree needs it)
            amount_inr: The amount in rupees, up to two decimals
            purpose: What the payment is for; the payer sees it
            plan: The plan bought, e.g. 'speech therapy pilot'
            period: The period it covers, e.g. '2026-12'
            next_due_date: When the next payment is due, YYYY-MM-DD
            child_name: The child it is for, when there is one
            client_id: The centre's client ID the payment runs under
            payer_email: The payer's email, optional

        Returns:
            JSON with link_url to send to the payer, and payment_id
        """
        name = str(payer_name or '').strip()
        phone = _digits(payer_phone)
        what = str(purpose or '').strip()
        problems = []
        if not name:
            problems.append('payer_name is empty')
        if not 10 <= len(phone) <= 15:
            problems.append('payer_phone needs 10 to 15 digits')
        if not what:
            problems.append('purpose is empty')
        try:
            amount = Decimal(str(amount_inr))
            if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal('0.01')):
                problems.append('amount_inr must be more than 0 with at most two decimals')
        except (InvalidOperation, TypeError, ValueError):
            amount = None
            problems.append('amount_inr is not a number')
        due = str(next_due_date or '').strip()
        if due:
            try:
                due = date.fromisoformat(due).isoformat()
            except ValueError:
                problems.append('next_due_date must be a date as YYYY-MM-DD')
        if problems:
            return json.dumps({'success': False, 'error': '; '.join(problems)
                               + '. Nothing was made; fix these and call again.'}, indent=2)

        ledger = get_payment_ledger()
        gateway = cashfree_gateway(ledger)
        if gateway is None:
            refuse_no_live_gateway('cashfree', user_id, settings=CASHFREE_SETTINGS)
            _ask_owner_for_cashfree(CashfreePaymentGateway().missing_settings())
            return json.dumps({'success': False, 'error': _NO_CASHFREE}, indent=2)

        record = {
            'kind': PAYMENT_LINK_KIND,
            'user_id': str(user_id) if user_id else None,
            'payer_name': name, 'payer_phone': phone,
            'payer_email': str(payer_email or '').strip() or None,
            'child': str(child_name or '').strip() or None,
            'plan': str(plan or '').strip() or None,
            'period': str(period or '').strip() or None,
            'next_due_date': due or None,
            'transaction_id': None,
            'client_id': str(client_id or '').strip() or None,
        }
        payment = ledger.create_payment_request(
            amount=amount, currency='INR', description=what[:500],
            requester_agent_id=agent_name, gateway=PaymentGateway.CASHFREE,
            metadata=record)
        made = gateway.create_payment(payment)
        with ledger.lock:
            if not made.get('success'):
                payment.update_status(PaymentStatus.FAILED, made.get('error'))
            else:
                payment.gateway_transaction_id = made['transaction_id']
                payment.metadata = {**(payment.metadata or {}),
                                    'link_url': made['redirect_url'],
                                    'cf_link_id': made.get('cf_link_id'),
                                    'link_expiry_time': made.get('link_expiry_time')}
                payment.update_status(PaymentStatus.PROCESSING,
                                      'awaiting payment on the Cashfree link')
            ledger.save_ledger()
        if not made.get('success'):
            return json.dumps({'success': False, 'payment_id': payment.payment_id,
                               'error': f"no link was made: {made.get('error')}"}, indent=2)
        return json.dumps({
            'success': True,
            'payment_id': payment.payment_id,
            'link_url': made['redirect_url'],
            'amount': str(amount.quantize(Decimal('0.01'))),
            'currency': 'INR',
            'expires_at': made.get('link_expiry_time'),
            'message': 'Send link_url to the payer in your reply. The payment is '
                       'recorded when Cashfree confirms it; check_payment_link '
                       'with this payment_id says whether it is paid.'}, indent=2)

    # The payer's contact details and the child's name stay in the ledger
    # file: the tool-execution log line withholds them (core.tool_logging).
    from core.tool_logging import PRIVATE_ARGUMENTS_ATTR
    setattr(create_payment_link, PRIVATE_ARGUMENTS_ATTR,
            ('payer_name', 'payer_phone', 'payer_email', 'child_name'))
    return create_payment_link


def check_payment_link_function(user_id: Optional[str] = None) -> Callable:
    """The ``check_payment_link`` tool: asks Cashfree whether a link this
    node made is paid, through the one finalizer (settle_cashfree_link), so
    a payment is recorded even when no webhook reaches the node."""

    def check_payment_link(payment_id: str) -> str:
        """
        Say whether a payment link has been paid.

        Args:
            payment_id: The payment_id create_payment_link returned

        Returns:
            JSON with status ('completed' means paid) and transaction_id
        """
        ledger = get_payment_ledger()
        payment = ledger.get_payment(str(payment_id or '').strip())
        meta = (payment.metadata or {}) if payment is not None else {}
        if (payment is None or meta.get('kind') != PAYMENT_LINK_KIND
                or (user_id and meta.get('user_id') not in (None, str(user_id)))):
            return json.dumps({'success': False, 'error': 'no payment link has that '
                               'payment_id; use the one create_payment_link returned'},
                              indent=2)
        settled = {}
        if payment.status == PaymentStatus.PROCESSING and payment.gateway_transaction_id:
            settled = settle_cashfree_link(payment.gateway_transaction_id, ledger)
        payment = ledger.get_payment(payment.payment_id)
        meta = payment.metadata or {}
        body = {'success': True, 'payment_id': payment.payment_id,
                'status': payment.status.value,
                'paid': payment.status == PaymentStatus.COMPLETED,
                'amount': str(payment.amount), 'currency': payment.currency,
                'transaction_id': meta.get('transaction_id')}
        if settled.get('retry'):
            body['note'] = 'Cashfree could not be asked just now; try again later'
        return json.dumps(body, indent=2)

    return check_payment_link


def get_ap2_tools_for_autogen(agent_name: str,
                              user_id: Optional[str] = None,
                              allow_llm_authorize: Optional[bool] = None
                              ) -> List[Dict[str, Any]]:
    """
    Get AP2 payment tools for autogen agent registration

    The model can never self-authorize by default: ``authorize_payment`` only
    ASKS the payment's owner (an approval card), and the ledger refuses
    'system' or the requesting agent as an approver in any case.  A node that
    deliberately wants a tool that authorizes directly sets
    ``AP2_ALLOW_LLM_AUTHORIZE=1`` (or passes ``allow_llm_authorize=True``);
    the caller must then still name a person.

    Args:
        agent_name: Name of the agent
        user_id: The person the agent works for (owner of its payments)
        allow_llm_authorize: None reads ``AP2_ALLOW_LLM_AUTHORIZE`` (default
            False).

    Returns:
        List of tool definitions for autogen
    """
    if allow_llm_authorize is None:
        from core.config_cache import env_flag
        allow_llm_authorize = env_flag('AP2_ALLOW_LLM_AUTHORIZE', False)
    return [
        {
            'function': create_payment_request_function(agent_name, user_id),
            'name': 'request_payment',
            'description': 'Request a payment transaction for services or resources. The person approves it on their screen. Returns payment_id for tracking.'
        },
        {
            'function': create_payment_authorization_function(
                ask_only=not allow_llm_authorize),
            'name': 'authorize_payment',
            'description': (
                'Authorize a pending payment request. Requires payment_id.'
                if allow_llm_authorize else
                'Ask the person to approve a pending payment (shows them an '
                'approval card). Agents cannot approve payments; the result is '
                'approval_required until the person answers. Requires payment_id.')
        },
        {
            'function': create_payment_processing_function(),
            'name': 'process_payment',
            'description': 'Process a payment the person has approved (approving usually completes it already). Requires payment_id.'
        },
        {
            'function': create_payment_link_function(agent_name, user_id),
            'name': 'create_payment_link',
            'description': ('Make a Cashfree payment link a customer (a centre or a '
                            'family) opens to pay in rupees, and send its link_url in '
                            'your reply. Needs payer_name, payer_phone, amount_inr and '
                            'purpose; also records plan, period, next_due_date, '
                            'child_name and client_id.')
        },
        {
            'function': check_payment_link_function(user_id),
            'name': 'check_payment_link',
            'description': ('Say whether a payment link from create_payment_link has '
                            'been paid. Requires payment_id.')
        }
    ]


# Convenience exports
__all__ = [
    'PaymentStatus', 'PaymentMethod', 'PaymentGateway',
    'PaymentRequest', 'PaymentLedger', 'PaymentGatewayConnector',
    'MockPaymentGateway', 'StripePaymentGateway', 'PhonePePaymentGateway',
    'payment_ledger', 'get_payment_ledger',
    'create_payment_request_function', 'create_payment_authorization_function',
    'create_payment_processing_function', 'get_ap2_tools_for_autogen',
    'own_gateway_urls', 'PHONEPE_CALLBACK_PATH', 'PHONEPE_REFUND_CALLBACK_PATH',
    'PHONEPE_RETURN_PATH', 'PHONEPE_SENT_AMOUNT_KEY',
    'mock_settlement_allowed', 'mock_would_settle', 'NO_LIVE_GATEWAY',
    'LIVE_GATEWAY_SETTINGS', 'NO_LIVE_GATEWAY_REASON', 'refuse_no_live_gateway',
    'CashfreePaymentGateway', 'cashfree_gateway', 'settle_cashfree_link',
    'create_payment_link_function', 'check_payment_link_function',
    'CASHFREE_WEBHOOK_PATH', 'CASHFREE_SETTINGS', 'PAYMENT_LINK_KIND',
    'REVENUE_KINDS', 'operator_usd_inr_rate',
]


# The credentials this module reads from the environment.  A value the
# owner stored in the vault is delivered there for these names
# (hartos.ai_key_vault.reads_from_env); tests/unit/
# test_env_secrets_declared.py fails on a secret read not declared.
# The Cashfree App ID is not secret, but the owner enters it on the same
# card, so it is delivered the same way.
ENV_SECRETS = (
    'CASHFREE_APP_ID',
    'CASHFREE_SECRET_KEY',
    'PHONEPE_SALT_KEY',
    'STRIPE_API_KEY',
    'STRIPE_WEBHOOK_SECRET',
)
