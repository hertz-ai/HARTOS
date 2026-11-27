"""A step banked 'no' that only answers her reply with a statement completes
on the reply; only a new QUESTION after the reply holds it (#295 F7, forward).

rv_f6's reading of HARTOS f914a0f61 (F7): M2 holds a step that asked again
after the person's reply, by the S2b rule (_reuse_hold_substeps_for_the_
reply), and that rule reads a step banked 'no' as asking with ANY send.  So
a 'no' step whose model answers her reply with a statement ("Lovely,
Zoey!", expects_reply false) was held on that statement instead of
completing on the reply, as it did before F7: then silence, and her next
words were recorded as the answer to the statement.  Flow 0 of the speech
agent 95568353503 banks steps 4, 5 and 12 'no' (step 5: greet the child by
name once).  F7's own tests banked every step 'yes'.

The completion receipt's hold now counts only a send that ASKS (send_is_ask)
and records that same send as the open ask, never a later statement; the
sub-step holds (SUBTASK-HOLD, BREAKDOWN) read a 'no' step's sends as before.

The walk of tests/unit/test_a_step_that_asks_again_after_the_reply_waits.py
(the real chat_agent, ledger and group of test_reuse_reply_is_evidence.py,
model seats scripted with the walk's own texts).

    python -m pytest tests/unit/test_a_no_step_completes_on_a_statement_after_the_reply.py -q
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.unit.test_a_step_that_asks_again_after_the_reply_waits import (  # noqa: E402,F401
    FLOW, MARK, Q0, Q1, Q3, REPLY, _answers, _held_on, _task, _verdicts, speech)

#: The same flow with step 1 banked 'no', as the live flow banks step 5
#: (a leading 'no' with its reason, as CREATE writes it).
FLOW_NO = ([dict(FLOW[0], can_perform_without_user_input='no - the person answers this step')]
           + FLOW[1:])
PRAISE = 'Lovely, Zoey! ஜோயி!'
TAKE_YOUR_TIME = 'Take your time, Zoey.'


def _calls(state, *texts_and_asks):
    n = len(state['sends'])
    return {'content': '', 'tool_calls': [{
        'id': 'send_%d' % (n + i), 'type': 'function',
        'function': {'name': 'send_message_to_user',
                     'arguments': json.dumps({'text': text, 'expects_reply': asks})}}
        for i, (text, asks) in enumerate(texts_and_asks)]}


def _the_4b(after_the_reply):
    """Step 1 calls her name on the ask turn; on the turn that carries her
    reply it sends ``after_the_reply`` (pairs of text and expects_reply) in
    one message; step 3 asks her to say it back.  Once per step and turn."""
    def script(state, aid, content):
        done = state.setdefault('asked', set())
        key = (state['turn'], aid)
        if key in done:
            return 'Waiting for her.' if aid in (1, 3) else None
        if aid == 1 and state['turn'] == 'ask':
            done.add(key)
            return _calls(state, (Q0, True))
        if aid == 1 and state['turn'] == 'reply':
            done.add(key)
            return _calls(state, *after_the_reply)
        if aid == 3:
            done.add(key)
            return _calls(state, (Q3, True))
        return None
    return script


def _walk(speech, after_the_reply, actions=FLOW_NO):
    run = speech(actions=actions, assistant_script=_the_4b(after_the_reply),
                 verdict_script=_verdicts())
    run.turn('hi, I am ready', 'request-1')
    assert run.state['sends'] == [Q0], run.state['sends']
    run.turn(REPLY, 'request-2', kind='reply')
    return run


def test_a_no_step_that_only_praises_her_reply_completes_on_it(speech):
    from agent_ledger.core import TaskStatus
    run = _walk(speech, [(PRAISE, False)])
    assert _task(run, 1).status == TaskStatus.COMPLETED, 'held on a statement'
    assert _answers(run, 1) == [REPLY]
    assert 2 in run.state['dispatched'], run.state['dispatched']
    assert run.state['sends'] == [Q0, PRAISE, Q3], run.state['sends']


def test_a_yes_step_that_only_praises_her_reply_completes_on_it(speech):
    """The same statement on a step banked 'yes' completed before this fix
    too: the rule reads both kinds of step the same way now."""
    from agent_ledger.core import TaskStatus
    run = _walk(speech, [(PRAISE, False)], actions=FLOW)
    assert _task(run, 1).status == TaskStatus.COMPLETED, 'held on a statement'
    assert _answers(run, 1) == [REPLY]
    assert run.state['sends'] == [Q0, PRAISE, Q3], run.state['sends']


def test_a_no_step_that_asks_a_new_question_after_her_reply_still_holds(speech):
    run = _walk(speech, [(Q1, True)])
    assert run.state['sends'] == [Q0, Q1], run.state['sends']
    assert _answers(run, 1) == [REPLY]
    _held_on(run, 1, Q1, 'request-2')


def test_the_hold_records_the_question_not_the_statement_after_it(speech):
    """A new question, then a statement in the same message: the step holds
    on the question, the one she is answering."""
    run = _walk(speech, [(Q1, True), (TAKE_YOUR_TIME, False)])
    assert run.state['sends'] == [Q0, Q1, TAKE_YOUR_TIME], run.state['sends']
    _held_on(run, 1, Q1, 'request-2')
