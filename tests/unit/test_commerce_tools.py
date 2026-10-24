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
import contextlib
import hashlib
import hmac
import inspect
import json
import logging
import time
from decimal import Decimal
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
from integrations.social.models import Notification, User, db_session, init_db
from integrations.social.services import NotificationService

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
#: McGroce once an order is placed from the cart: the shopper's open cart is
#: no longer that cart (Ap2CheckoutServiceImpl.completeAp2 finalizes it), so
#: GET /cart answers this, or a fresh cart with another id.
CART_GONE = (404, {'messages': [{'messageKey': 'CART_NOT_FOUND',
                                 'message': 'There is no cart'}]})


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.content = b'' if body is None else json.dumps(body).encode()

    def json(self):
        return self._body


class FakeMcGroce:
    """Routes (method, path) to canned answers and records every call.

    The AP2 order endpoint answers the way McGroce's does when it is up
    (Ap2CheckoutServiceImpl.completeAp2 on mcgroce-agentic-commerce): a
    mandate that one of this customer's placed orders already carries is
    answered with that order BEFORE any cart check, and an attestation that
    names no lines is refused for any other mandate (Ap2MandateVerifierImpl:
    'items'), so such a probe can place nothing.  An order McGroce placed is
    remembered by its mandate (``placed``); a test sets it to model an order
    placed on a call whose answer was lost.
    """

    REFUSED = (403, {'messages': [{'messageKey': 'AP2_MANDATE_REJECTED',
                                   'message': 'The payment does not match this '
                                              'order (items)'}]})

    def __init__(self):
        self.routes = {}
        self.calls = []
        # A McGroce with POST /cart/checkout/ap2 refuses a call without an
        # attestation and touches nothing: HARTOS's readiness check.
        self.probe = (403, {'messages': [{'messageKey': 'AP2_MANDATE_REJECTED',
                                          'message': 'no payment attestation'}]})
        self.probes = 0
        self.placed = {}          # mandate id -> the order McGroce placed for it

    def on(self, method, path, status=200, body=None):
        self.routes[(method, path)] = (status, body)

    def on_each(self, method, path, *answers):
        """Answer successive calls in turn ((status, body) each); the last
        answer repeats."""
        self.routes[(method, path)] = list(answers)

    def __call__(self, method, url, **kw):
        path = url[len(BASE):] if url.startswith(BASE) else url
        if (method, path) == ('POST', AP2) and \
                signing.MANDATE_HEADER not in (kw.get('headers') or {}):
            self.probes += 1
            return _Resp(*self.probe)
        self.calls.append((method, path, kw))
        answer = self.routes.get((method, path), (404, {'messages': [
            {'messageKey': 'NOT_FOUND', 'message': path}]}))
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        status, body = answer
        if (method, path) == ('POST', AP2) and status < 500:   # McGroce is up
            claims = claims_of(kw['headers'][signing.MANDATE_HEADER])
            if claims['mid'] in self.placed:
                return _Resp(200, {'orderNumber': self.placed[claims['mid']]})
            if not claims.get('lines'):
                return _Resp(*self.REFUSED)
            if status < 300 and isinstance(body, dict) and body.get('orderNumber'):
                self.placed[claims['mid']] = body['orderNumber']
        return _Resp(status, body)

    def called(self, method, path):
        return [c for c in self.calls if c[0] == method and c[1] == path]


@contextlib.contextmanager
def disk_full_once(store, field):
    """The mandate file cannot be written once any record carries ``field``:
    ENOSPC from the write, as atomic_json_write raises it.  The disk is the
    boundary; no store method is faked."""
    import errno
    real = store._save

    def save():
        if any(getattr(m, field) for m in store._mandates.values()):
            raise OSError(errno.ENOSPC, 'No space left on device')
        return real()
    with patch.object(store, '_save', side_effect=save):
        yield


def claims_of(token: str):
    """The claims an X-Commerce-Mandate carries (the fake's routing; the MAC
    is checked by attestation_of)."""
    b64 = token.split('.')[0]
    return json.loads(base64.urlsafe_b64decode(b64 + '=' * (-len(b64) % 4)))


def attestation_of(call):
    """The claims of the X-Commerce-Mandate one McGroce order call carried,
    after checking its MAC the way McGroce's verifier does."""
    token = call[2]['headers'][signing.MANDATE_HEADER]
    b64, mac = token.split('.')
    key = hmac.new(SECRET.encode(), signing.MANDATE_KEY_LABEL, hashlib.sha256).digest()
    assert hmac.compare_digest(hmac.new(key, b64.encode(), hashlib.sha256).hexdigest(), mac)
    return claims_of(token)


OWNER = '10202'          # this computer's owner, who hears about held payments
STRANGER = '20909'       # an id with no user row on this node


def node_user(uid):
    """``uid`` is a user of this node: a row in its users table, as the
    owner's and a signed-in shopper's are.  A McGroce-only shopper
    (``mcg-<id>``, bound but never signed in here) has none."""
    with db_session(commit=True) as db:
        if db.query(User.id).filter(User.id == uid).first() is None:
            db.add(User(id=uid, username=f'u-{uid}', user_type='human'))


def not_a_node_user(uid):
    with db_session(commit=True) as db:
        db.query(User).filter(User.id == uid).delete(synchronize_session=False)


def forget_bells(*uids):
    with db_session(commit=True) as db:
        db.query(Notification).filter(Notification.user_id.in_(uids)).delete(
            synchronize_session=False)


def bell(uid, payment_id=None):
    """What ``uid`` finds in their bell (the notification rows, which outlive
    a restart): (type, message) of each, for ``payment_id`` when given."""
    with db_session(commit=False) as db:
        rows, _ = NotificationService.get_for_user(db, uid)
        return [(r.type, r.message) for r in rows
                if payment_id is None or r.target_id == payment_id]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('COMMERCE_SESSION_SECRET', SECRET)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    # This node's users: the owner has an account here, the shopper does not
    # (the test database is this process's own, hermetic under pytest).
    init_db()
    node_user(OWNER)
    not_a_node_user(UID)
    forget_bells(OWNER, UID, STRANGER)
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


def pushed(env, type_=None, to=None):
    """Cards pushed so far, by type and by recipient (``to``; the shopper's
    when None, so the owner's cards never pass as the shopper's notice)."""
    want = OWNER if to == OWNER else None
    comps = [c.args[1] for c in env['ui'].call_args_list
             if (c.kwargs.get('user_id') == OWNER) == (want == OWNER)]
    return [c for c in comps if type_ is None or c['type'] == type_]


def owner_cards(env):
    return pushed(env, to=OWNER)


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
        # One call more left: a probe naming the paid cart and no lines, which
        # asks McGroce whether the mandate already became an order and can
        # place nothing (McGroce refuses an attestation without lines).
        (probe,) = env['fake'].called('POST', AP2)[n:]
        assert attestation_of(probe)['oid'] == '555' and attestation_of(probe)['lines'] == []
        assert env['mandates'].get(out['mandate_id']).order_ref is None

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

    def test_no_secret_means_the_shopper_is_never_asked_to_pay(self, env, monkeypatch):
        # No order could be signed for McGroce, so nothing is offered: money
        # taken for an order that cannot be placed is the failure to avoid.
        monkeypatch.delenv('COMMERCE_SESSION_SECRET')
        env['fake'].on('GET', '/cart', body=ORDER)
        with patch('core.config_cache.get_config', return_value={}):
            out = json.loads(ct.commerce_prepare_checkout(UID))
        assert out['success'] is False and 'not set up' in out['error']
        assert 'COMMERCE_SESSION_SECRET' not in out['error']   # no setting for the shopper
        assert pushed(env, 'approval') == [] and env['ledger'].payments == {}

    def test_a_secret_lost_after_approval_charges_nothing(self, env, monkeypatch):
        out = self._prepare(env)
        env['mandates'].approve(out['mandate_id'], UID)
        monkeypatch.delenv('COMMERCE_SESSION_SECRET')
        with patch('core.config_cache.get_config', return_value={}):
            res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is False and 'nothing was charged' in res['error']
        assert env['card'].captures == []
        assert env['ledger'].get_payment(out['payment_id']).status == PaymentStatus.CANCELLED
        assert not env['fake'].called('POST', AP2)

    def test_a_mcgroce_without_agent_orders_is_never_paid(self, env):
        # Until McGroce ships /cart/checkout/ap2 it answers 404: no card, no money.
        env['fake'].probe = (404, None)
        out = self._prepare(env)
        assert out['success'] is False and 'agent orders' in out['error']
        assert pushed(env, 'approval') == [] and env['ledger'].payments == {}

    def test_a_line_mcgroce_cannot_check_is_never_paid_for(self, env):
        no_sku = json.loads(json.dumps(ORDER))
        no_sku['orderItems'][0]['skuId'] = None
        env['fake'].on('GET', '/cart', body=no_sku)
        out = json.loads(ct.commerce_prepare_checkout(UID))
        assert out['success'] is False and env['ledger'].payments == {}

    def test_a_cart_changed_after_its_payment_is_not_paid_for_again(self, env):
        # Paid, the order failed, then McGroce repriced a line of the SAME cart:
        # the new contents must not open a second payment for it.
        out = self._prepare(env)
        env['mandates'].approve(out['mandate_id'], UID)
        env['fake'].on('POST', AP2, status=503, body=None)
        json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        repriced = json.loads(json.dumps(ORDER))
        repriced['orderItems'][1]['salePrice'] = {'amount': 150}
        repriced['total'] = {'amount': 230.0, 'currency': 'INR'}
        env['fake'].on('GET', '/cart', body=repriced)
        again = json.loads(ct.commerce_prepare_checkout(UID))
        assert again['success'] is False and out['mandate_id'] in again['error']
        assert len(env['card'].captures) == 1

    def test_two_approvals_for_one_cart_never_both_charge(self, env):
        # Two prepares that raced past supersede: both cards are live.
        from integrations.ap2.ap2_mandate import decide_payment
        first = self._prepare(env)
        second = env['mandates'].create_cart_mandate(
            UID, ct.MERCHANT, ct.mandate_cart(ct.cart_view(ORDER)),
            kind=ct.CHECKOUT_KIND, requester_agent_id=ct._AGENT_ID, cart_ref='555')
        env['fake'].on('POST', AP2, status=503, body=None)
        decide_payment(first['payment_id'], UID, True, store=env['mandates'])
        payload, code = decide_payment(second.payment_id, UID, True, store=env['mandates'])
        assert payload['result']['success'] is False
        assert 'nothing was charged' in payload['result']['error']
        assert len(env['card'].captures) == 1
        assert env['ledger'].get_payment(second.payment_id).status == PaymentStatus.CANCELLED

    def test_an_old_payment_never_funds_a_later_cart(self, env):
        # Consumed by the old build (no cart recorded); a week later the
        # shopper has the same basket in a new cart.
        old = env['mandates'].create_cart_mandate(
            UID, ct.MERCHANT, ct.mandate_cart(ct.cart_view(ORDER)),
            kind=ct.CHECKOUT_KIND, requester_agent_id=ct._AGENT_ID)
        env['mandates'].approve(old.mandate_id, UID)
        env['mandates'].consume(old.mandate_id)
        later = json.loads(json.dumps(ORDER))
        later['id'] = 556
        env['fake'].on('GET', '/cart', body=later)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-NEW'})
        res = json.loads(ct.commerce_checkout(UID, old.mandate_id))
        assert res['success'] is False and 'older version' in res['error']
        assert not env['fake'].called('POST', AP2)
        # ...and it does not block paying for the new cart.
        assert json.loads(ct.commerce_prepare_checkout(UID))['status'] == 'awaiting_approval'

    def test_a_retry_never_signs_for_another_cart(self, env):
        out = self._prepare(env)
        env['mandates'].approve(out['mandate_id'], UID)
        env['fake'].on('POST', AP2, status=503, body=None)
        json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        n = len(env['fake'].called('POST', AP2))
        moved = json.loads(json.dumps(ORDER))
        moved['id'] = 556
        env['fake'].on('GET', '/cart', body=moved)
        res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is False and res['needs_attention'] is True
        # The one call that left is the probe for the PAID cart, with no
        # lines: nothing is ever signed for the other cart.
        (probe,) = env['fake'].called('POST', AP2)[n:]
        assert attestation_of(probe)['oid'] == '555' and attestation_of(probe)['lines'] == []
        assert env['mandates'].get(out['mandate_id']).order_ref is None

    def test_a_declined_card_does_not_lock_the_shopper_out(self, env):
        from integrations.ap2.ap2_mandate import decide_payment
        first = self._prepare(env)
        with patch.object(env['card'], 'create_payment',
                          return_value={'success': False, 'error': 'card declined'}):
            decide_payment(first['payment_id'], UID, True, store=env['mandates'])
        assert env['ledger'].get_payment(first['payment_id']).status == PaymentStatus.FAILED
        again = self._prepare(env)
        assert again.get('status') == 'awaiting_approval', again
        assert env['mandates'].get(first['mandate_id']).status == 'rejected'

    def test_a_mcgroce_rolled_back_after_the_card_was_shown_is_never_paid(self, env):
        # Ready when the card was shown, gone when the shopper approves:
        # McGroce is asked again before the money moves, not remembered.
        from integrations.ap2.ap2_mandate import decide_payment
        out = self._prepare(env)
        env['fake'].probe = (404, None)
        payload, _ = decide_payment(out['payment_id'], UID, True, store=env['mandates'])
        assert payload['result']['success'] is False
        assert 'nothing was charged' in payload['result']['error']
        assert env['card'].captures == []
        assert env['ledger'].get_payment(out['payment_id']).status == PaymentStatus.CANCELLED

    def test_a_card_shown_by_the_older_version_is_never_paid(self, env):
        # No cart was recorded with it, so no order could be signed for it.
        from integrations.ap2.ap2_mandate import decide_payment
        env['fake'].on('GET', '/cart', body=ORDER)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-1'})
        old = env['mandates'].create_cart_mandate(
            UID, ct.MERCHANT, ct.mandate_cart(ct.cart_view(ORDER)),
            kind=ct.CHECKOUT_KIND, requester_agent_id=ct._AGENT_ID)
        payload, _ = decide_payment(old.payment_id, UID, True, store=env['mandates'])
        assert payload['result']['success'] is False
        assert 'nothing was charged' in payload['result']['error']
        assert env['card'].captures == [] and not env['fake'].called('POST', AP2)
        assert env['ledger'].get_payment(old.payment_id).status == PaymentStatus.CANCELLED
        assert pushed(env, 'notification')[-1]['title'] == 'Payment not taken'
        assert self._prepare(env)['status'] == 'awaiting_approval'

    def test_a_retry_while_the_approval_is_paying_leaves_it_alone(self, env):
        out = self._prepare(env)
        store = env['mandates']
        store.approve(out['mandate_id'], UID)
        assert store.begin_settle(out['mandate_id'])[0]   # the approval is paying
        try:
            res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        finally:
            store.end_settle(out['mandate_id'])
        assert res['success'] is False and 'already being taken' in res['error']
        assert store.get(out['mandate_id']).status == 'approved'
        assert env['ledger'].get_payment(out['payment_id']).status == PaymentStatus.AUTHORIZED
        assert pushed(env, 'notification') == []

    def test_an_edited_record_is_never_signed_for_mcgroce(self, env):
        # Paid 240, order failed; then the mandate file is edited to say 960
        # for a bigger basket.  The retry must not attest it, and nothing may
        # re-sign the edited record into a valid one.
        from integrations.ap2.ap2_mandate import canonical_cart_hash
        out = self._prepare(env)
        env['mandates'].approve(out['mandate_id'], UID)
        env['fake'].on('POST', AP2, status=503, body=None)
        json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        store = env['mandates']
        big = json.loads(json.dumps(ORDER))
        big['orderItems'][0]['quantity'] = 20
        big['total'] = {'amount': 960.0, 'currency': 'INR'}
        raw = json.loads(open(store.path, encoding='utf-8').read())
        rec = raw['mandates'][out['mandate_id']]
        rec['amount'] = '960.00'
        rec['cart_hash'] = canonical_cart_hash(ct.mandate_cart(ct.cart_view(big)))
        open(store.path, 'w', encoding='utf-8').write(json.dumps(raw))
        edited = MandateStore(store.path, ledger=store.ledger, key=b'k' * 32)
        n = len(env['fake'].called('POST', AP2))
        env['fake'].on('GET', '/cart', body=big)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-BIG'})
        with patch.object(ct, '_mandates', return_value=edited):
            res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is False and res['needs_attention'] is True
        assert len(env['fake'].called('POST', AP2)) == n
        assert edited.record_order(out['mandate_id'], 'ORD-BIG') is False
        m = edited.get(out['mandate_id'])
        assert not edited.paid_as_signed(m, env['ledger'].get_payment(m.payment_id))

    def test_an_approval_for_one_cart_never_pays_for_another(self, env):
        out = self._prepare(env)
        env['mandates'].approve(out['mandate_id'], UID)
        moved = json.loads(json.dumps(ORDER))
        moved['id'] = 556                   # same basket, another McGroce cart
        env['fake'].on('GET', '/cart', body=moved)
        res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is False and 'not the cart that was approved' in res['error']
        assert env['card'].captures == [] and not env['fake'].called('POST', AP2)
        assert env['ledger'].get_payment(out['payment_id']).status == PaymentStatus.CANCELLED

    def test_a_node_whose_key_cannot_be_read_pauses_and_changes_nothing(self, env):
        # A worker whose mandate key file is unreadable right now: the tools
        # say payments are paused, the approval stays as it was, no money
        # moves and no card is shown.  (The file-level rule is pinned in
        # test_ap2_mandate; here the key loader is the boundary.)
        from integrations.ap2.ap2_mandate import MandateKeyError, PAYMENTS_PAUSED
        out = self._prepare(env)
        env['mandates'].approve(out['mandate_id'], UID)
        payments = len(env['ledger'].payments)
        cards = len(pushed(env))
        paused = MandateStore(env['mandates'].path, ledger=env['ledger'])   # no cached key
        with patch.object(ct, '_mandates', return_value=paused), \
                patch('integrations.ap2.ap2_mandate._load_or_create_key',
                      side_effect=MandateKeyError()):
            res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
            again = json.loads(ct.commerce_prepare_checkout(UID))
        assert res == {'success': False, 'error': PAYMENTS_PAUSED}
        assert again == {'success': False, 'error': PAYMENTS_PAUSED}
        assert env['mandates'].get(out['mandate_id']).status == 'approved'
        assert env['ledger'].get_payment(out['payment_id']).status == PaymentStatus.AUTHORIZED
        assert env['card'].captures == [] and not env['fake'].called('POST', AP2)
        assert len(env['ledger'].payments) == payments and len(pushed(env)) == cards

    def test_only_a_mcgroce_checkout_mandate_is_checked_out(self, env):
        generic = env['mandates'].create_cart_mandate(
            UID, ct.MERCHANT, ct.mandate_cart(ct.cart_view(ORDER)))
        res = json.loads(ct.commerce_checkout(UID, generic.mandate_id))
        assert res['success'] is False and 'not found' in res['error']

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
        # The one call that left is the probe (the paid cart, no lines),
        # which McGroce refuses for a mandate no placed order carries.
        (probe,) = env['fake'].called('POST', AP2)
        assert attestation_of(probe)['oid'] == '555' and attestation_of(probe)['lines'] == []
        assert env['mandates'].get(out['mandate_id']).order_ref is None
        assert pushed(env, 'notification')[-1]['title'] == 'Payment received, order on hold'

    def test_callback_for_another_cart_holds_the_order_and_says_so(self, env):
        out = self._approved_on_phonepe(env)
        ct.commerce_checkout(UID, out['mandate_id'])
        env['ledger'].get_payment(out['payment_id']).update_status(PaymentStatus.COMPLETED)
        moved = json.loads(json.dumps(ORDER))
        moved['id'] = 556                         # same basket, another cart
        env['fake'].on('GET', '/cart', body=moved)
        res = ct.complete_redirect_checkout(out['payment_id'])
        assert res['success'] is False and res['needs_attention'] is True
        assert 'no longer holds what was paid for' in res['error']
        # Nothing is signed for the other cart: the one call is the probe
        # for the PAID cart with no lines, which McGroce refuses.
        (probe,) = env['fake'].called('POST', AP2)
        assert attestation_of(probe)['oid'] == '555' and attestation_of(probe)['lines'] == []
        assert env['mandates'].get(out['mandate_id']).order_ref is None
        assert pushed(env, 'notification')[-1]['title'] == 'Payment received, order on hold'

    def test_mcgroce_down_at_the_callback_is_placed_later_without_a_new_charge(self, env):
        out = self._approved_on_phonepe(env)
        ct.commerce_checkout(UID, out['mandate_id'])
        env['ledger'].get_payment(out['payment_id']).update_status(PaymentStatus.COMPLETED)
        env['fake'].on('GET', '/cart', status=503, body=None)
        held = ct.complete_redirect_checkout(out['payment_id'])
        assert held['success'] is False and held['needs_attention'] is True
        assert pushed(env, 'notification')[-1]['title'] == 'Payment received, order on hold'
        # The money is taken: the mandate is used up, so the order step can
        # be retried, and never charges again.
        assert env['mandates'].get(out['mandate_id']).status == 'consumed'
        env['fake'].on('GET', '/cart', body=ORDER)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-OK'})
        retry = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert retry['success'] is True and retry['order_id'] == 'ORD-OK'
        assert len(env['ledger'].payments) == 1
        assert attestation_of(env['fake'].called('POST', AP2)[0])['gw'] == 'phonepe'

    def test_a_cart_changed_while_paying_is_ordered_once_it_is_put_back(self, env):
        # McGroce keeps ONE open cart per shopper, so the changed cart is the
        # same cart: it is not offered for payment again, the shopper is told
        # how to get the order placed, and putting it back places it with the
        # payment already made.
        out = self._approved_on_phonepe(env)
        ct.commerce_checkout(UID, out['mandate_id'])
        env['ledger'].get_payment(out['payment_id']).update_status(PaymentStatus.COMPLETED)
        drifted = json.loads(json.dumps(ORDER))
        drifted['orderItems'].pop()
        env['fake'].on('GET', '/cart', body=drifted)
        held = ct.complete_redirect_checkout(out['payment_id'])
        assert held['success'] is False and held['cart_changed'] is True
        note = pushed(env, 'notification')[-1]
        assert note['title'] == 'Payment received, order on hold'
        assert 'put the cart back' in note['message'].lower()
        again = json.loads(ct.commerce_prepare_checkout(UID))
        assert again['success'] is False and out['mandate_id'] in again['error']
        assert 'commerce_checkout' in again['error']
        env['fake'].on('GET', '/cart', body=ORDER)              # put back
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-BACK'})
        res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is True and res['order_id'] == 'ORD-BACK'
        assert len(env['ledger'].payments) == 1

    def test_once_a_held_cart_is_ordered_another_way_the_next_cart_can_be_paid(self, env):
        # McGroce opens a new cart (another id) only after the held one is
        # ordered, on the site or by a person: that new cart is not held up.
        out = self._approved_on_phonepe(env)
        ct.commerce_checkout(UID, out['mandate_id'])
        env['ledger'].get_payment(out['payment_id']).update_status(PaymentStatus.COMPLETED)
        drifted = json.loads(json.dumps(ORDER))
        drifted['orderItems'].pop()
        env['fake'].on('GET', '/cart', body=drifted)
        ct.complete_redirect_checkout(out['payment_id'])
        nxt = json.loads(json.dumps(ORDER))
        nxt['id'] = 556
        env['fake'].on('GET', '/cart', body=nxt)
        assert json.loads(ct.commerce_prepare_checkout(UID))['status'] == 'awaiting_approval'

    def test_a_record_failing_its_signature_at_the_callback_is_held_and_said(self, env):
        # Edited outside the store, or signed with a key the node lost: the
        # money is taken and no order can be signed.  The shopper is told, the
        # cause is named, and only that cart waits.
        out = self._approved_on_phonepe(env)
        ct.commerce_checkout(UID, out['mandate_id'])
        env['ledger'].get_payment(out['payment_id']).update_status(PaymentStatus.COMPLETED)
        store = env['mandates']
        raw = json.loads(open(store.path, encoding='utf-8').read())
        raw['mandates'][out['mandate_id']]['description'] = 'edited'
        open(store.path, 'w', encoding='utf-8').write(json.dumps(raw))
        edited = MandateStore(store.path, ledger=store.ledger, key=b'k' * 32)
        with patch.object(ct, '_mandates', return_value=edited):
            res = ct.complete_redirect_checkout(out['payment_id'])
            assert res['success'] is False and res['needs_attention'] is True
            assert 'signature' in res['error'] and 'withdrawn' not in res['error']
            assert pushed(env, 'notification')[-1]['title'] == 'Payment received, order on hold'
            assert not env['fake'].called('POST', AP2)
            again = json.loads(ct.commerce_prepare_checkout(UID))
            assert again['success'] is False and out['mandate_id'] in again['error']
            nxt = json.loads(json.dumps(ORDER))
            nxt['id'] = 556
            env['fake'].on('GET', '/cart', body=nxt)
            assert json.loads(ct.commerce_prepare_checkout(UID))['status'] == 'awaiting_approval'

    def test_a_different_amount_taken_never_opens_a_second_payment(self, env):
        from integrations.ap2.ap2_protocol import MONEY_TAKEN
        out = self._approved_on_phonepe(env)
        ct.commerce_checkout(UID, out['mandate_id'])
        p = env['ledger'].get_payment(out['payment_id'])
        # what the PhonePe callback records when it captured another amount
        p.update_status(PaymentStatus.FAILED, 'amount mismatch')
        p.metadata = {**p.metadata, MONEY_TAKEN: True}
        ct.hold_paid_order(out['payment_id'], 'PhonePe took a different amount')
        assert pushed(env, 'notification')[-1]['title'] == 'Payment received, order on hold'
        again = json.loads(ct.commerce_prepare_checkout(UID))
        assert again['success'] is False and out['mandate_id'] in again['error']
        later = time.time() + 3600          # past the mandate's TTL: still held
        with patch('integrations.ap2.ap2_mandate.time.time', return_value=later):
            assert json.loads(ct.commerce_prepare_checkout(UID))['success'] is False
        assert len(env['ledger'].payments) == 1

    def test_a_callback_that_died_before_its_order_is_finished_by_checkout(self, env):
        out = self._approved_on_phonepe(env)
        ct.commerce_checkout(UID, out['mandate_id'])
        # The callback completed the payment and died before its order step.
        env['ledger'].get_payment(out['payment_id']).update_status(PaymentStatus.COMPLETED)
        again = json.loads(ct.commerce_prepare_checkout(UID))
        assert again['success'] is False and out['mandate_id'] in again['error']
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-LATE'})
        res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is True and res['order_id'] == 'ORD-LATE'
        assert len(env['ledger'].payments) == 1
        assert env['mandates'].get(out['mandate_id']).order_ref == 'ORD-LATE'

    def test_a_checkout_racing_the_callback_leaves_the_order_to_it(self, env):
        out = self._approved_on_phonepe(env)
        ct.commerce_checkout(UID, out['mandate_id'])
        env['ledger'].get_payment(out['payment_id']).update_status(PaymentStatus.COMPLETED)
        store = env['mandates']
        assert store.begin_settle(out['mandate_id'])[0]        # the callback holds it
        try:
            res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        finally:
            store.end_settle(out['mandate_id'])
        assert res['success'] is False and res['in_progress'] is True
        assert store.get(out['mandate_id']).status == 'approved'   # not used up here
        assert pushed(env, 'notification') == []

    def test_a_retry_racing_another_sends_no_second_order_call(self, env):
        out = self._approved_on_phonepe(env)
        ct.commerce_checkout(UID, out['mandate_id'])
        env['ledger'].get_payment(out['payment_id']).update_status(PaymentStatus.COMPLETED)
        env['fake'].on('GET', '/cart', status=503, body=None)
        ct.complete_redirect_checkout(out['payment_id'])           # held, used up
        env['fake'].on('GET', '/cart', body=ORDER)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-1'})
        store = env['mandates']
        assert store.begin_settle(out['mandate_id'])[0]        # another one is placing it
        try:
            res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        finally:
            store.end_settle(out['mandate_id'])
        assert res['success'] is False and res['in_progress'] is True
        assert not env['fake'].called('POST', AP2)
        res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is True and len(env['fake'].called('POST', AP2)) == 1

    def test_a_card_from_the_older_version_paid_on_phonepe_is_held_and_said(self, env):
        # Approved under a build that recorded no cart, confirmed after the
        # upgrade: no order can be signed, none exists, a person settles it.
        env['ledger'].gateways[ap2_protocol.PaymentGateway.PHONEPE] = _PhonePeDouble()
        env['fake'].on('GET', '/cart', body=ORDER)
        old = env['mandates'].create_cart_mandate(
            UID, ct.MERCHANT, ct.mandate_cart(ct.cart_view(ORDER)),
            kind=ct.CHECKOUT_KIND, requester_agent_id=ct._AGENT_ID)
        env['mandates'].approve(old.mandate_id, UID)
        env['ledger'].get_payment(old.payment_id).update_status(PaymentStatus.COMPLETED)
        res = ct.complete_redirect_checkout(old.payment_id)
        assert res['success'] is False and res['needs_attention'] is True
        assert 'older version' in res['error'] and 'refund' in res['error']
        assert pushed(env, 'notification')[-1]['title'] == 'Payment received, order on hold'
        assert not env['fake'].called('POST', AP2)
        assert env['mandates'].get(old.mandate_id).status == 'consumed'

    def test_the_callback_and_a_retry_racing_send_one_order_call(self, env):
        # The callback is inside its claimed order step (McGroce is being
        # asked for the cart) when commerce_checkout retries the same
        # mandate: exactly ONE order call reaches McGroce, counted at the
        # HTTP boundary, and the retry is told the order is in progress.
        import threading
        out = self._approved_on_phonepe(env)
        ct.commerce_checkout(UID, out['mandate_id'])
        env['ledger'].get_payment(out['payment_id']).update_status(PaymentStatus.COMPLETED)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-RACE'})
        fake, in_step, release, callback = env['fake'], threading.Event(), threading.Event(), []

        def gated(method, url, **kw):
            if method == 'GET' and url.endswith('/cart') and not in_step.is_set():
                in_step.set()               # the callback's order step has begun...
                release.wait(10)            # ...and holds here while the retry runs
            return fake(method, url, **kw)

        with patch('core.http_pool.pooled_request', side_effect=gated):
            t = threading.Thread(target=lambda: callback.append(
                ct.complete_redirect_checkout(out['payment_id'])))
            t.start()
            assert in_step.wait(10)
            retry = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
            release.set()
            t.join(30)
        assert retry['success'] is False and retry['in_progress'] is True
        assert callback and callback[0]['success'] is True
        assert callback[0]['order_id'] == 'ORD-RACE'
        assert len(env['fake'].called('POST', AP2)) == 1
        assert env['mandates'].get(out['mandate_id']).order_ref == 'ORD-RACE'
        later = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert later['success'] is True and later['order_id'] == 'ORD-RACE'
        assert len(env['fake'].called('POST', AP2)) == 1

    def test_not_completed_payment_places_nothing(self, env):
        out = self._approved_on_phonepe(env)
        ct.commerce_checkout(UID, out['mandate_id'])
        res = ct.complete_redirect_checkout(out['payment_id'])
        assert res == {'success': False, 'error': 'payment is not completed'}


class TestHeldPayments:
    """ONE hold rule for every payment whose money is taken and whose order is
    not placed: the shopper reads the real amounts and never "nothing was
    charged", this computer's owner gets a card naming what a person must do,
    and the sentence promises follow-up only when that card was published."""

    PAID = ('Your payment of ₹240.00 arrived, but your order is not placed yet, '
            'so it is on hold. Nothing more will be charged; we will sort it out '
            'with you.')
    #: An order call left this node and no answer confirmed or refused it.
    UNCONFIRMED = ('Your payment of ₹240.00 arrived, but we could not confirm your '
                   'order, so it is on hold. Nothing more will be charged; we will '
                   'sort it out with you.')

    def _paid_on_phonepe(self, env):
        env['ledger'].gateways[ap2_protocol.PaymentGateway.PHONEPE] = _PhonePeDouble()
        env['fake'].on('GET', '/cart', body=ORDER)
        out = json.loads(ct.commerce_prepare_checkout(UID))
        env['mandates'].approve(out['mandate_id'], UID)
        ct.commerce_checkout(UID, out['mandate_id'])           # redirect: PROCESSING
        env['ledger'].get_payment(out['payment_id']).update_status(PaymentStatus.COMPLETED)
        return out

    @contextlib.contextmanager
    def _pause(self, env):
        """A worker whose mandate key cannot be read (no cached key)."""
        from integrations.ap2.ap2_mandate import MandateKeyError
        worker = MandateStore(env['mandates'].path, ledger=env['ledger'])
        with patch.object(ct, '_mandates', return_value=worker), \
                patch('integrations.ap2.ap2_mandate._load_or_create_key',
                      side_effect=MandateKeyError()):
            yield worker

    def test_a_paid_shopper_under_the_key_pause_is_told_on_hold_not_nothing_charged(self, env):
        out = self._paid_on_phonepe(env)
        with self._pause(env):
            res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is False and res['needs_attention'] is True
        assert 'on hold' in res['error'] and 'nothing was charged' not in res['error']
        assert env['ledger'].get_payment(out['payment_id']).status == PaymentStatus.COMPLETED
        note = pushed(env, 'notification')[-1]
        assert note['title'] == ct.HOLD_TITLE and note['message'] == self.PAID
        (card,) = owner_cards(env)
        assert out['payment_id'] in card['message'] and UID in card['message']
        assert 'refund it from your PhonePe merchant account' in card['message']

    def test_the_callback_under_the_key_pause_holds_and_tells_instead_of_raising(self, env):
        out = self._paid_on_phonepe(env)
        with self._pause(env):
            res = ct.complete_redirect_checkout(out['payment_id'])
        assert res['held'] is True and res['needs_attention'] is True
        assert 'on hold' in res['error'] and 'nothing was charged' not in res['error']
        assert pushed(env, 'notification')[-1]['message'] == self.PAID
        assert len(owner_cards(env)) == 1
        assert env['mandates'].get(out['mandate_id']).status == 'approved'  # not used up yet
        # Once the key reads again, the retry places the order, no new charge.
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-LATER'})
        later = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert later['success'] is True and later['order_id'] == 'ORD-LATER'
        assert len(env['ledger'].payments) == 1

    def test_a_consumed_unordered_retry_under_the_key_pause_is_told_on_hold(self, env):
        out = self._paid_on_phonepe(env)
        env['fake'].on('GET', '/cart', status=503, body=None)
        ct.complete_redirect_checkout(out['payment_id'])         # used up, held
        env['fake'].on('GET', '/cart', body=ORDER)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-1'})
        with self._pause(env):
            res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is False and 'on hold' in res['error']
        assert 'nothing was charged' not in res['error']
        assert not env['fake'].called('POST', AP2)
        assert pushed(env, 'notification')[-1]['message'] == self.PAID

    def test_an_unpaid_shopper_under_the_key_pause_still_hears_nothing_was_charged(self, env):
        from integrations.ap2.ap2_mandate import PAYMENTS_PAUSED
        env['fake'].on('GET', '/cart', body=ORDER)
        out = json.loads(ct.commerce_prepare_checkout(UID))
        env['mandates'].approve(out['mandate_id'], UID)          # AUTHORIZED, nothing taken
        with self._pause(env):
            res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res == {'success': False, 'error': PAYMENTS_PAUSED}
        assert pushed(env, 'notification') == [] and owner_cards(env) == []

    def test_a_different_amount_names_both_figures(self, env):
        from integrations.ap2.ap2_protocol import MONEY_TAKEN
        out = self._paid_on_phonepe(env)
        p = env['ledger'].get_payment(out['payment_id'])
        p.update_status(PaymentStatus.FAILED, 'amount mismatch')
        p.metadata = {**p.metadata, MONEY_TAKEN: True}
        held = ct.hold_paid_order(out['payment_id'], 'PhonePe took a different amount',
                                  received=Decimal('100'))
        assert held['held'] is True and held['owner_told'] is True
        assert pushed(env, 'notification')[-1]['message'] == (
            'We received ₹100.00 but your order comes to ₹240.00, so your order is '
            'on hold. Nothing more will be charged; we will sort it out with you.')
        (card,) = owner_cards(env)
        assert '₹100.00' in card['message'] and '₹240.00' in card['message']
        assert 'nothing is refunded automatically' in card['message']
        # With no figure from the gateway, the ledger still says another
        # amount was taken: the shopper is not told their full payment arrived.
        ct.hold_paid_order(out['payment_id'], 'PhonePe took a different amount')
        assert pushed(env, 'notification')[-1]['message'].startswith(
            "We received a different amount from your order's ₹240.00, so your "
            'order is on hold.')

    def test_without_an_owner_told_the_sentence_names_the_next_step(self, env, monkeypatch):
        out = self._paid_on_phonepe(env)
        env['fake'].on('GET', '/cart', status=503, body=None)
        monkeypatch.delenv('HEVOLVE_OWNER_USER_ID')
        ct.complete_redirect_checkout(out['payment_id'])
        assert pushed(env, 'notification')[-1]['message'].endswith(
            'Nothing more will be charged; reply here or contact the shop.')
        assert owner_cards(env) == []
        # An owner is configured who is not a user of this node (central on
        # 2026-10-19: HEVOLVE_OWNER_USER_ID named an id with no user row):
        # the live card goes into the void, so the same honest ending.
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', STRANGER)
        held = ct.hold_paid_order(out['payment_id'], 'McGroce is down')
        assert held['owner_told'] is False
        assert pushed(env, 'notification')[-1]['message'].endswith(
            'reply here or contact the shop.')
        assert bell(STRANGER) == []
        # Told is the row in the owner's bell, not the live push: an owner
        # with no client open right now still finds it there.
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
        env['ui'].side_effect = lambda agent_id, component, user_id=None: user_id != OWNER
        held = ct.hold_paid_order(out['payment_id'], 'McGroce is down')
        assert held['owner_told'] is True
        assert pushed(env, 'notification')[-1]['message'].endswith(
            'we will sort it out with you.')
        ((kind, note),) = bell(OWNER, out['payment_id'])
        assert kind == ct.HOLD_NOTICE_TYPE and 'McGroce is down' in note

    def test_a_hold_lands_in_the_bell_of_everyone_who_is_a_user_of_this_node(self, env):
        # The notification row outlives a restart and reaches every client of
        # this node; the live card is pushed on top of it.  A McGroce-only
        # shopper (mcg-<id>, no user row here) gets the live card alone.
        out = self._paid_on_phonepe(env)
        env['fake'].on('GET', '/cart', status=503, body=None)
        held = ct.complete_redirect_checkout(out['payment_id'])
        assert held['needs_attention'] is True
        ((kind, note),) = bell(OWNER, out['payment_id'])
        assert kind == ct.HOLD_NOTICE_TYPE
        assert note == f"{ct.OWNER_HOLD_TITLE}. {owner_cards(env)[-1]['message']}"
        assert out['payment_id'] in note and UID in note and 'Place the order' in note
        assert bell(UID) == []
        assert pushed(env, 'notification')[-1]['title'] == ct.HOLD_TITLE
        # A shopper who is a user of this node reads it in their bell too.
        node_user(UID)
        env['fake'].on('GET', '/cart', body=ORDER)
        env['fake'].on('POST', AP2, status=503, body=None)
        json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        ((kind, note),) = bell(UID, out['payment_id'])
        assert kind == ct.HOLD_NOTICE_TYPE
        assert note == f"{ct.HOLD_TITLE}. {pushed(env, 'notification')[-1]['message']}"

    def test_an_order_call_without_an_answer_is_could_not_confirm(self, env):
        # Sent, and McGroce neither did nor refused it (5xx, twice): the order
        # may exist, so the shopper is never told "not placed yet" and the
        # owner looks at McGroce before refunding.
        out = self._paid_on_phonepe(env)
        env['fake'].on('POST', AP2, status=503, body=None)
        res = ct.complete_redirect_checkout(out['payment_id'])
        assert res['success'] is False and res['needs_attention'] is True
        assert len(env['fake'].called('POST', AP2)) == 2
        assert pushed(env, 'notification')[-1]['message'] == self.UNCONFIRMED
        (card,) = owner_cards(env)
        assert 'Check McGroce for the order before refunding' in card['message']
        assert 'PhonePe merchant account' in card['message']

    def test_an_order_call_that_got_no_answer_at_all_is_could_not_confirm(self, env):
        import requests
        out = self._paid_on_phonepe(env)
        fake = env['fake']

        def dropped(method, url, **kw):
            if method == 'POST' and url.endswith(AP2) and \
                    signing.MANDATE_HEADER in (kw.get('headers') or {}):
                raise requests.Timeout('no answer')
            return fake(method, url, **kw)
        with patch('core.http_pool.pooled_request', side_effect=dropped):
            ct.complete_redirect_checkout(out['payment_id'])
        assert pushed(env, 'notification')[-1]['message'] == self.UNCONFIRMED
        assert 'Check McGroce' in owner_cards(env)[-1]['message']

    def test_a_refused_order_call_is_not_placed_yet(self, env):
        # McGroce answered and refused: nothing was placed, and the owner is
        # not sent to look for an order that cannot exist.
        out = self._paid_on_phonepe(env)
        env['fake'].on('POST', AP2, status=409,
                       body={'messages': [{'messageKey': 'AMOUNT_MISMATCH'}]})
        ct.complete_redirect_checkout(out['payment_id'])
        assert len(env['fake'].called('POST', AP2)) == 1
        assert pushed(env, 'notification')[-1]['message'] == self.PAID
        (card,) = owner_cards(env)
        assert 'Place the order, or refund it' in card['message']
        assert 'Check McGroce' not in card['message']

    def test_the_card_path_holds_when_mcgroce_does_not_place_the_order(self, env):
        # The shopper's approval on a card rail (no gateway callback): the
        # settle takes the money and the order step fails right there.  The
        # same hold, from the same site, with the same sentence.
        env['fake'].on('GET', '/cart', body=ORDER)
        out = json.loads(ct.commerce_prepare_checkout(UID))
        env['mandates'].approve(out['mandate_id'], UID)
        env['fake'].on('POST', AP2, status=503, body=None)
        res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is False and res['needs_attention'] is True
        assert len(env['card'].captures) == 1
        note = pushed(env, 'notification')[-1]
        assert note['title'] == ct.HOLD_TITLE and note['message'] == self.UNCONFIRMED
        (card,) = owner_cards(env)
        assert out['payment_id'] in card['message']
        assert 'Stripe merchant account' in card['message']
        ((kind, _),) = bell(OWNER, out['payment_id'])
        assert kind == ct.HOLD_NOTICE_TYPE

    def test_a_retry_that_cannot_read_the_cart_holds_and_tells(self, env):
        out = self._paid_on_phonepe(env)
        env['fake'].on('POST', AP2, status=503, body=None)
        ct.complete_redirect_checkout(out['payment_id'])          # used up, held
        cards = len(pushed(env, 'notification'))
        env['fake'].on('GET', '/cart', status=503, body=None)
        res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is False and res['needs_attention'] is True
        assert len(pushed(env, 'notification')) == cards + 1
        # No call left this time, but the earlier one did and was never
        # answered: that mark stays with the mandate, so this hold too says
        # "could not confirm" and sends the owner to McGroce first.
        assert len(env['fake'].called('POST', AP2)) == 2
        assert pushed(env, 'notification')[-1]['message'] == self.UNCONFIRMED
        assert len(owner_cards(env)) == 2
        assert 'Check McGroce' in owner_cards(env)[-1]['message']

    def test_an_order_step_that_raises_is_held_as_could_not_confirm(self, env):
        # Anything raised mid-step after the money is taken: the step may
        # have sent the call, so nobody hears "not placed yet", nothing
        # propagates to the callback, and a person checks McGroce.
        out = self._paid_on_phonepe(env)
        with patch.object(ct, 'retry_order', side_effect=RuntimeError('boom')):
            res = ct.complete_redirect_checkout(out['payment_id'])
        assert res['held'] is True and res['needs_attention'] is True
        assert pushed(env, 'notification')[-1]['message'] == self.UNCONFIRMED
        assert 'Check McGroce' in owner_cards(env)[-1]['message']
        assert env['mandates'].get(out['mandate_id']).status == 'consumed'
        # The claim is released: the retry places it once McGroce answers.
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-AFTER'})
        later = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert later['success'] is True and later['order_id'] == 'ORD-AFTER'
        assert len(env['ledger'].payments) == 1

    def test_an_order_placed_but_not_recorded_is_still_an_order(self, env):
        # The key stops reading between the order call and its record: the
        # order IS placed, the shopper gets its tracking, nobody is told it
        # is on hold.  McGroce has checked the cart out by then (the open
        # cart is gone), so the retry cannot compare carts: it asks McGroce
        # by the mandate, with a probe naming the paid cart and no lines
        # (which can place nothing), and McGroce answers with its order.
        from integrations.ap2.ap2_mandate import MandateKeyError
        out = self._paid_on_phonepe(env)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-UNREC'})
        with patch.object(env['mandates'], 'record_order', side_effect=MandateKeyError()):
            res = ct.complete_redirect_checkout(out['payment_id'])
        assert res['success'] is True and res['order_id'] == 'ORD-UNREC'
        assert pushed(env, 'order_tracking')[-1]['order_id'] == 'ORD-UNREC'
        assert pushed(env, 'notification') == [] and owner_cards(env) == []
        assert env['mandates'].get(out['mandate_id']).order_ref is None
        env['fake'].on('GET', '/cart', *CART_GONE)
        later = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert later['success'] is True and later['order_id'] == 'ORD-UNREC'
        first, probe = env['fake'].called('POST', AP2)
        assert attestation_of(first)['lines'] and attestation_of(probe)['lines'] == []
        assert attestation_of(probe)['oid'] == '555'
        assert env['mandates'].get(out['mandate_id']).order_ref == 'ORD-UNREC'
        assert pushed(env, 'notification') == [] and owner_cards(env) == []
        assert bell(OWNER, out['payment_id']) == []

    def test_an_unrecorded_order_is_found_when_the_shopper_has_a_new_cart(self, env):
        # McGroce opened a fresh cart after placing the order: the live cart
        # is another cart, not a changed one, and the probe still finds it.
        from integrations.ap2.ap2_mandate import MandateKeyError
        out = self._paid_on_phonepe(env)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-UNREC'})
        with patch.object(env['mandates'], 'record_order', side_effect=MandateKeyError()):
            ct.complete_redirect_checkout(out['payment_id'])
        env['fake'].on('GET', '/cart', body=dict(ORDER, id=556, orderItems=[]))
        later = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert later['success'] is True and later['order_id'] == 'ORD-UNREC'
        assert env['mandates'].get(out['mandate_id']).order_ref == 'ORD-UNREC'
        assert pushed(env, 'notification') == [] and owner_cards(env) == []

    def test_a_probe_for_an_unused_mandate_places_nothing_and_the_hold_stands(self, env):
        # The cart changed and McGroce never saw the mandate: the probe is
        # refused (it names no lines), so no order exists and the hold says
        # "not placed yet" with the way to get it placed.
        out = self._paid_on_phonepe(env)
        drifted = json.loads(json.dumps(ORDER))
        drifted['orderItems'].pop()
        env['fake'].on('GET', '/cart', body=drifted)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-NEVER'})   # a full call's answer
        res = ct.complete_redirect_checkout(out['payment_id'])
        assert res['success'] is False and res['cart_changed'] is True
        (probe,) = env['fake'].called('POST', AP2)
        assert attestation_of(probe)['lines'] == [] and attestation_of(probe)['oid'] == '555'
        assert env['mandates'].get(out['mandate_id']).order_ref is None
        note = pushed(env, 'notification')[-1]['message']
        assert 'not placed yet' in note and 'put the cart back' in note
        assert 'Place the order, or refund it' in owner_cards(env)[-1]['message']

    def test_an_unanswered_order_call_keeps_its_mark_for_every_later_hold(self, env):
        # 503 twice: the call left and McGroce may have placed it.  The mark
        # is kept on the mandate, signed, so every later hold for it says
        # "could not confirm" and sends the owner to McGroce first, whether
        # that later attempt sent nothing (the cart could not be read) or
        # its probe was refused (the cart changed).
        out = self._paid_on_phonepe(env)
        env['fake'].on('POST', AP2, status=503, body=None)
        ct.complete_redirect_checkout(out['payment_id'])
        assert env['mandates'].get(out['mandate_id']).order_sent is not None
        env['fake'].on('GET', '/cart', status=503, body=None)
        res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['needs_attention'] is True
        assert len(env['fake'].called('POST', AP2)) == 2         # nothing more was sent
        assert pushed(env, 'notification')[-1]['message'] == self.UNCONFIRMED
        assert 'Check McGroce' in owner_cards(env)[-1]['message']
        # McGroce answers again, the cart has moved on: the probe is refused.
        drifted = json.loads(json.dumps(ORDER))
        drifted['orderItems'].pop()
        env['fake'].on('GET', '/cart', body=drifted)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-NEVER'})
        res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['cart_changed'] is True
        assert env['mandates'].get(out['mandate_id']).order_ref is None
        assert pushed(env, 'notification')[-1]['message'].startswith(
            'Your payment of ₹240.00 arrived, but we could not confirm your order, '
            'so it is on hold. Your cart changed after you paid')
        assert 'Check McGroce' in owner_cards(env)[-1]['message']
        # McGroce HAD placed it on the unanswered call: the next probe finds it.
        env['fake'].placed[out['mandate_id']] = 'ORD-LOST'
        later = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert later['success'] is True and later['order_id'] == 'ORD-LOST'
        assert env['mandates'].get(out['mandate_id']).order_ref == 'ORD-LOST'
        assert len(env['ledger'].payments) == 1

    def test_an_unanswered_probe_leaves_no_mark(self, env):
        # A probe can place nothing, so when McGroce does not answer it the
        # shopper still hears "not placed yet", the owner is not sent to look
        # for an order that cannot exist, and no mark is kept.
        out = self._paid_on_phonepe(env)
        drifted = json.loads(json.dumps(ORDER))
        drifted['orderItems'].pop()
        env['fake'].on('GET', '/cart', body=drifted)
        env['fake'].on('POST', AP2, status=503, body=None)
        res = ct.complete_redirect_checkout(out['payment_id'])
        assert res['success'] is False and res['needs_attention'] is True
        assert len(env['fake'].called('POST', AP2)) == 1          # the probe, once
        assert pushed(env, 'notification')[-1]['message'] == self.PAID
        assert 'Check McGroce' not in owner_cards(env)[-1]['message']
        assert env['mandates'].get(out['mandate_id']).order_sent is None

    def test_the_mark_survives_the_breaker_opening_between_the_two_attempts(self, env):
        # The first attempt went unanswered and the breaker opened on it:
        # the second never left.  The hold keeps the first attempt's mark.
        out = self._paid_on_phonepe(env)
        env['fake'].on('POST', AP2, status=503, body=None)
        ct._client().breaker = CircuitBreaker('one', threshold=1)
        ct.complete_redirect_checkout(out['payment_id'])
        assert len(env['fake'].called('POST', AP2)) == 1
        assert pushed(env, 'notification')[-1]['message'] == self.UNCONFIRMED
        assert 'Check McGroce' in owner_cards(env)[-1]['message']
        assert env['mandates'].get(out['mandate_id']).order_sent is not None

    def test_a_held_payment_writes_one_row_per_cause_however_often_it_is_retried(self, env):
        # The agent is told to retry, and every retry that holds again must
        # not grow the bells: one row per person, payment and sentence; the
        # live card may repeat.  A new cause is a new row.
        node_user(UID)
        out = self._paid_on_phonepe(env)
        env['fake'].on('GET', '/cart', status=503, body=None)
        ct.complete_redirect_checkout(out['payment_id'])
        for _ in range(3):
            ct.commerce_checkout(UID, out['mandate_id'])
        assert len(bell(OWNER, out['payment_id'])) == 1
        assert len(bell(UID, out['payment_id'])) == 1
        assert len(owner_cards(env)) == 4
        env['fake'].on('GET', '/cart', body=ORDER)
        env['fake'].on('POST', AP2, status=409,
                       body={'messages': [{'messageKey': 'AMOUNT_MISMATCH'}]})
        ct.commerce_checkout(UID, out['mandate_id'])
        assert len(bell(OWNER, out['payment_id'])) == 2
        assert len(bell(UID, out['payment_id'])) == 1          # the same sentence

    def test_the_card_path_tells_the_hold_when_the_mark_cannot_be_written(self, env):
        # hartos-70's R1 on 05025ce37: the mark's write (a full disk) raised
        # out of the order step BEFORE the hold, with the money taken and
        # nobody told.  The hold comes first; the mark's writer never raises.
        env['fake'].on('GET', '/cart', body=ORDER)
        out = json.loads(ct.commerce_prepare_checkout(UID))
        env['mandates'].approve(out['mandate_id'], UID)
        env['fake'].on('POST', AP2, status=503, body=None)
        with disk_full_once(env['mandates'], 'order_sent'):
            res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is False and res['needs_attention'] is True
        assert len(env['card'].captures) == 1                      # the money was taken
        assert pushed(env, 'notification')[-1]['message'] == self.UNCONFIRMED
        assert 'Check McGroce' in owner_cards(env)[-1]['message']

    def test_the_approval_tap_tells_the_hold_when_the_mark_cannot_be_written(self, env):
        from integrations.ap2.ap2_mandate import decide_payment
        env['fake'].on('GET', '/cart', body=ORDER)
        out = json.loads(ct.commerce_prepare_checkout(UID))
        env['fake'].on('POST', AP2, status=503, body=None)
        with disk_full_once(env['mandates'], 'order_sent'):
            payload, status = decide_payment(out['payment_id'], UID, True,
                                             store=env['mandates'])
        assert 'Errno' not in json.dumps(payload)
        assert len(env['card'].captures) == 1
        assert pushed(env, 'notification')[-1]['message'] == self.UNCONFIRMED
        assert 'Check McGroce' in owner_cards(env)[-1]['message']

    def test_the_retry_tells_the_hold_when_the_mark_cannot_be_written(self, env):
        out = self._paid_on_phonepe(env)
        env['fake'].on('GET', '/cart', status=503, body=None)
        ct.complete_redirect_checkout(out['payment_id'])         # used up, held, nothing sent
        n, c = len(pushed(env, 'notification')), len(owner_cards(env))
        env['fake'].on('GET', '/cart', body=ORDER)
        env['fake'].on('POST', AP2, status=503, body=None)
        with disk_full_once(env['mandates'], 'order_sent'):
            res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert res['success'] is False and res['needs_attention'] is True
        assert len(env['fake'].called('POST', AP2)) == 2         # the call left, twice
        assert [x['message'] for x in pushed(env, 'notification')[n:]] == [self.UNCONFIRMED]
        assert len(owner_cards(env)) == c + 1
        assert 'Check McGroce' in owner_cards(env)[-1]['message']

    def test_the_hold_is_told_before_the_mark_is_written(self, env):
        # Defence in depth behind the never-raising writer: were the mark's
        # writer ever to raise, the hold has already gone out.
        env['fake'].on('GET', '/cart', body=ORDER)
        out = json.loads(ct.commerce_prepare_checkout(UID))
        env['mandates'].approve(out['mandate_id'], UID)
        env['fake'].on('POST', AP2, status=503, body=None)
        with patch.object(ct, '_mark_order_sent', side_effect=RuntimeError('writer broke')):
            with pytest.raises(RuntimeError):
                ct.commerce_checkout(UID, out['mandate_id'])
        assert pushed(env, 'notification')[-1]['message'] == self.UNCONFIRMED
        assert 'Check McGroce' in owner_cards(env)[-1]['message']

    def test_a_step_that_raised_after_its_call_left_keeps_the_mark(self, env):
        # hartos-70's R2: the catch-all hold (sent=True) kept no mark, so a
        # restart forgot that a call had left.  McGroce placed ORD-X, the
        # step raised after the answer came back: held as could-not-confirm,
        # the mark written; after a restart the next hold still says so, and
        # the probe finds the order.
        out = self._paid_on_phonepe(env)
        env['fake'].on('POST', AP2, body={'orderNumber': 'ORD-X'})
        with patch.object(ct, '_order_placed', side_effect=RuntimeError('boom')):
            first = ct.complete_redirect_checkout(out['payment_id'])
        assert first['held'] is True
        assert pushed(env, 'notification')[-1]['message'] == self.UNCONFIRMED
        assert env['fake'].placed.get(out['mandate_id']) == 'ORD-X'
        after = MandateStore(env['mandates'].path, ledger=env['ledger'], key=b'k' * 32)
        rec = after.get(out['mandate_id'])
        assert rec.status == 'consumed' and rec.order_ref is None
        assert rec.order_sent is not None                        # on disk, signed
        n = len(pushed(env, 'notification'))
        env['fake'].on('GET', '/cart', status=503, body=None)    # McGroce not answering
        with patch.object(ct, '_mandates', return_value=after):
            res = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
            assert res['needs_attention'] is True
            assert pushed(env, 'notification')[n]['message'] == self.UNCONFIRMED
            assert 'Check McGroce' in owner_cards(env)[-1]['message']
            env['fake'].on('GET', '/cart', *CART_GONE)           # McGroce back: the probe finds it
            later = json.loads(ct.commerce_checkout(UID, out['mandate_id']))
        assert later['success'] is True and later['order_id'] == 'ORD-X'

    def test_a_changed_cart_keeps_its_recovery_in_the_sentence(self, env):
        out = self._paid_on_phonepe(env)
        drifted = json.loads(json.dumps(ORDER))
        drifted['orderItems'].pop()
        env['fake'].on('GET', '/cart', body=drifted)
        ct.complete_redirect_checkout(out['payment_id'])
        note = pushed(env, 'notification')[-1]
        assert note['message'] == (
            'Your payment of ₹240.00 arrived, but your order is not placed yet, so '
            'it is on hold. Your cart changed after you paid: put the cart back the '
            'way you approved it and ask your agent to check out, and it is placed '
            'with this payment, with no new charge. Nothing more will be charged; '
            'we will sort it out with you.')
        assert len(owner_cards(env)) == 1


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
