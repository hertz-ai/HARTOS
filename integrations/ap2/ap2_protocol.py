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

import json
import logging
import threading
import uuid
import os
from typing import Dict, List, Any, Optional, Callable
from datetime import datetime
from enum import Enum
from decimal import Decimal
import hashlib

logger = logging.getLogger(__name__)


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
    MOCK = "mock"  # For testing


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
            'metadata': self.metadata
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


class PaymentLedger:
    """
    Payment transaction ledger with audit trail

    Integrates with task_ledger to track payment workflows
    """

    def __init__(self, ledger_path: str = "agent_data/payment_ledger.json"):
        """
        Initialize payment ledger

        Args:
            ledger_path: Path to persist payment ledger
        """
        self.ledger_path = ledger_path
        self.payments: Dict[str, PaymentRequest] = {}
        self.lock = threading.Lock()
        self.gateways: Dict[PaymentGateway, PaymentGatewayConnector] = {}

        # Default to mock gateway for testing
        self.add_gateway(MockPaymentGateway())

        self.load_ledger()

    def add_gateway(self, gateway: PaymentGatewayConnector):
        """Add a payment gateway connector"""
        with self.lock:
            self.gateways[gateway.gateway] = gateway
            gateway.connect()
            logger.info(f"Added payment gateway: {gateway.gateway.value}")

    def create_payment_request(
        self,
        amount: Decimal,
        currency: str,
        description: str,
        requester_agent_id: str,
        payment_method: PaymentMethod = PaymentMethod.INTERNAL_CREDITS,
        gateway: PaymentGateway = PaymentGateway.MOCK,
        metadata: Optional[Dict[str, Any]] = None
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
            self.payments[payment.payment_id] = payment

            logger.info(f"Created payment request: {payment.payment_id} - ${amount} {currency}")

            self.save_ledger()
            return payment

    def authorize_payment(self, payment_id: str, approver_id: str) -> bool:
        """
        Authorize a payment request

        Args:
            payment_id: Payment ID to authorize
            approver_id: ID of the approver (user or agent)

        Returns:
            True if authorization successful
        """
        with self.lock:
            if payment_id not in self.payments:
                logger.error(f"Payment not found: {payment_id}")
                return False

            payment = self.payments[payment_id]

            if payment.status != PaymentStatus.PENDING:
                logger.warning(f"Payment {payment_id} not in pending state: {payment.status}")
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

    def process_payment(self, payment_id: str) -> Dict[str, Any]:
        """
        Process an authorized payment through the gateway

        Args:
            payment_id: Payment ID to process

        Returns:
            Processing result
        """
        with self.lock:
            if payment_id not in self.payments:
                return {'success': False, 'error': 'Payment not found'}

            payment = self.payments[payment_id]

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

            # Create payment in gateway
            payment.update_status(PaymentStatus.PROCESSING)
            self.save_ledger()

            try:
                result = gateway.create_payment(payment)

                if result.get('success'):
                    payment.gateway_transaction_id = result.get('transaction_id')

                    # Capture payment immediately for now
                    capture_result = gateway.capture_payment(
                        payment.payment_id,
                        payment.gateway_transaction_id
                    )

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
                else:
                    payment.update_status(
                        PaymentStatus.FAILED,
                        result.get('error', 'Gateway returned failure')
                    )
                    self.save_ledger()
                    return result

            except Exception as e:
                logger.error(f"Error processing payment {payment_id}: {e}")
                payment.update_status(PaymentStatus.FAILED, str(e))
                self.save_ledger()
                return {'success': False, 'error': str(e)}

    def get_payment(self, payment_id: str) -> Optional[PaymentRequest]:
        """Get payment request by ID"""
        with self.lock:
            return self.payments.get(payment_id)

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

            with open(self.ledger_path, 'w') as f:
                json.dump(data, f, indent=2)

            logger.debug(f"Payment ledger saved to {self.ledger_path}")
        except Exception as e:
            logger.error(f"Failed to save payment ledger: {e}")

    def load_ledger(self):
        """Load payment ledger from disk"""
        try:
            if os.path.exists(self.ledger_path):
                with open(self.ledger_path, 'r') as f:
                    data = json.load(f)

                # Reconstruct payment objects
                for pid, payment_data in data.get('payments', {}).items():
                    payment = PaymentRequest(
                        amount=Decimal(payment_data['amount']),
                        currency=payment_data['currency'],
                        description=payment_data['description'],
                        requester_agent_id=payment_data['requester_agent_id'],
                        payment_method=PaymentMethod(payment_data['payment_method']),
                        metadata=payment_data.get('metadata', {})
                    )

                    # Restore state
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

                    self.payments[pid] = payment

                logger.info(f"Loaded {len(self.payments)} payments from ledger")
        except Exception as e:
            logger.warning(f"Could not load payment ledger: {e}")


# Global payment ledger instance
payment_ledger = PaymentLedger()


def create_payment_request_function(agent_name: str) -> Callable:
    """
    Create a payment request function for an agent

    Args:
        agent_name: Name of the agent

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

        payment = payment_ledger.create_payment_request(
            amount=Decimal(str(amount)),
            currency=currency,
            description=description,
            requester_agent_id=agent_name,
            payment_method=method,
            gateway=PaymentGateway.MOCK
        )

        return json.dumps({
            'payment_id': payment.payment_id,
            'amount': str(payment.amount),
            'currency': payment.currency,
            'status': payment.status.value,
            'message': f'Payment request created. Awaiting authorization.'
        }, indent=2)

    return request_payment


def create_payment_authorization_function() -> Callable:
    """
    Create a payment authorization function

    Returns:
        Function that can be used to authorize payments
    """
    def authorize_payment(payment_id: str, approver_id: str = "system") -> str:
        """
        Authorize a payment request

        Args:
            payment_id: Payment ID to authorize
            approver_id: ID of the approver

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
        result = payment_ledger.process_payment(payment_id)
        return json.dumps(result, indent=2)

    return process_payment


def get_ap2_tools_for_autogen(agent_name: str) -> List[Dict[str, Any]]:
    """
    Get AP2 payment tools for autogen agent registration

    Args:
        agent_name: Name of the agent

    Returns:
        List of tool definitions for autogen
    """
    return [
        {
            'function': create_payment_request_function(agent_name),
            'name': 'request_payment',
            'description': 'Request a payment transaction for services or resources. Returns payment_id for tracking.'
        },
        {
            'function': create_payment_authorization_function(),
            'name': 'authorize_payment',
            'description': 'Authorize a pending payment request. Requires payment_id.'
        },
        {
            'function': create_payment_processing_function(),
            'name': 'process_payment',
            'description': 'Process an authorized payment through the gateway. Requires payment_id.'
        }
    ]


# Convenience exports
__all__ = [
    'PaymentStatus', 'PaymentMethod', 'PaymentGateway',
    'PaymentRequest', 'PaymentLedger', 'PaymentGatewayConnector',
    'MockPaymentGateway', 'payment_ledger',
    'create_payment_request_function', 'create_payment_authorization_function',
    'create_payment_processing_function', 'get_ap2_tools_for_autogen'
]
