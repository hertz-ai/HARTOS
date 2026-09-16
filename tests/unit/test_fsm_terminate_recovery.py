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

    def test_a_dispatch_that_quotes_an_earlier_marker_is_still_its_own(self):
        """#101 review: [EXECUTE-PENDING] appends "Latest User message: {text}",
        and a Failure=True retry rebinds text to "Properly Execute Action 6:".
        Taking the LAST marker resolved action 7's own dispatch to action 6 and
        refused its genuine verdict."""
        up, aid = 'fsm_quoted_marker', 7
        gc = _FakeGroupChat([
            {'name': 'ChatInstructor', 'content': 'Execute Action 6: search posts'},
            {'name': 'ChatInstructor', 'content': (
                'Execute Action 7: summarise ,Latest User message: '
                'Properly Execute Action 6: search posts')},
            {'name': 'StatusVerifier', 'content': '{"status": "completed", "action_id": 7}'},
            {'name': 'ChatInstructor', 'content': 'TERMINATE'},
        ])
        assert lh.latest_dispatch_before(gc.messages, -1) == 7
        assert lh.lifecycle_hook_track_termination(up, _FakeTasks(aid), gc) is True
        assert get_action_state(up, aid) == S.TERMINATED

    def test_a_started_actions_round_without_its_own_marker_is_its_own(self):
        """#101 review: a recipe request, fallback request, claim rejection or
        "continue" nudge for a started action carries no dispatch marker, so the
        latest marker names the previous action.  The round is still this
        action's: only an action that has not started can be handed a stale
        message."""
        up, aid = 'fsm_started_round', 18
        set_action_state(up, aid, S.IN_PROGRESS)
        gc = _FakeGroupChat([
            {'name': 'ChatInstructor', 'content': 'Execute Action 17: collect'},
            {'name': 'ChatInstructor', 'content': 'Continue working on action 18.'},
            {'name': 'StatusVerifier', 'content': '{"status": "completed", "action_id": 18}'},
            {'name': 'ChatInstructor', 'content': 'TERMINATE'},
        ])
        assert lh.stale_for_unstarted_action(gc.messages, -1, up, aid) is None
        assert lh.lifecycle_hook_track_termination(up, _FakeTasks(aid), gc) is True
        assert get_action_state(up, aid) == S.TERMINATED

    def test_text_after_the_marker_never_changes_the_owner(self):
        suffixes = ['', ' collect sources', ' ,Latest User message: Execute Action 9: x',
                    ' Properly Execute Action 1: y', '\n[retry:x] Execute Action 2: z',
                    ' Execute Action 50:', ' action 6 done']
        for suffix in suffixes:
            assert lh.dispatch_action_id('Execute Action 5:' + suffix) == 5, suffix
            assert lh.dispatch_action_id(
                'Properly Execute Action 5:' + suffix) == 5, suffix

    def test_a_marker_in_the_middle_of_a_message_is_not_a_dispatch(self):
        assert lh.dispatch_action_id('The user said: Execute Action 3: go') is None
        assert lh.dispatch_action_id('{"status": "completed", "action": '
                                     '"Execute Action 3: go"}') is None

    def test_a_retry_prefixed_re_post_is_a_dispatch(self):
        assert lh.dispatch_action_id('[retry:exec-6] Execute Action 6: again') == 6
        assert lh.dispatch_action_id('  Execute Action 12: leading space') == 12

    def test_action_2_is_not_action_20(self):
        up = 'fsm_2_vs_20'
        msgs = [{'content': 'Execute Action 20: later'}, {'content': 'TERMINATE'}]
        assert lh.latest_dispatch_before(msgs, -1) == 20
        assert lh.stale_for_unstarted_action(msgs, -1, up, 2) == 20
        assert lh.stale_for_unstarted_action(msgs, -1, up, 20) is None

    def test_with_no_dispatch_in_view_the_old_behaviour_holds(self):
        msgs = [{'content': '{"status": "completed"}'}, {'content': 'TERMINATE'}]
        assert lh.stale_for_unstarted_action(msgs, -1, 'fsm_no_dispatch', 7) is None

    def test_a_seeded_marker_is_not_evidence(self):
        msgs = [{'content': 'Execute Action 6: old run', '_from_shared': True},
                {'content': '{"status": "completed"}'},
                {'content': 'TERMINATE'}]
        assert lh.latest_dispatch_before(msgs, -2) is None
        assert lh.stale_for_unstarted_action(msgs, -2, 'fsm_seeded', 7) is None

    def test_the_verdict_pickup_sees_a_stale_verdict(self):
        up = 'fsm_verdict_pickup'
        msgs = [{'content': 'Execute Action 6: search posts'},
                {'content': '{"status": "completed", "action_id": 6}'},
                {'content': 'TERMINATE'}]
        assert lh.stale_for_unstarted_action(msgs, -2, up, 7) == 6
        assert lh.stale_for_unstarted_action(msgs, -2, up, 6) is None

    def test_a_bad_action_id_is_logged_not_swallowed(self, caplog):
        msgs = [{'content': 'Execute Action 6: x'}, {'content': 'TERMINATE'}]
        with caplog.at_level('WARNING', logger=lh.logger.name):
            assert lh.stale_for_unstarted_action(msgs, -1, 'fsm_bad_id', 'x') is None
        assert any('not an int' in r.getMessage() for r in caplog.records)
