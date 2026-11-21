"""send_message_to_roles as the model is shown it and as the agent runs it,
in both pipelines.

Review of c53452437: the tool answers from the asked agent's notes, yet its
description still said "Send a message ... deliver", so a "notify the
parent" step called it and took the answer for a delivery.  The description
now says what the tool does and what it does not.  The name and the two
arguments are what recipes and prompts already use, and stay.

Built by the REAL create_recipe.create_agents and
reuse_recipe.create_agents_for_user (the ``seats`` harness of
tests/unit/test_reel_tools_wired_both_pipelines.py), real autogen agents, no
LLM.  CREATE's Helper authors a recipe and its schema is deferred after it is
built (core.agent_tool_menu.defer_helper_schema), so there the entry is read
as the constructor registered it: its own register_dual call, replayed on a
fresh agent pair through the same register_dual.

    python -m pytest tests/unit/test_send_message_to_roles_is_offered_as_an_ask.py -q
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from tests.unit.test_create_loop_end_to_end import (  # noqa: E402,F401  (fixture)
    PROMPT_ID, USER_ID, create_env)
from tests.unit.test_reel_tools_wired_both_pipelines import seats  # noqa: E402,F401

TOOL = 'send_message_to_roles'


def _offered(agent):
    """The tool's entry on an agent's LLM schema: what the model reads."""
    for entry in (getattr(agent, 'llm_config', None) or {}).get('tools') or []:
        function = entry.get('function') if isinstance(entry, dict) else None
        if isinstance(function, dict) and function.get('name') == TOOL:
            return function
    return None


@pytest.fixture
def registered(monkeypatch):
    """What create_recipe's constructor registers under the tool's name:
    ``(func, description, the real register_dual)``, taken from its own
    register_dual call (it imports the function when it builds)."""
    import core.agent_tool_menu as menu
    real, seen = menu.register_dual, {}

    def spy(helper, executor, func, name, description):
        if name == TOOL:
            seen[name] = (func, description, real)
        return real(helper, executor, func, name, description)
    monkeypatch.setattr(menu, 'register_dual', spy)
    return seen


def _as_registered(func, description, register_dual):
    """The schema entry autogen makes of that registration."""
    import autogen
    llm = {'config_list': [{'model': 'm', 'api_key': 'k',
                            'base_url': 'http://127.0.0.1:9/v1'}], 'cache_seed': None}
    helper = autogen.AssistantAgent('ScratchHelper', llm_config=dict(llm))
    executor = autogen.AssistantAgent('ScratchExecutor', llm_config=False)
    register_dual(helper, executor, func, TOOL, description)
    return _offered(helper)


@pytest.fixture
def calls(monkeypatch):
    """Every call either pipeline's tool hands to the one implementation."""
    import core.persona_registry as pr
    import hartos.reuse_recipe as rr
    seen = []

    def impl(user_id, prompt_id, role, message, publish_fn=None):
        seen.append((user_id, prompt_id, role, message))
        return 'the answer'
    monkeypatch.setattr(pr, '_send_message_to_roles_impl', impl)
    monkeypatch.setattr(rr, '_send_message_to_roles_impl', impl)
    return seen


@pytest.mark.parametrize('pipeline', ['create', 'reuse'])
def test_the_model_is_offered_an_ask_not_a_delivery(seats, calls, registered, pipeline):
    built = seats(pipeline)
    shown = _offered(built.helper)
    if shown is None and pipeline == 'create':
        assert TOOL in registered, 'create_recipe did not register the tool'
        shown = _as_registered(*registered[TOOL])
    assert shown is not None, 'the Helper is not shown the tool'
    said = shown['description']
    assert said.startswith('Ask the agent of the person whose chat this person joined')
    assert 'get its answer from what it has saved about them' in said
    assert 'Nothing is sent to that person' in said and 'does not deliver or notify' in said
    assert 'Send a message' not in said
    # The name and the two arguments recipes already use.
    properties = shown['parameters']['properties']
    assert set(properties) == {'role', 'message'}
    assert set(shown['parameters'].get('required') or ()) == {'role', 'message'}
    assert properties['role']['description'] == 'Persona/role of the person to ask about'
    assert properties['message']['description'] == 'The question for their agent'


@pytest.mark.parametrize('pipeline', ['create', 'reuse'])
def test_the_call_reaches_the_one_implementation_as_this_person(seats, calls, pipeline):
    built = seats(pipeline)
    runner = next(agent for agent in (built.assistant, built.executor, built.helper)
                  if TOOL in (getattr(agent, 'function_map', None) or {}))
    with seats.env.app.app_context():
        out = runner.function_map[TOOL](role='Child', message='How did he do?')
    assert out == 'the answer'
    assert calls == [(USER_ID, PROMPT_ID, 'Child', 'How did he do?')]
