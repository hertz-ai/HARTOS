"""The fabrication gate must SEE a tool the action names and the leg can attach.

THE LIVE FAILURE THIS ENCODES (2026-09-10, agent 88719487304 action 4, rid
d60c-223537 — the SECOND drive, taken AFTER 7fd83678f was deployed and
byte-verified):

    22:38:50,363  [FAB-GUARD] watermark for action 4: 12 pre-existing tool call(s)
    22:39:12,160  reuse-w1-completed: terminal 'completed' verdict for action 4
    22:39:12,342  [REUSE] Action 4 TERMINATED, advancing
    22:39:12,344  [FAB-GUARD] watermark for action 5: 13

Action 4 is the ONLY action of the nine with no ``[FAB-GUARD] action 4 names
tool(s) ...; executed=[...]; unrun=[...]`` verdict line — actions 2,3,5,6,7,8,9
all emit one on that same drive.  Its recipe names ``execute_coding_task`` and
that is the action's whole job.

WHY 7fd83678f WAS NOT ENOUGH.  That commit moved the four coding closures into
``build_core_tool_closures``, so reuse can now BUILD them — they reach
``assistant._hart_core_tools`` (reuse_recipe.py:2445) and ``attach_for_names``
can attach them by name.  But ``_reuse_registered_and_referenced_tools``
derives its ``names`` set from only two places:

    ag._function_map.keys()                     -- already REGISTERED
    ag.llm_config['tools'][].function.name      -- already in the SCHEMA

A closure that is attachABLE but not yet attached is in neither.  So
``referenced`` comes back empty, ``_reuse_fabricated_tools`` returns [] at its
``if not referenced: return []`` early-return — which sits ABOVE its log line —
and an action naming a tool the leg could have run is indistinguishable from a
prose action naming no tool at all.  It advances having run nothing, silently.

MEASURED BLAST RADIUS on this box: 27 of 187 saved recipes cite an
Agent-Lightning span id as a source file (task #824), and 49 of those 27
agents' 84 actions name ``execute_coding_task``; separately 87 actions across
35 agents name it.  Every one of them takes this early-return today.

THE RULE THIS PINS.  "Which tools does this action name?" must be answered
against the same set ``attach_for_names`` can actually serve — registry names
PLUS the ``(name, description, func)`` core closures the leg carries.  Anything
narrower makes the gate structurally blind to exactly the tools D58 just made
reachable.

THE CONTRACT IS SEE, NOT PASS.  Widening the name set does not wave an action
through; it lets the gate ASK the question.  An action naming a tool that then
does not run gets a real ``unrun=[...]`` verdict and the honest bounded
re-steer (D42/#808), instead of a silent advance.
"""

import unittest

from hartos.reuse_recipe import _reuse_registered_and_referenced_tools


def _noop(*a, **k):
    return None


class _Agent(object):
    """Minimal stand-in carrying only the attributes the derivation reads."""

    def __init__(self, function_map=None, schema_tools=None, core_tools=None):
        self._function_map = dict(function_map or {})
        self.llm_config = {'tools': [
            {'type': 'function', 'function': {'name': n}}
            for n in (schema_tools or [])
        ]}
        if core_tools is not None:
            self._hart_core_tools = list(core_tools)


# The action text is the real one, verbatim from agent 89055815944 action 1.
_ACTION_TEXT = ("execute_coding_task: read source file "
                "llm/reuse_recipe_assistant_c23d388c-07a0-4a79-816d-5b9564")


class TestAttachableToolsAreVisibleToTheGate(unittest.TestCase):
    """RED until the derivation also reads the leg's core closures."""

    def test_a_tool_only_attachable_is_still_referenced(self):
        ag = _Agent(function_map={}, schema_tools=[],
                    core_tools=[('execute_coding_task', 'run a coding task',
                                 _noop)])
        names, referenced = _reuse_registered_and_referenced_tools(
            [ag], _ACTION_TEXT)
        self.assertIn(
            'execute_coding_task', names,
            'the name set ignores assistant._hart_core_tools, so a closure the '
            'leg can attach by name (attach_for_names, core_tools=) is invisible '
            'to the gate')
        self.assertIn(
            'execute_coding_task', referenced,
            'action 4 of agent 88719487304 names execute_coding_task and got NO '
            'FAB-GUARD verdict line at all on the 2026-09-10 22:38 drive, '
            'because referenced==[] returns before the log line')

    def test_the_tuple_name_is_read_not_the_description(self):
        """attach_for_names unpacks (name, description, func); match it."""
        ag = _Agent(core_tools=[('get_repository_map',
                                 'execute_coding_task lives here too', _noop)])
        names, _ = _reuse_registered_and_referenced_tools([ag], _ACTION_TEXT)
        self.assertIn('get_repository_map', names)
        self.assertNotIn(
            'execute_coding_task lives here too', names,
            'the DESCRIPTION was read as a name; core closures are '
            '(name, description, func) and only [0] is the name')


class TestNoRegression(unittest.TestCase):
    """The two existing sources must keep working exactly as before."""

    def test_registered_function_map_still_counted(self):
        ag = _Agent(function_map={'execute_coding_task': _noop})
        names, referenced = _reuse_registered_and_referenced_tools(
            [ag], _ACTION_TEXT)
        self.assertIn('execute_coding_task', names)
        self.assertIn('execute_coding_task', referenced)

    def test_llm_config_schema_still_counted(self):
        ag = _Agent(schema_tools=['execute_coding_task'])
        names, referenced = _reuse_registered_and_referenced_tools(
            [ag], _ACTION_TEXT)
        self.assertIn('execute_coding_task', names)
        self.assertIn('execute_coding_task', referenced)

    def test_an_agent_without_core_tools_does_not_raise(self):
        """Most agents never get the attribute; absence is normal, not an error."""
        ag = _Agent(function_map={'google_search': _noop})   # no _hart_core_tools
        names, referenced = _reuse_registered_and_referenced_tools(
            [ag], 'run google_search on the topic')
        self.assertEqual(referenced, ['google_search'])

    def test_a_tool_the_action_does_not_name_is_not_referenced(self):
        """Widening the NAME set must not widen the REFERENCED set."""
        ag = _Agent(core_tools=[('get_coding_benchmarks', 'd', _noop),
                                ('execute_coding_task', 'd', _noop)])
        names, referenced = _reuse_registered_and_referenced_tools(
            [ag], _ACTION_TEXT)
        self.assertIn('get_coding_benchmarks', names)
        self.assertNotIn(
            'get_coding_benchmarks', referenced,
            'a closure the leg carries but this action never names must stay '
            'out of referenced — otherwise every action would be held on tools '
            'it never asked for')


if __name__ == '__main__':
    unittest.main()
