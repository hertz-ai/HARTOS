"""#56 — break the dual-FSM STUCK LOOP at its ActionState root.

Recipe-gen pushed an already-TERMINATED action toward RECIPE_REQUESTED, which the
ActionState FSM had no edge for (TERMINATED→[ASSIGNED] only) → find_path failed →
autogen re-ran the same action (the STUCK LOOP).  RECIPE_REQUESTED→RECIPE_RECEIVED
→TERMINATED already exists, so adding TERMINATED→RECIPE_REQUESTED just lets the
capture flow complete.  (The ledger-side divergence is handled separately by the
advisory-sync terminal skip; the single-FSM unification is the deeper cleanup.)
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_terminated_recipe_capture_edge():
    try:
        import lifecycle_hooks as L
    except Exception as e:
        pytest.skip(f"lifecycle_hooks unavailable: {e}")

    up, aid = 'u56_777', 1
    with L._state_lock:
        L.action_states.setdefault(up, {})[aid] = L.ActionState.TERMINATED
    try:
        # The STUCK-LOOP fix: a TERMINATED action may now enter recipe-capture.
        assert L.validate_state_transition(up, aid, L.ActionState.RECIPE_REQUESTED) is True
        # The existing re-open edge still works.
        assert L.validate_state_transition(up, aid, L.ActionState.ASSIGNED) is True
        # TERMINATED stays a near-sink: no jump straight back into execution.
        assert L.validate_state_transition(up, aid, L.ActionState.IN_PROGRESS) is False
        assert L.validate_state_transition(up, aid, L.ActionState.COMPLETED) is False
    finally:
        with L._state_lock:
            L.action_states.pop(up, None)


def test_recipe_capture_cycle_from_terminated_is_complete():
    """The capture flow a terminated action can now follow must be a closed,
    terminating cycle (no path back into open execution that could re-loop)."""
    try:
        import lifecycle_hooks as L
    except Exception as e:
        pytest.skip(f"lifecycle_hooks unavailable: {e}")
    up, aid = 'u56_778', 2

    def _valid_from(state, target):
        with L._state_lock:
            L.action_states.setdefault(up, {})[aid] = state
        return L.validate_state_transition(up, aid, target)

    try:
        assert _valid_from(L.ActionState.TERMINATED, L.ActionState.RECIPE_REQUESTED)
        assert _valid_from(L.ActionState.RECIPE_REQUESTED, L.ActionState.RECIPE_RECEIVED)
        assert _valid_from(L.ActionState.RECIPE_RECEIVED, L.ActionState.TERMINATED)
    finally:
        with L._state_lock:
            L.action_states.pop(up, None)


def test_recipe_requested_has_recovery_edges():
    """#128 — RECIPE_REQUESTED was a near-trap: only →RECIPE_RECEIVED (happy path)
    or a self-loop, with NO recovery edge.  When the local model fails to emit a
    recipe (the common case for a 4B), the pipeline drives the action out of
    recipe_requested two ways, and the FSM rejected BOTH:

      * →FALLBACK_REQUESTED — the autonomous-fallback pattern: the verifier hook
        returns force_fallback (create_recipe.py:4066) → safe_set_state(...,
        FALLBACK_REQUESTED).  Rejected → the task's fallback flag flipped True
        while the FSM stayed recipe_requested (the dual-FSM divergence of #56).
      * →TERMINATED — the TERMINATE handler (lifecycle_hooks.py:1018) gates on
        validate_state_transition(..., TERMINATED).  Rejected → the action could
        never terminate.

    With neither edge, the action sat in recipe_requested until the stall-guard
    broke the flow ("stuck in recipe_requested ... with no recipe") — that's the
    live ~9% goal-completion rate (9 987 'Invalid transition: recipe_requested →
    ...' in one window).  The recovery edges mirror what COMPLETED and ERROR
    already have; the fix stays targeted (no jump back into open execution).
    """
    try:
        import lifecycle_hooks as L
    except Exception as e:
        pytest.skip(f"lifecycle_hooks unavailable: {e}")

    up, aid = 'u128_999', 1

    def _valid_from(state, target):
        with L._state_lock:
            L.action_states.setdefault(up, {})[aid] = state
        return L.validate_state_transition(up, aid, target)

    try:
        # the recovery edges that were missing (the live stuck-state bug)
        assert _valid_from(L.ActionState.RECIPE_REQUESTED, L.ActionState.FALLBACK_REQUESTED) is True
        assert _valid_from(L.ActionState.RECIPE_REQUESTED, L.ActionState.TERMINATED) is True
        assert _valid_from(L.ActionState.RECIPE_REQUESTED, L.ActionState.ERROR) is True
        # happy path + idempotent self-loop must still hold (no regression)
        assert _valid_from(L.ActionState.RECIPE_REQUESTED, L.ActionState.RECIPE_RECEIVED) is True
        assert _valid_from(L.ActionState.RECIPE_REQUESTED, L.ActionState.RECIPE_REQUESTED) is True
        # the fix must stay targeted — recipe_requested must NOT jump straight
        # back into open execution (that would re-introduce a loop surface).
        assert _valid_from(L.ActionState.RECIPE_REQUESTED, L.ActionState.IN_PROGRESS) is False
    finally:
        with L._state_lock:
            L.action_states.pop(up, None)


def test_force_path_recipe_requested_to_terminated_completes():
    """#128 sufficiency — the recovery edge must also be reachable via
    force_state_through_valid_path, the API the flow-complete logic actually uses.

    create_recipe.py:4464 drives every non-terminated action to TERMINATED with
    force_state_through_valid_path(...), which consults the SEPARATE state_paths
    map — not validate_state_transition's valid_transitions map.  A stuck
    recipe_requested action had no (RECIPE_REQUESTED, TERMINATED) entry there, so
    the force returned False ('No valid path') and the action stayed
    recipe_requested → ledger IN_PROGRESS → the goal never completed, EVEN with
    the valid_transitions edge added.  force_state_through_valid_path now falls
    back to the direct edge whenever validate_state_transition allows it, so
    valid_transitions is the single authority and state_paths is only multi-step
    shortcuts.  The action must actually END in TERMINATED (→ ledger COMPLETED).
    """
    try:
        import lifecycle_hooks as L
    except Exception as e:
        pytest.skip(f"lifecycle_hooks unavailable: {e}")

    up, aid = 'u128_force', 3
    with L._state_lock:
        L.action_states.setdefault(up, {})[aid] = L.ActionState.RECIPE_REQUESTED
    try:
        ok = L.force_state_through_valid_path(
            up, aid, L.ActionState.TERMINATED, "flow complete")
        assert ok is True, "force-terminate of a stuck recipe_requested action must succeed"
        assert L.get_action_state(up, aid) == L.ActionState.TERMINATED, (
            "action must actually reach TERMINATED (→ ledger COMPLETED), not stay "
            "recipe_requested (→ ledger IN_PROGRESS)")
        # the multi-step paths that ARE enumerated must still be honoured (the
        # fallback only fires for un-enumerated pairs, so no regression there).
        with L._state_lock:
            L.action_states[up][aid] = L.ActionState.ASSIGNED
        assert L.force_state_through_valid_path(
            up, aid, L.ActionState.COMPLETED, "enumerated multi-step") is True
    finally:
        with L._state_lock:
            L.action_states.pop(up, None)
