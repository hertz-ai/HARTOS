"""On the person's own turn outside a build, a question is asked in the
chat, never put on a card (#295).

Live walk, 2026-11-26 17:00-17:03 IST, installed build #5, local 4B, speech
agent 95568353503, child flow (REUSE): after the child said "amma", the agent
called ask_person four times in about 80 s and three typed quiz cards went
out ("What is the Tamil word for 'hello'?", "Could you tell me anything else
you know about your mother?", "Share your favorite animal story").  A card
offers only a box to type into; the child is three and learning to speak.
The step's own plan named send_message_to_user, whose chat message the page
speaks and the child answers by voice.  The open-ask limit could not stop
the burst: each card's wait had ended before the next was asked, so nothing
held more than one (sub B's check of design_1125 A, v4 M1).

THE RULE (person_ask.ask_person_tool): on the person's own turn, read from
the RUNNING turn's own id (core.agent_tools.is_a_persons_request of
core.llm_outbound_logger.turn_request_id, the camera tools' rule; a
scheduled run or a camera poll runs in no person's turn), outside a build
(the create_run marker), an ask
that is not a file_request is refused 'ask_in_the_chat' before any card,
row or push exists, with the note the card limit already gives on the
person's own turn: ask in your own chat message, with send_message_to_user
and expects_reply true.  A file request keeps its card, a secret still goes
to the vault, a build and background work ask as before, and reading an
earlier ask by its id is unchanged.

THE ASSUMPTION (hartos-17's condition on this rule, 2026-11-26): it refuses
on the turn's kind alone because an own-turn ask can only reach the
session's own person, until role= lands (tests/unit/
test_an_agent_asks_a_member_of_its_chat.py, red with no code yet).
test_an_own_turn_ask_reaches_only_the_sessions_own_person pins that on the
real path and fails the day an own-turn ask can reach another member of the
chat: the rule must then refuse only an ask of the session's own person.

    python -m pytest tests/unit/test_an_own_reuse_turn_asks_in_the_chat.py -q
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
from tests.unit.test_an_agent_asks_another_persons_agent import (  # noqa: E402,F401
    CHILD, P, PARENT, a_joined_parent, env)
from tests.unit.test_person_ask import _settle, legs  # noqa: E402,F401
from tests.unit.test_a_create_runs_marker_reaches_its_tool_on_the_real_chain import (  # noqa: E402,F401
    _build_chat, chain)

PERSON = '10202'
AGENT = '95568353503'
#: The first quiz card of the live turn, as the 4B wrote it.
QUIZ = {'type': 'form', 'description': "Let's practice saying 'hello' in Tamil together!",
        'fields': [{'name': 'answer', 'label': "What is the Tamil word for 'hello'?",
                    'type': 'text'}]}
POST = {'type': 'approval', 'action': 'post_the_reel',
        'description': "Post today's reel?"}
PHOTO = {'type': 'file_request', 'purpose': 'chat_attachment',
         'description': "Please share Zoey's photo so we can use it in the game."}
SECRET = {'type': 'form', 'description': 'To post for you I need your login.',
          'fields': [{'name': 'password', 'type': 'password',
                      'label': 'Your Instagram password?'}]}


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
    from integrations.social import chat_messages
    monkeypatch.setattr(chat_messages, 'publish_new', lambda row: None)
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


def _tool(request_id='msg_1795692632118_6tuxrs'):
    """The agent's ask_person on a turn: by default the child's own turn (a
    request id that is not a daemon's), outside any build.  Each call runs
    inside that turn, its id bound as chat_agent's with_llm_context binds it
    (core.llm_outbound_logger.request_id_context): the rule reads the running
    turn's own id, never only the session's latest one."""
    from core.llm_outbound_logger import request_id_context
    from integrations.agent_engine import person_ask
    func = person_ask.ask_person_tool(PERSON, AGENT, request_id=lambda: request_id)[2]

    def in_the_turn(*args, **kwargs):
        with request_id_context(request_id):
            return func(*args, **kwargs)
    return in_the_turn


def _in_a_build(func, *args):
    from integrations.agent_engine import person_ask
    token = person_ask._create_run_of.set((AGENT, PERSON))
    try:
        return func(*args)
    finally:
        person_ask._create_run_of.reset(token)


def _open_asks():
    from integrations.agent_engine import person_ask
    with person_ask._lock:
        return [a for a in person_ask._asks.values() if a.answered_at is None]


@pytest.mark.parametrize('component', [
    json.dumps(QUIZ),                       # the live quiz card
    json.dumps(POST),                       # an approval
    json.dumps({'description': 'Your turn!',
                'fields': [{'name': 'answer', 'label': 'Your turn!'}]}),  # no type
    'Tell me your name',                    # not JSON at all
])
def test_a_question_on_the_persons_own_turn_goes_in_the_chat(cards, component):
    reply = json.loads(_tool()(component, 0))
    note = reply.pop('note')
    assert reply == {'answered': False, 'refused': 'ask_in_the_chat',
                     'status': 'refused'}, reply
    assert ('talking with you now' in note and 'send_message_to_user' in note
            and 'expects_reply true' in note), note
    # What was wrong, and not to try again with a card (rv_f6, S1).
    assert 'not on a card' in note and 'do not ask it again with ask_person' in note.lower(), note
    assert cards == [], 'no card reached any device'
    assert _open_asks() == [], 'nothing the open-ask limit could count'


def test_the_note_is_the_card_limits_own_turn_note(cards):
    """One sentence sends a question to the chat, whichever refusal says it."""
    from integrations.agent_engine import person_ask
    note = json.loads(_tool()(json.dumps(QUIZ), 0))['note']
    over_limit = person_ask._refusal_note('too_many_open_asks', QUIZ, PERSON, AGENT,
                                          False)
    assert person_ask._ask_in_the_chat() in note
    assert person_ask._ask_in_the_chat() in over_limit


def test_a_file_request_on_the_persons_own_turn_still_gets_its_card(cards):
    reply = json.loads(_tool()(json.dumps(PHOTO), 0))
    assert reply['status'] == 'waiting_for_person', reply
    assert len(cards) == 1 and cards[0][1]['type'] == 'file_request', cards


def test_a_secret_is_still_sent_to_the_vault(cards):
    reply = json.loads(_tool()(json.dumps(SECRET), 0))
    assert reply['refused'] == 'secrets_go_to_the_vault', reply
    assert 'send_message_to_user' not in reply['note'], reply['note']
    assert cards == []


def test_a_builds_question_still_gets_its_card(cards):
    reply = json.loads(_in_a_build(_tool(), json.dumps(QUIZ), 0))
    assert reply['status'] == 'waiting_for_person', reply
    assert len(cards) == 1, cards


def test_a_background_turns_question_still_gets_its_card(cards):
    reply = json.loads(_tool('daemon_goal-1')(json.dumps(QUIZ), 0))
    assert reply['status'] == 'waiting_for_person', reply
    assert len(cards) == 1, cards


def test_reading_an_earlier_ask_by_its_id_is_unchanged(cards):
    reply = json.loads(_tool()('', 0, 'no-such-ask'))
    assert reply['status'] == 'not_found' and reply['answered'] is False, reply


def test_an_own_turn_ask_reaches_only_the_sessions_own_person(env, legs):
    """THE ASSUMPTION, on the real path.  The child's chat has the parent
    joined (the persona registry a role= ask would read).  On the child's
    own turn, in a build so that a card still goes out, the child's agent
    asks: the card reaches the child and no one else, and the tool takes no
    argument that could name the parent.  Green with and without the rule.
    The day an own-turn ask can reach another member of the chat this
    fails, and the rule must then refuse only an ask of the session's own
    person (person_to_ask)."""
    from integrations.agent_engine import person_ask
    for uid, name in ((CHILD, 'Arjun'), (PARENT, 'Meena')):
        with db_session() as db:
            if not db.query(User).filter_by(id=uid).first():
                db.add(User(id=uid, username=f'n_{uid}', display_name=name,
                            user_type='human', api_token=f'tok-{uid}'))
    from core.llm_outbound_logger import request_id_context
    a_joined_parent(env)
    func = person_ask.ask_person_tool(CHILD, P, request_id=f'req-{CHILD}-own')[2]
    token = person_ask._create_run_of.set((str(P), CHILD))
    try:
        with request_id_context(f'req-{CHILD}-own'):
            mine = json.loads(func(json.dumps(QUIZ), 0))
            try:
                theirs = func(json.dumps(POST), 0, role='Parent')
            except TypeError:
                theirs = None          # no argument names anyone else today
    finally:
        person_ask._create_run_of.reset(token)
    _settle()
    assert mine['status'] == 'waiting_for_person', mine
    assert {to for _agent, _card, to in legs['cards']} == {CHILD}, legs['cards']
    assert theirs is None, (
        'ask_person now takes role=: an own-turn ask can reach another member '
        "of the chat, so the rule must refuse only an ask of the session's own "
        'person (person_to_ask)', theirs)


def test_a_scheduled_run_in_a_session_she_chatted_in_still_gets_its_card(cards):
    """rv_f6's M1: a timed job or a camera poll runs in no person's turn,
    though the session's latest /chat id is hers (what request_id() reads).
    Its question is a card, as before: the rule reads the running turn's own
    id, and no turn is bound here."""
    from integrations.agent_engine import person_ask
    func = person_ask.ask_person_tool(PERSON, AGENT,
                                      request_id=lambda: 'msg_1795692632118_6tuxrs')[2]
    reply = json.loads(func(json.dumps(QUIZ), 0))
    assert reply['status'] == 'waiting_for_person', reply
    assert len(cards) == 1, cards


def test_a_reuse_turn_on_autogen_s_real_chain_asks_in_the_chat(chain):
    """The running turn's id reaches the tool on REUSE's own path: a function
    decorated as reuse_recipe.chat_agent is (with_llm_context('autogen.reuse'),
    no build marker) runs autogen's real group chat, HARTOS's patched
    execute_function, register_core_tools, log_tool_execution and the real
    ask_person (the harness of test_a_create_runs_marker_reaches_its_tool_on_
    the_real_chain.py).  The tool runs on chat_agent's own thread, inside the
    turn it binds, so the question is refused into the chat before ask()."""
    from core.llm_outbound_logger import with_llm_context

    @with_llm_context('autogen.reuse')
    def chat_agent(user_id, text, prompt_id, file_id, request_id):
        executor, manager, gc = _build_chat(user_id, prompt_id, 'Say ball with me?')
        executor.initiate_chat(manager, message=text, clear_history=False)
        return [json.loads(m['content']) for m in gc.messages
                if (m.get('role') == 'tool' or m.get('tool_responses'))
                and str(m.get('content') or '').startswith('{')]

    replies = chat_agent(PERSON, 'ba', AGENT, None, 'msg_1795692632118_6tuxrs')
    assert replies and all(r.get('refused') == 'ask_in_the_chat' for r in replies), replies
    assert chain['seen'] == [], 'ask() ran: a card was made'
