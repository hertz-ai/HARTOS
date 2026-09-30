"""A peer's A2A message/send to a dynamic agent must RUN the agent, and a
turn that did not run must come back FAILED, never COMPLETED.

THE DEFECT, traced in the review of 3a32d8e4b (the fix that made
/a2a/<id>/jsonrpc answer JSON-RPC instead of an HTML 500):

  * Production registers only dynamic agents, whose executor
    (DynamicAgentExecutor.execute_agent_task) called
    ``chat_agent(message, user_id=..., prompt_id=...)`` and
    ``recipe(user_id=..., message=..., prompt_id=...)``.  Both real
    signatures are ``(user_id, text, prompt_id, file_id, request_id)``, so
    every call raised TypeError.
  * The executor, and the wrapper around it, turned the exception into
    model text, so handle_message_send marked the task COMPLETED.
    peer_reuse.try_peer_recipe_reuse then recorded the error string as a
    successful remote outcome and agent_daemon skipped its local CREATE for
    the goal: a loud failure became a silent false success.

The executor now goes through dispatch.local_chat_dispatch, the ONE
in-process call to this node's own /chat, and any turn that did not run
raises.  These tests drive the real handle_message_send with the real
executor factory; only the discovery store and the /chat boundary are
stubbed.
"""
import asyncio

import pytest

import integrations.agent_engine.dispatch as dispatch
import integrations.google_a2a.dynamic_agent_registry as dar
from integrations.google_a2a.dynamic_agent_registry import (
    DynamicAgentExecutor, TrainedAgent)
from integrations.google_a2a.google_a2a_integration import A2AMessageHandler
from integrations.google_a2a.register_dynamic_agents import (
    create_dynamic_executor_function)

_AGENT = TrainedAgent(
    agent_id='7700000123_0', prompt_id=7700000123, flow_id=0,
    persona='livetest_persona', action='summarise', recipe=[],
    status='completed', can_perform_without_user_input='yes',
    fallback_action='', metadata={'user_id': 'livetest_owner'},
    recipe_file='')


class _Discovery:
    def get_agent_by_id(self, agent_id):
        return _AGENT if agent_id == _AGENT.agent_id else None


@pytest.fixture
def calls(monkeypatch):
    executor = DynamicAgentExecutor.__new__(DynamicAgentExecutor)
    executor.discovery = _Discovery()
    monkeypatch.setattr(dar, '_dynamic_executor', executor)
    class _Seen(list):
        pass
    seen = _Seen()

    def set_reply(status, text):
        def local_chat_dispatch(prompt, user_id, prompt_id, daemon_id=None,
                                **kw):
            seen.append((prompt, user_id, prompt_id, daemon_id))
            return status, text
        monkeypatch.setattr(dispatch, 'local_chat_dispatch',
                            local_chat_dispatch)
    seen.set_reply = set_reply
    return seen


def _send(agent, text='summarise the ferry text'):
    handler = A2AMessageHandler(create_dynamic_executor_function(agent))
    return asyncio.run(handler.handle_message_send({'message': {
        'messageId': 'livetest_m1', 'contextId': 'livetest_c1',
        'parts': [{'kind': 'text', 'text': text}]}}))


def test_a_turn_that_ran_completes_with_its_answer(calls):
    calls.set_reply('ok', 'The ferry carries 83 cyclists.')
    task = _send(_AGENT)
    assert task['state'] == 'completed', task
    assert task['content']['parts'][0]['text'] == 'The ferry carries 83 cyclists.'
    # The one /chat door, called with the agent's owner and prompt_id, and
    # marked as background (a peer is not this node's human).  The turn is
    # named by its TASK id, not the contextId the peer chooses (review of
    # f97b6bed8, F4: two tasks in one context shared a cancel binding).
    assert calls == [('summarise the ferry text', 'livetest_owner',
                      7700000123, 'a2a_livetest_m1')]


def test_a_deferred_turn_fails_the_task(calls):
    calls.set_reply('deferred', None)
    task = _send(_AGENT)
    assert task['state'] == 'failed', task
    assert 'deferred' in task['error']


def test_a_failed_turn_is_not_reported_as_an_answer(calls):
    """The pipeline's own failure sentence is not a result."""
    from core.agent_tools import _SNAG_REPLY
    calls.set_reply('ok', _SNAG_REPLY)
    task = _send(_AGENT)
    assert task['state'] == 'failed', task


def test_an_empty_reply_fails_the_task(calls):
    calls.set_reply('ok', '')
    assert _send(_AGENT)['state'] == 'failed'


def test_an_unknown_agent_fails_the_task(calls):
    calls.set_reply('ok', 'should not be reached')
    ghost = TrainedAgent(**dict(_AGENT.__dict__, agent_id='livetest_ghost_0'))
    task = _send(ghost)
    assert task['state'] == 'failed', task
    assert calls == []


# ---------------------------------------------------------------------------
# Running an agent needs what /chat needs (review of 309bcd032: /a2a/ is an
# exempt prefix, so once the executor really ran a turn an unauthenticated
# caller on another machine could run an autonomous /chat turn as the owner).
# The real app, the real API gate, the real jsonrpc view and executor.
# ---------------------------------------------------------------------------

from flask import Flask, jsonify  # noqa: E402

import integrations.google_a2a.peer_reuse as peer_reuse  # noqa: E402
from integrations.google_a2a.google_a2a_integration import (  # noqa: E402
    A2AProtocolServer)
from security.middleware import _apply_api_auth  # noqa: E402

_REMOTE = {'REMOTE_ADDR': '203.0.113.7'}
_LOCAL = {'REMOTE_ADDR': '127.0.0.1'}
_SEND = {'jsonrpc': '2.0', 'id': 1, 'method': 'message/send', 'params': {
    'message': {'contextId': 'livetest_c2',
                'parts': [{'kind': 'text', 'text': 'open notepad and type hi'}]}}}


@pytest.fixture
def node(monkeypatch, calls):
    for k in ('NUNBA_BUNDLED', 'HEVOLVE_NODE_TIER', 'HEVOLVE_API_KEY'):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(peer_reuse, 'export_allowed', lambda pid: True)
    calls.set_reply('ok', 'done')
    app = Flask('livetest_node')
    _apply_api_auth(app)

    @app.route('/chat', methods=['POST'])
    def chat():
        return jsonify({'text': 'chat ran'})
    srv = A2AProtocolServer(app, 'http://node')
    srv.register_agent(_AGENT.agent_id, 'n', 'd', [{'id': 's'}],
                       create_dynamic_executor_function(_AGENT))
    srv.setup_routes()
    return app.test_client()


def _post(client, environ):
    return client.post(f'/a2a/{_AGENT.agent_id}/jsonrpc', json=_SEND,
                       environ_base=environ)


@pytest.mark.parametrize('env', [{'NUNBA_BUNDLED': '1'},
                                 {'HEVOLVE_NODE_TIER': 'central'}])
def test_an_unauthenticated_remote_caller_cannot_run_an_agent(
        node, calls, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert node.post('/chat', json={'prompt': 'x'},
                     environ_base=_REMOTE).status_code == 401
    r = _post(node, _REMOTE)
    assert r.status_code == 401, r.get_json()
    assert calls == [], 'the /chat turn ran for an unauthenticated caller'


def test_the_desktops_own_caller_runs_the_agent(node, calls, monkeypatch):
    monkeypatch.setenv('NUNBA_BUNDLED', '1')
    r = _post(node, _LOCAL)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['result']['state'] == 'completed'
    assert len(calls) == 1


def test_a_caller_with_the_api_key_runs_the_agent(node, calls, monkeypatch):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    monkeypatch.setenv('HEVOLVE_API_KEY', 'livetest-key')
    r = node.post(f'/a2a/{_AGENT.agent_id}/jsonrpc', json=_SEND,
                  environ_base=_REMOTE, headers={'X-API-Key': 'livetest-key'})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['result']['state'] == 'completed'


def test_an_agent_this_node_would_not_export_is_not_run(
        node, calls, monkeypatch):
    monkeypatch.setattr(peer_reuse, 'export_allowed', lambda pid: False)
    r = _post(node, _LOCAL)
    assert r.status_code == 403, r.get_json()
    assert calls == []


def test_the_turn_runs_off_the_views_event_loop(node, calls, monkeypatch):
    """The /chat turn's sync->async bridges (asyncio.run, run_until_complete
    on a fresh loop) must work: measured failing inside the view's loop."""
    def local_chat_dispatch(prompt, user_id, prompt_id, daemon_id=None, **kw):
        import asyncio
        return 'ok', asyncio.run(asyncio.sleep(0, result='bridged'))
    monkeypatch.setattr(dispatch, 'local_chat_dispatch', local_chat_dispatch)
    r = _post(node, _LOCAL)
    assert r.get_json()['result']['state'] == 'completed', r.get_json()
    assert r.get_json()['result']['content']['parts'][0]['text'] == 'bridged'


def test_the_callers_flask_g_does_not_reach_the_inner_turn(
        node, calls, monkeypatch):
    """Review of 05641511d, probed: asyncio.to_thread copied the jsonrpc
    request's context, so the outer gate's g.auth_source / g.jwt_payload
    were visible to the inner /chat.  The turn must start clean."""
    from flask import g, has_app_context
    seen = {}

    def local_chat_dispatch(prompt, user_id, prompt_id, daemon_id=None, **kw):
        seen['app_context'] = has_app_context()
        if has_app_context():
            seen['auth_source'] = getattr(g, 'auth_source', None)
        return 'ok', 'done'
    monkeypatch.setattr(dispatch, 'local_chat_dispatch', local_chat_dispatch)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    monkeypatch.setenv('HEVOLVE_API_KEY', 'livetest-key')

    import integrations.social.auth as social_auth
    monkeypatch.setattr(social_auth, 'decode_jwt',
                        lambda tok: {'user_id': 'livetest_remote_caller'})
    r = node.post(f'/a2a/{_AGENT.agent_id}/jsonrpc', json=_SEND,
                  environ_base=_REMOTE,
                  headers={'Authorization': 'Bearer livetest'})
    assert r.get_json()['result']['state'] == 'completed', r.get_json()
    assert seen.get('app_context') is False, seen
