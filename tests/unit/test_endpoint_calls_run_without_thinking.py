"""Agent calls to the configured endpoint run with thinking off.

Measured on central 2026-09-13 (task #93) with a realistic agent turn (895
prompt tokens) against the hosted Qwen endpoint:

    thinking on, no cap     5,685 completion tokens, 47.1 s, 24.7k chars of reasoning
    thinking off, no cap      196 completion tokens,  3.2 s, a complete JSON reply
    thinking on, 300 cap    finish=length, content '' (the empty agent replies)

autogen sends no max_tokens, so every agent reply cost ~5.7k tokens against a
shared, rate-limited endpoint, and any cap emptied the reply. The endpoint
entry now carries LLM_THINKING_OFF_KWARGS; OpenAI and Azure OpenAI, which
reject unknown request fields, are left alone.

Run:
  pytest tests/unit/test_endpoint_calls_run_without_thinking.py -q
"""
import inspect
import os
import sys
from unittest.mock import patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.constants import LLM_THINKING_OFF_KWARGS  # noqa: E402

_CLEAR = {'HEVOLVE_NODE_TIER': '', 'HEVOLVE_ACTIVE_CLOUD_PROVIDER': '',
          'HEVOLVE_LLM_ENDPOINT_URL': '', 'HEVOLVE_LLM_API_KEY': '',
          'HEVOLVE_LLM_MODEL_NAME': ''}


def _resolve(**env):
    from core.autogen_config import resolve_llm_backend
    with patch.dict(os.environ, {**_CLEAR, **env}), \
            patch('core.port_registry.get_local_llm_url',
                  return_value='http://127.0.0.1:8080/v1'):
        return resolve_llm_backend()


def _central(endpoint):
    return _resolve(HEVOLVE_NODE_TIER='central', HEVOLVE_LLM_ENDPOINT_URL=endpoint,
                    HEVOLVE_LLM_MODEL_NAME='qwen', HEVOLVE_LLM_API_KEY='k')


def test_the_configured_endpoint_turns_thinking_off():
    kind, entry = _central('https://qwen.example-host.net/v1')
    assert kind == 'api'
    assert entry['extra_body'] == {'chat_template_kwargs': LLM_THINKING_OFF_KWARGS}
    assert entry['extra_body']['chat_template_kwargs'] is not LLM_THINKING_OFF_KWARGS, (
        'a caller mutating the request must not change the shared constant')


def test_openrouter_gets_its_own_reasoning_switch():
    """OpenRouter has no chat_template_kwargs; it reads reasoning.effort, and
    the effort is the shortest every routed model accepts ('low'), not
    'none' (see the next test)."""
    from core.constants import LLM_SHORT_REASONING
    kind, entry = _central('https://openrouter.ai/api/v1')
    assert kind == 'api'
    assert entry['extra_body'] == {'reasoning': {'effort': 'low'}}
    assert entry['extra_body']['reasoning'] is not LLM_SHORT_REASONING
    assert 'chat_template_kwargs' not in entry['extra_body']


_MANDATORY = 'Reasoning is mandatory for this endpoint and cannot be disabled.'


@pytest.fixture
def reasoning_mandatory_endpoint():
    """A loopback endpoint that answers as a reasoning-mandatory OpenRouter
    model did on central: 400 with OpenRouter's message for effort 'none',
    a completion for anything else."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if (body.get('reasoning') or {}).get('effort') == 'none':
                code, reply = 400, {'error': {'message': _MANDATORY, 'code': 400}}
            else:
                code, reply = 200, {
                    'id': 'c', 'object': 'chat.completion', 'created': 0,
                    'model': body.get('model'),
                    'choices': [{'index': 0, 'finish_reason': 'stop',
                                 'message': {'role': 'assistant', 'content': 'OK'}}],
                    'usage': {'prompt_tokens': 5, 'completion_tokens': 1,
                              'total_tokens': 6}}
            raw = json.dumps(reply).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_address[1]}/api/v1'
    server.shutdown()
    server.server_close()


def test_the_openrouter_entry_is_accepted_where_reasoning_is_mandatory(
        reasoning_mandatory_endpoint):
    """Measured on central 2026-10-27 00:01 UTC (openrouter/free, one
    tool-carrying call per row, three per value): effort 'none' -> 200, 200,
    400 "Reasoning is mandatory for this endpoint and cannot be disabled.";
    effort 'low' -> 200 x3 (41-65 reasoning tokens); no field -> 200 x3.
    On 2026-10-26 'none' had failed 3 of 4.  The free router picks a model
    per call, so 'none' failed whenever it picked one that must think.  The
    node's own OpenRouter entry, sent by the openai SDK the agents use, is
    answered."""
    openai = pytest.importorskip('openai')
    _kind, entry = _central('https://openrouter.ai/api/v1')
    client = openai.OpenAI(api_key='k', base_url=reasoning_mandatory_endpoint,
                           max_retries=0)
    reply = client.chat.completions.create(
        model=entry['model'], messages=[{'role': 'user', 'content': 'Say OK.'}],
        extra_body=entry['extra_body'])
    assert reply.choices[0].message.content == 'OK'


@pytest.mark.parametrize('endpoint', ['https://api.openai.com/v1',
                                      'https://my-res.openai.azure.com/openai'])
def test_openai_and_azure_get_no_template_kwargs(endpoint):
    kind, entry = _central(endpoint)
    assert kind == 'api'
    assert 'extra_body' not in entry


def test_provider_and_local_entries_are_unchanged():
    kind, entry = _resolve(HEVOLVE_ACTIVE_CLOUD_PROVIDER='openai', HEVOLVE_LLM_API_KEY='k',
                           HEVOLVE_LLM_MODEL_NAME='gpt')
    assert kind == 'api' and 'extra_body' not in entry
    kind, entry = _resolve()
    assert kind == 'local' and 'extra_body' not in entry


def test_the_openai_client_sends_extra_body_with_the_request():
    """autogen 0.2.x gives the client constructor only its keyword args and
    passes every other config key to completions.create(); extra_body only
    reaches the request if it is not a constructor argument and create()
    accepts it."""
    openai = pytest.importorskip('openai')
    ctor = set(inspect.getfullargspec(openai.OpenAI.__init__).kwonlyargs)
    assert 'extra_body' not in ctor
    from openai.resources.chat.completions import Completions
    assert 'extra_body' in inspect.signature(Completions.create).parameters
