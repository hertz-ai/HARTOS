"""A turn that NAMES a core tool must get that core tool attached.

MEASURED LIVE 2026-09-07/08 on the installed build, 23 agents driven through
REUSE via /chat, 672,846 server.log lines (server.log only -- gui_app.log is a
subset of it, and summing both double-counts):

    FAB-GUARD, tool named by an action        named  unrun  unrun%
    execute_windows_or_android_command           35     27     77%   <- NOT in
    google_search                                13      0      0%      MAIN_LEG
    send_message_to_user                          5      2     40%
    save_to_long_term_memory                      5      2     40%

    named tools IN  MAIN_LEG_CORE_TOOLS:  23 named,  4 unrun -> 17%
    named tools NOT in that frozenset  :  35 named, 27 unrun -> 77%

The split is the whole defect.  reuse_recipe.py builds ONE closure list and
hands the legs different slices:

    :2141  core_tools = build_core_tool_closures(_tool_ctx)   # full set
    :2142  register_core_tools(core_tools, helper1, time_agent)      # ALL
    :2167  register_core_tools(main_leg_core_tools(core_tools), ...) # 18 only
    :2250  register_core_tools(core_tools, helper2, visual_agent)    # ALL

So execute_windows_or_android_command IS registered -- on the time and visual
legs (which is why it was measured firing 38x, task #781) -- but NOT on the
MAIN leg, the one that actually runs recipe actions.  An action that names it
can never call it, StatusVerifier then honestly reports 'pending' forever, and
the turn burns its whole round budget on one action (tasks #770, #790).

The per-turn named attach was supposed to cover exactly this, and cannot:
attach_for_names iterates ``registry._tools`` ONLY -- the SERVICE registry,
13 names on this deployment (payments x3, seo_audit_score, gh_pr_open,
crawl4ai, crawl4ai_crawl, pocket_tts x3, acestep x3).  Core closures are not
in it.  Live proof across the same 23 drives: "Tier-1 named attach" logged
ZERO times, with "turn attach skipped" also zero -- so the block ran and the
matcher simply resolved nothing.

WHY NOT JUST ADD IT TO MAIN_LEG_CORE_TOOLS: that frozenset is the ALWAYS-ON
set for every agent on the main leg, and this tool executes arbitrary OS
commands.  Granting it globally is a security-posture change, not a bug fix.
Attaching it only for an action whose own recipe names it keeps the blast
radius at the action that asked.

    python -m pytest tests/unit/test_attach_names_reaches_core_tools.py -q
"""
import pytest

from core.agent_tools import MAIN_LEG_CORE_TOOLS, attach_for_names


class _FakeAgent:
    """Minimal stand-in for an AutoGen agent's registration surface."""

    def __init__(self, name):
        self.name = name
        self.llm_registered = []
        self.exec_registered = []

    def register_for_llm(self, name=None, description=None):
        def _wrap(func):
            self.llm_registered.append(name)
            return func
        return _wrap

    def register_for_execution(self, name=None):
        def _wrap(func):
            self.exec_registered.append(name)
            return func
        return _wrap


class _EmptyRegistry:
    """The live service registry as it is for these names: does not hold them.

    Not a strawman -- measured on the running app, the service registry holds
    13 names and none of them is a core tool.
    """

    _tools = {}

    def create_endpoint_function(self, tool_name, ep_name):  # pragma: no cover
        return None


def _core_closures():
    """(name, description, func) tuples, the shape build_core_tool_closures returns."""
    return [
        ('execute_windows_or_android_command', 'run an OS command', lambda **kw: 'ok'),
        ('google_search', 'search the web', lambda **kw: 'ok'),
        ('get_text_from_image', 'ocr', lambda **kw: 'ok'),
    ]


class TestTheGapIsReal:
    """Guards on the measured facts, so a future edit cannot quietly undo them."""

    def test_execute_command_is_absent_from_the_main_leg_set(self):
        """77% unrun tool is not in the always-on set -- the defect's precondition."""
        assert 'execute_windows_or_android_command' not in MAIN_LEG_CORE_TOOLS, (
            "if this tool has been added to the always-on main-leg set, that is a "
            "SECURITY POSTURE change (arbitrary OS command execution for every "
            "agent) -- it must be a deliberate owner decision, not a silent edit")

    def test_google_search_is_present(self):
        """Control: the 0%-unrun tool IS in the set. Pins the correlation."""
        assert 'google_search' in MAIN_LEG_CORE_TOOLS


class TestAttachForNamesReachesCoreTools:
    """RED before the fix: a named CORE tool is not attached by any path."""

    def test_named_core_tool_gets_attached(self):
        helper, executor = _FakeAgent('helper'), _FakeAgent('assistant')
        attached = set()

        n = attach_for_names(
            ['execute_windows_or_android_command'],
            helper, executor, _EmptyRegistry(), attached,
            core_tools=_core_closures(),
        )

        assert n == 1, (
            "an action naming execute_windows_or_android_command attached %d "
            "tools; the service registry does not hold core closures, so "
            "attach_for_names must also resolve against the core set" % n)
        assert 'execute_windows_or_android_command' in helper.llm_registered, (
            "the helper carries the SCHEMA, or the model is never offered the tool")
        assert 'execute_windows_or_android_command' in executor.exec_registered, (
            "the executor carries EXECUTION, or the call is proposed and stranded")

    def test_only_the_named_tool_is_attached(self):
        """Blast radius: naming one tool must not hand over the whole core set."""
        helper, executor = _FakeAgent('helper'), _FakeAgent('assistant')
        attached = set()

        attach_for_names(['execute_windows_or_android_command'],
                         helper, executor, _EmptyRegistry(), attached,
                         core_tools=_core_closures())

        assert helper.llm_registered == ['execute_windows_or_android_command'], (
            "attaching by name must attach ONLY what the action named; got %r"
            % (helper.llm_registered,))

    def test_idempotent_across_rounds(self):
        """The hook runs every round; a second pass must not re-register."""
        helper, executor = _FakeAgent('helper'), _FakeAgent('assistant')
        attached = set()
        args = (['execute_windows_or_android_command'], helper, executor,
                _EmptyRegistry(), attached)

        first = attach_for_names(*args, core_tools=_core_closures())
        second = attach_for_names(*args, core_tools=_core_closures())

        assert (first, second) == (1, 0), (
            "expected 1 then 0; re-attaching every round duplicates the schema "
            "and inflates the wire body (got %d then %d)" % (first, second))

    def test_unknown_name_is_ignored_not_raised(self):
        """A recipe may name a tool this deployment does not ship."""
        helper, executor = _FakeAgent('helper'), _FakeAgent('assistant')
        assert attach_for_names(['no_such_tool_anywhere'], helper, executor,
                                _EmptyRegistry(), set(),
                                core_tools=_core_closures()) == 0
        assert helper.llm_registered == []

    @pytest.mark.parametrize('core', [None, []])
    def test_core_tools_optional_keeps_old_behaviour(self, core):
        """Defaults must be a strict no-op for the service-registry path."""
        helper, executor = _FakeAgent('helper'), _FakeAgent('assistant')
        assert attach_for_names(['execute_windows_or_android_command'],
                                helper, executor, _EmptyRegistry(), set(),
                                core_tools=core) == 0
