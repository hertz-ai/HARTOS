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


# ── the narrow must FOLLOW the ledger, not the turn ───────────────────
# MEASURED 2026-09-08 20:21-20:27, same agent, on the build that already
# carried the narrowing above.  The turn entered on action 1, narrowed
# correctly (57,817 -> 16,744 chars), then the group chat advanced INSIDE
# the same autogen loop:
#
#     20:22:19  [REUSE] Action 1 TERMINATED, advancing
#     20:24:07  [FAB-GUARD] action 2 names ['execute_windows_or_android_command']
#
# but the narrow is applied once per get_agent_response entry, and the
# group chat's rounds never re-enter it.  Of 37 recipe-bearing calls on
# the wire, 32 were AFTER the advance and every one of them still carried
# 'action_id': 1.  Same class of defect as the [13..24] slice it replaced:
# the prompt names an action the pipeline is no longer on.
#
# The fix hangs the narrow off the ledger field itself
# (user_tasks[user_prompt].current_action) — the same single authority the
# scheduler writes at _advance_reuse_action, so the two cannot disagree.


class _FakeAssistant:
    """Enough of ConversableAgent to observe the effect.

    update_system_message is a real METHOD here on purpose: assigning to it
    (rather than calling it) shadows the bound method — create_recipe.py:5328
    records that trap — and this stand-in turns that mistake into a failure
    instead of a silent no-op.
    """

    def __init__(self, system_message, individual_recipe):
        self.system_message = system_message
        self._hart_individual_recipe = individual_recipe

    def update_system_message(self, msg):
        self.system_message = msg


class _FakeTask:
    def __init__(self, current_action):
        self.current_action = current_action


class TestNarrowFollowsTheLedger:

    def _wire(self, monkeypatch, current_action, recipes=RECIPES):
        from hartos import reuse_recipe as rr
        a = _FakeAssistant(_sysmsg(recipes), recipes)
        monkeypatch.setitem(rr.user_agents, 'u1', (a,) + (None,) * 11)
        monkeypatch.setitem(rr.user_tasks, 'u1', _FakeTask(current_action))
        return rr, a

    def test_narrows_to_the_action_the_ledger_holds(self, monkeypatch):
        rr, a = self._wire(monkeypatch, 2)
        assert rr._narrow_assistant_to_current_action('u1') is True
        assert "'action_id': 2" in a.system_message
        assert "'action_id': 1," not in a.system_message

    def test_renarrows_after_an_advance(self, monkeypatch):
        """The live sequence: narrow to 1, ledger advances, narrow to 2.

        The second call starts from an ALREADY-narrowed prompt — if the
        surgery could only run once (e.g. keyed on the full recipe list
        being present) the agent would stay pinned to action 1 exactly as
        measured on the wire.
        """
        rr, a = self._wire(monkeypatch, 1)
        assert rr._narrow_assistant_to_current_action('u1') is True
        assert "'action_id': 1" in a.system_message
        rr.user_tasks['u1'].current_action = 2          # _advance_reuse_action
        assert rr._narrow_assistant_to_current_action('u1') is True
        assert "'action_id': 2" in a.system_message, (
            "re-narrow must track the advance; pinning at the entry action is "
            "the 32-of-37 defect this guard exists for")

    def test_second_call_on_the_same_action_is_a_no_op(self, monkeypatch):
        rr, a = self._wire(monkeypatch, 2)
        assert rr._narrow_assistant_to_current_action('u1') is True
        before = a.system_message
        assert rr._narrow_assistant_to_current_action('u1') is False
        assert a.system_message == before

    @pytest.mark.parametrize("prompt", ['unknown-user', None])
    def test_unknown_user_returns_false_without_raising(self, monkeypatch, prompt):
        rr, _ = self._wire(monkeypatch, 1)
        assert rr._narrow_assistant_to_current_action(prompt) is False

    def test_no_recipe_stashed_returns_false_without_raising(self, monkeypatch):
        rr, a = self._wire(monkeypatch, 1)
        del a._hart_individual_recipe
        assert rr._narrow_assistant_to_current_action('u1') is False
        assert a.system_message == _sysmsg(RECIPES)


class TestEveryDispatchSiteNarrows:
    """Structure guard: source structure IS the subject here.

    A dispatch that commands action N while the prompt still describes
    action N-1 is the whole defect, so every site that posts an action
    command must re-pin the prompt first.  Behavioural coverage lives
    above; this catches a NEW dispatch site added without the narrow.
    """

    def _fn(self, name):
        import ast
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[2]
               / 'hartos' / 'reuse_recipe.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        return next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name), ast

    def test_advance_or_steer_repins_the_prompt(self):
        fn, ast = self._fn('_advance_or_steer')
        called = [n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        assert '_narrow_assistant_to_current_action' in called, (
            "_advance_or_steer posts the NEXT action's command; without a "
            "re-pin the agent reads the previous action's recipe")

    def test_one_narrow_implementation_not_two(self):
        """get_agent_response must call the helper, not re-inline it."""
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[2]
               / 'hartos' / 'reuse_recipe.py').read_text(encoding='utf-8')
        assert src.count('.update_system_message(') <= 2, (
            "update_system_message should be called from the narrow helper "
            "(plus the pre-existing verify site) — a second inline copy is "
            "the parallel path this consolidation removed")
