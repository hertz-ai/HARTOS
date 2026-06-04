"""No-progress stall guard for the CREATE loop (create_recipe.get_response_group).

The live 2026-06-04 bug: a multi-action CREATE flow whose LATER action stalls
in `recipe_requested` (while an EARLIER action already saved its recipe) spun
all 300 loop iterations before giving up with a generic TERMINATE, because the
old guard's progress check was "did ANY action 1..current save a recipe" (True
once action 1 finished) and was also skipped by `continue` branches.

`stall_guard_step` is the pure, reachable, per-action replacement. These are
behavioural tests of that real function (imported from lifecycle_hooks, which
is autogen-free) — call it, assert the returned counter + break decision. No
grep tests.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from lifecycle_hooks import (
    stall_guard_step, STALL_GUARD_MAX_ITERS, ActionState,
)


def _run_until_break(action_id, state, recipe_exists, max_steps):
    """Drive the tracker like the loop does and return (broke, iters_at_break)."""
    sid, it = None, 0
    for _ in range(max_steps):
        sid, it, brk = stall_guard_step(sid, it, action_id, state, recipe_exists)
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
    sid, it, brk = stall_guard_step(2, STALL_GUARD_MAX_ITERS,
                                    2, ActionState.RECIPE_REQUESTED,
                                    recipe_exists=True)
    assert (sid, it, brk) == (None, 0, False)


def test_never_breaks_for_non_stuck_state():
    broke, _ = _run_until_break(
        1, ActionState.IN_PROGRESS, recipe_exists=False,
        max_steps=STALL_GUARD_MAX_ITERS + 50)
    assert broke is False


def test_counter_resets_when_stuck_action_changes():
    # Action 2 nearly at the threshold, then action 3 becomes current:
    # the per-action counter must RESTART at 1 (not carry over and insta-break).
    sid, it, brk = stall_guard_step(2, STALL_GUARD_MAX_ITERS,
                                    3, ActionState.RECIPE_REQUESTED,
                                    recipe_exists=False)
    assert sid == 3 and it == 1 and brk is False


def test_later_action_stall_is_caught_even_if_earlier_action_done():
    """THE live bug: action 1 completed (its recipe exists), action 2 stalls.
    The per-action tracker keys off action 2's OWN recipe, so it still trips —
    unlike the old `_any_recipes(range(1, current+1))` check which stayed True."""
    # action 2 is the stuck one; its own recipe does NOT exist.
    broke, _ = _run_until_break(
        2, ActionState.RECIPE_REQUESTED, recipe_exists=False,
        max_steps=STALL_GUARD_MAX_ITERS + 5)
    assert broke is True
