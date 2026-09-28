"""A user's reply resumes the waiting action in BOTH lifecycle stores.

mark_action_waiting_for_user blocks the ledger task and sets the ActionState
to PENDING.  When the user answers, create_recipe._resume_prior_user_input_block
calls resume_from_user_input, which takes the ledger BLOCKED -> IN_PROGRESS,
and the loop's [EXECUTE-PENDING] dispatch then asks for ActionState
IN_PROGRESS.  The transition table gave PENDING only
{COMPLETED, ERROR, PENDING}, so that request was refused and its False return
ignored: the action ran while ActionState still said PENDING and the ledger
said IN_PROGRESS.  Measured in the Nunba log (gui_app.log.1, 21:23:20,532-533):
"[EXECUTE-PENDING] Starting action 1 ... Latest User message: Yes, proceed"
then "Invalid transition: Action 1 cannot go from pending to in_progress".

These tests drive the real lifecycle_hooks against a real SmartLedger; only
the recipe-experience timer (a telemetry boundary) is observed through a mock.
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..',
                                'agent-ledger-opensource'))

from agent_ledger.core import SmartLedger, Task, TaskStatus, TaskType  # noqa: E402
import hartos.lifecycle_hooks as L  # noqa: E402

S = L.ActionState


@pytest.fixture
def session(tmp_path):
    up = 'u_reply_p_reply'
    led = SmartLedger(agent_id='p_reply', session_id='u_reply_p_reply_1',
                      ledger_dir=str(tmp_path))
    led.tasks['action_1'] = Task(task_id='action_1', description='a1',
                                 task_type=TaskType.PRE_ASSIGNED)
    L.register_ledger_for_session(up, led)
    with L._state_lock:
        L.action_states.pop(up, None)
    yield up, led
    with L._state_lock:
        L.action_states.pop(up, None)
        L._ledger_registry.pop(up, None)


def test_execute_pending_after_a_user_reply_starts_the_action(session):
    up, led = session
    assert L.safe_set_state(up, 1, S.IN_PROGRESS, 'start') is True
    assert L.mark_action_waiting_for_user(up, 1, 'need a choice') is True
    assert L.get_action_state(up, 1) == S.PENDING
    assert led.tasks['action_1'].status == TaskStatus.BLOCKED

    assert L.resume_from_user_input(up, 1, 'user replied', 'Yes, proceed')
    assert led.tasks['action_1'].status == TaskStatus.IN_PROGRESS

    # The call create_recipe's [EXECUTE-PENDING] makes, verbatim.
    with patch('hartos.recipe_experience.RecipeExperienceRecorder'
               '.start_action_timer') as timer:
        ok = L.safe_set_state(up, 1, S.IN_PROGRESS,
                              'executing pending action')
    assert ok is True, 'the resumed action was refused IN_PROGRESS'
    assert L.get_action_state(up, 1) == S.IN_PROGRESS
    assert led.tasks['action_1'].status == TaskStatus.IN_PROGRESS
    timer.assert_called_once_with(up, 1)
    # And the resumed action can be verified again.
    assert L.safe_set_state(up, 1, S.STATUS_VERIFICATION_REQUESTED,
                            'verify') is True


def test_a_verifier_pending_action_re_executes_and_unblocks_the_ledger(session):
    """The other PENDING producer: a 'pending' verdict with no user needed.
    Re-executing it must move both stores back into active work."""
    up, led = session
    assert L.safe_set_state(up, 1, S.IN_PROGRESS, 'start') is True
    assert L.safe_set_state(up, 1, S.STATUS_VERIFICATION_REQUESTED, 'v')
    assert L.safe_set_state(up, 1, S.PENDING, 'verifier: pending') is True
    assert led.tasks['action_1'].status == TaskStatus.BLOCKED

    assert L.safe_set_state(up, 1, S.IN_PROGRESS,
                            'executing pending action') is True
    assert L.get_action_state(up, 1) == S.IN_PROGRESS
    assert led.tasks['action_1'].status == TaskStatus.IN_PROGRESS
    assert led.tasks['action_1'].blocked_reason is None


def test_pending_still_refuses_the_moves_it_refused_before(session):
    """Only the resume edge is new: PENDING still cannot jump to a
    post-completion state or be re-assigned."""
    up, _led = session
    L.safe_set_state(up, 1, S.IN_PROGRESS, 'start')
    L.safe_set_state(up, 1, S.PENDING, 'p')
    for refused in (S.ASSIGNED, S.STATUS_VERIFICATION_REQUESTED,
                    S.RECIPE_REQUESTED, S.TERMINATED):
        assert L.validate_state_transition(up, 1, refused) is False, refused
