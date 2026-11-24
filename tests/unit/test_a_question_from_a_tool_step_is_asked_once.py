"""A step whose banked tool ran and whose question reached the person holds
for their reply: the question is asked once, and the read that led to it is
not owed again after the reply.

WHAT WENT WRONG (live 2026-11-24 16:44 IST, installed HARTOS 6717104f6, Cafe
Flow 344, a turn from the tablet, request 0507de59, langchain.log):

    action 1 = banked get_data_by_key 'last_tip'.  In its window the 4B also
    16:44:23  [SEND-KIND] 10202_344 action=1 ... expects_reply=True
              "Hello! ... could you please tell me your name?"
              [FAB-GUARD] action 1 ... executed=['get_data_by_key',
              'send_message_to_user']   -> completed on the read's receipt
    16:44:54  [SEND-KIND] 10202_344 action=2 ... expects_reply=True
              "Hello! Welcome to Cafe Flow. Could you please tell me your name"
    16:44:58  [REUSE-WAIT] action 2 holds for the person's reply
    16:45:00  the turn's one voice clip

The person was asked for their name twice, and the tablet heard the first
question 37 s after it showed (the owner: "the audio itself is played late in
android").  When a turn ENDS at its question its voice is on time (Luna, the
same walk: text 16:38:02, tablet play 16:38:06).

THE RULE.  The owner on 476bbd5d8: "when the output is a question to get the
job done, how can that be counted as that step is done?".  The hold of
2da7dbd88 (record the ask, wait, the step's tools owed after the reply) took
a step whose tool is named in its WORDS only; a step whose tool is BANKED in
its recipe completed on that tool's receipt with its question out.  Now any
tool step that asked this run holds on a person's turn.  A turn nobody can
answer keeps the parent's rule.

WHAT IS OWED AFTER THE REPLY (hartos-17's review): order, not a read/write
list.  A tool whose result came back BEFORE the step's ask stands: the read
that found no name is why it asked, and re-reading after the reply finds no
name again, so the 4B would ask again (a name-ask loop with a child).  A
tool that ran AFTER the ask and before the reply is owed again: hartos-52's
drill, an attempt saved before the child tried.  The ask record keeps the
call ids made before the assistant message that asked
(_reuse_calls_before_ask); the reply's seed leaves them out of the ids whose
results do not count.  A record written before this has no such ids: every
named tool stays owed, as before.

Real code: REUSE's _advance_reuse_action with its real gate, readers and
receipt pick (the walk fixture of the words-step test), and the real seed;
the ledger reads, the wait and the commit are recorded, not run.

    python -m pytest tests/unit/test_a_question_from_a_tool_step_is_asked_once.py -q --noconftest
"""
import json

import pytest

from tests.unit.test_a_step_naming_its_tool_in_its_words_is_a_tool_step import (  # noqa: F401
    SESSION, _call, _dispatch, _result, _save, _seed, _verdict, walk)

READ_STEP = ("get_data_by_key: key 'child_name'; if it is missing, ask the "
             "parent for the child's name")
BANKED_READ = [{'steps': 'get_data_by_key({"key": "child_name"})',
                'tool_name': 'get_data_by_key'}]
QUESTION = "What is your child's name?"
NAME = 'Zoey'
ASK_REC = {'request_id': 'ask_req', 'via': 'send', 'question': QUESTION,
           'asked': QUESTION}


def _read(cid='r1'):
    return [_call(cid, 'get_data_by_key', {'key': 'child_name'}),
            _result(cid, "Key not found in stored data. Nothing is saved at child_name")]


def _name_ask(cid='s1', expects_reply=True):
    return [_call(cid, 'send_message_to_user',
                  {'text': QUESTION, 'expects_reply': expects_reply}),
            _result(cid, 'Message sent successfully to user with request_id: ask_req')]


def _reply_seed():
    """The reply turn's seed as the walk fixture records it (its pending
    reply is found by this exact content)."""
    return _seed(2, READ_STEP)


def _turn_one():
    return [_dispatch(2, READ_STEP), *_read('r1'), *_name_ask('s1'), _verdict(2)]


def _asks_sent(messages):
    """The sends that asked the person, by their own declaration."""
    n = 0
    for m in messages:
        for tc in (m or {}).get('tool_calls') or []:
            fn = (tc or {}).get('function') or {}
            if fn.get('name') == 'send_message_to_user':
                if json.loads(fn.get('arguments') or '{}').get('expects_reply') is not False:
                    n += 1
    return n


# -- turn 1: the question is out -------------------------------------------

def test_a_banked_read_that_asked_holds_for_the_reply(walk):
    """Cafe's action 1: its read ran and its question went out on a person's
    turn.  It records the ask and waits; it does not complete on the read, so
    the next step is not dispatched to ask the same thing again.  On the
    parent it completed on the read's receipt."""
    run = walk(_turn_one(), READ_STEP, request_id='ask_req', recipe=BANKED_READ)
    assert run.waits and not run.commits and not run.states, vars(run)
    assert run.asks['open'] and run.asks['open']['via'] == 'send', run.asks
    assert _asks_sent(run.chat.messages) == 1


def test_the_ask_record_keeps_the_read_made_before_the_question(walk):
    """The ids recorded with the ask: the read made before the assistant
    message that asked, never the ask itself, never a call after it."""
    rr = walk(_turn_one(), READ_STEP, request_id='ask_req', recipe=BANKED_READ,
              rounds=0).rr
    msgs = _turn_one()
    ask_idx = next(i for i, m in enumerate(msgs)
                   if m.get('role') == 'tool' and m['tool_responses'][0]['tool_call_id'] == 's1')
    assert rr._reuse_calls_before_ask(msgs, ask_idx) == ['r1']
    # hartos-52's drill: the save came after the ask, so it is not "before".
    drill = [_dispatch(2, READ_STEP), *_name_ask('s1'), *_save('v1', 'ball'), _verdict(2)]
    drill_ask = next(i for i, m in enumerate(drill)
                     if m.get('role') == 'tool' and m['tool_responses'][0]['tool_call_id'] == 's1')
    assert rr._reuse_calls_before_ask(drill, drill_ask) == []


def test_a_tool_called_beside_the_question_is_not_before_it(walk):
    """A read made in the SAME assistant message as the send did not lead to
    the question: it is not counted as before it (owed after the reply)."""
    rr = walk(_turn_one(), READ_STEP, request_id='ask_req', recipe=BANKED_READ,
              rounds=0).rr
    both = {'role': 'assistant', 'name': 'Helper', 'content': None, 'tool_calls': [
        {'id': 'r1', 'type': 'function', 'function': {
            'name': 'get_data_by_key', 'arguments': '{"key": "child_name"}'}},
        {'id': 's1', 'type': 'function', 'function': {
            'name': 'send_message_to_user',
            'arguments': json.dumps({'text': QUESTION, 'expects_reply': True})}}]}
    res = {'role': 'tool', 'name': 'Assistant', 'content': 'ok', 'tool_responses': [
        {'tool_call_id': 'r1', 'role': 'tool', 'content': 'Key not found'},
        {'tool_call_id': 's1', 'role': 'tool', 'content': 'Message sent successfully'}]}
    msgs = [_dispatch(2, READ_STEP), both, res, _verdict(2)]
    assert rr._reuse_calls_before_ask(msgs, 2) == []


# -- turn 2: the reply ------------------------------------------------------

def _seed_calls_before(walk_run, monkeypatch, open_ask, messages):
    """The real seed on the reply turn: what it records as the calls whose
    results do not count."""
    flask = pytest.importorskip('flask')
    rr = walk_run.rr
    chat = walk_run.chat
    chat.messages = list(messages)
    walk_run.asks['open'] = open_ask
    monkeypatch.setattr(rr, 'get_registered_groupchat', lambda _s: chat)
    monkeypatch.setattr(rr, '_reuse_camera_window', lambda *a, **k: '')
    monkeypatch.setattr(rr, '_reuse_voice_window', lambda *a, **k: '')
    monkeypatch.setitem(rr.request_id_list, SESSION, 'reply_req')
    with flask.Flask('seed').app_context():
        rr._reuse_seed_message(SESSION, NAME)
    return (rr._reuse_pending_reply.get(SESSION) or {}).get('calls_before')


def test_the_reply_seed_leaves_the_read_before_the_question_counted(walk, monkeypatch):
    run = walk(_turn_one(), READ_STEP, request_id='ask_req', recipe=BANKED_READ,
               rounds=0)
    got = _seed_calls_before(run, monkeypatch,
                             dict(ASK_REC, calls_before_ask=['r1']), _turn_one())
    assert got == ['s1'], got


def test_an_ask_recorded_before_this_owes_every_tool_as_before(walk, monkeypatch):
    """A record with no calls_before_ask (written by an older build): every
    call made before the reply is left out, as on the parent."""
    run = walk(_turn_one(), READ_STEP, request_id='ask_req', recipe=BANKED_READ,
               rounds=0)
    got = _seed_calls_before(run, monkeypatch, dict(ASK_REC), _turn_one())
    assert got == ['r1', 's1'], got


def test_on_the_reply_the_step_completes_without_reading_again(walk, monkeypatch):
    """Both turns, as the person sees them: one question in all; on the reply
    the step completes on it, and nothing steers the model to read the name
    key again (the re-read would find no name and ask again)."""
    turn_one = walk(_turn_one(), READ_STEP, request_id='ask_req', recipe=BANKED_READ)
    assert turn_one.waits and not turn_one.commits, vars(turn_one)
    before = turn_one.asks['open'] and ['r1']
    calls_before = _seed_calls_before(turn_one, monkeypatch,
                                      dict(ASK_REC, calls_before_ask=before),
                                      _turn_one())
    msgs = [*_turn_one()[:-1], _reply_seed(), _verdict(2)]
    run = walk(msgs, READ_STEP, request_id='reply_req', open_ask=dict(ASK_REC),
               calls_before=calls_before, recipe=BANKED_READ)
    assert len(run.commits) == 1 and not run.waits and not run.states, vars(run)
    assert not any('get_data_by_key' in p for p in run.pending), run.pending
    assert _asks_sent(run.chat.messages) == 1


def test_with_every_call_owed_the_read_is_demanded_again(walk):
    """What order prevents, pinned: if the read before the question counted
    for nothing, the reply turn would demand the read again."""
    msgs = [*_turn_one()[:-1], _reply_seed(), _verdict(2)]
    run = walk(msgs, READ_STEP, request_id='reply_req', open_ask=dict(ASK_REC),
               calls_before=['r1', 's1'], recipe=BANKED_READ)
    assert not run.commits, run.commits
    assert run.pending[0] == ['get_data_by_key'], run.pending


# -- what stays as it was ---------------------------------------------------

def test_a_banked_read_that_asked_nothing_completes_on_its_read(walk):
    run = walk([_dispatch(2, READ_STEP), *_read('r1'), _verdict(2)], READ_STEP,
               request_id='turn_req', recipe=BANKED_READ)
    assert len(run.commits) == 1 and not run.waits, vars(run)
    assert run.commits[0]['kind'] == 'tool_receipt', run.commits


def test_a_send_declared_as_no_question_does_not_hold(walk):
    """expects_reply=false: the step told the person something; it completes
    on its read as before."""
    msgs = [_dispatch(2, READ_STEP), *_read('r1'),
            *_name_ask('s1', expects_reply=False), _verdict(2)]
    run = walk(msgs, READ_STEP, request_id='turn_req', recipe=BANKED_READ)
    assert len(run.commits) == 1 and not run.waits, vars(run)


def test_on_a_turn_nobody_can_answer_it_is_judged_as_before(walk):
    """An empty id (standalone, WhatsApp, a scheduled run that lost its id):
    nobody can answer, so nothing waits; the parent's rule judges it."""
    run = walk(_turn_one(), READ_STEP, request_id='', recipe=BANKED_READ)
    assert not run.waits, vars(run)
    assert len(run.commits) == 1, vars(run)
