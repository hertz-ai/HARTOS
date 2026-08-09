"""
Test suite for AP2 (Agent Protocol 2) - Agentic Commerce integration

This test suite validates:
1. Payment request creation
2. Payment authorization workflow
3. Payment processing through gateway
4. Payment ledger persistence
5. Multi-agent payment coordination
6. Integration with task_ledger
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from decimal import Decimal
import base64
import hashlib
import hmac
import json
import pytest
from integrations.ap2 import (
    PaymentStatus, PaymentMethod, PaymentGateway,
    PaymentRequest, PaymentLedger, PaymentGatewayConnector, MockPaymentGateway,
    PhonePePaymentGateway,
    payment_ledger, create_payment_request_function,
    create_payment_authorization_function, create_payment_processing_function,
    get_ap2_tools_for_autogen
)


@pytest.fixture
def payment_id():
    """Create a pending payment and return its ID."""
    ledger = PaymentLedger(ledger_path="agent_data/test_payment_ledger.json")
    payment = ledger.create_payment_request(
        amount=Decimal("99.99"),
        currency="USD",
        description="Test payment for API credits",
        requester_agent_id="test_agent_1",
        payment_method=PaymentMethod.INTERNAL_CREDITS,
        gateway=PaymentGateway.MOCK
    )
    return payment.payment_id


def test_payment_request_creation():
    """Test creating a payment request"""
    print("\n" + "=" * 80)
    print("TEST 1: Payment Request Creation")
    print("=" * 80)

    ledger = PaymentLedger(ledger_path="agent_data/test_payment_ledger.json")

    payment = ledger.create_payment_request(
        amount=Decimal("99.99"),
        currency="USD",
        description="Test payment for API credits",
        requester_agent_id="test_agent_1",
        payment_method=PaymentMethod.INTERNAL_CREDITS,
        gateway=PaymentGateway.MOCK
    )

    assert payment.payment_id is not None
    assert payment.amount == Decimal("99.99")
    assert payment.currency == "USD"
    assert payment.status == PaymentStatus.PENDING
    assert payment.requester_agent_id == "test_agent_1"

    print(f"[OK] Payment request created successfully")
    print(f"   Payment ID: {payment.payment_id}")
    print(f"   Amount: ${payment.amount} {payment.currency}")
    print(f"   Status: {payment.status.value}")

    return payment.payment_id


def test_payment_authorization(payment_id):
    """Test authorizing a payment"""
    print("\n" + "=" * 80)
    print("TEST 2: Payment Authorization")
    print("=" * 80)

    ledger = PaymentLedger(ledger_path="agent_data/test_payment_ledger.json")

    success = ledger.authorize_payment(payment_id, "admin_user")

    assert success == True

    payment = ledger.get_payment(payment_id)
    assert payment.status == PaymentStatus.AUTHORIZED
    assert len(payment.approval_chain) == 1
    assert payment.approval_chain[0]['approver_id'] == "admin_user"

    print(f"[OK] Payment authorized successfully")
    print(f"   Payment ID: {payment_id}")
    print(f"   Status: {payment.status.value}")
    print(f"   Approved by: {payment.approval_chain[0]['approver_id']}")


def test_payment_processing(payment_id):
    """Test processing an authorized payment"""
    print("\n" + "=" * 80)
    print("TEST 3: Payment Processing")
    print("=" * 80)

    ledger = PaymentLedger(ledger_path="agent_data/test_payment_ledger.json")

    # Authorize first (required before processing)
    ledger.authorize_payment(payment_id, "admin_user")
    result = ledger.process_payment(payment_id)

    assert result['success'] == True

    payment = ledger.get_payment(payment_id)
    assert payment.status == PaymentStatus.COMPLETED
    assert payment.gateway_transaction_id is not None

    print(f"[OK] Payment processed successfully")
    print(f"   Payment ID: {payment_id}")
    print(f"   Status: {payment.status.value}")
    print(f"   Gateway Transaction: {payment.gateway_transaction_id}")


def test_payment_listing():
    """Test listing payments with filters"""
    print("\n" + "=" * 80)
    print("TEST 4: Payment Listing and Filtering")
    print("=" * 80)

    ledger = PaymentLedger(ledger_path="agent_data/test_payment_ledger.json")

    # Create multiple payments
    for i in range(3):
        ledger.create_payment_request(
            amount=Decimal(str(10.00 * (i + 1))),
            currency="USD",
            description=f"Test payment {i+1}",
            requester_agent_id=f"agent_{i}",
            payment_method=PaymentMethod.INTERNAL_CREDITS
        )

    # List all payments
    all_payments = ledger.list_payments()
    assert len(all_payments) >= 4  # 1 from earlier tests + 3 new ones

    # Filter by agent
    agent_payments = ledger.list_payments(agent_id="agent_1")
    assert len(agent_payments) >= 1

    # Filter by status
    completed_payments = ledger.list_payments(status=PaymentStatus.COMPLETED)
    assert len(completed_payments) >= 1

    print(f"[OK] Payment listing works correctly")
    print(f"   Total payments: {len(all_payments)}")
    print(f"   Agent 1 payments: {len(agent_payments)}")
    print(f"   Completed payments: {len(completed_payments)}")


def test_mock_gateway():
    """Test mock payment gateway"""
    print("\n" + "=" * 80)
    print("TEST 5: Mock Payment Gateway")
    print("=" * 80)

    gateway = MockPaymentGateway()
    gateway.connect()

    assert gateway.connected == True

    # Create test payment
    payment = PaymentRequest(
        amount=Decimal("50.00"),
        currency="USD",
        description="Gateway test",
        requester_agent_id="test_agent"
    )

    # Test gateway operations
    create_result = gateway.create_payment(payment)
    assert create_result['success'] == True
    assert 'transaction_id' in create_result

    txn_id = create_result['transaction_id']

    capture_result = gateway.capture_payment(payment.payment_id, txn_id)
    assert capture_result['success'] == True

    print(f"[OK] Mock gateway works correctly")
    print(f"   Transaction created: {txn_id}")
    print(f"   Payment captured successfully")


def test_autogen_tool_functions():
    """Test autogen tool function generation"""
    print("\n" + "=" * 80)
    print("TEST 6: Autogen Tool Functions")
    print("=" * 80)

    # Get tools for autogen
    tools = get_ap2_tools_for_autogen("test_agent")

    assert len(tools) == 3  # request, authorize, process
    assert all('function' in tool for tool in tools)
    assert all('name' in tool for tool in tools)
    assert all('description' in tool for tool in tools)

    tool_names = [tool['name'] for tool in tools]
    assert 'request_payment' in tool_names
    assert 'authorize_payment' in tool_names
    assert 'process_payment' in tool_names

    print(f"[OK] Autogen tools generated correctly")
    print(f"   Tools: {', '.join(tool_names)}")

    # Test using the request_payment function
    request_func = next(t['function'] for t in tools if t['name'] == 'request_payment')
    result_json = request_func(
        amount=25.50,
        currency="EUR",
        description="Tool function test",
        payment_method="internal_credits"
    )

    result = json.loads(result_json)
    assert 'payment_id' in result
    assert result['currency'] == "EUR"
    assert result['status'] == 'pending'

    print(f"[OK] request_payment tool function works")
    print(f"   Created payment: {result['payment_id']}")


def test_ledger_persistence():
    """Test payment ledger persistence"""
    print("\n" + "=" * 80)
    print("TEST 7: Ledger Persistence")
    print("=" * 80)

    test_ledger_path = "agent_data/test_persistence_ledger.json"

    # Create ledger and add payment
    ledger1 = PaymentLedger(ledger_path=test_ledger_path)
    payment1 = ledger1.create_payment_request(
        amount=Decimal("123.45"),
        currency="GBP",
        description="Persistence test",
        requester_agent_id="persist_agent"
    )
    payment_id = payment1.payment_id

    # Load ledger again and verify payment exists
    ledger2 = PaymentLedger(ledger_path=test_ledger_path)
    payment2 = ledger2.get_payment(payment_id)

    assert payment2 is not None
    assert payment2.amount == Decimal("123.45")
    assert payment2.currency == "GBP"
    assert payment2.requester_agent_id == "persist_agent"

    print(f"[OK] Ledger persistence works correctly")
    print(f"   Payment saved and loaded: {payment_id}")
    print(f"   Amount: £{payment2.amount}")

    # Cleanup
    if os.path.exists(test_ledger_path):
        os.remove(test_ledger_path)


def test_complete_payment_workflow():
    """Test complete end-to-end payment workflow"""
    print("\n" + "=" * 80)
    print("TEST 8: Complete Payment Workflow")
    print("=" * 80)

    ledger = PaymentLedger(ledger_path="agent_data/test_workflow_ledger.json")

    # Step 1: Agent requests payment
    print("\n  Step 1: Agent requests payment...")
    payment = ledger.create_payment_request(
        amount=Decimal("199.99"),
        currency="USD",
        description="API service subscription",
        requester_agent_id="service_agent",
        payment_method=PaymentMethod.STRIPE,
        gateway=PaymentGateway.MOCK,
        metadata={
            'service': 'premium_api',
            'duration': '1 month',
            'user_id': 'user_12345'
        }
    )
    assert payment.status == PaymentStatus.PENDING
    print(f"     + Payment requested: {payment.payment_id}")

    # Step 2: User/Admin authorizes payment
    print("\n  Step 2: Admin authorizes payment...")
    success = ledger.authorize_payment(payment.payment_id, "admin_john")
    assert success == True
    payment = ledger.get_payment(payment.payment_id)
    assert payment.status == PaymentStatus.AUTHORIZED
    print(f"     + Payment authorized by admin_john")

    # Step 3: System processes payment through gateway
    print("\n  Step 3: Processing through gateway...")
    result = ledger.process_payment(payment.payment_id)
    assert result['success'] == True
    payment = ledger.get_payment(payment.payment_id)
    assert payment.status == PaymentStatus.COMPLETED
    print(f"     + Payment completed: {payment.gateway_transaction_id}")

    # Step 4: Verify payment details
    print("\n  Step 4: Verifying payment details...")
    payment_dict = payment.to_dict()
    assert payment_dict['amount'] == "199.99"
    assert payment_dict['currency'] == "USD"
    assert len(payment_dict['approval_chain']) == 1
    assert payment_dict['metadata']['service'] == 'premium_api'
    print(f"     + All details verified")

    print(f"\n[OK] Complete workflow executed successfully")

    # Cleanup
    if os.path.exists("agent_data/test_workflow_ledger.json"):
        os.remove("agent_data/test_workflow_ledger.json")


# =============================================================================
# process_payment() authorization gate, double-charge/replay guard, and
# gateway-failure degrade paths.
#
# These are the security-critical branches the happy-path tests above never
# exercise or assert: a weakened authorization gate = authorization bypass; a
# weakened replay guard = silent double-charge; a mishandled gateway failure =
# a payment stuck in a non-terminal state instead of FAILED.  Each test drives
# the REAL PaymentLedger.process_payment and mocks only the gateway boundary,
# asserting both the returned result AND the observable side effects (persisted
# status, whether the gateway was actually invoked).
# =============================================================================


class _SpyGateway(PaymentGatewayConnector):
    """Controllable gateway boundary double.

    Records how many times create_payment / capture_payment are invoked so a
    test can prove the ledger did (or crucially did NOT) attempt to charge the
    gateway.  create/capture results and a create-time exception are all
    injectable so every degrade branch of process_payment is reachable.
    """

    def __init__(self, create_result=None, capture_result=None, create_raises=None):
        super().__init__(PaymentGateway.MOCK)
        self._create_result = create_result if create_result is not None else {
            'success': True, 'transaction_id': 'txn_spy_ok', 'status': 'authorized'
        }
        self._capture_result = capture_result if capture_result is not None else {
            'success': True, 'status': 'captured'
        }
        self._create_raises = create_raises
        self.create_calls = 0
        self.capture_calls = 0
        self.connected = True

    def connect(self) -> bool:
        self.connected = True
        return True

    def create_payment(self, payment_request):
        self.create_calls += 1
        if self._create_raises is not None:
            raise self._create_raises
        return self._create_result

    def capture_payment(self, payment_id, gateway_transaction_id):
        self.capture_calls += 1
        return self._capture_result

    def refund_payment(self, payment_id, gateway_transaction_id, amount=None):
        return {'success': True, 'status': 'refunded'}


def _ledger_with_gateway(tmp_path, spy):
    """Fresh ledger with an isolated on-disk path and the MOCK gateway
    slot replaced by the caller's spy (so a MOCK-routed payment hits it)."""
    ledger = PaymentLedger(ledger_path=str(tmp_path / "spy_ledger.json"))
    ledger.gateways[PaymentGateway.MOCK] = spy
    return ledger


def _authorized_payment(ledger, gateway=PaymentGateway.MOCK, amount="10.00"):
    payment = ledger.create_payment_request(
        amount=Decimal(amount),
        currency="USD",
        description="degrade-path test",
        requester_agent_id="agent_degrade",
        payment_method=PaymentMethod.INTERNAL_CREDITS,
        gateway=gateway,
    )
    assert ledger.authorize_payment(payment.payment_id, "admin_approver") is True
    return payment.payment_id


class TestProcessPaymentAuthorizationGate:
    """Refuse to process anything that is not currently AUTHORIZED."""

    def test_unknown_payment_id_returns_not_found(self, tmp_path):
        ledger = PaymentLedger(ledger_path=str(tmp_path / "l.json"))
        result = ledger.process_payment("no-such-payment")
        assert result == {'success': False, 'error': 'Payment not found'}

    def test_pending_payment_refused_and_gateway_never_charged(self, tmp_path):
        # A PENDING (un-authorized) payment must NOT reach the gateway — this
        # is the authorization bypass guard.
        spy = _SpyGateway()
        ledger = _ledger_with_gateway(tmp_path, spy)
        payment = ledger.create_payment_request(
            amount=Decimal("42.00"), currency="USD", description="x",
            requester_agent_id="agent_x", gateway=PaymentGateway.MOCK,
        )
        assert payment.status == PaymentStatus.PENDING

        result = ledger.process_payment(payment.payment_id)

        assert result['success'] is False
        assert 'not authorized' in result['error'].lower()
        # Boundary never touched -> no charge attempted.
        assert spy.create_calls == 0
        assert spy.capture_calls == 0
        # Status must be untouched (still PENDING so it can be authorized later),
        # NOT silently flipped to FAILED.
        assert ledger.get_payment(payment.payment_id).status == PaymentStatus.PENDING

    def test_failed_payment_cannot_be_reprocessed(self, tmp_path):
        # After a gateway create-failure the payment is FAILED; re-processing
        # must be refused (FAILED != AUTHORIZED), and must not re-hit the gateway.
        spy = _SpyGateway(create_result={'success': False, 'error': 'declined'})
        ledger = _ledger_with_gateway(tmp_path, spy)
        pid = _authorized_payment(ledger)

        first = ledger.process_payment(pid)
        assert first['success'] is False
        assert ledger.get_payment(pid).status == PaymentStatus.FAILED
        assert spy.create_calls == 1

        second = ledger.process_payment(pid)
        assert second['success'] is False
        assert 'not authorized' in second['error'].lower()
        # No second charge attempt.
        assert spy.create_calls == 1
        assert spy.capture_calls == 0


class TestProcessPaymentReplayGuard:
    """A COMPLETED payment must never be charged a second time."""

    def test_completed_payment_cannot_be_double_charged(self, tmp_path):
        spy = _SpyGateway()
        ledger = _ledger_with_gateway(tmp_path, spy)
        pid = _authorized_payment(ledger)

        first = ledger.process_payment(pid)
        assert first['success'] is True
        assert ledger.get_payment(pid).status == PaymentStatus.COMPLETED
        assert spy.create_calls == 1
        assert spy.capture_calls == 1

        # Replay the exact same call — the double-charge guard must refuse.
        second = ledger.process_payment(pid)
        assert second['success'] is False
        assert 'not authorized' in second['error'].lower()
        # Critically: the gateway was NOT invoked a second time.
        assert spy.create_calls == 1
        assert spy.capture_calls == 1
        # Terminal state preserved.
        assert ledger.get_payment(pid).status == PaymentStatus.COMPLETED

    def test_completed_payment_cannot_be_reauthorized(self, tmp_path):
        # The replay guard rests on status leaving AUTHORIZED.  Prove it cannot
        # be re-opened: authorize_payment refuses a non-PENDING payment, so a
        # completed charge can't be re-armed for a second process_payment.
        spy = _SpyGateway()
        ledger = _ledger_with_gateway(tmp_path, spy)
        pid = _authorized_payment(ledger)
        assert ledger.process_payment(pid)['success'] is True
        assert ledger.get_payment(pid).status == PaymentStatus.COMPLETED

        assert ledger.authorize_payment(pid, "attacker") is False
        assert ledger.get_payment(pid).status == PaymentStatus.COMPLETED
        # And a follow-up process still refuses / never re-charges.
        assert ledger.process_payment(pid)['success'] is False
        assert spy.create_calls == 1


class TestProcessPaymentGatewayDegradePaths:
    """Every gateway-failure mode must degrade to a FAILED terminal state."""

    def test_gateway_unavailable_marks_failed(self, tmp_path):
        # Route to a gateway that is not registered (PAYPAL is never added by
        # default) -> lookup returns None -> FAILED, no exception.
        ledger = PaymentLedger(ledger_path=str(tmp_path / "l.json"))
        assert ledger.gateways.get(PaymentGateway.PAYPAL) is None
        pid = _authorized_payment(ledger, gateway=PaymentGateway.PAYPAL)

        result = ledger.process_payment(pid)

        assert result == {'success': False, 'error': 'Gateway not available'}
        payment = ledger.get_payment(pid)
        assert payment.status == PaymentStatus.FAILED
        assert payment.error_message == "Gateway not available"

    def test_create_failure_marks_failed_without_capture(self, tmp_path):
        spy = _SpyGateway(create_result={'success': False, 'error': 'card declined'})
        ledger = _ledger_with_gateway(tmp_path, spy)
        pid = _authorized_payment(ledger)

        result = ledger.process_payment(pid)

        assert result['success'] is False
        assert result['error'] == 'card declined'
        # Capture must never run when create failed.
        assert spy.create_calls == 1
        assert spy.capture_calls == 0
        payment = ledger.get_payment(pid)
        assert payment.status == PaymentStatus.FAILED
        assert payment.error_message == 'card declined'

    def test_capture_failure_marks_failed(self, tmp_path):
        spy = _SpyGateway(
            create_result={'success': True, 'transaction_id': 'txn_created_1'},
            capture_result={'success': False, 'error': 'capture timeout'},
        )
        ledger = _ledger_with_gateway(tmp_path, spy)
        pid = _authorized_payment(ledger)

        result = ledger.process_payment(pid)

        # process_payment returns the capture result verbatim on capture failure.
        assert result['success'] is False
        assert result['error'] == 'capture timeout'
        assert spy.create_calls == 1
        assert spy.capture_calls == 1
        payment = ledger.get_payment(pid)
        assert payment.status == PaymentStatus.FAILED
        assert payment.error_message == 'capture timeout'
        # The gateway transaction id from create is still recorded for audit.
        assert payment.gateway_transaction_id == 'txn_created_1'

    def test_create_missing_error_key_uses_default_message(self, tmp_path):
        # A gateway that reports failure without an 'error' key must still
        # degrade cleanly (not KeyError) using the fallback message.
        spy = _SpyGateway(create_result={'success': False})
        ledger = _ledger_with_gateway(tmp_path, spy)
        pid = _authorized_payment(ledger)

        result = ledger.process_payment(pid)

        assert result['success'] is False
        payment = ledger.get_payment(pid)
        assert payment.status == PaymentStatus.FAILED
        assert payment.error_message == 'Gateway returned failure'

    def test_gateway_exception_is_caught_and_marks_failed(self, tmp_path):
        # An exception raised inside the gateway must be caught, recorded, and
        # the payment marked FAILED — never propagate out of process_payment.
        spy = _SpyGateway(create_raises=RuntimeError("network boom"))
        ledger = _ledger_with_gateway(tmp_path, spy)
        pid = _authorized_payment(ledger)

        result = ledger.process_payment(pid)

        assert result['success'] is False
        assert result['error'] == 'network boom'
        assert spy.capture_calls == 0
        payment = ledger.get_payment(pid)
        assert payment.status == PaymentStatus.FAILED
        assert 'network boom' in (payment.error_message or '')

    def test_failed_status_persisted_to_disk(self, tmp_path):
        # The degrade path must survive a reload — a fresh ledger reading the
        # same file must see FAILED, proving save_ledger ran on the failure path.
        spy = _SpyGateway(create_raises=RuntimeError("boom"))
        ledger_path = str(tmp_path / "persist_fail.json")
        ledger = PaymentLedger(ledger_path=ledger_path)
        ledger.gateways[PaymentGateway.MOCK] = spy
        pid = _authorized_payment(ledger)
        ledger.process_payment(pid)

        reloaded = PaymentLedger(ledger_path=ledger_path)
        assert reloaded.get_payment(pid).status == PaymentStatus.FAILED


# =============================================================================
# PhonePePaymentGateway.verify_callback() — S2S webhook signature verification.
#
# This is the trust boundary between the public internet and money settling in
# the ledger.  PhonePe posts `{"response": "<base64>"}` with an
# `X-VERIFY: sha256(base64_response + salt_key) + "###" + salt_index` header;
# HARTOS must accept ONLY a callback whose header matches the digest computed
# with the merchant's own secret salt.  A weakened digest/salt-index compare, or
# a removed not-connected/empty-salt fail-closed guard, would let a forged or
# tampered "PAYMENT_SUCCESS" callback verify and settle silently (revenue loss +
# fraudulent tier grants).
#
# Every test below drives the REAL PhonePePaymentGateway.verify_callback and
# asserts observable behaviour.  The only "boundary" touched is hmac.compare_digest
# (patched in exactly one test to prove a constant-time compare is used, not `==`).
# No network is involved — verify_callback is pure crypto over its two arguments.
# =============================================================================

_SALT_KEY = "s3cr3t_merchant_salt"
_SALT_INDEX = "1"

# A realistic PhonePe S2S "payment succeeded" body, base64-encoded exactly as it
# arrives on the wire inside {"response": <this>}.
_SUCCESS_B64 = base64.b64encode(json.dumps({
    "success": True,
    "code": "PAYMENT_SUCCESS",
    "message": "Your payment is successful.",
    "data": {
        "merchantId": "MERCHANT_X",
        "merchantTransactionId": "hartos_abc123",
        "amount": 90000,
        "state": "COMPLETED",
    },
}).encode("utf-8")).decode("utf-8")


def _connected_phonepe(merchant_id="MERCHANT_X", salt_key=_SALT_KEY,
                       salt_index=_SALT_INDEX, env="UAT"):
    """A PhonePe gateway with explicit creds (env-independent) and connected."""
    g = PhonePePaymentGateway(merchant_id=merchant_id, salt_key=salt_key,
                              salt_index=salt_index, env=env)
    assert g.connect() is True
    assert g.connected is True
    return g


def _valid_header(b64_payload, salt_key=_SALT_KEY, salt_index=_SALT_INDEX):
    """Compute the X-VERIFY header PhonePe would send for this payload+salt."""
    digest = hashlib.sha256((b64_payload + salt_key).encode("utf-8")).hexdigest()
    return f"{digest}###{salt_index}"


class TestPhonePeVerifyCallbackAccepts:
    """A genuine, correctly-signed callback must verify."""

    def test_valid_signature_verifies(self):
        g = _connected_phonepe()
        header = _valid_header(_SUCCESS_B64)
        assert g.verify_callback(_SUCCESS_B64, header) is True

    def test_valid_with_non_default_salt_index(self):
        # Merchant configured salt_index='2'; a header signed with ###2 must pass.
        g = _connected_phonepe(salt_index="2")
        header = _valid_header(_SUCCESS_B64, salt_index="2")
        assert g.verify_callback(_SUCCESS_B64, header) is True


class TestPhonePeVerifyCallbackRejectsForgery:
    """The security core: reject anything not signed with the merchant secret."""

    def test_forged_success_wrong_salt_key_rejected(self):
        # Attacker crafts a PAYMENT_SUCCESS body and signs it — but does not know
        # the merchant salt.  The signature must NOT verify.
        g = _connected_phonepe()
        forged_header = _valid_header(_SUCCESS_B64, salt_key="attacker_guess")
        assert g.verify_callback(_SUCCESS_B64, forged_header) is False

    def test_tampered_payload_with_stale_signature_rejected(self):
        # Attacker captures a valid (b64, header) pair for a benign body, then
        # swaps in a forged "success" body while replaying the old header.
        benign_b64 = base64.b64encode(json.dumps({
            "success": True, "code": "PAYMENT_PENDING",
            "data": {"amount": 1},
        }).encode("utf-8")).decode("utf-8")
        g = _connected_phonepe()
        stale_header = _valid_header(benign_b64)          # valid for benign body
        assert g.verify_callback(_SUCCESS_B64, stale_header) is False  # not the success body

    def test_wrong_salt_index_rejected_even_with_right_digest(self):
        # Digest is correct, but the appended salt index does not match the
        # merchant's configured index -> the salt-index compare must reject.
        g = _connected_phonepe(salt_index="1")
        digest = hashlib.sha256((_SUCCESS_B64 + _SALT_KEY).encode("utf-8")).hexdigest()
        wrong_index_header = f"{digest}###9"
        assert g.verify_callback(_SUCCESS_B64, wrong_index_header) is False

    def test_bare_digest_without_index_rejected(self):
        # A header that is just the digest (no "###index" suffix) must not pass —
        # guards against a prefix/substring style weakened compare.
        g = _connected_phonepe()
        digest = hashlib.sha256((_SUCCESS_B64 + _SALT_KEY).encode("utf-8")).hexdigest()
        assert g.verify_callback(_SUCCESS_B64, digest) is False

    def test_salt_key_of_a_different_merchant_rejected(self):
        # Signature valid for merchant A's salt must be rejected by merchant B's
        # gateway — proves the digest actually binds to this gateway's secret.
        gw_b = _connected_phonepe(salt_key="merchant_B_salt")
        header_for_a = _valid_header(_SUCCESS_B64, salt_key=_SALT_KEY)
        assert gw_b.verify_callback(_SUCCESS_B64, header_for_a) is False


class TestPhonePeVerifyCallbackFailsClosed:
    """Degrade / empty / malformed inputs must fail closed (return False)."""

    def test_not_connected_rejects_forgery_signed_with_empty_salt(self, monkeypatch):
        # THE critical fail-closed guard: with no credentials the gateway is
        # disconnected and holds an EMPTY salt.  If the `not self.connected`
        # guard were removed, an attacker could forge a valid signature using
        # the empty key (no secret needed) and settle a fake payment.
        for k in ("PHONEPE_MERCHANT_ID", "PHONEPE_SALT_KEY",
                  "PHONEPE_SALT_INDEX", "PHONEPE_ENV"):
            monkeypatch.delenv(k, raising=False)
        g = PhonePePaymentGateway()
        assert g.connect() is False
        assert g.connected is False
        assert g.api_key == ""  # no secret to protect the boundary
        # Forge exactly what the gateway itself would compute with its empty key.
        forged_header = _valid_header(_SUCCESS_B64, salt_key=g.api_key,
                                      salt_index=g.salt_index)
        assert g.verify_callback(_SUCCESS_B64, forged_header) is False

    def test_none_and_empty_inputs_rejected(self):
        g = _connected_phonepe()
        assert g.verify_callback(None, "sig###1") is False
        assert g.verify_callback(_SUCCESS_B64, None) is False
        assert g.verify_callback("", "sig###1") is False
        assert g.verify_callback(_SUCCESS_B64, "") is False
        assert g.verify_callback("", "") is False
        assert g.verify_callback(None, None) is False

    def test_garbage_header_rejected(self):
        g = _connected_phonepe()
        assert g.verify_callback(_SUCCESS_B64, "not-a-signature") is False
        assert g.verify_callback(_SUCCESS_B64, "###1") is False
        assert g.verify_callback(_SUCCESS_B64, "deadbeef###1###extra") is False

    def test_non_ascii_header_does_not_raise_and_rejects(self):
        # hmac.compare_digest raises TypeError on non-ASCII str comparison; the
        # method must catch it and fail closed, never propagate an exception out
        # onto the webhook handler (which would 500 or, worse, be mishandled).
        g = _connected_phonepe()
        result = g.verify_callback(_SUCCESS_B64, "café_signature###1")
        assert result is False


class TestPhonePeVerifyCallbackContract:
    """Lock in the exact crypto contract so a silent weakening is caught."""

    def test_uses_constant_time_compare(self, monkeypatch):
        # Prove the method routes through hmac.compare_digest (constant-time),
        # not a naive `==`, which would leak the digest byte-by-byte via timing.
        calls = []
        real = hmac.compare_digest

        def _spy(a, b):
            calls.append((a, b))
            return real(a, b)

        monkeypatch.setattr(hmac, "compare_digest", _spy)
        g = _connected_phonepe()
        header = _valid_header(_SUCCESS_B64)
        assert g.verify_callback(_SUCCESS_B64, header) is True
        assert len(calls) == 1, "verify_callback must use hmac.compare_digest exactly once"
        # It compared the fully-assembled expected header (digest###index), not
        # just the bare digest — so the salt index is inside the constant-time compare.
        expected_header, _received = calls[0]
        assert expected_header == header
        assert expected_header.endswith(f"###{_SALT_INDEX}")

    def test_digest_binds_base64_payload_exactly(self):
        # One trailing byte of difference in the payload must invalidate the
        # signature — the digest covers the exact base64 string, no normalisation.
        g = _connected_phonepe()
        header = _valid_header(_SUCCESS_B64)
        assert g.verify_callback(_SUCCESS_B64 + "=", header) is False

    def test_replay_of_identical_valid_callback_still_verifies(self):
        # HONEST CONTRACT: verify_callback authenticates the SIGNATURE only; it is
        # not a freshness/replay oracle.  A byte-identical genuine callback verifies
        # every time.  Replay defense lives upstream in the ledger's idempotent
        # settlement keyed on merchantTransactionId (see process_payment replay
        # guard tests above), NOT in this signature check.  This test documents
        # that boundary so nobody mistakes signature validity for replay safety.
        g = _connected_phonepe()
        header = _valid_header(_SUCCESS_B64)
        assert g.verify_callback(_SUCCESS_B64, header) is True
        assert g.verify_callback(_SUCCESS_B64, header) is True


def run_all_tests():
    """Run all AP2 integration tests"""
    print("\n" + "=" * 80)
    print("AP2 (AGENT PROTOCOL 2) - AGENTIC COMMERCE")
    print("Integration Test Suite")
    print("=" * 80)

    try:
        # Test 1: Create payment
        payment_id = test_payment_request_creation()

        # Test 2: Authorize payment
        test_payment_authorization(payment_id)

        # Test 3: Process payment
        test_payment_processing(payment_id)

        # Test 4: List payments
        test_payment_listing()

        # Test 5: Mock gateway
        test_mock_gateway()

        # Test 6: Autogen tools
        test_autogen_tool_functions()

        # Test 7: Persistence
        test_ledger_persistence()

        # Test 8: Complete workflow
        test_complete_payment_workflow()

        print("\n" + "=" * 80)
        print("ALL TESTS PASSED [OK]")
        print("=" * 80)
        print("\nAP2 Integration Summary:")
        print("  [OK] Payment request creation")
        print("  [OK] Payment authorization")
        print("  [OK] Payment processing")
        print("  [OK] Payment listing and filtering")
        print("  [OK] Mock gateway operations")
        print("  [OK] Autogen tool functions")
        print("  [OK] Ledger persistence")
        print("  [OK] Complete payment workflow")
        print("\nAP2 is ready for production use!")

    except AssertionError as e:
        print(f"\n[FAIL] TEST FAILED: {e}")
        raise
    except Exception as e:
        print(f"\n[FAIL] ERROR: {e}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    run_all_tests()
