"""create and reuse must register the main leg's core tools IDENTICALLY.

Both pipelines build the same helper/assistant pair and hand it to the same
factory, core.agent_tools.register_core_tools.  That factory exists precisely
so the two legs cannot drift:

    core/agent_tools.py:115-116
        "ONE filter for both pipelines -- call this rather than re-deriving the
         name set, so create and reuse can never drift apart again."

They drifted anyway, on the one keyword that decides whether the agent the
recipe names as the actor can call a tool at all:

    reuse_recipe.py:2167   register_core_tools(..., helper, assistant,
                               executor_proposes=True, second_executor=executor)
    create_recipe.py:1104  register_core_tools(..., helper, assistant)

WHAT THE MISSING KEYWORD COSTS, quoted from the factory's own docstring
(core/agent_tools.py:134-143), which was written from a live measurement:

    "executor_proposes exists because the helper=schema / executor=execution
     split silently disarms whichever agent the recipe actually assigns the
     work to.  Measured live 2026-09-06, agent 89555447799: the main leg
     registered with (helper, assistant), so the Assistant held execution only
     and its outbound bodies carried NO tools[] at all -- while ~591
     execution-persona bodies in the same window named it as the actor
     ('agent_to_perform_this_action': 'Assistant').  Downstream that produced
     26x 'The requested tool google_search is not available' and 2,657+
     'Error: Function <X> not found' (send_message_to_user x1052 -- the path
     that returns the agent's result to the user; request_tools x101 ...)"

So the remedy was designed, implemented and applied to reuse — and create was
left on the disarmed wiring, with the byte-identical agent pair.

STILL LIVE 2026-09-07: 2,570 'Function send_message_to_user not found' in the
current log, up from the 1,052 recorded when the remedy was written.  Which
pipeline emitted them is NOT established here (the sweep's attribution is
broken), and this test does not claim it — the drift is a defect on its own
terms, because the factory's contract is that the two legs match.

WHY second_executor MATTERS TOO: without it the Assistant's own structured
tool_calls strand under autogen's repeat-speaker rule.  create_recipe.py:1016
already binds `executor = instantiate_executor_agent()`, 88 lines above the
call site, so this needs no new object — only the same argument reuse passes.

    python -m pytest tests/unit/test_main_leg_registration_symmetry.py --noconftest -q
"""
import re
import pytest


REPO = __file__.rsplit("tests", 1)[0]


def _main_leg_call(filename):
    """The main-leg register_core_tools(...) call, whole, across line breaks.

    The main leg is the one filtered by main_leg_core_tools(); the time/visual
    legs call register_core_tools with the unfiltered list and are NOT this
    test's subject.
    """
    text = open(REPO + filename, encoding="utf-8", errors="replace").read()
    i = text.index("register_core_tools(main_leg_core_tools(")
    # balance parens from the opening one so multi-line calls are captured whole
    start = text.index("(", i)
    depth, j = 0, start
    while j < len(text):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return text[i:j + 1]


class TestMainLegSymmetry:

    def test_reuse_arms_the_actor(self):
        """The known-good side — guards against losing it."""
        call = _main_leg_call("hartos/reuse_recipe.py")
        assert "executor_proposes=True" in call
        assert "second_executor=" in call

    def test_create_arms_the_actor_too(self):
        """The drifted side: same pair, same factory, must be same wiring."""
        call = _main_leg_call("hartos/create_recipe.py")
        assert "executor_proposes=True" in call, (
            "create's main leg registers (helper, assistant) without "
            "executor_proposes, so the Assistant holds execution only and its "
            "outbound bodies carry no tools[] — the exact wiring the factory's "
            "docstring documents as producing 'Function <X> not found'")
        assert "second_executor=" in call, (
            "without second_executor the Assistant's own structured tool_calls "
            "strand under autogen's repeat-speaker rule")

    def test_both_legs_pass_the_same_keywords(self):
        """The real invariant — not two independent line assertions.

        A future edit that arms one side and not the other should fail here
        even if it satisfies the two tests above individually.
        """
        kw = re.compile(r"(\w+)\s*=")
        create = set(kw.findall(_main_leg_call("hartos/create_recipe.py")))
        reuse = set(kw.findall(_main_leg_call("hartos/reuse_recipe.py")))
        assert create == reuse, (
            f"main-leg registration drifted: only in reuse={sorted(reuse - create)}, "
            f"only in create={sorted(create - reuse)}")


class TestExecutorIsInScope:

    def test_create_binds_an_executor_before_the_call(self):
        """second_executor=executor needs `executor` to already exist.

        Measured: create_recipe.py:1016 `executor = instantiate_executor_agent()`
        precedes the :1104 call site, so no new object is introduced.
        """
        text = open(REPO + "hartos/create_recipe.py",
                    encoding="utf-8", errors="replace").read()
        bind = text.index("executor = instantiate_executor_agent()")
        call = text.index("register_core_tools(main_leg_core_tools(")
        assert bind < call, (
            "`executor` must be bound before the main-leg registration; "
            "otherwise second_executor=executor is a NameError")
