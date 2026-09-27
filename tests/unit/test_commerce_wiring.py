"""McGroce commerce wiring: goal types, tag detection, MCP bridge, the shell's
approval route, and the McGroce CORS origins.  Behavioural: real functions,
real Flask apps, mocked McGroce/push/secret boundaries."""
import json

import pytest
from flask import Flask, jsonify

from integrations.ap2 import ap2_mandate, ap2_protocol
from integrations.ap2.ap2_protocol import PaymentLedger, PaymentStatus
from integrations.commerce import bindings, commerce_api, commerce_tools


class _Cart:
    def __init__(self):
        self.calls = []

    def get_cart(self, user):
        self.calls.append(('get_cart', user))
        return {'items': [], 'total': 0}

    def create_order(self, user, store):
        return {'orderId': 'O-1', 'total': 50, 'items': []}

    def confirm_order(self, user, order_id, ref):
        return {'orderId': order_id, 'status': 'confirmed'}


@pytest.fixture
def world(tmp_path, monkeypatch):
    fake = _Cart()
    ledger = PaymentLedger(ledger_path=str(tmp_path / 'ledger.json'))
    monkeypatch.setattr(commerce_tools, '_client', lambda: fake)
    monkeypatch.setattr(bindings, 'push_ui', lambda uid, card: True)
    monkeypatch.setattr(ap2_mandate, '_push', lambda card, uid: True)
    monkeypatch.setattr(ap2_mandate, '_node_key', lambda: b'n' * 32)
    monkeypatch.setattr(ap2_protocol, 'payment_ledger', ledger)
    monkeypatch.setenv('COMMERCE_SESSION_SECRET', 'shared')
    return {'mcgroce': fake, 'ledger': ledger}


# ─── Goal types + tags ─────────────────────────────────────────────

@pytest.mark.parametrize('goal_type', ['mcgroce_shopper', 'mcgroce_merchant'])
def test_goal_type_prompt_unlocks_the_commerce_tools(goal_type):
    from integrations.agent_engine.goal_manager import (
        get_prompt_builder, get_tool_tags)
    from integrations.agent_engine.marketing_tools import detect_goal_tags
    prompt = get_prompt_builder(goal_type)({'title': 'Weekly shop',
                                            'description': 'milk, eggs'})
    assert 'commerce' in detect_goal_tags(prompt)
    assert get_tool_tags(goal_type) == ['commerce']
    assert 'never approve' in prompt.lower() or 'cannot approve' in prompt.lower()


@pytest.mark.parametrize('text,tagged', [
    ('order groceries for tonight', True),
    ('add to cart two litres of milk', True),
    ('help me list my store on the app', True),
    ('git checkout the release branch', False),
    ('write a poem about shopping', False),
])
def test_commerce_tag_detection(text, tagged):
    from integrations.agent_engine.marketing_tools import detect_goal_tags
    assert ('commerce' in detect_goal_tags(text)) is tagged


# ─── MCP bridge ────────────────────────────────────────────────────

@pytest.fixture
def mcp_fresh():
    from integrations.mcp import mcp_http_bridge
    mcp_http_bridge._tools_loaded = False
    mcp_http_bridge._local_tools.clear()
    yield mcp_http_bridge
    mcp_http_bridge._tools_loaded = False
    mcp_http_bridge._local_tools.clear()


def test_mcp_bridge_exposes_and_runs_commerce_tools(mcp_fresh, world):
    mcp_fresh._load_tools()
    names = {t['name'] for t in mcp_fresh._local_tools}
    assert {t['name'] for t in commerce_tools.COMMERCE_TOOLS} <= names
    payload, status = mcp_fresh._invoke_tool('view_cart', {'user_id': 'mcg-5'})
    assert status == 200 and payload['result']['success'] is True
    assert world['mcgroce'].calls == [('get_cart', 'mcg-5')]


# ─── Shell approval route delegates commerce decisions ─────────────

@pytest.fixture
def shell(monkeypatch):
    from integrations.agent_engine.liquid_ui_service import LiquidUIService
    monkeypatch.setattr(LiquidUIService, '_register_self', lambda self: None)
    return LiquidUIService()._create_flask_app().test_client()


def test_shell_approval_decides_the_payment_as_the_token_holder(shell, world):
    pid = json.loads(commerce_tools.checkout('mcg-1'))['payment_id']
    body = {'agent_id': 'ap2_payments', 'action': f'ap2_pay:{pid}',
            'decision': 'approve'}
    assert shell.post('/api/agent/approval', json=body).status_code == 401
    other = commerce_api.issue_session_token('2')['token']
    r = shell.post('/api/agent/approval', json=body,
                   headers={'Authorization': f'Bearer {other}'})
    assert r.status_code == 403
    owner = commerce_api.issue_session_token('1')['token']
    r = shell.post('/api/agent/approval', json=body,
                   headers={'Authorization': f'Bearer {owner}'})
    assert r.status_code == 200 and r.get_json()['status'] == 'approved'
    assert world['ledger'].get_payment(pid).status == PaymentStatus.COMPLETED


def test_shell_later_leaves_the_payment_waiting(shell, world):
    pid = json.loads(commerce_tools.checkout('mcg-1'))['payment_id']
    r = shell.post('/api/agent/approval', json={
        'agent_id': 'ap2_payments', 'action': f'ap2_pay:{pid}',
        'decision': 'later'})
    assert r.status_code == 200
    assert (world['ledger'].get_payment(pid).status
            == PaymentStatus.APPROVAL_REQUIRED)


# ─── CORS: McGroce origins join the one allowlist ─────────────────

@pytest.fixture
def cors_client(monkeypatch):
    def make(**env):
        for k in ('CORS_ORIGINS', 'MCGROCE_ORIGINS'):
            monkeypatch.setenv(k, env.get(k, ''))
        monkeypatch.setenv('HEVOLVE_ENV', 'development')
        app = Flask(__name__)
        from security.middleware import apply_security_middleware
        apply_security_middleware(app)

        @app.route('/status')
        def status():
            return jsonify({'ok': True})
        return app.test_client()
    return make


def test_mcgroce_origins_are_allowed_alongside_cors_origins(cors_client):
    c = cors_client(CORS_ORIGINS='https://hart.ai',
                    MCGROCE_ORIGINS='https://shop.mcgroce.in, https://m.mcgroce.in')
    for origin in ('https://hart.ai', 'https://shop.mcgroce.in',
                   'https://m.mcgroce.in'):
        r = c.get('/status', headers={'Origin': origin})
        assert r.headers.get('Access-Control-Allow-Origin') == origin
    pre = c.options('/status', headers={'Origin': 'https://shop.mcgroce.in'})
    assert pre.headers.get('Access-Control-Allow-Origin') == 'https://shop.mcgroce.in'
    r = c.get('/status', headers={'Origin': 'https://evil.in'})
    assert 'Access-Control-Allow-Origin' not in r.headers


def test_mcgroce_origins_alone_still_fail_closed_for_others(cors_client):
    c = cors_client(MCGROCE_ORIGINS='https://shop.mcgroce.in')
    assert c.get('/status', headers={'Origin': 'https://shop.mcgroce.in'}) \
        .headers.get('Access-Control-Allow-Origin') == 'https://shop.mcgroce.in'
    assert 'Access-Control-Allow-Origin' not in c.get(
        '/status', headers={'Origin': 'https://hart.ai'}).headers
