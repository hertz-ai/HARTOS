"""A 2xx chat/completions reply that carries no completion is a failed call.

Measured on central 2026-11-03 (read-only, ``docker logs --since 72h``): of
2,236 calls to openrouter.ai, 433 answered 200, and 418 times in the same
window autogen raised ``TypeError: 'NoneType' object is not iterable`` in
``OpenAIClient.message_retrieval`` (autogen/oai/client.py, ``choices =
response.choices`` then iterated), each one after an OpenRouter 200.  The 200
carried no ``choices``.  autogen's ``OpenAIWrapper.create`` moves to the next
config entry only when ``client.create`` raises ``APIError``; a 200 does not
raise, so the reply was returned as if it were a completion and the turn died
later, on the snag reply, with the next entry never tried.  What those bodies
held was NOT measured: the hosted path writes no record of a reply.

So the wire hook (core.llm_outbound_logger), the one place every framework's
LLM traffic passes, answers such a reply itself with a no-retry 502 of type
``server_error``, the way ``_day_cap_refusal`` answers a refused expert call
with a no-retry 429.  The openai SDK raises it once, autogen moves to the
next entry, and the provider's own words are logged.

The body shapes below are shapes a 200 can take with no completion in it.
None of them is claimed to be what OpenRouter sent.

These tests drive the real hook on the real httpx, the real openai SDK and
the real autogen OpenAIWrapper against a live local HTTP server (the async
case uses httpx's MockTransport: this venv's httpx 0.24.1 with httpcore
1.0.9 cannot make a real async request, task #204).  Only the spend meter is
modelled, so no ledger row is written.
"""
import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


def _completion(text):
    return json.dumps({
        'id': 'chatcmpl-own', 'object': 'chat.completion', 'created': 1,
        'model': 'own-model',
        'choices': [{'index': 0, 'finish_reason': 'stop',
                     'message': {'role': 'assistant', 'content': text}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
    }).encode()


_HEAD = {'id': 'gen-1', 'object': 'chat.completion', 'created': 1,
         'model': 'gateway-model'}

NOT_A_COMPLETION = {
    'choices null': dict(_HEAD, choices=None),
    'an error object and no choices': {
        'error': {'message': 'upstream provider failed', 'code': 502}},
    'an empty choices list': dict(_HEAD, choices=[]),
    'a choice with no message': dict(
        _HEAD, choices=[{'index': 0, 'finish_reason': 'stop'}]),
    'an empty body': b'',
}


def _raw(body):
    return body if isinstance(body, bytes) else json.dumps(body).encode()


class _Endpoints(BaseHTTPRequestHandler):
    """Answers each path's first segment with that segment's reply; counts."""

    def do_POST(self):                                   # noqa: N802
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        who = self.path.strip('/').split('/')[0]
        self.server.hits[who] = self.server.hits.get(who, 0) + 1
        raw = self.server.replies[who]
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def _serve(replies):
    srv = ThreadingHTTPServer(('127.0.0.1', 0), _Endpoints)
    srv.hits, srv.replies = {}, dict(replies)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _pristine(send):
    """httpx's own send, beneath any hook an earlier import installed in this
    process, so the hook under test wraps the transport once, as in
    production (same unwrap as test_claude_day_cap_holds_mid_turn)."""
    while getattr(send, '__name__', '') in ('_patched_send', '_patched'):
        cells = dict(zip(send.__code__.co_freevars, send.__closure__ or ()))
        send = (cells.get('_orig_send') or cells['_orig']).cell_contents
    return send


@pytest.fixture
def node(monkeypatch):
    """Central's shape: the node's own model is a configured API endpoint,
    no expert trio, the wire hook installed, and a modelled spend meter."""
    srv = _serve({'own': _completion('answered by own')})
    base = 'http://127.0.0.1:%d' % srv.server_address[1]
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    monkeypatch.setenv('HEVOLVE_LLM_ENDPOINT_URL', base + '/own/v1')
    monkeypatch.setenv('HEVOLVE_LLM_MODEL_NAME', 'own-model')
    monkeypatch.delenv('HEVOLVE_EXPERT_LLM_ENDPOINT_URL', raising=False)
    monkeypatch.delenv('HEVOLVE_EXPERT_LLM_MODEL_NAME', raising=False)

    import httpx
    monkeypatch.setattr(httpx.Client, 'send', _pristine(httpx.Client.send))
    monkeypatch.setattr(httpx.AsyncClient, 'send', _pristine(httpx.AsyncClient.send))
    import core.llm_outbound_logger as hook
    hook._install_sync_patch(httpx)
    hook._install_async_patch(httpx)
    monkeypatch.setattr(hook, '_configured_endpoints_cache', (0.0, frozenset()))
    monkeypatch.setattr(hook, '_target_ports_cache',
                        ({hook._TARGET_PORT}, time.time() + 3600))

    from integrations.agent_engine import budget_gate as bg
    metered = []

    def _meter(model, tokens_in, tokens_out, task_source='own', **_kw):
        metered.append((model, tokens_in, tokens_out))

    monkeypatch.setattr(bg, 'meter_llm_call', _meter)
    try:
        yield {'base': base, 'hits': srv.hits, 'replies': srv.replies,
               'metered': metered}
    finally:
        srv.shutdown()
        srv.server_close()


def _hi():
    return [{'role': 'user', 'content': 'hi'}]


@pytest.mark.parametrize('shape', sorted(NOT_A_COMPLETION))
def test_autogen_moves_to_the_next_entry_past_a_reply_with_no_completion(node, shape):
    """The live failure: the turn's first entry answers 200 with no
    completion.  Before, autogen returned that reply and reading it raised
    (central's TypeError, for 'choices null'); now the next entry, the node's
    own model that with_local_fallback puts last, answers the turn."""
    import autogen
    from core import autogen_config
    node['replies']['empty'] = _raw(NOT_A_COMPLETION[shape])
    tier = {'model': 'gateway-model', 'api_key': 'dummy',
            'base_url': node['base'] + '/empty/v1'}
    turn = autogen.OpenAIWrapper(
        config_list=autogen_config.with_local_fallback([tier]), cache_seed=None)

    reply = turn.create(messages=_hi())
    # The very read that raised on central (generate_oai_reply takes [0]).
    text = turn.extract_text_or_completion_object(reply)[0]

    assert text == 'answered by own'
    assert node['hits'] == {'empty': 1, 'own': 1}, node['hits']


def test_the_sdk_raises_once_and_says_what_the_provider_said(node):
    """No retry (it would spend another call on a reply that was not a
    completion), and the provider's own words travel in the error."""
    import openai
    node['replies']['empty'] = _raw(NOT_A_COMPLETION['an error object and no choices'])
    client = openai.OpenAI(base_url=node['base'] + '/empty/v1',
                           api_key='dummy', max_retries=2)
    with pytest.raises(openai.InternalServerError) as failed:
        client.chat.completions.create(model='gateway-model', messages=_hi())
    assert 'upstream provider failed' in str(failed.value), failed.value
    assert node['hits'] == {'empty': 1}, 'the SDK retried: %s' % node['hits']


def test_the_person_still_gets_the_snag_reply_when_no_entry_is_left(node):
    """With one entry there is nothing to fall to.  The failed turn must read
    to the person as it did before (the snag reply, never an HTTP error
    string spoken by TTS) and still count as a failed turn."""
    import autogen
    from core.agent_tools import _SNAG_REPLY, is_user_facing_error, user_facing_error
    node['replies']['empty'] = _raw(NOT_A_COMPLETION['an empty body'])
    turn = autogen.OpenAIWrapper(
        config_list=[{'model': 'gateway-model', 'api_key': 'dummy',
                      'base_url': node['base'] + '/empty/v1'}],
        cache_seed=None)
    with pytest.raises(Exception) as failed:
        turn.extract_text_or_completion_object(turn.create(messages=_hi()))[0]
    import openai
    assert isinstance(failed.value, openai.InternalServerError), repr(failed.value)
    reply = user_facing_error(failed.value)
    assert reply == _SNAG_REPLY, reply
    assert is_user_facing_error(reply)


def test_the_spend_on_a_reply_with_no_completion_is_still_metered(node):
    """A provider that bills a reply with no completion has still been paid:
    the meter reads the real reply before the hook answers in its place."""
    import openai
    node['replies']['empty'] = _raw(dict(
        NOT_A_COMPLETION['choices null'],
        usage={'prompt_tokens': 900, 'completion_tokens': 0, 'total_tokens': 900}))
    client = openai.OpenAI(base_url=node['base'] + '/empty/v1',
                           api_key='dummy', max_retries=0)
    with pytest.raises(openai.InternalServerError):
        client.chat.completions.create(model='gateway-model', messages=_hi())
    assert node['metered'] == [('gateway-model', 900, 0)], node['metered']


def test_the_async_path_answers_the_same_way(node):
    """AsyncClient is the second send site (langchain's async invokes)."""
    import httpx
    import openai
    seen = []

    def _handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=NOT_A_COMPLETION['choices null'])

    async def _call():
        client = openai.AsyncOpenAI(
            base_url=node['base'] + '/empty/v1', api_key='dummy', max_retries=2,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)))
        try:
            await client.chat.completions.create(model='gateway-model', messages=_hi())
        finally:
            await client.close()

    with pytest.raises(openai.InternalServerError):
        asyncio.run(_call())
    assert len(seen) == 1, seen


def test_a_completion_is_returned_as_it_came(node):
    """Scope guard, not a fail-first test."""
    import openai
    client = openai.OpenAI(base_url=node['base'] + '/own/v1',
                           api_key='dummy', max_retries=0)
    reply = client.chat.completions.create(model='own-model', messages=_hi())
    assert reply.choices[0].message.content == 'answered by own'
    assert node['hits'] == {'own': 1}


def test_an_endpoint_the_node_did_not_configure_is_left_alone(node):
    """Scope guard, not a fail-first test: a call to a host the node did not
    configure is left as it came (owner rule 2026-10-06, the same scope
    _is_configured_endpoint gives the day cap and the meter)."""
    import openai
    other = _serve({'empty': _raw(NOT_A_COMPLETION['choices null'])})
    try:
        client = openai.OpenAI(
            base_url='http://127.0.0.1:%d/empty/v1' % other.server_address[1],
            api_key='dummy', max_retries=0)
        reply = client.chat.completions.create(model='gateway-model', messages=_hi())
        assert reply.choices is None
        assert other.hits == {'empty': 1}
    finally:
        other.shutdown()
        other.server_close()
