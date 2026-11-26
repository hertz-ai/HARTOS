"""ask_person(role=...): an agent asks ANOTHER member of its joined chat, the
person, on that person's devices.

Owner, 2026-11-23T12:12:55Z, asked "When a parent or therapist joins the
child's chat, can the child's agent ask them, and their agent, for what it
needs?", chose "Yes, joining allows it": joining the chat lets every
member's agent ask the others.  Their AGENT is asked with
send_message_to_roles (tests/unit/test_an_agent_asks_another_persons_agent.py);
the PERSON is asked here, with the one ask machinery every agent already has
(integrations.agent_engine.person_ask): no new tool, no new path.  The member
is named by ``role`` and found by the same resolver send_message_to_roles
uses (core.persona_registry._others_in_the_chat), so the same people can be
asked, and the same people are refused.

Every ask has two identities:

  asked_of   the member asked (the parent): their card, their bell row,
             their push, and only their answer is taken.
  asked_for  the turn or run that asked (the child's): its wait, its read
             of the answer by id, its CREATE run's close, the asks a
             refusal lists as its own, and the question its own chat shows
             (reuse_recipe._reuse_card_question).

The open-ask limit stays the asked person's (MAX_OPEN_PER_PERSON): it is
their phone that rings.

Each test below pins one reader to one of the two.

Written RED before the change (no production code yet).  On HEAD 12fd0f819
every ask_person(role=...) call fails with "unexpected keyword argument
'role'": there is no way for an agent to ask anyone but the person it is
serving (person_ask.person_to_ask).  The card-question tests fail on what the
child's chat would read.

Real person_ask asks, real consent store (in-memory), real persona registry,
flow_progress and agent-data files; the card push, the Firebase send and the
realtime publish are recorded at their boundaries (the ``legs`` harness of
tests/unit/test_person_ask.py); the REUSE reply rules run real (the harness of
tests/unit/test_a_question_card_is_the_reply_not_its_narration.py).

    python -m pytest tests/unit/test_an_agent_asks_a_member_of_its_chat.py -q
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from tests.unit.test_an_agent_asks_another_persons_agent import (  # noqa: E402,F401
    CHILD, P, PARENT, THERAPIST, a_joined_parent, childs_chat, env, revoke, saved,
    uses)
from tests.unit.test_person_ask import (  # noqa: E402,F401
    _no_open_asks, _row, _settle, legs)
from tests.unit.test_a_question_card_is_the_reply_not_its_narration import (  # noqa: E402,F401
    NARRATION, QUESTION, SPEECH, THE_CARD, WAITING, _card_call, _card_result, _prose,
    _run, _seed, _verdict, _what_the_person_reads, rr)

FATHER = 'u-father'
NAMES = {CHILD: 'Arjun', PARENT: 'Meena', FATHER: 'Ravi', THERAPIST: 'Dr Rao'}
HOME_NAME = 'What does the family call Arjun at home?'
CARD = {'type': 'form',
        'description': 'So the lesson calls Arjun by the name he answers to.',
        'fields': [{'name': 'home_name', 'label': HOME_NAME, 'type': 'text'}]}
ANSWER = {'home_name': 'Chinnu'}


def people():
    """The members as users of this node, by the names their cards use."""
    from integrations.social.models import User, db_session
    for uid, name in NAMES.items():
        with db_session() as db:
            if not db.query(User).filter_by(id=uid).first():
                db.add(User(id=uid, username=f'n_{uid}', display_name=name,
                            user_type='human', api_token=f'tok-{uid}'))


def tool(user):
    """The ask_person tool of ``user``'s agent session, on their own turn."""
    from integrations.agent_engine import person_ask
    name, desc, func = person_ask.ask_person_tool(user, P, request_id=f'req-{user}-1')
    assert name == 'ask_person'
    return func


def asked(func, card=CARD, wait=0, **kw):
    return json.loads(func(json.dumps(card), wait, **kw))


def read(func, ask_id, **kw):
    return json.loads(func('', 0, ask_id=ask_id, **kw))


def cards_to(legs, user):
    return [card for _agent, card, to in legs['cards'] if to == user]


def open_asks():
    from integrations.agent_engine import person_ask
    with person_ask._lock:
        return list(person_ask._asks.values())


def a_family(env, *more):
    """The child's chat with the parent joined (and ``more`` joiners)."""
    people()
    a_joined_parent(env)
    for joiner, persona in more:
        uses((joiner, persona))
        assert env.pr.record_join(CHILD, P, joiner) is True
        env.sessions[f'{CHILD}_{P}'].append(
            {'agentInstanceID': f'com.hertzai.hevolve.chat.{P}.{joiner}',
             'user_id': joiner, 'role': persona, 'deviceID': 'something'})


# ── asked_of: the parent's card, bell, push and answer ────────────────────

def test_the_childs_agent_puts_the_question_on_the_parents_devices(env, legs):
    a_family(env)
    reply = asked(tool(CHILD), role='Parent')
    _settle()
    assert reply['status'] == 'waiting_for_person', reply
    ask_id = reply['ask_id']
    assert [c['ask_id'] for c in cards_to(legs, PARENT)] == [ask_id]
    assert cards_to(legs, CHILD) == []                   # never the turn's person
    assert _row(ask_id)['user_id'] == PARENT             # the parent's bell
    assert {uid for uid, *_rest in legs['pushes']} == {PARENT}
    # The tool reply says whom it asked.
    assert 'Parent' in reply['note']


def test_only_the_parent_answers_it(env, legs):
    from integrations.agent_engine import person_ask
    a_family(env)
    ask_id = asked(tool(CHILD), role='Parent')['ask_id']
    for who in (CHILD, THERAPIST, None):
        body, code = person_ask.answer_ask(ask_id, ANSWER, who)
        assert (code, body.get('reason')) == (403, 'not_yours'), who
    body, code = person_ask.answer_ask(ask_id, ANSWER, PARENT)
    assert code == 200, body


def test_the_card_says_the_answer_goes_to_the_childs_agent(env, legs):
    a_family(env)
    asked(tool(CHILD), role='Parent')
    card = cards_to(legs, PARENT)[0]
    assert "your answer goes to arjun's agent" in card['description'].lower(), card


# ── asked_for: the child's wait, read-back and run ────────────────────────

def test_the_childs_wait_returns_the_parents_answer_when_it_comes_in_time(env, legs):
    from integrations.agent_engine import person_ask
    a_family(env)
    func = tool(CHILD)

    def parent_answers():
        for _ in range(100):
            mine = cards_to(legs, PARENT)
            if mine:
                person_ask.answer_ask(mine[-1]['ask_id'], ANSWER, PARENT)
                return
            time.sleep(0.05)

    answering = threading.Thread(target=parent_answers)
    answering.start()
    reply = asked(func, wait=5, role='Parent')
    answering.join(timeout=6)
    assert reply['answered'] is True and reply['answer'] == ANSWER, reply


def test_the_childs_agent_reads_the_answer_by_id_and_the_parents_agent_does_not(env, legs):
    from integrations.agent_engine import person_ask
    a_family(env)
    child = tool(CHILD)
    ask_id = asked(child, role='Parent')['ask_id']
    assert read(child, ask_id)['status'] == 'waiting_for_person'
    assert person_ask.answer_ask(ask_id, ANSWER, PARENT)[1] == 200
    got = read(child, ask_id)
    assert got['answered'] is True and got['answer'] == ANSWER, got
    # The parent's own agent did not ask it, so it is not its to read.
    assert read(tool(PARENT), ask_id)['status'] == 'not_found'


def test_a_parent_ask_made_in_the_childs_build_closes_when_the_build_finishes(env, legs):
    """The CREATE-asks close (41b299cbb and its forward fix: agent + user +
    any CREATE run) keys on asked_for: the run is the child's, the card the
    parent's.  The child's agent then reads 'expired' and the parent's
    devices drop the card."""
    from integrations.agent_engine import person_ask
    a_family(env)
    child, made = tool(CHILD), []

    @person_ask.create_run
    def recipe(user_id, prompt_id):
        made.append(asked(child, role='Parent')['ask_id'])
        return person_ask.AGENT_BUILT[0]

    assert recipe(CHILD, P) == person_ask.AGENT_BUILT[0]
    _settle()
    assert len(made) == 1, made
    assert read(child, made[0])['status'] == 'expired'
    dropped = [to for topic, data, to in legs['events']
               if topic == 'chat.social' and data.get('type') == 'ask.answered'
               and data.get('ask_id') == made[0]]
    assert dropped == [PARENT]


# ── one ask per (asked_of, asked_for, question); no flood ─────────────────

def test_the_childs_agent_and_the_parents_own_agent_asking_alike_are_two_asks(env, legs):
    a_family(env)
    by_child = asked(tool(CHILD), role='Parent')['ask_id']
    by_parent = asked(tool(PARENT))['ask_id']            # the parent's own turn
    assert by_child != by_parent
    assert [c['ask_id'] for c in cards_to(legs, PARENT)] == [by_child, by_parent]
    # The same agent asking the same member the same thing is one ask.
    assert asked(tool(CHILD), role='Parent')['ask_id'] == by_child


def test_one_question_asked_in_a_loop_is_one_card_on_the_parents_phone(env, legs):
    a_family(env)
    child = tool(CHILD)
    ids = {asked(child, role='Parent')['ask_id'] for _ in range(20)}
    assert len(ids) == 1
    assert len(cards_to(legs, PARENT)) == 1


def _question(i):
    return dict(CARD, fields=[{'name': f'q{i}', 'label': f'Question {i} for you?',
                               'type': 'text'}])


def test_twenty_questions_stop_at_the_parents_open_ask_limit(env, legs):
    """The limiter is the parent's (asked_of): their phone holds at most
    MAX_OPEN_PER_PERSON open questions, their own agent's included.  What the
    refusal tells the child's agent is the child's agent's (asked_for): its
    own open asks, and never "ask in your own chat" (send_message_to_user),
    which would put the parent's question to the child."""
    from integrations.agent_engine import person_ask
    a_family(env)
    parents_own = asked(tool(PARENT), card=_question('own'))['ask_id']
    child = tool(CHILD)
    replies = [asked(child, card=_question(i), role='Parent') for i in range(20)]
    assert len(cards_to(legs, PARENT)) == person_ask.MAX_OPEN_PER_PERSON
    took = [r['ask_id'] for r in replies if r['status'] == 'waiting_for_person']
    refused = [r for r in replies if r['status'] == 'refused']
    assert len(took) == person_ask.MAX_OPEN_PER_PERSON - 1
    assert len(refused) == 20 - len(took)
    assert {r['refused'] for r in refused} == {'too_many_open_asks'}
    for r in refused:
        assert all(i in r['note'] for i in took), r['note']
        assert parents_own not in r['note'], r['note']
        assert 'send_message_to_user' not in r['note'], r['note']
    assert cards_to(legs, CHILD) == []


# ── a role that does not resolve: refused by name, never the turn's person ─

@pytest.mark.parametrize('case', ['unknown', 'two_parents', 'revoked', 'nobody_joined'])
def test_a_role_that_names_no_one_member_is_refused_and_asks_no_one(env, legs, case):
    people()
    if case == 'unknown':
        a_family(env, (THERAPIST, 'Therapist'))
        role, named = 'Teacher', ('Parent', 'Therapist')
    elif case == 'two_parents':
        a_family(env, (FATHER, 'Parent'))
        role, named = 'Parent', (PARENT, FATHER)
    elif case == 'revoked':
        a_family(env, (THERAPIST, 'Therapist'))
        revoke(CHILD, PARENT)
        role, named = 'Parent', ('Therapist',)
    else:
        uses((CHILD, 'Child'))
        childs_chat(env)
        role, named = 'Parent', ('no other person has joined this chat',)
    reply = asked(tool(CHILD), role=role)
    assert reply['status'] == 'refused' and reply['answered'] is False, reply
    for name in named:
        assert name in reply['note'], (name, reply['note'])
    assert legs['cards'] == [] and open_asks() == []     # no card, not even the child's


def test_the_tool_tells_the_model_what_role_is_for(env):
    from integrations.agent_engine import person_ask
    _name, desc, _func = person_ask.ask_person_tool(CHILD, P)
    assert 'ask a member of this chat instead of the person you are serving' in desc.lower(), desc


# ── the child's own chat: the parent's question is not the child's ────────

def _a_parent_card_turn():
    """Action 3 still working: this turn's card went to the PARENT (the call
    names role 'Parent'), the card waits for them, then the model's words
    and a pending verdict."""
    return [_seed(),
            _card_call('call_card', THE_CARD, role='Parent'),
            _card_result('call_card', WAITING),
            _prose(NARRATION),
            _verdict()]


def test_the_childs_chat_does_not_show_the_parents_question(rr, monkeypatch):
    """RED on HEAD: the child read "What is your mother called in Tamil?",
    the question put to the parent."""
    _posted, chat, _seat = _run(rr, monkeypatch, _a_parent_card_turn(),
                                round_messages=[_prose(NARRATION)])
    assert _what_the_person_reads(rr, chat) != QUESTION


def test_the_childs_reply_is_not_taken_as_the_parents_answer(rr, monkeypatch):
    """The question a working step waits on is _reuse_turn_delivered_send
    (asks=True): it becomes the turn's reply, is recorded as the step's open
    ask (via 'reply', _reuse_ask_index), and the CHILD's next message is then
    recorded as its answer.  A card asked of the parent is no question of the
    child's.  RED on HEAD: the parent's question came back."""
    history = _a_parent_card_turn()
    monkeypatch.setattr(rr, '_reuse_turn_start',
                        {SPEECH: {'seed_text': history[0]['content']}}, raising=True)
    assert rr._reuse_turn_delivered_send(SPEECH, history, asks=True) is None
