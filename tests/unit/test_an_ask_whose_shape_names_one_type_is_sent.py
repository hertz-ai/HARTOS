"""An ask whose props name one component type is sent as that type.

LIVE, 2026-11-16 14:44:54-14:58:46 IST, the owner's desktop (Nunba, the local
4B), agent_system.log, prompt 1794820266612 (the Email Draft Helper): all 8
of its ask_person calls were refused not_an_object.  Each component had its
reason and its fields, and no "type":

    {"fields": [{"label": "How would you like to proceed with this email?",
                 "name": "proceed", "type": "text"}],
     "description": "Confirming next steps with user", "wait_seconds": 15}

So the agent sent the draft again with send_message_to_user and asked again,
three asks in 20 seconds (14:47:23, :27, :42), and never got an answer.
The type-first hint (eb6e8053c) cut the misses on hartos-94's replay to 7 of
46, not to none.

Now the ask gate (person_ask._refusal) gives a component with no type the one
builtin type whose required props it all carries (liquid_ui_service.
COMPONENT_TYPES: "fields" is only a form's, "purpose" only a file_request's),
and the ask then goes through every check a typed one does.  No match, or
more than one ("items" is a list's and a cart's), is refused as before, with
the same note.  A component that names its type is untouched.

    python -m pytest tests/unit/test_an_ask_whose_shape_names_one_type_is_sent.py -q
"""
from __future__ import annotations

import json
import os
import sys

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from tests.unit.test_person_ask import _no_open_asks, _settle, legs  # noqa: E402,F401

#: The Email Draft Helper's ask as it was sent (14:47:23 IST), word for word.
LOGGED = ('{"fields": [{"label": "How would you like to proceed with this email?", '
          '"name": "proceed", "type": "text"}], "description": "Confirming next steps '
          'with user", "wait_seconds": 15}')
PROMPT = '1794820266612'


def _tool(prompt_id=PROMPT):
    from integrations.agent_engine import person_ask
    return person_ask.ask_person_tool('10202', prompt_id)[2]


def _send(component, prompt_id=PROMPT):
    sent = component if isinstance(component, str) else json.dumps(component)
    return json.loads(_tool(prompt_id)(sent, 0))


def _types_requiring(prop):
    from integrations.agent_engine.liquid_ui_service import COMPONENT_TYPES
    return sorted(t for t, e in COMPONENT_TYPES.items()
                  if isinstance(e, dict) and prop in (e.get('required') or []))


# -- the defect: a clear shape with no type was refused ----------------------

def test_the_logged_email_ask_reaches_the_person_as_a_form(legs):
    reply = _send(LOGGED)
    assert reply['status'] == 'waiting_for_person', reply
    assert len(legs['cards']) == 1, legs['cards']
    card = legs['cards'][0][1]
    assert card['type'] == 'form', card
    assert card['fields'][0]['label'] == 'How would you like to proceed with this email?'
    assert card['description'] == 'Confirming next steps with user'
    _settle()


def test_the_gate_gives_the_logged_component_its_one_type():
    from integrations.agent_engine import person_ask
    comp = json.loads(LOGGED)
    assert person_ask._refusal(comp, PROMPT) is None
    assert comp['type'] == 'form', comp


def test_a_file_ask_with_its_purpose_and_no_type_is_a_file_request(legs):
    """"purpose" is only a file_request's: the card is the file picker, its
    accept and count set as for a typed one."""
    reply = _send({'purpose': 'reel_source',
                   'description': 'Send the clips for your reel.'}, prompt_id='342')
    assert reply['status'] == 'waiting_for_person', reply
    card = legs['cards'][0][1]
    assert card['type'] == 'file_request', card
    assert card['accept'] == 'video' and card['prompt_id'] == '342', card
    _settle()


def test_a_typeless_form_is_held_to_the_question_rule_like_a_typed_one(legs):
    """Given its type, it meets every check after the type: a form that
    asks nothing is refused no_question, as a typed one is."""
    reply = _send({'description': 'Extract facts from the search results',
                   'fields': [{'name': 'facts', 'label': 'Facts', 'type': 'text'}]})
    assert reply['refused'] == 'no_question', reply
    assert legs['cards'] == []


def test_a_typeless_form_that_asks_for_a_secret_goes_to_the_vault(legs):
    reply = _send({'description': 'To sign in to your mail, what is it?',
                   'fields': [{'name': 'password', 'label': 'Your password?',
                               'type': 'text'}]})
    assert reply['refused'] == 'secrets_go_to_the_vault', reply
    assert legs['cards'] == []


# -- guards: unclear shapes stay refused, typed ones are untouched ----------

def test_items_name_two_types_so_it_stays_refused_with_todays_note(legs):
    assert len(_types_requiring('items')) >= 2, _types_requiring('items')
    reply = _send({'description': 'Which of these should I send first?',
                   'items': ['the draft', 'the summary']})
    assert reply['refused'] == 'not_an_object', reply
    assert 'no "type"' in reply['note'] and '"type": "form"' in reply['note'], reply
    assert legs['cards'] == []


def test_props_of_two_types_at_once_stay_refused(legs):
    """"fields" is a form's and "action" an approval's: which one was meant
    is not guessed, even though either would be sent."""
    comp = {'description': 'Before I post the reel',
            'action': 'post_the_reel',
            'fields': [{'name': 'a', 'label': 'Post it now?', 'type': 'text'}]}
    reply = _send(comp)
    assert reply['refused'] == 'not_an_object', reply
    assert legs['cards'] == []
    from integrations.agent_engine import person_ask
    same = json.loads(json.dumps(comp))
    assert person_ask._refusal(same, PROMPT) == 'not_an_object'
    assert 'type' not in same, same


def test_props_no_type_requires_stay_refused(legs):
    reply = _send({'description': 'Why I ask.',
                   'questions': [{'name': 'a', 'label': 'Which day?', 'type': 'text'}]})
    assert reply['refused'] == 'not_an_object', reply
    assert legs['cards'] == []


def test_a_required_prop_sent_as_null_is_not_there(legs):
    """Present means not null, the card gate's own rule
    (liquid_ui_service.validate_component)."""
    reply = _send({'description': 'Which day suits you?', 'fields': None})
    assert reply['refused'] == 'not_an_object', reply
    assert legs['cards'] == []


@pytest.mark.parametrize('component', [
    {'type': 'approval', 'description': "Post today's reel?",
     'fields': [{'name': 'note', 'label': 'Anything to add?', 'type': 'text'}]},
    {'type': 'form', 'description': 'To plan the week.', 'action': 'plan_week',
     'fields': [{'name': 'day', 'label': 'Which day suits you?', 'type': 'text'}]},
], ids=['approval_with_fields', 'form_with_action'])
def test_a_component_that_names_its_type_keeps_it(legs, component):
    reply = _send(component)
    assert reply['status'] == 'waiting_for_person', reply
    assert legs['cards'][0][1]['type'] == component['type'], legs['cards']
    _settle()
