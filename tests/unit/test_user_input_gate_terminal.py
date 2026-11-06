"""Guard: a TERMINAL action must never be flagged as blocked on user input.

MEASURED LIVE 2026-09-07, agent 88761328396 ("Nunba Error Reporter",
debug.local.falcon) — a 5-action recipe created through the live /chat CREATE
flow.  All five action files were written by 11:40:05, action 5 among them:

    88761328396_0_5.json  action='Output the extracted line verbatim'
                          status='done'  can_perform_without_user_input='yes'

and the state machine agreed it was finished:

    11:41:40  Action 5: terminated [OK] TERMINATED
    11:41:40  [LOCKED] Action 5 in terminated - skipping assignment hook

Yet SIX MINUTES LATER the create loop began telling the user it was stuck on
exactly that step, and kept doing so for another 14 minutes:

    11:43:32  [USER-INPUT-GATE] Action 5 flagged as blocked on user input
              (can_perform_without_user_input='no')          <- x8
    11:46:15  [USER-INPUT-GATE] OUTER loop returning at iteration #12
    11:46:33  Clearing prior block on action 5 - user has replied
    11:59:45  [USER-INPUT-GATE] Action 5 flagged as blocked ...  <- again

User-visible cost: 20.4 minutes of wall clock across three turns, each ending
with "Step 5 ... isn't coming together from what I have so far", on a recipe
that had been complete since 11:40:05.  The build could never finish, because
answering the question could not change a verdict about an action that was
already over.

MECHANISM (measured, not inferred).  `create_recipe.py`'s StatusVerifier
`pending` branch does two things:

    safe_set_state(user_prompt, current_action_id, ActionState.PENDING, ...)
    ... if gate value starts with 'no': set _needs_user_input_action_id

The first is correctly REFUSED on a terminal action — verified against the real
state machine:

    validate_state_transition(TERMINATED -> PENDING)          -> False
    [ERROR] Invalid transition: Action 5 cannot go from terminated to pending
    state after safe_set_state(PENDING)                        -> terminated

but the flag was set regardless, because nothing checked whether the transition
took.  The sticky flag then drives the OUTER-loop gate, which asks the user
about a finished step forever.

THE PREDICATE, deliberately NARROW: block iff the verdict says 'no' AND the
action is not TERMINAL.  The tempting stronger form — require the state to read
back as PENDING — would also stop blocking for ASSIGNED and IN_PROGRESS, and
nothing measured says those are broken; ASSIGNED->PENDING is refused by the
same table, so that form would have silently changed a second behaviour on a
hunch.  Terminal is the only case with evidence, so terminal is the only case
this narrows.

Terminality is asked of `lifecycle_hooks.is_terminal_state`, which reads that
module's own `_TERMINAL_STATES`, rather than re-listing the states here — a
fourth copy of that tuple is exactly the drift this codebase keeps paying for.

    python -m pytest tests/unit/test_user_input_gate_terminal.py -q
"""
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hartos import create_recipe
from hartos.create_recipe import (
    _resume_prior_user_input_block, _should_block_on_user_input,
)
from hartos.lifecycle_hooks import (
    ActionState, clear_action_states, force_state_through_valid_path,
    get_action_state, safe_set_state,
)

_UP = 'gate_terminal_probe'


class UserInputGateTerminal(unittest.TestCase):

    def setUp(self):
        clear_action_states(_UP)

    def tearDown(self):
        clear_action_states(_UP)

    def test_terminated_action_is_NOT_blocked(self):
        """THE REGRESSION — the exact live state of 88761328396's action 5."""
        force_state_through_valid_path(_UP, 5, ActionState.TERMINATED,
                                       'flow-complete force')
        # Precondition: this is the refusal the fix keys on.
        safe_set_state(_UP, 5, ActionState.PENDING, 'verifier pending')
        self.assertEqual(get_action_state(_UP, 5), ActionState.TERMINATED,
                         'precondition: PENDING must be refused on a terminal action')
        self.assertFalse(
            _should_block_on_user_input(_UP, 5, 'no'),
            'a finished action cannot be waiting for the user')

    def test_genuinely_pending_action_IS_blocked(self):
        """The gate must still fire for its real case — 2026-05-08's Action 3.

        Without this the fix would be a mute button, not a repair: the gate
        exists to stop the StatusVerifier drifting to 'yes' over retries and
        hallucinating a confirmation the user never gave.
        """
        safe_set_state(_UP, 3, ActionState.IN_PROGRESS, 'working')
        safe_set_state(_UP, 3, ActionState.STATUS_VERIFICATION_REQUESTED, 'verify')
        safe_set_state(_UP, 3, ActionState.PENDING, 'verifier pending')
        self.assertEqual(get_action_state(_UP, 3), ActionState.PENDING,
                         'precondition: the action really is pending')
        self.assertTrue(
            _should_block_on_user_input(_UP, 3, 'no'),
            "a pending action with 'no' MUST still block — this is the gate's job")

    def test_yes_never_blocks(self):
        """'yes' is the recipe's own answer: no input needed."""
        safe_set_state(_UP, 3, ActionState.IN_PROGRESS, 'working')
        safe_set_state(_UP, 3, ActionState.STATUS_VERIFICATION_REQUESTED, 'verify')
        safe_set_state(_UP, 3, ActionState.PENDING, 'verifier pending')
        self.assertFalse(_should_block_on_user_input(_UP, 3, 'yes'))

    def test_missing_or_empty_gate_value_never_blocks(self):
        """Only an explicit 'no' blocks; absence is not consent to stall."""
        safe_set_state(_UP, 3, ActionState.IN_PROGRESS, 'working')
        safe_set_state(_UP, 3, ActionState.STATUS_VERIFICATION_REQUESTED, 'verify')
        safe_set_state(_UP, 3, ActionState.PENDING, 'verifier pending')
        for value in ('', None, '   '):
            self.assertFalse(_should_block_on_user_input(_UP, 3, value),
                             f'{value!r} must not block')

    def test_completed_action_is_NOT_blocked(self):
        """TERMINATED is not the only terminal — COMPLETED must behave the same."""
        safe_set_state(_UP, 2, ActionState.IN_PROGRESS, 'working')
        safe_set_state(_UP, 2, ActionState.STATUS_VERIFICATION_REQUESTED, 'verify')
        safe_set_state(_UP, 2, ActionState.COMPLETED, 'verified complete')
        self.assertFalse(_should_block_on_user_input(_UP, 2, 'no'))

    def test_unknown_action_keeps_todays_behaviour(self):
        """An unseen id reads ASSIGNED — non-terminal, so nothing changes.

        `get_action_state` defaults an unknown (user_prompt, action_id) to
        ASSIGNED, which is not terminal, so the gate still blocks exactly as it
        does today.  Pinned deliberately: the fix must narrow the TERMINAL case
        and nothing else, and this is the case most likely to be narrowed by
        accident.
        """
        self.assertTrue(_should_block_on_user_input(_UP, 999, 'no'))

    def test_same_round_cannot_answer_the_block_it_just_created(self):
        task = SimpleNamespace(_needs_user_input_action_id=None)
        create_recipe.user_tasks[_UP] = task
        create_recipe.request_id_list[_UP] = 'user-request-1'
        with patch('hartos.create_recipe.resume_from_user_input') as resume:
            self.assertFalse(
                _resume_prior_user_input_block(_UP, 'original request'))
        resume.assert_not_called()

    def test_daemon_retry_cannot_impersonate_human_unblocker(self):
        task = SimpleNamespace(
            _needs_user_input_action_id=3, _needs_help_reason='need choice',
            _needs_user_input_kind='human_required')
        create_recipe.user_tasks[_UP] = task
        create_recipe.request_id_list[_UP] = 'daemon_goal-123'
        with patch('hartos.create_recipe.resume_from_user_input') as resume:
            self.assertFalse(
                _resume_prior_user_input_block(_UP, 'retry original task'))
        self.assertEqual(task._needs_user_input_action_id, 3)
        resume.assert_not_called()

    def test_assigned_expert_can_retry_a_recoverable_stall(self):
        task = SimpleNamespace(
            _needs_user_input_action_id=3,
            _needs_help_reason='conversation looped',
            _needs_user_input_kind='recoverable_stall')
        create_recipe.user_tasks[_UP] = task
        create_recipe.request_id_list[_UP] = 'daemon_goal-123'
        with patch('hartos.create_recipe._is_serving_escalation_expert',
                   return_value=True), patch(
                       'hartos.create_recipe.resume_blocked_action',
                       return_value=True) as resume:
            self.assertTrue(
                _resume_prior_user_input_block(_UP, 'retry with expert'))
        resume.assert_called_once()
        self.assertIsNone(task._needs_user_input_action_id)
        self.assertIsNone(task._needs_user_input_kind)

    def test_expert_cannot_clear_a_human_authority_gate(self):
        task = SimpleNamespace(
            _needs_user_input_action_id=3,
            _needs_help_reason='payment approval needed',
            _needs_user_input_kind='human_required')
        create_recipe.user_tasks[_UP] = task
        create_recipe.request_id_list[_UP] = 'daemon_goal-123'
        with patch('hartos.create_recipe._is_serving_escalation_expert',
                   return_value=True), patch(
                       'hartos.create_recipe.resume_blocked_action') as resume:
            self.assertFalse(
                _resume_prior_user_input_block(_UP, 'approve it'))
        self.assertEqual(task._needs_user_input_action_id, 3)
        resume.assert_not_called()

    def test_genuine_next_turn_resumes_and_records_the_answer(self):
        task = SimpleNamespace(
            _needs_user_input_action_id=3, _needs_help_reason='need choice',
            _needs_user_input_kind='human_required')
        create_recipe.user_tasks[_UP] = task
        create_recipe.request_id_list[_UP] = 'user-request-2'
        with patch('hartos.create_recipe.resume_from_user_input',
                   return_value=True) as resume, \
                patch('hartos.create_recipe.record_user_answer',
                      return_value=True) as record:
            self.assertTrue(
                _resume_prior_user_input_block(_UP, 'Use account A'))
        resume.assert_called_once_with(
            _UP, 3, 'User supplied input for the blocked action')
        # The one writer of the person's answer, tagged with the flow it
        # answers (a later flow's action_1 is the same ledger task).
        record.assert_called_once_with(
            _UP, 3, 'Use account A', create_recipe.get_current_flow(_UP),
            request_id='user-request-2')
        self.assertIsNone(task._needs_user_input_action_id)

    def test_failed_durable_resume_keeps_the_sticky_gate(self):
        task = SimpleNamespace(
            _needs_user_input_action_id=3,
            _needs_help_reason='need choice',
            _needs_user_input_kind='human_required')
        create_recipe.user_tasks[_UP] = task
        create_recipe.request_id_list[_UP] = 'user-request-2'
        with patch('hartos.create_recipe.resume_from_user_input',
                   return_value=False) as resume:
            self.assertFalse(
                _resume_prior_user_input_block(_UP, 'Use account A'))
        resume.assert_called_once()
        self.assertEqual(task._needs_user_input_action_id, 3)
        self.assertEqual(task._needs_user_input_kind, 'human_required')
        self.assertEqual(task._needs_help_reason, 'need choice')


class TheReplyMovesTheActionOutOfPending(unittest.TestCase):
    """hartos-f9's replay of 49afbeba4: mark_action_waiting_for_user projects
    the wait into BOTH authorities (ledger BLOCKED, ActionState PENDING); the
    reply resumed only the ledger.  Left PENDING, the loop re-read the verdict
    still at the tail, the verifier hook answered 'allow' without moving a
    PENDING action, [COMPLETION-GATE] left it open, and the lap repeated to
    the stall guard (120 laps; action 2 pending for every turn run), the
    person receiving the verifier's message as the reply.  Before 49afbeba4
    the false [CLAIM REJECTED] re-posted the action and moved the state by
    accident.  Driven through the real hooks and a real ledger."""

    def setUp(self):
        import tempfile
        from agent_ledger.core import SmartLedger, add_actions_to_ledger
        from hartos import lifecycle_hooks as L
        self.L = L
        clear_action_states(_UP)
        self.ledger = SmartLedger(agent_id='54', session_id=f'{_UP}_3',
                                  ledger_dir=tempfile.mkdtemp())
        add_actions_to_ledger(self.ledger, [
            {'action': 'say Hello Arjun and wait', 'action_id': 1},
            {'action': 'ask the child to say ball', 'action_id': 3},
        ], flow_id=0)
        L.register_ledger_for_session(_UP, self.ledger)
        create_recipe.user_tasks[_UP] = SimpleNamespace(
            _needs_user_input_action_id=3, _needs_help_reason='say ball',
            _needs_user_input_kind='human_required')
        create_recipe.request_id_list[_UP] = 'user-request-9'

    def tearDown(self):
        clear_action_states(_UP)
        with self.L._state_lock:
            self.L._ledger_registry.pop(_UP, None)
        for store in (create_recipe.user_tasks, create_recipe.request_id_list):
            try:
                del store[_UP]
            except Exception:
                pass

    def test_a_genuine_reply_puts_the_action_back_in_progress(self):
        from agent_ledger.core import TaskStatus
        task = self.ledger.tasks['action_3']
        task.status = TaskStatus.IN_PROGRESS
        safe_set_state(_UP, 3, ActionState.IN_PROGRESS, 'working')
        self.assertTrue(self.L.mark_action_waiting_for_user(_UP, 3, 'say ball'))
        self.assertEqual(get_action_state(_UP, 3), ActionState.PENDING)
        self.assertEqual(task.status, TaskStatus.BLOCKED)

        self.assertTrue(_resume_prior_user_input_block(_UP, 'ball'))

        self.assertEqual(task.status, TaskStatus.IN_PROGRESS)
        self.assertEqual(get_action_state(_UP, 3), ActionState.IN_PROGRESS,
                         'the reply moved the ledger but left the action PENDING')
        self.assertTrue(task.verify_integrity(),
                        'G2: the seal must survive the production resume path')

    def test_a_release_the_ledger_never_held_moves_it_too(self):
        """The other resume: the verifier's 'pending' set PENDING, the ledger
        refused the block (task not in progress), and the reply releases the
        gate through ledger_holds_no_block.  Same projection."""
        from agent_ledger.core import TaskStatus
        task = self.ledger.tasks['action_3']
        self.assertNotEqual(task.status, TaskStatus.IN_PROGRESS)
        safe_set_state(_UP, 3, ActionState.IN_PROGRESS, 'working')
        safe_set_state(_UP, 3, ActionState.STATUS_VERIFICATION_REQUESTED, 'verify')
        safe_set_state(_UP, 3, ActionState.PENDING, 'verifier pending')
        self.assertFalse(self.L.block_for_user_input(_UP, 3, 'say ball'))

        self.assertTrue(_resume_prior_user_input_block(_UP, 'ball'))

        self.assertEqual(get_action_state(_UP, 3), ActionState.IN_PROGRESS)
        self.assertTrue(
            self.ledger.tasks['action_3'].context.get(self.L.USER_INPUT_EVIDENCE_KEY),
            'the released answer must still be recorded, or the next '
            'completed + no asks again')
        self.assertTrue(self.ledger.tasks['action_3'].verify_integrity())

    def test_the_assigned_experts_turn_moves_it_too_and_is_no_persons_answer(self):
        """The projection runs for the expert path as well (same resume
        site); an expert's evidence stays unblock_evidence, never a person's
        answer."""
        from agent_ledger.core import TaskStatus
        task = self.ledger.tasks['action_3']
        task.status = TaskStatus.IN_PROGRESS
        safe_set_state(_UP, 3, ActionState.IN_PROGRESS, 'working')
        self.assertTrue(self.L.mark_action_waiting_for_user(_UP, 3, 'needs an expert'))
        create_recipe.user_tasks[_UP]._needs_user_input_kind = 'recoverable_stall'
        create_recipe.request_id_list[_UP] = 'daemon_expert_turn'
        with patch('hartos.create_recipe._is_serving_escalation_expert',
                   return_value=True):
            self.assertTrue(_resume_prior_user_input_block(_UP, 'expert text'))
        self.assertEqual(get_action_state(_UP, 3), ActionState.IN_PROGRESS)
        self.assertEqual(task.status, TaskStatus.IN_PROGRESS)
        self.assertTrue(task.context.get('unblock_evidence'))
        self.assertFalse(task.context.get(self.L.USER_INPUT_EVIDENCE_KEY),
                         "an expert's turn is not the person's answer")

    def _wait_on_action_3(self):
        from agent_ledger.core import TaskStatus
        task = self.ledger.tasks['action_3']
        task.status = TaskStatus.IN_PROGRESS
        safe_set_state(_UP, 3, ActionState.IN_PROGRESS, 'working')
        # As the create loop marks it: the flow and the person's request that
        # asked, an earlier one than the reply's (setUp's user-request-9).
        self.assertTrue(self.L.mark_action_waiting_for_user(
            _UP, 3, 'say ball', flow=0, asked_in='user-request-8'))
        return task

    def test_the_answer_is_tagged_with_the_flow_it_answers(self):
        """G6: flow 1's action_3 is flow 0's ledger task, so the record says
        which flow the answer belongs to; a record stamped flow 0 for every
        answer would close flow 1's question with flow 0's reply."""
        task = self._wait_on_action_3()
        create_recipe.recipe_for_persona[_UP] = 1
        try:
            self.assertTrue(_resume_prior_user_input_block(_UP, 'ball'))
        finally:
            create_recipe.initialise_current_flow_to_zero(_UP)
        self.assertTrue(self.L.ledger_user_answered(_UP, 3, 1))
        self.assertFalse(self.L.ledger_user_answered(_UP, 3, 0))
        self.assertTrue(task.verify_integrity())

    def test_an_answer_the_ledger_cannot_save_keeps_the_gate(self):
        """G3: the resume saved but the answer's save failed.  The answer then
        exists only in memory, so the gate stays (the same answer is retried
        next turn) and the sealed context is put back as it was."""
        task = self._wait_on_action_3()
        before = dict(task.context)
        real_save = self.ledger.save
        calls = {'n': 0}

        def save_once_then_fail(*args, **kwargs):
            calls['n'] += 1
            return real_save(*args, **kwargs) if calls['n'] == 1 else False

        with patch.object(self.ledger, 'save', side_effect=save_once_then_fail):
            self.assertFalse(_resume_prior_user_input_block(_UP, 'ball'))
        self.assertEqual(create_recipe.user_tasks[_UP]._needs_user_input_action_id, 3,
                         'an unrecorded answer must not clear the gate')
        self.assertEqual(task.context, before)
        self.assertTrue(task.verify_integrity())

    def test_after_a_restart_the_ledger_alone_names_the_waiting_action(self):
        """G5: the sticky flag lives on the in-memory task and a restart
        between the ask and the reply loses it; the ledger still holds the
        task BLOCKED for the person, so the reply resumes that action."""
        from agent_ledger.core import TaskStatus
        task = self._wait_on_action_3()
        # A fresh process: the loop is back on the waiting action, no flag.
        create_recipe.user_tasks[_UP] = SimpleNamespace(current_action=3)
        self.assertTrue(_resume_prior_user_input_block(_UP, 'ball'))
        self.assertEqual(task.status, TaskStatus.IN_PROGRESS)
        self.assertTrue(self.L.ledger_user_answered(_UP, 3, 0))
        self.assertIsNone(getattr(create_recipe.user_tasks[_UP],
                                  '_needs_user_input_action_id', None))
        self.assertTrue(task.verify_integrity())


class AGateTheLedgerNeverHeld(unittest.TestCase):
    """LIVE 2026-10-10 14:48-14:53 IST, agent 54, user 10202.  CREATE resumed a
    session another build had left (its action_2 FAILED).  The verifier said
    action 2 waits for the person; block_for_user_input refused the block (the
    task was not in progress) and the gate flag was set anyway.  The person's
    "yes" then read:

        [USER-INPUT-GATE] Keeping action 2 blocked: durable resume from a
        genuine user reply failed

    and got the same question back, every turn: nothing could resume a block
    the ledger never held.  A genuine reply releases a gate whose block is not
    in the ledger; a block the ledger DOES hold is still kept when its resume
    fails, so the same answer can be retried."""

    def setUp(self):
        import tempfile
        from agent_ledger.core import SmartLedger, Task, TaskType
        from hartos import lifecycle_hooks as L
        self.L = L
        clear_action_states(_UP)
        self.ledger = SmartLedger(agent_id='54', session_id=f'{_UP}_1',
                                  ledger_dir=tempfile.mkdtemp())
        self.ledger.add_task(Task(task_id='action_2', description='reply',
                                  task_type=TaskType.PRE_ASSIGNED))
        L.register_ledger_for_session(_UP, self.ledger)
        self.task = SimpleNamespace(
            _needs_user_input_action_id=2, _needs_help_reason='confirm topic',
            _needs_user_input_kind='human_required')
        create_recipe.user_tasks[_UP] = self.task
        create_recipe.request_id_list[_UP] = 'user-request-3'

    def tearDown(self):
        clear_action_states(_UP)
        with self.L._state_lock:
            self.L._ledger_registry.pop(_UP, None)

    def test_a_block_the_ledger_never_held_is_released_by_the_reply(self):
        """THE LIVE SHAPE: the task the ledger holds is FAILED, not BLOCKED."""
        from agent_ledger.core import TaskStatus
        self.ledger.tasks['action_2'].status = TaskStatus.FAILED
        self.assertTrue(_resume_prior_user_input_block(_UP, 'yes'),
                        'the answer could never release this gate')
        self.assertIsNone(self.task._needs_user_input_action_id)
        self.assertIsNone(self.task._needs_user_input_kind)
        self.assertIsNone(self.task._needs_help_reason)

    def test_a_block_the_ledger_holds_is_kept_when_its_resume_fails(self):
        """The storage-failure case the sticky gate exists for: the task IS
        blocked, its resume could not be saved, the same answer can retry."""
        from agent_ledger.core import TaskStatus
        self.ledger.tasks['action_2'].status = TaskStatus.BLOCKED
        with patch('hartos.create_recipe.resume_from_user_input',
                   return_value=False):
            self.assertFalse(_resume_prior_user_input_block(_UP, 'yes'))
        self.assertEqual(self.task._needs_user_input_action_id, 2)

    def test_a_daemon_turn_still_cannot_release_it(self):
        """Releasing a block the ledger never held is the PERSON's to do; a
        background retry is still not their answer."""
        from agent_ledger.core import TaskStatus
        self.ledger.tasks['action_2'].status = TaskStatus.FAILED
        create_recipe.request_id_list[_UP] = 'daemon_goal-123'
        self.assertFalse(_resume_prior_user_input_block(_UP, 'retry'))
        self.assertEqual(self.task._needs_user_input_action_id, 2)


class AResumedTaskStillPassesItsIntegrityCheck(unittest.TestCase):
    """#159, MEASURED 2026-10-30 on the installed desktop (hartos-f9's reading):
    the gate asked, the person answered, resume_from_user_input released the
    block, the verifier passed the step, and the completion claim was refused:

        [CLAIM REJECTED] Task action_3 integrity check failed — data_hash
        mismatch, possible corruption

    four times on the flow 1 duplicate (20:13:07 to 20:13:48), four on the
    flow 3 text-only agent, six on the Spoken English Coach, six on flow 7:
    21 refusals in 23 minutes, each followed by the same question again.

    WHY: Task.compute_data_hash covers `context`, and the ledger seals the
    hash only in add_task and update_task_context.  resume_blocked_action
    appended the person's answer straight into task.context and called
    ledger.save(), which does not seal, so the stored hash described the
    task from BEFORE the answer.  The claim handler's verify_integrity then
    read the person's own reply as corruption.
    """

    def setUp(self):
        import tempfile
        from agent_ledger.core import SmartLedger, add_actions_to_ledger
        from hartos import lifecycle_hooks as L
        self.L = L
        clear_action_states(_UP)
        self.ledger = SmartLedger(agent_id='54', session_id=f'{_UP}_2',
                                  ledger_dir=tempfile.mkdtemp())
        # The create loop's own path (create_action_with_ledger ->
        # add_actions_to_ledger) seals every task it adds; a bare add_task
        # leaves data_hash None and verify_integrity then passes vacuously.
        add_actions_to_ledger(self.ledger, [
            {'action': 'say Hello Arjun and wait', 'action_id': 1},
            {'action': 'ask the child to say ball', 'action_id': 3},
        ], flow_id=0)
        L.register_ledger_for_session(_UP, self.ledger)

    def tearDown(self):
        clear_action_states(_UP)
        with self.L._state_lock:
            self.L._ledger_registry.pop(_UP, None)
        for store in (create_recipe.user_tasks, create_recipe.request_id_list):
            try:
                del store[_UP]
            except Exception:
                pass

    def _reply(self, text='Hi!', request_id='user-request-7'):
        """The production path: the create loop's own resume takes the reply."""
        create_recipe.user_tasks[_UP] = SimpleNamespace(
            _needs_user_input_action_id=3, _needs_help_reason='waiting for Arjun',
            _needs_user_input_kind='human_required')
        create_recipe.request_id_list[_UP] = request_id
        return _resume_prior_user_input_block(_UP, text)

    def _block(self):
        from agent_ledger.core import TaskStatus
        task = self.ledger.tasks['action_3']
        self.assertIsNotNone(task.data_hash, 'precondition: the create path seals its tasks')
        task.status = TaskStatus.IN_PROGRESS
        self.assertTrue(self.L.block_for_user_input(_UP, 3, 'waiting for Arjun'))
        self.assertTrue(task.verify_integrity(), 'precondition: the block alone keeps the seal')
        return task

    def test_the_persons_answer_is_kept_and_the_seal_still_holds(self):
        """Through the production path (G2): the create loop's resume takes
        the reply, resumes the ledger and records the answer in one record."""
        task = self._block()
        self.assertTrue(self._reply('Hi!'))
        self.assertEqual(task.context[self.L.USER_INPUT_EVIDENCE_KEY][0]['answer'], 'Hi!',
                         'the answer must land in the task, that is the point of resuming')
        self.assertTrue(
            task.verify_integrity(),
            'the stored hash describes the task from before the answer: the '
            'completion claim will be refused as "data_hash mismatch" (21 times '
            'live on 2026-10-30)')

    def test_the_seal_holds_on_the_released_path_too(self):
        """The ledger refused the block (its task was not in progress, live
        2026-10-10 agent 54): the reply releases the gate through
        ledger_holds_no_block, and the answer is still recorded and sealed."""
        from agent_ledger.core import TaskStatus
        task = self.ledger.tasks['action_3']
        self.assertIsNotNone(task.data_hash)
        self.assertNotEqual(task.status, TaskStatus.BLOCKED)
        self.assertTrue(self._reply('Hi!'))
        self.assertEqual(task.context[self.L.USER_INPUT_EVIDENCE_KEY][0]['answer'], 'Hi!')
        self.assertTrue(task.verify_integrity())

    def test_an_experts_evidence_is_sealed_the_same_way(self):
        task = self._block()
        self.assertTrue(self.L.resume_blocked_action(
            _UP, 3, 'Assigned expert is retrying',
            evidence={'source': 'escalation_expert', 'action_id': 3}))
        self.assertEqual(task.context['unblock_evidence'][0]['source'], 'escalation_expert')
        self.assertTrue(task.verify_integrity())

    def test_a_resume_without_evidence_is_unchanged(self):
        task = self._block()
        self.assertTrue(self.L.resume_blocked_action(_UP, 3, 'Block resolved'))
        self.assertTrue(task.verify_integrity())
        self.assertNotIn('unblock_evidence', task.context)


class ThePersonsWaitIsTheAskRecord(unittest.TestCase):
    """hartos-00's review of cb13208d2, items 1, 4 and 5.  The ledger alone
    could not tell a person's wait from a daemon's park: _ask_for_help, the
    verifier hook and mark_action_waiting_for_user all leave a task BLOCKED
    with blocked_reason input_required.  cb13208d2 derived the waiting action
    from that on any turn, for the lowest such task: a daemon turn and the
    assigned expert's turn took a gate on a parked action and asked for help
    again with the wrong reason, a person's unrelated chat answered a
    daemon's park, a reply for action 3 resumed action 2, and a flow-0 wait
    was answered in flow 1.  The person's wait is now the ask record REUSE
    already keeps (record_ask_delivered on a genuine request, read by
    ledger_open_ask, flow-matched), and only a person's turn derives it, for
    the current action.  Driven through the real create loop resume, the real
    hooks and a real JSON ledger."""

    def setUp(self):
        import tempfile
        from agent_ledger.core import SmartLedger, add_actions_to_ledger
        from hartos import lifecycle_hooks as L
        self.L = L
        clear_action_states(_UP)
        self.ledger = SmartLedger(agent_id='54', session_id=f'{_UP}_ask',
                                  ledger_dir=tempfile.mkdtemp())
        add_actions_to_ledger(self.ledger, [
            {'action': 'say Hello Arjun', 'action_id': 1},
            {'action': 'ask the child his name', 'action_id': 2},
            {'action': 'ask the child to say ball', 'action_id': 3},
        ], flow_id=0)
        L.register_ledger_for_session(_UP, self.ledger)
        # The create loop holds the session's ledger in both places
        # (_ask_for_help reads its own cache).
        create_recipe.user_ledgers[_UP] = self.ledger
        create_recipe.user_tasks[_UP] = SimpleNamespace(current_action=3)
        create_recipe.request_id_list[_UP] = 'user-request-2'

    def tearDown(self):
        clear_action_states(_UP)
        create_recipe.initialise_current_flow_to_zero(_UP)
        with self.L._state_lock:
            self.L._ledger_registry.pop(_UP, None)
        for store in (create_recipe.user_tasks, create_recipe.request_id_list,
                      create_recipe.user_ledgers):
            try:
                del store[_UP]
            except Exception:
                pass

    def _task(self, aid):
        return self.ledger.tasks[f'action_{aid}']

    def _in_progress(self, aid):
        from agent_ledger.core import TaskStatus
        self._task(aid).status = TaskStatus.IN_PROGRESS
        safe_set_state(_UP, aid, ActionState.IN_PROGRESS, 'working')

    def _park(self, aid=3):
        """The `_attempt > 3` exit on a daemon turn: _ask_for_help holds the
        action (PENDING, BLOCKED input_required) and sets no gate flag."""
        from flask import Flask
        from agent_ledger.core import TaskStatus
        from core.agent_tools import is_help_pause
        self._in_progress(aid)
        with Flask(__name__).app_context(), patch(
                'integrations.agent_engine.dispatch.is_current_request_autonomous',
                return_value=True):
            reply = create_recipe._ask_for_help(
                _UP, 54, aid, 'say ball', 'it did not complete after 3 attempts')
        self.assertTrue(is_help_pause(reply), reply)
        self.assertEqual(self._task(aid).status, TaskStatus.BLOCKED)
        self.assertEqual(self._task(aid).blocked_reason, 'input_required')
        self.assertIsNone(getattr(create_recipe.user_tasks[_UP],
                                  '_needs_user_input_action_id', None))

    def _person_asked(self, aid=3, request='user-request-1', flow=0):
        self._in_progress(aid)
        self.assertTrue(self.L.mark_action_waiting_for_user(
            _UP, aid, 'say ball', flow=flow, asked_in=request))

    def _turn(self, request, text):
        create_recipe.request_id_list[_UP] = request
        return _resume_prior_user_input_block(_UP, text)

    def _flag(self):
        return getattr(create_recipe.user_tasks[_UP], '_needs_user_input_action_id', None)

    def test_a_daemon_turn_after_the_park_takes_no_gate(self):
        self._park()
        self.assertFalse(self._turn('daemon_goal-7', 'tick'))
        self.assertIsNone(self._flag(), 'the parent ran the step; no gate may be set up')

    def test_the_assigned_experts_turn_after_the_park_takes_no_gate(self):
        """#106d: the expert's turn must run the step, not meet a gate whose
        kind it can never clear."""
        self._park()
        with patch('hartos.create_recipe._is_serving_escalation_expert',
                   return_value=True):
            self.assertFalse(self._turn('daemon_expert_turn', 'expert'))
        self.assertIsNone(self._flag())

    def test_a_persons_unrelated_message_does_not_answer_the_park(self):
        from agent_ledger.core import TaskStatus
        self._park()
        self.assertFalse(self._turn('user-request-2', 'hi, how is the weather'))
        self.assertIsNone(self._flag())
        self.assertEqual(self._task(3).status, TaskStatus.BLOCKED)
        self.assertFalse(self.L.ledger_user_answered(_UP, 3, 0))
        self.assertFalse(self._task(3).context.get(self.L.USER_INPUT_EVIDENCE_KEY))

    def test_a_daemon_turns_wait_records_no_ask(self):
        """The verifier hook and state_transition mark a wait on any turn; on
        a daemon turn nobody was asked, so no ask is recorded."""
        self._in_progress(3)
        self.assertTrue(self.L.mark_action_waiting_for_user(
            _UP, 3, 'say ball', flow=0, asked_in='daemon_goal-7'))
        self.assertIsNone(self.L.ledger_open_ask(_UP, 3, 0))
        self.assertFalse(self._turn('user-request-2', 'hi'))
        self.assertIsNone(self._flag())

    def test_after_a_restart_the_persons_ask_is_taken_on_a_persons_turn_only(self):
        from agent_ledger.core import TaskStatus
        self._person_asked(3, 'user-request-1')
        self.assertFalse(self._turn('daemon_goal-7', 'tick'))
        self.assertIsNone(self._flag())
        self.assertEqual(self._task(3).status, TaskStatus.BLOCKED)
        self.assertTrue(self._turn('user-request-2', 'ball'))
        self.assertEqual(self._task(3).status, TaskStatus.IN_PROGRESS)
        self.assertTrue(self.L.ledger_user_answered(_UP, 3, 0))
        self.assertIsNone(self.L.ledger_open_ask(_UP, 3, 0), 'the answer closes the ask')
        self.assertTrue(self._task(3).verify_integrity())

    def test_the_asking_requests_own_retry_is_not_its_answer(self):
        """The 10-19 retry storm re-posts the asking request; REUSE's rule,
        now CREATE's too, on both the derived and the flagged path."""
        self._person_asked(3, 'user-request-2')
        self.assertFalse(self._turn('user-request-2', 'hello?'))
        create_recipe.user_tasks[_UP]._needs_user_input_action_id = 3
        create_recipe.user_tasks[_UP]._needs_user_input_kind = 'human_required'
        self.assertFalse(self._turn('user-request-2', 'hello?'))
        self.assertFalse(self.L.ledger_user_answered(_UP, 3, 0))

    def test_the_reply_goes_to_the_current_action_not_the_lowest(self):
        from agent_ledger.core import TaskStatus
        self._person_asked(2, 'user-request-1')
        self._person_asked(3, 'user-request-1')
        self.assertTrue(self._turn('user-request-2', 'ball'))
        self.assertTrue(self.L.ledger_user_answered(_UP, 3, 0))
        self.assertFalse(self.L.ledger_user_answered(_UP, 2, 0))
        self.assertEqual(self._task(2).status, TaskStatus.BLOCKED)

    def test_a_flow_0_wait_is_not_taken_in_flow_1(self):
        self._person_asked(3, 'user-request-1', flow=0)
        create_recipe.recipe_for_persona[_UP] = 1
        self.assertFalse(self._turn('user-request-2', 'ball'))
        self.assertFalse(self.L.ledger_user_answered(_UP, 3, 0))
        self.assertFalse(self.L.ledger_user_answered(_UP, 3, 1))

    def test_a_failed_answer_save_leaves_the_ask_open(self):
        """The ask closes when the answer is recorded, in the same sealed
        update and save: a failed save leaves both as they were."""
        self._person_asked(3, 'user-request-1')
        with patch.object(self.ledger, 'save', return_value=False):
            self.assertFalse(self.L.record_user_answer(_UP, 3, 'ball', 0))
        self.assertIsNotNone(self.L.ledger_open_ask(_UP, 3, 0))
        self.assertFalse(self.L.ledger_user_answered(_UP, 3, 0))
        self.assertTrue(self.L.record_user_answer(_UP, 3, 'ball', 0))
        self.assertIsNone(self.L.ledger_open_ask(_UP, 3, 0))
        asks = self._task(3).context[self.L.ASK_DELIVERED_KEY]
        self.assertEqual([a['answered'] for a in asks], [True])
        self.assertTrue(self._task(3).verify_integrity())

    def test_an_answer_after_a_give_up_and_a_new_ask_closes_the_new_ask(self):
        """hartos-00's review of 077e779b7, item 1: record_user_answer closed
        the first unanswered entry of the flow, abandoned or not.  After a
        give-up (the ask abandoned), a re-open (GAVE_UP -> ASSIGNED, #139) and
        a fresh ask, the answer marked the OLD entry and left the new one
        open, so a later daemon park plus a person's unrelated chat resumed
        the action with that chat as the answer.  One open-ask rule now."""
        from agent_ledger.core import TaskStatus
        self._person_asked(3, 'user-request-1')
        safe_set_state(_UP, 3, ActionState.GAVE_UP, 'gave up')
        safe_set_state(_UP, 3, ActionState.ASSIGNED, 're-opened')
        self.assertEqual(get_action_state(_UP, 3), ActionState.ASSIGNED)
        self.assertIsNone(self.L.ledger_open_ask(_UP, 3, 0), 'the give-up abandoned it')
        self._person_asked(3, 'user-request-5')
        self.assertEqual(self.L.ledger_open_ask(_UP, 3, 0)['request_id'], 'user-request-5')
        self.assertTrue(self._turn('user-request-6', 'ball'))
        self.assertEqual(self._task(3).status, TaskStatus.IN_PROGRESS)
        self.assertIsNone(self.L.ledger_open_ask(_UP, 3, 0), 'no ask is left open')
        asks = [(a['request_id'], a.get('answered'), bool(a.get('abandoned')))
                for a in self._task(3).context[self.L.ASK_DELIVERED_KEY]]
        self.assertEqual(asks, [('user-request-1', False, True),
                                ('user-request-5', True, False)])
        self.assertTrue(self._task(3).verify_integrity())

    def test_close_open_ask_is_gone_from_hartos_and_nunba(self):
        """The ask closes only inside record_user_answer now; a caller left
        anywhere would import a name that no longer exists.  Tracked files
        only (git grep), both repositories when the Nunba clone is beside
        this one."""
        import os
        import subprocess
        self.assertFalse(hasattr(self.L, 'close_open_ask'))
        here = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
        repos = [here]
        nunba = os.path.join(os.path.dirname(here), 'Nunba-HART-Companion')
        if os.path.isdir(os.path.join(nunba, '.git')):
            repos.append(nunba)
        hits = []
        for repo in repos:
            out = subprocess.run(
                ['git', '-C', repo, 'grep', '-l', '-w', 'close_open_ask', '--', '*.py'],
                capture_output=True, text=True)
            hits += [f'{repo}:{p}' for p in out.stdout.split()
                     if not p.endswith(os.path.basename(__file__))]
        self.assertEqual(hits, [])


class APersonsReplyToAnUnheldQuestionIsItsAnswer(unittest.TestCase):
    """Review of 65db06972, must-fix 3.  Only a recorded answer closes a
    step's question (review of ec41bdbf6, P5), and the gate records only
    the reply to a question it held.  A step that sent a question the gate
    never held (no flag, no ask record) left the person's real reply
    unrecorded, so the lesson after it never counted and the step ended
    GAVE_UP where it used to complete.  The create loop now takes the
    person's reply on their own turn into its gate when the current action's
    window holds the open question, so the one writer records it and a
    refused save keeps it; never a daemon turn's text, the asking request's
    own retry (told by its request id, never its words: review of
    c906283b6, must-fix 2), or a reply meant for a parked step.  And once
    answered, a question the step asks again still waits (must-fix 1).
    Driven through the real create loop resume, the real hooks, a real
    ledger and a registered group chat."""

    _QUESTION = 'Which sound should we practise first?'

    def setUp(self):
        import tempfile
        from agent_ledger.core import SmartLedger, add_actions_to_ledger
        from hartos import lifecycle_hooks as L
        self.L = L
        clear_action_states(_UP)
        self.ledger = SmartLedger(agent_id='54', session_id=f'{_UP}_unheld',
                                  ledger_dir=tempfile.mkdtemp())
        add_actions_to_ledger(self.ledger, [
            {'action': 'say Hello Arjun', 'action_id': 1},
            {'action': 'ask the child which sound to practise', 'action_id': 3},
        ], flow_id=0)
        L.register_ledger_for_session(_UP, self.ledger)
        create_recipe.user_ledgers[_UP] = self.ledger
        create_recipe.user_tasks[_UP] = SimpleNamespace(current_action=3)
        create_recipe.request_id_list[_UP] = 'user-request-2'
        self.gc = SimpleNamespace(messages=[], agents=[])
        L.register_groupchat_for_session(_UP, self.gc)
        from agent_ledger.core import TaskStatus
        self.ledger.tasks['action_3'].status = TaskStatus.IN_PROGRESS
        safe_set_state(_UP, 3, ActionState.IN_PROGRESS, 'working')

    def tearDown(self):
        clear_action_states(_UP)
        create_recipe.initialise_current_flow_to_zero(_UP)
        with self.L._state_lock:
            self.L._ledger_registry.pop(_UP, None)
            self.L._groupchat_registry.pop(_UP, None)
        for store in (create_recipe.user_tasks, create_recipe.request_id_list,
                      create_recipe.user_ledgers):
            try:
                del store[_UP]
            except Exception:
                pass

    @staticmethod
    def _send(call_id, text, asked_in, expects_reply=True):
        """A send_message_to_user call and the receipt CREATE's own writer
        (create_recipe.send_message_to_user1) returns for it in the turn
        ``asked_in``, its publisher stubbed: a change to the receipt's words
        fails these tests (review of 328ad07d0)."""
        import json
        user_id, prompt_id = _UP.rsplit('_', 1)
        before = create_recipe.request_id_list.get(_UP)
        create_recipe.request_id_list[_UP] = asked_in
        try:
            with patch.object(create_recipe, 'is_bundled', return_value=True), \
                    patch('core.peer_link.crossbar_publish.publish_agent_message',
                          return_value=True):
                receipt = create_recipe.send_message_to_user1(
                    user_id, text, '', prompt_id)
        finally:
            if before is None:
                create_recipe.request_id_list.pop(_UP, None)
            else:
                create_recipe.request_id_list[_UP] = before
        call = {'role': 'assistant', 'name': 'Assistant', 'content': '',
                'tool_calls': [{'id': call_id, 'type': 'function', 'function': {
                    'name': 'send_message_to_user',
                    'arguments': json.dumps({'text': text,
                                             'expects_reply': expects_reply})}}]}
        result = {'role': 'tool', 'name': 'Assistant', 'content': receipt,
                  'tool_responses': [{'tool_call_id': call_id, 'role': 'tool',
                                      'content': receipt}]}
        return [call, result]

    def _window(self, *before, expects_reply=True, asked_in='user-request-1'):
        """Action 3's window: its dispatch, then a send it made (asking, or
        a delivery) in the turn ``asked_in``, after any earlier messages
        ``before``."""
        dispatch = {'role': 'assistant', 'name': 'ChatInstructor',
                    'content': 'Execute Action 3: ask the child which sound to practise'}
        self.gc.messages = [*before, dispatch,
                            *self._send('s3', self._QUESTION, asked_in, expects_reply)]

    def _turn(self, request, text, saves=True):
        create_recipe.request_id_list[_UP] = request
        if saves:
            return _resume_prior_user_input_block(_UP, text)
        with patch.object(self.ledger, 'save', return_value=False):
            return _resume_prior_user_input_block(_UP, text)

    def _posted(self, text):
        """The turn's words as a later turn's initiate_chat posts them."""
        self.gc.messages.append({'role': 'user', 'name': 'UserProxy',
                                 'content': create_recipe._answer_for_this_turn(_UP, text)})

    def _answers(self):
        return self.L.recorded_answers(_UP, 3)

    def _flag(self):
        return getattr(create_recipe.user_tasks[_UP], '_needs_user_input_action_id', None)

    def test_the_persons_reply_is_recorded_and_closes_the_question(self):
        self._window()
        self.assertTrue(self._turn('user-request-2', 'the b sound'),
                        'the reply is taken for the step')
        self.assertIsNone(self._flag(), 'and the gate it entered is clear again')
        self.assertEqual(self._answers(), ['the b sound'])
        self.assertTrue(self.L.ledger_user_answered(_UP, 3, 0))
        self.assertTrue(self.ledger.tasks['action_3'].verify_integrity())
        posted = [*self.gc.messages,
                  {'role': 'user', 'name': 'UserProxy', 'content': 'the b sound'},
                  {'role': 'assistant', 'name': 'Assistant',
                   'content': 'Great, the b sound. Say ball with me: b, b, ball.'}]
        self.assertTrue(self.L.window_asked(self.gc.messages, 3, self._answers()))
        self.assertFalse(self.L.window_asked(posted, 3, self._answers()),
                         'the recorded reply closes the question it answers')

    def test_a_daemon_turns_text_is_not_recorded(self):
        self._window()
        self.assertFalse(self._turn('daemon_goal-7', 'Build the agent now'))
        self.assertEqual(self._answers(), [])
        self.assertIsNone(self._flag(), 'a daemon turn takes no gate')

    def test_the_asking_requests_own_retry_is_not_its_answer(self):
        """The 10-19 retry storm re-posts the request that asked, words and
        id: it is not its own answer (the held gate's rule, by the ask
        record; here the receipt names the request).  The same words in a
        later request are the person's answer."""
        before = {'role': 'user', 'name': 'UserProxy', 'content': 'teach Arjun a sound'}
        self._window(before, asked_in='user-request-1')
        self.assertFalse(self._turn('user-request-1', 'teach Arjun a sound'))
        self.assertEqual(self._answers(), [])
        self.assertIsNone(self._flag(), 'a retry takes no gate')
        self.assertTrue(self._turn('user-request-2', 'teach Arjun a sound'))
        self.assertEqual(self._answers(), ['teach Arjun a sound'])

    def test_a_person_who_says_the_same_thing_twice_is_heard(self):
        """Review of c906283b6, must-fix 2: an earlier "yes" (or "ok"), then
        the question, then "yes" again was dropped as a resend, and the step
        ended GAVE_UP where it used to complete."""
        for word in ('yes', 'ok'):
            with self.subTest(word=word):
                self.tearDown()
                self.setUp()
                before = {'role': 'user', 'name': 'UserProxy', 'content': word}
                self._window(before, asked_in='user-request-2')
                self.assertTrue(self._turn('user-request-3', word))
                self.assertEqual(self._answers(), [word])

    def test_a_blank_message_is_no_answer(self):
        self._window()
        for blank in ('', '   ', None):
            with self.subTest(blank=blank):
                self.assertFalse(self._turn('user-request-2', blank))
        self.assertEqual(self._answers(), [])
        self.assertIsNone(self._flag())

    def test_a_reply_the_ledger_cannot_save_is_kept_and_retried_first(self):
        """Review of c906283b6: recorded beside the gate, a refused save
        dropped the reply with a warning, and the step ended GAVE_UP.  In
        the gate it is kept like any answer: the turn says so, and the
        person's next turn records it before their new words."""
        self._window()
        self.assertFalse(self._turn('user-request-2', 'the b sound', saves=False))
        self.assertEqual(self._answers(), [])
        reply = create_recipe._unsaved_answer_reply(_UP)
        self.assertIn("couldn't save your answer to step 3", reply or '')
        self.assertEqual(self._flag(), 3, 'the step waits for the kept answer')
        self.assertEqual(create_recipe.user_tasks[_UP]._needs_user_input_kind,
                         create_recipe.USER_INPUT_GATE_AWAITING_REPLY,
                         'the step asked and waits for the reply')
        self.assertTrue(self._turn('user-request-3', 'what time is it'))
        self.assertEqual(self._answers(), ['the b sound'],
                         'the kept reply is the answer, not the next message')
        self.assertIsNone(create_recipe._unsaved_answer_reply(_UP))
        self.assertEqual(create_recipe._answer_for_this_turn(_UP, 'what time is it'),
                         'the b sound\nwhat time is it')
        self.assertTrue(self.ledger.tasks['action_3'].verify_integrity())

    def test_a_delivery_that_asks_nothing_takes_no_answer(self):
        self._window(expects_reply=False)
        self.assertFalse(self._turn('user-request-2', 'thanks'))
        self.assertEqual(self._answers(), [])

    def test_a_reply_meant_for_a_parked_step_is_not_recorded(self):
        """A park holds the task BLOCKED (input_required) with no ask: the
        person's next message does not answer it (as the ask-record tests
        above pin for the gate)."""
        self._window()
        self.assertTrue(self.L.block_for_user_input(_UP, 3, 'it did not complete'))
        self.assertFalse(self._turn('user-request-2', 'hi, how is the weather'))
        self.assertEqual(self._answers(), [])

    def test_an_answered_question_takes_no_second_answer(self):
        self._window()
        self.assertTrue(self._turn('user-request-2', 'the b sound'))
        self._posted('the b sound')
        self.assertFalse(self._turn('user-request-3', 'and now the m sound'))
        self.assertEqual(self._answers(), ['the b sound'])

    def _still_waits(self, request='user-request-3'):
        """The `completed` + `no` gate as state_transition asks it, on the
        person's turn ``request``."""
        from hartos.threadlocal import thread_local_data
        thread_local_data.set_request_id(request)
        try:
            return create_recipe._completed_verdict_still_waits(_UP, 3, 'no')
        finally:
            thread_local_data.set_request_id(None)

    def test_after_the_answer_the_lesson_completes(self):
        """The commit's named effect, kept: a reply, then the lesson, then
        `completed` + `no` goes to the completion owner, not to the person."""
        self._window()
        self.assertTrue(self._still_waits('user-request-2'), 'unanswered: ask once')
        self.assertTrue(self._turn('user-request-2', 'the b sound'))
        self._posted('the b sound')
        self.gc.messages.append({'role': 'assistant', 'name': 'Assistant',
                                 'content': 'Great, the b sound. Say ball with me.'})
        self.assertFalse(self._still_waits())

    def test_a_question_asked_again_after_the_answer_still_waits(self):
        """Review of c906283b6, must-fix 1: the reply "umm" was recorded, the
        model asked again, and `completed` + `no` closed on the recorded
        answer: GAVE_UP, the new question never put to the person."""
        self._window()
        self.assertTrue(self._turn('user-request-2', 'umm'))
        self._posted('umm')
        self.assertFalse(self._still_waits(), 'answered, nothing asked since')
        self.gc.messages.extend(self._send(
            's4', 'Which sound would you like: b, m or p?', 'user-request-2'))
        self.assertTrue(self._still_waits(), 'the new question waits for its answer')
        self.assertTrue(self._turn('user-request-3', 'the b sound'))
        self._posted('the b sound')
        self.assertFalse(self._still_waits('user-request-4'))
        self.assertEqual(self._answers(), ['umm', 'the b sound'])

    def test_the_held_gates_own_re_ask_still_waits_too(self):
        """The same shape on the gate's held path (its flag and ask record),
        which ended GAVE_UP before this review as well."""
        self._window()
        self.assertTrue(self.L.mark_action_waiting_for_user(
            _UP, 3, 'which sound', flow=0, asked_in='user-request-1'))
        task = create_recipe.user_tasks[_UP]
        task._needs_user_input_action_id = 3
        task._needs_user_input_kind = create_recipe.USER_INPUT_GATE_AWAITING_REPLY
        self.assertTrue(self._turn('user-request-2', 'umm'))
        self._posted('umm')
        self.gc.messages.extend(self._send(
            's4', 'Which sound would you like: b, m or p?', 'user-request-2'))
        self.assertTrue(self._still_waits())

    def test_a_retry_of_the_answering_request_leaves_its_new_question_open(self):
        """Review of 328ad07d0, must-fix 1: "umm" recorded in request 2, the
        model asks again in request 2's round, the round ends without a
        verdict and request 2 is retried.  Its words are posted again, and
        matched on the words alone they closed the new question: the step
        COMPLETED on the model's "Okay, take your time."."""
        self._window()
        self.assertTrue(self._turn('user-request-2', 'umm'))
        self._posted('umm')
        self.gc.messages.extend(self._send(
            's4', 'Which sound would you like: b, m or p?', 'user-request-2'))
        self.assertFalse(self._turn('user-request-2', 'umm'), 'the retry is no answer')
        self._posted('umm')
        self.gc.messages.append({'role': 'assistant', 'name': 'Assistant',
                                 'content': 'Okay, take your time.'})
        self.assertIsNotNone(self.L.step_open_question(_UP, 3),
                             'the new question is still open')
        self.assertTrue(self._still_waits())
        self.assertTrue(self._turn('user-request-3', 'the b sound'))
        self._posted('the b sound')
        self.assertIsNone(self.L.step_open_question(_UP, 3))
        self.assertFalse(self._still_waits('user-request-4'))
        self.assertEqual(self._answers(), ['umm', 'the b sound'])

    def test_an_answer_closes_only_questions_another_request_put_out(self):
        """window_question with answers known by request; a plain string
        (an answer whose request is not known) closes as it always did."""
        self._window()
        person = {'role': 'user', 'name': 'UserProxy', 'content': 'umm'}
        self.gc.messages.append(dict(person))
        self.gc.messages.extend(self._send(
            's4', 'Which sound would you like: b, m or p?', 'user-request-2'))
        self.gc.messages.append(dict(person))
        found = self.L.window_question(self.gc.messages, 3, [('umm', 'user-request-2')])
        self.assertEqual((found or (None, ''))[1], 'Which sound would you like: b, m or p?')
        self.assertIsNone(self.L.window_question(
            self.gc.messages, 3, [('umm', 'user-request-3')]))
        self.assertIsNone(self.L.window_question(self.gc.messages, 3, ['umm']))

    def test_a_refused_turn_retried_records_its_answer_once(self):
        """The same request twice, its words never posted (a turn refused
        after the record): the answer is recorded once."""
        self._window()
        self.assertTrue(self._turn('user-request-2', 'the b sound'))
        self.assertFalse(self._turn('user-request-2', 'the b sound'))
        self.assertEqual(self._answers(), ['the b sound'])
        entries = self.ledger.tasks['action_3'].context[self.L.USER_INPUT_EVIDENCE_KEY]
        self.assertEqual([e.get('request_id') for e in entries], ['user-request-2'])

    def test_an_earlier_orders_question_takes_no_reply(self):
        """Flow 1, before its action 3 runs: the newest send in the chat is
        still flow 0's action-3 question, but the chat's newest order is
        flow 1's action 2, so "hello" answers nothing (review of
        328ad07d0)."""
        create_recipe.recipe_for_persona[_UP] = 1
        self._window()
        self.gc.messages += [
            {'role': 'assistant', 'name': 'ChatInstructor',
             'content': 'Execute Action 1: say Hello Arjun'},
            {'role': 'assistant', 'name': 'Assistant', 'content': 'Hello Arjun!'},
            {'role': 'assistant', 'name': 'ChatInstructor',
             'content': 'Execute Action 2: tell the child what we will do today'},
            {'role': 'assistant', 'name': 'Assistant',
             'content': 'Today we practise one sound.'}]
        self.assertFalse(self._turn('user-request-5', 'hello'))
        self.assertEqual(self._answers(), [])
        self.assertIsNone(self._flag())

    def test_a_reply_met_by_its_own_asks_record_leaves_no_gate(self):
        """An open ask recorded in this very request while nothing holds the
        step: the reply is no answer, and the flag the gate took for it does
        not outlive the turn (review of 328ad07d0)."""
        self._window()
        self.assertTrue(self.L.mark_action_waiting_for_user(
            _UP, 3, 'which sound', flow=0, asked_in='user-request-2'))
        self.assertTrue(self.L.resume_blocked_action(_UP, 3, 'resumed elsewhere'))
        self.assertIsNotNone(self.L.ledger_open_ask(_UP, 3, 0))
        self.assertFalse(self._turn('user-request-2', 'the b sound'))
        self.assertIsNone(self._flag())
        self.assertEqual(self._answers(), [])

    def test_a_daemon_turn_still_never_waits(self):
        self._window()
        self.assertTrue(self._turn('user-request-2', 'umm'))
        self._posted('umm')
        self.gc.messages.extend(self._send(
            's4', 'Which sound would you like: b, m or p?', 'user-request-2'))
        self.assertFalse(self._still_waits('daemon_goal-7'),
                         'nobody reads a daemon turn\'s question')


class AnAnswerTheLedgerCannotSaveIsKeptAndRetried(unittest.TestCase):
    """hartos-00's review of cb13208d2, item 2.  With the answer's save
    failing, cb13208d2 kept the gate but nothing retried the answer: every
    turn ran a full model round, the person got "Step 1 ... is waiting for
    your reply" right after replying, and once storage came back the next
    message ("what time is it") was recorded as the answer.  Now the answer
    is kept on the task and retried at the next person's turn before anything
    else; until it saves, the turn says so and runs no model round."""

    def setUp(self):
        import tempfile
        from agent_ledger.core import SmartLedger, add_actions_to_ledger, TaskStatus
        from hartos import lifecycle_hooks as L
        self.L = L
        clear_action_states(_UP)
        self.ledger = SmartLedger(agent_id='54', session_id=f'{_UP}_unsaved',
                                  ledger_dir=tempfile.mkdtemp())
        add_actions_to_ledger(self.ledger, [
            {'action': 'ask the child to say ball', 'action_id': 3}], flow_id=0)
        L.register_ledger_for_session(_UP, self.ledger)
        self.task = self.ledger.tasks['action_3']
        self.task.status = TaskStatus.IN_PROGRESS
        safe_set_state(_UP, 3, ActionState.IN_PROGRESS, 'working')
        self.assertTrue(L.mark_action_waiting_for_user(
            _UP, 3, 'say ball', flow=0, asked_in='user-request-1'))
        create_recipe.user_tasks[_UP] = SimpleNamespace(
            current_action=3, _needs_user_input_action_id=3,
            _needs_user_input_kind='human_required', _needs_help_reason='say ball')

    def tearDown(self):
        clear_action_states(_UP)
        with self.L._state_lock:
            self.L._ledger_registry.pop(_UP, None)
        for store in (create_recipe.user_tasks, create_recipe.request_id_list,
                      create_recipe.user_agents, create_recipe.messages,
                      create_recipe.recipe_for_persona):
            try:
                del store[_UP]
            except Exception:
                pass

    def _round(self, request, text, saves=True):
        """The WHOLE turn through get_response_group (hartos-00's review of
        ca2e52c91: tests that called the helpers directly skipped the early
        return where the person's words leaked).  Returns ('round', the text
        its model round is given) or ('reply', what it answered without
        one); the round itself is stopped where its text is attached."""
        from flask import Flask
        user_id, prompt_id = _UP.rsplit('_', 1)
        create_recipe.request_id_list[_UP] = request
        create_recipe.user_agents.pop(_UP, None)

        class RoundStarted(Exception):
            pass

        given = []

        def attach(agents_object, round_text, user_prompt):
            given.append(round_text)
            raise RoundStarted()

        agents = tuple(MagicMock() for _ in range(7))
        agents[3].messages = []
        with ExitStack() as stack:
            stack.enter_context(Flask(__name__).app_context())
            stack.enter_context(patch.object(create_recipe, 'create_agents',
                                             return_value=agents))
            stack.enter_context(patch.object(create_recipe, '_attach_for_create_turn',
                                             side_effect=attach))
            if not saves:
                stack.enter_context(patch.object(self.ledger, 'save', return_value=False))
            try:
                reply = create_recipe.get_response_group(user_id, text, prompt_id)
            except RoundStarted:
                return 'round', given[0]
            finally:
                create_recipe.user_agents.pop(_UP, None)
        return 'reply', reply

    def _ask_on_action_4(self, request='user-request-5'):
        """The flow moved on to a NEW ask step (action 4) waiting on this
        person, asked in ``request``."""
        from agent_ledger.core import TaskStatus, add_actions_to_ledger
        add_actions_to_ledger(self.ledger, [
            {'action': 'ask which topic comes next', 'action_id': 4}], flow_id=0)
        self.ledger.tasks['action_4'].status = TaskStatus.IN_PROGRESS
        safe_set_state(_UP, 4, ActionState.IN_PROGRESS, 'working')
        self.assertTrue(self.L.mark_action_waiting_for_user(
            _UP, 4, 'which topic', flow=0, asked_in=request))
        task = create_recipe.user_tasks[_UP]
        task.current_action, task._needs_user_input_action_id = 4, 4
        task._needs_user_input_kind = 'human_required'

    def _answers_4(self):
        return [e['answer'] for e in self.ledger.tasks['action_4'].context.get(
            self.L.USER_INPUT_EVIDENCE_KEY) or []]

    def _turn(self, request, text, saves=True):
        create_recipe.request_id_list[_UP] = request
        if saves:
            return _resume_prior_user_input_block(_UP, text)
        with patch.object(self.ledger, 'save', return_value=False):
            return _resume_prior_user_input_block(_UP, text)

    def _answers(self):
        return [e['answer'] for e in
                self.task.context.get(self.L.USER_INPUT_EVIDENCE_KEY) or []]

    def test_the_kept_answer_is_saved_first_and_the_new_message_follows_it(self):
        self.assertFalse(self._turn('user-request-2', 'ba', saves=False))
        reply = create_recipe._unsaved_answer_reply(_UP)
        self.assertIn("couldn't save your answer to step 3", reply)
        self.assertIn('no need to send it again', reply)
        self.assertFalse(self._turn('user-request-3', 'what time is it', saves=False))
        self.assertTrue(self._turn('user-request-4', 'what time is it'))
        self.assertEqual(self._answers(), ['ba'], 'the next message is not the answer')
        self.assertIsNone(create_recipe._unsaved_answer_reply(_UP))
        self.assertEqual(create_recipe._answer_for_this_turn(_UP, 'what time is it'),
                         'ba\nwhat time is it')
        self.assertEqual(create_recipe._answer_for_this_turn(_UP, 'later'), 'later',
                         'the kept answer is owed to one turn only')
        self.assertIsNone(self.L.ledger_open_ask(_UP, 3, 0))
        self.assertTrue(self.task.verify_integrity())

    def test_a_resend_of_the_kept_answer_is_dropped(self):
        self.assertFalse(self._turn('user-request-2', 'ba', saves=False))
        self.assertTrue(self._turn('user-request-3', 'ba'))
        self.assertEqual(self._answers(), ['ba'])
        self.assertEqual(create_recipe._answer_for_this_turn(_UP, 'ba'), 'ba')

    def test_a_record_that_fails_after_the_resume_saved_is_retried_too(self):
        real_save = self.ledger.save
        calls = {'n': 0}

        def save_once_then_fail(*args, **kwargs):
            calls['n'] += 1
            return real_save(*args, **kwargs) if calls['n'] == 1 else False

        create_recipe.request_id_list[_UP] = 'user-request-2'
        with patch.object(self.ledger, 'save', side_effect=save_once_then_fail):
            self.assertFalse(_resume_prior_user_input_block(_UP, 'ba'))
        self.assertTrue(self._turn('user-request-3', 'what time is it'))
        self.assertEqual(self._answers(), ['ba'])

    def test_a_daemon_turn_neither_retries_nor_gets_the_reply(self):
        """And the kept answer survives the daemon turn (hartos-00's review
        of b599e7f98, Medium 1: with this pin absent, a daemon turn that
        dropped the kept answer passed every test, cb13208d2 item 2's bug)."""
        self.assertFalse(self._turn('user-request-2', 'ba', saves=False))
        self.assertFalse(self._turn('daemon_goal-7', 'tick'))
        self.assertEqual(self._answers(), [])
        self.assertIsNone(create_recipe._unsaved_answer_reply(_UP))
        kept = getattr(create_recipe.user_tasks[_UP], '_unsaved_answer', None)
        self.assertEqual((kept or {}).get('text'), 'ba',
                         'the daemon turn dropped the kept answer')
        self.assertFalse(self._turn('user-request-3', 'ba?', saves=False))
        self.assertIsNotNone(create_recipe._unsaved_answer_reply(_UP),
                             'the next person\'s turn retried it and failed')
        kept = getattr(create_recipe.user_tasks[_UP], '_unsaved_answer', None)
        self.assertEqual((kept or {}).get('text'), 'ba')
        self.assertEqual((kept or {}).get('later'), ['ba?'],
                         'the new words are kept beside the answer, not as it')

    def test_a_kept_answer_is_dropped_once_its_action_no_longer_waits_for_the_person(self):
        """hartos-00's review of 077e779b7, item 3: on a recoverable_stall
        gate the person's answer failed to save and was kept; the assigned
        expert's turn then resumed the action, clearing the gate but not the
        kept answer, and every later person's turn got "I couldn't save your
        answer to step 3 yet..." with no retry and no model round until a
        restart.  The kept answer goes once its action no longer waits for
        the person, and the line is only for a turn whose retry ran and
        failed."""
        create_recipe.user_tasks[_UP]._needs_user_input_kind = 'recoverable_stall'
        self.assertFalse(self._turn('user-request-2', 'ba', saves=False))
        self.assertIsNotNone(create_recipe._unsaved_answer_reply(_UP))
        with patch('hartos.create_recipe._is_serving_escalation_expert',
                   return_value=True):
            self.assertTrue(self._turn('daemon_expert_turn', 'expert'))
        self.assertIsNone(getattr(create_recipe.user_tasks[_UP],
                                  '_needs_user_input_action_id', None))
        for n in (3, 4, 5):
            self._turn(f'user-request-{n}', 'how is it going')
            self.assertIsNone(create_recipe._unsaved_answer_reply(_UP),
                              f'turn {n} was locked out by a stale kept answer')
        self.assertIsNone(getattr(create_recipe.user_tasks[_UP], '_unsaved_answer', None))
        self.assertEqual(self._answers(), [])

    def _expert_resumes(self):
        """'ba' and then 'hello' fail to save; the assigned expert's turn
        then resumes the action, so it no longer waits for the person."""
        create_recipe.user_tasks[_UP]._needs_user_input_kind = 'recoverable_stall'
        self.assertFalse(self._turn('user-request-2', 'ba', saves=False))
        self.assertFalse(self._turn('user-request-3', 'hello', saves=False))
        with patch('hartos.create_recipe._is_serving_escalation_expert',
                   return_value=True):
            self.assertTrue(self._turn('daemon_expert_turn', 'expert'))

    def test_the_person_keeps_their_words_when_the_step_moves_on_without_them(self):
        """hartos-00's review of b599e7f98, Medium 2: the kept answer was
        dropped once the expert resumed the step, so the model got only 'how
        is it going', nothing was recorded, and the person had been told
        there was no need to send it again.  Their words go to their next
        round as context; nothing is recorded as an answer."""
        self._expert_resumes()
        self._turn('user-request-4', 'how is it going')
        self.assertEqual(
            create_recipe._answer_for_this_turn(_UP, 'how is it going'),
            'ba\nhello\nhow is it going')
        self.assertEqual(self._answers(), [])
        self.assertIsNone(getattr(create_recipe.user_tasks[_UP], '_unsaved_answer', None))

    def test_a_daemon_turn_between_never_gets_the_persons_words(self):
        """The hand-over is on the person's own turn only: a daemon tick that
        comes first runs with its own text and leaves the words kept."""
        self._expert_resumes()
        self._turn('daemon_goal-7', 'tick')
        self.assertEqual(create_recipe._answer_for_this_turn(_UP, 'tick'), 'tick')
        self._turn('user-request-4', 'how is it going')
        self.assertEqual(
            create_recipe._answer_for_this_turn(_UP, 'how is it going'),
            'ba\nhello\nhow is it going')

    def test_the_kept_words_are_never_the_answer_to_a_new_ask(self):
        """hartos-00's case: the flow moved on to a NEW ask step waiting on
        this person.  The old 'ba' (the answer to "say ball") and 'hello' are
        context for the model only; the new ask's answer is this turn's own
        message, never the old answer to a different question."""
        self._expert_resumes()
        self._ask_on_action_4()
        self.assertTrue(self._turn('user-request-6', 'algebra'))
        self.assertEqual(self._answers_4(), ['algebra'])
        self.assertEqual(self._answers(), [], "'ba' was never recorded for action 3")
        self.assertEqual(create_recipe._answer_for_this_turn(_UP, 'algebra'),
                         'ba\nhello\nalgebra')

    def test_what_waits_beside_a_kept_answer_is_capped_newest_kept(self):
        """Minor 3: `later` grew without bound (61 failed turns, one 62-line
        message, a 12k context at risk).  It keeps the newest ten."""
        self.assertFalse(self._turn('user-request-2', 'ba', saves=False))
        for n in range(3, 64):
            self._turn(f'user-request-{n}', f'm{n}', saves=False)
        kept = create_recipe.user_tasks[_UP]._unsaved_answer
        self.assertEqual(kept['text'], 'ba')
        self.assertEqual(kept['later'], [f'm{n}' for n in range(54, 64)])

    def test_a_resend_differing_only_in_spaces_is_a_resend(self):
        """Minor 4: 'ba ' after 'ba' was kept beside it.  Repeats compare
        stripped; case is kept ('Ba' is the person's own word)."""
        self.assertFalse(self._turn('user-request-2', 'ba', saves=False))
        self.assertFalse(self._turn('user-request-3', 'ba ', saves=False))
        self.assertEqual(create_recipe.user_tasks[_UP]._unsaved_answer['later'], [])
        self.assertFalse(self._turn('user-request-4', 'Ba', saves=False))
        self.assertEqual(create_recipe.user_tasks[_UP]._unsaved_answer['later'], ['Ba'])
        self.assertTrue(self._turn('user-request-5', ' Ba'))
        self.assertEqual(create_recipe._answer_for_this_turn(_UP, ' Ba'), 'ba\nBa')

    def test_the_line_is_only_for_the_turn_whose_retry_failed(self):
        """Item 3's second half: a person's turn that tried nothing gets no
        "couldn't save" line.  The asking request re-posted (the 10-19 retry
        storm) is not the answer, retries nothing, and runs as usual; the
        answer stays kept for the next real turn."""
        self.assertFalse(self._turn('user-request-2', 'ba', saves=False))
        self.assertIsNotNone(create_recipe._unsaved_answer_reply(_UP))
        self.assertFalse(self._turn('user-request-1', 'say ball'))
        self.assertIsNone(create_recipe._unsaved_answer_reply(_UP))
        self.assertIsNotNone(getattr(create_recipe.user_tasks[_UP], '_unsaved_answer', None))
        self.assertTrue(self._turn('user-request-3', 'ok'))
        self.assertEqual(self._answers(), ['ba'])

    def test_words_sent_while_the_retry_fails_are_kept_with_the_answer(self):
        """Item 4: "ba" failed, the next message's retry failed too, and the
        kept text stayed "ba", so once the save worked the model saw
        'ba\\nok' and the middle message was gone without a word."""
        self.assertFalse(self._turn('user-request-2', 'ba', saves=False))
        self.assertFalse(self._turn('user-request-3', 'can you hear me', saves=False))
        self.assertTrue(self._turn('user-request-4', 'ok'))
        self.assertEqual(self._answers(), ['ba'], 'only the answer is recorded as the answer')
        self.assertEqual(create_recipe._answer_for_this_turn(_UP, 'ok'),
                         'ba\ncan you hear me\nok')

    def test_the_third_failure_is_an_error(self):
        with self.assertLogs('hartos.create_recipe', level='ERROR') as logs:
            for n in (2, 3, 4):
                self.assertFalse(self._turn(f'user-request-{n}', 'ba', saves=False))
        self.assertTrue(any('3 times' in line for line in logs.output), logs.output)

    def test_the_turn_answers_honestly_and_runs_no_model_round(self):
        from flask import Flask
        user_id, prompt_id = _UP.rsplit('_', 1)
        create_recipe.request_id_list[_UP] = 'user-request-2'
        create_recipe.user_agents.pop(_UP, None)
        with Flask(__name__).app_context(), \
                patch.object(self.ledger, 'save', return_value=False), \
                patch.object(create_recipe, 'create_agents',
                             side_effect=AssertionError('a model round ran')) as made:
            reply = create_recipe.get_response_group(user_id, 'ba', prompt_id)
        made.assert_not_called()
        self.assertIn("couldn't save your answer to step 3", reply)

    def test_a_daemon_tick_never_gets_the_persons_words_in_a_whole_turn(self):
        """hartos-00's review of ca2e52c91, finding 1 (critical): the person's
        turn handed 'ba' and 'hello' over, then its save of the NEW ask's
        answer failed and it returned before its round; the next daemon
        tick's round got 'ba\\nhello\\ntick'."""
        self._expert_resumes()
        self._ask_on_action_4()
        kind, said = self._round('user-request-6', 'algebra', saves=False)
        self.assertEqual(kind, 'reply')
        self.assertIn("couldn't save your answer to step 4", said)
        self.assertEqual(self._round('daemon_goal-7', 'tick'), ('round', 'tick'))

    def test_the_persons_next_round_gets_every_word_in_the_order_sent(self):
        """Finding 2: the retry that saved ASSIGNED the owed words, so 'ba'
        and 'hello' were overwritten and the model got 'algebra\\ndid you get
        that' after the person had been told not to resend.  Every word, in
        order; only 'algebra' is recorded, and for action 4."""
        self._expert_resumes()
        self._ask_on_action_4()
        self._round('user-request-6', 'algebra', saves=False)
        self._round('daemon_goal-7', 'tick')
        self.assertEqual(self._round('user-request-7', 'did you get that'),
                         ('round', 'ba\nhello\nalgebra\ndid you get that'))
        self.assertEqual(self._answers_4(), ['algebra'])
        self.assertEqual(self._answers(), [])

    def test_a_kept_answer_from_another_flow_never_answers_this_one(self):
        """Finding 3: the drop compared only the action id.  'ba' was kept
        for flow 0's action 3, the expert resumed it, the flow moved to 1 and
        action 3 was flagged again there; the person's 'cat' is flow 1's
        answer, and 'ba' is never recorded against flow 0."""
        from agent_ledger.core import TaskStatus
        self._expert_resumes()
        create_recipe.recipe_for_persona[_UP] = 1
        self.task.status = TaskStatus.IN_PROGRESS
        self.assertTrue(self.L.mark_action_waiting_for_user(
            _UP, 3, 'say cat', flow=1, asked_in='user-request-5'))
        task = create_recipe.user_tasks[_UP]
        task._needs_user_input_action_id = 3
        task._needs_user_input_kind = 'human_required'
        self.assertEqual(self._round('user-request-6', 'cat'),
                         ('round', 'ba\nhello\ncat'))
        self.assertTrue(self.L.ledger_user_answered(_UP, 3, 1))
        self.assertFalse(self.L.ledger_user_answered(_UP, 3, 0))
        self.assertEqual(self._answers(), ['cat'])

    def test_owed_words_are_only_ever_the_persons(self):
        """The guard behind finding 1, on its own: whatever is owed, a daemon
        turn's round gets its own text and leaves the words; the person's
        next round gets them."""
        create_recipe.user_tasks[_UP]._answer_owed_to_turn = ['ba']
        create_recipe.request_id_list[_UP] = 'daemon_goal-7'
        self.assertEqual(create_recipe._answer_for_this_turn(_UP, 'tick'), 'tick')
        create_recipe.request_id_list[_UP] = 'user-request-9'
        self.assertEqual(create_recipe._answer_for_this_turn(_UP, 'hi'), 'ba\nhi')

    def test_a_hand_over_adds_to_what_is_owed(self):
        """Finding 2's rule at the drop too: words already owed are kept and
        the dropped answer's words follow them, never written over them."""
        self._expert_resumes()
        create_recipe.user_tasks[_UP]._answer_owed_to_turn = ['earlier']
        self._turn('user-request-4', 'how is it going')
        self.assertEqual(
            create_recipe._answer_for_this_turn(_UP, 'how is it going'),
            'earlier\nba\nhello\nhow is it going')

    def test_the_line_promises_only_what_happens(self):
        """Finding 4: the line promised the answer would be saved, and after
        a hand-over it never is; it is passed on.  True both ways now."""
        self.assertFalse(self._turn('user-request-2', 'ba', saves=False))
        line = create_recipe._unsaved_answer_reply(_UP)
        self.assertNotIn('save it before anything else', line)
        self.assertIn('pass it on with your next message', line)
        self.assertIn('no need to send it again', line)


if __name__ == '__main__':
    unittest.main()
