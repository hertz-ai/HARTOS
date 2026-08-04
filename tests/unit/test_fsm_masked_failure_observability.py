"""#139 — observability for the #128 FAILED→COMPLETED recovery reconcile.

_is_possible_masked_failure flags ONLY the ambiguous case: a ledger FAILED
reconciled to COMPLETED because the ActionState reached TERMINATED (the
forced/give-up terminal) — which may be masking a genuine failure as success.
A verified-success terminal (COMPLETED / RECIPE_RECEIVED) over a stale-FAILED
ledger is a GENUINE recovery and must NOT be flagged.

Behavioural: real predicate, asserts the boolean. Observability only — the
reconcile behaviour is unchanged; PREVENTING the masking is a policy call (#139).

    python -m pytest tests/unit/test_fsm_masked_failure_observability.py --noconftest -q
"""
from lifecycle_hooks import _is_possible_masked_failure, ActionState


def test_terminated_recovery_is_flagged():
    # FAILED → COMPLETED via TERMINATED (forced/give-up terminal): ambiguous,
    # possibly a masked failure → flagged True.
    assert _is_possible_masked_failure(True, ActionState.TERMINATED) is True


def test_verified_success_terminals_are_genuine_recovery_not_flagged():
    # COMPLETED / RECIPE_RECEIVED are verified-success terminals → true
    # recovery of a stale-FAILED ledger, never flagged.
    assert _is_possible_masked_failure(True, ActionState.COMPLETED) is False
    assert _is_possible_masked_failure(True, ActionState.RECIPE_RECEIVED) is False


def test_not_a_recovery_is_never_flagged():
    # recover_failed False → not a FAILED→COMPLETED reconcile; nothing to flag.
    assert _is_possible_masked_failure(False, ActionState.TERMINATED) is False
    assert _is_possible_masked_failure(False, ActionState.COMPLETED) is False


def test_non_completed_mapping_states_not_flagged():
    # ERROR / IN_PROGRESS don't map to ledger COMPLETED, so recover_failed
    # would be False for them anyway; assert the predicate stays False.
    assert _is_possible_masked_failure(True, ActionState.ERROR) is False
    assert _is_possible_masked_failure(True, ActionState.IN_PROGRESS) is False


# ── #139 policy (task #6): the reconcile now DISAMBIGUATES by banked artifact ──
# Behavioural: drives the REAL _auto_sync_to_ledger with a stub ledger whose
# task is FAILED and an ActionState of TERMINATED, and asserts the observable
# side-effect (update_task_status called or NOT) against a real temp artifact.

import os
from unittest.mock import MagicMock

import lifecycle_hooks as LH


class _FakeStatus:
    """Stands in for agent_ledger's LedgerTaskStatus enum (the real package
    is absent in this test env; the sync only compares identities)."""
    PENDING = 'pending'
    IN_PROGRESS = 'in_progress'
    COMPLETED = 'completed'
    FAILED = 'failed'
    BLOCKED = 'blocked'
    PAUSED = 'paused'
    USER_STOPPED = 'user_stopped'
    CANCELLED = 'cancelled'


def _stub_ledger_with_failed_task(recipe_coords):
    """A minimal ledger whose action_7 task is terminal-FAILED."""
    ledger = MagicMock()
    task = MagicMock()
    task.is_owned = True
    task.is_terminal.return_value = True
    task.status = _FakeStatus.FAILED
    p, f, a = recipe_coords
    task.recipe_prompt_id = p
    task.recipe_flow_id = f
    task.recipe_action_id = a
    ledger.tasks = {'action_7': task}
    return ledger, task


def _run_sync(recipe_coords, monkeypatch):
    monkeypatch.setattr(LH, '_get_ledger_task_status', lambda: _FakeStatus)
    key = 'u1_ptest'
    ledger, task = _stub_ledger_with_failed_task(recipe_coords)
    LH._ledger_registry[key] = ledger
    try:
        LH._auto_sync_to_ledger(key, 7, ActionState.TERMINATED)
    finally:
        LH._ledger_registry.pop(key, None)
    return ledger


def test_terminated_without_artifact_stays_failed(tmp_path, monkeypatch):
    """No banked recipe -> the reconcile REFUSES the FAILED->COMPLETED flip."""
    import helper
    monkeypatch.setattr(helper, 'PROMPTS_DIR', str(tmp_path))
    before = LH._MASKED_FAILURE_STATE['prevented']
    ledger = _run_sync(('9001', '0', '7'), monkeypatch)
    ledger.update_task_status.assert_not_called()
    assert LH._MASKED_FAILURE_STATE['prevented'] == before + 1


def test_terminated_with_banked_artifact_recovers(tmp_path, monkeypatch):
    """Banked recipe present -> genuine recovery, the flip proceeds."""
    import helper
    monkeypatch.setattr(helper, 'PROMPTS_DIR', str(tmp_path))
    (tmp_path / '9001_0_7.json').write_text('{"recipe": true}')
    ledger = _run_sync(('9001', '0', '7'), monkeypatch)
    assert ledger.update_task_status.call_count == 1


def test_unknown_coordinates_fail_open(tmp_path, monkeypatch):
    """Task lacks recipe coordinates -> historical reconcile behaviour."""
    import helper
    monkeypatch.setattr(helper, 'PROMPTS_DIR', str(tmp_path))
    ledger = _run_sync((None, None, None), monkeypatch)
    assert ledger.update_task_status.call_count == 1


def test_banked_artifact_exists_tristate(tmp_path, monkeypatch):
    import helper
    monkeypatch.setattr(helper, 'PROMPTS_DIR', str(tmp_path))
    t = MagicMock()
    t.recipe_prompt_id, t.recipe_flow_id, t.recipe_action_id = '5', '1', '2'
    assert LH._banked_artifact_exists(t) is False
    (tmp_path / '5_1_2.json').write_text('{}')
    assert LH._banked_artifact_exists(t) is True
    t2 = MagicMock()
    t2.recipe_prompt_id = None
    t2.recipe_flow_id = None
    t2.recipe_action_id = None
    assert LH._banked_artifact_exists(t2) is None
