"""force_state_through_valid_path must reach TERMINATED from ANY pre-terminal
state. TERMINATED is the absorbing terminal — terminating an action can never
be a dead end.

The live 2026-06-13 flywheel stall (the genuine "goals never complete
autonomously" blocker): an action still ASSIGNED / IN_PROGRESS / PENDING /
awaiting-verification when the flow tried to complete (create_recipe.py ~4587,
the flow-complete force-terminate) could NOT be driven to TERMINATED —
``state_paths`` only had routes from COMPLETED / RECIPE_RECEIVED. So the force
failed ('Invalid transition: assigned -> terminated', 187x/boot on the live
build), ``lifecycle_hook_can_increment_action`` blocked on the non-TERMINATED
action, the pipeline re-ran the same action forever, and no goal ever reached
recipe-save -> the flywheel never spun + the CPU churned.

Safety vs #139 (force-complete masking a genuine failure): walking through
COMPLETED is guarded downstream — an action that actually ran tools banks a
real recipe via trace-derived banking (#143); one that did nothing banks a
placebo that the placebo-rejection (#140) drops. Loop-breaking (liveness) is
the concern here; recipe quality is a separate, already-gated concern.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from hartos import lifecycle_hooks as lh  # noqa: E402
from hartos.lifecycle_hooks import (  # noqa: E402
    ActionState as S, force_state_through_valid_path, set_action_state,
    get_action_state)


def _walk(up, aid, *states):
    """Drive an action through valid direct transitions to set up a state."""
    for s in states:
        set_action_state(up, aid, s)


class TestForceTerminateFromAnyState:
    def test_assigned_reaches_terminated(self):
        up = 'fsm_rec_assigned'
        assert get_action_state(up, 1) == S.ASSIGNED  # default for a fresh action
        assert force_state_through_valid_path(up, 1, S.TERMINATED, 'flow complete') is True
        assert get_action_state(up, 1) == S.TERMINATED

    def test_in_progress_reaches_terminated(self):
        up = 'fsm_rec_inprog'
        set_action_state(up, 1, S.IN_PROGRESS)
        assert force_state_through_valid_path(up, 1, S.TERMINATED, 'flow complete') is True
        assert get_action_state(up, 1) == S.TERMINATED

    def test_pending_reaches_terminated(self):
        up = 'fsm_rec_pending'
        _walk(up, 1, S.IN_PROGRESS, S.PENDING)
        assert force_state_through_valid_path(up, 1, S.TERMINATED, 'flow complete') is True
        assert get_action_state(up, 1) == S.TERMINATED

    def test_status_verification_reaches_terminated(self):
        up = 'fsm_rec_sv'
        _walk(up, 1, S.IN_PROGRESS, S.STATUS_VERIFICATION_REQUESTED)
        assert force_state_through_valid_path(up, 1, S.TERMINATED, 'flow complete') is True
        assert get_action_state(up, 1) == S.TERMINATED

    def test_completed_still_reaches_terminated(self):
        # Regression: the pre-existing COMPLETED -> TERMINATED route must survive.
        up = 'fsm_rec_completed'
        _walk(up, 1, S.IN_PROGRESS, S.STATUS_VERIFICATION_REQUESTED, S.COMPLETED)
        assert force_state_through_valid_path(up, 1, S.TERMINATED, 'normal') is True
        assert get_action_state(up, 1) == S.TERMINATED

    def test_already_terminated_is_idempotent_true(self):
        up = 'fsm_rec_term'
        _walk(up, 1, S.IN_PROGRESS, S.STATUS_VERIFICATION_REQUESTED, S.COMPLETED, S.TERMINATED)
        assert get_action_state(up, 1) == S.TERMINATED
        assert force_state_through_valid_path(up, 1, S.TERMINATED, 'again') is True
        assert get_action_state(up, 1) == S.TERMINATED


class _FakeTasks:
    def __init__(self, current_action):
        self.current_action = current_action


class _FakeGroupChat:
    def __init__(self, messages):
        self.messages = messages


class TestTerminationHookEscapesStuckAction:
    """lifecycle_hook_track_termination must terminate a TERMINATE'd action even
    if it never left ASSIGNED — otherwise the hook returns False, the action
    stays non-terminal, can_increment blocks, and the pipeline re-runs it forever
    (the live loop). It must use the force-to-terminal recovery path, not a bare
    validate that rejects ASSIGNED -> TERMINATED."""

    def test_terminate_message_escapes_stuck_assigned(self):
        up, aid = 'fsm_hook_assigned', 3
        assert get_action_state(up, aid) == S.ASSIGNED  # 4B never drove IN_PROGRESS
        gc = _FakeGroupChat([{'name': 'ChatInstructor', 'content': 'TERMINATE'}])
        ok = lh.lifecycle_hook_track_termination(up, _FakeTasks(aid), gc)
        assert ok is True
        assert get_action_state(up, aid) == S.TERMINATED

    def test_no_terminate_message_is_noop(self):
        # Guard: the hook must only act on an actual TERMINATE, not any message.
        up, aid = 'fsm_hook_noterm', 4
        gc = _FakeGroupChat([{'name': 'ChatInstructor', 'content': 'Action 4 working'}])
        ok = lh.lifecycle_hook_track_termination(up, _FakeTasks(aid), gc)
        assert ok is False
        assert get_action_state(up, aid) == S.ASSIGNED  # untouched


class TestAStaleTerminateIsNotTheCurrentActions:
    """#101, central 2026-09-13: after [ADVANCE] 6->7 the previous action's
    verdict and TERMINATE stay the last messages.  The hook terminated action 7
    on that old TERMINATE before 7 ran, and the create loop then asked the
    model for a recipe of work that never happened."""

    def test_the_previous_actions_terminate_leaves_the_new_action_alone(self):
        up, aid = 'fsm_stale_term', 7
        gc = _FakeGroupChat([
            {'name': 'ChatInstructor', 'content': 'Execute Action 6: search posts'},
            {'name': 'StatusVerifier', 'content': '{"status": "completed", "action_id": 6}'},
            {'name': 'ChatInstructor', 'content': 'TERMINATE'},
        ])
        assert lh.lifecycle_hook_track_termination(up, _FakeTasks(aid), gc) is False
        assert get_action_state(up, aid) == S.ASSIGNED

    def test_its_own_terminate_still_terminates_it(self):
        up, aid = 'fsm_own_term', 7
        gc = _FakeGroupChat([
            {'name': 'ChatInstructor', 'content': 'Execute Action 6: search posts'},
            {'name': 'ChatInstructor', 'content': 'Execute Action 7: summarise'},
            {'name': 'StatusVerifier', 'content': '{"status": "completed", "action_id": 7}'},
            {'name': 'ChatInstructor', 'content': 'TERMINATE'},
        ])
        assert lh.lifecycle_hook_track_termination(up, _FakeTasks(aid), gc) is True
        assert get_action_state(up, aid) == S.TERMINATED

    def test_a_re_posted_dispatch_still_owns_its_verdict(self):
        """The verifier mislabels about a third of genuine verdicts; one for
        action 6 that says action_id=1 after a re-post is still action 6's."""
        up, aid = 'fsm_repost_term', 6
        gc = _FakeGroupChat([
            {'name': 'ChatInstructor', 'content': 'Execute Action 6: search posts'},
            {'name': 'Assistant', 'content': 'searching'},
            {'name': 'ChatInstructor', 'content': 'Properly Execute Action 6: continue'},
            {'name': 'StatusVerifier', 'content': '{"status": "completed", "action_id": 1}'},
            {'name': 'ChatInstructor', 'content': 'TERMINATE'},
        ])
        assert lh.lifecycle_hook_track_termination(up, _FakeTasks(aid), gc) is True
        assert get_action_state(up, aid) == S.TERMINATED

    def test_action_2_is_not_action_20(self):
        msgs = [{'content': 'Execute Action 20: later'}, {'content': 'TERMINATE'}]
        assert lh.belongs_to_other_action(msgs, -1, 2) is True
        assert lh.belongs_to_other_action(msgs, -1, 20) is False

    def test_with_no_dispatch_in_view_the_old_behaviour_holds(self):
        msgs = [{'content': '{"status": "completed"}'}, {'content': 'TERMINATE'}]
        assert lh.belongs_to_other_action(msgs, -1, 7) is False

    def test_a_seeded_marker_is_not_evidence(self):
        msgs = [{'content': 'Execute Action 6: old run', '_from_shared': True},
                {'content': '{"status": "completed"}'},
                {'content': 'TERMINATE'}]
        assert lh.belongs_to_other_action(msgs, -2, 7) is False

    def test_the_verdict_pickup_sees_a_stale_verdict(self):
        msgs = [{'content': 'Execute Action 6: search posts'},
                {'content': '{"status": "completed", "action_id": 6}'},
                {'content': 'TERMINATE'}]
        assert lh.belongs_to_other_action(msgs, -2, 7) is True
        assert lh.belongs_to_other_action(msgs, -2, 6) is False
