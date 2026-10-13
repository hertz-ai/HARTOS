"""A metered call whose backend produced no answer is neither billed nor
reported as success (task #110).

Measured live on central: on 2026-10-12 18:14Z a paid call whose turn failed
was answered with the "internal snag" sentence, ``success: true``, and billed;
on 2026-10-13 ``GET /api/v1/intelligence/hivemind`` answered
``{"result": null, "success": true}`` and was counted as a call, and
``generate`` counted a ``status: unavailable`` result the same way.

The pipeline does not raise when the model call fails: ``user_facing_error()``
turns the exception into a polite sentence and /chat returns it as the reply
with HTTP 200, which is right for a person and wrong for a caller deciding
whether work was done.  ``core.agent_tools.is_user_facing_error`` is the one
recogniser of that sentence; dispatch, the hive worker and the channel
contract already use it, and the metered endpoints were the missing caller.
For hivemind the bridge returns ``None`` when it has nothing (no core, no CCT
for the capability, the filter refused, every leg failed; the metered call
names no user, so a person's hive opt-out never applies), and for generate the
media agent says ``status: unavailable`` or ``status: error``: none of those is
work either.  The HTTP core's own 200 keeps its words under ``answer`` (the
review of 10600d264 measured it refused as no thought), so that leg is driven
through the real bridge below.

Each test drives the real blueprint with only the key gate stubbed (the
fixtures of tests/unit/test_commercial_api_chat_credential.py) and reads what
the customer receives and what the billing row records.

Run:
  pytest tests/unit/test_metered_failed_turn_is_not_billed.py -q
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from tests.unit.test_commercial_api_chat_credential import (  # noqa: F401
    MINTED, _resp, app_client, passing_auth)
from tests.unit.test_world_model_bridge import _make_bridge


# --------------------------------------------------------------------------
# What a failed turn looks like on the wire: the sentences the codebase emits
# --------------------------------------------------------------------------

FAILED_TURN_KINDS = ['could-not-finish-400', 'internal-snag', 'llm-loading',
                     'llm-generic-error', 'build-incomplete', 'empty']


def _failed_turn_sentence(kind):
    """The sentence /chat returns (HTTP 200) for a failed turn of ``kind``,
    taken from where the codebase writes it, never copied.  'empty' is a 200
    with no text at all: no answer either."""
    from core import constants as C
    from core.agent_tools import user_facing_error
    if kind == 'empty':
        return ''
    if kind == 'could-not-finish-400':
        return user_facing_error(Exception(
            "Error code: 400 - {'error': {'message': 'Reasoning is mandatory "
            "for this endpoint and cannot be disabled.'}}"))
    if kind == 'internal-snag':
        return user_facing_error(Exception('Connection reset by peer'))
    return {'llm-loading': C.LLM_LOADING_REPLY,
            'llm-generic-error': C.LLM_GENERIC_ERROR_REPLY,
            'build-incomplete': C.BUILD_INCOMPLETE_REPLY}[kind]


def _assert_not_billed(r, log_usage):
    payload = r.get_json()
    assert payload.get('success') is False, (
        'a call that produced no answer was reported %r' % (payload,))
    assert r.status_code != 200
    assert log_usage.called, 'the call should still be logged for audit'
    kwargs = log_usage.call_args.kwargs
    assert kwargs.get('status_code') not in (None, 200), (
        'the billing row recorded a failed call as 200')
    billable = (kwargs.get('tokens_in', 0) or 0) + (kwargs.get('tokens_out', 0) or 0)
    assert billable == 0, 'customer billed %d tokens for no answer' % billable


def _assert_billed_as_a_call(r, log_usage):
    assert r.status_code == 200, r.get_json()
    assert r.get_json().get('success') is True
    assert log_usage.called
    assert log_usage.call_args.kwargs.get('status_code', 200) == 200


# --------------------------------------------------------------------------
# chat and analyze: the self-POST answered 200 with a failed-turn sentence
# --------------------------------------------------------------------------

CHAT_ROUTES = [
    ('/api/v1/intelligence/chat', {'prompt': 'What is the capital of France?'}, 'response'),
    ('/api/v1/intelligence/analyze',
     {'document': 'France. Capital: Paris.', 'question': 'What is the capital?'},
     'analysis'),
]


def _metered(app_client, route, body, chat_reply):
    """Drive ``route``; the self-POST to /chat is answered 200 with
    ``chat_reply`` as the response text."""
    with patch('core.http_pool.pooled_post',
               side_effect=lambda url, **kw: _resp(200, {'response': chat_reply})), \
            patch('integrations.agent_engine.dispatch._internal_auth_headers',
                  return_value=MINTED):
        return app_client.post(route, json=body, headers={'X-API-Key': 'k'})


@pytest.mark.parametrize('route,body,resp_key', CHAT_ROUTES)
@pytest.mark.parametrize('kind', FAILED_TURN_KINDS)
def test_a_failed_turn_is_not_billed_and_not_success(
        app_client, passing_auth, route, body, resp_key, kind):
    r = _metered(app_client, route, body, _failed_turn_sentence(kind))
    _assert_not_billed(r, passing_auth)
    # The failure sentence is not handed over as the product either.
    assert r.get_json().get(resp_key) is None


@pytest.mark.parametrize('route,body,resp_key', CHAT_ROUTES)
def test_a_real_answer_is_still_billed_and_returned(
        app_client, passing_auth, route, body, resp_key):
    """The recogniser matches exactly the sentences the codebase emits for a
    failure; an answer that merely mentions trouble is an answer."""
    answer = ("Paris is the capital of France. I couldn't finish the second "
              "part of your question because it was cut off.")
    r = _metered(app_client, route, body, answer)
    _assert_billed_as_a_call(r, passing_auth)
    assert r.get_json()[resp_key] == answer
    assert passing_auth.call_args.kwargs['tokens_out'] == len(answer.split())


# --------------------------------------------------------------------------
# hivemind: the bridge had nothing
# --------------------------------------------------------------------------

def _hivemind(app_client, bridge_result=None, raises=None):
    bridge = MagicMock()
    if raises is not None:
        bridge.query_hivemind.side_effect = raises
    else:
        bridge.query_hivemind.return_value = bridge_result
    with patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge',
               return_value=bridge):
        return app_client.get('/api/v1/intelligence/hivemind?query=what+is+the+hive',
                              headers={'X-API-Key': 'k'})


def _empty_peer_reply():
    """What a connected peer with no hive core answers (the real sentinel)."""
    from core.peer_link.hivemind_handler import _EMPTY_REPLY
    return dict(_EMPTY_REPLY)


@pytest.mark.parametrize('nothing', [
    pytest.param(None, id='none: no core, no CCT, filter refused, every leg failed'),
    pytest.param({}, id='empty dict'),
    pytest.param('peerlink-empty', id='two peers that answered the empty reply'),
    pytest.param({'thought': ' '}, id='a blank thought'),
    pytest.param({'thoughts': [{'thought': '  '}], 'source': 'peerlink', 'peer_count': 1},
                 id='one peer that answered blank'),
])
def test_hivemind_with_nothing_to_say_is_not_billed(app_client, passing_auth, nothing):
    """query_hivemind returns None when the core is not installed, the node
    has no CCT, or the filter refused the prompt: measured on central as
    {"result": null, "success": true}, counted as a call.  A PeerLink
    envelope whose every peer sent hivemind_handler's empty reply (a peer
    with no core) is a non-empty dict and was still billed (the review of
    2efb73519 measured it); the ONE rule is a non-empty thought."""
    if nothing == 'peerlink-empty':
        nothing = {'thoughts': [_empty_peer_reply(), _empty_peer_reply()],
                   'source': 'peerlink', 'peer_count': 2}
    r = _hivemind(app_client, bridge_result=nothing)
    _assert_not_billed(r, passing_auth)


def test_hivemind_whose_bridge_raised_is_not_billed(app_client, passing_auth):
    r = _hivemind(app_client, raises=RuntimeError('hive core not installed'))
    _assert_not_billed(r, passing_auth)


@pytest.mark.parametrize('thought', [
    {'thought': 'The hive thinks Paris.'},
    {'thoughts': [{'thought': 'Paris'}], 'source': 'peerlink', 'peer_count': 1},
    # One silent peer and one that answered: a thought was had.
    {'thoughts': [{'type': 'reply', 'thought': ''}, {'response': 'Paris'}],
     'source': 'peerlink', 'peer_count': 2},
    {'thought': 'cached answer', 'source': 'cached', 'cct_gated': True},
])
def test_hivemind_with_a_thought_is_a_call(app_client, passing_auth, thought):
    r = _hivemind(app_client, bridge_result=thought)
    _assert_billed_as_a_call(r, passing_auth)
    assert r.get_json()['result'] == thought


#: The 200 body HevolveAI's api_server answers on POST /v1/hivemind/think
#: (hevolveai/src/hevolveai/server/api_server.py, the dict built at the end
#: of hivemind_think_together).  Values are placeholders; the KEYS are the
#: server's.
HTTP_CORE_BODY = {
    'status': 'ok', 'query': 'what is the hive',
    'answer': 'The hive is a network of agents thinking together.',
    'reasoning_chain': [], 'mode': 'LOCAL_ONLY', 'latency_ms': 12.0,
    'phase_times': {}, 'contributing_agents': ['inst-1'],
    'weights': {'inst-1': 1.0}, 'confidence': 0.9, 'num_contributors': 1,
    'distributed': False, 'topology': {}, 'adapted_timeout_ms': 1000,
}


def test_the_hivemind_rule_is_the_bridges_own():
    """The endpoint reads the bridge's own predicate, driven here on the
    shapes query_hivemind returns.  The bridge's peer count keeps its older,
    looser rule (telemetry, _peer_said_something); billing is the stricter
    one: a non-blank string under a thought key."""
    from integrations.agent_engine.world_model_bridge import (
        hivemind_result_has_a_thought as has_thought)
    assert not has_thought(None)
    assert not has_thought({})
    assert not has_thought({'thought': ''})
    assert not has_thought({'thought': ' '})
    assert not has_thought({'thoughts': [_empty_peer_reply()], 'source': 'peerlink',
                            'peer_count': 1})
    assert not has_thought({'thoughts': [], 'source': 'peerlink', 'peer_count': 0})
    assert has_thought({'thought': 'x'})
    assert has_thought({'thoughts': [_empty_peer_reply(), {'text': 'x'}],
                        'source': 'peerlink', 'peer_count': 2})
    assert has_thought({'response': 'a peer answered'})
    # The HTTP core's own body: its words are under 'answer', None when it
    # had none (HevolveAI api_server, hivemind_think_together).
    assert has_thought(dict(HTTP_CORE_BODY))
    assert not has_thought(dict(HTTP_CORE_BODY, answer=None))


# --------------------------------------------------------------------------
# hivemind: the HTTP core's real answer, through the real bridge
# --------------------------------------------------------------------------

def _hivemind_through_the_http_core(app_client, core_body):
    """Drive the endpoint with a REAL bridge whose only leg is the HTTP core:
    no in-process core, no connected peer (collect answers []), the CCT held,
    and the core's POST /v1/hivemind/think answered 200 with ``core_body``.
    Returns (endpoint response, the pooled_post mock)."""
    bridge = _make_bridge()
    bridge._http_disabled = False
    bridge._api_url = 'http://localhost:8000'
    mgr = MagicMock()
    mgr.collect.return_value = []
    post = MagicMock(return_value=_resp(200, core_body))
    with patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge',
               return_value=bridge), \
            patch('core.peer_link.link_manager.get_link_manager', return_value=mgr), \
            patch.object(bridge, '_check_cct_access', return_value=True), \
            patch('integrations.agent_engine.world_model_bridge.pooled_post', post):
        r = app_client.get('/api/v1/intelligence/hivemind?query=what+is+the+hive',
                           headers={'X-API-Key': 'k'})
    assert post.called, 'the HTTP core was never asked'
    assert post.call_args.args[0].endswith('/v1/hivemind/think')
    return r, post


def test_the_http_cores_answer_is_a_call(app_client, passing_auth):
    """On a node whose core is reached over HTTP (central's topology once a
    core is installed), the core's real 200 is the answer and is billed; the
    second round answered it 503 and dropped it (the review of 10600d264
    drove this leg on the parent and at HEAD)."""
    r, _ = _hivemind_through_the_http_core(app_client, HTTP_CORE_BODY)
    _assert_billed_as_a_call(r, passing_auth)
    assert r.get_json()['result'] == HTTP_CORE_BODY


def test_the_http_core_with_no_answer_is_not_billed(app_client, passing_auth):
    r, _ = _hivemind_through_the_http_core(app_client, dict(HTTP_CORE_BODY, answer=None))
    _assert_not_billed(r, passing_auth)


# --------------------------------------------------------------------------
# chat and analyze: the other 200s the one reader refuses
# --------------------------------------------------------------------------

@pytest.mark.parametrize('route,body,resp_key', CHAT_ROUTES)
@pytest.mark.parametrize('answer', [
    pytest.param({'success': False, 'error': 'busy with another task'}, id='success-false'),
    pytest.param({'response': 'Starting up', 'loading': True}, id='loading'),
    pytest.param({'response': 'Pick a model first', 'source': 'system'}, id='source-system'),
])
def test_a_200_the_call_path_would_not_pass_off_is_not_billed(
        app_client, passing_auth, route, body, resp_key, answer):
    """chat_contract.chat_turn_answer is the reader the call path uses
    (integrations.agentic_router.dispatch_to_agent, paired with
    chat_turn_result): what it refuses to speak in the agent's voice, the
    paid API does not bill."""
    with patch('core.http_pool.pooled_post',
               side_effect=lambda url, **kw: _resp(200, answer)), \
            patch('integrations.agent_engine.dispatch._internal_auth_headers',
                  return_value=MINTED):
        r = app_client.post(route, json=body, headers={'X-API-Key': 'k'})
    _assert_not_billed(r, passing_auth)


@pytest.mark.parametrize('route,body,resp_key', CHAT_ROUTES)
def test_a_200_that_is_not_chats_json_is_not_billed(
        app_client, passing_auth, route, body, resp_key):
    """A 200 whose body is not /chat's JSON object (a proxy's or server's
    error page): chat_turn_result keeps its words under 'error', so it is a
    failed turn, not an exception out of the metered endpoint."""
    page = MagicMock()
    page.status_code = 200
    page.json.side_effect = ValueError('not JSON')
    page.text = '<html><body>Bad Gateway</body></html>'
    with patch('core.http_pool.pooled_post', side_effect=lambda url, **kw: page), \
            patch('integrations.agent_engine.dispatch._internal_auth_headers',
                  return_value=MINTED):
        r = app_client.post(route, json=body, headers={'X-API-Key': 'k'})
    _assert_not_billed(r, passing_auth)
    assert r.get_json().get(resp_key) is None


# --------------------------------------------------------------------------
# generate: the media agent could not generate
# --------------------------------------------------------------------------

def _generate(app_client, media_result=None, raises=None):
    def fake_generate_media(**kw):
        if raises is not None:
            raise raises
        return json.dumps(media_result)
    with patch('integrations.service_tools.media_agent.generate_media',
               side_effect=fake_generate_media):
        return app_client.post('/api/v1/intelligence/generate',
                               json={'prompt': 'a red square', 'modality': 'image'},
                               headers={'X-API-Key': 'k'})


@pytest.mark.parametrize('media_result', [
    pytest.param({'status': 'unavailable', 'modality': 'image',
                  'error': 'image not available on this node right now.',
                  'suggestion': 'Describe the image in text instead.'},
                 id='unavailable'),
    pytest.param({'status': 'error', 'error': 'Unhandled modality: image'}, id='error'),
    # The composer is still starting: no file, no task id to poll, only a
    # message to ask again shortly (media_agent._generate_audio_music).
    pytest.param({'status': 'warming_up', 'output_modality': 'audio_music',
                  'message': 'The composer is starting up. Ask again shortly.'},
                 id='warming-up'),
])
def test_generate_that_generated_nothing_is_not_billed(
        app_client, passing_auth, media_result):
    r = _generate(app_client, media_result=media_result)
    _assert_not_billed(r, passing_auth)


def test_generate_whose_agent_raised_is_not_billed(app_client, passing_auth):
    r = _generate(app_client, raises=RuntimeError('sidecar down'))
    _assert_not_billed(r, passing_auth)


@pytest.mark.parametrize('media_result', [
    pytest.param({'status': 'completed', 'output_modality': 'image',
                  'results': [{'url': '/media/red.png'}]}, id='completed'),
    pytest.param({'status': 'pending', 'task_id': 'wan2gp_abc123',
                  'poll_tool': 'check_media_status'}, id='pending'),
])
def test_generate_that_produced_or_started_work_is_a_call(
        app_client, passing_auth, media_result):
    r = _generate(app_client, media_result=media_result)
    _assert_billed_as_a_call(r, passing_auth)
    assert r.get_json()['result'] == media_result
