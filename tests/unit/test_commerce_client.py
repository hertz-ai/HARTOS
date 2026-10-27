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
        assert out == {'success': False, 'error': 'McGroce timed out', 'unconfirmed': True}

    def test_only_a_sent_and_unanswered_request_is_unconfirmed(self, client):
        # commerce_tools._submit_order reads this mark to tell a paid shopper
        # "we could not confirm your order" (the call left, McGroce neither
        # did nor refused it: it may be placed) from "not placed yet" (refused,
        # or never sent).  It comes from the transport's own path, never from
        # an error text.
        uid = hartos_user_id(4242)
        with patch('core.http_pool.pooled_request',
                   side_effect=requests.ConnectionError('reset')):
            assert client.cart_get(uid)['unconfirmed'] is True
        with patch('core.http_pool.pooled_request', return_value=_Resp(503, {})):
            assert client.cart_get(uid)['unconfirmed'] is True
        with patch('core.http_pool.pooled_request', return_value=_Resp(409, {})):
            assert 'unconfirmed' not in client.cart_get(uid)
        for _ in range(5):
            client.breaker.record_failure()
        with patch('core.http_pool.pooled_request') as m:
            out = client.cart_get(uid)
        m.assert_not_called()
        assert out['success'] is False and 'unconfirmed' not in out

    def test_error_wrapper_message_is_surfaced(self, client):
        body = {'httpStatusCode': 404, 'messages': [
            {'messageKey': 'CART_NOT_FOUND', 'message': 'No cart'}]}
        with patch('core.http_pool.pooled_request', return_value=_Resp(404, body)):
            out = client.cart_get(hartos_user_id(4242))
        assert out == {'success': False, 'error': 'CART_NOT_FOUND: No cart',
                       'status': 404, 'key': 'CART_NOT_FOUND'}

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
        # McGroce did it and this node could not read what: neither confirmed
        # nor refused, with the 2xx status telling it apart from a 5xx.
        assert out['unconfirmed'] is True and out['status'] == 200

    def test_an_ap2_success_that_names_no_order_is_unconfirmed(self, client):
        # COMMERCE_MCGROCE.md's order contract is 200 {"orderNumber": ...}:
        # a success without it (an empty body, a wrapper without the keys,
        # an empty number) is never a legitimate success.  McGroce did
        # something this node cannot read, so the caller takes it as
        # unconfirmed, with the 2xx status telling it apart from a 5xx.
        for body in (None, {}, {'orderNumber': ''}, {'status': 'SUBMITTED'}):
            with patch('core.http_pool.pooled_request', return_value=_Resp(200, body)):
                out = client.complete_ap2_checkout(hartos_user_id(4242), 'tok')
            assert out['success'] is False and out['unconfirmed'] is True, body
            assert out['status'] == 200 and 'without an order number' in out['error']

    def test_an_ap2_success_with_only_an_id_is_placed(self, client):
        # The check must not be stricter than the reader _order_placed
        # uses: an older McGroce names the order by id alone.
        from integrations.commerce.mcgroce_client import order_number_of
        with patch('core.http_pool.pooled_request', return_value=_Resp(200, {'id': 'ORD-ID'})):
            out = client.complete_ap2_checkout(hartos_user_id(4242), 'tok')
        assert out['success'] is True and out['data'] == {'id': 'ORD-ID'}
        assert order_number_of(out['data']) == 'ORD-ID'
        assert order_number_of({'orderNumber': 'N1', 'id': 7}) == 'N1'
        assert order_number_of(['N1']) is None and order_number_of(None) is None

    def test_the_reader_names_an_order_only_for_a_real_number(self, client):
        # hartos-94's order-zero probe: 0, {} and '   ' passed as order
        # numbers, and for the falsy ones the placer skipped the record
        # while answering "ordered".  McGroce's orderNumber is
        # yyyyMMddHHmmssS plus the order id (CompleteOrderActivity), never
        # blank or all zeros; an older McGroce's id is a positive int.
        from integrations.commerce.mcgroce_client import order_number_of
        for body in ({'id': 0}, {'orderNumber': '   '}, {'orderNumber': {}},
                     {'id': -1}, {'id': True}, {'orderNumber': '0'},
                     {'orderNumber': '00'}, {'orderNumber': ['N1']}):
            assert order_number_of(body) is None, body
            with patch('core.http_pool.pooled_request', return_value=_Resp(200, body)):
                out = client.complete_ap2_checkout(hartos_user_id(4242), 'tok')
            assert out['success'] is False and out['unconfirmed'] is True, body
        assert order_number_of({'orderNumber': ' ORD-S '}) == 'ORD-S'       # stripped
        assert order_number_of({'id': 7}) == 7
        assert order_number_of({'orderNumber': '2026102500000011'}) == '2026102500000011'

    def test_a_refusal_carries_mcgroce_key_as_its_own_field(self, client):
        # The caller decides on McGroce's messageKey, never by parsing the
        # error text, which is rendered for people.  A 4xx without one came
        # from something in front of McGroce (Kong's 404, a 401 wall).
        from integrations.commerce.mcgroce_client import AP2_REFUSAL_KEYS
        refused = _Resp(403, {'messages': [{'messageKey': 'AP2_MANDATE_REJECTED',
                                            'message': 'The payment does not match (items)'}]})
        with patch('core.http_pool.pooled_request', return_value=refused):
            out = client.complete_ap2_checkout(hartos_user_id(4242), 'tok')
        assert out['key'] == 'AP2_MANDATE_REJECTED' and out['key'] in AP2_REFUSAL_KEYS
        assert out['status'] == 403 and 'unconfirmed' not in out
        kong = _Resp(404, {'message': 'no Route matched with those values',
                           'request_id': 'abc'})
        with patch('core.http_pool.pooled_request', return_value=kong):
            out = client.complete_ap2_checkout(hartos_user_id(4242), 'tok')
        assert out['success'] is False and out['status'] == 404 and 'key' not in out
        wall = _Resp(401, None)
        wall.content = b'<html>sign in</html>'
        with patch('core.http_pool.pooled_request', return_value=wall):
            out = client.complete_ap2_checkout(hartos_user_id(4242), 'tok')
        assert out['success'] is False and out['status'] == 401 and 'key' not in out
        # A keyed 4xx that is not a refusal keeps its key and is not in the set.
        other = _Resp(400, {'messages': [{'messageKey': 'VALIDATION_ERROR',
                                          'message': 'bad request'}]})
        with patch('core.http_pool.pooled_request', return_value=other):
            out = client.complete_ap2_checkout(hartos_user_id(4242), 'tok')
        assert out['key'] == 'VALIDATION_ERROR' and out['key'] not in AP2_REFUSAL_KEYS
        # Several messages: the FIRST key is the answer's key, the text has all.
        many = _Resp(409, {'messages': [{'messageKey': 'AMOUNT_MISMATCH', 'message': 'a'},
                                        {'messageKey': 'OTHER', 'message': 'b'}]})
        with patch('core.http_pool.pooled_request', return_value=many):
            out = client.complete_ap2_checkout(hartos_user_id(4242), 'tok')
        assert out['key'] == 'AMOUNT_MISMATCH' and out['error'] == 'AMOUNT_MISMATCH: a; OTHER: b'

    def test_readiness_reads_the_refusal_from_the_key(self, client):
        refused = _Resp(403, {'messages': [{'messageKey': 'AP2_MANDATE_REJECTED',
                                            'message': 'no payment attestation'}]})
        with patch('core.http_pool.pooled_request', return_value=refused):
            assert client.agent_orders_ready(hartos_user_id(4242)) is None
        wall = _Resp(403, {'message': 'forbidden by the gateway'})
        with patch('core.http_pool.pooled_request', return_value=wall):
            assert client.agent_orders_ready(hartos_user_id(4242)) is not None

    def test_a_cart_answer_naming_no_cart_is_a_failure_not_an_empty_cart(self, client):
        # McGroce answers a customer with no cart 404 CART_NOT_FOUND
        # (CartEndpoint.findCartForCustomer), so a 2xx without a cart id is
        # an answer this node cannot read as a cart: a failure, read as
        # such by the caller (not unconfirmed: nothing was placed).
        for body in (None, {}, {'orderItems': []}, {'id': ''}):
            with patch('core.http_pool.pooled_request', return_value=_Resp(200, body)):
                out = client.cart_get(hartos_user_id(4242))
            assert out['success'] is False and 'empty answer' in out['error'], body
            assert out['status'] == 200 and 'unconfirmed' not in out
        with patch('core.http_pool.pooled_request',
                   return_value=_Resp(200, {'id': 555, 'orderItems': []})):
            out = client.cart_get(hartos_user_id(4242))
        assert out['success'] is True and out['data']['id'] == 555


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


class TestMerchantIsNotAShopper:
    """A merchant's id comes from McGroce's admin table; sent as customerId it
    would name whichever shopper has the same number."""

    def _merchant(self, bindings):
        bindings.upsert(4242, 'shop@example.com', 'merchant')
        return hartos_user_id(4242, 'merchant')

    def test_a_merchant_cannot_read_a_shoppers_cart(self, client, bindings):
        uid = self._merchant(bindings)
        with patch('core.http_pool.pooled_request') as m:
            out = client.cart_get(uid)
        assert out['success'] is False and 'merchant' in out['error']
        m.assert_not_called()

    def test_a_merchant_catalog_read_carries_no_customer_id(self, client, bindings):
        uid = self._merchant(bindings)
        with patch('core.http_pool.pooled_request',
                   return_value=_Resp(200, [])) as m:
            client.site(uid, 'GET', '/catalog/search', need_customer=False)
        _, _, kw = _call(m)
        assert 'customerId' not in kw['headers']

    def test_the_customer_still_shops_as_before(self, client):
        with patch('core.http_pool.pooled_request',
                   return_value=_Resp(200, {'id': 1, 'orderItems': []})) as m:
            assert client.cart_get(hartos_user_id(4242))['success'] is True
        _, _, kw = _call(m)
        assert kw['headers']['customerId'] == '4242'


class TestIdentityNamespaces:
    def test_a_customer_id_starting_m_dash_never_lands_on_a_merchant(self):
        assert hartos_user_id('m-5', 'customer') != hartos_user_id(5, 'merchant')
        assert hartos_user_id('m-5', 'customer').startswith('mcg-')

    def test_ordinary_ids_map_as_before(self):
        assert hartos_user_id(4242) == 'mcg-4242'
        assert hartos_user_id(5, 'merchant') == 'mcg-m-5'
