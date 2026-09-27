"""McGroce commerce: tools, client, session API and approvals.

Behavioural: the real functions run; McGroce (HTTP), the UI push, the WAMP
publish and the node secret are the mocked boundaries.
"""
import json
from unittest.mock import MagicMock

import pytest
from flask import Flask

from integrations.ap2 import ap2_mandate, ap2_protocol
from integrations.ap2.ap2_protocol import PaymentLedger, PaymentStatus
from integrations.commerce import bindings, commerce_api, commerce_tools
from integrations.commerce.mcgroce_client import McGroceClient, McGroceError

SECRET = 's3cret-shared-with-mcgroce'
ANDROID_TYPES = {'product_card', 'cart', 'checkout', 'payment_status',
                 'order_tracking', 'approval', 'form', 'list', 'navigate',
                 'notification', 'progress', 'agent_action'}


class FakeMcGroce:
    """Stands in for McGroceClient; records every call."""

    def __init__(self):
        self.calls = []
        self.fail = None

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args))
            if self.fail == name:
                raise McGroceError('boom', 500)
            return self.answers[name](*args)
        return call

    answers = {
        'search_products': lambda q, s: {'products': [
            {'id': f'p{i}', 'name': f'Milk {i}', 'price': '45.5'}
            for i in range(5)]},
        'get_cart': lambda u: {'items': [{'id': 'p1', 'name': 'Milk',
                                          'price': 45.5, 'quantity': 2}]},
        'add_to_cart': lambda u, p, q, s: {'items': [
            {'id': p, 'name': 'Milk', 'price': 45.5, 'quantity': q}],
            'total': 45.5 * q},
        'create_order': lambda u, s: {'orderId': 'O-7', 'total': 91.0,
                                      'storeId': 'S1', 'items': [
                                          {'id': 'p1', 'name': 'Milk',
                                           'price': 45.5, 'quantity': 2}]},
        'confirm_order': lambda u, o, ref: {'orderId': o, 'status': 'confirmed'},
        'cancel_order': lambda u, o, r: {'orderId': o, 'status': 'cancelled'},
        'get_order': lambda u, o: {'orderId': o, 'status': 'out_for_delivery'},
        'onboard_merchant': lambda u, d: {'merchantId': 'M-1'},
    }


@pytest.fixture
def world(tmp_path, monkeypatch):
    fake = FakeMcGroce()
    pushes = []
    ledger = PaymentLedger(ledger_path=str(tmp_path / 'ledger.json'))
    monkeypatch.setattr(commerce_tools, '_client', lambda: fake)
    monkeypatch.setattr(bindings, 'push_ui',
                        lambda uid, card: pushes.append((uid, card)) or True)
    monkeypatch.setattr(ap2_mandate, '_push',
                        lambda card, uid: pushes.append((uid, card)) or True)
    monkeypatch.setattr(ap2_mandate, '_node_key', lambda: b'n' * 32)
    monkeypatch.setattr(ap2_protocol, 'payment_ledger', ledger)
    monkeypatch.setattr(commerce_tools, '_onboarding',
                        bindings.JsonState('merchant_onboarding', str(tmp_path)))
    monkeypatch.setenv('COMMERCE_SESSION_SECRET', SECRET)
    return {'mcgroce': fake, 'pushes': pushes, 'ledger': ledger}


def _types(pushes):
    return [card['type'] for _, card in pushes]


# ─── Cards ─────────────────────────────────────────────────────────

def test_cards_use_the_android_prop_names_in_inr():
    card = bindings.product_card({'id': 'p1', 'name': 'Milk', 'price': '45.5'})
    assert card['name'] == 'Milk' and card['price'] == 45.5
    assert card['currency'] == 'INR'
    cart = bindings.cart_card({'items': [{'id': 'p1', 'quantity': 2}],
                               'total': 91})
    assert cart['items'][0]['product_id'] == 'p1'
    assert (cart['total'], cart['currency']) == (91.0, 'INR')
    assert bindings.order_tracking_card({'order_id': 7, 'status': 's'})[
        'order_id'] == '7'


def test_a_type_the_phone_cannot_render_is_refused():
    with pytest.raises(ValueError):
        bindings.component('comparison', apps=[])


def test_mcgroce_identity_is_stable_and_file_safe():
    assert bindings.mcgroce_identity('1234') == 'mcg-1234'
    hashed = bindings.mcgroce_identity('a.b@shop.in')
    assert hashed == bindings.mcgroce_identity('a.b@shop.in')
    assert hashed.startswith('mcg-') and '@' not in hashed and '.' not in hashed
    assert bindings.mcgroce_identity('') is None


# ─── Push path ─────────────────────────────────────────────────────

def test_push_reaches_the_shell_and_the_users_own_stream(monkeypatch):
    from integrations.agent_engine import liquid_ui_service as lui
    shell = MagicMock()
    shell.agent_ui_update.return_value = True
    registry = MagicMock()
    registry.get_or_none.return_value = shell
    published = []
    monkeypatch.setattr('core.platform.registry.get_registry', lambda: registry)
    monkeypatch.setattr('integrations.social.realtime.publish_event',
                        lambda t, d, user_id='': published.append((t, d, user_id)))

    card = bindings.cart_card({'items': [], 'total': 0})
    assert lui.push_agent_ui('mcgroce_commerce', card, user_id='mcg-1')

    shell.agent_ui_update.assert_called_once()
    assert shell.agent_ui_update.call_args.kwargs['user_id'] == 'mcg-1'
    topic, data, uid = published[0]
    assert (topic, uid) == ('chat.social', 'mcg-1')
    assert data['type'] == 'agent_ui_update'
    assert data['component']['type'] == 'cart'


def test_push_without_a_shell_still_gates_the_card(monkeypatch):
    from integrations.agent_engine import liquid_ui_service as lui
    registry = MagicMock()
    registry.get_or_none.return_value = None
    published = []
    monkeypatch.setattr('core.platform.registry.get_registry', lambda: registry)
    monkeypatch.setattr('integrations.social.realtime.publish_event',
                        lambda t, d, user_id='': published.append(t))
    assert not lui.push_agent_ui('a', {'type': 'bogus'}, user_id='u')
    assert not lui.push_agent_ui(
        'a', {'type': 'notification', 'message': '<script>x</script>'},
        user_id='u')
    assert published == []
    assert lui.push_agent_ui('a', {'type': 'notification', 'message': 'hi'},
                             user_id='u')
    assert published == ['chat.social']


def test_agent_ui_update_routes_to_the_named_user(monkeypatch):
    from integrations.agent_engine import liquid_ui_service as lui
    emitted = []
    monkeypatch.setattr('core.platform.events.emit_event',
                        lambda topic, data=None, **k: emitted.append(data))
    svc = lui.LiquidUIService.__new__(lui.LiquidUIService)
    svc.a2ui_enabled = True
    svc._custom_component_types = {}
    svc._agent_components = {}
    svc._a2ui_buckets = {}
    import threading
    svc._lock = threading.Lock()
    svc._ui_event_cv = threading.Condition()
    assert svc.agent_ui_update('mcgroce_commerce', {'type': 'cart'},
                               user_id='mcg-9')
    assert emitted[-1]['user_id'] == 'mcg-9'


# ─── McGroce client transport ──────────────────────────────────────

def test_client_calls_mcgroce_through_the_pool(monkeypatch):
    seen = {}

    def fake_request(method, url, timeout=None, **kw):
        seen.update(method=method, url=url, timeout=timeout, **kw)
        resp = MagicMock(status_code=200, content=b'{}')
        resp.json.return_value = {'items': []}
        return resp
    monkeypatch.setattr('core.http_pool.pooled_request', fake_request)

    client = McGroceClient('https://shop.example/api/v1/', SECRET)
    client.add_to_cart('mcg-1', 'p/1', 2)

    assert seen['method'] == 'POST'
    assert seen['url'] == 'https://shop.example/api/v1/agent/cart/items'
    assert seen['headers']['X-Commerce-Secret'] == SECRET
    assert seen['headers']['X-Commerce-Customer'] == 'mcg-1'
    assert seen['json'] == {'productId': 'p/1', 'quantity': 2}
    assert seen['timeout'] is not None
    client.remove_from_cart('mcg-1', 'p/1')
    assert seen['url'].endswith('/agent/cart/items/p%2F1')


def test_client_errors_are_mcgroce_errors(monkeypatch):
    resp = MagicMock(status_code=404, content=b'x')
    resp.json.return_value = {'message': 'no such order'}
    monkeypatch.setattr('core.http_pool.pooled_request',
                        lambda *a, **k: resp)
    with pytest.raises(McGroceError, match='no such order'):
        McGroceClient('https://shop.example', '').get_order('u', 'O-1')
    with pytest.raises(McGroceError, match='not configured'):
        McGroceClient('', '').get_cart('u')


# ─── Shopper flow ──────────────────────────────────────────────────

def test_search_shows_at_most_three_product_cards(world):
    out = json.loads(commerce_tools.search_products('mcg-1', 'milk'))
    assert out['success'] and out['count'] == 5
    assert _types(world['pushes']) == ['product_card'] * 3


def test_checkout_asks_the_shopper_and_does_not_pay(world):
    out = json.loads(commerce_tools.checkout('mcg-1'))
    assert out['status'] == 'approval_required' and out['currency'] == 'INR'
    payment = world['ledger'].get_payment(out['payment_id'])
    assert payment.status == PaymentStatus.APPROVAL_REQUIRED
    assert str(payment.amount) == '91.0' and payment.currency == 'INR'
    assert _types(world['pushes']) == ['checkout', 'approval']
    assert world['pushes'][-1][0] == 'mcg-1'
    assert 'confirm_order' not in [c for c, _ in world['mcgroce'].calls]


def test_shopper_approval_pays_and_confirms_the_order(world):
    pid = json.loads(commerce_tools.checkout('mcg-1'))['payment_id']
    payload, status = ap2_mandate.decide_payment(pid, 'mcg-1', True)
    assert status == 200
    assert world['ledger'].get_payment(pid).status == PaymentStatus.COMPLETED
    assert ('confirm_order', ('mcg-1', 'O-7',
                              world['ledger'].get_payment(pid)
                              .gateway_transaction_id)) in world['mcgroce'].calls
    assert _types(world['pushes'])[-2:] == ['payment_status', 'order_tracking']
    assert payload['hooks'][0]['order_status'] == 'confirmed'


def test_shopper_denial_cancels_the_order(world):
    pid = json.loads(commerce_tools.checkout('mcg-1'))['payment_id']
    _, status = ap2_mandate.decide_payment(pid, 'mcg-1', False)
    assert status == 200
    assert world['ledger'].get_payment(pid).status == PaymentStatus.CANCELLED
    assert [c for c, _ in world['mcgroce'].calls][-1] == 'cancel_order'


def test_a_mcgroce_failure_is_reported_not_raised(world):
    world['mcgroce'].fail = 'create_order'
    out = json.loads(commerce_tools.checkout('mcg-1'))
    assert out['success'] is False
    assert world['pushes'][-1][1]['type'] == 'notification'
    assert world['ledger'].list_payments() == []


# ─── Merchant onboarding ───────────────────────────────────────────

def test_merchant_onboarding_waits_for_the_merchant(world):
    out = json.loads(commerce_tools.request_merchant_onboarding(
        'mcg-m', 'Anna Stores', '1 Main St', '99999'))
    rid = out['request_id']
    card = world['pushes'][-1][1]
    assert card['type'] == 'approval'
    assert commerce_tools.parse_merchant_action(card['action']) == rid
    assert not any(c == 'onboard_merchant' for c, _ in world['mcgroce'].calls)

    assert commerce_tools.decide_merchant_onboarding(rid, 'mcg-x', True)[1] == 403
    payload, status = commerce_tools.decide_merchant_onboarding(rid, 'mcg-m', True)
    assert status == 200 and payload['merchant'] == {'merchantId': 'M-1'}
    assert commerce_tools.decide_merchant_onboarding(rid, 'mcg-m', True)[1] == 409


# ─── Agent registration ────────────────────────────────────────────

def test_tools_bind_the_turns_user_and_hide_it_from_the_model(world):
    helper, assistant, executor = MagicMock(), MagicMock(), MagicMock()
    registered = {}
    helper.register_for_llm.side_effect = \
        lambda name, description: (lambda f: registered.setdefault(name, f))
    n = commerce_tools.register_commerce_tools(helper, assistant, 'mcg-1',
                                               executor=executor)
    assert n == len(commerce_tools.COMMERCE_TOOLS)
    from autogen.function_utils import get_function_schema
    schema = get_function_schema(registered['add_to_cart'], name='add_to_cart',
                                 description='d')
    params = schema['function']['parameters']
    assert 'user_id' not in params['properties']
    assert params['required'] == ['product_id']
    json.loads(registered['add_to_cart']('p1', 2))
    assert world['mcgroce'].calls[-1] == ('add_to_cart', ('mcg-1', 'p1', 2, None))


# ─── Session API + approvals ───────────────────────────────────────

@pytest.fixture
def client(world):
    app = Flask(__name__)
    app.register_blueprint(commerce_api.commerce_bp)
    return app.test_client()


def test_session_contract_token_and_expires_in(client):
    r = client.post('/api/commerce/session', json={'customerId': '1234'},
                    headers={'X-Commerce-Secret': SECRET})
    assert r.status_code == 200
    body = r.get_json()
    assert set(body) == {'token', 'expiresIn'} and body['expiresIn'] > 0
    assert commerce_api.verify_session_token(body['token']) == 'mcg-1234'


def test_session_refuses_a_wrong_or_missing_secret(client, monkeypatch):
    assert client.post('/api/commerce/session', json={'customerId': '1'},
                       headers={'X-Commerce-Secret': 'nope'}).status_code == 403
    assert client.post('/api/commerce/session',
                       json={'customerId': '1'}).status_code == 403
    assert client.post('/api/commerce/session', json={},
                       headers={'X-Commerce-Secret': SECRET}).status_code == 400
    monkeypatch.setattr(bindings, 'commerce_session_secret', lambda: '')
    assert client.post('/api/commerce/session', json={'customerId': '1'},
                       headers={'X-Commerce-Secret': ''}).status_code == 503


def test_a_token_from_another_secret_is_not_a_session():
    token = commerce_api.issue_session_token('1', secret='other')['token']
    assert commerce_api.verify_session_token(token, secret=SECRET) is None


def test_embed_routes_act_as_the_token_holder(client, world):
    token = commerce_api.issue_session_token('77')['token']
    assert client.get('/api/commerce/cart').status_code == 401
    r = client.get('/api/commerce/cart',
                   headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 200
    assert world['mcgroce'].calls[-1] == ('get_cart', ('mcg-77',))
    s = client.get('/api/commerce/stream',
                   headers={'Authorization': f'Bearer {token}'}).get_json()
    assert s['wamp_topic'] == 'com.hertzai.hevolve.social.mcg-77'


def test_the_approver_is_the_token_not_the_body(world):
    pid = json.loads(commerce_tools.checkout('mcg-1'))['payment_id']
    app = Flask(__name__)
    intruder = commerce_api.issue_session_token('2')['token']
    with app.test_request_context(
            json={'user_id': 'mcg-1'},
            headers={'Authorization': f'Bearer {intruder}'}):
        payload, status = commerce_api.handle_commerce_approval(
            f'ap2_pay:{pid}', True)
    assert status == 403
    owner = commerce_api.issue_session_token('1')['token']
    with app.test_request_context(
            headers={'Authorization': f'Bearer {owner}'}):
        payload, status = commerce_api.handle_commerce_approval(
            f'ap2_pay:{pid}', True)
    assert status == 200 and payload['status'] == 'approved'
    assert world['ledger'].get_payment(pid).status == PaymentStatus.COMPLETED


def test_no_token_no_approval(world):
    pid = json.loads(commerce_tools.checkout('mcg-1'))['payment_id']
    with Flask(__name__).test_request_context():
        _, status = commerce_api.handle_commerce_approval(f'ap2_pay:{pid}', True)
    assert status == 401
