"""#139 — the GAVE_UP terminal ends the fake-success bug, honestly.

Before: a stalled/give-up action was force-driven to TERMINATED (→ ledger
COMPLETED), so the user was told "done" for work that failed, and the reconcile
flipped a reaped-FAILED ledger to COMPLETED. Now a give-up routes to the DISTINCT
GAVE_UP terminal (→ ledger FAILED), re-openable so the daemon can retry it via a
hive peer. A verified action still reaches TERMINATED (→ COMPLETED), so #128's
genuine-recovery reconcile is preserved.

Behavioural: exercises the real FSM (validate/force/_auto_sync_to_ledger) + a real
in-memory SmartLedger, not string checks.
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


def _L():
    try:
        import lifecycle_hooks as L
        return L
    except Exception as e:  # pragma: no cover
        pytest.skip(f"lifecycle_hooks unavailable: {e}")


# ─── the honest ledger mapping ───

def test_gave_up_maps_to_ledger_failed():
    L = _L()
    from agent_ledger.core import TaskStatus
    # STATE_MAP is built inside _auto_sync_to_ledger; assert via a real sync below.
    assert L.ActionState.GAVE_UP in L._TERMINAL_STATES


def test_gave_up_over_failed_ledger_stays_failed_not_completed():
    """THE #139 fix: a GAVE_UP (force-abandoned) action maps to ledger FAILED, so
    the reconcile never flips FAILED -> COMPLETED. The fake success is gone."""
    L = _L()
    try:
        from agent_ledger.core import Task, TaskType, TaskStatus, SmartLedger
        from agent_ledger.backends import InMemoryBackend
    except Exception as e:
        pytest.skip(f"agent_ledger unavailable: {e}")
    up = 'u139_giveup'
    ledger = SmartLedger(agent_id="test", session_id="s", backend=InMemoryBackend())
    ledger.add_task(Task(task_id="action_1", description="d",
                         task_type=TaskType.PRE_ASSIGNED, status=TaskStatus.FAILED))
    L._ledger_registry[up] = ledger
    try:
        L._auto_sync_to_ledger(up, 1, L.ActionState.GAVE_UP)
        assert ledger.tasks["action_1"].status == TaskStatus.FAILED, (
            "a GAVE_UP action must stay FAILED, never be promoted to COMPLETED (#139)")
    finally:
        L._ledger_registry.pop(up, None)


def test_genuine_terminated_over_failed_still_completes():
    """Guard: a VERIFIED recovery (TERMINATED over FAILED) still promotes to
    COMPLETED — #128's genuine-recovery reconcile is preserved, unchanged."""
    L = _L()
    try:
        from agent_ledger.core import Task, TaskType, TaskStatus, SmartLedger
        from agent_ledger.backends import InMemoryBackend
    except Exception as e:
        pytest.skip(f"agent_ledger unavailable: {e}")
    up = 'u139_genuine'
    ledger = SmartLedger(agent_id="test", session_id="s", backend=InMemoryBackend())
    ledger.add_task(Task(task_id="action_1", description="d",
                         task_type=TaskType.PRE_ASSIGNED, status=TaskStatus.FAILED))
    L._ledger_registry[up] = ledger
    try:
        L._auto_sync_to_ledger(up, 1, L.ActionState.TERMINATED)
        assert ledger.tasks["action_1"].status == TaskStatus.COMPLETED, (
            "a verified TERMINATED recovery must still reconcile FAILED -> COMPLETED (#128)")
    finally:
        L._ledger_registry.pop(up, None)


# ─── the FSM edges ───

def _valid(L, up, aid, frm, to):
    with L._state_lock:
        L.action_states.setdefault(up, {})[aid] = frm
    return L.validate_state_transition(up, aid, to)


def test_stalled_states_can_give_up():
    L = _L()
    up = 'u139_edges'
    try:
        for frm in (L.ActionState.RECIPE_REQUESTED, L.ActionState.ERROR,
                    L.ActionState.IN_PROGRESS, L.ActionState.ASSIGNED,
                    L.ActionState.STATUS_VERIFICATION_REQUESTED,
                    L.ActionState.PENDING, L.ActionState.FALLBACK_REQUESTED):
            assert _valid(L, up, 1, frm, L.ActionState.GAVE_UP) is True, (
                f"{frm.value} must be able to give up")
    finally:
        with L._state_lock:
            L.action_states.pop(up, None)


def test_verified_states_cannot_give_up():
    """A verified/terminal action must NOT route to GAVE_UP (it goes to TERMINATED)."""
    L = _L()
    up = 'u139_noverify'
    try:
        for frm in (L.ActionState.COMPLETED, L.ActionState.RECIPE_RECEIVED,
                    L.ActionState.TERMINATED):
            assert _valid(L, up, 1, frm, L.ActionState.GAVE_UP) is False, (
                f"{frm.value} (verified/terminal) must NOT give up")
    finally:
        with L._state_lock:
            L.action_states.pop(up, None)


def test_gave_up_is_reopenable_for_hive_retry():
    """Re-openable so the daemon can retry via a hive peer — but never straight
    back into open execution (no loop surface), mirroring TERMINATED."""
    L = _L()
    up = 'u139_reopen'
    try:
        assert _valid(L, up, 1, L.ActionState.GAVE_UP, L.ActionState.ASSIGNED) is True
        assert _valid(L, up, 1, L.ActionState.GAVE_UP, L.ActionState.RECIPE_REQUESTED) is True
        assert _valid(L, up, 1, L.ActionState.GAVE_UP, L.ActionState.IN_PROGRESS) is False
    finally:
        with L._state_lock:
            L.action_states.pop(up, None)


def test_force_stalled_action_reaches_gave_up():
    """The flow-complete force path (force_state_through_valid_path) must land a
    stalled recipe_requested action in GAVE_UP, not launder it to TERMINATED."""
    L = _L()
    up = 'u139_force'
    with L._state_lock:
        L.action_states.setdefault(up, {})[1] = L.ActionState.RECIPE_REQUESTED
    try:
        ok = L.force_state_through_valid_path(up, 1, L.ActionState.GAVE_UP, "flow complete")
        assert ok is True
        assert L.get_action_state(up, 1) == L.ActionState.GAVE_UP
    finally:
        with L._state_lock:
            L.action_states.pop(up, None)


def test_enforce_all_terminated_accepts_gave_up():
    """A flow with a gave-up action is still 'all terminal' (no more work this
    pass); the give-up is honestly failed, retryable later."""
    L = _L()
    up = 'u139_enforce'
    with L._state_lock:
        L.action_states.setdefault(up, {})[1] = L.ActionState.TERMINATED
        L.action_states[up][2] = L.ActionState.GAVE_UP
    try:
        ok, _msg = L.enforce_all_actions_terminated(up, 2)
        assert ok is True, "a TERMINATED + GAVE_UP flow must count as all-terminal"
    finally:
        with L._state_lock:
            L.action_states.pop(up, None)
