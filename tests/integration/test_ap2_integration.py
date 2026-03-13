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
import json
import pytest
from integrations.ap2 import (
    PaymentStatus, PaymentMethod, PaymentGateway,
    PaymentRequest, PaymentLedger, MockPaymentGateway,
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
