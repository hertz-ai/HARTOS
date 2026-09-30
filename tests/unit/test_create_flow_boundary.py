"""A finished flow must END the create loop, not re-author itself forever.

THE LIVE FAILURE THIS ENCODES (2026-09-10, installed build, agent 28160128202):

    Current Flow -> recipe_for_persona[user_prompt]:0  total_persona_actions[...]:1
    [NEXT-FLOW] Completed flow 0, starting next     x9   17:28:44 .. 17:49:04
    [ALL-FLOWS-DONE]                                x0
    [AUTO-ADVANCE] action 1..5 already terminated, advancing
    [FLOW-COMPLETE] All 5 actions done in flow, ensuring termination
    [FLOW-COMPLETE] All actions terminated, creating flow recipe
    ... and again, every ~2.5 minutes, for 27 minutes.

`get_current_flow()` is 0-BASED; `get_total_flows()` is a COUNT.  The two
inline guards (create_recipe.py :4864 and :5084) compared them directly, so on
the LAST flow of a 1-flow agent the test read `0 < 1` -> TRUE, the terminal
`[ALL-FLOWS-DONE] ... return` was unreachable, and the branch re-read
`config['flows'][get_current_flow(...)]` -- the SAME flow -- rebuilt the ledger
and recursed into get_response_group.  Neither site incremented the flow, so
even a genuine multi-flow agent would re-walk flow 0 forever.

Scope: EVERY single-flow agent, i.e. essentially all of them.

The correct idiom was already in the file: the exception path (:4896) checks
completion, then calls safe_increment_flow() BEFORE re-reading the config.
"""

import ast
import io
import os
import sys
import unittest

import hartos.create_recipe  # noqa: F401  (ensure cached)

cr = sys.modules['hartos.create_recipe']
_SRC_PATH = os.path.join(os.path.dirname(cr.__file__), 'create_recipe.py')
NL = chr(10)


class TestHasMoreFlows(unittest.TestCase):
    """ONE predicate, both call sites -- a second copy is what drifted."""

    _UP = 'u_flowboundary'

    def tearDown(self):
        cr.recipe_for_persona.pop(self._UP, None)
        cr.total_persona_actions.pop(self._UP, None)

    def _at(self, current, total):
        cr.recipe_for_persona[self._UP] = current
        cr.total_persona_actions[self._UP] = total

    def test_single_flow_agent_is_done_on_flow_zero(self):
        """THE LIVE SHAPE: current=0, total=1. `0 < 1` said 'keep going'."""
        self._at(0, 1)
        self.assertFalse(
            cr._has_more_flows(self._UP),
            'a 1-flow agent sitting on flow 0 was told another flow remains — '
            'this is the re-walk that ran 9 times in 27 minutes')

    def test_two_flow_agent_advances_from_the_first(self):
        self._at(0, 2)
        self.assertTrue(cr._has_more_flows(self._UP))

    def test_two_flow_agent_is_done_on_the_last(self):
        self._at(1, 2)
        self.assertFalse(cr._has_more_flows(self._UP))

    def test_a_zero_flow_config_is_done(self):
        """Degenerate, but it must not read as 'one more flow'."""
        self._at(0, 0)
        self.assertFalse(cr._has_more_flows(self._UP))


def _module_ast():
    return ast.parse(io.open(_SRC_PATH, encoding='utf-8', errors='replace').read())


def _bare_flow_comparisons(tree):
    """Compare nodes of the exact broken shape `get_current_flow(x) < get_total_flows(x)`."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], ast.Lt):
            continue

        def _callee(n):
            return (n.func.id if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name) else None)

        if (_callee(node.left) == 'get_current_flow'
                and _callee(node.comparators[0]) == 'get_total_flows'):
            found.append(getattr(node, 'lineno', -1))
    return found


class TestTheBrokenComparisonIsGone(unittest.TestCase):
    """Drift guard: the off-by-one must not come back at a third site."""

    def test_no_bare_current_lt_total_comparison_remains(self):
        lines = _bare_flow_comparisons(_module_ast())
        self.assertEqual(
            lines, [],
            'create_recipe.py still compares a 0-based flow index against a '
            'flow COUNT at line(s) %s — that is TRUE on the last flow and '
            'makes [ALL-FLOWS-DONE] unreachable. Use _has_more_flows().'
            % lines)


# The flow index moves by one of two idioms in this file: the checked
# `safe_increment_flow()` (which also re-ASSIGNs the new flow's actions), and a
# bare `recipe_for_persona[user_prompt] += 1` in the resume helper.  The
# invariant is that it MOVED, not which idiom moved it.
_INDEX_MOVERS = ('safe_increment_flow', 'recipe_for_persona[user_prompt] += 1')


class TestEveryNextFlowBranchMovesTheIndex(unittest.TestCase):
    """Re-reading the config without moving the index re-authors the SAME flow.

    Scoped to FLOW TRANSITIONS, identified by their own signature: the branch
    drops the agents (`del user_agents[user_prompt]`) to rebuild them for a
    different flow.  Plain reads of the CURRENT flow -- e.g. the persona lookup
    at :932 -- are not transitions and are deliberately left alone.
    """

    def test_flow_transition_branches_move_the_index_first(self):
        src = io.open(_SRC_PATH, encoding='utf-8', errors='replace').read()
        lines = src.splitlines()
        anchor = "config['flows'][get_current_flow(user_prompt)]"
        rereads = [i for i, ln in enumerate(lines) if anchor in ln]
        self.assertTrue(rereads, 'anchor line vanished -- re-point this guard')
        transitions = [
            i for i in rereads
            if 'del user_agents[user_prompt]' in NL.join(lines[i:i + 14])]
        self.assertGreaterEqual(
            len(transitions), 3,
            'expected the 3 known flow-transition branches; found %d -- the '
            'guard has drifted off its anchors' % len(transitions))
        for i in transitions:
            window = NL.join(lines[max(0, i - 14):i])
            self.assertTrue(
                any(m in window for m in _INDEX_MOVERS),
                'line %d starts a new flow but nothing moved the flow index '
                'first -- it will re-author the flow that just finished'
                % (i + 1))


if __name__ == '__main__':
    unittest.main()
