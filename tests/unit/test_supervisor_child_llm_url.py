"""#137 (HARTOS half): the hevolveai child inherits the canonical local-LLM URL.

WHY: HevolveAI's QwenAutoEncoder was spawning its OWN llama-server on :8080,
redundant with HARTOS's. The HARTOS-side contract is to hand the child the ONE
canonical URL (port_registry.get_local_llm_url -- the 4-tier resolver, distinct
from get_local_draft_url) via _build_env, so the sibling can read
HEVOLVE_LOCAL_LLM_URL and skip its own server. (The sibling consumer change is
in the hevolveai repo -- this is only the env-contract half.)

Behavioral: call the real _build_env with a fake self, mock the resolver
boundary, assert the env it returns. Covers: injects when absent; preserves an
operator override (resolver NOT invoked); fail-open when the resolver raises.
"""
import os
import sys
import types
from unittest.mock import patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.agent_engine.hevolveai_supervisor import _Supervisor  # noqa: E402


def _fake_self():
    return types.SimpleNamespace(
        api_url='http://127.0.0.1:8000', port=8000, pythonpath='')


def test_injects_canonical_llm_url_when_absent():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('HEVOLVE_LOCAL_LLM_URL', None)
        with patch('core.port_registry.get_local_llm_url',
                   return_value='http://127.0.0.1:8080/v1'):
            env = _Supervisor._build_env(_fake_self())
    assert env['HEVOLVE_LOCAL_LLM_URL'] == 'http://127.0.0.1:8080/v1'


def test_preserves_operator_override():
    with patch.dict(os.environ,
                    {'HEVOLVE_LOCAL_LLM_URL': 'http://operator:9999'},
                    clear=False):
        with patch('core.port_registry.get_local_llm_url',
                   return_value='http://resolver:8080') as m:
            env = _Supervisor._build_env(_fake_self())
    assert env['HEVOLVE_LOCAL_LLM_URL'] == 'http://operator:9999'
    m.assert_not_called()  # override present -> resolver never invoked


def test_resolver_failure_is_fail_open():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('HEVOLVE_LOCAL_LLM_URL', None)
        with patch('core.port_registry.get_local_llm_url',
                   side_effect=RuntimeError('boom')):
            env = _Supervisor._build_env(_fake_self())  # must not raise
    assert not env.get('HEVOLVE_LOCAL_LLM_URL')  # absent/empty, no crash
