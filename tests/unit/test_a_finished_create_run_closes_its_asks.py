"""A finished CREATE run's question leaves every surface; an ask kept past its
time does too.

Owner report, 2026-11-23: an agent's question card from a CREATE run that had
already finished ("Agent Created Successfully") came up on the desktop
minutes later and covered the screen.  Measured at HEAD that day: nothing
closed an ask when its run ended.  person_ask._prune only forgot an open ask
from memory after OPEN_KEEP_S (24 h); _close, which tells every surface
(ask.answered) and marks the bell row read, ran only for an answer, a
dismissal or a late answer after a restart.

Now:
  * create_recipe.recipe is a CREATE run (person_ask.create_run): an ask made
    inside it belongs to that run, and once the run answers that the agent is
    built, every ask a CREATE run of that agent made that is still open is
    closed through _close; nothing waits on it (a waiting tool call wakes,
    held hive work goes back) and the agent reads 'expired', the record its
    ask_person tool already reads.
  * An ask made outside a CREATE run (a REUSE turn's) is untouched: it may
    wait across turns.  So is a run that has not finished.
  * _prune closes the open asks it drops, so an expired ask leaves every
    surface too.

The harness of test_person_ask.py: real NotificationService, realtime
fan-out and in-memory social DB; Firebase, WAMP and the Liquid UI card
recorded at their boundaries.

    python -m pytest tests/unit/test_a_finished_create_run_closes_its_asks.py -q --noconftest
"""
from __future__ import annotations

import ast
import json
import os
import sys
import threading
import time

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.unit.test_person_ask import (  # noqa: E402,F401
    POST, _answered_events, _ask, _no_open_asks, _row, _settle, legs)

BUILT = 'Agent Created Successfully'


def _create_run(asks, reply=BUILT, prompt_id='342', during=None):
    """A CREATE run of agent ``prompt_id`` as create_recipe.recipe is one:
    decorated with person_ask.create_run, taking recipe()'s arguments.  Its
    step asks through the agent's own ask_person tool (each of ``asks`` a
    component), holds the ask (wait 0), runs ``during()`` while it is still
    running (another run, which then starts and finishes inside this one's
    lifetime), and answers ``reply``."""
    from integrations.agent_engine import person_ask
    held = []

    @person_ask.create_run
    def recipe(user_id, text, prompt_id, file_id, request_id, autonomous=False,
               text_is_the_persons=True):
        func = person_ask.ask_person_tool(user_id, prompt_id)[2]
        for comp in asks:
            got = json.loads(func(json.dumps(comp), 0))
            assert got['status'] == 'waiting_for_person', got
            held.append(got['ask_id'])
        if during is not None:
            during()
        if isinstance(reply, Exception):
            raise reply
        return reply

    return recipe, held


def _q(action, question):
    return {'type': 'approval', 'action': action, 'description': question}


def _read(ask_id, prompt_id='342'):
    """What the asking agent reads for ``ask_id`` through its tool."""
    from integrations.agent_engine import person_ask
    return json.loads(person_ask.ask_person_tool('10202', prompt_id)[2]('', 0, ask_id))


def test_a_finished_create_run_closes_its_open_ask_on_every_surface(legs):
    from integrations.agent_engine import person_ask
    recipe, held = _create_run([POST])
    assert recipe('10202', 'make me a reel agent', '342', None, 'msg_T1') == BUILT
    _settle()
    (ask_id,) = held
    assert len(_answered_events(legs, ask_id)) == 1      # every surface drops it
    assert _row(ask_id)['is_read'] is True               # the bell stops listing it
    assert person_ask.ask_state(ask_id, '10202') == {'status': 'expired', 'answer': None}
    assert person_ask.open_ask_ids('10202', '342', background=True) == []
    assert person_ask.open_ask_ids('10202', '342', background=False) == []


def test_the_asking_agent_reads_that_its_ask_closed_unanswered(legs):
    recipe, held = _create_run([POST])
    recipe('10202', 'make me a reel agent', '342', None, 'msg_T1')
    _settle()
    reply = _read(held[0])
    assert reply['answered'] is False and reply['status'] == 'expired'
    assert 'the run that asked it finished' in reply['note']


def test_her_answer_after_the_run_finished_is_too_late(legs):
    from integrations.agent_engine import person_ask
    recipe, held = _create_run([POST])
    recipe('10202', 'make me a reel agent', '342', None, 'msg_T1')
    assert person_ask.answer_ask(held[0], {'decision': 'approve'}, '10202') == (
        {'status': 'error', 'reason': 'expired'}, 410)


def test_a_tool_call_still_waiting_wakes_when_the_run_finishes(legs):
    """A step waiting on the person (a second tool call of the same run, say)
    is not left waiting on an ask that is gone."""
    from integrations.agent_engine import person_ask
    got = {}

    @person_ask.create_run
    def finishing(user_id, text, prompt_id, file_id, request_id):
        func = person_ask.ask_person_tool(user_id, prompt_id)[2]
        ask_id = json.loads(func(json.dumps(POST), 0))['ask_id']   # asks, holds

        def wait():
            t0 = time.monotonic()
            got['answer'] = person_ask.wait_for_answer(ask_id, 5)
            got['waited'] = time.monotonic() - t0
        got['t'] = threading.Thread(target=wait)
        got['t'].start()
        time.sleep(0.1)
        return BUILT

    finishing('10202', 'x', '342', None, 'msg_T1')
    got['t'].join(timeout=6)
    assert got['answer'] is None and got['waited'] < 2


def test_a_reuse_turns_ask_of_the_same_agent_is_untouched(legs):
    from integrations.agent_engine import person_ask
    reuse = _ask(component=dict(POST), agent_id='342', prompt_id='342')['ask_id']
    recipe, held = _create_run([{'type': 'approval', 'action': 'name_it',
                                 'description': 'Call it Reely?'}])
    recipe('10202', 'make me a reel agent', '342', None, 'msg_T1')
    _settle()
    assert person_ask.ask_state(held[0], '10202')['status'] == 'expired'
    assert person_ask.ask_state(reuse, '10202') == {'status': 'open', 'answer': None}
    assert _row(reuse)['is_read'] is False
    assert _answered_events(legs, reuse) == []


def test_a_run_that_has_not_finished_leaves_its_ask_open(legs):
    from integrations.agent_engine import person_ask
    for reply in ('Which song should the reel use?', None, False,
                  {'status': 'Plan Mode'}, RuntimeError('the model went away')):
        with person_ask._lock:
            person_ask._asks.clear()
        recipe, held = _create_run([POST], reply=reply)
        try:
            recipe('10202', 'make me a reel agent', '342', None, 'msg_T1')
        except RuntimeError:
            pass
        assert person_ask.ask_state(held[0], '10202')['status'] == 'open', reply
        assert _answered_events(legs, held[0]) == [], reply


#  The four scope rules of the review of 93937a53c (which closed by agent
#  only), each failing on that parent.

def test_1_two_people_on_one_agent_never_close_each_others_asks(legs):
    """User B's CREATE run of agent 342 asks and is still running when user
    A's CREATE run of 342 asks and finishes: A's ask closes, B's stays open
    until B's own run finishes."""
    from integrations.agent_engine import person_ask
    a_run, a_held = _create_run([_q('a_name', 'Call it Reely?')], reply=BUILT)
    b_run, b_held = _create_run(
        [_q('b_name', 'Call it Clipper?')], reply=BUILT,
        during=lambda: a_run('10202', 'make me a reel agent', '342', None, 'msg_A1'))
    observed = {}
    orig = person_ask.close_create_run_asks

    def close_and_look(*key):
        n = orig(*key)
        if 'b_after_a' not in observed:     # the first close is A's run's
            observed['b_after_a'] = person_ask.ask_state(b_held[0], '777')['status']
        return n
    person_ask.close_create_run_asks = close_and_look
    try:
        b_run('777', 'make me a reel agent', '342', None, 'msg_B1')
    finally:
        person_ask.close_create_run_asks = orig
    _settle()
    assert observed['b_after_a'] == 'open'
    assert person_ask.ask_state(a_held[0], '10202')['status'] == 'expired'
    assert person_ask.ask_state(b_held[0], '777')['status'] == 'expired'   # B's own finish


def test_2_a_phone_users_asks_on_the_owners_desktop_close_only_with_that_users_run(legs):
    """The owner (10202) and a phone user signed in as themselves (777) both
    build agent 342 on the owner's desktop.  The phone user's run finishing
    leaves the owner's open question; the owner's run finishing leaves the
    phone user's."""
    from integrations.agent_engine import person_ask
    phone_done, phone_held = _create_run([_q('p', 'Use my voice?')], reply=BUILT)
    owner_open, owner_held = _create_run(
        [_q('o', 'Post it daily?')], reply='Waiting on your answer.',
        during=lambda: phone_done('777', 'make me a reel agent', '342', None, 'msg_P1'))
    owner_open('10202', 'make me a reel agent', '342', None, 'msg_O1')
    _settle()
    assert person_ask.ask_state(phone_held[0], '777')['status'] == 'expired'
    assert person_ask.ask_state(owner_held[0], '10202') == {'status': 'open', 'answer': None}
    assert _answered_events(legs, owner_held[0]) == []

    owner_done, owner_held2 = _create_run([_q('o2', 'Post it weekly?')], reply=BUILT)
    phone_open, phone_held2 = _create_run(
        [_q('p2', 'Use my photo?')], reply='Waiting.',
        during=lambda: owner_done('10202', 'go on', '342', None, 'msg_O2'))
    phone_open('777', 'go on', '342', None, 'msg_P2')
    _settle()
    assert person_ask.ask_state(owner_held2[0], '10202')['status'] == 'expired'
    assert person_ask.ask_state(phone_held2[0], '777') == {'status': 'open', 'answer': None}


def test_3_a_finish_closes_only_its_own_runs_asks(legs):
    """The first run of agent 342 for 10202 asked and stopped; a later run of
    the same agent for the same user finished: the first run's ask is that
    run's, and stays open.  Another agent's run is untouched too."""
    from integrations.agent_engine import person_ask
    first, first_held = _create_run([POST], reply='Waiting on your answer.')
    first('10202', 'make me a reel agent', '342', None, 'msg_T1')
    other, other_held = _create_run([_q('other', 'Use my voice?')],
                                    reply='Waiting.', prompt_id='777')
    other('10202', 'make me a voice agent', '777', None, 'msg_T2')
    second, second_held = _create_run([_q('second', 'Call it Reely?')], reply=BUILT)
    second('10202', 'go on', '342', None, 'msg_T3')
    _settle()
    assert person_ask.ask_state(second_held[0], '10202')['status'] == 'expired'
    assert person_ask.ask_state(first_held[0], '10202') == {'status': 'open', 'answer': None}
    assert _answered_events(legs, first_held[0]) == []
    assert person_ask.ask_state(other_held[0], '10202')['status'] == 'open'


def test_4_a_run_with_no_user_id_closes_nothing(legs):
    """A run whose user id is missing marks nothing and closes nothing: not
    its own asks, not another run's, not everything of the agent."""
    from integrations.agent_engine import person_ask
    for missing in (None, ''):
        with person_ask._lock:
            person_ask._asks.clear()
        reuse = _ask(component=_q('r', 'Keep the music?'), agent_id='342',
                     prompt_id='342')['ask_id']
        still, still_held = _create_run([_q('s', 'Call it Reely?')], reply='Waiting.')
        still('10202', 'make me a reel agent', '342', None, 'msg_T1')
        nobody_held = []

        @person_ask.create_run
        def nobody(user_id, text, prompt_id, file_id, request_id):
            # a framework ask inside the run (the tool needs a person)
            nobody_held.append(_ask(component=_q('n', 'Post it daily?'), agent_id='342',
                                    prompt_id='342')['ask_id'])
            return BUILT
        nobody(missing, 'make me a reel agent', '342', None, 'msg_T2')
        _settle()
        for ask_id in (reuse, still_held[0], nobody_held[0]):
            with person_ask._lock:
                a = person_ask._asks.get(ask_id)
            assert a is not None and a.answered_at is None, (missing, ask_id)
    assert person_ask.close_create_run_asks('342', '', 'r1') == 0
    assert person_ask.close_create_run_asks('342', None, 'r1') == 0
    assert person_ask.close_create_run_asks(None, '10202', 'r1') == 0
    assert person_ask.close_create_run_asks('342', '10202', '') == 0


def test_the_waiting_tool_call_reads_expired_when_the_run_ends(legs):
    """A step's ask_person call still waiting when its run's close lands is
    told its ask is gone, not that the person has yet to answer."""
    from integrations.agent_engine import person_ask
    got = {}

    @person_ask.create_run
    def recipe(user_id, text, prompt_id, file_id, request_id):
        func = person_ask.ask_person_tool(user_id, prompt_id)[2]
        closer = threading.Timer(0.3, person_ask.close_create_run_asks,
                                 args=person_ask._create_run_of.get())
        closer.start()
        got['reply'] = json.loads(func(json.dumps(POST), 5))
        closer.join()
        return 'Waiting.'

    t0 = time.monotonic()
    recipe('10202', 'make me a reel agent', '342', None, 'msg_T1')
    assert time.monotonic() - t0 < 3
    assert got['reply']['answered'] is False and got['reply']['status'] == 'expired'
    assert 'the run that asked it finished' in got['reply']['note']


def test_an_answered_ask_of_the_run_stays_answered(legs):
    from integrations.agent_engine import person_ask
    before = {}

    def answer_it():
        assert person_ask.answer_ask(held[0], {'decision': 'approve'}, '10202')[1] == 200
        _settle()
        before['n'] = len(_answered_events(legs, held[0]))
    recipe, held = _create_run([POST], reply=BUILT, during=answer_it)
    recipe('10202', 'make me a reel agent', '342', None, 'msg_T1')
    _settle()
    assert person_ask.ask_state(held[0], '10202') == {
        'status': 'answered', 'answer': {'decision': 'approve'}}
    assert len(_answered_events(legs, held[0])) == before['n']


def test_an_ask_made_outside_any_run_is_never_a_runs():
    from integrations.agent_engine import person_ask
    assert person_ask._create_run_of.get() is None


# -- an ask kept past its time leaves every surface ----------------------------

def test_an_open_ask_pruned_after_its_time_closes_on_every_surface(legs):
    from integrations.agent_engine import person_ask
    old = _ask(component=dict(POST))['ask_id']
    with person_ask._lock:
        person_ask._asks[old].created -= person_ask.OPEN_KEEP_S + 1
    _ask(component={'type': 'approval', 'action': 'another',
                    'description': 'Another?'})             # prunes on the way in
    _settle()
    assert len(_answered_events(legs, old)) == 1
    assert _row(old)['is_read'] is True
    assert person_ask.ask_state(old, '10202') == {'status': 'expired', 'answer': None}


def test_an_answered_ask_pruned_after_its_time_is_not_announced_again(legs):
    from integrations.agent_engine import person_ask
    done = _ask(component=dict(POST))['ask_id']
    assert person_ask.answer_ask(done, {'decision': 'deny'}, '10202')[1] == 200
    _settle()
    before = len(_answered_events(legs, done))
    with person_ask._lock:
        person_ask._asks[done].answered_at -= person_ask.ANSWERED_KEEP_S + 1
    _ask(component={'type': 'approval', 'action': 'another',
                    'description': 'Another?'})
    _settle()
    assert len(_answered_events(legs, done)) == before
    assert person_ask.ask_state(done, '10202')['status'] == 'expired'


# -- the CREATE run is create_recipe.recipe --------------------------------------

def test_create_recipes_recipe_is_a_create_run():
    """create_recipe imports the whole agent stack, so its decorators are read
    from the module's syntax tree: recipe() carries person_ask.create_run,
    inside with_llm_context (which binds the turn's id first)."""
    path = os.path.join(ROOT, 'hartos', 'create_recipe.py')
    with open(path, encoding='utf-8') as fh:
        tree = ast.parse(fh.read())
    aliases = {a.asname or a.name: (node.module, a.name)
               for node in tree.body if isinstance(node, ast.ImportFrom)
               for a in node.names}
    (recipe,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'recipe']
    names = [d.func.id if isinstance(d, ast.Call) else d.id for d in recipe.decorator_list]
    assert aliases[names[-1]] == ('integrations.agent_engine.person_ask', 'create_run')
    assert aliases[names[0]] == ('core.llm_outbound_logger', 'with_llm_context')
