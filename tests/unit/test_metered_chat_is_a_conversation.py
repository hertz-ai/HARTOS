"""A paying customer's metered call is a conversation, never an agent creation.

Measured live on central (e829b072, 2026-10-13 03:20-03:35Z) through fresh
free keys: POST /api/v1/intelligence/chat answered "What is the capital of
France?" with "I have the name and goal, but not every step came through as a
step I can build. Tell me the actions this agent should take, in order, and I
will build it from those." (twice) and echoed the third prompt back, each
``success: true`` and billed.  The self-POST named an agent that does not
exist (``prompt_id`` ``api_intelligence`` / ``api_analyze``, no such prompt
file on any node), so /chat's ladder logged 'GATHER JSON doesnot EXISTS', set
``create_agent`` and ran ``gather_info`` turn 1/12; the model's non-JSON reply
to the agent-building prompt was returned raw.  The same questions through
/chat with ``prompt_id`` null answered "Paris is the capital of France." and a
correct function, 3/3.

The test drives the real metered endpoint and forwards its self-POST to the
real /chat, where the two ways a turn can be answered are told apart: the
plain answer (``get_ans``) and the agent-building turn (``gather_info``).
On the old code the customer receives the creation text.

Two more things the self-POST must say, both from the review of e0abe7c34:
the draft classifier is never consulted for a paid call (a confident
``is_create_agent`` verdict would mint an agent and bring the creation text
back; a confident ``delegate=none`` would ship the 0.8B draft as the paid
answer), and the turn is NOT the casual tool set (``casual_conv`` False):
that set carries ``Shell_Command`` and ``Computer_Action``, which a
self-issued free key must never reach.
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

ANSWER = 'Paris is the capital of France.'
CREATION = ('I have the name and goal, but not every step came through as a '
            'step I can build.')
NODE_KEY = 'node-key-for-the-metered-conversation-test'

ROUTES = [
    ('/api/v1/intelligence/chat',
     {'prompt': 'What is the capital of France? Answer in one sentence.'},
     'response'),
    ('/api/v1/intelligence/analyze',
     {'document': 'France. Capital: Paris.', 'question': 'What is the capital?'},
     'analysis'),
]


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


class _DraftConsulted(Exception):
    """/chat asked for the speculative dispatcher: the draft was about to answer."""


def _metered_call_through_real_chat(hie, monkeypatch, route, body):
    """Drive the real metered endpoint (its auth stubbed).  Its self-POST goes
    to the real /chat, whose two answer paths are stubbed apart; the stubs are
    the only LLM in the room.  The draft dispatcher raises if asked.  TTS and
    the chat_messages fan-out are stubbed so the test sends nothing to the
    desktop's bus (the review of e0abe7c34 saw a crossbar publish reach
    localhost:8088 from here).  Returns (endpoint response, what was sent,
    what the stubs saw)."""
    import hartos.gather_agentdetails as gad
    from integrations.social import chat_messages as _cm
    from integrations.agent_engine.commercial_api import (
        CommercialAPIService, commercial_api_bp)

    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    # A keyed central: the self-POST is admitted by the node's own key, the
    # way it is on a deployed node (dispatch._internal_auth_headers).
    monkeypatch.setenv('HEVOLVE_API_KEY', NODE_KEY)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    # No env override either way: /chat's default is draft-first ON, so only
    # the body's own flag can keep the draft out of a paid call.
    monkeypatch.delenv('HEVOLVE_DRAFT_FIRST', raising=False)
    monkeypatch.setattr(hie, '_persist_language', lambda *a, **k: None)
    monkeypatch.setattr(hie, '_cleanup_stale_agents', lambda: None)
    monkeypatch.setattr(hie, '_tts_synthesize_and_publish', lambda *a, **k: None)
    monkeypatch.setattr(_cm, 'persist_and_publish_async', lambda *a, **k: None)
    # The turn's own side effects, none of which belong in a unit test: the
    # longrunning.log publishes to the desktop's bus (Crossbar :8088) and
    # the resonance tuner's call to the live model (the review of 7e7ce19bc
    # counted 12 publishes and 12 llama-server requests from this file).
    monkeypatch.setattr(hie, 'publish_async', lambda *a, **k: None)
    monkeypatch.setattr(hie, '_tune_resonance_after_chat', lambda *a, **k: {})
    seen = {'get_ans': 0, 'gather_info': 0, 'casual_conv': None, 'draft': 0}

    def fake_get_ans(casual_conv, *a, **k):
        seen['get_ans'] += 1
        seen['casual_conv'] = casual_conv
        return ANSWER

    def fake_gather_info(*a, **k):
        seen['gather_info'] += 1
        return CREATION

    def draft_asked(*a, **k):
        seen['draft'] += 1
        raise _DraftConsulted()
    monkeypatch.setattr(hie, 'get_ans', fake_get_ans)
    monkeypatch.setattr(gad, 'gather_info', fake_gather_info)

    sent = {}

    def forward(url, json=None, headers=None, timeout=None, **_):
        sent['json'], sent['headers'] = json, dict(headers or {})
        with patch('integrations.agent_engine.speculative_dispatcher.'
                   'get_speculative_dispatcher', draft_asked):
            r = hie.app.test_client().post('/chat', json=json, headers=headers or {})
        out = MagicMock()
        out.status_code = r.status_code
        out.json.return_value = r.get_json() or {}
        sent['chat_status'], sent['chat_json'] = r.status_code, out.json.return_value
        return out

    app = Flask(__name__)
    app.register_blueprint(commercial_api_bp)
    key_data = {'id': 'key-1', 'user_id': 'cust-42', 'tier': 'pro'}
    with patch('integrations.social.models.get_db', return_value=MagicMock()), \
            patch.object(CommercialAPIService, 'validate_api_key', return_value=key_data), \
            patch.object(CommercialAPIService, 'check_rate_limit', return_value=True), \
            patch.object(CommercialAPIService, 'reserve_quota', return_value=True), \
            patch.object(CommercialAPIService, 'log_usage'), \
            patch('core.http_pool.pooled_post', side_effect=forward):
        r = app.test_client().post(route, json=body, headers={'X-API-Key': 'customer-key'})
    return r, sent, seen


@pytest.mark.parametrize('route,body,resp_key', ROUTES)
def test_a_metered_call_is_answered_not_turned_into_an_agent(
        hie, monkeypatch, route, body, resp_key):
    r, sent, seen = _metered_call_through_real_chat(hie, monkeypatch, route, body)

    # What the customer receives, first: on the old code this is the
    # agent-building text, success true.
    assert sent['chat_status'] == 200, sent['chat_json']
    assert r.status_code == 200
    assert r.get_json()[resp_key] == ANSWER, (
        'the customer received %r (gather_info called %d time(s), get_ans %d)'
        % (r.get_json().get(resp_key), seen['gather_info'], seen['get_ans']))
    assert seen['gather_info'] == 0, (
        'the customer\'s question was turned into an agent-building turn')
    assert seen['get_ans'] == 1
    # And why: the self-POST names no agent.  A made-up agent id exists on no
    # node, and /chat answers a missing agent with CREATE mode.
    assert 'prompt_id' in sent['json'] and sent['json']['prompt_id'] is None


@pytest.mark.parametrize('route,body,resp_key', ROUTES)
def test_a_paid_call_never_consults_the_draft(hie, monkeypatch, route, body, resp_key):
    """/chat defaults to draft-first for an agent-less turn.  For a paid call
    the self-POST turns it off: the dispatcher stub raises if asked, so the
    answer below exists only because it was never asked."""
    r, sent, seen = _metered_call_through_real_chat(hie, monkeypatch, route, body)
    assert sent['json'].get('draft_first') is False
    assert seen['draft'] == 0, 'the draft classifier was consulted for a paid call'
    assert sent['chat_status'] == 200, sent['chat_json']
    assert r.get_json()[resp_key] == ANSWER


@pytest.mark.parametrize('route,body,resp_key', ROUTES)
def test_a_paid_call_is_not_the_casual_tool_set(hie, monkeypatch, route, body, resp_key):
    """casual_conv stays False: the casual tool set carries Shell_Command and
    Computer_Action.  The shell handler is a denylist plus the owner's
    computer-control consent, and one grant covers every agent, so a free key
    must not be able to select that set."""
    r, sent, seen = _metered_call_through_real_chat(hie, monkeypatch, route, body)
    assert not sent['json'].get('casual_conv')
    assert seen['casual_conv'] is False, seen
    assert r.get_json()[resp_key] == ANSWER
