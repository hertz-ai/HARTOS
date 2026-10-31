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
from types import SimpleNamespace
from unittest.mock import patch

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
            _UP, 3, 'Use account A', create_recipe.get_current_flow(_UP))
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
        for store in (create_recipe.user_tasks, create_recipe.request_id_list):
            try:
                del store[_UP]
            except Exception:
                pass

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
        self.assertFalse(self._turn('user-request-2', 'ba', saves=False))
        self.assertFalse(self._turn('daemon_goal-7', 'tick'))
        self.assertEqual(self._answers(), [])
        self.assertIsNone(create_recipe._unsaved_answer_reply(_UP))
        create_recipe.request_id_list[_UP] = 'user-request-3'
        self.assertIsNotNone(create_recipe._unsaved_answer_reply(_UP),
                             'the kept answer is still waiting for a person\'s turn')

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


if __name__ == '__main__':
    unittest.main()
