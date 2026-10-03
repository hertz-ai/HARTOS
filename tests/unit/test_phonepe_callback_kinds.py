"""PhonePe S2S callback (commercial_api.phonepe_callback) for AP2 payments
that are NOT API-tier upgrades.

PaymentLedger.select_gateway routes INR to PhonePe, so an agent's
request_payment and a McGroce cart mandate can land on the one PhonePe
callback.  Once the status API confirms the money, such a payment must
COMPLETE -- the tier-upgrade metadata check used to mark it FAILED -- and a
commerce checkout must go on to place its order.
"""
import base64
import json
from decimal import Decimal
from unittest.mock import patch

import pytest
from flask import Flask

import integrations.ap2 as ap2_pkg
from integrations.ap2.ap2_protocol import (
    PaymentGateway, PaymentLedger, PaymentStatus, PhonePePaymentGateway,
)


class _TrustingPhonePe(PhonePePaymentGateway):
    def __init__(self):
        super().__init__(merchant_id='M', salt_key='S')
        self.connected = True

    def verify_callback(self, b64_response, x_verify_header):
        return x_verify_header == 'good'

    def capture_payment(self, payment_id, gateway_transaction_id):
        return {'success': True, 'status': 'captured'}


@pytest.fixture
def ctx(tmp_path):
    ledger = PaymentLedger(ledger_path=str(tmp_path / 'l.json'))
    ledger.gateways[PaymentGateway.PHONEPE] = _TrustingPhonePe()
    from integrations.agent_engine.commercial_api import commercial_api_bp
    app = Flask(__name__)
    app.register_blueprint(commercial_api_bp)
    with patch.object(ap2_pkg, 'payment_ledger', ledger):
        yield {'client': app.test_client(), 'ledger': ledger}


def _processing(ledger, metadata):
    p = ledger.create_payment_request(
        amount=Decimal('240'), currency='INR', description='x',
        requester_agent_id='user:u', gateway=PaymentGateway.PHONEPE,
        metadata=metadata)
    p.status = PaymentStatus.PROCESSING
    p.gateway_transaction_id = 'hartos_txn_1'
    return p


def _callback(client, sig='good'):
    envelope = {'code': 'PAYMENT_SUCCESS',
                'data': {'merchantTransactionId': 'hartos_txn_1'}}
    b64 = base64.b64encode(json.dumps(envelope).encode()).decode()
    return client.post('/api/v1/intelligence/phonepe/callback',
                       json={'response': b64}, headers={'X-VERIFY': sig})


def test_commerce_payment_completes_and_places_the_order(ctx):
    p = _processing(ctx['ledger'], {'kind': 'commerce_checkout', 'mandate_id': 'mdt_1'})
    with patch('integrations.commerce.commerce_tools.complete_redirect_checkout',
               return_value={'success': True, 'order_id': 'ORD-1'}) as place:
        r = _callback(ctx['client'])
    assert r.status_code == 200
    body = r.get_json()
    assert body['status'] == 'completed' and body['order'] == {'success': True, 'order_id': 'ORD-1'}
    place.assert_called_once_with(p.payment_id)
    assert ctx['ledger'].get_payment(p.payment_id).status == PaymentStatus.COMPLETED


def test_agent_payment_without_kind_completes(ctx):
    p = _processing(ctx['ledger'], {})
    with patch('integrations.commerce.commerce_tools.complete_redirect_checkout') as place:
        r = _callback(ctx['client'])
    assert r.get_json()['status'] == 'completed'
    place.assert_not_called()
    assert ctx['ledger'].get_payment(p.payment_id).status == PaymentStatus.COMPLETED


def test_order_step_failure_still_completes_the_payment(ctx):
    p = _processing(ctx['ledger'], {'kind': 'commerce_checkout'})
    with patch('integrations.commerce.commerce_tools.complete_redirect_checkout',
               side_effect=RuntimeError('mcgroce down')):
        r = _callback(ctx['client'])
    assert r.status_code == 200 and r.get_json()['order'] is None
    assert ctx['ledger'].get_payment(p.payment_id).status == PaymentStatus.COMPLETED


def test_tier_upgrade_path_is_unchanged(ctx):
    p = _processing(ctx['ledger'], {'kind': 'tier_upgrade'})
    r = _callback(ctx['client'])
    assert r.get_json()['error'] == 'Payment metadata malformed'
    assert ctx['ledger'].get_payment(p.payment_id).status == PaymentStatus.FAILED


def test_bad_signature_changes_nothing(ctx):
    p = _processing(ctx['ledger'], {'kind': 'commerce_checkout'})
    assert _callback(ctx['client'], sig='bad').status_code == 401
    assert ctx['ledger'].get_payment(p.payment_id).status == PaymentStatus.PROCESSING
