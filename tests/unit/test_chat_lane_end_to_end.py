"""The lane a /chat turn is registered on, driven through the REAL handler.

Owner policy 2026-10-12: a paying customer's metered call and a McGroce
shopper's turn run on the revenue lane (the expert model may serve them,
nothing throttles them); a person's turn runs on the user lane; the node's
own daemon turn keeps whatever lane dispatch_goal gave it.  The pure rule is
core.request_lane; the review of 7086adff8 found the three real /chat paths
it got wrong were all paths no test drove:

  * the node's own daemon turn was registered 'user' over the goal's lane;
  * a paying tier never reached revenue: on a keyless node the self-POST
    carries the node's system JWT (not the key), and on a keyed node /chat
    refused the X-API-Key header the middleware had already accepted;
  * a stranger whose request named a revenue goal's id inherited its lane.

Each test here POSTs to hart_intelligence_entry.chat and stops the turn at
the statement after the lane registration (the speculative dispatcher is
asked for there; the stub raises), then reads the registry the wire reads.
"""
import os
import sys
import tempfile

os.environ['HEVOLVE_DB_PATH'] = ':memory:'
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core import llm_outbound_logger as lol  # noqa: E402

KEY = 'node-key-for-the-lane-test'


class _Stop(Exception):
    """Raised where /chat asks for the speculative dispatcher: the lane was
    registered one statement earlier; nothing past this point is under test."""


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


@pytest.fixture
def lanes():
    with lol._request_lanes_lock:
        lol._request_lanes.clear()
    yield lol
    with lol._request_lanes_lock:
        lol._request_lanes.clear()


def _chat(hie, monkeypatch, body, headers):
    """POST ``body`` to the real /chat.  Returns 'registered' when the turn
    got past the lane registration, else the HTTP status it was refused with.

    Two stop points, both after the registration: an agent-bound turn asks
    for the speculative dispatcher; an agent-less turn with the draft off
    (the metered self-POST since the review of e0abe7c34 sends prompt_id
    None and draft_first False) reaches get_ans instead.  Without the second
    stop that turn ran to the end of /chat and published to the desktop's
    bus from a unit test."""
    def stop(*_a, **_k):
        raise _Stop()
    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    monkeypatch.setattr(hie, '_persist_language', lambda *a, **k: None)
    monkeypatch.setattr(hie, '_cleanup_stale_agents', lambda: None)
    monkeypatch.setattr(hie, 'get_ans', stop)
    # The INITIALIZED publish sits just before get_ans; a unit test must not
    # hand it to the desktop's bus (review of 7e7ce19bc: 2 publishes here).
    monkeypatch.setattr(hie, 'publish_async', lambda *a, **k: None)
    body = dict(body, speculative=True)
    body.setdefault('prompt_id', '7')
    with patch('integrations.agent_engine.speculative_dispatcher.get_speculative_dispatcher',
               stop):
        try:
            return hie.app.test_client().post('/chat', json=body, headers=headers).status_code
        except _Stop:
            return 'registered'


def _metered_call(hie, monkeypatch, user_id='cust-42'):
    """Drive the real metered endpoint (its auth stubbed; the key belongs to
    ``user_id``); its self-POST is forwarded to the real /chat with the body
    and headers it really sends.  Returns (the endpoint's response, what was
    sent, /chat's outcome)."""
    from integrations.agent_engine.commercial_api import (
        CommercialAPIService, commercial_api_bp)
    app = Flask(__name__)
    app.register_blueprint(commercial_api_bp)
    sent = {}

    def forward(url, json=None, headers=None, timeout=None, **_):
        sent['json'], sent['headers'] = json, dict(headers or {})
        sent['chat'] = _chat(hie, monkeypatch, json, headers or {})
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {'response': 'ok'}
        return r

    key_data = {'id': 'key-1', 'user_id': user_id, 'tier': 'pro'}
    with patch('integrations.social.models.get_db', return_value=MagicMock()), \
            patch.object(CommercialAPIService, 'validate_api_key', return_value=key_data), \
            patch.object(CommercialAPIService, 'check_rate_limit', return_value=True), \
            patch.object(CommercialAPIService, 'reserve_quota', return_value=True), \
            patch.object(CommercialAPIService, 'log_usage'), \
            patch('core.http_pool.pooled_post', side_effect=forward):
        r = app.test_client().post('/api/v1/intelligence/chat', json={'prompt': 'hello'},
                                   headers={'X-API-Key': 'customer-key'})
    return r, sent


def test_a_paid_call_on_a_keyed_node_is_admitted_by_chat_and_runs_revenue(
        hie, lanes, monkeypatch):
    monkeypatch.setenv('HEVOLVE_API_KEY', KEY)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    r, sent = _metered_call(hie, monkeypatch)
    assert sent['headers'] == {'X-API-Key': KEY}          # what _internal_auth_headers sends
    assert sent['chat'] == 'registered', 'the keyed node refused its own metered self-POST'
    assert lanes.lane_for_request(sent['json']['request_id']) == 'revenue'
    assert r.status_code == 200


def _bearer_user(headers):
    """Who the self-POST's JWT says is calling, as /chat reads it."""
    from integrations.social.auth import decode_jwt
    token = headers.get('Authorization', '')
    assert token.startswith('Bearer '), headers
    return decode_jwt(token[7:]).get('user_id')


def test_a_paid_call_on_a_keyless_node_runs_as_the_customer_on_the_revenue_lane(
        hie, lanes, monkeypatch):
    """Without HEVOLVE_API_KEY the self-POST carries a JWT, and /chat trusts a
    JWT over the body (correctly).  Minted for the node's own system identity
    it put every paying customer in ONE shared session with history and
    memory tools (task #153; the same collapse the channel relays had on
    2026-08-06).  The relay now mints it for the key's owner through
    chat_turn_request, and the node's lane word rides as the token's claim,
    so the turn is still revenue."""
    monkeypatch.delenv('HEVOLVE_API_KEY', raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    r, sent = _metered_call(hie, monkeypatch)
    assert _bearer_user(sent['headers']) == 'cust-42', (
        'the self-POST ran as %r, not the customer' % _bearer_user(sent['headers']))
    assert sent['chat'] == 'registered'
    assert lanes.lane_for_request(sent['json']['request_id']) == 'revenue'
    assert r.status_code == 200


def test_a_captured_customer_token_does_not_buy_the_revenue_lane_for_another_turn(
        hie, lanes, monkeypatch):
    """Task #157: the self-POST's token carries the node's lane word for ONE
    turn (its rid claim).  Replayed at the real /chat by the same user with a
    fresh request id and the same body word, the turn is admitted (the
    identity is good) but registered on the user lane."""
    monkeypatch.delenv('HEVOLVE_API_KEY', raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    _r, sent = _metered_call(hie, monkeypatch)
    assert lanes.lane_for_request(sent['json']['request_id']) == 'revenue'
    replay = dict(sent['json'], request_id='api_replay_' + sent['json']['request_id'][-8:])
    assert _chat(hie, monkeypatch, replay, sent['headers']) == 'registered'
    assert lanes.lane_for_request(replay['request_id']) == 'user'
    # The original turn's registration is untouched.
    assert lanes.lane_for_request(sent['json']['request_id']) == 'revenue'


def test_two_customers_on_a_keyless_node_never_share_an_identity(hie, lanes, monkeypatch):
    monkeypatch.delenv('HEVOLVE_API_KEY', raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    _r1, first = _metered_call(hie, monkeypatch, user_id='cust-1')
    _r2, second = _metered_call(hie, monkeypatch, user_id='cust-2')
    assert first['chat'] == second['chat'] == 'registered'
    assert _bearer_user(first['headers']) == 'cust-1'
    assert _bearer_user(second['headers']) == 'cust-2'
    for sent in (first, second):
        assert lanes.lane_for_request(sent['json']['request_id']) == 'revenue'


def _node_credential(hie, monkeypatch, mode):
    """The credential the node's own calls carry: the key on a keyed node, the
    system JWT on a keyless one (dispatch._internal_auth_headers decides), or
    nothing on a bundled desktop, where Nunba's in-process adapter posts
    /chat bare and the gate trusts the machine's own callers."""
    from integrations.agent_engine.dispatch import _internal_auth_headers
    monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
    if mode == 'keyed':
        monkeypatch.setenv('HEVOLVE_API_KEY', KEY)
        monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
        return _internal_auth_headers()
    if mode == 'keyless':
        monkeypatch.delenv('HEVOLVE_API_KEY', raising=False)
        monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
        return _internal_auth_headers()
    monkeypatch.delenv('HEVOLVE_API_KEY', raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    monkeypatch.setenv('NUNBA_BUNDLED', '1')
    return {}


@pytest.mark.parametrize('mode', ['keyed', 'keyless', 'bundled'])
def test_the_nodes_own_daemon_turn_keeps_the_lane_dispatch_goal_gave_it(
        hie, lanes, monkeypatch, mode):
    headers = _node_credential(hie, monkeypatch, mode)
    lanes.register_request_lane('daemon_goal-rev-1', 'revenue')     # a goal that earns
    body = {'user_id': 'system_daemon', 'prompt': 'market the thing',
            'request_id': 'daemon_goal-rev-1'}
    assert _chat(hie, monkeypatch, body, headers) == 'registered'
    assert lanes.lane_for_request('daemon_goal-rev-1') == 'revenue'
    # A plain goal's turn: nothing registered; the id's prefix rule says
    # daemon, which is what the wire stamps right after this turn.
    body['request_id'] = 'daemon_goal-plain-1'
    assert _chat(hie, monkeypatch, body, headers) == 'registered'
    assert lanes.lane_for_request('daemon_goal-plain-1') == ''
    assert hie.thread_local_data.get_request_id() == 'daemon_goal-plain-1'
    assert lanes.current_lane() == 'daemon'


@pytest.mark.parametrize('mode', ['keyed', 'keyless'])
def test_the_experts_inner_chat_call_keeps_the_revenue_turns_lane(hie, lanes, monkeypatch, mode):
    """Review of 8947e2753, C1: when a revenue turn uses the expert, the
    speculative dispatcher POSTs /chat again from inside the node with the
    person's request id, the node's credential, model_config and no lane
    field (speculative_dispatcher._build_dispatch_payload).  That inner call
    re-registered the turn as 'user', so the Claude request went out on the
    user lane into Kong's Claude cap.  Measured here through the real /chat:
    the shopper's outer call, then the inner call it triggers."""
    headers = _node_credential(hie, monkeypatch, mode)
    from integrations.commerce.bindings import TENANT
    from integrations.social.auth import generate_jwt
    shopper = generate_jwt('c1', 'Shopper', tenant_id=TENANT)
    outer = {'user_id': 'c1', 'prompt': 'add milk', 'request_id': 'msg_c1_expert'}
    assert _chat(hie, monkeypatch, outer, {'Authorization': 'Bearer ' + shopper}) == 'registered'
    assert lanes.lane_for_request('msg_c1_expert') == 'revenue'
    inner = {'user_id': 'c1', 'prompt': 'add milk', 'create_agent': False, 'autonomous': False,
             'casual_conv': False, 'draft_first': False, 'prompt_id': '7',
             'model_config': [{'model': 'claude-sonnet-4-5', 'api_key': 'k',
                               'base_url': 'http://172.17.0.1:8100/llm/v1'}],
             'request_id': 'msg_c1_expert'}
    assert _chat(hie, monkeypatch, inner, headers) == 'registered'
    assert lanes.lane_for_request('msg_c1_expert') == 'revenue'
    # What the wire would stamp on the Claude call right after this turn.
    assert hie.thread_local_data.get_request_id() == 'msg_c1_expert'
    assert lanes.current_lane() == 'revenue'


def test_a_stranger_naming_a_goals_request_id_does_not_inherit_its_lane(
        hie, lanes, monkeypatch):
    monkeypatch.delenv('HEVOLVE_API_KEY', raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    from integrations.social.auth import generate_jwt
    lanes.register_request_lane('daemon_goal-rev-2', 'revenue')
    token = generate_jwt('stranger-7', 'Stranger')
    body = {'user_id': 'stranger-7', 'prompt': 'hi', 'request_id': 'daemon_goal-rev-2',
            'lane': 'revenue'}
    assert _chat(hie, monkeypatch, body, {'Authorization': 'Bearer ' + token}) == 'registered'
    assert lanes.lane_for_request('daemon_goal-rev-2') == 'user'


def test_a_shopper_runs_revenue_and_a_person_runs_user_whatever_the_body_says(
        hie, lanes, monkeypatch):
    monkeypatch.delenv('HEVOLVE_API_KEY', raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    from integrations.commerce.bindings import TENANT
    from integrations.social.auth import generate_jwt
    shopper = generate_jwt('c1', 'Shopper', tenant_id=TENANT)
    person = generate_jwt('p1', 'Person')
    assert _chat(hie, monkeypatch,
                 {'user_id': 'c1', 'prompt': 'add milk', 'request_id': 'msg_c1'},
                 {'Authorization': 'Bearer ' + shopper}) == 'registered'
    assert _chat(hie, monkeypatch,
                 {'user_id': 'p1', 'prompt': 'hi', 'request_id': 'msg_p1', 'lane': 'revenue'},
                 {'Authorization': 'Bearer ' + person}) == 'registered'
    assert lanes.lane_for_request('msg_c1') == 'revenue'
    assert lanes.lane_for_request('msg_p1') == 'user'
