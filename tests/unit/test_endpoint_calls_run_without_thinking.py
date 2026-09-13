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
