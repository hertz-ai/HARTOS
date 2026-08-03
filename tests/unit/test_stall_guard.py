"""No-progress stall guard for the CREATE loop (create_recipe.get_response_group).

Two live bugs this guards:
  1. (2026-06-04, recipe phase) a multi-action CREATE whose LATER action stalls
     in `recipe_requested` (while an EARLIER action already saved its recipe)
     spun all 300 loop iterations — the old "did ANY action save a recipe"
     progress check stayed True once action 1 finished.
  2. (2026-06-04, execution phase) a daemon coding goal's action 2 sat in
     IN_PROGRESS emitting unparseable recipe JSON, NEVER reaching
     recipe_requested, so the old action-id-only guard (which only watched the
     "requested" states) reset every iteration and never fired — it ground
     ~11 min toward the 300-iter cap.

`stall_guard_step` keys on `(action_id, state)` and uses a tight cap for the
"requested" states and a deliberately-generous cap for any other non-terminal
state (so a slow-but-progressing IN_PROGRESS action is never tripped, only a
genuinely-wedged one).  These are behavioural tests of the real function
(imported from lifecycle_hooks, which is autogen-free): call it, assert the
returned counter + break decision.  No grep tests.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from lifecycle_hooks import (
    stall_guard_step, STALL_GUARD_MAX_ITERS, STALL_GUARD_INPROGRESS_ITERS,
    ActionState, recipe_correction_directive,
)


def _run_until_break(action_id, state, recipe_exists, max_steps):
    """Drive the tracker like the loop does and return (broke, iters_at_break)."""
    key, it = None, 0
    for _ in range(max_steps):
        key, it, brk = stall_guard_step(key, it, action_id, state, recipe_exists)
        if brk:
            return True, it
    return False, it


def test_breaks_just_after_threshold_when_stuck_no_recipe():
    broke, at = _run_until_break(
        2, ActionState.RECIPE_REQUESTED, recipe_exists=False,
        max_steps=STALL_GUARD_MAX_ITERS + 5)
    assert broke is True
    assert at == STALL_GUARD_MAX_ITERS + 1   # fires the iteration AFTER threshold


def test_fallback_requested_also_guarded():
    broke, _ = _run_until_break(
        1, ActionState.FALLBACK_REQUESTED, recipe_exists=False,
        max_steps=STALL_GUARD_MAX_ITERS + 5)
    assert broke is True


def test_never_breaks_when_recipe_exists():
    # Stuck state but the current action's recipe IS saved -> progress -> reset.
    key, it, brk = stall_guard_step((2, ActionState.RECIPE_REQUESTED),
                                    STALL_GUARD_MAX_ITERS,
                                    2, ActionState.RECIPE_REQUESTED,
                                    recipe_exists=True)
    assert (key, it, brk) == (None, 0, False)


def test_terminal_state_resets():
    # A completed/terminated action is progress, never a stall — even with no
    # recipe file yet (it may be saved a beat later).
    for _term in (ActionState.COMPLETED, ActionState.TERMINATED, ActionState.ERROR):
        key, it, brk = stall_guard_step((1, ActionState.IN_PROGRESS), 99,
                                        1, _term, recipe_exists=False)
        assert (key, it, brk) == (None, 0, False)


def test_in_progress_spared_within_working_zone():
    # Bug #2's flip side: a legit action genuinely working in IN_PROGRESS must
    # NOT be killed.  Well past the old proven-safe zone (~80) it still runs.
    broke, _ = _run_until_break(
        1, ActionState.IN_PROGRESS, recipe_exists=False,
        max_steps=STALL_GUARD_INPROGRESS_ITERS - 1)
    assert broke is False


def test_in_progress_breaks_when_wedged():
    # Bug #2: an action wedged in IN_PROGRESS (never reaching recipe_requested)
    # IS now caught — at the looser cap, not the tight one.
    broke, at = _run_until_break(
        2, ActionState.IN_PROGRESS, recipe_exists=False,
        max_steps=STALL_GUARD_INPROGRESS_ITERS + 5)
    assert broke is True
    assert at == STALL_GUARD_INPROGRESS_ITERS + 1
    # And it took strictly longer than the tight recipe-phase cap would have.
    assert at > STALL_GUARD_MAX_ITERS + 1


def test_counter_resets_when_current_action_changes():
    # Action 2 near threshold, then action 3 becomes current: the per-action
    # counter must RESTART at 1 (not carry over and insta-break).
    key, it, brk = stall_guard_step((2, ActionState.RECIPE_REQUESTED),
                                    STALL_GUARD_MAX_ITERS,
                                    3, ActionState.RECIPE_REQUESTED,
                                    recipe_exists=False)
    assert key == (3, ActionState.RECIPE_REQUESTED) and it == 1 and brk is False


def test_counter_resets_on_state_advance():
    # An action working in IN_PROGRESS accrues some count, then advances to
    # RECIPE_REQUESTED (a real state change = progress) — the (action,state) key
    # changes, so the counter RESTARTS at 1 rather than carrying over.
    key, it = None, 0
    for _ in range(10):
        key, it, _ = stall_guard_step(key, it, 2, ActionState.IN_PROGRESS, False)
    assert it == 10 and key == (2, ActionState.IN_PROGRESS)
    key2, it2, brk2 = stall_guard_step(key, it, 2, ActionState.RECIPE_REQUESTED, False)
    assert key2 == (2, ActionState.RECIPE_REQUESTED) and it2 == 1 and brk2 is False


def test_later_action_stall_is_caught_even_if_earlier_action_done():
    """THE recipe-phase live bug: action 1 completed (its recipe exists),
    action 2 stalls.  The per-action tracker keys off action 2's OWN recipe, so
    it still trips — unlike the old `_any_recipes(range(1, current+1))` check
    which stayed True."""
    broke, _ = _run_until_break(
        2, ActionState.RECIPE_REQUESTED, recipe_exists=False,
        max_steps=STALL_GUARD_MAX_ITERS + 5)
    assert broke is True


# ── recipe_correction_directive (#89 content-side: unparseable recipe JSON) ──
def test_correction_directive_clean_first_attempt_is_empty():
    assert recipe_correction_directive(0) == ''
    assert recipe_correction_directive(-1) == ''


def test_correction_directive_first_failure_demands_only_json():
    d = recipe_correction_directive(1)
    assert 'ONLY' in d and 'JSON' in d and 'fences' in d
    # first correction does NOT yet hand over the verbatim fallback object
    assert '"status":"done"' not in d


def test_correction_directive_escalates_with_fallback_object():
    d2 = recipe_correction_directive(2)
    assert 'ONLY' in d2                      # still demands clean JSON
    assert '"status":"done"' in d2           # plus a minimal valid object to emit
    assert '"recipe":[]' in d2
    assert len(d2) > len(recipe_correction_directive(1))   # escalation grows it


# The measured Action-2 state sequence from the live spin, in order, as taken
# from server.log 01:42:30-01:43:10 via `grep -oE "Action 2: [a-z_]+" | uniq -c`:
#   9 assigned | 3 in_progress | 1 status_verification_requested | 3 completed
#   | 1 terminated | 3 recipe_requested | 3 terminated | 5 recipe_requested
#   | 9 recipe_received
# Kept as (state, repeat) so the shape stays readable and the run-lengths are
# the real ones rather than invented.
_LIVE_CYCLE_2026_08_04 = [
    (ActionState.ASSIGNED, 9),
    (ActionState.IN_PROGRESS, 3),
    (ActionState.STATUS_VERIFICATION_REQUESTED, 1),
    (ActionState.COMPLETED, 3),
    (ActionState.TERMINATED, 1),
    (ActionState.RECIPE_REQUESTED, 3),
    (ActionState.TERMINATED, 3),
    (ActionState.RECIPE_REQUESTED, 5),
    (ActionState.RECIPE_RECEIVED, 9),
]


def _replay(sequence, action_id=2, rounds=20):
    """Drive the tracker over a repeating state sequence, as the loop does.

    Returns (broke, total_steps). recipe_exists stays False throughout: in the
    live incident action 2's own recipe never landed, which is exactly why
    AUTO-ADVANCE kept re-requesting it.
    """
    key, it, steps = None, 0, 0
    for _ in range(rounds):
        for state, repeat in sequence:
            for _ in range(repeat):
                steps += 1
                key, it, brk = stall_guard_step(key, it, action_id, state, False)
                if brk:
                    return True, steps
    return False, steps


@pytest.mark.xfail(strict=True, reason=(
    "#485 KNOWN GAP: stall_guard_step detects an action STUCK IN ONE STATE, not "
    "one CYCLING through states. Two independent resets defeat it here — the "
    "(action_id, state) key changes on every transition so `iters` restarts at "
    "1, and the cycle revisits COMPLETED/TERMINATED which hard-reset to 0. "
    "Measured live 2026-08-04: action 2 churned these 7 states while the loop "
    "burned toward max_iterations=300, emitting ~321 log lines in a single "
    "second and starving the chat hot path (58s for a one-word reply). "
    "STALL-GUARD did fire twice elsewhere in that window, so the guard is live "
    "— it simply cannot see this shape. "
    "xfail(strict) NOT skip: this must flip to a hard failure the moment the "
    "gap is closed, so the marker gets removed rather than quietly rotting. "
    "Fix by EXTENDING stall_guard_step (revisited-state detection) — do NOT add "
    "a second cap; test_terminal_state_resets and "
    "test_counter_resets_on_state_advance encode deliberate behaviour that must "
    "keep passing."))
def test_cycling_action_is_eventually_caught():
    broke, steps = _replay(_LIVE_CYCLE_2026_08_04)
    assert broke is True, (
        f"guard never fired across {steps} iterations of a non-progressing "
        f"cycle — an action that never finishes must be caught, not just one "
        f"that sits still")


def test_replay_harness_is_not_vacuous():
    """The xfail above must fail for the RIGHT reason.

    A single unchanging state from the same sequence still trips the guard, so
    the harness itself drives the tracker correctly and the xfail is about
    cycling specifically — not a broken replay helper.
    """
    broke, _ = _replay([(ActionState.RECIPE_REQUESTED, 1)], rounds=STALL_GUARD_MAX_ITERS + 5)
    assert broke is True
