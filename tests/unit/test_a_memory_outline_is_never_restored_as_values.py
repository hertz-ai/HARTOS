"""A memory outline shows a saved text value as redacted, and is never
restored into a person's memory as if it were the values.

WHAT WENT WRONG (live, source Nunba on the local 4B, 18 Nov 2026 22:21-22:24
IST, a test account, the email agent): every read of the agent's memory came
back {'email_brief': {'recipient': None, 'subject': None, 'body': None}, ...}.
The 4B concluded its saved draft was empty, asked the person for the
manager's address again, and the turn ended "I couldn't finish that".

Two defects behind it:
  1. helper.strip_json_values returned None for a plain-text leaf: the str
     branch caught the JSON error and fell off the end of the elif chain,
     instead of returning the redaction marker its docstring promises (task
     #181).  So the outline the model reads showed every saved text as None.
  2. create_recipe.load_existing_metadata merged the newest action file's
     'metadata' into the creator's own memory and saved it.  That metadata is
     always written as strip_json_values(memory_view(...)), a values-stripped
     outline, so the restore wrote placeholders into the person's memory as
     data, bypassing the placeholder refusal saves have.

THE RULE
  * every leaf outlines as "<saved_<type>>", a text leaf as "<saved_str>",
    never None;
  * a string holding a JSON object or list still outlines as that JSON; a
    string that parses as a JSON number or word ("123") is a saved string;
  * the marker is a shape the existing text-placeholder rule refuses, so a
    model that copies it into a message is told to fill it in (review by
    hartos-43, 18 Nov: "redacted str" could be copied and nothing refused it);
  * restoring from an action file never writes an outline leaf into a
    person's memory: pinned in test_agent_memory_is_per_person.py
    (test_create_resume_restores_no_outline_into_the_creators_memory, and
    test_create_resume_still_restores_the_agents_saved_store for the saved
    store that resume does bring back), which runs initialize_with_resume
    from its source.

    python -m pytest tests/unit/test_a_memory_outline_is_never_restored_as_values.py -q --noconftest
"""
import json

import hartos.helper as helper


def test_a_saved_text_value_outlines_as_saved_not_none():
    out = helper.strip_json_values({'email_brief': {'subject': 'Friday off',
                                                    'body': 'Hi, may I take Friday off?'}})
    assert out == {'email_brief': {'subject': '<saved_str>',
                                   'body': '<saved_str>'}}, out


def test_no_leaf_of_an_outline_is_none_for_a_value_that_was_saved():
    out = helper.strip_json_values(['a', 3, 2.5, True, {'k': 'v'}])
    assert out == ['<saved_str>', '<saved_int>', '<saved_float>', '<saved_bool>',
                   {'k': '<saved_str>'}], out


def test_a_json_string_still_outlines_as_the_json_it_holds():
    out = helper.strip_json_values({'draft': json.dumps({'to': 'x@y.z'})})
    assert out == {'draft': {'to': '<saved_str>'}}, out


def test_text_that_parses_as_a_number_is_still_a_saved_string():
    assert helper.strip_json_values({'code': '123', 'flag': 'true'}) == {
        'code': '<saved_str>', 'flag': '<saved_str>'}


def test_a_copied_marker_in_a_message_is_refused_by_the_existing_rule():
    from core.agent_tools import unfilled_text_placeholder
    assert unfilled_text_placeholder('Subject: <saved_str>') == '<saved_str>'
    assert unfilled_text_placeholder('<saved_str>')
