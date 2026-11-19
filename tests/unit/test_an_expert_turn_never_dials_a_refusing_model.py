"""A goal turn on the expert never dials a model that is refusing calls.

Measured on central, 19 Nov 2026, 19:20-20:20Z (build 089bf4de0).  Goals
09bbc67a (marketing), 74416785 and fb84cdce (outreach) and 5ba50f98 (sales)
all run their turns on the configured expert, claude-haiku-5-5; the node's
own model is openrouter/free, whose day quota was spent (its provider breaker
OPEN).  /chat composes the turn's model list with with_local_fallback, which
gave every one of those turns [claude-haiku-5-5, openrouter/free].  Two
paths still reached openrouter inside those turns:

1. autogen.  Each of the 6 Anthropic 400s in 6 h was followed within 0.5 s by
   three openrouter 429s (the SDK's two retries): OpenAIWrapper falls to the
   next config_list entry, the refusing own model, and the turn ends on
   openai.RateLimitError (create_recipe send_retry -> initiate_chat).  The
   Anthropic error that ended the turn was never seen.
2. LangChain.  get_llm, the one factory the agent matcher, the planner and
   dispatch_to_agent use, ignored the turn's model: turns 5ba50f98 and
   fb84cdce made 0 Anthropic calls and their matcher posted to openrouter.

The rule: every LLM a turn builds runs on the turn's model list, and that
list never names the node's own model while its provider refuses calls.
With the own model healthy it stays the last entry: the owner's fallback
for an expert call the day cap refuses (revenue_expert_config).

Real composition (with_local_fallback), real provider breaker, real autogen
OpenAIWrapper against two live HTTP endpoints; only the node's own backend
is pinned, and the LangChain chat class is the mocked boundary.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core import autogen_config  # noqa: E402
from core.circuit_breaker import llm_provider_breaker  # noqa: E402
from hartos.threadlocal import thread_local_data  # noqa: E402

# The breaker is keyed by host without the port, so the expert and the own
# model need two hosts on this box: 127.0.0.1 and localhost.
_OWN_HOST = 'localhost'


class _Endpoints(BaseHTTPRequestHandler):
    """/expert answers Anthropic's 400; /own answers and counts."""
    own_calls = 0

    def do_POST(self):                                   # noqa: N802
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        if self.path.startswith('/expert'):
            body = {'type': 'error',
                    'error': {'type': 'invalid_request_error',
                              'message': 'messages: a refused request'}}
            self.send_response(400)
        else:
            type(self).own_calls += 1
            body = {
                'id': 'chatcmpl-own-1', 'object': 'chat.completion',
                'created': 1, 'model': 'openrouter/free',
                'choices': [{'index': 0, 'finish_reason': 'stop',
                             'message': {'role': 'assistant',
                                         'content': 'answered by the own model'}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1,
                          'total_tokens': 2},
            }
            self.send_response(200)
        raw = json.dumps(body).encode()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture
def endpoints(monkeypatch):
    """The expert and the node's own model, live; the own model pinned as
    the node's backend; the own host's breaker reset before and after."""
    _Endpoints.own_calls = 0
    srv = HTTPServer(('127.0.0.1', 0), _Endpoints)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    expert = {'model': 'claude-haiku-5-5', 'api_key': 'dummy',
              'base_url': 'http://127.0.0.1:%d/expert/v1' % port,
              'max_retries': 0}
    own = {'model': 'openrouter/free', 'api_key': 'dummy',
           'base_url': 'http://%s:%d/own/v1' % (_OWN_HOST, port),
           'max_retries': 0}
    monkeypatch.setattr(autogen_config, 'get_autogen_config_list',
                        lambda: [dict(own)])
    llm_provider_breaker.reset(_OWN_HOST)
    try:
        yield expert, own
    finally:
        llm_provider_breaker.reset(_OWN_HOST)
        thread_local_data.clear_model_config_override()
        srv.shutdown()
        srv.server_close()


def _own_model_refusing():
    """The wire's reading of a spent quota: three refusals open the host's
    breaker (core.llm_outbound_logger._feed_provider_breaker)."""
    for _ in range(3):
        llm_provider_breaker.record_failure(_OWN_HOST)


def _expert_turn(expert):
    """The turn's model list, set as /chat sets it for a dispatched turn."""
    turn = autogen_config.with_local_fallback([dict(expert)])
    thread_local_data.set_model_config_override(turn)
    return turn


def _ask(turn):
    import autogen
    wrapper = autogen.OpenAIWrapper(config_list=turn, cache_seed=None)
    return wrapper.create(messages=[{'role': 'user', 'content': 'hi'}])


def test_an_expert_400_never_falls_to_the_refusing_own_model(endpoints):
    """The live 19 Nov failure: the expert's 400 must end the call as the
    400, with not one request to the own model whose quota is spent."""
    import openai
    expert, own = endpoints
    _own_model_refusing()
    turn = _expert_turn(expert)
    assert [e['model'] for e in turn] == ['claude-haiku-5-5'], (
        'the turn runs on the expert because the own model is refusing; its '
        'model list must not name that model again.  got %r'
        % ([e['model'] for e in turn],))
    with pytest.raises(openai.BadRequestError):
        _ask(turn)
    assert _Endpoints.own_calls == 0, (
        'an expert 400 fell to the own model while its provider refuses '
        'calls (on central: 3 openrouter 429s per expert 400)')


def test_a_healthy_own_model_stays_the_cap_fallback(endpoints):
    """Non-vacuity, and the owner's policy kept: with the own model answering,
    it is still the last entry, and a failed expert call lands on it."""
    expert, own = endpoints
    turn = _expert_turn(expert)
    assert [e['model'] for e in turn] == ['claude-haiku-5-5', 'openrouter/free']
    resp = _ask(turn)
    assert resp.choices[0].message.content == 'answered by the own model'
    assert _Endpoints.own_calls == 1


class _ChatModel:
    """The LangChain chat class at its boundary: records what it was built
    with and what it falls back to."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.fallbacks = []

    def with_fallbacks(self, fallbacks):
        self.fallbacks = list(fallbacks)
        return self


def _dials(model):
    return [(m.kwargs.get('model_name'), m.kwargs.get('openai_api_base'))
            for m in [model] + model.fallbacks]


def test_the_turns_langchain_model_is_the_turns_model(endpoints):
    """get_llm's turn: the agent matcher, the planner and dispatch_to_agent
    run on the expert, never on the refusing own model."""
    expert, own = endpoints
    _own_model_refusing()
    _expert_turn(expert)
    model = autogen_config.turn_chat_model(_ChatModel, temperature=0.1,
                                           max_tokens=300)
    assert _dials(model) == [('claude-haiku-5-5', expert['base_url'])], (
        'the LangChain model of an expert turn must dial the expert only; '
        'on central the matcher of turns 5ba50f98 and fb84cdce posted to '
        'openrouter/free.  got %r' % (_dials(model),))
    assert model.kwargs['openai_api_key'] == expert['api_key']
    assert model.kwargs['temperature'] == 0.1
    assert model.kwargs['max_tokens'] == 300


def test_the_langchain_model_falls_back_as_autogen_does(endpoints):
    """With the own model answering, the LangChain model falls to it exactly
    as the turn's autogen agents do: one composition for both."""
    expert, own = endpoints
    _expert_turn(expert)
    model = autogen_config.turn_chat_model(_ChatModel)
    assert _dials(model) == [('claude-haiku-5-5', expert['base_url']),
                             ('openrouter/free', own['base_url'])]


def test_a_turn_that_names_no_model_keeps_the_nodes_own(endpoints):
    """No model named for the turn: get_llm builds the node's own as before."""
    thread_local_data.clear_model_config_override()
    assert autogen_config.turn_chat_model(_ChatModel) is None
