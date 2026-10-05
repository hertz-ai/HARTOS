"""While the owner's copilot switch is off, the coding router's default
harness is Hermes; every other step of the selection keeps its meaning.

Measured 2026-10-05: 30 headless `claude -p` runs came from the coding
dispatcher on a node whose switch had been off since 2026-09-16.  With the
launcher fixed those runs are refused, so what the router offers in
claude_code's place is now a decision, and it is Hermes.

    python -m pytest tests/unit/test_router_defaults_to_hermes_when_copilot_off.py -q
"""
import pytest

import integrations.coding_agent.claude_code_backend as cc
from integrations.coding_agent.tool_router import (
    COPILOT_OFF_DEFAULT, HEURISTIC_DEFAULTS, CodingToolRouter)


class _B:
    def __init__(self, name):
        self.name = name


def _offer(monkeypatch, names, copilot_on):
    """The router sees exactly `names`, the switch is `copilot_on`, and no
    benchmark or hive row decides anything."""
    monkeypatch.setattr('integrations.coding_agent.tool_router.get_available_backends',
                        lambda: {n: _B(n) for n in names})
    monkeypatch.setattr(cc, 'copilot_enabled', lambda: copilot_on)
    monkeypatch.setattr(CodingToolRouter, '_check_local_benchmarks',
                        lambda self, t, a: None)
    monkeypatch.setattr(CodingToolRouter, '_check_hive_intelligence',
                        lambda self, t, a: None)


def test_the_default_is_hermes():
    assert COPILOT_OFF_DEFAULT == 'hermes'


@pytest.mark.parametrize('task_type', sorted(
    t for t, n in HEURISTIC_DEFAULTS.items() if n == 'claude_code'))
def test_a_claude_code_row_goes_to_hermes_when_the_switch_is_off(monkeypatch, task_type):
    _offer(monkeypatch, ['hermes', 'kilocode', 'pi'], copilot_on=False)
    assert CodingToolRouter().route('t', task_type).name == 'hermes'


def test_the_same_row_stays_claude_code_when_the_switch_is_on(monkeypatch):
    _offer(monkeypatch, ['claude_code', 'hermes'], copilot_on=True)
    assert CodingToolRouter().route('t', 'code_review').name == 'claude_code'


def test_other_heuristic_rows_keep_their_tool_when_the_switch_is_off(monkeypatch):
    _offer(monkeypatch, ['hermes', 'aider_native', 'kilocode'], copilot_on=False)
    assert CodingToolRouter().route('t', 'refactor').name == 'aider_native'
    assert CodingToolRouter().route('t', 'feature').name == 'kilocode'


def test_an_unlisted_task_type_falls_to_hermes_not_the_first_tool(monkeypatch):
    _offer(monkeypatch, ['kilocode', 'pi', 'hermes'], copilot_on=False)
    assert CodingToolRouter().route('t', 'no_such_task_type').name == 'hermes'


def test_an_unlisted_task_type_keeps_first_available_when_the_switch_is_on(monkeypatch):
    _offer(monkeypatch, ['kilocode', 'pi', 'hermes'], copilot_on=True)
    assert CodingToolRouter().route('t', 'no_such_task_type').name == 'kilocode'


def test_without_hermes_installed_selection_is_unchanged(monkeypatch):
    _offer(monkeypatch, ['kilocode', 'pi'], copilot_on=False)
    assert CodingToolRouter().route('t', 'code_review').name == 'kilocode'


def test_an_explicit_override_still_wins(monkeypatch):
    _offer(monkeypatch, ['hermes', 'pi'], copilot_on=False)
    assert CodingToolRouter().route('t', 'code_review', user_override='pi').name == 'pi'


def test_benchmark_data_still_outranks_the_default(monkeypatch):
    _offer(monkeypatch, ['hermes', 'opencode'], copilot_on=False)
    monkeypatch.setattr(CodingToolRouter, '_check_local_benchmarks',
                        lambda self, t, a: a['opencode'])
    assert CodingToolRouter().route('t', 'code_review').name == 'opencode'
