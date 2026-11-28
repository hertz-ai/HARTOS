"""A name the model made up while an agent was being built is banked as its
blank, like a name the person gave, so the next family's own name goes there.

#282, live 2026-11-27 on the desktop: building the speech agent 95568353503,
the model saved child_responses.name_recognition = {"name": "Liam",
"recognized": true} in flow 8.  Nobody had said "Liam" (the child is Zoey).
lifecycle_hooks.build_answer_blanks blanked a name only when the person had
said it, so the trace bank wrote the step word for word into
95568353503_8_2.json and 95568353503_8_recipe.json, a made-up child's name in
the recipe every family's agent runs.

Now a name nobody said, saved where names of a person go (_holds_names: the
key's last part is a name, and _about_a_person: a part of the key is about a
person other than by the word "name"), is that key's blank too.  A thing's
name (agent.name, game_name) and a value saved for everyone (owner ruling
2026-10-17) stay as written.  Design agreed with hartos-91.

Only a value that reads as a name is taken for a made-up one, and only as a
whole (lifecycle_hooks._reads_as_a_name; review of ff09dcf68): "not yet"
and "Unknown" saved where a name goes are no names, and blanking them word
by word took "not yet" and "unknown" out of every step of the lesson.

The bank, the ledger and lifecycle_hooks are the real ones, through the
fixtures of tests/unit/test_a_banked_step_keeps_no_build_answer.py.

    python -m pytest tests/unit/test_banked_steps_keep_no_invented_name.py -q
"""
import json
import os
import re
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tests.unit.test_a_banked_step_keeps_no_build_answer import (  # noqa: E402,F401
    UP, _asked_and_answered, _call, _result, _steps, bank, lh)


def _flow8_name_call(**extra):
    """The live flow-8 save: the child's name in a name-recognition note."""
    return [_call('c2', 'save_data_in_memory',
                  key='child_responses.name_recognition',
                  value={'name': 'Liam', 'recognized': True}, **extra),
            _result('c2', 'Saved')]


def test_a_name_nobody_said_under_a_childs_key_is_banked_as_its_blank(bank, lh):
    assert lh.record_user_answer(UP, 1, 'Zoey', 0)
    raw, data = bank(_asked_and_answered('Zoey') + _flow8_name_call())
    assert 'liam' not in raw.lower(), raw
    step = _steps(data)[1]
    assert '<child_responses_name_recognition_name>' in step, step
    assert '"recognized": true' in step, step      # the rest of the note stays


def test_said_and_made_up_names_in_one_family_are_all_blanks(bank, lh):
    assert lh.record_user_answer(UP, 1, 'Lakshmi', 0)
    raw, data = bank(_asked_and_answered('Lakshmi') + [
        _call('c2', 'save_data_in_memory', key='family_members', value=[
            {'relation': 'Amma', 'name': 'Lakshmi'},
            {'relation': 'Appa', 'name': 'Rahul'}]),
        _result('c2', 'Saved'),
    ])
    assert 'lakshmi' not in raw.lower() and 'rahul' not in raw.lower(), raw
    assert '<family_members_name>' in _steps(data)[1]
    # family_members.relation says who a person is to the child, not a name:
    # "Amma" and "Appa" are the lesson's words and stay.
    assert 'Amma' in raw and 'Appa' in raw, raw


def test_a_made_up_name_saved_for_everyone_stays_as_written(bank, lh):
    """Owner ruling 2026-10-17: a value saved for everyone (a sample lesson)
    is no person's data."""
    assert lh.record_user_answer(UP, 1, 'Zoey', 0)
    raw, data = bank(_asked_and_answered('Zoey')
                     + _flow8_name_call(for_everyone=True))
    assert '"name": "Liam"' in _steps(data)[1], raw
    assert '<child_responses_name_recognition_name>' not in raw


def test_the_name_of_a_thing_stays_as_written(bank, lh):
    """agent.name and game_name are names, but not of a person: "name" alone
    does not make a key about a person."""
    assert lh.record_user_answer(UP, 1, 'Zoey', 0)
    raw, data = bank(_asked_and_answered('Zoey') + [
        _call('c2', 'save_data_in_memory', key='agent',
              value={'name': 'Buddy Bear'}),
        _result('c2', 'Saved'),
        _call('c3', 'save_data_in_memory', key='game_name', value='Fruit Match'),
        _result('c3', 'Saved'),
    ])
    steps = _steps(data)
    assert '"name": "Buddy Bear"' in steps[1], steps[1]
    assert '"value": "Fruit Match"' in steps[2], steps[2]
    assert '<agent_name>' not in raw and '<game_name>' not in raw, raw


LESSON = ('If the child has not yet said a word, wait. '
          'An unknown word is fine. Try the summer sale words.')


def _said_after(key, value):
    """A save where a name goes, then a step of the lesson in the agent's
    own words."""
    return [_call('c2', 'save_data_in_memory', key=key, value=value),
            _result('c2', 'Saved'),
            _call('c3', 'send_message_to_user', text=LESSON, expects_reply=False),
            _result('c3', 'Message sent successfully to user')]


def test_a_value_that_is_no_name_takes_nothing_out_of_the_lesson(bank, lh):
    """Review of ff09dcf68: {"name": "not yet"} blanked "not" and "yet"."""
    assert lh.record_user_answer(UP, 1, 'Zoey', 0)
    raw, data = bank(_asked_and_answered('Zoey') + _said_after(
        'child_responses.name_recognition', {'name': 'not yet'}))
    assert 'has not yet said a word' in raw, raw
    assert '<child_responses_name_recognition_name>' not in raw, raw


def test_a_no_name_value_is_kept_and_blanks_no_word(bank, lh):
    assert lh.record_user_answer(UP, 1, 'Zoey', 0)
    raw, data = bank(_asked_and_answered('Zoey') + _said_after(
        'child_name', 'Unknown'))
    assert 'An unknown word is fine' in raw, raw
    assert '<child_name>' not in raw, raw


def test_a_made_up_name_of_two_words_is_one_blank_not_two(bank, lh):
    """The whole value is the blank; each of its words stays where the
    lesson uses it alone ("Meena Rose" as a made-up parent name, "a rose")."""
    assert lh.record_user_answer(UP, 1, 'Zoey', 0)
    raw, data = bank(_asked_and_answered('Zoey') + [
        _call('c2', 'save_data_in_memory', key='parent_name', value='Meena Rose'),
        _result('c2', 'Saved'),
        _call('c3', 'send_message_to_user', text='Say rose. A rose is a flower.',
              expects_reply=False),
        _result('c3', 'Message sent successfully to user')])
    assert 'Say rose. A rose is a flower.' in raw, raw
    assert 'Meena Rose' not in raw, raw


def test_reads_as_a_name_is_capitals_and_no_filler(lh):
    assert lh._reads_as_a_name('Liam') and lh._reads_as_a_name('Meena Kumari')
    for no_name in ('not yet', 'Unknown', 'unknown', 'N/A', 'The Child', '', 'liam',
                    'Child', 'Friend', 'Kid', 'Little Friend'):
        assert not lh._reads_as_a_name(no_name), no_name


def test_a_word_for_who_someone_is_takes_no_word_out_of_the_lesson(bank, lh):
    """Review of 4326f94a7: child_name "Child" blanked every "child"."""
    assert lh.record_user_answer(UP, 1, 'Zoey', 0)
    raw, data = bank(_asked_and_answered('Zoey') + [
        _call('c2', 'save_data_in_memory', key='parent_name', value='Child'),
        _result('c2', 'Saved'),
        _call('c3', 'send_message_to_user', text='When the child says ball, clap.',
              expects_reply=False),
        _result('c3', 'Message sent successfully to user')])
    assert 'When the child says ball, clap.' in raw, raw


def _reuse_view():
    """reuse_recipe's own view of a saved action as REUSE shows it to the
    model, run as written (importing reuse_recipe waits on live services)."""
    src = open(os.path.join(_ROOT, 'hartos/reuse_recipe.py'),
               encoding='utf-8').read()
    ns = {'json': json}
    for name in ('_TRACE_RECIPE_SOURCE = ', 'def _step_without_values',
                 'def _saved_action_without_values'):
        block = re.search(rf'^{re.escape(name)}.*?(?=^\S)', src,
                          re.DOTALL | re.MULTILINE).group(0)
        exec(block, ns)
    return ns['_saved_action_without_values']


def test_a_replay_is_never_shown_the_blank_to_save(bank, lh):
    """Review hartos-91: a replay of the blanked step with nothing in the
    next person's memory must not save the blank as their child's name.
    save_data_in_memory refuses only the outline's mark as a value, not a
    blank like this one; what holds is that REUSE shows a trace-banked step
    by its argument names only, so the replay never sees the blank (or any
    value) and writes the value from its own person's turn."""
    assert lh.record_user_answer(UP, 1, 'Zoey', 0)
    raw, data = bank(_asked_and_answered('Zoey') + _flow8_name_call())
    assert data.get('recipe_source') == 'execution_trace', data
    shown = _reuse_view()(data)
    shown_raw = json.dumps(shown)
    assert '<child_responses_name_recognition_name>' not in shown_raw, shown_raw
    assert 'Liam' not in shown_raw
    assert _steps(shown)[1] == 'save_data_in_memory(key, value)', _steps(shown)
