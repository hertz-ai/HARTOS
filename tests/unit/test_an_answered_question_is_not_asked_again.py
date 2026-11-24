"""A question the person already answered is not sent to them again, word for
word, on their own later turn.

WHAT WENT WRONG.  The 11-23 walk: the speech child heard the name question
twice.  rv_382c's review of 382cb5dfd (2026-11-24): on the reply turn the 4B
read the name key, found nothing (the step never saves the answer) and sent
"What is your child's name?" again.  The step completed on the reply in hand
and the next step started, so the person saw the question a second time and
their second answer landed on whatever step was current: a drill would score
"Zoey".

WHY NOT HOLD THE STEP (traced, agreed with hartos-17): on the reply turn the
answered ask is still the open one, so record_ask_delivered records nothing
new; holding would leave this turn's read out of what stands on the next
reply, the read would be owed again, find nothing again, and the model would
ask again: the name-ask loop.  The owner, 11-19: a question the person
already answered is not asked again.

THE RULE (hartos-17, narrow): on a person's turn after the question went out,
a send with expects_reply=True whose question part equals the open ask's
question WORD FOR WORD (crossbar_publish.whole_words, the reader that says
each message of a turn once) is refused with core.tool_refusal's envelope.
The same question in other words still goes out: a follow-up that clarifies,
or one after a reply that missed (#202 M2's 'Aaa!').  Whether the reply
answered is the verifier's judgement, not this one's.  The asking turn, a
goal or timer turn, and a statement are not refused.

Real code: the send tool's closure (build_core_tool_closures, a recording
sender), lifecycle_hooks.answered_question_asked_again over a ledger task's
ask record, and REUSE's gate through the walk fixture.

    python -m pytest tests/unit/test_an_answered_question_is_not_asked_again.py -q --noconftest
"""
from types import SimpleNamespace

import pytest

from core.constants import tool_reply_failed
from core.tool_refusal import refusal_has_hint
from tests.unit.test_a_tool_refusal_says_what_was_wrong import (  # noqa: F401
    _send_tools, tool_log)
from tests.unit.test_a_tool_step_that_asked_holds_for_the_reply import (  # noqa: F401
    NO_RECIPE, READ_STEP, _asked_before_reading, _read, _reply_seed,
    _seed_calls_before, walk)
from tests.unit.test_a_step_naming_its_tool_in_its_words_is_a_tool_step import (  # noqa: F401
    _call, _result, _verdict)

SESSION = '10202_54'            # the send closures' user_prompt
ACTION = 2
QUESTION = "What is your child's name?"
SENT_IN_FULL = f"Hello! Welcome. {QUESTION}"


@pytest.fixture
def asked(monkeypatch):
    """Action 2's question went out in request 'ask_req' (its open ask
    record), the action is in progress, and the turn id is settable."""
    import core.llm_outbound_logger as lol
    import hartos.lifecycle_hooks as lh
    ask = {'action_id': ACTION, 'flow': 0, 'request_id': 'ask_req',
           'message_index': 3, 'via': 'send', 'question': SENT_IN_FULL,
           'asked': lh.asked_part(SENT_IN_FULL), 'answered': False}
    task = SimpleNamespace(context={lh.ASK_DELIVERED_KEY: [ask]})
    monkeypatch.setitem(lh._ledger_registry, SESSION,
                        SimpleNamespace(tasks={f'action_{ACTION}': task}))
    monkeypatch.setitem(lh.action_states, SESSION,
                        {ACTION: lh.ActionState.IN_PROGRESS})
    tokens = []

    def turn(request_id):
        tokens.append(lol._request_id_var.set(request_id))
    yield SimpleNamespace(ask=ask, turn=turn)
    for t in reversed(tokens):
        lol._request_id_var.reset(t)


def _send(text, expects_reply=True):
    tools, sent, _scheduled = _send_tools()
    return tools['send_message_to_user'](text, expects_reply), sent


# -- refused: the same words, on the person's later turn ---------------------

@pytest.mark.parametrize('text', [
    QUESTION,
    # the question part is what is compared: a new greeting before it
    f"Thanks! {QUESTION}",
    # case and the marks around words aside
    "what is your child's name",
])
def test_the_answered_question_sent_again_is_refused(tool_log, asked, caplog, text):
    asked.turn('reply_req')
    with caplog.at_level('WARNING', logger='tool_execution'):
        reply, sent = _send(text)
    assert sent == [], 'the person must not get the question a second time'
    assert tool_reply_failed(reply) and refusal_has_hint(reply), reply
    assert QUESTION in reply and 'different words' in reply, reply
    # Counted in the walk: one line per refused repeat, with both requests.
    assert any('[SEND-REPEAT]' in r.getMessage() and 'ask_req' in r.getMessage()
               for r in caplog.records), caplog.text


# -- sent: everything else ---------------------------------------------------

def test_the_question_in_other_words_still_goes_out(tool_log, asked):
    """A clarifying follow-up, or a re-ask after a reply that missed: the
    verifier judges the reply, this does not."""
    asked.turn('reply_req')
    text = "Sorry, I didn't catch that. Could you tell me your child's name again?"
    reply, sent = _send(text)
    assert sent == [text] and not tool_reply_failed(reply), reply


def test_on_the_turn_that_asked_nothing_is_refused(tool_log, asked):
    asked.turn('ask_req')
    reply, sent = _send(QUESTION)
    assert sent == [QUESTION] and not tool_reply_failed(reply), reply


def test_a_goal_or_timer_turn_may_ask_again(tool_log, asked):
    """Nobody spoke on a background turn: a re-engagement after silence
    (the owner's 11-24 waits-with-back-off rule) is not refused."""
    asked.turn('daemon_goal1')
    reply, sent = _send(QUESTION)
    assert sent == [QUESTION] and not tool_reply_failed(reply), reply


def test_a_statement_is_not_refused(tool_log, asked):
    asked.turn('reply_req')
    reply, sent = _send(QUESTION, expects_reply=False)
    assert sent == [QUESTION] and not tool_reply_failed(reply), reply


def test_once_the_answer_is_recorded_nothing_is_refused(tool_log, asked):
    asked.ask['answered'] = True
    asked.turn('reply_req')
    reply, sent = _send(QUESTION)
    assert sent == [QUESTION] and not tool_reply_failed(reply), reply


def test_another_steps_question_is_not_this_one(tool_log, asked, monkeypatch):
    import hartos.lifecycle_hooks as lh
    monkeypatch.setitem(lh.action_states, SESSION,
                        {ACTION + 1: lh.ActionState.IN_PROGRESS})
    asked.turn('reply_req')
    reply, sent = _send(QUESTION)
    assert sent == [QUESTION] and not tool_reply_failed(reply), reply


# -- the step: completes on the reply, and nothing next is asked twice -------

def test_the_step_completes_on_the_reply_when_the_repeat_is_refused(walk, monkeypatch):
    """rv_382c's shape through the real gate: on the reply turn the owed
    read ran, the repeated question was refused (so it never reached the
    person), and the verifier judged the step done.  It completes on the
    reply in hand; it does not hold, and it does not demand the send."""
    from core.tool_refusal import refuse
    turn_one = walk(_asked_before_reading(), READ_STEP, request_id='ask_req',
                    recipe=NO_RECIPE)
    open_ask = dict(turn_one.asks['open'])
    calls_before = _seed_calls_before(turn_one, monkeypatch, open_ask,
                                      _asked_before_reading())
    refused = refuse('send_message_to_user', 'asked already', 'use the reply')
    msgs = [*_asked_before_reading()[:-1], _reply_seed(), *_read('r2'),
            _call('s2', 'send_message_to_user', {'text': QUESTION, 'expects_reply': True}),
            _result('s2', refused), _verdict(2)]
    run = walk(msgs, READ_STEP, request_id='reply_req', open_ask=open_ask,
               calls_before=calls_before, recipe=NO_RECIPE)
    assert len(run.commits) == 1 and not run.waits and not run.states, vars(run)
    assert not any('send_message_to_user' in p for p in run.pending), run.pending
