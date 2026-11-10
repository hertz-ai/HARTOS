"""A 2xx chat/completions reply that carries no completion is a failed call.

Measured on central 2026-11-03 (read-only, ``docker logs --since 72h``): of
2,236 calls to openrouter.ai, 433 answered 200, and 418 times in the same
window autogen raised ``TypeError: 'NoneType' object is not iterable`` in
``OpenAIClient.message_retrieval`` (autogen/oai/client.py, ``choices =
response.choices`` then iterated), each one after an OpenRouter 200.  The 200
carried no ``choices``.  autogen's ``OpenAIWrapper.create`` moves to the next
config entry only when ``client.create`` raises ``APIError``; a 200 does not
raise, so the reply was returned as if it were a completion, reading it
raised, and the next entry was never tried.  The person read that raw
exception ("I couldn't finish that: 'NoneType' object is not iterable",
measured by the review of f8af9b3af), not the snag reply.  What those bodies
held was NOT measured: the hosted path writes no record of a reply.

So the wire hook (core.llm_outbound_logger), which every httpx-based LLM call
passes (autogen, langchain, the openai SDK; not the raw callers that post
through core.http_pool's requests session), answers such a reply itself with
a no-retry 502 of type
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
    """With one entry there is nothing to fall to.  The failed turn reads to
    the person as the snag reply (never an HTTP error string spoken by TTS)
    and still counts as a failed turn.  Before, the person read the raw
    exception ("I couldn't finish that: 'NoneType' object is not
    iterable"), which also counted as failed."""
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


def test_agent_lightning_does_not_resample_a_reply_with_no_completion(node, monkeypatch):
    """FAILS ON f8af9b3af: agent_lightning's wrapper (on by default in Nunba)
    took the hook's 502 for a bad sample, called the endpoint 3 times, filed
    a self-heal report and returned its fallback sentence as the Assistant's
    answer.  The real autogen Assistant on one entry, wrapped the way
    test_toolcall_parse_500_retry wraps it."""
    import autogen
    import openai
    from hartos import exception_collector
    from integrations.agent_lightning.wrapper import AgentLightningWrapper
    reports = []
    monkeypatch.setattr(exception_collector, 'report_subsystem_failure',
                        lambda **kw: reports.append(kw))
    node['replies']['empty'] = _raw(NOT_A_COMPLETION['choices null'])
    assistant = autogen.AssistantAgent('Assistant', llm_config={
        'config_list': [{'model': 'gateway-model', 'api_key': 'dummy',
                         'base_url': node['base'] + '/empty/v1'}],
        'cache_seed': None})
    w = AgentLightningWrapper.__new__(AgentLightningWrapper)
    w.agent, w.agent_id = assistant, 'Assistant'
    w.track_rewards = w.auto_trace = False
    w.tracer = w.reward_calculator = w.current_span_id = None
    w.execution_count = 0
    generate_reply = w._wrap_generate_reply(assistant.generate_reply)

    with pytest.raises(openai.InternalServerError):
        generate_reply(messages=_hi())
    assert node['hits'] == {'empty': 1}, node['hits']
    assert reports == []


def test_a_completion_that_starts_with_a_byte_order_mark_is_returned(node):
    """FAILS ON f8af9b3af: the body was decoded as text before it was
    parsed, so a real completion behind a UTF-8 byte order mark was 'not a
    JSON object' and was answered with a 502.  Read at the hook, on the real
    httpx: the reply comes back as the provider sent it."""
    import httpx
    node['replies']['bom'] = b'\xef\xbb\xbf' + _completion('answered behind a bom')
    reply = httpx.post(node['base'] + '/bom/v1/chat/completions',
                       json={'model': 'gateway-model', 'messages': _hi()})
    assert reply.status_code == 200, reply.text
    assert reply.content.startswith(b'\xef\xbb\xbf')
    assert node['hits'] == {'bom': 1}


def test_the_breaker_reads_the_real_reply_not_the_hooks_answer(node):
    """Pins a claim, not a fail-first test: a 200 says the account is in
    good standing, so it clears the provider breaker's count even when the
    hook answers it with a 502."""
    import openai
    from core.circuit_breaker import CircuitState, llm_provider_breaker
    host = '127.0.0.1'
    llm_provider_breaker.reset(host)
    try:
        for _ in range(llm_provider_breaker._threshold - 1):
            llm_provider_breaker.record_failure(host)
        node['replies']['empty'] = _raw(NOT_A_COMPLETION['choices null'])
        client = openai.OpenAI(base_url=node['base'] + '/empty/v1',
                               api_key='dummy', max_retries=0)
        with pytest.raises(openai.InternalServerError):
            client.chat.completions.create(model='gateway-model', messages=_hi())
        llm_provider_breaker.record_failure(host)
        assert llm_provider_breaker.state(host) == CircuitState.CLOSED
    finally:
        llm_provider_breaker.reset(host)


def test_the_providers_words_are_logged(node, monkeypatch):
    """Pins a claim, not a fail-first test: the hosted path keeps no record
    of a reply, so the warning is the only place the provider's words land."""
    import openai
    import core.llm_outbound_logger as hook
    warned = []
    monkeypatch.setattr(hook.logger, 'warning',
                        lambda msg, *a, **k: warned.append(msg % a))
    node['replies']['empty'] = _raw(NOT_A_COMPLETION['an error object and no choices'])
    client = openai.OpenAI(base_url=node['base'] + '/empty/v1',
                           api_key='dummy', max_retries=0)
    with pytest.raises(openai.InternalServerError):
        client.chat.completions.create(model='gateway-model', messages=_hi())
    assert any('upstream provider failed' in w and 'failed call' in w
               for w in warned), warned


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
