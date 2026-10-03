"""Behavioural tests for the /api/commerce/* blueprint (commerce_api.py)."""
from unittest.mock import patch

import pytest
from flask import Flask

from integrations.ap2.ap2_mandate import MandateStore
from integrations.ap2.ap2_protocol import PaymentLedger
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


def _session(client, secret=SECRET, **body):
    body = body or {'customerId': 4242, 'username': 'asha@example.com',
                    'role': 'customer', 'storeId': 7}
    return client.post('/api/commerce/session', json=body,
                       headers={'X-Commerce-Secret': secret} if secret is not None else {})


class TestSession:
    def test_good_secret_binds_and_returns_a_decodable_tenant_jwt(self, ctx):
        r = _session(ctx['client'])
        assert r.status_code == 200
        body = r.get_json()
        assert body['userId'] == 'mcg-4242'
        claims = decode_jwt(body['token'])
        assert claims['user_id'] == 'mcg-4242'
        assert claims['tid'] == 'mcgroce'
        assert body['expiresIn'] > 0
        row = ctx['bindings'].get('mcg-4242')
        assert row['customer_id'] == '4242' and row['store_id'] == '7'

    @pytest.mark.parametrize('secret', ['wrong', '', SECRET + 'x', None])
    def test_bad_secret_is_refused_and_binds_nothing(self, ctx, secret):
        r = _session(ctx['client'], secret=secret)
        assert r.status_code == 403
        assert ctx['bindings'].get('mcg-4242') is None

    def test_unconfigured_secret_is_503(self, ctx, monkeypatch):
        monkeypatch.delenv('COMMERCE_SESSION_SECRET')
        with patch('core.config_cache.get_config', return_value={}):
            r = _session(ctx['client'])
        assert r.status_code == 503

    def test_missing_fields_and_unsafe_ids(self, ctx):
        assert _session(ctx['client'], customerId=1).status_code == 400
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
