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

Rework after rv_camask's review of ede3b91c5 (REJECTED, board 2026-11-16
22:45:18Z), each case below taken from that review's probe:
 - only a question that was SENT holds the sub-steps (M1): a line the model
   wrote before the split reached no one, and the hold that took it ended the
   turn with no question asked and failed the step on the next turn;
 - only the parent's own split counts (S2a): add_subtasks stamps its
   sub-steps context['split_of']; a delegation, a dynamic child or a FAILED
   sub-step keeps the refusal (real SmartLedger, not fakes);
 - on the reply turn, the answered question holds nothing, and a send after
   the reply is a new question that waits (S2b);
 - the live 'yes'-step shape is pinned, and the re-prompts are counted (S2d).

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
NAME_Q = "What is your child's name?"
#: The live speech agent's flow 0 (1794540972213_0_recipe.json, read
#: 2026-11-16): every action is 'yes'; action 1 names send_message_to_user.
LIVE_FLOW0 = [
    {'action': ("send_message_to_user: get the child's name from memory key child_name; if it "
                "is not saved, ask the parent 'What is your child's name?' and wait for the answer"),
     'action_id': 1, 'can_perform_without_user_input': 'yes', 'recipe': []},
    {'action': ("save_data_in_memory: if the child's name was just asked, save the name from the "
                "parent's reply as child_name; then send_message_to_user '<child_name>!' and wait "
                "for the child to respond"),
     'action_id': 2, 'can_perform_without_user_input': 'yes', 'recipe': []},
    {'action': ("send_message_to_user: read the child's reply to their name in the person's latest "
                "message: if they responded, send 'Super <child_name>!'; if not, send "
                "'<child_name>, look here!'"),
     'action_id': 3, 'can_perform_without_user_input': 'yes', 'recipe': []},
]


def _send(state, text, expects=True):
    return {'content': '', 'tool_calls': [{
        'id': 'ask_%d' % len(state['sends']), 'type': 'function',
        'function': {'name': 'send_message_to_user',
                     'arguments': json.dumps({'text': text, 'expects_reply': expects})}}]}


def _asks_every_time(state, aid, content):
    """The live 4B: on the ask turn it sends its question each time it is
    steered."""
    if aid == 1 and state['turn'] == 'ask':
        return _send(state, ASK_LINE)
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


def _answers(run, action_id=1):
    ctx = getattr(_task(run, f'action_{action_id}'), 'context', None) or {}
    return [e.get('answer') for e in (ctx.get(run.L.USER_INPUT_EVIDENCE_KEY) or [])]


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


@pytest.fixture
def real_ledger():
    """A real SmartLedger (InMemoryBackend) with two steps, step 1 running,
    registered where lifecycle_hooks finds a session's ledger."""
    from agent_ledger.backends import InMemoryBackend
    from agent_ledger.core import SmartLedger, Task, TaskStatus, TaskType
    from hartos import lifecycle_hooks as L
    keys = []

    def make(key):
        led = SmartLedger(agent_id='61', session_id=key, backend=InMemoryBackend())
        for n in (1, 2):
            led.add_task(Task(task_id=f'action_{n}', description=f'step {n}',
                              task_type=TaskType.PRE_ASSIGNED))
        led.update_task_status('action_1', TaskStatus.IN_PROGRESS)
        L._ledger_registry[key] = led
        keys.append(key)
        return led

    yield make
    for key in keys:
        L._ledger_registry.pop(key, None)


def test_a_parent_blocked_on_its_own_split_takes_the_wait_in_place(real_ledger):
    """The split (ledger.add_subtasks, the call the verifier's breakdown
    makes) leaves the parent BLOCKED with no reason of its own; the wait is
    taken in place, its sub-steps kept, and recorded as task.block() records
    it (S2e): the reason in error_message and an entry in the state history."""
    from agent_ledger.core import TaskStatus
    from hartos import lifecycle_hooks as L
    led = real_ledger('split_takes_the_wait')
    led.add_subtasks(1, SUBS[:2])
    parent = led.tasks['action_1']
    assert parent.status == TaskStatus.BLOCKED and not parent.blocked_reason
    history = len(parent.state_history)
    assert L.block_for_user_input('split_takes_the_wait', 1, "waiting for the person's reply")
    assert parent.status == TaskStatus.BLOCKED
    assert parent.blocked_reason == 'input_required'
    assert parent.error_message == "waiting for the person's reply"
    assert len(parent.state_history) == history + 1
    assert "waiting for the person's reply" in str(parent.state_history[-1])
    assert {led.tasks[s['subtask_id']].status for s in SUBS[:2]} == {TaskStatus.PENDING}


def test_a_parent_blocked_on_anything_but_its_own_split_keeps_the_refusal(real_ledger):
    """Review of ede3b91c5, S2a, each block made by the ledger's own calls:
    a delegation to another agent (create_parent_child_task and the bridge's
    BLOCKED), a dynamic child (add_dynamic_task, relationship 'child'), a
    split whose only sub-step FAILED, and a split parent already blocked for
    a reason of its own.  None of them is the person's input wait."""
    from agent_ledger.core import TaskStatus, TaskType
    from hartos import lifecycle_hooks as L

    def refused(key, led):
        parent = led.tasks['action_1']
        before = (parent.status, parent.blocked_reason)
        assert L.block_for_user_input(key, 1, 'q') is False, key
        assert (parent.status, parent.blocked_reason) == before, key

    led = real_ledger('delegation')
    child = led.create_parent_child_task(
        parent_task_id='action_1', child_description='look up the clinic hours',
        child_type=TaskType.AUTONOMOUS, context={'delegated_to': 'specialist_agent'})
    led.update_task_status('action_1', TaskStatus.BLOCKED,
                           f'Waiting for delegated task: {child.task_id}')
    refused('delegation', led)

    led = real_ledger('dynamic_child')
    led._get_default_llm_client = lambda: None
    # The classifier is the model's seat; it judged the new task a child.
    led._classify_task_relationship = lambda **kw: {
        'relationship': 'child', 'execution_mode': 'sequential',
        'related_to_task_id': 'action_1'}
    dyn = led.add_dynamic_task('follow up the clinic', {'current_action_id': 1})
    assert dyn is not None and dyn.parent_task_id == 'action_1'
    assert led.tasks['action_1'].status == TaskStatus.BLOCKED
    refused('dynamic_child', led)

    led = real_ledger('failed_substep')
    led.add_subtasks(1, SUBS[:1])
    led.update_task_status('1.1', TaskStatus.IN_PROGRESS)
    led.update_task_status('1.1', TaskStatus.FAILED, error_message='tool refused')
    refused('failed_substep', led)

    for reason in ('approval_required', 'dependency', 'input_required'):
        key = 'own_reason_' + reason
        led = real_ledger(key)
        led.add_subtasks(1, SUBS[:2])
        led.tasks['action_1'].set_blocked_reason(reason)
        refused(key, led)


@pytest.mark.parametrize('first', ['I will now ask Arjun to say ball.', ASK_LINE],
                         ids=['its_plan', 'the_question_as_text'])
def test_a_question_only_written_before_the_split_does_not_end_the_turn(speech, first):
    """Review of ede3b91c5, M1 (probe p3a/p3b): a 'no' step naming
    send_message_to_user whose model WRITES a line (its plan, or the question
    as text) before the verifier splits it.  Nothing reached the person, so
    the split does not hold; the sub-step steer makes the model send the
    question, that send holds, and the reply is taken as the answer."""
    def assistant(state, aid, content):
        if aid == 1 and state['turn'] == 'ask':
            if content.startswith('Work on subtask: '):
                return _send(state, ASK_LINE)
            return first
        return None

    run = speech(assistant_script=assistant, verdict_script=_splits_once)
    run.turn('hi, I am ready', 'request-1')
    assert run.state['sends'] == [ASK_LINE], run.state['sends']
    ask = run.L.ledger_open_ask(run.key, 1, 0) or {}
    assert ask.get('via') == 'send', ask
    assert ask.get('question') == ASK_LINE, ask
    assert run.L.person_waits_on(run.key, 1, 0) is True
    run.turn('ba', 'request-2', kind='reply')
    assert run.L.ledger_user_answered(run.key, 1, 0) is True
    assert _answers(run) == ['ba'], _answers(run)
    from agent_ledger.core import TaskStatus
    assert _task(run, 'action_1').status != TaskStatus.FAILED


def test_a_substeps_own_question_on_the_reply_turn_waits_for_its_answer(speech):
    """Review of ede3b91c5, S2b (probe p5): on the turn that brings the
    reply, a sub-step that asks the person something new ("how old is
    he?") holds for that answer; the next sub-step does not run at once."""
    subs = [
        {'subtask_id': '1.1', 'description': 'Ask Arjun to say ball'},
        {'subtask_id': '1.2', 'description': 'Ask the parent how old Arjun is'},
        {'subtask_id': '1.3', 'description': 'Praise Arjun by name'},
    ]
    age_q = 'How old is Arjun?'

    def assistant(state, aid, content):
        if aid == 1 and state['turn'] == 'ask':
            return _send(state, ASK_LINE)
        if aid == 1 and 'how old' in content.lower() and age_q not in state['sends']:
            return _send(state, age_q)
        return None

    def verdict(state, aid, content):
        if aid == 1 and not state.get('broke'):
            state['broke'] = True
            return json.dumps({'status': 'requires_breakdown', 'action_id': 1,
                               'subtasks': subs, 'message': 'split'})
        return json.dumps({'status': 'completed', 'action_id': aid,
                           'can_perform_without_user_input': 'no'})

    run = speech(assistant_script=assistant, verdict_script=verdict)
    run.turn('hi, I am ready', 'request-1')
    assert run.state['sends'] == [ASK_LINE]
    before = len(run.state['steers'])
    run.turn('ba', 'request-2', kind='reply')
    assert run.state['sends'] == [ASK_LINE, age_q], run.state['sends']
    assert not any(subs[2]['description'] in s for s in _substep_steers(run, before)), \
        'the sub-step after the new question ran before its answer came'
    assert run.rr.user_tasks[run.key].current_action == 1
    assert run.L.person_waits_on(run.key, 1, 0) is True
    assert (run.L.ledger_open_ask(run.key, 1, 0) or {}).get('question') == age_q
    run.turn('3 years', 'request-3', kind='reply')
    assert '3 years' in _answers(run), _answers(run)
    assert run.state['sends'] == [ASK_LINE, age_q], 'nobody is asked again'


def test_a_model_that_asks_on_every_steer_asks_once_per_turn(speech):
    """Review of ede3b91c5, S2b (probe p2): a 'no' step whose model re-sends
    its question every time it is steered, on the reply turn as well.  The
    reply turn sends it once (it was three times, once per sub-step) and
    waits for the person, as the ask turn does."""
    def assistant(state, aid, content):
        return _send(state, ASK_LINE) if aid == 1 else None

    run = speech(assistant_script=assistant, verdict_script=_splits_once)
    run.turn('hi, I am ready', 'request-1')
    assert run.state['sends'] == [ASK_LINE]
    run.turn('ba', 'request-2', kind='reply')
    assert run.state['sends'] == [ASK_LINE] * 2, run.state['sends']
    assert 'ba' in _answers(run), _answers(run)
    assert run.L.person_waits_on(run.key, 1, 0) is True


def test_the_live_name_step_asks_once_and_takes_the_name(speech):
    """Review of ede3b91c5, S2d (probe p1): the live flow 0 as banked (every
    step 'yes'), the 4B sending the name question with expects_reply on every
    steer of the ask turn, the verifier splitting step 1 once.  One question,
    the reply saved as the name, and the child's sound next turn is not."""
    def assistant(state, aid, content):
        if aid == 1 and state['turn'] == 'ask':
            return _send(state, NAME_Q)
        if aid == 1:
            return 'Thank you, I will call him Arjun.'
        return None

    def verdict(state, aid, content):
        if aid == 1 and not state.get('broke'):
            state['broke'] = True
            return json.dumps({'status': 'requires_breakdown', 'action_id': 1,
                               'subtasks': SUBS, 'message': 'split'})
        return json.dumps({'status': 'completed', 'action_id': aid})

    run = speech(actions=LIVE_FLOW0, assistant_script=assistant, verdict_script=verdict)
    run.turn('hi', 'request-1')
    assert run.state['sends'] == [NAME_Q], run.state['sends']
    assert run.L.person_waits_on(run.key, 1, 0) is True
    run.turn('Arjun', 'request-2', kind='reply')
    assert _answers(run) == ['Arjun'], _answers(run)
    assert run.state['sends'] == [NAME_Q]
    run.turn('Aaa!', 'request-3', kind='reply')
    assert 'Aaa!' not in _answers(run), _answers(run)


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
    # Every re-prompt went out: one call when the step was dispatched and one
    # each time a sub-step was steered (review of ede3b91c5, S2d), and no
    # sub-step was held back.
    assert len(_substep_steers(run)) == len(SUBS), run.state['steers']
    assert run.state['sends'] == ['Arjun!'] * (1 + len(SUBS)), run.state['sends']
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
