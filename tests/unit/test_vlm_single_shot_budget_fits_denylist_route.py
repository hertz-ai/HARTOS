"""The single_shot iteration budget must fit the route the denylist FORCES.

MEASURED LIVE 2026-09-10 00:54-00:57, agent 18088688973, action 2.  Two
`execute_windows_or_android_command` calls ran for real (agent_system.log:
TOOL EXECUTION SUCCESS latency_ms=12005.6 and 30245.8) and BOTH came back
`{'status': 'incomplete', 'exit_reason': 'max_iterations'}`, which the tool
converts into "Not able to perform this action now please try later" -- a
core.constants.TOOL_FAILURE_RESULTS member.  The reuse fabrication gate then
correctly reported unrun=['execute_windows_or_android_command'] and refused
to advance.  FAB-GUARD was RIGHT; the loop never finished its work.

WHY IT NEVER FINISHED, read not guessed.  `route_task` (qwen3vl_backend) is a
keyword classifier with exactly two positive patterns lists -- _ENUMERATE_*
and _MULTI_STEP_* -- and `single_shot` is its DEFAULT FALL-THROUGH:

    for pat in self._ENUMERATE_PATTERNS: ...
    for pat in self._MULTI_STEP_PATTERNS: ...
    return 'single_shot'

local_loop then treats that fall-through as a positive "one click" verdict and
clamps the caller's budget to 3, with the comment "Cap at 3 -- gives one
nudge-retry + one followup if the click misses".  Both live tasks fell
through: "Execute the following sequence of commands:" (the word `execute` is
not in the multi-step alternation at all) and "Run the following Python script
to verify ... and report the actual ..." (`run ... and ...` needs a following
click|type|select|press|enter|play|search; `report` is not in that set).

So a task whose text literally says "sequence of commands" was given a
click-sized budget of 3.

THE ARITHMETIC, which is the whole defect.  The safety denylist refuses
interpreter one-liners (`\\bpython[23]?\\s+-c\\s` et al -- an ethical-hacker
review added them deliberately and they must NOT be weakened).  The sanctioned
alternative, which _VLM_ACTION_LIST now states explicitly, is write_file the
script then shell it.  That route costs:

    1. shell `python -c ...`   -> REFUSED by the denylist
    2. write_file the script   -> ok
    3. shell `python file.py`  -> ok
    4. done                    -> the loop's own exit action

`done` consumes an iteration of its own and yields a `completion` response,
not an action -- pinned by the sibling tests test_loop_exits_on_done (DONE on
call 1 => 1 iteration) and test_loop_3_iterations (IN_PROGRESS, IN_PROGRESS,
DONE => 3 iterations).  So the minimum is 4 and the cap was 3: the route the
system itself forces could not complete even when walked perfectly.  Live
iteration 2 DID write_file correctly -- the model followed the prompt -- and
the budget died one step later.

NOT A TUNING KNOB.  The number below is derived from that route, and
test_the_sanctioned_alternative_still_costs_these_steps re-derives it from the
prompt text so the two cannot drift apart silently.

WHY THIS WAS INVISIBLE.  Every existing loop test sets HEVOLVE_VLM_UNIFIED=0,
which leaves `qwen3vl is None`, so the router block never executes and the
clamp is never reached.  The clamp had no test at any value.

    python -m pytest tests/unit/test_vlm_single_shot_budget_fits_denylist_route.py --noconftest -q
"""
import pytest

from integrations.vlm.local_loop import (
    MAX_ITERATIONS,
    _DENYLIST_RECOVERY_ITERATIONS,
    _SINGLE_SHOT_RECOVERY_MARGIN,
    _VLM_ACTION_LIST,
    _route_iteration_budget,
)


class TestTheBudgetFitsTheForcedRoute:

    def test_single_shot_can_walk_the_denylist_recovery_route(self):
        """The fall-through route must afford refuse -> write_file -> shell -> done."""
        budget = _route_iteration_budget('single_shot', MAX_ITERATIONS)
        assert budget >= _DENYLIST_RECOVERY_ITERATIONS, (
            f"single_shot is capped at {budget} iterations but the route the "
            f"safety denylist FORCES costs {_DENYLIST_RECOVERY_ITERATIONS} "
            f"(refused one-liner, write_file, shell, done). Live 2026-09-10 "
            f"agent 18088688973 action 2: both VLM runs exited "
            f"exit_reason=max_iterations, the tool returned a "
            f"TOOL_FAILURE_RESULTS string, and the fabrication gate refused "
            f"the action. `single_shot` is route_task's DEFAULT, not a "
            f"positive 'one click' verdict -- it must not carry a click-sized "
            f"budget.")

    def test_the_sanctioned_alternative_still_costs_these_steps(self):
        """Re-derive the cost from the prompt, so the two cannot drift.

        If the prompt ever stops telling the model to write_file-then-shell,
        this number is no longer derived from anything and must be re-thought
        rather than silently kept.
        """
        low = _VLM_ACTION_LIST.lower()
        assert 'write_file' in low and 'shell' in low, (
            "_VLM_ACTION_LIST no longer names the write_file->shell route; "
            "_DENYLIST_RECOVERY_ITERATIONS was derived from it, so re-derive "
            "the constant instead of leaving it as a bare number.")
        # refused one-liner + write_file + shell + done
        assert _DENYLIST_RECOVERY_ITERATIONS == 4, (
            "the derivation is 4 steps; changing the constant without "
            "changing the route it is derived from makes it a tuning knob")

    def test_the_margin_is_the_original_authors_not_mine(self):
        """Applying the shipped margin to a CLICK must reproduce the old 3.

        The clamp that shipped for two years said, in its own comment, "Cap at
        3 -- gives one nudge-retry + one followup if the click misses".  A
        click is a route of length 1, so that policy is `length + 2`.  If the
        same formula does not reproduce 3 for a click, then the margin I
        applied to the length-4 route is my own invention and has to be
        argued for on its own evidence rather than inherited.
        """
        click_route_length = 1
        assert click_route_length + _SINGLE_SHOT_RECOVERY_MARGIN == 3, (
            f"margin {_SINGLE_SHOT_RECOVERY_MARGIN} does not reproduce the "
            f"historical cap of 3 for a one-click route, so it is no longer "
            f"the original policy")


class TestTheOtherRoutesAreUntouched:
    """The clamp exists for real reasons on the POSITIVELY-matched routes."""

    def test_enumerate_still_gets_one(self):
        assert _route_iteration_budget('enumerate', MAX_ITERATIONS) == 1, (
            "enumerate is a parse_and_reason snapshot with no follow-up; "
            "that clamp is a positive classification and must survive")

    def test_multi_step_keeps_the_callers_budget(self):
        assert _route_iteration_budget('multi_step', MAX_ITERATIONS) == MAX_ITERATIONS

    def test_unknown_route_keeps_the_callers_budget(self):
        """route_task may gain a fourth verdict; default must not over-cap."""
        assert _route_iteration_budget('something_new', MAX_ITERATIONS) == MAX_ITERATIONS


class TestItNeverRaisesACallersBudget:
    """A budget is a CEILING the caller chose; routing may lower, never raise.

    The original code expressed this with `and max_iterations > 3` guards.
    Keeping it matters: tests/functional call the loop with max_iterations=1
    and 5 to pin specific control flow, and a routing decision that raised
    those would silently invalidate them.
    """

    @pytest.mark.parametrize('route', ['single_shot', 'enumerate', 'multi_step'])
    @pytest.mark.parametrize('requested', [1, 2, 3])
    def test_small_caller_budget_survives(self, route, requested):
        assert _route_iteration_budget(route, requested) <= requested
