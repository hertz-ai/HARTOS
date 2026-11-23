"""On the person's own turn, an ask refused for too many open cards says
to ask in the chat, where the person is talking with the agent now.

Live walk, 2026-11-22 14:25-14:26 IST, source Nunba on the local 4B: one
person had used three agents, and three question cards of theirs were still
open (Teach Yourself's file request at 14:12, two from the speech agent at
14:18 and 14:19).  The person then said "Hi" to the cafe agent.  Its step 2
was "get the person's name first (from memory, or ask them), then greet
them".  It asked with ask_person, and the card was refused
(too_many_open_asks).  The note said "They are other agents' questions:
carry on without asking, and ask once one of them is answered", so the
agent asked again three more times, never sent its question in the chat,
and after 60.5 s and 18 model calls the person read "I couldn't finish
that: I did not get an answer ready for you."

The card limit stays (a runaway agent cannot ring the person's phone
without end), and so does the background note.  What changes is what the
agent is told to do instead on the person's own turn: the person is in the
conversation now, so the question goes in the agent's own chat message
(send_message_to_user, expects_reply true), the step's own send.  Owner
2026-11-09: a refusal says what was wrong and what to do.

    python -m pytest tests/unit/test_an_own_turn_ask_past_the_card_limit_asks_in_the_chat.py -q
"""
from __future__ import annotations

import json
import os
import sys

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.social.models import Base, User, db_session, get_engine  # noqa: E402

PERSON = '10202'
#: An ask that is a question (an approval), so it is held to no other rule.
POST = {'type': 'approval', 'action': 'post_the_reel',
        'description': 'Post today\'s reel?'}


def _people():
    Base.metadata.create_all(get_engine())
    with db_session() as db:
        if not db.query(User).filter_by(id=PERSON).first():
            db.add(User(id=PERSON, username=f'p{PERSON}', display_name='P',
                        user_type='human', api_token=f'tok-{PERSON}'))


@pytest.fixture(autouse=True)
def cards(monkeypatch):
    """No ask open at the start; the card, push and realtime legs recorded."""
    from integrations.agent_engine import person_ask
    with person_ask._lock:
        person_ask._asks.clear()
    _people()
    import core.fcm_sync as fcm
    import integrations.agent_engine.liquid_ui_service as lui
    import integrations.social.realtime as rt
    got = []
    monkeypatch.setattr(fcm, 'send_fcm_push', lambda *a, **k: True)
    monkeypatch.setattr(rt, 'publish_event', lambda *a, **k: None)
    monkeypatch.setattr(lui, 'push_agent_ui',
                        lambda agent_id, card, user_id=None: got.append(
                            (agent_id, dict(card))) or True)
    monkeypatch.setattr(lui, 'withdraw_agent_ui', lambda msg_id: 0, raising=False)
    yield got
    with person_ask._lock:
        person_ask._asks.clear()


def _own_turn_tool(agent):
    """The agent's ask_person on the person's own turn (a request id that is
    not a daemon's)."""
    from integrations.agent_engine import person_ask
    return person_ask.ask_person_tool(PERSON, agent,
                                      request_id=lambda: f'req-own-{agent}')[2]


def _background_tool(agent):
    """The same tool on a background turn (a daemon goal's request id)."""
    from integrations.agent_engine import person_ask
    return person_ask.ask_person_tool(PERSON, agent,
                                      request_id=lambda: 'daemon_goal-1')[2]


def _fill(tool):
    from integrations.agent_engine import person_ask
    ids = []
    for i in range(person_ask.MAX_OPEN_PER_PERSON):
        reply = json.loads(tool(json.dumps(dict(POST, action=f'a{i}')), 0))
        assert reply['status'] == 'waiting_for_person', reply
        ids.append(reply['ask_id'])
    return ids


def _refused(tool):
    reply = json.loads(tool(json.dumps(dict(POST, action='one_more')), 0))
    note = reply.pop('note')
    # The failed shape is unchanged: the pipeline reads it as a refusal.
    assert reply == {'answered': False, 'refused': 'too_many_open_asks',
                     'status': 'refused'}, reply
    return note


def _asks_in_the_chat(note):
    return ('send_message_to_user' in note and 'expects_reply' in note
            and 'talking with you now' in note)


def test_other_agents_cards_send_the_question_to_the_chat(cards):
    """The cafe shape: the cards are other agents'; the note names the
    in-chat ask and no longer tells the agent to carry on without asking."""
    _fill(_own_turn_tool('54'))
    sent = len(cards)
    note = _refused(_own_turn_tool('1793372451267'))
    assert len(cards) == sent, 'a refused ask sends no card'
    assert "other agents' questions" in note, note
    assert _asks_in_the_chat(note), note
    assert 'carry on without asking' not in note, note
    assert 'from background work' not in note, note


def test_its_own_cards_are_listed_and_the_chat_is_named(cards):
    """The agent's own three cards: it is told which to read, and that a new
    question goes in the chat."""
    tool = _own_turn_tool('342')
    ids = _fill(tool)
    note = _refused(tool)
    assert all(i in note for i in ids), note
    assert 'ask_person(ask_id=' in note, note
    assert _asks_in_the_chat(note), note


def test_ask_person_is_not_called_again_for_it(cards):
    """'Do not ask again' would read as 'do not ask in the chat either': on
    the person's own turn it names the tool it is about."""
    _fill(_own_turn_tool('54'))
    note = _refused(_own_turn_tool('999'))
    assert 'Do not ask again now with ask_person' in note, note


def test_a_background_turn_keeps_its_note(cards):
    """No one is in a conversation with background work: its note is the
    one it had, with no chat route."""
    _fill(_background_tool('54'))
    note = _refused(_background_tool('999'))
    assert 'from background work' in note, note
    assert "other agents' questions: carry on without asking" in note, note
    assert 'send_message_to_user' not in note, note
    assert 'Do not ask again now.' in note, note


def test_the_card_limit_itself_is_unchanged(cards):
    """Three own-turn cards of other agents still refuse a fourth card; one
    answered makes room, as before."""
    from integrations.agent_engine import person_ask
    ids = _fill(_own_turn_tool('54'))
    _refused(_own_turn_tool('999'))
    person_ask.answer_ask(ids[0], {'decision': 'approve'}, PERSON)
    again = json.loads(_own_turn_tool('999')(json.dumps(dict(POST, action='one_more')), 0))
    assert again['status'] == 'waiting_for_person', again
