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

from hartos.lifecycle_hooks import (
    stall_guard_step, STALL_GUARD_MAX_ITERS, STALL_GUARD_INPROGRESS_ITERS,
    cycle_guard_step, CYCLE_GUARD_MAX_REVISITS,
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


def _replay(sequence, action_id=2, rounds=20, both=True):
    """Drive the trackers over a repeating state sequence, as the loop does.

    ``both=True`` mirrors production: the CREATE loop breaks if EITHER detector
    trips — one break decision, two signals.  ``both=False`` drives only
    stall_guard_step, to show what that one can and cannot see on its own.

    Returns (broke, total_steps). recipe_exists stays False throughout: in the
    live incident action 2's own recipe never landed, which is exactly why
    AUTO-ADVANCE kept re-requesting it.
    """
    key, it, steps = None, 0, 0
    ckey, centries = None, {}
    for _ in range(rounds):
        for state, repeat in sequence:
            for _ in range(repeat):
                steps += 1
                key, it, brk = stall_guard_step(key, it, action_id, state, False)
                if both:
                    ckey, centries, cbrk = cycle_guard_step(
                        ckey, centries, action_id, state, False)
                    brk = brk or cbrk
                if brk:
                    return True, steps
    return False, steps


def test_cycling_action_is_eventually_caught():
    """#485: the composed decision must catch a non-progressing cycle."""
    broke, steps = _replay(_LIVE_CYCLE_2026_08_04)
    assert broke is True, (
        f"neither detector fired across {steps} iterations of a non-progressing "
        f"cycle — an action that never finishes must be caught, not just one "
        f"that sits still")
    # It must also be caught FAST — well inside max_iterations=300, or the loop
    # has already done the damage (the live incident emitted ~321 log lines in
    # one second while burning toward that cap).
    assert steps < 300


def test_stall_guard_alone_still_cannot_see_the_cycle():
    """Pins WHY cycle_guard_step has to exist — this is the #485 gap itself.

    stall_guard_step's scope is deliberately narrow: it treats every state
    change as progress and every terminal state as a reset, which is right for
    a machine that advances monotonically. Driven alone over the live cycle it
    never fires. If someone later widens stall_guard_step to catch cycling too,
    this test fails and forces the duplication question to be answered rather
    than drifting into two overlapping detectors.
    """
    broke, _ = _replay(_LIVE_CYCLE_2026_08_04, both=False)
    assert broke is False


def test_replay_harness_is_not_vacuous():
    """The cycle tests must pass/fail for the RIGHT reason.

    A single unchanging state still trips stall_guard_step through the very
    same helper, so the harness drives the trackers correctly and the cycling
    results are about cycling — not a broken replay helper.
    """
    broke, _ = _replay([(ActionState.RECIPE_REQUESTED, 1)],
                       rounds=STALL_GUARD_MAX_ITERS + 5, both=False)
    assert broke is True


def test_cycle_guard_spares_a_long_single_state_occupancy():
    """THE regression risk of counting wrong.

    An action legitimately working in IN_PROGRESS for the whole
    STALL_GUARD_INPROGRESS_ITERS window is ONE entry, not N. If cycle_guard_step
    counted iterations instead of entries it would kill working actions at
    CYCLE_GUARD_MAX_REVISITS+1 — long before stall_guard_step's own 120 cap and
    contradicting test_in_progress_spared_within_working_zone.
    """
    key, entries = None, {}
    for _ in range(STALL_GUARD_INPROGRESS_ITERS + 50):
        key, entries, brk = cycle_guard_step(key, entries, 2, ActionState.IN_PROGRESS, False)
        assert brk is False
    assert entries == {ActionState.IN_PROGRESS: 1}


def test_cycle_guard_counts_entries_not_iterations():
    seq = [(ActionState.RECIPE_REQUESTED, 4), (ActionState.TERMINATED, 4)]
    key, entries = None, {}
    for _ in range(3):                      # 3 laps = 3 entries per state
        for state, repeat in seq:
            for _ in range(repeat):
                key, entries, _ = cycle_guard_step(key, entries, 2, state, False)
    assert entries == {ActionState.RECIPE_REQUESTED: 3, ActionState.TERMINATED: 3}


def test_cycle_guard_resets_when_the_action_advances():
    """current_action moving on is real progress — the pipeline did something."""
    key, entries = None, {}
    for _ in range(CYCLE_GUARD_MAX_REVISITS * 3):
        for state in (ActionState.RECIPE_REQUESTED, ActionState.TERMINATED):
            key, entries, _ = cycle_guard_step(key, entries, 2, state, False)
    key, entries, brk = cycle_guard_step(key, entries, 3, ActionState.ASSIGNED, False)
    assert brk is False and entries == {ActionState.ASSIGNED: 1}


def test_cycle_guard_resets_when_the_recipe_lands():
    """The action's OWN recipe on disk is the canonical progress signal, and it
    means the same thing here as it does in stall_guard_step."""
    key, entries, brk = cycle_guard_step(
        (2, ActionState.RECIPE_REQUESTED),
        {ActionState.RECIPE_REQUESTED: CYCLE_GUARD_MAX_REVISITS + 5},
        2, ActionState.RECIPE_REQUESTED, recipe_exists=True)
    assert (key, entries, brk) == (None, {}, False)


def test_cycle_guard_is_pure():
    """Callers thread state back in; the tracker must not mutate what it got."""
    passed_in = {ActionState.RECIPE_REQUESTED: 2}
    cycle_guard_step((2, ActionState.TERMINATED), passed_in,
                     2, ActionState.RECIPE_REQUESTED, False)
    assert passed_in == {ActionState.RECIPE_REQUESTED: 2}
