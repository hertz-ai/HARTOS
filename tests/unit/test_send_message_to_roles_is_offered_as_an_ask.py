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


# ── what the agents and the plan writer are told about the tool ───────────
# rv_c534's review of 494c91a08: the schema said "ask", while the builder's
# catalogue filed the tool under user communication ("message a specific
# persona/role") and eight system-prompt lines still said to use it "when
# contacting personas", so a new recipe planned a delivery through it.

ROLE_LINE = ("3. The \"send_message_to_roles\" tool asks another person's agent a "
             'question and returns the answer from its notes; it delivers no message. '
             'To message the user, use the "send_message_to_user" tool.')
PROMPT_COPIES = {'hartos/reuse_recipe.py': 4, 'hartos/create_recipe.py': 2,
                 'hartos/helper.py': 2}


def _source(rel):
    with open(os.path.join(ROOT, *rel.split('/')), encoding='utf-8') as f:
        return f.read()


@pytest.mark.parametrize('rel', sorted(PROMPT_COPIES))
def test_every_system_prompt_copy_says_the_tool_asks_and_names_the_delivery_tool(rel):
    source = _source(rel)
    assert source.count(ROLE_LINE) == PROMPT_COPIES[rel]
    assert 'contacting personas' not in source
    # The one line about the tool in a system prompt is this one.
    told = [line.strip() for line in source.splitlines()
            if line.strip().startswith('3. ') and TOOL in line]
    assert told == [ROLE_LINE] * PROMPT_COPIES[rel], told


def test_the_reuse_helper_and_executor_are_told_so(seats):
    built = seats('reuse')
    for agent in (built.helper, built.executor):
        said = agent.system_message
        assert ROLE_LINE in said, agent.name
        assert 'contacting personas' not in said, agent.name
    # The delivery tool the line points at is one the Helper is offered.
    offered = {(entry.get('function') or {}).get('name')
               for entry in (built.helper.llm_config or {}).get('tools') or []}
    assert 'send_message_to_user' in offered


def test_the_plan_writers_catalogue_files_the_tool_as_an_ask():
    import hartos.gather_agentdetails as ga
    catalogue = ga.AUTONOMOUS_TOOL_CATALOG
    line = next(l for l in catalogue.splitlines() if TOOL in l)
    assert line == ("  - send_message_to_roles       ask another person's agent, "
                    "answered from its notes (delivers no message)")
    assert 'message a specific persona/role' not in catalogue
    sections = catalogue.split('\n\n')
    home = next(s for s in sections if TOOL in s)
    assert home.startswith('Memory:'), home
    talk = next(s for s in sections if s.startswith('User communication:'))
    assert TOOL not in talk
    # The tools that do deliver to the user stay where they were.
    assert 'send_message_to_user        message the user immediately' in talk
    assert 'send_message_in_seconds     message the user after a delay' in talk
