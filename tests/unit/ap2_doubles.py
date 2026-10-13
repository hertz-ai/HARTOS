"""A live synchronous card rail for the AP2 and commerce tests.

``PaymentLedger.select_gateway`` answers MOCK whenever neither PhonePe nor
Stripe is registered, and since abb2dcfaf a Mock payment settles nothing for
a person (ap2_protocol.mock_would_settle), so a ledger with only the mock is
the "node with no payment keys" case, not the normal one.  Test modules that
want a keyed node register ``CardRailDouble`` (Stripe's shape: create, then
capture) with ``ledger.add_gateway``; the no-keys tests pop it again.  One
double, imported by tests/unit/test_commerce_tools.py and
tests/unit/test_ap2_mandate.py (the review of abb2dcfaf found the mandate
tests pinning the mock settling on a mock-only ledger).
"""
from integrations.ap2 import ap2_protocol


class CardRailDouble(ap2_protocol.PaymentGatewayConnector):
    def __init__(self):
        super().__init__(ap2_protocol.PaymentGateway.STRIPE)
        self.captures = []

    def connect(self):
        self.connected = True
        return True

    def create_payment(self, payment_request):
        return {'success': True,
                'transaction_id': f'card_{payment_request.payment_id}'}

    def capture_payment(self, payment_id, gateway_transaction_id):
        self.captures.append((payment_id, gateway_transaction_id))
        return {'success': True}
