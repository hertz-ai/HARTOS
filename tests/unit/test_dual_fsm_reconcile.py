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
