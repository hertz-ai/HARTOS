"""An ask's card says which agent asks and why.

Owner, 2026-11-03 ~17:15 IST, of the card on his Nunba desktop: "Your agent
asks for files / this is the only message i received in nunba desktop not
why , for what , etc".  Measured: ask 9e24bad3 at 17:02:08 IST, by agent
1793359802508 ("Syllable Coach Ball Exercise"), component
{"type": "file_request", "purpose": "reel_source", "accept": "video"} with no
title and no description.  person_ask.ask computed a title ("Your agent needs
your answer") and a body for the bell row and the push, but never put them
on the card, and nothing asked the agent for its reason, so the desktop card
fell back to its own "Your agent asks for files".

Now an ask carries its reason (the component's description, in the agent's
words) or is not sent; the card and the push carry the same title and reason,
and the bell row the title.  With no title of its own, the title names the
agent by the name the person knows it by (consent_service.agent_display_name,
the one lookup consent cards use).  A reason or question still written as the
hint's '<...>' blank is not sent either (review of ab68963ea, MF-1).

The tool's hint (owner, 2026-11-07, of the speech-therapy agent's "choose
video" cards): it no longer holds up Reel Studio's file request as the one
example; the tests at the end of this file.

Real person_ask.ask, NotificationService and in-memory social DB; the card
leg (liquid_ui_service.push_agent_ui) and the push are recorded by the
``legs`` fixture of test_person_ask.

    python -m pytest tests/unit/test_an_ask_says_who_and_why.py -q
"""
from __future__ import annotations

import os
import sys

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.social.models import Notification, User, db_session  # noqa: E402
from tests.unit.test_person_ask import _no_open_asks, _settle, legs  # noqa: E402,F401

COACH = '1793359802508'
COACH_NAME = 'Syllable Coach Ball Exercise'
#: The component the coach sent at 17:02:08.
AS_SENT = {'type': 'file_request', 'purpose': 'reel_source', 'accept': 'video'}
WHY = 'Send a short video of Arjun saying "ball" so I can hear how he says it.'


def _coach_has_a_name():
    with db_session() as db:
        if not db.query(User).filter_by(agent_id=COACH).first():
            db.add(User(id='agent-' + COACH, username='syllable_coach',
                        display_name=COACH_NAME, user_type='agent', agent_id=COACH,
                        api_token='tok-coach'))


def _ask(component, agent=COACH):
    from integrations.agent_engine import person_ask
    return person_ask.ask('10202', agent, dict(component), prompt_id=agent)


def _row(ask_id):
    with db_session(commit=False) as db:
        n = db.query(Notification).filter_by(id=ask_id).first()
        return None if n is None else n.message


def test_an_ask_with_no_reason_is_not_sent(legs):
    """THE 17:02 CARD: nothing in it said why; now it never reaches her."""
    out = _ask(AS_SENT)
    assert out['ok'] is False
    assert out['refused'] == 'say_why_in_description'
    assert legs['cards'] == []
    _settle()
    assert legs['pushes'] == []


def test_the_card_names_the_agent_and_carries_its_reason(legs):
    _coach_has_a_name()
    out = _ask(dict(AS_SENT, description=WHY))
    assert out['ok'] is True, out
    _agent, card, _user = legs['cards'][-1]
    assert COACH_NAME in card['title']
    assert card['description'] == WHY
    # The bell row and the push say the same as the card.
    assert _row(out['ask_id']) == card['title']
    _settle()
    titles_bodies = [(t, b) for _u, t, b, _d in legs['pushes']]
    assert (card['title'], card['description']) in titles_bodies


def test_an_agents_own_title_stays(legs):
    _coach_has_a_name()
    out = _ask(dict(AS_SENT, title='One short video of Arjun, please', description=WHY))
    _agent, card, _user = legs['cards'][-1]
    assert out['ok'] is True
    assert card['title'] == 'One short video of Arjun, please'
    assert card['description'] == WHY


def test_an_agent_with_no_known_name_still_shows_its_reason(legs):
    out = _ask(dict(AS_SENT, description=WHY), agent='55501')
    _agent, card, _user = legs['cards'][-1]
    assert out['ok'] is True
    assert card['title'] == 'Your agent needs your answer'
    assert card['description'] == WHY


# -- what the tool tells every agent --------------------------------------------
#
# Owner, 2026-11-07: "why is choose video prompt asking in speech therapy agent
# session?"  Measured: the speech-therapy agent 1794031134533 (request
# msg_1794031134534_create) found no camera shared and called ask_person with
# {"type": "file_request", "purpose": "reel_source", "accept": "video"} at
# 11:34:20 and 11:38:39 IST: word for word the one example the tool's hint
# gave, Reel Studio's.  The 11-03 coach card above was the same component.
# Whether a neutral hint changes what the model sends is measured live, not
# here; these tests hold what the tool hands the model.

SPEECH = '1794031134533'


def _what_the_model_reads(prompt_id=SPEECH):
    """ask_person as an agent gets it: its description, its parameters'
    descriptions (the Annotated metadata autogen puts in the tool schema) and
    the function."""
    import typing
    from integrations.agent_engine import person_ask
    _name, desc, func = person_ask.ask_person_tool('10202', prompt_id)
    hints = typing.get_type_hints(func, include_extras=True)
    params = [str(m) for h in hints.values() for m in getattr(h, '__metadata__', ())]
    return desc, params, func


def _json_objects(text):
    """Every complete top-level {...} in ``text`` that parses as a JSON object."""
    import json
    out, depth, start = [], 0, None
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}' and depth:
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    return out


def test_the_tool_holds_up_no_one_agents_purpose():
    """Every purpose is named as often as every other: none is the example
    the model copies (the hint held up Reel Studio's twice, every other
    purpose once)."""
    import re
    from core.user_file_store import INBOUND_PURPOSES
    desc, params, _func = _what_the_model_reads()
    text = '\n'.join([desc] + params)
    counts = {p: len(re.findall(r'\b%s\b' % re.escape(p), text)) for p in INBOUND_PURPOSES}
    assert len(set(counts.values())) == 1 and min(counts.values()) >= 1, counts


def _filled(value):
    """``value`` with every '<...>' blank written over in words of the
    agent's own, the way a model that fills the hint's template would."""
    import re
    if isinstance(value, dict):
        return {k: _filled(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_filled(v) for v in value]
    if isinstance(value, str):
        return re.sub(r'<([^<>]+)>', lambda m: 'Arjun %s' % m.group(1), value)
    return value


def test_the_shape_the_tool_shows_is_an_ask_it_takes_once_filled_in(legs):
    """The hint shows the model the shape of an ask with its reason; filled
    in with the agent's own words, that shape is one the tool sends (the
    first hint's example had no reason, so the tool refused its own
    example)."""
    import json
    desc, params, func = _what_the_model_reads()
    shown = [o for t in [desc] + params for o in _json_objects(t)]
    assert shown, 'the hint shows no ask'
    for component in shown:
        reply = json.loads(func(json.dumps(_filled(component)), 0))
        assert reply.get('status') == 'waiting_for_person', (component, reply)


def test_the_hints_example_copied_word_for_word_never_reaches_her(legs):
    """REVIEW OF ab68963ea, MF-1: the example's blanks ("<why you ask>",
    "<your question>") sent as they are put a card in front of her that
    said "<why you ask>" over a box labelled "<your question>", and its push
    said "<why you ask>".  The 4B model copies the example word for word
    (the cause of the 11-07 cards), so a copy is refused, nothing reaches
    her, and the note names the blank to write over."""
    import json
    desc, params, func = _what_the_model_reads()
    shown = [o for t in [desc] + params for o in _json_objects(t)]
    assert shown, 'the hint shows no ask'
    for component in shown:
        reply = json.loads(func(json.dumps(component), 0))
        assert reply.get('status') == 'refused', (component, reply)
        assert reply['refused'] == 'unfilled_placeholder', reply
        blanks = [v for v in (component.get('description'),
                              *(f.get('label') for f in component.get('fields') or []))
                  if isinstance(v, str) and v.startswith('<')]
        assert any(b in reply['note'] for b in blanks), reply
    assert legs['cards'] == []
    _settle()
    assert legs['pushes'] == []


def test_a_question_label_still_a_blank_is_not_sent(legs):
    """The reason is the agent's own, but the question is the template's."""
    out = _ask({'type': 'form', 'description': WHY,
                'fields': [{'name': 'answer', 'label': '<your question>', 'type': 'text'}]})
    assert out['ok'] is False and out['refused'] == 'unfilled_placeholder'
    assert legs['cards'] == []


def test_a_reason_still_a_blank_is_not_sent(legs):
    """The question is the agent's own, but the reason is the template's;
    a file ask has no question at all, so its reason is all she reads."""
    for component in ({'type': 'form', 'description': '<why you ask>',
                       'fields': [{'name': 'answer', 'label': 'Which word did Arjun say?',
                                   'type': 'text'}]},
                      dict(AS_SENT, description=' <why you ask> ')):
        out = _ask(component)
        assert out['ok'] is False and out['refused'] == 'unfilled_placeholder', component
    assert legs['cards'] == []


def test_words_with_angle_brackets_in_them_are_still_asked(legs):
    """Only a reason or question that IS a blank is refused: the person's
    own words may hold '<' and '>' (a size, a comparison)."""
    out = _ask({'type': 'form', 'description': 'To pick the box size: <30 cm or >50 cm?',
                'fields': [{'name': 'answer', 'label': 'Is the parcel <2 kg?',
                            'type': 'text'}]})
    assert out['ok'] is True, out


def test_a_reason_given_as_the_body_reaches_the_card(legs):
    """REVIEW OF ab68963ea, SF-4: a caller's body= is the card's reason too
    (hartos/reuse_recipe.py's join card asks that way)."""
    from integrations.agent_engine import person_ask
    out = person_ask.ask('10202', COACH, dict(AS_SENT), prompt_id=COACH, body=WHY)
    assert out['ok'] is True, out
    _agent, card, _user = legs['cards'][-1]
    assert card['description'] == WHY


def test_the_tool_says_what_each_purpose_is_for():
    """REVIEW OF ab68963ea, SF-4: the model picks a purpose by what it is
    for, so the description it reads carries each purpose's own words."""
    from core.user_file_store import INBOUND_PURPOSES, PURPOSES
    desc, _params, _func = _what_the_model_reads()
    for p in INBOUND_PURPOSES:
        assert PURPOSES[p].use and PURPOSES[p].use in desc, p


def test_an_ask_with_no_reason_tells_the_agent_to_give_one(legs):
    """The refusal says what to fix, so the next ask can carry the reason."""
    import json
    _desc, _params, func = _what_the_model_reads()
    reply = json.loads(func(json.dumps(AS_SENT), 0))
    assert reply['refused'] == 'say_why_in_description'
    assert 'description' in reply.get('note', ''), reply
    assert legs['cards'] == []
