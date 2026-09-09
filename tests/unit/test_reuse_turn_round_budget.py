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


class TestBudgetIsSpentPerAction:
    """#790/D23 — the allowance is PER ACTION, so whichever action runs first
    cannot spend the whole turn.

    Measured on agent 33323830039, twice within one hour, both ending
    `exhausted 12 rounds at action 2/2`:

      04:48 drive   action 1 used 2 while1 iterations, action 2 used 14
      05:31 drive   the other way round — action 1 did the REAL work
                    (tool executed, one fabrication refusal at 05:30:01, one
                    under-report re-steer at 05:30:43, honest advance at
                    05:30:49) and action 2 got what was left, ~12 seconds

    Whichever action goes first spends the turn.  The old formula made that
    inevitable: for a 2-action recipe the turn total (n*4+4 = 12) equalled one
    action's measured honest need (12).
    """

    def _budget(self, n_actions):
        rr = pytest.importorskip('hartos.reuse_recipe')

        class _Task:
            actions = [{'action_id': i + 1} for i in range(n_actions)]

        rr.user_tasks['probe_prompt'] = _Task()
        try:
            return rr._reuse_turn_round_budget('probe_prompt')
        finally:
            rr.user_tasks.pop('probe_prompt', None)

    def test_per_action_allowance_covers_the_measured_need(self):
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._REUSE_ROUNDS_PER_ACTION >= 12, (
            'an action that executed its tool, was refused once by the '
            'fabrication gate and re-steered once by the under-report escape '
            'used 12 counted rounds live; a smaller allowance cuts off work '
            'that is going right')

    def test_turn_ceiling_is_n_actions_of_that_allowance(self):
        """One constant feeds both, so per-action and per-turn cannot drift."""
        rr = pytest.importorskip('hartos.reuse_recipe')
        for n in (1, 2, 6, 24):
            assert self._budget(n) == n * rr._REUSE_ROUNDS_PER_ACTION

    def test_a_second_action_can_still_do_a_full_actions_work(self):
        """The property the live drives lacked, stated as arithmetic."""
        rr = pytest.importorskip('hartos.reuse_recipe')
        spent_by_first = rr._REUSE_ROUNDS_PER_ACTION
        assert self._budget(2) - spent_by_first >= rr._REUSE_ROUNDS_PER_ACTION, (
            'after action 1 spends its full allowance there must still be a '
            "full allowance for action 2 — otherwise the recipe's later "
            'actions are unreachable however well they would have run')

    def test_both_loops_reset_the_counter_when_the_action_advances(self):
        src = _source()
        assert src.count('_action_rounds = 0') == 6, (
            'each loop needs three: initialisation, reset-on-advance, and '
            'reset-on-progress (3 sites x 2 loops).  A loop missing the '
            "advance reset spends the successor action's allowance on its "
            'predecessor; one missing the progress reset caps an action whose '
            'tools are running (measured 05:55:50, 0.445 s after the tool ran)')
        assert src.count('_action_rounds >= _REUSE_ROUNDS_PER_ACTION') == 2, (
            'both reuse loops must bound the CURRENT action, not only the turn')

    def test_current_action_read_is_guarded(self):
        """The budget path must not raise on a missing session."""
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._reuse_current_action_id('no_such_prompt') is None


class TestProgressResetsTheAllowance:
    """A cap that stops STALLS must not stop WORK.

    Measured 2026-09-09 on agent 33323830039, the drive after the per-action
    split landed.  Action 2 got its own allowance (`turn spend 12/24`, which
    is the split working), used it, and the cap fired:

        05:55:49,887  VLM loop finished: 2 actions in 9.2s (exit_reason=done)
        05:55:50,122  [725-SYNC] ... spliced 2 real tool answer(s)
        05:55:50,332  [REUSE-ROUNDS] while1 action 2/2 used its 12 rounds
                      without completing — ending turn (turn spend 12/24)

    The tool had executed 0.445 s earlier and its answers were already in the
    group log.  The action was moving; the budget ended it anyway.  New tool
    evidence now earns a fresh window, bounded by the unchanged turn ceiling.
    """

    def test_counts_results_not_proposals(self):
        """A tool_call is the model ASKING; only a RESULT is progress.

        Counting proposals would make the budget unable to end the exact
        stall it exists for — a model that keeps proposing and never runs
        anything would extend its own allowance forever.  Same distinction
        the fabrication gate draws.
        """
        rr = pytest.importorskip('hartos.reuse_recipe')

        class _Chat:
            agents = []

            def __init__(self, messages):
                self.messages = messages

        proposals_only = _Chat([
            {'role': 'assistant', 'tool_calls': [
                {'id': 'a1', 'function': {'name': 'execute_windows_or_android_command'}}]},
            {'role': 'assistant', 'tool_calls': [
                {'id': 'a2', 'function': {'name': 'execute_windows_or_android_command'}}]},
        ])
        assert rr._reuse_evidence_count(proposals_only) == 0, (
            'two unexecuted proposals counted as progress — that is the '
            'stall the budget must be able to end')

        with_result = _Chat(list(proposals_only.messages) + [
            {'role': 'tool', 'name': 'execute_windows_or_android_command',
             'content': 'Directory of C:\\Users\\sathi\\Documents ...'},
        ])
        assert rr._reuse_evidence_count(with_result) == 1

    def test_placeholder_result_is_not_progress(self):
        """The stand-in is minted BECAUSE nothing executed."""
        rr = pytest.importorskip('hartos.reuse_recipe')
        c = pytest.importorskip('core.constants')

        class _Chat:
            agents = []
            messages = [{'role': 'tool', 'name': 'x',
                         'content': c.HISTORICAL_TOOL_PLACEHOLDER}]

        assert rr._reuse_evidence_count(_Chat()) == 0

    def test_empty_chat_counts_zero_and_does_not_raise(self):
        rr = pytest.importorskip('hartos.reuse_recipe')

        class _Empty:
            messages = []
            agents = []

        assert rr._reuse_evidence_count(_Empty()) == 0

    def test_unmeasurable_evidence_cannot_extend_the_allowance(self):
        """Fail-closed: an object that raises must never read as progress."""
        rr = pytest.importorskip('hartos.reuse_recipe')

        class _Hostile:
            agents = []

            @property
            def messages(self):
                raise RuntimeError('buffer gone')

        assert rr._reuse_evidence_count(_Hostile()) == -1
        for previous in (0, 1, 7):
            assert not (rr._reuse_evidence_count(_Hostile()) > previous), (
                'an unmeasurable chat must not satisfy `> previous`, or the '
                'allowance is extended forever on uncertainty')

    def test_both_loops_reset_on_new_evidence(self):
        src = _source()
        assert src.count('_evidence_now > _action_evidence') == 2, (
            'both reuse loops must extend the CURRENT action when its tools '
            'produce new results; a loop without it caps working actions')
        assert src.count('_action_evidence = _reuse_evidence_count(') == 4, (
            'each loop needs the initial mark AND the re-mark on advance '
            '(2 sites x 2 loops), or the successor action inherits the '
            "predecessor's evidence count and never registers progress")

    def test_the_turn_ceiling_still_bounds_it(self):
        """Anti-vacuity: progress may consume the turn, not exceed it."""
        src = _source()
        assert src.count('count >= _round_budget') == 2, (
            'the turn ceiling is what keeps the progress reset from running '
            'forever; it must remain in both loops')
