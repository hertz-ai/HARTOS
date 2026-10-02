"""The reuse main group routes a Helper turn like main did: its user-bound
message is delivered, its TERMINATE ends the turn, otherwise the Assistant.

#126 (ported from #124) added "Helper" to state_transition's name check
("the Helper -> Assistant edge never matched", since the old comparison said
"helper").  But a Helper turn already reached the Assistant at the function's
last line; matching it early only SKIPPED the message2userfinal delivery (the
Helper's prompt, rule 9, pushes proactive data to the user that way) and the
TERMINATE check.  Review of #126 caught it; nothing tested it.

state_transition is a closure inside create_agents_for_user, so this compiles
the REAL nested function out of reuse_recipe.py (AST, no text edits) and runs
it with the names it closes over bound to fakes.  The boundaries mocked are
the agents, the group chat, the Flask logger and the user-delivery call.
"""
import ast
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')


def _main_group_selector():
    src = open(SRC, encoding='utf-8').read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == 'state_transition':
            body = ast.get_source_segment(src, node)
            # The main group's selector (same locator as
            # test_reuse_tool_call_routing): it routes tool calls through
            # _agent_that_executes and @statusverifier to `verify`.
            if body and '_agent_that_executes(' in body and 'verify' in body:
                return node
    raise AssertionError('main-group state_transition not found')


def _agent(name):
    return SimpleNamespace(name=name)


@pytest.fixture()
def route():
    agents = {n: _agent(n) for n in (
        'Assistant', 'Helper', 'Executor', 'StatusVerifier', 'ChatInstructor')}
    says = MagicMock()
    ns = {
        'user_id': 'u1', 'prompt_id': 'p1', 'user_prompt': 'u1_p1',
        'request_id_list': {'u1_p1': 'r1'},
        'assistant': agents['Assistant'], 'helper': agents['Helper'],
        'executor': agents['Executor'], 'verify': agents['StatusVerifier'],
        'chat_instructor': agents['ChatInstructor'],
        'current_app': MagicMock(),
        '_agent_that_executes': lambda group, msg: (None, None),
        'retrieve_json': _retrieve_json,
        # No recipe entry for the action: the verdict block's autonomy
        # lookup raises IndexError and falls through, as for a turn whose
        # message is not a StatusVerifier verdict.
        'user_tasks': {'u1_p1': SimpleNamespace(current_action=1)},
        'individual_recipe': [],
        'action_is_autonomous': lambda v: bool(v),
        'publish_intermediate_thoughts_to_user': MagicMock(),
        '_reuse_speaker_says_to_user': says,
        're': __import__('re'),
    }
    exec(compile(ast.Module(body=[_main_group_selector()], type_ignores=[]),
                 SRC, 'exec'), ns)
    select = ns['state_transition']

    def run(speaker, content):
        group = SimpleNamespace(messages=[
            {'role': 'user', 'content': 'do the thing'},
            {'role': 'assistant', 'name': speaker, 'content': content},
        ], agents=list(agents.values()))
        return select(agents[speaker], group)
    run.agents = agents
    run.says = says
    return run


def _retrieve_json(text):
    start = text.find('{')
    try:
        return json.loads(text[start:]) if start >= 0 else None
    except ValueError:
        return None


USER_BOUND = 'message2userfinal {"message2userfinal": "Your report is ready."}'


def test_helper_message_to_the_user_is_delivered(route):
    nxt = route('Helper', USER_BOUND)
    route.says.assert_called_once()
    assert nxt is route.agents['Assistant']


def test_helper_terminate_ends_the_turn(route):
    assert route('Helper', 'All done. TERMINATE') is None


def test_helper_plain_turn_goes_to_the_assistant(route):
    assert route('Helper', 'I looked it up.') is route.agents['Assistant']
    route.says.assert_not_called()


def test_executor_still_hands_straight_to_the_assistant(route):
    """The name check still routes the agents it names, before delivery."""
    assert route('Executor', USER_BOUND) is route.agents['Assistant']
    route.says.assert_not_called()
