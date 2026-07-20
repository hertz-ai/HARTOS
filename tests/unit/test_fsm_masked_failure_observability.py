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
