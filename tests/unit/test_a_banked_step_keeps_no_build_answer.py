"""A step CREATE banks from its trace carries no answer the person gave
while the agent was being built: each one is written as its blank.

An agent is built once and used by many people (owner, d1d88c954: people
are blanks, <child_name>, <home_language>, ..., filled per person at run
time).  The trace bank (create_recipe._bank_action_recipe_from_trace)
stored each step as the call's raw arguments, so a build step that asked
"what is the child's name?" and heard "Meena" banked
save_data_in_memory(key='child_name', value='Meena') and every later
send_message_to_user(text='Hi Meena!') with the name in them.  The recipe
file leaves the node (push_recipe, peer_reuse's export) and its code steps
reach the REUSE model as written, so one test child's name became a detail
of the agent.  Live 2026-11-02 the Syllable Coach's banked praise held the
build child's attempt, 'You said "Ba"' (aae1bce84).

The person's words are the answers the create gate recorded on the ledger
(lifecycle_hooks.record_user_answer).  A value the agent saved from one of
them is written as the blank of its memory key (<child_name>); a whole
answer used with no such save is written as the outline's saved mark
(<saved_str>, core.constants.outline_marker).  A word the agent wrote
before the person said it (the lesson's "ba" the child repeats) is the
agent's, and stays.

The bank runs as written (extract-and-exec, as tests/unit/
test_trace_action_banking.py does: importing create_recipe waits on live
services); the ledger and lifecycle_hooks are the real ones.

    python -m pytest tests/unit/test_a_banked_step_keeps_no_build_answer.py -q
"""
import json
import os
import re
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

UP = 'u_bank'
PID = '4242'


class _Task:
    def get_action(self, idx):
        return {'action': f'step {idx + 1} of the speech lesson',
                'fallback_action': 'ask again'}


@pytest.fixture()
def lh(tmp_path):
    import hartos.lifecycle_hooks as L
    from agent_ledger.core import SmartLedger, Task, TaskStatus, TaskType
    led = SmartLedger(agent_id=PID, session_id=f'{UP}_0',
                      ledger_dir=str(tmp_path))
    for aid in (1, 2):
        led.tasks[f'action_{aid}'] = Task(task_id=f'action_{aid}',
                                          description=f'step {aid}',
                                          task_type=TaskType.PRE_ASSIGNED)
        led.tasks[f'action_{aid}'].status = TaskStatus.IN_PROGRESS
    L.register_ledger_for_session(UP, led)
    yield L
    with L._state_lock:
        L._ledger_registry.pop(UP, None)


@pytest.fixture()
def bank(tmp_path):
    src = open(os.path.join(_ROOT, 'hartos/create_recipe.py'),
               encoding='utf-8').read()
    block = re.search(
        r'def _bank_action_recipe_from_trace.*?\n        return False\n',
        src, re.DOTALL).group(0)
    from core.file_cache import atomic_json_write
    ns = {
        'json': json,
        'atomic_json_write': atomic_json_write,
        'user_tasks': {UP: _Task()},
        'helper_fun': SimpleNamespace(safe_prompt_path=lambda pid, flow, aid: str(
            tmp_path / f'{pid}_{flow}_{aid}.json')),
        'current_app': SimpleNamespace(logger=MagicMock()),
    }
    exec(block, ns)

    def run(messages, action_id=1, flow=0):
        gc = SimpleNamespace(messages=messages, agents=[])
        assert ns['_bank_action_recipe_from_trace'](UP, PID, flow, action_id, gc)
        path = tmp_path / f'{PID}_{flow}_{action_id}.json'
        raw = path.read_text(encoding='utf-8')
        return raw, json.loads(raw)

    return run


def _dispatch(aid, text):
    return {'role': 'user', 'name': 'ChatInstructor',
            'content': f'Execute Action {aid}: {text}'}


def _call(cid, tool, **args):
    return {'role': 'assistant', 'name': 'Assistant', 'content': None,
            'tool_calls': [{'id': cid, 'type': 'function', 'function': {
                'name': tool, 'arguments': json.dumps(args)}}]}


def _result(cid, body='Message sent successfully to user with request_id: r1'):
    return {'role': 'tool', 'name': 'Helper', 'content': body,
            'tool_responses': [{'tool_call_id': cid, 'role': 'tool',
                                'content': body}]}


def _person(text):
    return {'role': 'user', 'name': 'UserProxy', 'content': text}


def _asked_and_answered(answer):
    return [
        _dispatch(1, "ask the parent for the child's name and save it"),
        _call('c1', 'send_message_to_user', text="What is your child's name?",
              expects_reply=True),
        _result('c1'),
        _person(answer),
    ]


def _steps(data):
    return [s['steps'] for s in data['recipe']]


def test_a_name_the_person_gave_is_banked_as_its_memory_blank(bank, lh):
    assert lh.record_user_answer(UP, 1, 'Meena', 0)
    raw, data = bank(_asked_and_answered('Meena') + [
        _call('c2', 'save_data_in_memory', key='child_name', value='Meena'),
        _result('c2', 'Saved'),
        _call('c3', 'send_message_to_user', text='Hi Meena! Let us practise.'),
        _result('c3'),
    ])
    assert 'meena' not in raw.lower(), raw
    save, greet = _steps(data)[1], _steps(data)[2]
    assert '<child_name>' in save, save
    assert 'Hi <child_name>! Let us practise.' in greet, greet
    # The question the agent asked is its own and stays.
    assert "What is your child's name?" in _steps(data)[0]


def test_a_later_steps_use_of_the_name_is_banked_as_the_blank(bank, lh):
    """The name was asked and saved in action 1; action 2's window only
    greets.  The blank is still the saved key's."""
    assert lh.record_user_answer(UP, 1, 'Meena', 0)
    raw, data = bank(_asked_and_answered('Meena') + [
        _call('c2', 'save_data_in_memory', key='child_name', value='Meena'),
        _result('c2', 'Saved'),
        _dispatch(2, 'greet the child by name'),
        _call('c3', 'send_message_to_user', text='Hi Meena!'),
        _result('c3'),
    ], action_id=2)
    assert 'meena' not in raw.lower(), raw
    assert _steps(data) == ['send_message_to_user({"text": "Hi <child_name>!"})']


def test_answers_to_every_step_and_flow_of_the_build_are_blanks(bank, lh):
    """The name was answered at flow 0's action 1, the home language at
    flow 1's action 2; flow 1's action 2 banks with both as blanks."""
    assert lh.record_user_answer(UP, 1, 'Meena', 0)
    assert lh.record_user_answer(UP, 2, 'Tamil', 1)
    raw, data = bank(_asked_and_answered('Meena') + [
        _call('c2', 'save_data_in_memory', key='child_name', value='Meena'),
        _result('c2', 'Saved'),
        _dispatch(2, 'ask which language the family speaks at home'),
        _call('c3', 'send_message_to_user', text='Which language do you speak at home?',
              expects_reply=True),
        _result('c3'),
        _person('Tamil'),
        _call('c4', 'save_data_in_memory', key='home_language', value='Tamil'),
        _result('c4', 'Saved'),
        _call('c5', 'send_message_to_user', text='Hi Meena, today we speak Tamil.'),
        _result('c5'),
    ], action_id=2, flow=1)
    assert 'meena' not in raw.lower() and 'tamil' not in raw.lower(), raw
    assert 'Hi <child_name>, today we speak <home_language>.' in _steps(data)[-1]


def test_a_name_inside_a_longer_answer_is_banked_as_the_blank(bank, lh):
    assert lh.record_user_answer(UP, 1, 'Her name is Meena.', 0)
    raw, data = bank(_asked_and_answered('Her name is Meena.') + [
        _call('c2', 'save_data_in_memory', key='child_name', value='Meena'),
        _result('c2', 'Saved'),
        _call('c3', 'send_message_to_user', text='Hi MEENA!'),
        _result('c3'),
    ])
    assert 'meena' not in raw.lower(), raw
    assert 'Hi <child_name>!' in _steps(data)[2]


def test_an_answer_saved_under_no_key_is_banked_as_the_saved_mark(bank, lh):
    from core.constants import outline_marker
    assert lh.record_user_answer(UP, 1, 'Meena', 0)
    raw, data = bank(_asked_and_answered('Meena') + [
        _call('c3', 'send_message_to_user', text='Hi Meena!'),
        _result('c3'),
    ])
    assert 'meena' not in raw.lower(), raw
    assert f'Hi {outline_marker("str")}!' in _steps(data)[1]


def test_a_key_that_is_no_blank_shape_gets_the_saved_mark(bank, lh):
    """'<name>' is no blank the send tools refuse (an identifier joined by
    '_'), so a model that copied it would send it: the saved mark instead."""
    from core.constants import outline_marker
    assert lh.record_user_answer(UP, 1, 'Meena', 0)
    raw, data = bank(_asked_and_answered('Meena') + [
        _call('c2', 'save_data_in_memory', key='name', value='Meena'),
        _result('c2', 'Saved'),
        _call('c3', 'send_message_to_user', text='Hi Meena!'),
        _result('c3'),
    ])
    assert 'meena' not in raw.lower(), raw
    assert '<name>' not in raw
    assert f'Hi {outline_marker("str")}!' in _steps(data)[2]


def test_a_name_in_a_saved_object_is_banked_as_its_path_blank(bank, lh):
    assert lh.record_user_answer(UP, 1, 'Meena', 0)
    raw, data = bank(_asked_and_answered('Meena') + [
        _call('c2', 'save_data_in_memory', key='child',
              value={'name': 'Meena', 'level': 'beginner'}),
        _result('c2', 'Saved'),
        _call('c3', 'send_message_to_user', text='Hi Meena!'),
        _result('c3'),
    ])
    assert 'meena' not in raw.lower(), raw
    assert 'Hi <child_name>!' in _steps(data)[2]
    assert 'beginner' in _steps(data)[1]


def test_a_name_written_as_json_escapes_is_banked_as_the_blank(bank, lh):
    """The 4B's arguments can carry a Tamil name as \\u escapes."""
    name = 'மீனா'
    assert lh.record_user_answer(UP, 1, name, 0)
    raw, data = bank(_asked_and_answered(name) + [
        _call('c2', 'save_data_in_memory', key='child_name', value=name),
        _result('c2', 'Saved'),
        _call('c3', 'send_message_to_user', text=f'Vanakkam {name}!'),
        _result('c3'),
    ])
    assert name not in raw and json.dumps(name)[1:-1] not in raw, raw
    assert 'Vanakkam <child_name>!' in _steps(data)[2]


def test_code_that_used_the_answer_is_banked_with_the_blank(bank, lh):
    """REUSE shows code steps as written (reuse_recipe._step_without_values
    leaves them), so a name in banked code reached the next child."""
    assert lh.record_user_answer(UP, 1, 'Meena', 0)
    raw, data = bank(_asked_and_answered('Meena') + [
        {'role': 'assistant', 'name': 'Assistant',
         'content': "```python\nprint('Well done, Meena!')\n```"},
        {'role': 'user', 'name': 'Executor',
         'content': 'exitcode: 0 (execution succeeded)\nCode output: Well done, Meena!'},
    ])
    assert 'meena' not in raw.lower(), raw
    code = data['recipe'][-1]['generalized_functions']
    assert "print('Well done, <saved_str>!')" in code, code


def test_an_answer_from_before_a_restart_is_still_a_blank(bank, lh):
    """The trace of this run need not hold the person's message (the build
    restarted after they answered); the ledger still holds the answer."""
    assert lh.record_user_answer(UP, 1, 'Meena', 0)
    raw, data = bank([
        _dispatch(2, 'greet the child by name'),
        _call('c2', 'get_data_by_key', key='child_name'),
        _result('c2', 'Meena'),
        _call('c3', 'send_message_to_user', text='Hi Meena!'),
        _result('c3'),
    ], action_id=2)
    assert 'meena' not in raw.lower(), raw


# ---- guards: what is the agent's stays as the agent wrote it --------------

def test_values_the_agent_chose_are_banked_unchanged(bank, lh):
    assert lh.record_user_answer(UP, 1, 'Meena', 0)
    raw, data = bank(_asked_and_answered('Meena') + [
        _call('c2', 'save_data_in_memory', key='lesson_level', value='beginner'),
        _result('c2', 'Saved'),
        _call('c3', 'send_message_to_user', text='Today we say ball and cup.'),
        _result('c3'),
    ])
    steps = _steps(data)
    assert steps[1] == ('save_data_in_memory({"key": "lesson_level", '
                        '"value": "beginner"})'), steps[1]
    assert steps[2] == ('send_message_to_user({"text": "Today we say '
                        'ball and cup."})'), steps[2]
    assert '<saved_str>' not in raw and '<lesson_level>' not in raw


def test_a_word_the_agent_said_first_stays_when_the_child_repeats_it(bank, lh):
    """The lesson asks the child to say "ba"; the build's test child says
    "Ba".  The syllable is the lesson's, not the child's detail."""
    assert lh.record_user_answer(UP, 1, 'Ba', 0)
    raw, data = bank([
        _dispatch(1, 'teach the syllable'),
        _call('c1', 'send_message_to_user', text='Can you say ba?',
              expects_reply=True),
        _result('c1'),
        _person('Ba'),
        _call('c2', 'save_data_in_memory', key='last_attempt', value='Ba'),
        _result('c2', 'Saved'),
        _call('c3', 'send_message_to_user', text='Lovely! Say ba once more.'),
        _result('c3'),
    ])
    steps = _steps(data)
    assert 'Can you say ba?' in steps[0]
    assert '"value": "Ba"' in steps[1], steps[1]
    assert 'Lovely! Say ba once more.' in steps[2], steps[2]
    assert '<last_attempt>' not in raw and '<saved_str>' not in raw


def test_a_word_inside_another_word_is_not_the_answer(bank, lh):
    assert lh.record_user_answer(UP, 1, 'Ba', 0)
    raw, data = bank(_asked_and_answered('Ba') + [
        _call('c3', 'send_message_to_user', text='You said ball, well done!'),
        _result('c3'),
    ])
    assert 'You said ball, well done!' in _steps(data)[1], raw


def test_with_no_answer_recorded_the_steps_are_unchanged(bank, lh):
    raw, data = bank(_asked_and_answered('Meena') + [
        _call('c2', 'save_data_in_memory', key='child_name', value='Meena'),
        _result('c2', 'Saved'),
    ])
    assert '"value": "Meena"' in _steps(data)[1]
