"""A Claude call goes out with its prompt cached, and the agent reads the same answer.

Owner 2026-11-11 (via the investor session): "implement prompt caching
properly without degradation in agentic skill".  Central's Claude calls were
OpenAI-shaped requests to Anthropic's compatibility layer, which does not
cache prompts: about 17k input tokens per call, all at the full price.  The
wire hook now sends a configured endpoint's Claude call to the Messages API
with cache breakpoints (core.claude_native) and hands the caller the chat
completion it expects.

These tests drive the real openai SDK and autogen's OpenAIWrapper through
the installed hook; the only stand-in is Anthropic's API, an httpx
MockTransport that records what reached it.
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

import httpx
import openai
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import llm_outbound_logger as hook
from integrations.agent_engine import budget_gate as bg

MODEL = 'claude-sonnet-5-5'
BASE = 'https://api.anthropic.com/v1'
KEY = 'sk-ant-test-key'
SYSTEM = 'You are the sales agent for a speech therapy clinic.'
PARAMS = {'type': 'object',
          'properties': {'channel': {'type': 'string'}, 'text': {'type': 'string'}},
          'required': ['channel', 'text']}
TOOL = {'type': 'function',
        'function': {'name': 'post_to_channel', 'description': 'Post a message to a channel.',
                     'parameters': PARAMS}}
EPHEMERAL = {'type': 'ephemeral'}


@pytest.fixture
def metered(monkeypatch):
    """Central's configuration: Claude is the configured expert at Anthropic,
    the node's own model is an API (so Claude is priced), the day cap has
    room.  Returns the rows the meter would write."""
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_ENDPOINT_URL', BASE)
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_MODEL_NAME', MODEL)
    monkeypatch.setenv('HEVOLVE_NODE_ID', 'node-test')
    monkeypatch.setattr(hook, '_configured_endpoints_cache', (0.0, frozenset()))
    # Restored after each test: the hook is installed on the real classes.
    monkeypatch.setattr(httpx.Client, 'send', httpx.Client.send)
    hook._install_sync_patch(httpx)
    monkeypatch.setattr(bg, 'expert_budget_allows', lambda model: (True, 'under the cap'))
    monkeypatch.setattr(bg, '_is_local_model', lambda: False)
    rows = []
    monkeypatch.setattr(bg, 'record_metered_usage', lambda **kw: rows.append(kw) or 'row')
    return rows


def _message(content, stop='end_turn', usage=None):
    return {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': MODEL,
            'content': content, 'stop_reason': stop, 'stop_sequence': None,
            'usage': usage or {'input_tokens': 20, 'output_tokens': 5,
                               'cache_creation_input_tokens': 900,
                               'cache_read_input_tokens': 0}}


def _anthropic(reply, status=200, headers=None):
    """A stand-in for Anthropic: the Messages API answers ``reply``; the
    compatibility layer's path answers 404, so reaching it fails the test."""
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path != '/v1/messages':
            return httpx.Response(404, json={'error': {'type': 'not_found_error',
                                                       'message': 'reached ' + request.url.path}})
        return httpx.Response(status, json=reply,
                              headers=headers or {'request-id': 'req_011'})
    return handler, seen


def _claude(handler, base=BASE, max_retries=0):
    return openai.OpenAI(api_key=KEY, base_url=base, max_retries=max_retries,
                         http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def _sent(seen):
    return json.loads(seen[-1].content)


# ─── The request ────────────────────────────────────────────────────────

def test_a_claude_call_goes_to_the_messages_api_with_its_prompt_cached(metered):
    handler, seen = _anthropic(_message([{'type': 'text', 'text': 'Hello from Claude'}]))
    reply = _claude(handler).chat.completions.create(
        model=MODEL, user='req-42', tools=[TOOL],
        messages=[{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': 'hi'}])

    assert len(seen) == 1 and seen[0].url.path == '/v1/messages'
    assert seen[0].headers['x-api-key'] == KEY
    assert 'authorization' not in seen[0].headers
    assert seen[0].headers['anthropic-version'] == '2023-06-01'
    body = _sent(seen)
    # The agent's instructions and its tools are one cached prefix; the
    # conversation is cached by the automatic breakpoint.
    assert body['system'] == [{'type': 'text', 'text': SYSTEM, 'cache_control': EPHEMERAL}]
    assert body['cache_control'] == EPHEMERAL
    assert body['tools'] == [{'name': 'post_to_channel',
                              'description': 'Post a message to a channel.',
                              'input_schema': PARAMS}]
    assert body['messages'] == [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}]
    assert body['max_tokens'] == 32000
    assert 'user' not in body and 'thinking' not in body     # ignored / left at the default

    assert reply.choices[0].message.content == 'Hello from Claude'
    assert reply.choices[0].finish_reason == 'stop'
    assert reply.usage.prompt_tokens == 920 and reply.usage.completion_tokens == 5
    # A dict on openai 1.45 (this venv), a typed field on 1.82 (central).
    assert reply.usage.model_dump()['prompt_tokens_details']['cached_tokens'] == 0


def test_a_tool_loop_round_trips(metered):
    """The agent's tool call and its result go back as Claude's own blocks,
    and Claude's next tool call comes back as an OpenAI tool call.  Thinking
    is not part of the reply, as on the compatibility layer."""
    handler, seen = _anthropic(_message(
        [{'type': 'thinking', 'thinking': '', 'signature': 'sig'},
         {'type': 'tool_use', 'id': 'toolu_2', 'name': 'post_to_channel',
          'input': {'channel': 'email', 'text': 'Free first month'}}],
        stop='tool_use'))
    reply = _claude(handler).chat.completions.create(model=MODEL, tools=[TOOL], messages=[
        {'role': 'system', 'content': SYSTEM},
        {'role': 'user', 'content': 'Post the launch note'},
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'toolu_1', 'type': 'function',
             'function': {'name': 'post_to_channel',
                          'arguments': '{"channel": "telegram", "text": "We are live"}'}}]},
        {'role': 'tool', 'tool_call_id': 'toolu_1', 'content': 'posted: message 77'},
    ])

    assert _sent(seen)['messages'] == [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'Post the launch note'}]},
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'toolu_1', 'name': 'post_to_channel',
             'input': {'channel': 'telegram', 'text': 'We are live'}}]},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'posted: message 77'}]},
    ]
    message = reply.choices[0].message
    assert message.content is None
    assert [c.id for c in message.tool_calls] == ['toolu_2']
    assert message.tool_calls[0].function.name == 'post_to_channel'
    assert json.loads(message.tool_calls[0].function.arguments) == {
        'channel': 'email', 'text': 'Free first month'}
    assert reply.choices[0].finish_reason == 'tool_calls'


def test_system_messages_are_joined_as_the_compatibility_layer_joins_them(metered):
    """The layer joins every system message with one newline, wherever it
    sits.  A later one changes the joined prompt, so the cached prefix then
    ends at the tools instead."""
    handler, seen = _anthropic(_message([{'type': 'text', 'text': 'ok'}]))
    _claude(handler).chat.completions.create(model=MODEL, tools=[TOOL], messages=[
        {'role': 'system', 'content': 'Rule one.'},
        {'role': 'user', 'content': 'u1'},
        {'role': 'assistant', 'content': 'a1'},
        {'role': 'system', 'content': 'Rule two.'},
        {'role': 'user', 'content': 'u2'},
    ])
    body = _sent(seen)
    assert body['system'] == 'Rule one.\nRule two.'
    assert body['tools'][-1]['cache_control'] == EPHEMERAL
    assert [m['role'] for m in body['messages']] == ['user', 'assistant', 'user']


# ─── What is left alone ─────────────────────────────────────────────────

def _compat_reply():
    return {'id': 'chatcmpl-1', 'object': 'chat.completion', 'created': 1, 'model': MODEL,
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': 'from the layer'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}}


@pytest.mark.parametrize('extra', [
    {'response_format': {'type': 'json_schema',
                         'json_schema': {'name': 'x', 'strict': True, 'schema': {}}}},
    {'n': 2},
    {'messages': [{'role': 'user', 'content': [
        {'type': 'input_audio', 'input_audio': {'data': 'AAAA', 'format': 'wav'}}]}]},
    {'messages': [{'role': 'function', 'name': 'f', 'content': 'x'}]},
    # The Messages API answers 400 to a first message that is not the
    # user's; 114 of 1,101 real request bodies on the owner's desktop started
    # with an assistant turn (review of 2fc216a38, MF-1).  A LAST assistant
    # message is no longer sent as it came: the layer refused it on every
    # current model (see test_the_models_own_last_turn_is_followed_by_a_user_turn).
    {'messages': [{'role': 'assistant', 'content': 'Hello, how can I help?'},
                  {'role': 'user', 'content': 'hi'}]},
    {'messages': [{'role': 'system', 'content': SYSTEM},
                  {'role': 'assistant', 'content': 'Hello, how can I help?'},
                  {'role': 'user', 'content': 'hi'}]},
], ids=['response_format', 'n', 'audio', 'function_role', 'leading_assistant',
        'assistant_after_system'])
def test_a_request_not_mapped_one_to_one_goes_to_the_layer_as_it_came(metered, extra):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_compat_reply())
    body = dict({'model': MODEL, 'messages': [{'role': 'user', 'content': 'hi'}]}, **extra)
    raw = json.dumps(body).encode()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        answer = client.post(BASE + '/chat/completions', content=raw,
                             headers={'authorization': 'Bearer ' + KEY,
                                      'content-type': 'application/json'})
    assert seen[-1].url.path == '/v1/chat/completions'
    assert seen[-1].content == raw
    assert answer.json()['choices'][0]['message']['content'] == 'from the layer'


def test_a_claude_model_on_another_host_is_left_alone(metered):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_compat_reply())
    reply = _claude(handler, base='https://openrouter.ai/api/v1').chat.completions.create(
        model=MODEL, messages=[{'role': 'user', 'content': 'hi'}])
    assert seen[-1].url.path == '/api/v1/chat/completions'
    assert reply.choices[0].message.content == 'from the layer'


# ─── Errors and replies that are not answers ────────────────────────────

def test_an_error_keeps_its_status_words_and_retry_advice(metered):
    handler, seen = _anthropic(
        {'type': 'error', 'error': {'type': 'rate_limit_error',
                                    'message': 'Number of request tokens has exceeded your rate limit.'}},
        status=429, headers={'retry-after': '7', 'x-should-retry': 'false',
                             'request-id': 'req_429'})
    with pytest.raises(openai.RateLimitError) as caught:
        _claude(handler, max_retries=2).chat.completions.create(
            model=MODEL, messages=[{'role': 'user', 'content': 'hi'}])
    assert len(seen) == 1                       # x-should-retry: false was honoured
    assert 'exceeded your rate limit' in str(caught.value)
    assert caught.value.response.headers['retry-after'] == '7'
    assert caught.value.response.headers['x-request-id'] == 'req_429'


def test_a_bad_request_keeps_its_words(metered):
    handler, _ = _anthropic(
        {'type': 'error', 'error': {'type': 'invalid_request_error',
                                    'message': 'messages.0.content: empty'}}, status=400)
    with pytest.raises(openai.BadRequestError) as caught:
        _claude(handler).chat.completions.create(
            model=MODEL, messages=[{'role': 'user', 'content': 'hi'}])
    assert 'messages.0.content: empty' in str(caught.value)


def test_a_2xx_that_is_not_a_message_is_a_failed_call(metered):
    handler, _ = _anthropic({'type': 'error', 'error': {'type': 'overloaded_error',
                                                        'message': 'Overloaded'}})
    with pytest.raises(openai.InternalServerError):
        _claude(handler).chat.completions.create(
            model=MODEL, messages=[{'role': 'user', 'content': 'hi'}])


# ─── The meter ──────────────────────────────────────────────────────────

def test_each_native_call_logs_its_cache_use(metered, caplog):
    """Review of 2fc216a38, MF-4: nothing recorded the cache counts, so a
    deploy could not show whether caching happened.  Each native call now
    leaves one INFO line in the node's log with its prompt tokens and how
    many were written to and read from the cache."""
    import logging
    handler, _ = _anthropic(_message([{'type': 'text', 'text': 'ok'}], usage={
        'input_tokens': 100, 'output_tokens': 50,
        'cache_creation_input_tokens': 1000, 'cache_read_input_tokens': 9000}))
    with caplog.at_level(logging.INFO, logger='llm_outbound'):
        _claude(handler).chat.completions.create(
            model=MODEL, messages=[{'role': 'user', 'content': 'hi'}])
    lines = [r.getMessage() for r in caplog.records if '[claude-native]' in r.getMessage()]
    assert len(lines) == 1, lines
    assert ('model=%s' % MODEL) in lines[0]
    assert 'prompt=10100 output=50 cache_write=1000 cache_read=9000' in lines[0]


def test_cache_reads_and_writes_are_metered_at_their_own_prices(metered, monkeypatch):
    monkeypatch.setenv('HEVOLVE_CLAUDE_USD_PER_1K_IN', '0.002')
    monkeypatch.setenv('HEVOLVE_CLAUDE_USD_PER_1K_OUT', '0.010')
    handler, _ = _anthropic(_message([{'type': 'text', 'text': 'ok'}], usage={
        'input_tokens': 100, 'output_tokens': 50,
        'cache_creation_input_tokens': 1000, 'cache_read_input_tokens': 9000}))
    reply = _claude(handler).chat.completions.create(
        model=MODEL, messages=[{'role': 'user', 'content': 'hi'}])
    assert reply.usage.prompt_tokens == 10100
    assert reply.usage.model_dump()['prompt_tokens_details']['cached_tokens'] == 9000
    row = metered[-1]
    assert (row['tokens_in'], row['tokens_out']) == (10100, 50)
    # 100 fresh + 1000 written at 1.25x + 9000 read at 0.05x (Sonnet 5.5),
    # then 50 output tokens: $0.0041, where the full price was $0.0207.
    dollars = (row['tokens_in'] + row['tokens_out']) / 1000.0 * row['cost_per_1k']
    assert dollars == pytest.approx((0.002 * (100 + 1000 * 1.25 + 9000 * 0.05)
                                     + 0.010 * 50) / 1000.0)


# ─── The agent's own reader ─────────────────────────────────────────────

def test_autogen_reads_the_answer(metered):
    from autogen import OpenAIWrapper
    handler, seen = _anthropic(_message(
        [{'type': 'text', 'text': 'Posting now.'},
         {'type': 'tool_use', 'id': 'toolu_9', 'name': 'post_to_channel',
          'input': {'channel': 'telegram', 'text': 'hello'}}], stop='tool_use'))
    wrapper = OpenAIWrapper(cache_seed=None, config_list=[{
        'model': MODEL, 'api_key': KEY, 'base_url': BASE, 'price': [0.002, 0.01],
        'http_client': httpx.Client(transport=httpx.MockTransport(handler))}])
    response = wrapper.create(messages=[{'role': 'system', 'content': SYSTEM},
                                        {'role': 'user', 'content': 'Post hello'}],
                              tools=[TOOL])
    assert seen[-1].url.path == '/v1/messages'
    (message,) = wrapper.extract_text_or_completion_object(response)
    assert message.content == 'Posting now.'
    assert message.tool_calls[0].function.name == 'post_to_channel'
    assert json.loads(message.tool_calls[0].function.arguments) == {
        'channel': 'telegram', 'text': 'hello'}


# ─── The async client ───────────────────────────────────────────────────

def test_the_async_path_goes_native_too(monkeypatch):
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_ENDPOINT_URL', BASE)
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_MODEL_NAME', MODEL)
    monkeypatch.setattr(hook, '_configured_endpoints_cache', (0.0, frozenset()))
    monkeypatch.setattr(bg, 'expert_budget_allows', lambda model: (True, 'under the cap'))
    monkeypatch.setattr(bg, 'meter_llm_call', lambda **kw: None)
    seen = []

    class _AsyncClient:
        async def send(self, request, **kwargs):
            seen.append(request)
            return httpx.Response(200, json=_message([{'type': 'text', 'text': 'async ok'}]),
                                  request=request)
    hook._install_async_patch(SimpleNamespace(AsyncClient=_AsyncClient, Request=httpx.Request,
                                              Response=httpx.Response))
    request = httpx.Request('POST', BASE + '/chat/completions',
                            headers={'authorization': 'Bearer ' + KEY},
                            json={'model': MODEL, 'messages': [{'role': 'user', 'content': 'hi'}]})
    answer = asyncio.run(_AsyncClient().send(request))
    assert seen[-1].url.path == '/v1/messages'
    assert answer.json()['choices'][0]['message']['content'] == 'async ok'
    assert answer.request is request


# ─── Turns the Messages API refuses ─────────────────────────────────────
# Central, 2026-11-19, the daemon's goal turns on claude-haiku-5-5: two
# shapes of group-chat history were refused with a 400 and failed the turn.

def _assert_the_api_accepts(turns):
    """The Messages API's own turn rules: the conversation starts and ends
    with the user; each tool call is answered in the very next user turn, its
    results ahead of anything else there; no result without its call."""
    assert turns[0]['role'] == 'user' and turns[-1]['role'] == 'user', \
        [t['role'] for t in turns]
    for i, turn in enumerate(turns):
        if i:
            assert turn['role'] != turns[i - 1]['role'], [t['role'] for t in turns]
        kinds = [b['type'] for b in turn['content']]
        if turn['role'] == 'assistant':
            calls = [b['id'] for b in turn['content'] if b['type'] == 'tool_use']
            if calls:
                answer = turns[i + 1]['content']
                results = [b['tool_use_id'] for b in answer if b['type'] == 'tool_result']
                assert sorted(results) == sorted(calls), (i, calls, results)
                assert [b['type'] for b in answer[:len(results)]] == ['tool_result'] * len(results)
        else:
            previous = turns[i - 1]['content'] if i else []
            called = {b['id'] for b in previous if b['type'] == 'tool_use'}
            assert {b['tool_use_id'] for b in turn['content']
                    if b['type'] == 'tool_result'} <= called, (i, kinds)


def _call(call_id):
    return {'id': call_id, 'type': 'function',
            'function': {'name': 'post_to_channel',
                         'arguments': '{"channel": "email", "text": "%s"}' % call_id}}


def test_the_models_own_last_turn_is_followed_by_a_user_turn(metered):
    """Central 20:58:22: the StatusVerifier was asked to speak after the
    Assistant said nothing.  The role-order guard drops that empty turn, so
    the verifier's request ended on its own earlier verdict, and Claude 5
    models refuse a conversation that ends on the assistant ('This model does
    not support assistant message prefill').  The verdict stays where it was,
    and the request ends on a user turn."""
    from core.constants import WIRE_USER_SEED_TEXT
    handler, seen = _anthropic(_message([{'type': 'text', 'text': '{"status": "done"}'}]))
    reply = _claude(handler).chat.completions.create(model=MODEL, messages=[
        {'role': 'system', 'content': SYSTEM},
        {'role': 'user', 'name': 'ChatInstructor', 'content': 'Execute Action 2: draft the post'},
        {'role': 'assistant', 'content': '{"status": "pending"}'},
        {'role': 'user', 'name': 'Assistant', 'content': 'Assistant called post_to_channel'},
        {'role': 'assistant', 'content': '{"status": "pending", "reason": "no reply yet"}'},
    ])
    assert seen[-1].url.path == '/v1/messages'
    turns = _sent(seen)['messages']
    _assert_the_api_accepts(turns)
    assert turns[3] == {'role': 'assistant', 'content': [
        {'type': 'text', 'text': '{"status": "pending", "reason": "no reply yet"}'}]}
    assert turns[4] == {'role': 'user', 'content': [{'type': 'text', 'text': WIRE_USER_SEED_TEXT}]}
    assert reply.choices[0].message.content == '{"status": "done"}'


def test_a_tool_call_with_no_result_right_after_it_is_left_out(metered, monkeypatch):
    """Central 21:01:14, 'messages.6: tool_use ids were found without
    tool_result blocks immediately after'.  The Assistant's history, through
    the seats' own ToolMessageHandler: the token limiter's window opened on a
    bundled result whose call it had cut, the handler turned that bundle into
    a user message and appended it AFTER the Assistant's three new calls, so
    those calls had no result after them.  The orphaned calls are left out;
    the answered calls keep their results right behind them."""
    from unittest.mock import MagicMock
    monkeypatch.setattr('hartos.helper.current_app', MagicMock())
    from hartos.helper import ToolMessageHandler
    trimmed_bundle = {'role': 'tool', 'name': 'Assistant', 'content': 'r1\n\nr2',
                      'tool_responses': [
                          {'tool_call_id': 'toolu_cut1', 'role': 'tool', 'content': 'r1'},
                          {'tool_call_id': 'toolu_cut2', 'role': 'tool', 'content': 'r2'}]}
    answered = ['toolu_a%d' % i for i in range(1, 5)]
    pending = ['toolu_b%d' % i for i in range(1, 4)]
    history = ToolMessageHandler().apply_transform([
        {'role': 'user', 'name': 'ChatInstructor', 'content': 'Execute Action 2: post the offer'},
        trimmed_bundle,
        {'role': 'assistant', 'name': 'Assistant', 'content': 'Checking the channels first.'},
        {'role': 'user', 'name': 'StatusVerifier', 'content': '{"status": "pending"}'},
        {'role': 'assistant', 'content': None, 'tool_calls': [_call(c) for c in answered]},
        {'role': 'tool', 'name': 'Assistant', 'content': 'ok',
         'tool_responses': [{'tool_call_id': c, 'role': 'tool', 'content': 'posted ' + c}
                            for c in answered]},
        {'role': 'user', 'name': 'StatusVerifier', 'content': '{"status": "pending"}'},
        {'role': 'assistant', 'content': None, 'tool_calls': [_call(c) for c in pending]},
    ])
    handler, seen = _anthropic(_message([{'type': 'text', 'text': 'Posted.'}]))
    _claude(handler).chat.completions.create(
        model=MODEL, tools=[TOOL], messages=[{'role': 'system', 'content': SYSTEM}] + history)
    turns = _sent(seen)['messages']
    _assert_the_api_accepts(turns)
    sent_calls = [b['id'] for t in turns for b in t['content'] if b['type'] == 'tool_use']
    assert sent_calls == answered
    texts = [b['text'] for t in turns for b in t['content'] if b['type'] == 'text']
    assert 'r1\n\nr2' in texts          # the cut call's results still reach the model


def test_results_split_by_another_message_still_lead_their_turn(metered):
    """Parallel calls whose results arrive split up, with a message between
    them: every result goes first in the user turn after the calls, the
    message after them."""
    handler, seen = _anthropic(_message([{'type': 'text', 'text': 'ok'}]))
    _claude(handler).chat.completions.create(model=MODEL, tools=[TOOL], messages=[
        {'role': 'user', 'content': 'Post both notes'},
        {'role': 'assistant', 'content': None, 'tool_calls': [_call('toolu_1'), _call('toolu_2')]},
        {'role': 'tool', 'tool_call_id': 'toolu_1', 'content': 'posted 1'},
        {'role': 'user', 'name': 'StatusVerifier', 'content': '{"status": "pending"}'},
        {'role': 'tool', 'tool_call_id': 'toolu_2', 'content': 'posted 2'},
    ])
    turns = _sent(seen)['messages']
    _assert_the_api_accepts(turns)
    assert [b.get('tool_use_id') or b.get('text') for b in turns[2]['content']] == [
        'toolu_1', 'toolu_2', '{"status": "pending"}']


def test_a_result_whose_call_was_cut_reaches_the_model_as_text(metered):
    """A tool result with no call right before it (here two turns sit
    between them) goes as text, and the call it answered, which had no result
    right after it, is left out: the API refuses both."""
    handler, seen = _anthropic(_message([{'type': 'text', 'text': 'ok'}]))
    _claude(handler).chat.completions.create(model=MODEL, tools=[TOOL], messages=[
        {'role': 'user', 'content': 'Post the note'},
        {'role': 'assistant', 'content': None, 'tool_calls': [_call('toolu_1')]},
        {'role': 'user', 'name': 'StatusVerifier', 'content': '{"status": "pending"}'},
        {'role': 'assistant', 'content': 'Waiting for the channel.'},
        {'role': 'tool', 'tool_call_id': 'toolu_1', 'content': 'posted 1'},
    ])
    turns = _sent(seen)['messages']
    _assert_the_api_accepts(turns)
    assert turns == [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'Post the note'},
                                     {'type': 'text', 'text': '{"status": "pending"}'}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Waiting for the channel.'}]},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'posted 1'}]},
    ]


# ─── One translation detail ─────────────────────────────────────────────

def test_an_inline_image_becomes_a_base64_image_block():
    from core import claude_native
    body = claude_native.messages_body({'model': MODEL, 'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'what is this?'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBORw0KGgo='}}]}]})
    assert body['messages'][0]['content'][1] == {
        'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                                    'data': 'iVBORw0KGgo='}}


# ─── The model the owner pays for: effort, sampling, price ──────────────
# Owner 2026-11-11: "choose the best bang for the buck yourself".  Measured
# on central's agent tasks: Claude Haiku 5.5 at effort 'high' passed 17 of
# 17 at ~1/40 of today's cost; at its default effort it passed 13 of 14.

HAIKU = 'claude-haiku-5-5'


def _sent_body(monkeypatch, model, **extra):
    """What reaches Anthropic for one call to ``model``, the configured expert."""
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_MODEL_NAME', model)
    handler, seen = _anthropic(_message([{'type': 'text', 'text': 'ok'}]))
    _claude(handler).chat.completions.create(
        model=model, messages=[{'role': 'system', 'content': SYSTEM},
                               {'role': 'user', 'content': 'hi'}], **extra)
    return _sent(seen)


def test_the_effort_the_owner_set_reaches_claude(metered, monkeypatch):
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_EFFORT', 'high')
    assert _sent_body(monkeypatch, HAIKU)['output_config'] == {'effort': 'high'}


@pytest.mark.parametrize('effort', ['', 'turbo'])
def test_an_unset_or_unknown_effort_leaves_the_models_default(metered, monkeypatch, effort):
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_EFFORT', effort)
    assert 'output_config' not in _sent_body(monkeypatch, HAIKU)


def test_the_effort_is_only_the_configured_experts(monkeypatch):
    """Another model, even another Claude, keeps its own default."""
    from core import claude_native
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_EFFORT', 'high')
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_MODEL_NAME', HAIKU)
    body = claude_native.messages_body({'model': MODEL,
                                        'messages': [{'role': 'user', 'content': 'hi'}]})
    assert 'output_config' not in body


@pytest.mark.parametrize('model', [HAIKU, 'claude-sonnet-5-5', 'claude-sonnet-5',
                                   'claude-opus-5-5', 'claude-opus-4-8',
                                   'claude-fable-5-2', 'claude-mythos-5-1'])
def test_no_model_is_sent_a_sampling_value_it_would_refuse(metered, monkeypatch, model):
    """Anthropic's reference: Sonnet 5 / 5.5 and Haiku 5.5 refuse a
    non-default temperature or top_p, Opus 4.7 and later, Fable and Mythos
    refuse any (review of 2fc216a38, MF-2).  Left out, the model samples at
    its default."""
    body = _sent_body(monkeypatch, model, temperature=0.2, top_p=0.9)
    assert 'temperature' not in body and 'top_p' not in body


def test_an_older_claude_keeps_its_temperature(metered, monkeypatch):
    assert _sent_body(monkeypatch, 'claude-sonnet-4-6', temperature=0.2)['temperature'] == 0.2


def test_each_claude_model_is_priced_at_its_own_list_price(monkeypatch):
    from core import autogen_config as ac
    monkeypatch.delenv('HEVOLVE_CLAUDE_USD_PER_1K_IN', raising=False)
    monkeypatch.delenv('HEVOLVE_CLAUDE_USD_PER_1K_OUT', raising=False)
    assert ac.model_usd_per_1k(HAIKU) == (0.0001, 0.0005)
    assert ac.model_usd_per_1k(HAIKU, prompt_tokens=150_000) == (0.0005, 0.0025)
    assert ac.model_usd_per_1k(HAIKU, prompt_tokens=100_000) == (0.0001, 0.0005)
    assert ac.model_usd_per_1k(MODEL) == (0.002, 0.010)
    # Review of 2fc216a38, MF-3: every current model at its own list price
    # (Anthropic, 2026-11), the most specific name winning over its prefix.
    assert ac.model_usd_per_1k('claude-opus-5-5') == (0.004, 0.020)
    assert ac.model_usd_per_1k('claude-opus-5') == (0.005, 0.025)
    assert ac.model_usd_per_1k('claude-fable-5-2') == (0.008, 0.040)
    assert ac.model_usd_per_1k('claude-mythos-5-2') == (0.008, 0.040)
    assert ac.model_usd_per_1k('claude-fable-5-1') == (0.010, 0.050)
    assert ac.model_usd_per_1k('claude-fable-5') == (0.010, 0.050)
    assert ac.model_usd_per_1k('claude-opus-4-1') == (0.003, 0.015)   # the family's
    assert ac.model_usd_per_1k('gpt-4o') is None
    # The meter: 1,000 fresh prompt tokens and 1,000 output tokens on Haiku 5.5.
    assert bg.blended_usd_per_1k(HAIKU, 1000, 1000) == pytest.approx((0.0001 + 0.0005) / 2)
    monkeypatch.setenv('HEVOLVE_CLAUDE_USD_PER_1K_IN', '0.001')
    assert ac.model_usd_per_1k(HAIKU)[0] == 0.001                      # the owner's pin wins


def test_the_expert_entry_carries_its_models_price(monkeypatch):
    from core import autogen_config as ac
    monkeypatch.delenv('HEVOLVE_CLAUDE_USD_PER_1K_IN', raising=False)
    monkeypatch.delenv('HEVOLVE_CLAUDE_USD_PER_1K_OUT', raising=False)
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_ENDPOINT_URL', BASE)
    monkeypatch.setenv('HEVOLVE_EXPERT_LLM_MODEL_NAME', HAIKU)
    assert ac.resolve_expert_backend()['price'] == [0.0001, 0.0005]
