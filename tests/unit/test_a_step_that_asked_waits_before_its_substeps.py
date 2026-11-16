"""A step that has asked the person a question waits for the answer, even
when the verifier has split that step into sub-steps of its own.

MEASURED on the owner's desktop, 2026-11-16, Nunba build 75 on the local 4B,
the Tamil speech agent (1794540972213), a test account new to it.  ONE turn
(msg_1794824159377_sim, 15:46:32-15:51:38 IST) sent send_message_to_user with
expects_reply=True eight times ("What is your child's name?" twice, then
"Thank you for sharing your child's name...", ...), with [SUBTASK-HOLD]
steers between them ("Once the child's name is received, respond in
Tamil...") and no hold.  The person never got to answer; the next turns
saved the child's sound "Aaa!" as her name.

Cause, as the review of 8ddff06f7 measured on this harness (rv_hartos_2575_
8ddf, probe on the real chat_agent: 4 sends and 3 steers on parent and commit
alike): ledger.add_subtasks leaves the parent BLOCKED on its own sub-steps,
and lifecycle_hooks.block_for_user_input takes the canonical wait only from
IN_PROGRESS, so _reuse_note_ask_and_wait recorded the ask, found the wait
"already held or refused", and the hold steered the next sub-step.  The fix
lets a parent blocked on its own pending sub-steps take the wait (its block
re-marked input_required, its sub-steps kept); any other block is still
refused.  The next person turn then resumes it through the one held-step gate
and the answer is recorded.

Driven through the real chat_agent, state machine, ledger and autogen group
chat of tests/unit/test_reuse_reply_is_evidence.py; only the model seats are
scripted, and nothing the fix depends on is stubbed.

    python -m pytest tests/unit/test_a_step_that_asked_waits_before_its_substeps.py -q
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from tests.unit.test_reuse_reply_is_evidence import ASK_LINE, speech  # noqa: E402,F401

#: The sub-steps the live verifier split the name step into (11-16).
SUBS = [
    {'subtask_id': '1.1', 'description': "Ask the parent for the child's name"},
    {'subtask_id': '1.2', 'description': "Once the child's name is received, respond in Tamil"},
    {'subtask_id': '1.3', 'description': 'Thank the parent for sharing the name'},
]


def _asks_every_time(state, aid, content):
    """The live 4B: on the ask turn it sends its question each time it is
    steered."""
    if aid == 1 and state['turn'] == 'ask':
        return {'content': '', 'tool_calls': [{
            'id': 'ask_%d' % len(state['sends']), 'type': 'function',
            'function': {'name': 'send_message_to_user',
                         'arguments': json.dumps({'text': ASK_LINE, 'expects_reply': True})}}]}
    return None


def _splits_once(state, aid, content):
    if aid == 1 and not state.get('broke'):
        state['broke'] = True
        return json.dumps({'status': 'requires_breakdown', 'action_id': 1,
                           'subtasks': SUBS, 'message': 'split'})
    return json.dumps({'status': 'completed', 'action_id': aid,
                       'can_perform_without_user_input': 'no'})


def _task(run, task_id):
    return run.ledger.tasks[task_id]


def _substep_steers(run, since=0):
    return [s for s in run.state['steers'][since:]
            if s.startswith(run.rr._REUSE_SUBTASK_STEER_PREFIX)]


def _asked(speech):
    run = speech(assistant_script=_asks_every_time, verdict_script=_splits_once)
    run.turn('hi, I am ready', 'request-1')
    return run


def test_the_ask_turn_asks_once_and_holds_with_its_substeps_kept(speech):
    from agent_ledger.core import TaskStatus
    run = _asked(speech)
    assert run.state['sends'] == [ASK_LINE], run.state['sends']
    later = [s for s in _substep_steers(run)
             if SUBS[1]['description'] in s or SUBS[2]['description'] in s]
    assert later == [], 'a sub-step that needs the answer was steered before it came'
    assert run.rr.user_tasks[run.key].current_action == 1
    parent = _task(run, 'action_1')
    assert parent.status == TaskStatus.BLOCKED
    assert parent.blocked_reason == 'input_required'
    assert run.L.ledger_open_ask(run.key, 1, 0) is not None
    assert run.L.person_waits_on(run.key, 1, 0) is True
    waiting = {t.task_id: t.status for t in run.ledger.tasks.values()
               if getattr(t, 'parent_task_id', None) == 'action_1'}
    assert any(s != TaskStatus.COMPLETED for s in waiting.values()), waiting


def test_the_reply_turn_takes_the_answer_and_never_asks_again(speech):
    run = _asked(speech)
    before = len(run.state['steers'])
    run.turn('ba', 'request-2', kind='reply')
    assert run.L.ledger_user_answered(run.key, 1, 0) is True
    assert run.state['sends'] == [ASK_LINE], 'the person must not hear the question again'
    assert run.L.ledger_open_ask(run.key, 1, 0) is None
    # The work that waited for the answer runs on the turn that brought it.
    assert any(SUBS[1]['description'] in s for s in _substep_steers(run, before)) \
        or run.rr.user_tasks[run.key].current_action > 1


def test_a_step_split_on_the_reply_turn_works_its_substeps_with_the_answer(speech):
    """The verifier may split the step only on the turn that brings the
    answer.  That turn's split is not a new wait: the question it answers
    is still in the window, but the person has replied, so the first
    sub-step is worked now and nobody is asked again."""
    def _splits_on_the_reply(state, aid, content):
        if aid == 1 and state['turn'] == 'reply' and not state.get('broke'):
            state['broke'] = True
            return json.dumps({'status': 'requires_breakdown', 'action_id': 1,
                               'subtasks': SUBS, 'message': 'split'})
        return None

    run = speech(verdict_script=_splits_on_the_reply)
    run.turn('hi, I am ready', 'request-1')
    assert run.state['sends'] == [ASK_LINE]
    assert run.L.person_waits_on(run.key, 1, 0) is True
    before = len(run.state['steers'])
    run.turn('ba', 'request-2', kind='reply')
    assert run.state.get('broke'), 'the verifier never split the step'
    assert run.L.ledger_user_answered(run.key, 1, 0) is True
    assert run.state['sends'] == [ASK_LINE], 'the person must not hear the question again'
    assert run.L.person_waits_on(run.key, 1, 0) is False, 'the answer came; nothing waits'
    assert _substep_steers(run, before) or run.rr.user_tasks[run.key].current_action > 1


def test_a_parent_blocked_for_any_other_reason_is_not_taken():
    """The new edge is the parent blocked on its OWN pending sub-steps only:
    a task blocked by anything else keeps the refusal it always had."""
    from agent_ledger.core import TaskStatus
    from hartos import lifecycle_hooks as L

    class _Task:
        def __init__(self, status, reason, task_id='action_1', parent=None):
            self.task_id, self.status, self.blocked_reason = task_id, status, reason
            self.parent_task_id = parent

    class _Ledger:
        def __init__(self, tasks):
            self.tasks = {t.task_id: t for t in tasks}

        def save(self):
            return True

    key = 'blocked_other_reason_test'

    def _refused(*tasks):
        L._ledger_registry[key] = _Ledger(list(tasks))
        try:
            return L.block_for_user_input(key, 1, 'waiting') is False
        finally:
            L._ledger_registry.pop(key, None)

    pending_own = _Task(TaskStatus.PENDING, None, 'action_1_1', parent='action_1')
    for reason in ('approval_required', 'dependency', 'input_required'):
        parent = _Task(TaskStatus.BLOCKED, reason)
        assert _refused(parent), reason
        assert parent.blocked_reason == reason
        # Blocked for its own reason, even with a sub-step of its own still
        # to run: the reason wins, the block is not re-marked.
        parent = _Task(TaskStatus.BLOCKED, reason)
        assert _refused(parent, pending_own), reason
        assert parent.blocked_reason == reason
    # Blocked with no reason and no sub-steps of its own: still refused.
    assert _refused(_Task(TaskStatus.BLOCKED, None))
    # Its sub-steps all done (the ledger unblocks such a parent itself).
    assert _refused(_Task(TaskStatus.BLOCKED, None),
                    _Task(TaskStatus.COMPLETED, None, 'action_1_1', parent='action_1'),
                    _Task(TaskStatus.SKIPPED, None, 'action_1_2', parent='action_1'))
    # The pending sub-step belongs to another step.
    assert _refused(_Task(TaskStatus.BLOCKED, None),
                    _Task(TaskStatus.PENDING, None, 'action_2_1', parent='action_2'))


def test_a_step_that_does_not_wait_keeps_every_reprompt(speech):
    """Owner, 2026-11-16: "to a kid asking repeatedly is a feature to a parent
    it's spam", and the framework supports both, chosen by the flow action.  A
    step whose action does not wait for a reply (can_perform_without_user_input
    'yes': call the child again, as many times as the step says) is never held
    by the wait above, under the same split.  Its re-prompts are calls, not
    questions (send_message_to_user with expects_reply False): a send that
    asks the person makes any step wait ("a question is never the step done",
    _reuse_ask_only_step), which is the other mechanism.  A preservation
    check: it holds on the parent code too; it fails if the wait ever takes a
    step that only calls."""
    def _calls_every_time(state, aid, content):
        if aid == 1 and state['turn'] == 'ask':
            return {'content': '', 'tool_calls': [{
                'id': 'call_%d' % len(state['sends']), 'type': 'function',
                'function': {'name': 'send_message_to_user',
                             'arguments': json.dumps({'text': 'Arjun!', 'expects_reply': False})}}]}
        return None

    def _splits_once_needs_nobody(state, aid, content):
        # The same split, judged the way a step that does not wait is: its
        # verdict never says it needs the person.
        if aid == 1 and not state.get('broke'):
            state['broke'] = True
            return json.dumps({'status': 'requires_breakdown', 'action_id': 1,
                               'subtasks': SUBS, 'message': 'split'})
        return json.dumps({'status': 'completed', 'action_id': aid})

    run = speech(actions=[
        {'action': ("send_message_to_user: call the child by name, then call again "
                    "until she looks up"), 'action_id': 1,
         'can_perform_without_user_input': 'yes',
         'recipe': [{'steps': 'call the child', 'tool_name': 'send_message_to_user'}]},
        {'action': 'say goodbye to Arjun', 'action_id': 2,
         'can_perform_without_user_input': 'yes', 'recipe': []},
    ], assistant_script=_calls_every_time, verdict_script=_splits_once_needs_nobody)
    run.turn('hi, I am ready', 'request-1')
    assert run.state['sends'], 'the call went out'
    assert _task(run, 'action_1').blocked_reason != 'input_required'
    assert run.L.person_waits_on(run.key, 1, 0) is False


def test_a_step_with_no_question_out_still_steers_its_next_substep(speech):
    """The 2026-09-10 rule the sub-step hold exists for (a401c14a0) is kept:
    a split step that asked nobody works its sub-steps in the same turn."""
    def _writes(state, aid, content):
        return 'Noted the plan.' if aid == 1 else None

    run = speech(actions=[
        {'action': 'Plan the session: list three sounds to practise', 'action_id': 1,
         'can_perform_without_user_input': 'yes', 'recipe': []},
        {'action': 'say goodbye to Arjun', 'action_id': 2,
         'can_perform_without_user_input': 'yes', 'recipe': []},
    ], assistant_script=_writes, verdict_script=_splits_once)
    run.turn('hi, I am ready', 'request-1')
    assert run.state['sends'] == []
    assert any(SUBS[1]['description'] in s for s in _substep_steers(run)), run.state['steers']
