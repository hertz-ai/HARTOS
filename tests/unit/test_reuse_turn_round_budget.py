"""A reuse turn's round budget must come from the recipe, not a literal 4.

Both reuse loops in ``get_agent_response`` capped a turn with
``if count == 4: break``.  The fabrication gate already allows
``_REUSE_FAB_STEER_MAX`` (3) re-steers per action before it advances anyway,
so ONE action can honestly consume 4 rounds — the old cap budgeted the whole
turn for what a single action may need.

Measured live 2026-09-05, driving every saved agent through POST /chat: six
agents stopped at EXACTLY action 4 no matter how long their recipe was —

    89555447799  4/24     52612946585  4/15     18895904180  4/15
    19166205319  4/6      22979930562  4/6      70264903070  4/6

and the only two recorded as "finished" were the two whose recipes are
SHORTER than the cap (20260824301 at 4/3, 45620673143 at 4/2).  Their log
slices carry ``state_transition with action id 1..4``, so advancement was
working; the turn just ran out of rounds.  75 of 127 saved agents have >= 2
actions and the largest has 24.

    python -m pytest tests/unit/test_reuse_turn_round_budget.py --noconftest -q
"""
import ast
import os

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_REUSE_SRC = os.path.join(_ROOT, 'hartos', 'reuse_recipe.py')


def _source():
    with open(_REUSE_SRC, encoding='utf-8') as fh:
        return fh.read()


class TestNoFixedCapRemains:

    def test_no_literal_count_equals_four_cap(self):
        """The exact expression that truncated the population.

        Matched over the AST, not the text: prose may legitimately quote the
        old ``count == 4`` while explaining why it went (this file's own
        docstring does). Only a real comparison in executable code counts.
        """
        hits = []
        for node in ast.walk(ast.parse(_source())):
            if not isinstance(node, ast.Compare):
                continue
            if not (isinstance(node.left, ast.Name) and node.left.id == 'count'):
                continue
            if not (len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)):
                continue
            rhs = node.comparators[0]
            if isinstance(rhs, ast.Constant) and rhs.value == 4:
                hits.append(node.lineno)
        assert not hits, (
            'a literal `count == 4` turn cap is back at '
            + ', '.join(f'line {i}' for i in hits)
            + '. It ends a turn after 4 rounds regardless of recipe length — '
              'measured live: six agents stalled at exactly action 4 '
              '(4/24, 4/15, 4/15, 4/6, 4/6, 4/6).')

    def test_both_loops_consult_the_recipe_derived_budget(self):
        """Both while-loops, not just the first, must use the budget."""
        src = _source()
        assert src.count('_reuse_turn_round_budget(') >= 3, (
            'expected the helper definition plus a call in EACH reuse loop; '
            'a loop still carrying its own cap will truncate long recipes.')
        assert src.count('count >= _round_budget') == 2, (
            'both reuse loops must bound themselves by _round_budget')


class TestBudgetScalesWithTheRecipe:

    def _budget(self, n_actions):
        rr = pytest.importorskip('hartos.reuse_recipe')

        class _Task:
            actions = [{'action_id': i + 1} for i in range(n_actions)]

        rr.user_tasks['probe_prompt'] = _Task()
        try:
            return rr._reuse_turn_round_budget('probe_prompt')
        finally:
            rr.user_tasks.pop('probe_prompt', None)

    @pytest.mark.parametrize('n_actions', [2, 3, 6, 15, 24])
    def test_budget_exceeds_action_count(self, n_actions):
        """Every action needs at least one round; the old cap of 4 did not."""
        budget = self._budget(n_actions)
        assert budget > n_actions, (
            f'{n_actions}-action recipe got a {budget}-round budget — it '
            'cannot finish. This is the live 4/24 and 4/15 signature.')

    def test_budget_allows_the_gates_own_resteers(self):
        """One action may honestly consume _REUSE_FAB_STEER_MAX + 1 rounds."""
        rr = pytest.importorskip('hartos.reuse_recipe')
        per_action = rr._REUSE_FAB_STEER_MAX + 1
        assert self._budget(6) >= 6 * per_action, (
            'budget must let every action use its full re-steer allowance, '
            'otherwise the fabrication gate and the round cap fight and a '
            'legitimately re-steered action silently loses the turn.')

    def test_single_action_keeps_its_old_headroom(self):
        """No regression for the recipes the old cap did fit."""
        assert self._budget(1) >= 4

    def test_missing_task_entry_does_not_raise(self):
        """Budget is read on a hot path; an absent session must not crash it."""
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._reuse_turn_round_budget('no_such_prompt') >= 4
