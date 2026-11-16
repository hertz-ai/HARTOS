"""An ask card an agent sends says what it asks: its question reaches the
person, never the agent's own step.

MEASURED on the owner's desktop, 2026-11-16 (Nunba build 75, the local 4B,
server.log.4 .. server.log): 418 ask_person calls, 26 cards that reached a
person, 17 of them the owner from background agents.  About half the 26 asked
nothing.  The card's reason was the agent's own step and its one field a noun:

    "Capture pre-upgrade benchmark metrics for performance baseline"
        field "Benchmark Capture"                      (13:34:39, agent 84515916338)
    "Creating social media post for HART Guardian Angels campaign with
     guardian angel messaging"  field "Social Media Post Content"   (19:40:04)
    "Extract facts from the search results about speech therapy apps for
     children"  field "Facts"                          (14:18:55, 1794818852965)
    "Extract clean markdown content from the top speech therapy app result"
        an approval                                    (14:41:29, 1794820266612)

Owner, 2026-11-16: "the questions that gets asked in Nunba are all stupid",
"most question does not have detail on what the ask is".

The tool's hint asked for "why you ask" and nothing more, and a small model
answers "why" with what it is doing.  Now a form or an approval the model sends
carries its question (a "?" in its title, reason or a field's label) or is not
sent, and the note says to write the question itself, and to do its own step's
work with its tools.  A file ask is exempt: its purpose says what is asked.
Cards the framework builds (the join card, the plan approval, the post
destination) go through ask() without the rule and are untouched.

The hint itself is hartos-94's measured candidate (hint_ab, 11-12, real
requests replayed on the 4B: asks with a type and a reason 28 of 53 under the
current hint, 39 of 46 with "type" named first), its example label one whole
blank "<your question?>".

    python -m pytest tests/unit/test_an_ask_card_asks_its_question.py -q
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

#: Cards that reached the person on 2026-11-16, as the agents sent them.
ASKED_NOTHING = [
    {'type': 'form',
     'description': 'Capture pre-upgrade benchmark metrics for performance baseline',
     'fields': [{'name': 'metrics', 'label': 'Benchmark Capture', 'type': 'text'}]},
    {'type': 'form',
     'description': ('Creating social media post for HART Guardian Angels campaign '
                     'with guardian angel messaging'),
     'fields': [{'name': 'post', 'label': 'Social Media Post Content', 'type': 'text'}]},
    {'type': 'form',
     'description': 'Extract facts from the search results about speech therapy apps for children',
     'fields': [{'name': 'facts', 'label': 'Facts', 'type': 'text'}]},
    {'type': 'approval',
     'description': 'Extract clean markdown content from the top speech therapy app result'},
    {'type': 'form', 'description': "Ask for child's name",
     'fields': [{'name': 'child_name', 'label': "Child's Name", 'type': 'text'}]},
]

#: Real questions, from the same day's cards and the tests' own.
ASKS_SOMETHING = [
    {'type': 'form',
     'description': "I'd love to know your child's name so we can personalize our Tamil sessions!",
     'fields': [{'name': 'child_name', 'label': "What is your child's name?", 'type': 'text'}]},
    {'type': 'approval',
     'description': ('We found an unhealthy TTS engine. Would you like us to repair it by '
                     're-installing its packages?')},
    {'type': 'approval', 'action': 'post_the_reel', 'description': "Post today's reel?"},
    # The question in the title, as the post-destination card writes it.
    {'type': 'form', 'title': 'Where should posts on Telegram go?',
     'description': 'Your agent is ready to post on Telegram.',
     'fields': [{'name': 'chat_id', 'label': 'Telegram group or channel', 'type': 'text'}]},
    # Other scripts' question marks.
    {'type': 'form', 'description': 'お子さんの名前は？',
     'fields': [{'name': 'n', 'label': '名前', 'type': 'text'}]},
    {'type': 'form', 'description': 'ما اسم طفلك؟',
     'fields': [{'name': 'n', 'label': 'الاسم', 'type': 'text'}]},
]


def _tool(prompt_id='84515916338'):
    from integrations.agent_engine import person_ask
    return person_ask.ask_person_tool('10202', prompt_id)


def _send(component):
    _name, _desc, func = _tool()
    return json.loads(func(json.dumps(component), 0))


@pytest.mark.parametrize('component', ASKED_NOTHING,
                         ids=[c['description'][:30] for c in ASKED_NOTHING])
def test_a_card_that_asks_nothing_never_reaches_the_person(legs, component):
    reply = _send(component)
    assert reply['status'] == 'refused', reply
    assert reply['refused'] == 'no_question', reply
    assert legs['cards'] == []
    _settle()
    assert legs['pushes'] == []


def test_the_note_says_what_the_card_said_and_what_to_do_instead(legs):
    reply = _send(ASKED_NOTHING[0])
    note = reply['note']
    assert 'Capture pre-upgrade benchmark metrics' in note, note
    assert '?' in note and 'label' in note, note
    assert 'tools' in note, note          # its own step is done with its tools


@pytest.mark.parametrize('component', ASKS_SOMETHING,
                         ids=[(c.get('title') or c['description'])[:30] for c in ASKS_SOMETHING])
def test_a_card_with_its_question_still_reaches_the_person(legs, component):
    reply = _send(component)
    assert reply['status'] == 'waiting_for_person', reply
    assert len(legs['cards']) == 1


def test_a_file_ask_needs_no_question_mark(legs):
    """Its purpose says what is asked; 'Send the clips for your reel.' is a
    fair ask (test_person_ask's own)."""
    reply = _send({'type': 'file_request', 'purpose': 'reel_source', 'accept': 'video',
                   'description': 'Send the clips for your reel.'})
    assert reply['status'] == 'waiting_for_person', reply


def test_a_card_the_framework_builds_is_untouched(legs):
    """The plan approval hart_intelligence_entry builds carries no '?' and
    goes through ask() without the rule."""
    from integrations.agent_engine import person_ask
    out = person_ask.ask('10202', '1794828497631', {
        'type': 'approval', 'action': 'approve_plan:1794828497631',
        'description': ('The plan for Upgrade Pipeline did not settle in review. '
                        'Approve to build it on your next message, or deny to drop it.')},
        prompt_id='1794828497631')
    assert out['ok'] is True, out


def test_the_earlier_refusals_keep_their_order(legs):
    """A card with no type, or no reason, is told that first: the note it
    gets is the one for what is missing."""
    assert _send({'fields': [{'name': 'a', 'label': 'Facts', 'type': 'text'}],
                  'description': 'Extract facts'})['refused'] == 'not_an_object'
    assert _send({'type': 'form', 'fields': [{'name': 'a', 'label': 'Facts',
                                              'type': 'text'}]})['refused'] == \
        'say_why_in_description'


def test_the_hint_names_type_first():
    """hartos-94's measured candidate: "type" is the first key the model
    reads, then the reason."""
    import typing
    _name, _desc, func = _tool()
    hints = typing.get_type_hints(func, include_extras=True)
    shape = ' '.join(str(m) for m in getattr(hints['component'], '__metadata__', ()))
    assert shape.index('"type"') < shape.index('"description"'), shape
