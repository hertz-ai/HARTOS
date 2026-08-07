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

_LEDGER_SRC = os.path.join(ROOT, 'agent-ledger-opensource')
if _LEDGER_SRC not in sys.path:
    sys.path.insert(0, _LEDGER_SRC)


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


def test_ledger_allows_failed_recovery_to_success():
    """#128 reconcile (ledger half): a FAILED ledger task may RECOVER to a SUCCESS
    terminal (COMPLETED/TERMINATED) when the agent fallback FSM drives it there —
    but NEVER back into active work (no retry storm; #59's circuit breaker holds),
    and a genuine hard-terminal (CANCELLED) stays terminal.

    Why this exists: a task can be reaped to FAILED (zombie_reaper) while merely
    stalled in recipe_requested; now that #128 lets the action recover (fallback →
    terminated), the ledger's FAILED is stale and must follow, else the recovered
    work reads as failed forever and the goal-completion count never moves.
    """
    try:
        from agent_ledger.core import Task, TaskType, TaskStatus
    except Exception as e:
        pytest.skip(f"agent_ledger unavailable: {e}")

    def _t(status):
        return Task(task_id="t1", description="d",
                    task_type=TaskType.PRE_ASSIGNED, status=status)

    # recovery to success — newly allowed, and it actually applies
    rec = _t(TaskStatus.FAILED)
    assert rec.transition_to(TaskStatus.COMPLETED) is True
    assert rec.status == TaskStatus.COMPLETED
    assert _t(TaskStatus.FAILED).transition_to(TaskStatus.TERMINATED) is True
    # but NEVER back into active work (would re-enable retry storms #59 fixed)
    assert _t(TaskStatus.FAILED).transition_to(TaskStatus.IN_PROGRESS) is False
    assert _t(TaskStatus.FAILED).transition_to(TaskStatus.BLOCKED) is False
    # a genuine hard-terminal stays terminal
    assert _t(TaskStatus.CANCELLED).transition_to(TaskStatus.COMPLETED) is False
    # the pre-existing COMPLETED→ROLLED_BACK special case is unregressed
    assert _t(TaskStatus.COMPLETED).transition_to(TaskStatus.ROLLED_BACK) is True


def test_auto_sync_reconciles_failed_ledger_on_recovery():
    """#128 reconcile (sync half): when an action's ledger task is stale-FAILED
    and the ActionState recovers to TERMINATED (→ ledger COMPLETED),
    _auto_sync_to_ledger must reconcile the ledger to COMPLETED instead of the
    #56 advisory-skip (which left the recovered goal reading FAILED and capped
    the completed count)."""
    try:
        import lifecycle_hooks as L
        from agent_ledger.core import Task, TaskType, TaskStatus, SmartLedger
        from agent_ledger.backends import InMemoryBackend
    except Exception as e:
        pytest.skip(f"deps unavailable: {e}")

    up = 'u128_reconcile'
    ledger = SmartLedger(agent_id="test", session_id="s", backend=InMemoryBackend())
    ledger.add_task(Task(task_id="action_1", description="d",
                         task_type=TaskType.PRE_ASSIGNED, status=TaskStatus.FAILED))
    L._ledger_registry[up] = ledger
    try:
        # ActionState recovered to TERMINATED (maps to ledger COMPLETED)
        L._auto_sync_to_ledger(up, 1, L.ActionState.TERMINATED)
        assert ledger.tasks["action_1"].status == TaskStatus.COMPLETED, (
            "a recovered action must reconcile the stale ledger FAILED → COMPLETED, "
            "not leave it FAILED")
    finally:
        L._ledger_registry.pop(up, None)


def test_refusal_is_not_logged_as_error(caplog):
    """A PREDICATE must not log ERROR when the answer is simply 'no'.

    validate_state_transition() is used purely as a question — :730, :901,
    :1024, :1059, :1091, :1109, :1119, :1148, :1174, :1197, :1223 all consume
    its RETURN VALUE, and the tests above assert True/False.  Nothing reads
    its log to decide anything.

    It also double-logged every genuine failure: set_action_state() calls it
    (:730), it logged ERROR, then set_action_state raised
    StateTransitionError (:732), which safe_set_state caught and logged
    ERROR again (:777).  One bad set produced TWO error lines — observed
    2026-08-05 as 275 of each for ~275 real terminated→pending events, i.e.
    550 lines reading as 550 failures.

    FAILS PRE-FIX: the refusal was logger.error at :982.
    """
    import logging
    try:
        import lifecycle_hooks as L
    except Exception as e:
        pytest.skip(f"lifecycle_hooks unavailable: {e}")

    up, aid = 'u626_predicate', 1
    with L._state_lock:
        L.action_states.setdefault(up, {})[aid] = L.ActionState.TERMINATED
    try:
        with caplog.at_level(logging.DEBUG):
            caplog.clear()
            # terminated → pending is NOT an allowed edge (:971 lists only
            # ASSIGNED and RECIPE_REQUESTED) — the exact live case.
            assert L.validate_state_transition(up, aid, L.ActionState.PENDING) is False

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not errors, (
            "predicate logged at ERROR for a normal 'no' answer: "
            f"{[r.getMessage() for r in errors]}")

        # The diagnostic must survive — silencing it would lose the only
        # evidence the pattern exists.  It just belongs at DEBUG.
        assert any('Invalid transition' in r.getMessage() for r in caplog.records), \
            "refusal text disappeared entirely; it should be DEBUG, not gone"
    finally:
        with L._state_lock:
            L.action_states.pop(up, None)
