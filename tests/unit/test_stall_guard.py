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
