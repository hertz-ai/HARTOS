"""Metering must follow record_metered_usage's real contract and must never cost
the call it meters (hevolveai Master 11.435 S1/S2).

Both production callers passed keywords record_metered_usage does not accept.
In the coding adapter the TypeError escaped `except ImportError` and the
completion came back None, so every native coding edit failed; in the SDK
proxy it was swallowed and nothing was metered.
"""
import ast
import inspect
import os
import sys
import types
from unittest.mock import patch

import pytest

ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, ROOT)

from integrations.agent_engine import budget_gate  # noqa: E402


PRODUCTION_FILES = [
    'integrations/coding_agent/aider_core/hart_model_adapter.py',
    'hart_intelligence_entry.py',
    'integrations/agent_engine/budget_gate.py',
]


def _calls(path, name):
    tree = ast.parse(open(os.path.join(ROOT, path), encoding='utf-8').read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            called = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)
            if called == name:
                yield node


@pytest.mark.parametrize('callee', ['record_metered_usage', 'meter_llm_call'])
def test_every_production_call_uses_only_real_keywords(callee):
    params = set(inspect.signature(getattr(budget_gate, callee)).parameters)
    seen = 0
    for path in PRODUCTION_FILES:
        for call in _calls(path, callee):
            seen += 1
            bad = {k.arg for k in call.keywords if k.arg} - params
            assert not bad, f"{path}:{call.lineno} {callee}() got {sorted(bad)}"
    assert seen, f"no production call of {callee} found: the check selected nothing"


def test_meter_llm_call_prices_local_at_zero_and_cloud_from_spark(monkeypatch):
    got = []
    monkeypatch.setattr(budget_gate, 'record_metered_usage',
                        lambda **kw: got.append(kw) or 'id')
    monkeypatch.setenv('HEVOLVE_NODE_ID', 'node-x')
    monkeypatch.setenv('HEVOLVE_SPARK_PER_USD', '100')

    monkeypatch.setattr(budget_gate, '_is_local_model', lambda: True)
    budget_gate.meter_llm_call('qwen', 10, 20, task_source='own')
    assert got[-1]['cost_per_1k'] == 0.0 and got[-1]['node_id'] == 'node-x'

    monkeypatch.setattr(budget_gate, '_is_local_model', lambda: False)
    monkeypatch.setattr(budget_gate, '_resolve_model_name', lambda m: m)
    budget_gate.meter_llm_call('gpt-4o', 1000, 1000, task_source='hive')
    kw = got[-1]
    assert kw['cost_per_1k'] == pytest.approx(budget_gate.spark_per_1k('gpt-4o') / 100)
    assert (kw['tokens_in'], kw['tokens_out'], kw['task_source']) == (1000, 1000, 'hive')


def test_meter_llm_call_never_raises(monkeypatch):
    def boom(**kw):
        raise TypeError('contract drift')
    monkeypatch.setattr(budget_gate, 'record_metered_usage', boom)
    monkeypatch.setattr(budget_gate, '_is_local_model', lambda: True)
    assert budget_gate.meter_llm_call('m', 1, 1) is None


def _fake_openai(text):
    usage = types.SimpleNamespace(prompt_tokens=7, completion_tokens=3)
    msg = types.SimpleNamespace(content=text)
    resp = types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)],
                                 usage=usage)
    client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=lambda **kw: resp)))
    return types.SimpleNamespace(OpenAI=lambda **kw: client)


def test_a_completion_survives_metering(monkeypatch):
    from integrations.coding_agent.aider_core import hart_model_adapter as hma
    monkeypatch.setitem(sys.modules, 'openai', _fake_openai('EDIT BLOCK'))
    monkeypatch.setattr('core.autogen_config.get_autogen_config_list',
                        lambda: [{'api_key': 'k', 'base_url': 'http://x', 'model': 'm'}])
    monkeypatch.setattr(budget_gate, '_is_local_model', lambda: True)
    # the real meter_llm_call and record_metered_usage: the old keywords raised
    assert hma.send_completion([{'role': 'user', 'content': 'x'}]) == 'EDIT BLOCK'
    # and a metering layer that raises still does not cost the completion
    monkeypatch.setattr(budget_gate, 'meter_llm_call',
                        lambda **kw: (_ for _ in ()).throw(RuntimeError('down')))
    assert hma.send_completion([{'role': 'user', 'content': 'x'}]) == 'EDIT BLOCK'
