"""A step whose banked tool ran and whose question reached the person holds
for their reply: the next step is not started while the question is open, and
the read that led to the question is not owed again after the reply.

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

THE RULE.  7043d635e (31 Oct): a question is never the step done; it was
applied to steps naming no tool but the send.  The hold of 2da7dbd88 (record
the ask, wait, the step's tools owed after the reply) took a step whose tool
is named in its WORDS only; a step whose tool is BANKED in its recipe
completed on that tool's receipt with its question out, and the pipeline
started the next step, which asked again.  Now any tool step that asked this
run holds on a person's turn.  A turn nobody can answer keeps the parent's
rule.  Whether the 4B then asks again on a later step is the model's; what
this pins is that the next step is not started while the question is open.

WHAT IS OWED AFTER THE REPLY (hartos-17's review): order, not a read/write
list.  A tool whose result came back BEFORE the step's ask, in the step's own
dispatch window, stands: the read that found no name is why it asked, and
re-reading after the reply finds no name again, so the 4B would ask again (a
name-ask loop with a child).  A tool that ran AFTER the ask and before the
reply is owed again: hartos-52's drill, an attempt saved before the child
tried.  A tool from an EARLIER step (or an earlier run of this one) is not
this step's work at all: counting it let a drill complete on the child's
reply with no attempt saved (hartos-17's review of 8693955ef, MUST 1).  The
ask record keeps the call ids made in the step's dispatch window before the
assistant message that asked (_reuse_calls_before_ask); the reply's seed
leaves them out of the ids whose results do not count.  A record written
before this has no such ids: every named tool stays owed, as before.

Real code: REUSE's _advance_reuse_action with its real gate, readers and
receipt pick (the walk fixture of the words-step test, whose ask stub keeps
every field the gate records), and the real seed; the ledger reads, the wait,
the commit and the next step's start are recorded, not run.

    python -m pytest tests/unit/test_a_tool_step_that_asked_holds_for_the_reply.py -q --noconftest
"""
import json

import pytest

from tests.unit.test_a_step_naming_its_tool_in_its_words_is_a_tool_step import (  # noqa: F401
    DRILL_STEP, REPLY, SESSION, _ask, _call, _dispatch, _result, _save, _seed,
    _verdict, walk)

READ_STEP = ("get_data_by_key: key 'child_name'; if it is missing, ask the "
             "parent for the child's name")
BANKED_READ = [{'steps': 'get_data_by_key({"key": "child_name"})',
                'tool_name': 'get_data_by_key'}]
QUESTION = "What is your child's name?"
NAME = 'Zoey'


def _read(cid='r1'):
    return [_call(cid, 'get_data_by_key', {'key': 'child_name'}),
            _result(cid, "Key not found in stored data. Nothing is saved at child_name")]


def _name_ask(cid='s1', expects_reply=True):
    return [_call(cid, 'send_message_to_user',
                  {'text': QUESTION, 'expects_reply': expects_reply}),
            _result(cid, 'Message sent successfully to user with request_id: ask_req')]


def _name_saved(cid='v0'):
    """Action 1 of the drill walk: the child's name saved, an earlier step."""
    return [_call(cid, 'save_data_in_memory', {'key': 'child_name', 'value': NAME}),
            _result(cid, f'Saved at child_name: "{NAME}"')]


def _reply_seed(step=READ_STEP):
    """The reply turn's seed as the walk fixture records it (its pending
    reply is found by this exact content)."""
    return _seed(2, step)


def _turn_one():
    return [_dispatch(2, READ_STEP), *_read('r1'), *_name_ask('s1'), _verdict(2)]


def _drill_turn_one():
    """An earlier step saved the name; then the drill asked 'say ball'."""
    return [_dispatch(1, "send_message_to_user: get the child's name"),
            *_name_saved('v0'), _verdict(1),
            _dispatch(2, DRILL_STEP), *_ask('s1'), _verdict(2)]


def _ask_index(msgs, cid='s1'):
    return next(i for i, m in enumerate(msgs)
                if m.get('role') == 'tool' and m['tool_responses'][0]['tool_call_id'] == cid)


def _dispatch_index(msgs, aid=2):
    marker = f'Action #{aid}:'
    return max(i for i, m in enumerate(msgs)
               if m.get('role') == 'user' and marker in str(m.get('content')))


# -- turn 1: the question is out -------------------------------------------

def test_a_banked_read_that_asked_holds_for_the_reply(walk):
    """Cafe's action 1: its read ran and its question went out on a person's
    turn.  It records the ask and waits; it does not complete on the read,
    and the next step is not started (on the parent it completed on the
    read's receipt and the pipeline started action 2, which asked again)."""
    run = walk(_turn_one(), READ_STEP, request_id='ask_req', recipe=BANKED_READ)
    assert run.waits and not run.commits and not run.states, vars(run)
    assert run.asks['open'] and run.asks['open']['via'] == 'send', run.asks
    assert run.starts == [], run.starts


def test_the_gate_records_the_read_made_before_the_question(walk):
    """Through the real gate (_reuse_record_ask_in_window): the ask record
    carries the read made before the assistant message that asked, and
    nothing else.  A gate that dropped the field would leave it out."""
    run = walk(_turn_one(), READ_STEP, request_id='ask_req', recipe=BANKED_READ)
    assert run.asks['open'].get('calls_before_ask') == ['r1'], run.asks


def test_the_ask_record_keeps_only_calls_before_the_question(walk):
    """The ids recorded with the ask: calls in the step's window before the
    assistant message that asked, never the ask itself, never a call after
    it (hartos-52's drill: the save came after the ask)."""
    rr = walk(_turn_one(), READ_STEP, request_id='ask_req', recipe=BANKED_READ,
              rounds=0).rr
    msgs = _turn_one()
    assert rr._reuse_calls_before_ask(msgs, 0, _ask_index(msgs)) == ['r1']
    drill = [_dispatch(2, READ_STEP), *_name_ask('s1'), *_save('v1', 'ball'), _verdict(2)]
    assert rr._reuse_calls_before_ask(drill, 0, _ask_index(drill)) == []


def test_a_call_from_an_earlier_step_is_not_before_the_question(walk):
    """hartos-17's MUST 1: action 1's save (v0) came before the drill's
    dispatch, so it is not one of the drill's calls before its question."""
    run = walk(_drill_turn_one(), DRILL_STEP, request_id='ask_req')
    assert run.waits and not run.commits, vars(run)
    assert run.asks['open'].get('calls_before_ask') == [], run.asks
    msgs = _drill_turn_one()
    assert run.rr._reuse_calls_before_ask(
        msgs, _dispatch_index(msgs), _ask_index(msgs)) == []


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
    assert rr._reuse_calls_before_ask(msgs, 0, 2) == []


# -- turn 2: the reply ------------------------------------------------------

def _seed_calls_before(walk_run, monkeypatch, open_ask, messages, words=NAME):
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
        rr._reuse_seed_message(SESSION, words)
    return (rr._reuse_pending_reply.get(SESSION) or {}).get('calls_before')


def test_the_reply_seed_leaves_the_read_before_the_question_counted(walk, monkeypatch):
    """The seed reads the record the gate wrote on turn one (not a
    hard-coded list): the read stands, the send's own call does not count."""
    turn_one = walk(_turn_one(), READ_STEP, request_id='ask_req', recipe=BANKED_READ)
    got = _seed_calls_before(turn_one, monkeypatch, dict(turn_one.asks['open']),
                             _turn_one())
    assert got == ['s1'], got


def test_an_ask_recorded_before_this_owes_every_tool_as_before(walk, monkeypatch):
    """A record with no calls_before_ask (written by an older build): every
    call made before the reply is left out, as on the parent."""
    turn_one = walk(_turn_one(), READ_STEP, request_id='ask_req', recipe=BANKED_READ)
    old = {k: v for k, v in turn_one.asks['open'].items() if k != 'calls_before_ask'}
    got = _seed_calls_before(turn_one, monkeypatch, old, _turn_one())
    assert got == ['r1', 's1'], got


def test_on_the_reply_the_step_completes_without_reading_again(walk, monkeypatch):
    """Both turns through the real gate and seed: on turn one the step holds;
    on the reply it completes on the reply, and nothing steers the model to
    read the name key again (the re-read would find no name and ask again)."""
    turn_one = walk(_turn_one(), READ_STEP, request_id='ask_req', recipe=BANKED_READ)
    assert turn_one.waits and not turn_one.commits, vars(turn_one)
    open_ask = dict(turn_one.asks['open'])
    calls_before = _seed_calls_before(turn_one, monkeypatch, open_ask, _turn_one())
    msgs = [*_turn_one()[:-1], _reply_seed(), _verdict(2)]
    run = walk(msgs, READ_STEP, request_id='reply_req', open_ask=open_ask,
               calls_before=calls_before, recipe=BANKED_READ)
    assert len(run.commits) == 1 and not run.waits and not run.states, vars(run)
    assert not any('get_data_by_key' in p for p in run.pending), run.pending


def test_on_the_reply_a_drill_still_owes_its_own_save(walk, monkeypatch):
    """hartos-17's MUST 1, both turns: an earlier step's save of the name
    does not stand in for the drill's save of the child's attempt.  On
    8693955ef the record held ['v0'], v0 counted as run, and the drill
    committed on the child's reply with no attempt saved."""
    turn_one = walk(_drill_turn_one(), DRILL_STEP, request_id='ask_req')
    assert turn_one.waits and not turn_one.commits, vars(turn_one)
    open_ask = dict(turn_one.asks['open'])
    calls_before = _seed_calls_before(turn_one, monkeypatch, open_ask,
                                      _drill_turn_one(), words=REPLY)
    assert 'v0' in calls_before, calls_before
    msgs = [*_drill_turn_one()[:-1], _reply_seed(DRILL_STEP), _verdict(2)]
    run = walk(msgs, DRILL_STEP, request_id='reply_req', open_ask=open_ask,
               calls_before=calls_before)
    assert not run.commits, run.commits
    assert any('save_data_in_memory' in p for p in run.pending), run.pending


def test_with_every_call_owed_the_read_is_demanded_again(walk):
    """What order prevents, pinned: if the read before the question counted
    for nothing, the reply turn would demand the read again."""
    msgs = [*_turn_one()[:-1], _reply_seed(), _verdict(2)]
    run = walk(msgs, READ_STEP, request_id='reply_req',
               open_ask={'request_id': 'ask_req', 'via': 'send', 'question': QUESTION},
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


# -- an empty recipe: the read is named in the step's words only -----------
#
# rv_8693's probe (hartos-17, 2026-11-24): READ_STEP with recipe: [] (the
# 11-21 speech agent banked every step of flow 0 that way), read then a
# separate asking send, a person's turn.  On 1f5c0e2c6 and before:
# waits=[], three "<no output produced>" steers, GAVE_UP.  The step's words
# name a read, which never made it a tool step (rv_2da7 F2: a read owes the
# sentence it answers from), and its asking send was no output.  A step that
# asked to get its job done holds for the reply, whichever tool its words name.

NO_RECIPE = ()


def test_an_empty_recipe_read_step_that_asked_holds_for_the_reply(walk):
    run = walk(_turn_one(), READ_STEP, request_id='ask_req', recipe=NO_RECIPE)
    assert run.waits and not run.commits and not run.states, vars(run)
    assert not any(run.rr._REUSE_NO_OUTPUT_SENTINEL in p for p in run.pending), run.pending
    assert run.asks['open'].get('calls_before_ask') == ['r1'], run.asks
    assert run.starts == [], run.starts


def test_on_the_reply_the_empty_recipe_read_step_completes(walk, monkeypatch):
    turn_one = walk(_turn_one(), READ_STEP, request_id='ask_req', recipe=NO_RECIPE)
    assert turn_one.waits and not turn_one.commits, vars(turn_one)
    open_ask = dict(turn_one.asks['open'])
    calls_before = _seed_calls_before(turn_one, monkeypatch, open_ask, _turn_one())
    msgs = [*_turn_one()[:-1], _reply_seed(), _verdict(2)]
    run = walk(msgs, READ_STEP, request_id='reply_req', open_ask=open_ask,
               calls_before=calls_before, recipe=NO_RECIPE)
    assert len(run.commits) == 1 and not run.waits and not run.states, vars(run)
    assert not any('get_data_by_key' in p for p in run.pending), run.pending


def test_the_real_not_found_answer_is_work_not_a_failure():
    """hartos-17 (b): the read that found nothing stands as work only while
    get_data_by_key's real miss is not a failed call by the one rule.  If the
    miss ever goes out as a refusal, the read before the question is owed
    again and the step asks again: this pins it."""
    from core.agent_tools import KEY_NOT_FOUND, key_not_found_reply
    from core.constants import tool_reply_failed
    reply = key_not_found_reply({}, 'child_name')
    assert reply.startswith(KEY_NOT_FOUND), reply
    assert tool_reply_failed(reply) is False


def _asked_before_reading():
    """The model asked WITHOUT reading first (hartos-17 (c))."""
    return [_dispatch(2, READ_STEP), *_name_ask('s1'), _verdict(2)]


def test_an_ask_before_any_read_holds_and_owes_the_read(walk, monkeypatch):
    turn_one = walk(_asked_before_reading(), READ_STEP, request_id='ask_req',
                    recipe=NO_RECIPE)
    assert turn_one.waits and not turn_one.commits, vars(turn_one)
    assert turn_one.asks['open'].get('calls_before_ask') == [], turn_one.asks
    open_ask = dict(turn_one.asks['open'])
    calls_before = _seed_calls_before(turn_one, monkeypatch, open_ask,
                                      _asked_before_reading())
    # On the reply the read is owed; it runs, finds nothing, and the step
    # completes on the reply.
    msgs = [*_asked_before_reading()[:-1], _reply_seed(), *_read('r2'), _verdict(2)]
    run = walk(msgs, READ_STEP, request_id='reply_req', open_ask=open_ask,
               calls_before=calls_before, recipe=NO_RECIPE)
    assert len(run.commits) == 1 and not run.waits, vars(run)


def test_a_re_ask_after_the_owed_read_completes_on_the_reply_in_hand(walk, monkeypatch):
    """hartos-17 (c), pinned as it stands: after the reply the owed read finds
    nothing and the model asks again in the same turn.  The person's answer
    is already in hand and the step's named work ran after it, so the step
    completes (on the read's receipt) instead of demanding the same answer
    twice; the gate's turn-one hold is not re-applied on a reply turn.  The
    re-sent question has already reached the person either way: it is the
    model's, and the next step reads the answer they give it."""
    turn_one = walk(_asked_before_reading(), READ_STEP, request_id='ask_req',
                    recipe=NO_RECIPE)
    open_ask = dict(turn_one.asks['open'])
    calls_before = _seed_calls_before(turn_one, monkeypatch, open_ask,
                                      _asked_before_reading())
    msgs = [*_asked_before_reading()[:-1], _reply_seed(), *_read('r2'),
            *_name_ask('s2'), _verdict(2)]
    run = walk(msgs, READ_STEP, request_id='reply_req', open_ask=open_ask,
               calls_before=calls_before, recipe=NO_RECIPE)
    assert len(run.commits) == 1 and not run.waits and not run.states, vars(run)


def test_an_empty_recipe_read_step_that_asked_nothing_is_judged_as_before(walk):
    """The counter-case: a read and a statement (expects_reply false) never
    hold; the step is under the text rule as on the parent, and its sent
    statement is its output."""
    msgs = [_dispatch(2, READ_STEP), *_read('r1'),
            *_name_ask('s1', expects_reply=False), _verdict(2)]
    run = walk(msgs, READ_STEP, request_id='turn_req', recipe=NO_RECIPE)
    assert not run.waits, vars(run)
    assert len(run.commits) == 1, vars(run)


def test_an_empty_recipe_read_step_on_a_turn_nobody_can_answer_is_judged_as_before(walk):
    run = walk(_turn_one(), READ_STEP, request_id='', recipe=NO_RECIPE)
    assert not run.waits, vars(run)
