"""create_action_with_ledger must accept the action shape prompts actually carry.

THE LIVE FAILURE (2026-09-13, installed Nunba, agent 87400889007): flow 0
was authored and its recipe banked, then [NEXT-FLOW] crashed:

    create_recipe.py:5154  safe_increment_flow(user_prompt, prompt_id)
    create_recipe.py:6384  increment_current_flow(user_prompt, prompt_id)
    create_recipe.py:6365  user_tasks[user_prompt] = create_action_with_ledger(
    create_recipe.py:4095  task_id = f"action_{action.get('action_id', 'unknown')}"
    AttributeError: 'str' object has no attribute 'get'

and the user was told "I couldn't finish that: 'str' object has no attribute
'get'".

The flow's actions are plain strings.  307 of the 749 prompt configs on the
box carry string actions, including every one of the 151 multi-flow configs.
A fresh ledger goes through agent_ledger.create_ledger_from_actions, which
accepts strings; the "ledger already exists" branch rebuilt the same Tasks in
its own loop and never learned the string case.  increment_current_flow runs
while the previous flow's ledger is still registered, so every multi-flow
agent reaches that branch at its first flow boundary.
"""

import sys
from unittest.mock import MagicMock, patch

import hartos.create_recipe  # noqa: F401  (ensure cached)
from agent_ledger import InMemoryBackend, create_ledger_from_actions

cr = sys.modules['hartos.create_recipe']

PROMPT = '87400889007'
USER_PROMPT = 'u_87400889007'
FLOW_0 = ['verify_device_permissions', 'fetch_current_location',
          'query_weather_api']
FLOW_1 = ['verify_device_permissions', 'fetch_activity_metrics']


def _ids(ledger):
    return sorted(ledger.tasks, key=lambda t: int(t.split('_', 1)[1]))


def _ledger_for(actions):
    return create_ledger_from_actions(
        agent_id=PROMPT, session_id='u_87400889007_1', actions=actions,
        backend=InMemoryBackend())


def _build_on_existing(ledger, actions, flow_id):
    app = MagicMock()
    cr.user_ledgers[USER_PROMPT] = ledger
    cr.user_delegation_bridges[USER_PROMPT] = MagicMock()
    try:
        with patch.object(cr, 'current_app', app), \
                patch('hartos.helper.current_app', app):
            return cr.create_action_with_ledger(
                actions, 'u', PROMPT, USER_PROMPT, flow_id=flow_id)
    finally:
        cr.user_ledgers.pop(USER_PROMPT, None)
        cr.user_delegation_bridges.pop(USER_PROMPT, None)


def test_next_flow_string_actions_on_the_previous_flows_ledger():
    """THE LIVE SHAPE: flow 0's ledger is still registered when flow 1 is built."""
    ledger = _ledger_for(FLOW_0)
    action = _build_on_existing(ledger, FLOW_1, flow_id=1)
    assert action.ledger is ledger
    # The Action keeps the prompt's own strings: get_action() renders them
    # into "Execute Action N: ..." verbatim.
    assert action.actions == FLOW_1


def test_rebuilding_the_same_flow_adds_nothing():
    ledger = _ledger_for(FLOW_0)
    _build_on_existing(ledger, FLOW_0, flow_id=0)
    assert _ids(ledger) == ['action_1', 'action_2', 'action_3']


def test_a_longer_list_adds_only_the_new_positions():
    """get_total_actions_for_current_flow_and_reset_actions rebuilds a flow
    whose ledger already exists."""
    ledger = _ledger_for(FLOW_0[:2])
    _build_on_existing(ledger, FLOW_0, flow_id=0)
    assert _ids(ledger) == ['action_1', 'action_2', 'action_3']
    assert ledger.tasks['action_3'].description == 'query_weather_api'
