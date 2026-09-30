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
                   return_value=True) as resume:
            self.assertTrue(
                _resume_prior_user_input_block(_UP, 'Use account A'))
        resume.assert_called_once_with(
            _UP, 3, 'User supplied input for the blocked action',
            'Use account A')
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


if __name__ == '__main__':
    unittest.main()
