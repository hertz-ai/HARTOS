"""Behavioural tests for the /api/commerce/* blueprint (commerce_api.py)."""
import json
import os
import time
from unittest.mock import patch

import pytest
from flask import Flask

from integrations.ap2.ap2_mandate import MandateStore
from integrations.ap2.ap2_protocol import PaymentLedger
from integrations.commerce import signing
from integrations.commerce.bindings import CommerceBindings
from integrations.commerce.commerce_api import commerce_bp
from integrations.social.auth import decode_jwt, generate_jwt

SECRET = 's3cret-shared-with-tomcat'
CART = {'lines': [{'sku_id': 1, 'qty': 1, 'unit_price': 50}],
        'total': 50, 'currency': 'INR'}


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv('COMMERCE_SESSION_SECRET', SECRET)
    bindings = CommerceBindings(str(tmp_path / 'b.json'))
    ledger = PaymentLedger(ledger_path=str(tmp_path / 'l.json'))
    mandates = MandateStore(str(tmp_path / 'm.json'), ledger=ledger, key=b'k' * 32)
    app = Flask(__name__)
    app.register_blueprint(commerce_bp)
    with patch('integrations.commerce.bindings.get_bindings', return_value=bindings), \
            patch('integrations.ap2.ap2_mandate.get_mandate_store', return_value=mandates):
        yield {'client': app.test_client(), 'bindings': bindings,
               'mandates': mandates}


ROUTE = '/api/commerce/session'


def _signed_headers(raw, secret=SECRET, ts=None, nonce=None, route=ROUTE):
    """What McGroce's AgentTokenServiceImpl sends: never the secret itself."""
    ts = int(time.time()) if ts is None else ts
    nonce = nonce or os.urandom(16).hex()
    return {signing.TIMESTAMP_HEADER: str(ts), signing.NONCE_HEADER: nonce,
            signing.SIGNATURE_HEADER: signing.request_signature(
                secret, 'POST', route, ts, nonce, raw)}


def _session(client, secret=SECRET, headers=None, raw=None, **body):
    body = body or {'customerId': 4242, 'username': 'asha@example.com',
                    'role': 'customer', 'storeId': 7}
    raw = json.dumps(body).encode() if raw is None else raw
    if headers is None:
        headers = _signed_headers(raw, secret) if secret is not None else {}
    return client.post(ROUTE, data=raw, content_type='application/json',
                       headers=headers)


class TestSession:
    def test_good_signature_binds_and_returns_a_decodable_tenant_jwt(self, ctx):
        r = _session(ctx['client'])
        assert r.status_code == 200
        body = r.get_json()
        assert body['userId'] == 'mcg-4242'
        claims = decode_jwt(body['token'])
        assert claims['user_id'] == 'mcg-4242'
        assert claims['tid'] == 'mcgroce'
        # The tenant claim is this session's revenue proof; the node's
        # per-turn 'lane'/'rid' claims (the metered relay's) are never
        # minted here (task #157).
        assert 'lane' not in claims and 'rid' not in claims
        assert body['expiresIn'] > 0
        row = ctx['bindings'].get('mcg-4242')
        assert row['customer_id'] == '4242' and row['store_id'] == '7'

    def test_the_answer_is_signed_for_this_request(self, ctx):
        raw = json.dumps({'customerId': 4242, 'username': 'asha@example.com'}).encode()
        headers = _signed_headers(raw)
        r = _session(ctx['client'], headers=headers, raw=raw)
        assert r.status_code == 200
        assert r.headers[signing.SIGNATURE_HEADER] == signing.response_signature(
            SECRET, headers[signing.NONCE_HEADER], r.get_data())

    @pytest.mark.parametrize('secret', ['wrong', '', SECRET + 'x', None])
    def test_bad_key_is_refused_and_binds_nothing(self, ctx, secret):
        r = _session(ctx['client'], secret=secret)
        assert r.status_code == 403
        assert ctx['bindings'].get('mcg-4242') is None

    def test_the_old_plain_secret_header_is_refused(self, ctx):
        # Sent in the clear, the secret let anyone who saw one request mint
        # a token for every McGroce customer.
        r = _session(ctx['client'], headers={'X-Commerce-Secret': SECRET})
        assert r.status_code == 403
        assert ctx['bindings'].get('mcg-4242') is None

    def test_a_replayed_request_is_refused(self, ctx):
        raw = json.dumps({'customerId': 4242, 'username': 'asha@example.com'}).encode()
        headers = _signed_headers(raw)
        assert _session(ctx['client'], headers=headers, raw=raw).status_code == 200
        ctx['bindings'].remove('mcg-4242')
        assert _session(ctx['client'], headers=headers, raw=raw).status_code == 403
        assert ctx['bindings'].get('mcg-4242') is None

    def test_a_signature_for_one_customer_cannot_bind_another(self, ctx):
        raw = json.dumps({'customerId': 4242, 'username': 'asha@example.com'}).encode()
        swapped = json.dumps({'customerId': 5, 'username': 'asha@example.com'}).encode()
        r = _session(ctx['client'], headers=_signed_headers(raw), raw=swapped)
        assert r.status_code == 403
        assert ctx['bindings'].get('mcg-5') is None

    def test_a_stale_request_is_refused(self, ctx):
        raw = json.dumps({'customerId': 4242, 'username': 'asha@example.com'}).encode()
        old = _signed_headers(raw, ts=int(time.time()) - signing.SIGNATURE_WINDOW_S - 5)
        assert _session(ctx['client'], headers=old, raw=raw).status_code == 403

    def test_a_body_that_is_not_an_object_is_400(self, ctx):
        raw = b'[1, 2]'
        assert _session(ctx['client'], raw=raw).status_code == 400

    def test_unconfigured_secret_is_503(self, ctx, monkeypatch):
        monkeypatch.delenv('COMMERCE_SESSION_SECRET')
        with patch('core.config_cache.get_config', return_value={}):
            r = _session(ctx['client'])
        assert r.status_code == 503

    def test_missing_fields_and_unsafe_ids(self, ctx):
        assert _session(ctx['client'], customerId=1).status_code == 400
        # Signature first: an unsigned bad body is 403, never a 400 that
        # tells a stranger what the route wants.
        assert _session(ctx['client'], secret=None, customerId=1).status_code == 403
        r = _session(ctx['client'], customerId='a.b@shop.in', username='x')
        assert r.status_code == 200
        assert '@' not in r.get_json()['userId']

    def test_merchant_role_gets_its_own_namespace(self, ctx):
        r = _session(ctx['client'], customerId=9, username='o@shop.in', role='merchant')
        assert r.get_json()['userId'] == 'mcg-m-9'


class TestMandateRead:
    def test_owner_reads_their_mandate(self, ctx):
        m = ctx['mandates'].create_cart_mandate('mcg-4242', 'mcgroce', CART)
        tok = generate_jwt('mcg-4242', 'asha', tenant_id='mcgroce')
        r = ctx['client'].get(f'/api/commerce/mandates/{m.mandate_id}',
                              headers={'Authorization': f'Bearer {tok}'})
        assert r.status_code == 200
        assert r.get_json()['mandate']['status'] == 'pending'

    def test_someone_elses_mandate_looks_missing(self, ctx):
        m = ctx['mandates'].create_cart_mandate('mcg-4242', 'mcgroce', CART)
        tok = generate_jwt('mcg-5', 'ravi', tenant_id='mcgroce')
        r = ctx['client'].get(f'/api/commerce/mandates/{m.mandate_id}',
                              headers={'Authorization': f'Bearer {tok}'})
        assert r.status_code == 404

    def test_no_token(self, ctx):
        assert ctx['client'].get('/api/commerce/mandates/x').status_code == 401

    def test_a_node_whose_key_cannot_be_read_answers_the_pause(self, ctx):
        from integrations.ap2.ap2_mandate import MandateKeyError, PAYMENTS_PAUSED
        m = ctx['mandates'].create_cart_mandate('mcg-4242', 'mcgroce', CART)
        tok = generate_jwt('mcg-4242', 'asha', tenant_id='mcgroce')
        with patch.object(ctx['mandates'], 'get', side_effect=MandateKeyError()):
            r = ctx['client'].get(f'/api/commerce/mandates/{m.mandate_id}',
                                  headers={'Authorization': f'Bearer {tok}'})
        assert r.status_code == 503
        assert r.get_json() == {'success': False, 'error': PAYMENTS_PAUSED}


def test_the_signing_memory_starts_when_the_routes_load():
    # The "signed before this process started" line is drawn when signing
    # loads; it must load with the blueprint at boot, not on the first
    # session request (that request would be refused when McGroce's clock
    # trails by a second).
    from integrations.commerce import commerce_api
    assert commerce_api.verify_request is signing.verify_request


def test_health_reports_configuration(ctx):
    r = ctx['client'].get('/api/commerce/health')
    assert r.status_code == 200
    assert set(r.get_json()) == {'status', 'configured', 'admin_configured', 'breaker'}


def test_stream_names_the_callers_own_topic(ctx):
    tok = generate_jwt('mcg-4242', 'asha', tenant_id='mcgroce')
    r = ctx['client'].get('/api/commerce/stream',
                          headers={'Authorization': f'Bearer {tok}'})
    assert r.get_json() == {'user_id': 'mcg-4242',
                            'wamp_topic': 'com.hertzai.hevolve.social.mcg-4242',
                            'sse_event': 'chat.social',
                            'message_type': 'agent_ui_update'}
    assert ctx['client'].get('/api/commerce/stream').status_code == 401
