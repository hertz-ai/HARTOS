"""The reuse system prompt must carry the action being dispatched.

MEASURED AT THE WIRE 2026-09-08 19:44:31 on the installed build, agent
89555447799 (growth.local.executor, 24 actions), driven live with its own
goal.  The outbound body had two messages:

    [0] role=system  len=18,961  recipe entries for action_id [13..24] ONLY
    [1] role=user    len=247     "Perform this action -> Action #2: Open the
                                  LinkedIn web interface in a browser"

The model was asked for action 2 while holding the recipes for 13-24, and
answered the user:

    "those actions do not exist in my immediate view ... please paste the
     list of specific actions"

It was telling the truth about its context.

WHY THE SLICE IS 13-24 — every link measured, none inferred:

    wire-trim: the TOOL SCHEMA alone is 8487 tokens against an n_ctx of
    12288 (66 tool(s)) -- no amount of message trimming can make this fit

    [TRIM] left-trimmed 13 msg(s) + 39363 char(s)
           est tokens 15879 -> 4308, budget 5484

66 tools eat 69% of the window; the recipe prompt is ~15,879 est tokens
against a 5,484 budget; the trim removes from the FRONT, so the early
actions go first and the tail survives.

NOT the loader: 24 load attempts, 0 errors, indices 1..24 complete, all 24
files present and json.load()-clean.  individual_recipe was built whole.

NOT the trim's fault either: it is doing its job against an impossible
budget.  The defect this file guards is upstream of both — the agent is
asked for ONE action and its prompt carries TWENTY-FOUR recipes.  Sending
the dispatched action's entry is smaller and strictly more correct; the
action-title list (role_actions) still carries the overall plan.

    python -m pytest tests/unit/test_reuse_prompt_carries_current_action.py \
        --noconftest -q
"""
import pytest

from hartos.reuse_recipe import _recipe_section_for_action


RECIPES = [
    {'action_id': i, 'recipe': [{'steps': 'do step %d' % i}],
     'persona': 'Executor'}
    for i in range(1, 25)
]

# The delimiters the live template uses (reuse_recipe.py:1310).  Real system
# messages wrap the recipe in these, so the surgery keys on them.
def _sysmsg(payload):
    return (
        "You are a Helpful Assistant.\n"
        "        Actions: <actionsStart>['a1','a2']<actionEnd>\n"
        "        Recipe  & generalized_functions: <recipeStart>"
        "<generalized_functionsStart>" + str(payload) +
        "<generalized_functionsEnd><recipeEnd>\n"
        "        PREVIOUS EXPERIENCE: none.\n"
    )


class TestNarrowsToTheDispatchedAction:

    def test_only_the_current_action_survives(self):
        out = _recipe_section_for_action(_sysmsg(RECIPES), RECIPES, 2)
        assert "'action_id': 2" in out, (
            "the dispatched action's recipe must be present -- this is the "
            "exact entry the live trim removed")
        for gone in (13, 24):
            assert "'action_id': %d" % gone not in out, (
                "action %d is not the dispatched action and must not be sent; "
                "carrying all 24 is what pushed the body past the budget" % gone)

    def test_everything_outside_the_recipe_block_is_preserved(self):
        src = _sysmsg(RECIPES)
        out = _recipe_section_for_action(src, RECIPES, 2)
        for keep in ("You are a Helpful Assistant.",
                     "Actions: <actionsStart>['a1','a2']<actionEnd>",
                     "PREVIOUS EXPERIENCE: none."):
            assert keep in out, (
                "surgery must replace ONLY the recipe block; lost: %r" % keep)

    def test_it_actually_shrinks_the_prompt(self):
        src = _sysmsg(RECIPES)
        out = _recipe_section_for_action(src, RECIPES, 2)
        assert len(out) < len(src), (
            "narrowing 24 recipes to 1 must reduce the prompt; got %d -> %d"
            % (len(src), len(out)))

    @pytest.mark.parametrize("aid", [1, 12, 13, 24])
    def test_every_action_id_is_reachable(self, aid):
        """The live failure was action 2 with 13-24 present. Any id must work."""
        out = _recipe_section_for_action(_sysmsg(RECIPES), RECIPES, aid)
        assert "'action_id': %d" % aid in out


class TestNeverRaisesOnTheHotPath:
    """This runs on every reuse turn; it must not be able to kill one."""

    @pytest.mark.parametrize("aid", [0, 25, -1, None])
    def test_out_of_range_returns_original_unchanged(self, aid):
        src = _sysmsg(RECIPES)
        assert _recipe_section_for_action(src, RECIPES, aid) == src

    def test_missing_delimiters_returns_original_unchanged(self):
        src = "a system prompt with no recipe block at all"
        assert _recipe_section_for_action(src, RECIPES, 2) == src

    @pytest.mark.parametrize("recipes", [None, []])
    def test_empty_recipe_list_returns_original_unchanged(self, recipes):
        src = _sysmsg(RECIPES)
        assert _recipe_section_for_action(src, recipes, 2) == src

    def test_none_system_message_returns_it_unchanged(self):
        assert _recipe_section_for_action(None, RECIPES, 2) is None
