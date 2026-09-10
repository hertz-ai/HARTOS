"""A tool CREATE can author into a recipe must be a tool REUSE can execute.

THE LIVE FAILURE THIS ENCODES (2026-09-10, agent 88719487304, REUSE walk
rid walk-88719487304-210203):

    21:10:13  [FAB-GUARD] watermark for action 4: 23 pre-existing tool call(s)
    21:10:21  reuse-w1-completed: terminal 'completed' verdict for action 4
    21:10:21  [SUBTASK] action 4 subtask 4.1 -> completed
    21:10:27  [SUBTASK] action 4 subtask 4.2 -> completed
    21:10:27  [REUSE] Action 4 TERMINATED, advancing
    21:10:27  [FAB-GUARD] watermark for action 5: 23 pre-existing tool call(s)

Watermark 23 in, watermark 23 out: ZERO tool calls happened in action 4's
whole window.  Its recipe names ``execute_coding_task`` and that is the
action's entire job.  Action 4 is also the ONLY action of the nine with no
``[FAB-GUARD] action 4 names tool(s) ...`` verdict line at all.

Both facts have ONE cause.  ``execute_coding_task`` is registered only on the
CREATE leg (create_recipe.py register_dual).  It appears nowhere in
reuse_recipe.py and nowhere in build_core_tool_closures.  So on the REUSE leg:

  a) the tool cannot execute -- nothing holds the closure; and
  b) the fabrication gate cannot even NOTICE.  _reuse_fabricated_tools asks
     _reuse_registered_and_referenced_tools which names the action text
     references, and that helper intersects the text with names REGISTERED ON
     THE AGENTS.  An unregistered name is never `referenced`, so the guard
     returns [] at its second early-return -- before its log line.  A tool the
     leg cannot run is therefore indistinguishable from an action that names
     no tool, and the action advances silently.

Measured blast radius over the 185 saved recipes on this box: 36 agents
(19.5%) contain at least one action naming a tool REUSE cannot reach;
``execute_coding_task`` alone is named by 87 actions across 35 agents.

THE RULE THIS PINS, which the codebase already wrote down at
reuse_recipe.py:2429-2442 for execute_windows_or_android_command: a closure
that can be built from the ctx dict belongs in build_core_tool_closures, the
ONE factory both legs call (create_recipe.py:1096, reuse_recipe.py:2238).
Only a closure that cannot -- execute_windows_or_android_command closes over
33 locals of its defining function -- gets handed to _hart_core_tools inline.
All four coding closures close over nothing but ``user_id``, which the factory
already unpacks, so the factory is where they belong.

They are deliberately NOT added to MAIN_LEG_CORE_TOOLS: reuse attaches a
named tool per-turn via attach_for_names, so the always-on schema stays at 18
tools / ~1,859 tokens and the 12,288-token slot is unaffected (#730).
"""

import io
import os
import re
import unittest

import core.agent_tools as at

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The four closures create_recipe defines inline and reuse cannot reach.
# validate_json_response is create-only too but names 0 saved actions, so it
# is not in this set -- this list is the MEASURED user-facing gap.
CODING_TOOLS = (
    'execute_coding_task',
    'get_repository_map',
    'create_code_shard',
    'get_coding_benchmarks',
)


def _src(rel):
    return io.open(os.path.join(_HARTOS, rel), encoding='utf-8',
                   errors='replace').read()


def _factory_tool_names():
    """Names build_core_tool_closures appends, parsed from its own body."""
    src = _src(os.path.join('core', 'agent_tools.py'))
    body = src[src.index('def build_core_tool_closures('):]
    return set(re.findall(r'tools\.append\(\(\s*\n?\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']',
                          body))


def _register_dual_names(rel):
    """The tool names a module registers via register_dual(...)."""
    src = _src(rel)
    out = set()
    for m in re.finditer(r'register_dual\s*\((.{0,400}?)\)\s*\n', src, re.S):
        quoted = re.findall(r'["\']([A-Za-z_][A-Za-z0-9_]*)["\']', m.group(1))
        if quoted:
            out.add(quoted[0])
    return out


class TestCodingToolsLiveInTheSharedFactory(unittest.TestCase):
    """RED until the four closures move into build_core_tool_closures."""

    def test_factory_produces_every_coding_tool(self):
        names = _factory_tool_names()
        missing = [t for t in CODING_TOOLS if t not in names]
        self.assertEqual(
            missing, [],
            'build_core_tool_closures does not produce %s, so reuse_recipe\'s '
            'core_tools list (L2238) cannot hold them, assistant._hart_core_tools '
            '(L2445) cannot carry them, and attach_for_names can never attach '
            'them for an action whose recipe names them' % missing)

    def test_they_are_not_added_to_the_always_on_main_leg(self):
        """Named-attach only -- the 12,288-token slot must not grow (#730)."""
        overreach = [t for t in CODING_TOOLS if t in at.MAIN_LEG_CORE_TOOLS]
        self.assertEqual(
            overreach, [],
            '%s were put on the always-on main leg; they must be reachable '
            'ONLY through attach_for_names, for the action that names them'
            % overreach)


class TestNoToolIsCreateOnly(unittest.TestCase):
    """The general defect: CREATE authors a name REUSE cannot execute."""

    def test_every_create_registered_tool_is_reachable_from_reuse(self):
        factory = _factory_tool_names()
        reuse_src = _src(os.path.join('hartos', 'reuse_recipe.py'))
        offenders = []
        for name in sorted(_register_dual_names(os.path.join('hartos',
                                                             'create_recipe.py'))):
            if name in factory:
                continue          # both legs build it from the one factory
            # or reuse defines/registers its own copy in its own scope
            if re.search(r'\bdef\s+%s\b' % re.escape(name), reuse_src):
                continue
            if re.search(r'["\']%s["\']' % re.escape(name), reuse_src):
                continue
            offenders.append(name)
        self.assertEqual(
            offenders, [],
            'create_recipe registers %s, and the recipe-authoring LLM is told '
            'it may use them -- but reuse can neither execute them nor even '
            'see them as `referenced`, so FAB-GUARD returns [] and the action '
            'advances having run nothing (agent 88719487304 action 4, '
            '2026-09-10 21:10:13-21:10:27, watermark 23 -> 23)' % offenders)


if __name__ == '__main__':
    unittest.main()
