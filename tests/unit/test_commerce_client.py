"""Behavioural tests for integrations/commerce/mcgroce_client.py.

The boundary is core.http_pool.pooled_request; everything above it runs for
real (bindings on a tmp path, a fresh CircuitBreaker per test).
"""
import base64
import json
from unittest.mock import patch

import pytest
import requests

from core.circuit_breaker import CircuitBreaker
from integrations.commerce.bindings import CommerceBindings, hartos_user_id
from integrations.commerce.mcgroce_client import McGroceClient

BASE = 'https://mcgroce.example/api/v1'


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.content = b'' if body is None else json.dumps(body).encode()

    def json(self):
        if self._body is None:
            raise ValueError('no body')
        return self._body


@pytest.fixture
def bindings(tmp_path):
    b = CommerceBindings(path=str(tmp_path / 'bindings.json'))
    b.upsert(4242, 'asha@example.com', 'customer', store_id=7)
    return b


@pytest.fixture
def client(bindings):
    return McGroceClient(base_url=BASE, user='svc@mcgroce', password='pw',
                         admin_url='https://mcgroce.example/admin/api/v1',
                         admin_key='adm-key', bindings=bindings,
                         breaker=CircuitBreaker('t', threshold=5, cooldown=60))


def _call(mock):
    args, kwargs = mock.call_args
    return args[0], args[1], kwargs


class TestIdentity:
    def test_basic_auth_and_customer_id_come_from_the_binding(self, client):
        uid = hartos_user_id(4242)
        with patch('core.http_pool.pooled_request',
                   return_value=_Resp(200, {'id': 1, 'orderItems': []})) as m:
            out = client.cart_get(uid)
        assert out == {'success': True, 'status': 200,
                       'data': {'id': 1, 'orderItems': []}}
        method, url, kw = _call(m)
        assert (method, url) == ('GET', BASE + '/cart')
        h = kw['headers']
        assert h['Authorization'] == 'Basic ' + base64.b64encode(
            b'svc@mcgroce:pw').decode()
        assert h['customerId'] == '4242'
        assert h['Content-Type'] == 'application/json'
        assert kw['timeout'] == (3, 15)

    def test_unbound_user_never_reaches_mcgroce(self, client):
        with patch('core.http_pool.pooled_request') as m:
            out = client.cart_get('mcg-999')
        assert out['success'] is False and 'not linked' in out['error']
        m.assert_not_called()

    def test_catalog_search_carries_the_bound_store(self, client):
        with patch('core.http_pool.pooled_request',
                   return_value=_Resp(200, {'products': []})) as m:
            client.search_catalog(hartos_user_id(4242), 'milk', page_size=5)
        _, url, kw = _call(m)
        assert url == BASE + '/catalog/search'
        assert kw['params'] == {'q': 'milk', 'page': 1, 'pageSize': 5, 'storeId': '7'}

    def test_path_arguments_cannot_add_path_segments(self, client):
        with patch('core.http_pool.pooled_request',
                   return_value=_Resp(200, {})) as m:
            client.product(hartos_user_id(4242), '1/../../admin')
        _, url, _ = _call(m)
        assert url == BASE + '/catalog/product/1%2F..%2F..%2Fadmin'

    def test_admin_calls_use_the_bearer_key(self, client):
        with patch('core.http_pool.pooled_request',
                   return_value=_Resp(201, {'id': 9})) as m:
            out = client.onboard_merchant({'displayName': 'X'})
        assert out['success'] is True
        method, url, kw = _call(m)
        assert (method, url) == ('POST', 'https://mcgroce.example/admin/api/v1/mcg/merchants')
        assert kw['headers']['Authorization'] == 'Bearer adm-key'
        assert kw['json'] == {'displayName': 'X'}


class TestTransportSecurity:
    def test_plain_http_is_refused(self, bindings):
        c = McGroceClient(base_url='http://mcgroce.example/api/v1', user='u',
                          password='p', bindings=bindings,
                          breaker=CircuitBreaker('t'))
        with patch('core.http_pool.pooled_request') as m:
            out = c.find_stores(None, zipcode='600078')
        assert out['success'] is False and 'https' in out['error']
        m.assert_not_called()

    def test_http_loopback_allowed_only_with_the_flag(self, bindings, monkeypatch):
        c = McGroceClient(base_url='http://127.0.0.1:8080/api/v1', user='u',
                          password='p', bindings=bindings,
                          breaker=CircuitBreaker('t'))
        with patch('core.http_pool.pooled_request',
                   return_value=_Resp(200, {})) as m:
            monkeypatch.delenv('MCGROCE_ALLOW_HTTP', raising=False)
            assert c.find_stores(None, zipcode='600078')['success'] is False
            monkeypatch.setenv('MCGROCE_ALLOW_HTTP', '1')
            assert c.find_stores(None, zipcode='600078')['success'] is True
        assert m.call_count == 1

    def test_flag_does_not_open_http_to_a_remote_host(self, bindings, monkeypatch):
        monkeypatch.setenv('MCGROCE_ALLOW_HTTP', '1')
        c = McGroceClient(base_url='http://mcgroce.example/api/v1', user='u',
                          password='p', bindings=bindings,
                          breaker=CircuitBreaker('t'))
        with patch('core.http_pool.pooled_request') as m:
            assert c.find_stores(None, zipcode='600078')['success'] is False
        m.assert_not_called()

    def test_unconfigured_base_url(self, bindings):
        c = McGroceClient(base_url='', user='u', password='p',
                          bindings=bindings, breaker=CircuitBreaker('t'))
        assert 'not configured' in c.find_stores(None, zipcode='600078')['error']


class TestFailures:
    def test_timeout_is_a_structured_error(self, client):
        with patch('core.http_pool.pooled_request',
                   side_effect=requests.Timeout('slow')):
            out = client.cart_get(hartos_user_id(4242))
        assert out == {'success': False, 'error': 'McGroce timed out'}

    def test_error_wrapper_message_is_surfaced(self, client):
        body = {'httpStatusCode': 404, 'messages': [
            {'messageKey': 'CART_NOT_FOUND', 'message': 'No cart'}]}
        with patch('core.http_pool.pooled_request', return_value=_Resp(404, body)):
            out = client.cart_get(hartos_user_id(4242))
        assert out == {'success': False, 'error': 'CART_NOT_FOUND: No cart',
                       'status': 404}

    def test_breaker_opens_after_five_failures(self, client):
        uid = hartos_user_id(4242)
        with patch('core.http_pool.pooled_request',
                   side_effect=requests.ConnectionError('down')) as m:
            for _ in range(5):
                assert 'unreachable' in client.cart_get(uid)['error']
            out = client.cart_get(uid)
        assert m.call_count == 5
        assert out['success'] is False and 'not answering' in out['error']

    def test_5xx_counts_and_4xx_does_not(self, client):
        uid = hartos_user_id(4242)
        with patch('core.http_pool.pooled_request', return_value=_Resp(400, {})):
            for _ in range(6):
                client.cart_get(uid)
        assert client.breaker.get_stats()['failures'] == 0
        with patch('core.http_pool.pooled_request', return_value=_Resp(503, {})):
            for _ in range(5):
                client.cart_get(uid)
        assert client.breaker.get_stats()['state'] == 'open'

    def test_non_json_body(self, client):
        class _Bad(_Resp):
            def json(self):
                raise ValueError('html')
        r = _Bad(200, {})
        r.content = b'<html>'
        with patch('core.http_pool.pooled_request', return_value=r):
            out = client.cart_get(hartos_user_id(4242))
        assert out['success'] is False and 'non-JSON' in out['error']


class TestBindings:
    def test_ids_are_namespaced_file_safe_and_stable(self):
        assert hartos_user_id(12) == 'mcg-12'
        assert hartos_user_id(12, 'merchant') == 'mcg-m-12'
        hashed = hartos_user_id('a.b@shop.in')
        assert hashed == hartos_user_id('a.b@shop.in')
        assert hashed.startswith('mcg-') and not set('@./_') & set(hashed)
        with pytest.raises(ValueError):
            hartos_user_id('')

    def test_upsert_persists_and_reloads(self, tmp_path):
        path = str(tmp_path / 'b.json')
        CommerceBindings(path).upsert(5, 'a@b.c', 'merchant', store_id=3)
        row = CommerceBindings(path).get('mcg-m-5')
        assert row['customer_id'] == '5' and row['store_id'] == '3'
        assert row['tenant'] == 'mcgroce'
