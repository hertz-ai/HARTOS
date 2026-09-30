"""How the McGroce commerce tools reach an agent.

Behavioural: the 'commerce' goal tag (detect_goal_tags) fires on McGroce work
and nowhere else in the in-repo goal corpus; the mcgroce_shopper /
mcgroce_merchant goal types exist with their tool tags and their prompts
name only tools the registrar really provides.

Source guards (clearly labelled): create_recipe / reuse_recipe run the Tier-2
dispatch inside multi-thousand-line agent builders that cannot be driven in a
unit test, so an AST check pins that each leg calls register_commerce_tools
under ``if 'commerce' in goal_tags``.  The registrar itself is covered
behaviourally in test_commerce_tools.py.
"""
import ast
import os
import re

import pytest

from integrations.agent_engine import goal_manager as gm
from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
from integrations.agent_engine.marketing_tools import detect_goal_tags
from integrations.commerce.commerce_tools import COMMERCE_TOOLS

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _corpus():
    docs = {}
    for s in SEED_BOOTSTRAP_GOALS:
        docs['seed:' + s.get('slug', '')] = ' '.join(
            str(s.get(k, '')) for k in ('title', 'description'))
    for t in gm.get_registered_types():
        try:
            docs['type:' + t] = str(gm.get_prompt_builder(t)(
                {'title': t, 'description': '', 'goal_type': t, 'config': {}}) or '')
        except Exception:
            continue
    return docs


class TestCommerceTagPrecision:
    def test_fires_only_on_mcgroce_work_across_the_goal_corpus(self):
        hits = {n for n, text in _corpus().items()
                if 'commerce' in detect_goal_tags(text)}
        assert hits == {'seed:bootstrap_p2p_grocery', 'type:p2p_grocery',
                        'type:mcgroce_shopper', 'type:mcgroce_merchant'}

    @pytest.mark.parametrize('text', [
        'git checkout main and rebase', 'checkout the release branch',
        'send an order confirmation email', 'plan a rideshare pool',
    ])
    def test_does_not_fire_on_lookalikes(self, text):
        assert 'commerce' not in detect_goal_tags(text)

    @pytest.mark.parametrize('text', [
        '[mcgroce_ctx]{"page":"home"}\nadd 2 milk',
        'please add to cart the paneer', 'what is my order status',
        'help me onboard merchant stores', 'create a new sku for ghee',
        'place a grocery order', 'proceed to checkout',
    ])
    def test_fires_on_commerce_requests(self, text):
        assert 'commerce' in detect_goal_tags(text)


class TestGoalTypes:
    def test_types_and_tool_tags(self):
        assert gm.get_tool_tags('mcgroce_shopper') == ['commerce']
        assert gm.get_tool_tags('mcgroce_merchant') == ['commerce', 'marketing']
        assert 'commerce' in gm.get_tool_tags('p2p_grocery')

    def test_merchant_prompt_attaches_marketing_but_not_outreach(self):
        text = gm.get_prompt_builder('mcgroce_merchant')({})
        tags = detect_goal_tags(text)
        assert {'commerce', 'marketing'} <= set(tags)
        assert 'outreach' not in tags

    @pytest.mark.parametrize('goal_type', ['mcgroce_shopper', 'mcgroce_merchant',
                                           'p2p_grocery'])
    def test_prompts_name_only_real_commerce_tools(self, goal_type):
        text = gm.get_prompt_builder(goal_type)({'config': {}})
        named = set(re.findall(r'\bcommerce_[a-z_]+', text))
        assert named, goal_type
        assert named <= {t['name'] for t in COMMERCE_TOOLS}

    def test_grocery_full_search_points_at_the_live_endpoint(self):
        text = gm.get_prompt_builder('p2p_grocery')({'config': {}})
        assert '/catalog/search?q=' in text
        assert '/search/{query} — full search' not in text


def _commerce_branch_calls(path):
    tree = ast.parse(open(os.path.join(REPO, path), encoding='utf-8').read())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        t = node.test
        if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Constant)
                and t.left.value == 'commerce'
                and isinstance(t.ops[0], ast.In)
                and isinstance(t.comparators[0], ast.Name)
                and t.comparators[0].id == 'goal_tags'):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                        and sub.func.id == 'register_commerce_tools'):
                    found.append(sub)
    return found


@pytest.mark.parametrize('leg', ['hartos/create_recipe.py', 'hartos/reuse_recipe.py'])
def test_source_guard_tier2_dispatches_commerce(leg):
    calls = _commerce_branch_calls(leg)
    assert len(calls) == 1, f'{leg}: expected one gated register_commerce_tools call'
    args = [a.id for a in calls[0].args if isinstance(a, ast.Name)]
    assert args == ['helper', 'assistant', 'user_id']


# ── Delivery: push_agent_ui (ported from the parallel WP-H branch) ──

def test_push_reaches_the_shell_and_the_users_own_stream(monkeypatch):
    from unittest.mock import MagicMock
    from integrations.agent_engine import liquid_ui_service as lui
    shell = MagicMock()
    shell.agent_ui_update.return_value = True
    registry = MagicMock()
    registry.get_or_none.return_value = shell
    published = []
    monkeypatch.setattr('core.platform.registry.get_registry', lambda: registry)
    monkeypatch.setattr('integrations.social.realtime.publish_event',
                        lambda t, d, user_id='': published.append((t, d, user_id)))
    card = {'type': 'cart', 'items': [], 'total': 0, 'currency': 'INR'}
    assert lui.push_agent_ui('mcgroce', card, user_id='mcg-1')
    assert shell.agent_ui_update.call_args.kwargs['user_id'] == 'mcg-1'
    topic, data, uid = published[0]
    assert (topic, uid) == ('chat.social', 'mcg-1')
    assert data['type'] == 'agent_ui_update' and data['component']['type'] == 'cart'


def test_push_without_a_shell_still_gates_the_card(monkeypatch):
    from unittest.mock import MagicMock
    from integrations.agent_engine import liquid_ui_service as lui
    registry = MagicMock()
    registry.get_or_none.return_value = None
    published = []
    monkeypatch.setattr('core.platform.registry.get_registry', lambda: registry)
    monkeypatch.setattr('integrations.social.realtime.publish_event',
                        lambda t, d, user_id='': published.append(t))
    assert not lui.push_agent_ui('a', {'type': 'bogus'}, user_id='u')
    assert not lui.push_agent_ui(
        'a', {'type': 'notification', 'message': '<script>x</script>'}, user_id='u')
    assert published == []
    assert lui.push_agent_ui('a', {'type': 'notification', 'message': 'hi'}, user_id='u')
    assert published == ['chat.social']


# ── The shell's own /api/agent/approval decides commerce cards ──

@pytest.fixture
def shell_world(monkeypatch, tmp_path):
    from integrations.agent_engine.liquid_ui_service import LiquidUIService
    from integrations.ap2.ap2_mandate import MandateStore
    from integrations.ap2.ap2_protocol import PaymentLedger
    monkeypatch.setattr(LiquidUIService, '_register_self', lambda self: None)
    monkeypatch.setattr('integrations.agent_engine.liquid_ui_service.push_agent_ui',
                        lambda *a, **k: True)
    ledger = PaymentLedger(ledger_path=str(tmp_path / 'l.json'))
    store = MandateStore(str(tmp_path / 'm.json'), ledger=ledger, key=b'k' * 32)
    monkeypatch.setattr('integrations.ap2.ap2_mandate.get_mandate_store', lambda: store)
    client = LiquidUIService()._create_flask_app().test_client()
    cart = {'lines': [{'sku_id': 1, 'qty': 1, 'unit_price': 50}],
            'total': 50, 'currency': 'INR'}
    m = store.create_cart_mandate('mcg-1', 'mcgroce', cart)
    return client, ledger, m


def _bearer(uid):
    from integrations.social.auth import generate_jwt
    return {'Authorization': 'Bearer ' + generate_jwt(uid, uid, tenant_id='mcgroce')}


def test_shell_approval_decides_the_payment_as_the_token_holder(shell_world):
    from integrations.ap2.ap2_protocol import PaymentStatus
    client, ledger, m = shell_world
    body = {'agent_id': 'mcgroce', 'action': f'ap2_pay:{m.payment_id}',
            'decision': 'approve'}
    assert client.post('/api/agent/approval', json=body,
                       environ_base={'REMOTE_ADDR': '203.0.113.9'}).status_code == 401
    assert client.post('/api/agent/approval', json=body,
                       headers=_bearer('mcg-2')).status_code == 403
    r = client.post('/api/agent/approval', json=body, headers=_bearer('mcg-1'))
    assert r.status_code == 200 and r.get_json()['status'] == 'approved'
    assert ledger.get_payment(m.payment_id).status == PaymentStatus.COMPLETED


def test_shell_local_tokenless_answer_is_the_signed_in_owner(shell_world, monkeypatch):
    from integrations.ap2.ap2_protocol import PaymentStatus
    client, ledger, m = shell_world
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'mcg-1')
    r = client.post('/api/agent/approval', json={
        'agent_id': 'mcgroce', 'action': f'ap2_pay:{m.payment_id}',
        'decision': 'approve'})
    assert r.status_code == 200 and r.get_json()['status'] == 'approved'
    assert ledger.get_payment(m.payment_id).status == PaymentStatus.COMPLETED


def test_shell_later_leaves_the_payment_waiting(shell_world):
    from integrations.ap2.ap2_protocol import PaymentStatus
    client, ledger, m = shell_world
    r = client.post('/api/agent/approval', json={
        'agent_id': 'mcgroce', 'action': f'ap2_pay:{m.payment_id}',
        'decision': 'later'}, headers=_bearer('mcg-1'))
    assert r.status_code == 200
    assert ledger.get_payment(m.payment_id).status == PaymentStatus.APPROVAL_REQUIRED


# ── The emitted props are what the three renderers read ──

def _emitted(monkeypatch, card):
    from unittest.mock import MagicMock
    from integrations.agent_engine import liquid_ui_service as lui
    shell = MagicMock()
    shell.agent_ui_update.return_value = True
    registry = MagicMock()
    registry.get_or_none.return_value = shell
    monkeypatch.setattr('core.platform.registry.get_registry', lambda: registry)
    assert lui.push_agent_ui('mcgroce', card)
    return [c.args[1] for c in shell.agent_ui_update.call_args_list]


@pytest.mark.parametrize('sent,shown', [
    ('completed', 'success'), ('processing', 'pending'),
    ('failed', 'error'), ('cancelled', 'error')])
def test_payment_status_uses_the_renderers_vocabulary(monkeypatch, sent, shown):
    got = _emitted(monkeypatch, {'type': 'payment_status', 'status': sent,
                                 'amount': 5, 'method': 'mock'})
    assert got[-1]['status'] == shown


def test_payment_redirect_becomes_the_cards_action_link(monkeypatch):
    got = _emitted(monkeypatch, {
        'type': 'payment_status', 'status': 'processing', 'method': 'phonepe',
        'redirect_url': 'https://pay.example/x'})
    status = [c for c in got if c['type'] == 'payment_status'][0]
    link = [c for c in got if c['type'] == 'oauth_link'][0]
    assert 'redirect_url' not in status
    assert link['authorize_url'] == 'https://pay.example/x'


def test_order_tracking_steps_carry_every_clients_progress_marker(monkeypatch):
    got = _emitted(monkeypatch, {
        'type': 'order_tracking', 'order_id': 9, 'status': 'SUBMITTED',
        'steps': [{'label': 'a', 'done': True}, {'label': 'b', 'done': True},
                  {'label': 'c', 'done': False}]})[-1]
    assert [s['completed'] for s in got['steps']] == [True, True, False]
    assert got['current_step'] == 2
