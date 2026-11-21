"""A node with no coding tool installed is not offered the coding tools.

Live on central, 2026-11-21 02:37Z, goal 4f294fa3 (the rice buyer sales
agent): action 2 was planned as "execute_coding_task: read
trade/leads/rice_importers.csv and trade/leads/hartos_import_rice_importers.json
and report row counts and emails".  The tool answered "No coding tools
installed. Install one: kilocode, claude_code, opencode, pi, hermes,
aider_native." (integrations/coding_agent/orchestrator.py, _execute_local).
The agent then read both files with get_user_uploaded_file, but the step
names execute_coding_task, a tool the agents could serve
(hartos.lifecycle_hooks.action_named_tools reads _function_map, the schema
and _hart_core_tools), so the step stayed open and the goal paused
needs_help after 3 tries.

Cause: core.agent_tools.build_core_tool_closures built the coding closures
on every node, and CREATE registers them always-on (CREATE_LEG_EXTRA_TOOLS),
while nothing asked tool_backends.get_available_backends(), the one "is any
coding tool installed" check.  get_repository_map's import gate never fires:
recipe_bridge imports without aider_core, and its map then answers
"Repository map not available (aider_core not installed)".

The rule pinned here: the factory builds the four coding closures only where
a coding tool can run; a desktop with one installed keeps exactly today's
list; validate_json_response stays everywhere; and a call to a coding tool on
a node without one is refused with what to do instead.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from integrations.coding_agent import tool_backends as tb

CODING_TOOLS = {'execute_coding_task', 'get_repository_map',
                'create_code_shard', 'get_coding_benchmarks'}

LIVE_STEP = ('execute_coding_task: read trade/leads/rice_importers.csv and '
             'trade/leads/hartos_import_rice_importers.json and report row '
             'counts and emails')


def _ctx():
    return {
        'user_id': '999', 'prompt_id': '8888', 'agent_data': {},
        'helper_fun': MagicMock(), 'user_prompt': '999_8888',
        'request_id_list': {'999_8888': 'req1'}, 'recent_file_id': {},
        'scheduler': MagicMock(), 'send_message_to_user1': MagicMock(),
        'retrieve_json': MagicMock(return_value={}),
        'strip_json_values': MagicMock(return_value=''),
        'save_conversation_db': MagicMock(return_value='1'),
    }


@pytest.fixture
def installed(monkeypatch):
    """Set which coding tools this node has (the get_available_backends
    boundary), with the short-lived answer cleared."""
    calls = []

    def _set(backends):
        monkeypatch.setattr(tb, '_coding_tool_seen',
                            {'at': None, 'answer': False}, raising=False)

        def fake():
            calls.append(1)
            return dict(backends)
        monkeypatch.setattr(tb, 'get_available_backends', fake)
        return calls
    return _set


def _names(installed_backends, installed):
    installed(installed_backends)
    from core.agent_tools import build_core_tool_closures
    return [name for name, _d, _f in build_core_tool_closures(_ctx())]


def test_no_coding_tool_installed_builds_no_coding_closure(installed):
    names = _names({}, installed)
    assert not CODING_TOOLS & set(names), names
    # The CREATE leg's other always-on extra, and the file reader the live
    # agent finished the step with, are still there.
    assert 'validate_json_response' in names
    assert 'get_user_uploaded_file' in names


def test_a_desktop_with_a_coding_tool_keeps_todays_list(installed):
    with_tool = _names({'hermes': object()}, installed)
    without = _names({}, installed)
    assert CODING_TOOLS <= set(with_tool)
    # Nothing else moves: the only difference is the four coding closures.
    assert [n for n in with_tool if n not in CODING_TOOLS] == without


def test_the_live_step_is_not_held_for_a_tool_the_node_cannot_run(installed):
    """The step checker's servable set is what the agents hold; built on
    central, it no longer holds execute_coding_task, so the live step reads
    as naming no tool and the get_user_uploaded_file work can complete it."""
    from core.agent_tools import build_core_tool_closures
    from hartos.lifecycle_hooks import action_named_tools

    def seat():
        return SimpleNamespace(_function_map={}, llm_config={'tools': []},
                               _hart_core_tools=build_core_tool_closures(_ctx()))

    installed({})
    assert action_named_tools([seat()], LIVE_STEP)[1] == []
    installed({'hermes': object()})
    assert 'execute_coding_task' in action_named_tools([seat()], LIVE_STEP)[1]


def test_the_check_is_asked_once_per_window_not_per_build(installed):
    calls = installed({})
    assert tb.coding_tool_installed() is False
    assert tb.coding_tool_installed() is False
    assert len(calls) == 1
    assert tb.coding_tool_installed(max_age_s=0) is False
    assert len(calls) == 2


def test_a_coding_tool_call_on_such_a_node_says_what_to_do(installed):
    from hartos.helper import executor_refusal
    installed({})
    reply = executor_refusal('execute_coding_task',
                             {'get_user_uploaded_file': None,
                              'send_message_to_user': None})
    assert reply.startswith('Error:')
    assert 'no coding tool is installed' in reply
    assert 'get_user_uploaded_file' in reply and 'full path' in reply
    # A desktop that has one but did not attach it keeps the plain refusal.
    installed({'hermes': object()})
    plain = executor_refusal('execute_coding_task',
                             {'get_user_uploaded_file': None})
    assert 'no coding tool is installed' not in plain
