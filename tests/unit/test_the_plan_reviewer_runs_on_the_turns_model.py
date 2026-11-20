"""The autonomous plan reviewer answers on the turn's model.

Central, 19 Nov 2026 (build c2097bab4): the daemon goal turns run on the
configured expert, claude-haiku-5-5, because the node's own model,
openrouter/free, has its day quota spent (provider breaker open).  A new
plan of such a turn goes to hart_intelligence_entry._review_proposed_plan,
the stateless reviewer call (owner: "the verifier is a stateless LLM call").
It posted to GPT_API with LLM_MODEL_NAME, the node's own model, whatever
the turn ran on, so on central it could only fail, answer 'unreviewed', and
hold the plan for the owner's card: nothing built, no tool run.

The rule: the reviewer runs on the model the turn runs on
(core.autogen_config.turn_chat_model, the list /chat composed with
with_local_fallback, which never names a refusing provider).  A turn that
names no model (a desktop turn) keeps the node's own endpoint exactly as
before: same URL, same model, same 256-token cap.

The expert's cap: on central (19-20 Nov) 13 claude-haiku-5-5 calls stopped
at exactly output=300, the cap of the agent matcher (get_llm
max_tokens=300), whose answer is one agent id; Claude 5 models think by
default and the thinking counts against max_tokens.  The fake expert below
spends 300 tokens before its verdict, as measured.

The HTTP boundary is faked: two live local endpoints.  The reviewer, the
composition, the provider breaker and pooled_post are the real ones; the
LangChain chat class, absent from the test venv, is a stand-in that sends
the same openai-SDK request (_OpenAIChat).
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
# What central's claude-haiku-5-5 spent on a short answer under a 300 cap.
_EXPERT_THINKING_TOKENS = 300

_VERDICT = {'status': 'needs_refinement', 'flow_name': 'Outreach',
            'feedback': 'step 2 must name the tool that sends the email'}


def _completion(model, content, finish='stop'):
    return {
        'id': 'chatcmpl-1', 'object': 'chat.completion', 'created': 1,
        'model': model,
        'choices': [{'index': 0, 'finish_reason': finish,
                     'message': {'role': 'assistant', 'content': content}}],
        'usage': {'prompt_tokens': 1, 'completion_tokens': 1,
                  'total_tokens': 2},
    }


class _Endpoints(BaseHTTPRequestHandler):
    """/expert thinks, then answers a verdict; /own answers a verdict, or
    the 429 of a spent day quota when ``own_refuses``."""
    calls = []
    own_refuses = False

    def do_POST(self):                                   # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
        type(self).calls.append({'path': self.path, 'model': body.get('model'),
                                 'max_tokens': body.get('max_tokens')})
        verdict = json.dumps(_VERDICT)
        if self.path.startswith('/expert'):
            cap = body.get('max_tokens')
            if cap is not None and cap < _EXPERT_THINKING_TOKENS + 50:
                payload, status = _completion(body.get('model'), '', 'length'), 200
            else:
                payload, status = _completion(body.get('model'), verdict), 200
        elif type(self).own_refuses:
            payload = {'error': {'message': 'Rate limit exceeded: free-models-per-day',
                                 'code': 429}}
            status = 429
        else:
            payload, status = _completion(body.get('model'), verdict), 200
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class _Reply:
    def __init__(self, text):
        self.content = text


class _OpenAIChat:
    """hart_intelligence_entry's ChatOpenAI where the test venv has none.

    On central it is langchain_community.chat_models.openai.ChatOpenAI
    (re-exported by langchain_classic.chat_models), which this venv lacks, so
    the module binds a stub that refuses every call.  This stand-in takes the
    constructor arguments core.autogen_config._chat_model_kwargs gives it and
    sends the request that class sends: one openai-SDK chat completion to the
    entry's endpoint, the next model tried when one raises."""

    def __init__(self, model_name, openai_api_key, temperature, max_tokens,
                 openai_api_base=None, max_retries=2, model_kwargs=None):
        import openai
        self._client = openai.OpenAI(api_key=openai_api_key, base_url=openai_api_base,
                                     max_retries=max_retries)
        self._args = dict(model=model_name, temperature=temperature,
                          max_tokens=max_tokens, **(model_kwargs or {}))
        self._fallbacks = []

    def with_fallbacks(self, fallbacks):
        self._fallbacks = list(fallbacks)
        return self

    def invoke(self, prompt):
        for i, model in enumerate([self] + self._fallbacks):
            try:
                resp = model._client.chat.completions.create(
                    messages=[{'role': 'user', 'content': prompt}], **model._args)
                return _Reply(resp.choices[0].message.content)
            except Exception:
                if i == len(self._fallbacks):
                    raise


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


@pytest.fixture
def endpoints(hie, monkeypatch):
    """The expert and the node's own model, live; the own model pinned as the
    node's backend and as the reviewer's GPT_API."""
    monkeypatch.setattr(hie, 'ChatOpenAI', _OpenAIChat)
    _Endpoints.calls = []
    _Endpoints.own_refuses = False
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
    monkeypatch.setattr(hie, 'GPT_API', own['base_url'] + '/chat/completions')
    monkeypatch.setattr(hie, 'LLM_MODEL_NAME', own['model'])
    monkeypatch.setattr(hie, 'LLM_AUTH_HEADERS', {})
    thread_local_data.clear_model_config_override()
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
    _Endpoints.own_refuses = True
    for _ in range(3):
        llm_provider_breaker.record_failure(_OWN_HOST)


def _expert_turn(expert):
    """The turn's model list, set as /chat sets it for a dispatched turn."""
    thread_local_data.set_model_config_override(
        autogen_config.with_local_fallback([dict(expert)]))


def _plan():
    return {'status': 'proposed_plan', 'name': 'textile supplier sourcing',
            'flows': [{'flow_name': 'Outreach',
                       'actions': [{'action_id': 1, 'action': 'find suppliers'},
                                   {'action_id': 2, 'action': 'email them'}]}]}


def _dialled(prefix):
    return [c for c in _Endpoints.calls if c['path'].startswith(prefix)]


def test_an_expert_turns_plan_is_reviewed_by_the_expert(hie, endpoints):
    """The live 19 Nov failure: the own model refuses, the turn runs on the
    expert, and the reviewer must answer the expert's verdict, never post to
    the refusing own model and hold the plan as unreachable."""
    expert, own = endpoints
    _own_model_refusing()
    _expert_turn(expert)
    verdict = hie._review_proposed_plan(_plan(), 3)
    assert verdict == ('needs_refinement',
                       "In flow 'Outreach': step 2 must name the tool that "
                       "sends the email"), (
        'an expert turn\'s plan was not reviewed by the expert (on central '
        'every such plan was held as "the reviewer could not be reached").  '
        'got %r, calls %r' % (verdict, _Endpoints.calls))
    assert [c['model'] for c in _dialled('/expert')] == ['claude-haiku-5-5']
    assert _dialled('/own') == [], (
        'the reviewer dialled the own model while its provider refuses calls')


def test_the_expert_has_room_to_think_before_its_verdict(hie, endpoints):
    """A thinking model's verdict is not cut off: the expert spends what
    central's claude-haiku-5-5 spent on a short answer, then answers."""
    expert, own = endpoints
    _expert_turn(expert)
    verdict = hie._review_proposed_plan(_plan(), 3)
    assert verdict[0] == 'needs_refinement', (
        'the expert\'s verdict was cut off by the token cap: %r, calls %r'
        % (verdict, _Endpoints.calls))
    assert _dialled('/own') == []


def test_a_turn_that_names_no_model_keeps_the_nodes_own_endpoint(hie, endpoints):
    """A desktop turn: no turn model, so the reviewer posts to the node's own
    endpoint exactly as before (its model, its 256-token cap), and never to
    another model."""
    expert, own = endpoints
    verdict = hie._review_proposed_plan(_plan(), 3)
    assert verdict[0] == 'needs_refinement'
    assert _dialled('/expert') == []
    assert _Endpoints.calls == [{'path': '/own/v1/chat/completions',
                                 'model': 'openrouter/free', 'max_tokens': 256}]
