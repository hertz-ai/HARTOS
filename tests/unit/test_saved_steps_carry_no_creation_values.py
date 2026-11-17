"""A step the trace saver banked from a creation run reaches the REUSE model
as the tool and its argument NAMES, never the creation run's values.

Live 2026-11-02 13:25:52 IST, Syllable Coach 1793359802508 on the owner's
desktop (docs/VERIFICATION.md, commit aae1bce84): prompts/
1793359802508_0_6.json, recipe_source 'execution_trace', saved the 30 Oct
build's call

    send_message_to_user({"text":"You did AMAZING, Arjun! ... You said
    \"Ba\" which is so close to \"ball\"! ...","avatar_id":"0"})

and the reuse turn sent that text verbatim to a child who had just said
"bal".  The saver (create_recipe._bank_action_recipe_from_trace) writes
"tool(<raw JSON arguments, cut at 400 chars>)", and REUSE handed it on in two
places: the dispatch message's "follow these steps" and the Assistant
prompt's recipe block (individual_recipe, also narrowed per action for
Tier-1).  Rule D (hartos-c9): both show "tool(arg, arg)" for such a step.
A model-authored recipe, and code steps, are unchanged.

    python -m pytest tests/unit/test_saved_steps_carry_no_creation_values.py -q
"""
import json
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.constants import ACTION_STEPS_SEPARATOR  # noqa: E402
from hartos import reuse_recipe as rr  # noqa: E402
from tests.unit.test_create_loop_end_to_end import (  # noqa: E402,F401  (fixture)
    PROMPT_ID, create_env)
from tests.unit.test_reuse_scheduler_stays_gated import (  # noqa: E402,F401  (fixture)
    reuse_agents)

KEY = 'u1_p1'
_PRAISE = ('You did AMAZING, Arjun! You said \\"Ba\\" which is so close to '
           '\\"ball\\"! Can you try to say \\"ball\\" please?')
_SAVED_SEND = ('send_message_to_user({"text":"' + _PRAISE
               + '","avatar_id":"0"})')
_SEND_STEP = {'steps': _SAVED_SEND, 'tool_name': 'send_message_to_user',
              'generalized_functions': '', 'agent_to_perform_this_action': 'Helper'}
_CODE_STEP = {'steps': 'run the code in generalized_functions', 'tool_name': None,
              'generalized_functions': "print('You said Ba')"}
_ACTION_WORDS = 'send_message_to_user: if attempt has ball sounds, praise Arjun'


class _Task:
    def get_action(self, _idx):
        return {'action': _ACTION_WORDS}


def _message(monkeypatch, action):
    monkeypatch.setattr(rr, 'user_tasks', {KEY: _Task()})
    monkeypatch.setattr(rr, 'recipes', {KEY: {'actions': [action]}})
    return rr._build_reuse_action_message(KEY, 1)


def _old_message(action):
    """The message exactly as the builder rendered it before rule D."""
    steps = [{str(x['steps']): {'tool_name': x.get('tool_name', None),
                                'code': x.get('generalized_functions', None)}}
             for x in action.get('recipe', [])]
    return (f"{rr._REUSE_ACTION_MESSAGE_PREFIX}1:{_ACTION_WORDS}"
            f"{ACTION_STEPS_SEPARATOR}{steps}")


def test_a_trace_saved_send_reaches_the_model_without_its_words(monkeypatch):
    action = {'action_id': 1, 'recipe_source': 'execution_trace',
              'recipe': [dict(_SEND_STEP)]}
    message = _message(monkeypatch, action)
    assert 'send_message_to_user(text, avatar_id)' in message
    assert 'Ba' not in message.split(ACTION_STEPS_SEPARATOR, 1)[1]
    assert 'AMAZING' not in message
    # Read-only: the recipe the session holds keeps its saved call.
    assert action['recipe'][0]['steps'] == _SAVED_SEND


def test_a_model_authored_recipe_renders_byte_identical(monkeypatch):
    action = {'action_id': 1, 'recipe': [dict(_SEND_STEP)]}
    assert _message(monkeypatch, action) == _old_message(action)


def test_a_code_step_in_a_trace_saved_action_is_unchanged(monkeypatch):
    action = {'action_id': 1, 'recipe_source': 'execution_trace',
              'recipe': [dict(_CODE_STEP)]}
    assert _message(monkeypatch, action) == _old_message(action)


def test_arguments_the_saver_cut_short_show_no_value(monkeypatch):
    cut = dict(_SEND_STEP, steps=_SAVED_SEND[:60])
    message = _message(monkeypatch, {'action_id': 1, 'recipe_source': 'execution_trace',
                                     'recipe': [cut]})
    assert 'send_message_to_user(...)' in message
    assert 'AMAZING' not in message


def test_a_trace_saved_memory_write_does_not_hand_on_another_persons_key(
        monkeypatch):
    """The shape of ~/Documents/Nunba/data/prompts/54_0_3.json (agent 54,
    saved 11 Oct, relayed by fix-all and hartos-c9 on 2 Nov): a trace-saved
    action whose memory writes carry the creation run's literal keys, one of
    them naming a person other than the one in the turn, and literal
    values.  The replay is shown the calls without either.  (The ids below
    are made up; the real file's are not copied here.)"""
    other = 'teach.00000000-aaaa-4bbb-8ccc-000000000001'
    action = {'action_id': 1, 'recipe_source': 'execution_trace', 'recipe': [
        dict(_SEND_STEP, tool_name='save_data_in_memory',
             steps='save_data_in_memory({"key":"teach.90001","value":'
                   '{"book_name":"Turkish A1","progress":"lesson 3"}})'),
        dict(_SEND_STEP, tool_name='get_data_by_key',
             steps='get_data_by_key({"key":"teach.90001"})'),
        dict(_SEND_STEP, tool_name='save_data_in_memory',
             steps='save_data_in_memory({"key":"' + other + '","value":'
                   '{"quiz_score":"100%"}})'),
    ]}
    message = _message(monkeypatch, action)
    steps = message.split(ACTION_STEPS_SEPARATOR, 1)[1]
    assert steps.count('save_data_in_memory(key, value)') == 2
    assert 'get_data_by_key(key)' in steps
    for value in ('teach.', '90001', '00000000-aaaa', 'Turkish', 'lesson 3',
                  'quiz_score', '100%'):
        assert value not in steps, value


def _write_trace_action(create_env):
    (create_env.prompts / f'{PROMPT_ID}_0_1.json').write_text(json.dumps({
        'status': 'done', 'action': _ACTION_WORDS, 'action_id': 1,
        'persona': 'Researcher', 'can_perform_without_user_input': 'yes',
        'recipe_source': 'execution_trace', 'recipe': [_SEND_STEP]}),
        encoding='utf-8')
    return create_env


@pytest.fixture
def trace_saved_send(create_env):
    return _write_trace_action(create_env)


def test_the_assistant_prompt_and_its_one_action_narrow_carry_no_value(
        trace_saved_send, reuse_agents):
    """The real create_agents_for_user: the recipe block of the Assistant's
    system prompt, and the Tier-1 narrow to action 1, name the call without
    the creation run's words."""
    _env, _rr, built, _scheduler, _params = reuse_agents
    assistant = built[0]
    system = assistant.system_message
    assert 'send_message_to_user(text, avatar_id)' in system
    assert 'AMAZING' not in system
    narrowed = rr._recipe_section_for_action(
        system, assistant._hart_individual_recipe, 1)
    assert 'AMAZING' not in narrowed
    assert 'send_message_to_user(text, avatar_id)' in narrowed
    # The file on disk keeps the saved call.
    on_disk = json.loads((_env.prompts / f'{PROMPT_ID}_0_1.json').read_text(
        encoding='utf-8'))
    assert on_disk['recipe'][0]['steps'] == _SAVED_SEND
