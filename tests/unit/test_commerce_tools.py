"""Behavioural tests for integrations/commerce/commerce_tools.py.

Real McGroceClient, bindings, PaymentLedger, MandateStore and DraftStore on
tmp paths.  Mocked boundaries only: core.http_pool.pooled_request (McGroce)
and the LiquidUIService from core.platform.registry (the UI push).  The
ledger carries a live synchronous card rail (tests/unit/ap2_doubles.py
CardRailDouble, Stripe's shape), so the checkout tests run as a node with
gateway keys; the node WITHOUT one, where select_gateway answers MOCK, is
TestNoLiveGateway.
"""
import base64
import hashlib
import hmac
import inspect
import json
import logging
import time
from unittest.mock import MagicMock, patch

import pytest

from core.circuit_breaker import CircuitBreaker
from integrations.ap2 import ap2_protocol
from tests.unit.ap2_doubles import CardRailDouble
from integrations.ap2.ap2_mandate import MandateStore
from integrations.ap2.ap2_protocol import PaymentLedger, PaymentStatus
from integrations.commerce import commerce_tools as ct
from integrations.commerce import signing
from integrations.commerce.bindings import CommerceBindings, hartos_user_id
from integrations.commerce.drafts import DraftStore
from integrations.commerce.mcgroce_client import McGroceClient

BASE = 'https://mcgroce.example/api/v1'
SECRET = 'shared-with-tomcat-test-only'
AP2 = '/cart/checkout/ap2'
UID = hartos_user_id(4242)
MUID = hartos_user_id(77, 'merchant')

ORDER = {
    'id': 555, 'status': 'IN_PROCESS',
    'total': {'amount': 240.0, 'currency': 'INR'},
    'orderItems': [
        {'id': 1, 'skuId': 11, 'productId': 101, 'categoryId': 3,
         'name': 'Milk 1 L', 'quantity': 2, 'retailPrice': {'amount': 40}},
        {'id': 2, 'skuId': 12, 'productId': 102, 'categoryId': 3,
         'name': 'Paneer', 'quantity': 1, 'retailPrice': {'amount': 200},
         'salePrice': {'amount': 160}},
    ],
}


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.content = b'' if body is None else json.dumps(body).encode()

    def json(self):
        return self._body


class FakeMcGroce:
    """Routes (method, path) to canned answers and records every call."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def on(self, method, path, status=200, body=None):
        self.routes[(method, path)] = (status, body)

    def on_each(self, method, path, *answers):
        """Answer successive calls in turn ((status, body) each); the last
        answer repeats."""
        self.routes[(method, path)] = list(answers)

    def __call__(self, method, url, **kw):
        path = url[len(BASE):] if url.startswith(BASE) else url
        self.calls.append((method, path, kw))
        answer = self.routes.get((method, path), (404, {'messages': [
            {'messageKey': 'NOT_FOUND', 'message': path}]}))
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        status, body = answer
        return _Resp(status, body)

    def called(self, method, path):
        return [c for c in self.calls if c[0] == method and c[1] == path]


def attestation_of(call):
    """The claims of the X-Commerce-Mandate one McGroce order call carried,
    after checking its MAC the way McGroce's verifier does."""
    token = call[2]['headers'][signing.MANDATE_HEADER]
    b64, mac = token.split('.')
    key = hmac.new(SECRET.encode(), signing.MANDATE_KEY_LABEL, hashlib.sha256).digest()
    assert hmac.compare_digest(hmac.new(key, b64.encode(), hashlib.sha256).hexdigest(), mac)
    return json.loads(base64.urlsafe_b64decode(b64 + '=' * (-len(b64) % 4)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('COMMERCE_SESSION_SECRET', SECRET)
    bindings = CommerceBindings(str(tmp_path / 'bindings.json'))
    bindings.upsert(4242, 'asha@example.com', 'customer', store_id=7)
    bindings.upsert(77, 'owner@shop.in', 'merchant', store_id=9)
    client = McGroceClient(base_url=BASE, user='svc', password='pw',
                           admin_url='https://mcgroce.example/admin/api/v1',
                           admin_key='k', bindings=bindings,
                           breaker=CircuitBreaker('t'))
    ledger = PaymentLedger(ledger_path=str(tmp_path / 'ledger.json'))
    card = CardRailDouble()
    ledger.add_gateway(card)
    mandates = MandateStore(str(tmp_path / 'mandates.json'), ledger=ledger,
                            key=b'k' * 32)
    drafts = DraftStore(str(tmp_path / 'drafts.json'))
    fake = FakeMcGroce()
    ui = MagicMock(return_value=True)   # liquid_ui_service.push_agent_ui
    with patch.object(ct, '_client', return_value=client), \
            patch.object(ct, '_mandates', return_value=mandates), \
            patch.object(ct, '_drafts', return_value=drafts), \
            patch.object(ap2_protocol, 'payment_ledger', ledger), \
            patch('integrations.commerce.bindings.get_bindings', return_value=bindings), \
            patch('core.http_pool.pooled_request', side_effect=fake), \
            patch('integrations.agent_engine.liquid_ui_service.push_agent_ui', ui):
        yield {'fake': fake, 'ui': ui, 'ledger': ledger, 'mandates': mandates,
               'drafts': drafts, 'bindings': bindings, 'card': card}


def pushed(env, type_=None):
    comps = [c.args[1] for c in env['ui'].call_args_list]
    return [c for c in comps if type_ is None or c['type'] == type_]


class TestRegistration:
    def test_registers_every_tool_on_helper_assistant_and_executor(self):
        helper, assistant, executor = MagicMock(), MagicMock(), MagicMock()
        n = ct.register_commerce_tools(helper, assistant, UID, executor=executor)
        assert n == len(ct.COMMERCE_TOOLS) == 14
        names = [c.kwargs['name'] for c in helper.register_for_llm.call_args_list]
        assert names == [t['name'] for t in ct.COMMERCE_TOOLS]
        assert assistant.register_for_llm.call_count == 14
        assert assistant.register_for_execution.call_count == 14
        assert executor.register_for_execution.call_count == 14

    def test_bound_tools_hide_user_id_and_act_as_the_registrar_user(self, env):
        helper = MagicMock()
        registered = {}
        helper.register_for_llm.side_effect = (
            lambda name, description: lambda f: registered.setdefault(name, f))
        ct.register_commerce_tools(helper, MagicMock(), UID)
        fn = registered['commerce_cart_view']
        assert 'user_id' not in inspect.signature(fn).parameters
        env['fake'].on('GET', '/cart', body=ORDER)
        json.loads(fn())
        assert env['fake'].calls[-1][2]['headers']['customerId'] == '4242'

    def test_real_autogen_schema_has_no_user_id(self):
        autogen = pytest.importorskip('autogen')
        agent = autogen.ConversableAgent(
            'a', llm_config={'config_list': [{'model': 'x', 'api_key': 'x'}]})
        ct.register_commerce_tools(agent, agent, UID)
        tools = {t['function']['name']: t['function']
                 for t in agent.llm_config['tools']}
        assert len(tools) == 14
        params = tools['commerce_cart_add']['parameters']
        assert 'user_id' not in params['properties']
        assert set(params['required']) == {'product_id', 'category_id'}

    def test_mcp_list_functions_take_user_id_first(self):
        for t in ct.COMMERCE_TOOLS:
            assert list(inspect.signature(t['func']).parameters)[0] == 'user_id'
            assert t['tags'] == ['commerce']


class TestShopping:
    def test_search_pushes_at_most_three_product_cards(self, env):
        products = [{'id': i, 'name': f'Milk {i}', 'retailPrice': {'amount': 30 + i},
                     'defaultCategoryId': 3,
                     'primaryMedia': {'url': f'https://img/{i}.png'}}
                    for i in range(5)]
        env['fake'].on('GET', '/catalog/search',
                       body={'products': products, 'totalResults': 5, 'page': 1})
        out = json.loads(ct.commerce_search_catalog(UID, 'milk'))
        assert out['success'] and out['total'] == 5 and len(out['products']) == 5
        cards = pushed(env, 'product_card')
        assert len(cards) == 3
        assert cards[0]['image'] == cards[0]['image_url'] == 'https://img/0.png'
        assert cards[0]['price'] == 30.0 and cards[0]['currency'] == 'INR'
        assert cards[0]['name'] == 'Milk 0'

    def test_empty_query_refused_without_a_call(self, env):
        out = json.loads(ct.commerce_search_catalog(UID, '  '))
        assert out['success'] is False and env['fake'].calls == []

    def test_find_stores_lists_nearest_first(self, env):
        env['fake'].on('GET', '/zipcodesearch/stores/600078', body={
            '2500.0': {'id': 2, 'name': 'Far', 'address1': 'A', 'city': 'Chennai',
                       'deliveryAvailable': False},
            '300.0': {'id': 1, 'name': 'Near', 'address1': 'B', 'city': 'Chennai',
                      'deliveryAvailable': True}})
        out = json.loads(ct.commerce_find_stores(UID, zipcode='600078'))
        assert [s['name'] for s in out['stores']] == ['Near', 'Far']
        lst = pushed(env, 'list')[0]
        assert lst['items'][0]['title'] == 'Near'
        assert 'Delivers' in lst['items'][0]['subtitle']

    def test_find_stores_rows_carry_the_text_the_list_renderers_read(self, env):
        # COMPONENT_TYPES['list']: each item is a string or {text|label}.  The
        # Nunba overlay draws item.text || item.label, so a row that had only
        # title/subtitle came out as an empty bullet.
        env['fake'].on('GET', '/zipcodesearch/stores/600078', body={
            '300.0': {'id': 1, 'name': 'Near', 'address1': 'B', 'city': 'Chennai',
                      'deliveryAvailable': True}})
        json.loads(ct.commerce_find_stores(UID, zipcode='600078'))
        row = pushed(env, 'list')[0]['items'][0]
        shown = row.get('text') or row.get('label')
        assert shown and 'Near' in shown and '0.3 km' in shown
        # Product text carries no em dash (the steward's rule); the first
        # version of this row joined with one.
        assert '—' not in shown

    def test_find_stores_validates_pincode(self, env):
        assert json.loads(ct.commerce_find_stores(UID, zipcode='12'))['success'] is False
        assert json.loads(ct.commerce_find_stores(UID))['success'] is False
        assert env['fake'].calls == []

    def test_cart_add_pushes_the_cart(self, env):
        env['fake'].on('POST', '/cart/101', body=ORDER)
        out = json.loads(ct.commerce_cart_add(UID, 101, 3, quantity=2))
        assert out['cart']['total'] == 240.0
        assert env['fake'].calls[0][2]['params'] == {'categoryId': 3, 'quantity': 2}
        cart = pushed(env, 'cart')[0]
        assert cart['currency'] == 'INR' and cart['total'] == 240.0
        assert [i['price'] for i in cart['items']] == [40.0, 160.0]

    def test_cart_update_to_zero_removes(self, env):
        env['fake'].on('DELETE', '/cart/items/1', body=ORDER)
        json.loads(ct.commerce_cart_update(UID, 1, 0))
        assert env['fake'].called('DELETE', '/cart/items/1')

    def test_unlinked_user_gets_a_clear_error(self, env):
        out = json.loads(ct.commerce_cart_view('mcgroce_1'))
        assert out['success'] is False and 'not linked' in out['error']

    def test_order_status_pushes_tracking(self, env):
        env['fake'].on('GET', '/orders', body=[
            {'id': 3, 'orderNumber': 'ORD-3', 'status': 'SUBMITTED', 'total': 10},
            {'id': 8, 'orderNumber': 'ORD-8', 'status': 'OUT_FOR_DELIVERY', 'total': 20}])
        out = json.loads(ct.commerce_order_status(UID))
        assert out['orders'][0]['order_id'] == 'ORD-8'
        tr = pushed(env, 'order_tracking')[0]
        assert tr['order_id'] == 'ORD-8'
        assert [s['done'] for s in tr['steps']] == [True, True, True, False]

    def test_no_orders_is_not_an_error(self, env):
        env['fake'].on('GET', '/orders', status=404,
                       body={'messages': [{'messageKey': 'CART_NOT_FOUND'}]})
        assert json.loads(ct.commerce_order_status(UID)) == {'success': True, 'orders': []}


class TestCheckout:
    def _prepare(self, env, cap=None):
        env['fake'].on('GET', '/cart', body=ORDER)
        return json.loads(ct.commerce_prepare_checkout(UID, cap=cap))

    def test_prepare_creates_a_pending_mandate_and_the_approval_card(self, env):
        out = self._prepare(env)
        assert out['status'] == 'awaiting_approval'
        m = env['mandates'].get(out['mandate_id'])
        assert m.status == 'pending' and m.user_id == UID and m.amount == '240.00'
        assert env['ledger'].get_payment(m.payment_id).status == PaymentStatus.APPROVAL_REQUIRED
        assert pushed(env, 'checkout')[0]['total'] == 240.0
        card = pushed(env, 'approval')[0]
        assert card['action'] == f'ap2_pay:{m.payment_id}'
        assert '₹240.00' in card['description'] and '3 items' in card['description']

    def test_prepare_respects_the_cap(self, env):
        out = self._prepare(env, cap=100)
        assert out['success'] is False and 'over the cap' in out['error']
        assert pushed(env, 'approval') == []

    def test_prepare_refuses_an_empty_cart(self, env):
        env['fake'].on('GET', '/cart', body={'id': 1, 'orderItems': [], 'total': 0})
        assert json.loads(ct.commerce_prepare_checkout(UID))['success'] is False

    def _assert_refused(self, env, out, why):
        assert out['success'] is False and why in out['error']
        assert not env['fake'].called('POST', AP2)

    def test_refuses_a_missing_mandate(self, env):
        env['fake'].on('GET', '/cart', body=ORDER)
        self._assert_refused(env, json.loads(ct.commerce_checkout(UID, 'mdt_nope')),
                             'not found')

    def test_refuses_a_pending_mandate(self, env):
        out = self._prepare(env)
        self._assert_refused(env, json.loads(ct.commerce_checkout(UID, out['mandate_id'])),
                             'not approved')

    def test_refuses_another_users_mandate(self, env):
        out = self._prepare(env)
        env['mandates'].approve(out['mandate_id'], UID)
        other = env['bindings'].upsert(5, 'ravi@example.com')['user_id']
        self._assert_refused(env, json.loads(ct.commerce_checkout(other, out['mandate_id'])),
                             'not found')

    def test_refuses_when_the_cart_changed_after_approval(self, env):
        out = self._prepare(env)
        env['mandates'].approve(out['mandate_id'], UID)
        drifted = json.loads(json.dumps(ORDER))
        drifted['orderItems'][0]['quantity'] = 6
        env['fake'].on('GET', '/cart', body=drifted)
        self._assert_refused(env, json.loads(ct.commerce_checkout(UID, out['mandate_id'])),
                             'cart changed')
        m = env['mandates'].get(out['mandate_id'])
        # The approval is void: nothing may take this payment later.
        assert m.status == 'rejected'
        assert env['ledger'].get_payment(m.payment_id).status == PaymentStatus.CANCELLED

    def test_refuses_an_expired_mandate(self, env):
        out = self._prepare(env)
        env['mandates'].approve(out['mandate_id'], UID)
        later = time.time() + 3600
        with patch('integrations.ap2.ap2_mandate.time.time', return_value=later):
            res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        self._assert_refused(env, res, 'expired')

    def test_approved_checkout_pays_and_places_the_order(self, env):
        out = self._prepare(env)
        env['mandates'].approve(out['mandate_id'], UID)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-555',
                                          'paymentType': 'THIRD_PARTY_ACCOUNT'})
        res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is True and res['order_id'] == 'ORD-555'
        m = env['mandates'].get(out['mandate_id'])
        assert m.status == 'consumed'
        assert env['ledger'].get_payment(m.payment_id).status == PaymentStatus.COMPLETED
        # ONE order call, as the bound customer, carrying the signed word
        # that HARTOS took exactly this amount for this customer.
        (call,) = env['fake'].called('POST', AP2)
        assert call[2]['headers']['customerId'] == '4242'
        claims = attestation_of(call)
        assert claims['mid'] == m.mandate_id and claims['pid'] == m.payment_id
        assert claims['cid'] == '4242' and claims['amt'] == '240.00'
        assert claims['cur'] == 'INR' and claims['st'] == 'consumed' and claims['v'] == 1
        assert claims['exp'] - claims['iat'] == signing.ATTESTATION_TTL_S
        assert claims['txn'] == f"card_{m.payment_id}"
        # Bound to this exact cart: McGroce compares its repriced cart to it.
        assert claims['oid'] == '555'
        assert claims['lines'] == [['11', 2, '40.00'], ['12', 1, '160.00']]
        # The raw payment write that let anyone mark a cart paid is gone.
        assert not env['fake'].called('POST', '/cart/checkout/payment')
        assert not env['fake'].called('POST', '/cart/checkout')
        assert pushed(env, 'payment_status')[-1]['status'] == 'completed'
        assert pushed(env, 'order_tracking')[-1]['order_id'] == 'ORD-555'
        assert env['mandates'].get(out['mandate_id']).order_ref == 'ORD-555'
        # one-shot: asking again names the same order; nothing is charged or
        # sent to McGroce a second time.
        again = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert again['success'] is True and again['order_id'] == 'ORD-555'
        assert len(env['fake'].called('POST', AP2)) == 1
        assert len(env['card'].captures) == 1

    def test_a_lost_answer_is_retried_once_and_never_charges_twice(self, env):
        out = self._prepare(env)
        env['mandates'].approve(out['mandate_id'], UID)
        env['fake'].on_each('POST', AP2, (502, None), (200, {'orderNumber': 'ORD-2'}))
        res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is True and res['order_id'] == 'ORD-2'
        calls = env['fake'].called('POST', AP2)
        assert len(calls) == 2
        assert attestation_of(calls[0])['mid'] == attestation_of(calls[1])['mid']
        assert len(env['card'].captures) == 1

    def test_a_paid_order_that_failed_is_placed_later_without_a_new_charge(self, env):
        out = self._prepare(env)
        env['mandates'].approve(out['mandate_id'], UID)
        env['fake'].on('POST', AP2, status=503, body=None)
        first = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert first['success'] is False and first['needs_attention'] is True
        assert env['mandates'].get(out['mandate_id']).status == 'consumed'
        # McGroce is back (or had placed it and the answer was lost: it
        # answers a used mandate with that same order).
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-3'})
        later = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert later['success'] is True and later['order_id'] == 'ORD-3'
        assert len(env['card'].captures) == 1

    def test_a_retry_refuses_when_the_cart_is_no_longer_the_one_paid_for(self, env):
        out = self._prepare(env)
        env['mandates'].approve(out['mandate_id'], UID)
        env['fake'].on('POST', AP2, status=503, body=None)
        json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        n = len(env['fake'].called('POST', AP2))
        drifted = json.loads(json.dumps(ORDER))
        drifted['orderItems'][0]['quantity'] = 9
        env['fake'].on('GET', '/cart', body=drifted)
        res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is False and res['needs_attention'] is True
        assert len(env['fake'].called('POST', AP2)) == n

    def test_a_new_checkout_withdraws_the_earlier_one(self, env):
        from integrations.ap2.ap2_mandate import decide_payment
        first = self._prepare(env)
        second = self._prepare(env)
        old = env['mandates'].get(first['mandate_id'])
        assert old.status == 'rejected'
        assert env['ledger'].get_payment(old.payment_id).status == PaymentStatus.CANCELLED
        # The earlier card can no longer be paid: one cart, one payment.
        payload, code = decide_payment(first['payment_id'], UID, True,
                                       store=env['mandates'])
        assert code == 409
        assert env['mandates'].get(second['mandate_id']).status == 'pending'

    def test_a_cart_already_paid_for_is_not_offered_for_payment_again(self, env):
        out = self._prepare(env)
        env['mandates'].approve(out['mandate_id'], UID)
        env['fake'].on('POST', AP2, status=503, body=None)
        json.loads(ct.commerce_checkout(UID, out['mandate_id']))   # paid, not ordered
        again = json.loads(ct.commerce_prepare_checkout(UID))
        assert again['success'] is False and 'already paid for' in again['error']
        assert out['mandate_id'] in again['error']
        assert len(pushed(env, 'approval')) == 1

    def test_no_secret_means_no_order_and_a_person_is_told(self, env, monkeypatch):
        # Money taken but nothing McGroce could verify: flagged, never sent
        # unsigned.
        monkeypatch.delenv('COMMERCE_SESSION_SECRET')
        out = self._prepare(env)
        env['mandates'].approve(out['mandate_id'], UID)
        with patch('core.config_cache.get_config', return_value={}):
            res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is False and res['needs_attention'] is True
        assert not env['fake'].called('POST', AP2)

    def test_a_second_settle_of_the_same_mandate_places_no_second_order(self, env):
        out = self._prepare(env)
        env['mandates'].approve(out['mandate_id'], UID)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-1'})
        m = env['mandates'].get(out['mandate_id'])
        view = ct.cart_view(ORDER)
        payment = env['ledger'].get_payment(m.payment_id)
        first = ct._place_order(ct._client(), UID, m, view, payment)
        second = ct._place_order(ct._client(), UID, m, view, payment)
        assert first['success'] is True and second['success'] is False
        assert len(env['fake'].called('POST', AP2)) == 1

    def test_the_shoppers_approval_pays_and_places_the_order(self, env):
        from integrations.ap2.ap2_mandate import decide_payment
        out = self._prepare(env)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-7'})
        payload, code = decide_payment(out['payment_id'], UID, True,
                                       store=env['mandates'])
        assert code == 200 and payload['status'] == 'approved'
        assert payload['result']['order_id'] == 'ORD-7'
        assert env['mandates'].get(out['mandate_id']).status == 'consumed'
        assert env['ledger'].get_payment(out['payment_id']).status == PaymentStatus.COMPLETED
        # every card went to this shopper's own stream
        assert {c.kwargs['user_id'] for c in env['ui'].call_args_list} == {UID}

    def test_approval_with_a_changed_cart_charges_nothing(self, env):
        from integrations.ap2.ap2_mandate import decide_payment
        out = self._prepare(env)
        drifted = json.loads(json.dumps(ORDER))
        drifted['orderItems'][1]['quantity'] = 4
        env['fake'].on('GET', '/cart', body=drifted)
        payload, code = decide_payment(out['payment_id'], UID, True,
                                       store=env['mandates'])
        assert code == 200 and payload['result']['success'] is False
        # Nothing was charged and the approval is void: the payment is
        # cancelled, not left AUTHORIZED for anything else to take.
        assert env['ledger'].get_payment(out['payment_id']).status == PaymentStatus.CANCELLED
        assert env['mandates'].find_by_payment_id(out['payment_id']).status == 'rejected'
        assert pushed(env, 'notification')[-1]['title'] == 'Your cart changed'
        assert not env['fake'].called('POST', AP2)

    def test_paid_but_order_failed_is_flagged_for_a_person(self, env):
        out = self._prepare(env)
        env['mandates'].approve(out['mandate_id'], UID)
        env['fake'].on('POST', AP2, status=409,
                       body={'messages': [{'messageKey': 'AMOUNT_MISMATCH'}]})
        res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is False and res['needs_attention'] is True
        assert 'AMOUNT_MISMATCH' in res['error']
        # A definite refusal is not retried blindly.
        assert len(env['fake'].called('POST', AP2)) == 1


class TestMerchant:
    def test_onboard_validates_before_drafting(self, env):
        out = json.loads(ct.commerce_onboard_merchant(
            MUID, '', 'x', '12', '1', 'nope'))
        assert out['success'] is False
        for word in ('store name', 'pincode', 'phone', 'email'):
            assert word in out['error']
        assert pushed(env) == []

    def test_onboard_drafts_and_asks_for_approval(self, env):
        out = json.loads(ct.commerce_onboard_merchant(
            MUID, 'Asha Fresh', '12 Main Rd', '600078', '+91 98400 12345',
            'owner@shop.in', delivery_radius_km=4))
        d = env['drafts'].get(out['draft_id'])
        assert d['kind'] == 'merchant' and d['status'] == 'pending'
        assert d['payload']['deliveryRadiusKm'] == 4.0
        form = pushed(env, 'form')[0]
        assert form['title'] == 'Review your store details'
        assert {f['name'] for f in form['fields']} >= {'displayName', 'zip', 'email'}
        assert pushed(env, 'approval')[0]['action'] == f"merchant_onboard:{out['draft_id']}"
        assert env['fake'].calls == []  # nothing reaches McGroce before approval

    def test_create_sku_needs_a_merchant(self, env):
        out = json.loads(ct.commerce_create_sku(UID, 'Ghee', 450, 'Dairy'))
        assert out['success'] is False and 'merchant' in out['error']

    def test_create_sku_drafts_with_the_merchants_store(self, env):
        out = json.loads(ct.commerce_create_sku(
            MUID, 'Ghee 500 ml', 450, 'Dairy', image_url='https://img/g.png',
            options='500 ml, 1 L'))
        d = env['drafts'].get(out['draft_id'])
        assert d['payload']['storeId'] == '9'
        assert d['payload']['options'] == ['500 ml', '1 L']
        card = pushed(env, 'product_card')[0]
        assert card['image_url'] == 'https://img/g.png' and card['price'] == 450.0
        assert pushed(env, 'approval')[0]['action'] == f"merchant_sku:{out['draft_id']}"

    def test_create_sku_rejects_bad_price_and_http_image(self, env):
        assert json.loads(ct.commerce_create_sku(MUID, 'X', 0, 'D'))['success'] is False
        assert json.loads(ct.commerce_create_sku(
            MUID, 'X', 5, 'D', image_url='http://img/x.png'))['success'] is False


class _PhonePeDouble(ap2_protocol.PaymentGatewayConnector):
    def __init__(self):
        super().__init__(ap2_protocol.PaymentGateway.PHONEPE)
        self.capture_calls = 0

    def connect(self):
        self.connected = True
        return True

    def create_payment(self, payment_request):
        return {'success': True, 'transaction_id': 'hartos_txn',
                'redirect_url': 'https://pay.example/c'}

    def capture_payment(self, payment_id, gateway_transaction_id):
        self.capture_calls += 1
        return {'success': True}


class TestRedirectGatewayCheckout:
    """INR goes to PhonePe when it is live: checkout hands back a redirect,
    and the gateway callback -- not the agent -- places the order."""

    def _approved_on_phonepe(self, env):
        env['ledger'].gateways[ap2_protocol.PaymentGateway.PHONEPE] = _PhonePeDouble()
        env['fake'].on('GET', '/cart', body=ORDER)
        out = json.loads(ct.commerce_prepare_checkout(UID))
        env['mandates'].approve(out['mandate_id'], UID)
        return out

    def test_checkout_returns_the_redirect_and_places_nothing(self, env):
        out = self._approved_on_phonepe(env)
        res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res == {'success': True, 'status': 'awaiting_payment',
                       'redirect_url': 'https://pay.example/c',
                       'payment_id': out['payment_id']}
        assert env['ledger'].get_payment(out['payment_id']).status == PaymentStatus.PROCESSING
        assert pushed(env, 'payment_status')[-1]['status'] == 'processing'
        assert not env['fake'].called('POST', AP2)
        # a second call cannot charge again
        again = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert again['success'] is False
        assert pushed(env, 'payment_status')[-1]['status'] == 'processing'

    def test_callback_completion_places_the_approved_order(self, env):
        out = self._approved_on_phonepe(env)
        ct.commerce_checkout(UID, out['mandate_id'])
        env['ledger'].get_payment(out['payment_id']).update_status(PaymentStatus.COMPLETED)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-9'})
        later = time.time() + 3600  # the callback may land after the mandate TTL
        with patch('integrations.ap2.ap2_mandate.time.time', return_value=later):
            res = ct.complete_redirect_checkout(out['payment_id'])
        assert res['success'] is True and res['order_id'] == 'ORD-9'
        assert env['mandates'].get(out['mandate_id']).status == 'consumed'
        # The attestation is fresh at order time, not at mandate creation:
        # McGroce sees it unexpired however late the callback came.
        claims = attestation_of(env['fake'].called('POST', AP2)[0])
        assert claims['iat'] >= int(later) and claims['gw'] == 'phonepe'

    def test_callback_with_a_changed_cart_holds_the_order(self, env):
        out = self._approved_on_phonepe(env)
        ct.commerce_checkout(UID, out['mandate_id'])
        env['ledger'].get_payment(out['payment_id']).update_status(PaymentStatus.COMPLETED)
        drifted = json.loads(json.dumps(ORDER))
        drifted['orderItems'].pop()
        env['fake'].on('GET', '/cart', body=drifted)
        res = ct.complete_redirect_checkout(out['payment_id'])
        assert res['success'] is False and res['needs_attention'] is True
        assert not env['fake'].called('POST', AP2)
        assert pushed(env, 'notification')[-1]['title'] == 'Payment received, order on hold'

    def test_not_completed_payment_places_nothing(self, env):
        out = self._approved_on_phonepe(env)
        ct.commerce_checkout(UID, out['mandate_id'])
        res = ct.complete_redirect_checkout(out['payment_id'])
        assert res == {'success': False, 'error': 'payment is not completed'}


class TestNoLiveGateway:
    """A node with no gateway keys cannot take money.  select_gateway then
    answers MOCK, and before this the mock captured anything: the cart was
    marked paid and the McGroce order placed with nothing taken (read at
    903cd58c0 by claude-autostart-93, 2026-10-14).  Dev mode is the one
    exemption (security.master_key.is_dev_mode), the same rule the paid-tier
    upgrade route reads (ap2_protocol.mock_settlement_allowed)."""

    def _mock_only(self, env):
        env['ledger'].gateways.pop(ap2_protocol.PaymentGateway.STRIPE)
        env['fake'].on('GET', '/cart', body=ORDER)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-X'})

    def _mandate_on_the_mock(self, env):
        # Created around the prepare-time refusal (an older build, keys
        # removed after the approval): the settler must be the gate itself.
        m = env['mandates'].create_cart_mandate(
            UID, ct.MERCHANT, ct.mandate_cart(ct.cart_view(ORDER)),
            kind=ct.CHECKOUT_KIND, requester_agent_id=ct._AGENT_ID)
        assert env['ledger'].get_payment(m.payment_id).gateway == ap2_protocol.PaymentGateway.MOCK
        return m

    @staticmethod
    def _warnings(caplog):
        """The operator's WARNINGs on the one refusal logger."""
        return [r.getMessage() for r in caplog.records
                if r.name == 'integrations.ap2.ap2_protocol' and r.levelno == logging.WARNING]

    def test_prepare_refuses_before_any_approval_is_asked(self, env, caplog):
        self._mock_only(env)
        with caplog.at_level(logging.WARNING, logger='integrations.ap2.ap2_protocol'):
            out = json.loads(ct.commerce_prepare_checkout(UID))
        assert out['success'] is False and 'cannot take payments' in out['error']
        assert pushed(env, 'approval') == [] and pushed(env, 'checkout') == []
        assert env['ledger'].payments == {}
        warned = self._warnings(caplog)
        assert len(warned) == 1 and 'STRIPE_API_KEY' in warned[0] and '=' not in warned[0]
        assert 'STRIPE_API_KEY' not in out['error']       # the shopper reads no setting

    def test_settle_refuses_a_mock_payment_and_cancels_it(self, env, caplog):
        self._mock_only(env)
        m = self._mandate_on_the_mock(env)
        env['mandates'].approve(m.mandate_id, UID)
        with caplog.at_level(logging.WARNING, logger='integrations.ap2.ap2_protocol'):
            res = json.loads(ct.commerce_checkout(UID, m.mandate_id))
        assert res['success'] is False and 'cannot take payments' in res['error']
        assert not env['fake'].called('POST', AP2)
        assert env['mandates'].get(m.mandate_id).status == 'rejected'
        assert env['ledger'].get_payment(m.payment_id).status == PaymentStatus.CANCELLED
        assert pushed(env, 'notification')[-1]['title'] == 'Payment not available'
        assert pushed(env, 'payment_status') == []
        warned = self._warnings(caplog)
        assert len(warned) == 1 and 'STRIPE_API_KEY' in warned[0] and m.payment_id in warned[0]

    def test_the_shoppers_approval_is_refused_the_same_way(self, env):
        from integrations.ap2.ap2_mandate import decide_payment
        self._mock_only(env)
        m = self._mandate_on_the_mock(env)
        payload, code = decide_payment(m.payment_id, UID, True, store=env['mandates'])
        assert code == 200 and payload['result']['success'] is False
        assert 'cannot take payments' in payload['result']['error']
        assert env['ledger'].get_payment(m.payment_id).status == PaymentStatus.CANCELLED
        assert not env['fake'].called('POST', AP2)

    def test_dev_mode_node_still_settles_through_the_mock(self, env, monkeypatch):
        from security import master_key
        self._mock_only(env)
        monkeypatch.setattr(master_key, 'is_dev_mode', lambda: True)
        out = json.loads(ct.commerce_prepare_checkout(UID))
        assert out['status'] == 'awaiting_approval'
        env['mandates'].approve(out['mandate_id'], UID)
        res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is True and res['order_id'] == 'ORD-X'
        assert env['ledger'].get_payment(out['payment_id']).gateway == ap2_protocol.PaymentGateway.MOCK

    def test_a_live_rail_is_untouched_by_the_rule(self, env):
        env['fake'].on('GET', '/cart', body=ORDER)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-Y'})
        out = json.loads(ct.commerce_prepare_checkout(UID))
        env['mandates'].approve(out['mandate_id'], UID)
        res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is True and res['order_id'] == 'ORD-Y'
        assert env['card'].captures == [(out['payment_id'], f"card_{out['payment_id']}")]
