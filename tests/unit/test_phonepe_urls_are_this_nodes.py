"""PhonePe is sent this node's URLs, never a guessed host (task #122).

The gateway's pay request used to default the buyer's return URL and the
server callback to the web app's host, which answers the API paths with
the web app's 404: the signed confirmation never arrived, so the money was
taken and no tier was bumped and no order placed.  The refund request named
a callback route that existed nowhere.  Now the URLs are the payment's own
(its metadata) or this node's (core.public_url), the gateway refuses to
start a payment without them, the upgrade route never takes a callback URL
from its caller, and the refund callback has a signature-checked landing.

Real PhonePePaymentGateway and PaymentLedger; only requests.post (the wire
to PhonePe) and the token-to-user lookup are stubbed.
"""
import base64
import json
import logging
import types
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

import integrations.ap2 as ap2_pkg
from integrations.ap2.ap2_protocol import (
    PaymentGateway, PaymentLedger, PaymentMethod, PaymentRequest, PaymentStatus,
    PhonePePaymentGateway,
)

BASE = 'https://node.example'
CALLBACK = '/api/v1/intelligence/phonepe/callback'
REFUND_CALLBACK = '/api/v1/intelligence/phonepe/refund-callback'


def _gateway():
    g = PhonePePaymentGateway(merchant_id='M', salt_key='S', salt_index='1', env='UAT')
    g.connect()
    return g


def _request(metadata=None):
    return PaymentRequest(amount=Decimal('9.00'), currency='USD', description='x',
                          requester_agent_id='user:u-1',
                          payment_method=PaymentMethod.INTERNAL_CREDITS,
                          metadata=metadata or {})


def _phonepe_says(body):
    resp = MagicMock()
    resp.json.return_value = body
    return resp


_CHECKOUT_MINTED = {'success': True, 'data': {'instrumentResponse': {
    'redirectInfo': {'url': 'https://pay.example/checkout/1'}}}}


def _sent(post):
    """The request PhonePe received, decoded from its base64 envelope."""
    return json.loads(base64.b64decode(post.call_args.kwargs['json']['request']))


class TestThePayRequestsUrls:
    def test_the_payments_own_urls_are_sent(self, monkeypatch):
        monkeypatch.delenv('HARTOS_PUBLIC_URL', raising=False)
        req = _request({'redirect_url': 'https://app.example/back',
                        'callback_url': BASE + CALLBACK})
        with patch('requests.post', return_value=_phonepe_says(_CHECKOUT_MINTED)) as post:
            out = _gateway().create_payment(req)
        assert out['success'] is True
        assert out['redirect_url'] == 'https://pay.example/checkout/1'
        sent = _sent(post)
        assert (sent['redirectUrl'], sent['callbackUrl']) == (
            'https://app.example/back', BASE + CALLBACK)

    def test_this_nodes_urls_fill_what_the_payment_did_not_name(self, monkeypatch):
        monkeypatch.setenv('HARTOS_PUBLIC_URL', BASE)
        with patch('requests.post', return_value=_phonepe_says(_CHECKOUT_MINTED)) as post:
            _gateway().create_payment(_request({'redirect_url': 'https://app.example/back'}))
        sent = _sent(post)
        assert sent['redirectUrl'] == 'https://app.example/back'
        assert sent['callbackUrl'] == BASE + CALLBACK

    def test_a_payment_naming_nothing_gets_both_of_this_nodes_urls(self, monkeypatch):
        monkeypatch.setenv('HARTOS_PUBLIC_URL', BASE + '/')
        with patch('requests.post', return_value=_phonepe_says(_CHECKOUT_MINTED)) as post:
            _gateway().create_payment(_request())
        sent = _sent(post)
        assert (sent['redirectUrl'], sent['callbackUrl']) == (BASE + '/', BASE + CALLBACK)

    def test_without_a_public_url_nothing_is_sent_and_the_setting_is_named(self, monkeypatch):
        monkeypatch.delenv('HARTOS_PUBLIC_URL', raising=False)
        with patch('requests.post') as post:
            out = _gateway().create_payment(_request())
        assert out['success'] is False
        assert 'HARTOS_PUBLIC_URL' in out['error']
        post.assert_not_called()


class TestTheRefundRequestsCallback:
    def test_the_refund_callback_is_this_nodes_route(self, monkeypatch):
        monkeypatch.setenv('HARTOS_PUBLIC_URL', BASE + '/')
        with patch('requests.post',
                   return_value=_phonepe_says({'success': True, 'code': 'PAYMENT_PENDING'})) as post:
            out = _gateway().refund_payment('pay-1', 'hartos_txn_1', Decimal('5'))
        assert out['success'] is True
        assert _sent(post)['callbackUrl'] == BASE + REFUND_CALLBACK

    def test_without_a_public_url_no_callback_is_named(self, monkeypatch):
        monkeypatch.delenv('HARTOS_PUBLIC_URL', raising=False)
        with patch('requests.post',
                   return_value=_phonepe_says({'success': True, 'code': 'PAYMENT_PENDING'})) as post:
            _gateway().refund_payment('pay-1', 'hartos_txn_1')
        assert 'callbackUrl' not in _sent(post)


# ── the routes: the upgrade initiation and the refund callback ───────────

class _Minting(PhonePePaymentGateway):
    """Connected; trusts X-VERIFY 'good'; mints a checkout without the wire."""

    def __init__(self):
        super().__init__(merchant_id='M', salt_key='S', salt_index='1', env='UAT')
        self.connected = True

    def verify_callback(self, b64_response, x_verify_header):
        return x_verify_header == 'good'

    def create_payment(self, payment_request):
        return {'success': True, 'transaction_id': 'hartos_t1', 'status': 'pending',
                'redirect_url': 'https://pay.example/checkout/1'}


@pytest.fixture
def ctx(tmp_path):
    ledger = PaymentLedger(ledger_path=str(tmp_path / 'l.json'))
    ledger.gateways[PaymentGateway.PHONEPE] = _Minting()
    from integrations.agent_engine.commercial_api import commercial_api_bp
    app = Flask(__name__)
    app.register_blueprint(commercial_api_bp)
    with patch.object(ap2_pkg, 'payment_ledger', ledger):
        yield {'client': app.test_client(), 'ledger': ledger, 'app': app}


def _signed_in():
    user = types.SimpleNamespace(id='u-1', email=None)
    api_key = types.SimpleNamespace(id='k1', tier='free', rate_limit_per_day=100,
                                    monthly_quota=3000)
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = api_key
    return patch('integrations.social.auth._get_user_from_token', return_value=(user, db))


def _initiate(ctx, body):
    with _signed_in():
        return ctx['client'].post('/api/v1/intelligence/keys/k1/upgrade/phonepe',
                                  json=body, headers={'Authorization': 'Bearer tok'})


class TestTheUpgradeRouteNamesNoCallback:
    def test_a_callers_callback_url_is_ignored_and_its_return_page_kept(self, ctx):
        r = _initiate(ctx, {'target_tier': 'starter',
                            'redirect_url': 'https://app.example/back',
                            'callback_url': 'https://elsewhere.example/cb'})
        assert r.status_code == 202, r.get_json()
        (payment,) = ctx['ledger'].payments.values()
        assert payment.metadata.get('redirect_url') == 'https://app.example/back'
        assert 'callback_url' not in payment.metadata
        assert payment.status == PaymentStatus.PROCESSING

    def test_a_body_naming_no_urls_stamps_none(self, ctx):
        """The gateway resolves this node's URLs when it mints the checkout;
        the route no longer writes a guessed host into the payment."""
        r = _initiate(ctx, {'target_tier': 'starter'})
        assert r.status_code == 202, r.get_json()
        (payment,) = ctx['ledger'].payments.values()
        assert 'redirect_url' not in payment.metadata
        assert 'callback_url' not in payment.metadata


def _refund_callback(client, sig='good', envelope=None):
    envelope = envelope or {'code': 'PAYMENT_SUCCESS',
                            'data': {'merchantTransactionId': 'refund_abc',
                                     'transactionId': 'T1', 'amount': 500}}
    b64 = base64.b64encode(json.dumps(envelope).encode()).decode()
    return client.post(REFUND_CALLBACK, json={'response': b64}, headers={'X-VERIFY': sig})


class TestTheRefundCallbackLandsSomewhere:
    def test_a_verified_refund_is_acknowledged_and_named_for_a_person(self, ctx, caplog):
        log = logging.getLogger('hevolve_social')
        log.addHandler(caplog.handler)
        level = log.level
        log.setLevel(logging.WARNING)
        try:
            r = _refund_callback(ctx['client'])
        finally:
            log.removeHandler(caplog.handler)
            log.setLevel(level)
        assert r.status_code == 200
        assert r.get_json() == {'success': True, 'status': 'acknowledged',
                                'refund_id': 'refund_abc', 'code': 'PAYMENT_SUCCESS'}
        said = [rec for rec in caplog.records if 'refund_abc' in rec.getMessage()]
        assert said and said[0].levelno == logging.WARNING, caplog.records

    def test_a_bad_signature_is_refused(self, ctx):
        assert _refund_callback(ctx['client'], sig='bad').status_code == 401

    def test_a_missing_signature_is_refused(self, ctx):
        assert ctx['client'].post(REFUND_CALLBACK, json={}).status_code == 400

    def test_an_unreadable_payload_is_refused(self, ctx):
        r = ctx['client'].post(REFUND_CALLBACK, json={'response': 'not base64 json'},
                               headers={'X-VERIFY': 'good'})
        assert r.status_code == 400

    def test_no_gateway_registered_is_a_503(self, ctx):
        del ctx['ledger'].gateways[PaymentGateway.PHONEPE]
        assert _refund_callback(ctx['client']).status_code == 503

    def test_the_routes_are_where_the_gateway_sends_phonepe(self, ctx):
        """The gateway composes its callback URLs from these constants; the
        blueprint must serve exactly those paths."""
        from integrations.ap2.ap2_protocol import (
            PHONEPE_CALLBACK_PATH, PHONEPE_REFUND_CALLBACK_PATH,
        )
        assert (PHONEPE_CALLBACK_PATH, PHONEPE_REFUND_CALLBACK_PATH) == (CALLBACK, REFUND_CALLBACK)
        rules = {r.rule for r in ctx['app'].url_map.iter_rules()}
        assert PHONEPE_CALLBACK_PATH in rules
        assert PHONEPE_REFUND_CALLBACK_PATH in rules
