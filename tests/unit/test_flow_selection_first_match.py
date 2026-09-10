"""When one persona owns several flows, reuse must walk the FIRST of them.

MEASURED LIVE 2026-09-10 on agent 92583386981 ("English Learning Session",
5 flows, every flow persona "Executor"), driven as its owner through the real
/chat route.  The saved flow-0 recipe is:

    92583386981_0_recipe.json -> action 1 "Retrieve chat history for English
                                 progress topic", tool_name get_chat_history

and what the live run actually walked was flow 4:

    14:09:47  GOT role index as 0
    14:09:47  GOT role index as 1
    14:09:47  GOT role index as 2
    14:09:47  GOT role index as 3
    14:09:47  GOT role index as 4      <- five overwrites, last one wins
    14:10:36  [FAB-GUARD] action 1 names tool(s) ['save_data_in_memory'] ...

`get_chat_history` was called ZERO times in the whole run.  The user was told
"Based on your history, you are currently at a B1 CEFR level" — a claim with
no tool behind it, because the recall flow is unreachable.

THE CAUSE IS ONE MISSING `break` (reuse_recipe.py:5219-5223):

    role_number = 0
    for num, i in enumerate(available_flows):
        if i['persona'].lower() == role.lower():
            role_number = num          # every match overwrites the last
    return role_number, role

FIRST-MATCH IS THE INTENDED SEMANTIC, on three independent signals in the same
file — this is not a preference being imposed:
  1. the initialiser is `role_number = 0`, i.e. the first flow;
  2. the no-role fallback is `role = available_roles[0]`, i.e. the first
     persona;
  3. the sibling `get_role` (:438) resolves its own lookups with `break` in
     BOTH loops (:445, :452).  This function is the odd one out.
Every existing test that stubs it agrees: test_scheduler_creation.py patches
`get_flow_number` with return_value=(0, ...) at :198, :244, :270 and :308.

BLAST RADIUS, counted over the real store (~/Documents/Nunba/data/prompts):
711 agents declare flows[]; 58 have two or more flows sharing one persona and
therefore change selection; 653 have a unique persona per flow and are
byte-identical either way.  Worst case in the wild is one agent with 51 flows
on a single persona.

    python -m pytest tests/unit/test_flow_selection_first_match.py --noconftest -q
"""
import ast
import io
import json
import os
import types
from unittest.mock import MagicMock

MODULE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'hartos', 'reuse_recipe.py')


def _source():
    with io.open(MODULE, encoding='utf-8', errors='replace') as fh:
        return fh.read()


def _compile_get_flow_number(namespace):
    """Compile the REAL get_flow_number out of reuse_recipe.py.

    Importing hartos.reuse_recipe pulls autogen -> llmlingua -> torch, and on
    a workstation with a broken torch install that is an OSError at collection
    time (WinError 1114, c10.dll) — the function under test needs none of it.

    This is NOT a reimplementation, which would be the vacuous-test trap: the
    exact AST node from the shipped file is compiled and executed, so the
    assertions below run the same bytes production runs.  Only the three
    collaborators it reaches for (the Flask logger, the role lookup, the path
    resolver) are injected.
    """
    for node in ast.parse(_source()).body:
        if isinstance(node, ast.FunctionDef) and node.name == 'get_flow_number':
            mod = ast.Module(body=[node], type_ignores=[])
            exec(compile(ast.fix_missing_locations(mod), MODULE, 'exec'),
                 namespace)
            return namespace['get_flow_number']
    raise AssertionError('get_flow_number not found in reuse_recipe.py')


def _select(tmp_path, personas, flow_personas, role):
    """Drive the real function against a manifest written to disk.

    Hand-written dicts are not enough: the function opens and parses the
    manifest itself, so the test gives it a real file.
    """
    manifest = tmp_path / 'agent.json'
    with io.open(str(manifest), 'w', encoding='utf-8') as fh:
        json.dump({'personas': [{'name': p} for p in personas],
                   'flows': [{'flow_name': 'f%d' % n, 'persona': p}
                             for n, p in enumerate(flow_personas)]}, fh)

    helper_fun = types.SimpleNamespace(safe_prompt_path=lambda *a, **k: str(manifest))
    ns = {'json': json,
          'current_app': MagicMock(),
          'get_role': lambda u, p: role,
          'helper_fun': helper_fun}
    return _compile_get_flow_number(ns)('u1', 'p1')


class TestFirstMatchWins:
    """The defect: several flows on one persona, and the LAST one is picked."""

    def test_five_flows_one_persona_selects_the_first(self, tmp_path):
        idx, role = _select(tmp_path,
                            personas=['Executor'],
                            flow_personas=['Executor'] * 5,
                            role='Executor')
        assert idx == 0, (
            'with 5 flows all owned by "Executor" the FIRST is the one whose '
            'recipe the loader opens (<prompt_id>_0_recipe.json); selecting '
            '%d makes flows 0..%d unreachable for reuse' % (idx, idx - 1))
        assert role == 'Executor'

    def test_the_live_92583386981_shape(self, tmp_path):
        """Exactly the path the live run took on 2026-09-10.

        `role=None` is load-bearing and this test was WRONG on its first cut:
        it passed `role='user'`, which is truthy, matches no persona, and so
        returned 0 trivially — green on the broken file, i.e. vacuous.  The
        live run logged five matches ("GOT role index as 0..4"), which can
        only happen once `role` equals the flows' persona, and that happens
        through the falsy branch: `if not role: role = available_roles[0]`.
        """
        idx, role = _select(tmp_path,
                            personas=['Executor'],
                            flow_personas=['Executor'] * 5,
                            role=None)
        assert role == 'Executor', 'the falsy-role fallback must select persona 0'
        assert idx == 0, (
            'this is the English Learning Session agent: index %d loads the '
            'save_data_in_memory flow, so get_chat_history can never run and '
            'the CEFR level it reports has no tool behind it' % idx)

    def test_two_flows_one_persona_selects_the_first(self, tmp_path):
        """The commonest shape in the wild: 40 of the 58 affected agents."""
        idx, _ = _select(tmp_path,
                         personas=['News Scout', 'Story Curator'],
                         flow_personas=['News Scout', 'News Scout',
                                        'Story Curator'],
                         role='News Scout')
        assert idx == 0


class TestNoRegressionOnTheOther653:
    """653 of 711 agents have a unique persona per flow — they must not move.

    A fix that changed those would be a regression far larger than the bug.
    """

    def test_single_match_keeps_its_index(self, tmp_path):
        idx, _ = _select(tmp_path,
                         personas=['A', 'B', 'C'],
                         flow_personas=['A', 'B', 'C'],
                         role='C')
        assert idx == 2, 'a unique persona must still select its own flow'

    def test_match_in_the_middle_keeps_its_index(self, tmp_path):
        idx, _ = _select(tmp_path,
                         personas=['A', 'B', 'C'],
                         flow_personas=['A', 'B', 'C'],
                         role='B')
        assert idx == 1

    def test_no_match_falls_back_to_zero(self, tmp_path):
        idx, _ = _select(tmp_path,
                         personas=['A', 'B'],
                         flow_personas=['A', 'B'],
                         role='Nobody')
        assert idx == 0, 'the documented default when nothing matches'

    def test_absent_role_uses_the_first_persona(self, tmp_path):
        """get_role returning falsy -> available_roles[0], which is 'A'."""
        idx, role = _select(tmp_path,
                            personas=['A', 'B'],
                            flow_personas=['B', 'A'],
                            role=None)
        assert role == 'A'
        assert idx == 1, 'persona A owns flow 1 here, and it is the only match'

    def test_match_is_case_insensitive(self, tmp_path):
        idx, _ = _select(tmp_path,
                         personas=['Executor'],
                         flow_personas=['X', 'EXECUTOR'],
                         role='executor')
        assert idx == 1


class TestTheLoopActuallyStops:
    """Structural drift-guard, and it must FAIL on the unfixed file.

    Behavioural tests above are the real proof; this one exists so a future
    refactor cannot quietly reintroduce "last match wins" while keeping the
    same observable shape on the small fixtures.  It is pinned to the for-loop
    inside get_flow_number, not to the module, because a `break` anywhere else
    in the file would satisfy a naive scan — the vacuous-guard failure mode.
    """

    def test_the_persona_loop_breaks_on_first_match(self):
        with io.open(MODULE, encoding='utf-8', errors='replace') as fh:
            src = fh.read()
        fn = None
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.FunctionDef) and node.name == 'get_flow_number':
                fn = node
                break
        assert fn is not None, 'get_flow_number not found in reuse_recipe.py'

        loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)]
        assert loops, 'get_flow_number no longer iterates the flows'
        breaks = [n for loop in loops for n in ast.walk(loop)
                  if isinstance(n, ast.Break)]
        assert breaks, (
            'the persona loop in get_flow_number has no break, so every '
            'matching flow overwrites the previous one and the LAST match is '
            'returned instead of the first (live: agent 92583386981 walked '
            'flow 4 of 5 and never called get_chat_history)')
