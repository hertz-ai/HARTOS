"""A learned-recipe file must be named for the action it belongs to.

MEASURED LIVE 2026-09-09, agent 33323830039 on the installed build.  Its saved
recipe (``33323830039_0_recipe.json``, authored 2026-05-27) holds exactly ONE
action.  On disk beside it:

    33323830039_0_1_vlm_agent.json   2026-09-07 10:07   action_id 1   cpwui 'yes'
    33323830039_0_2_vlm_agent.json   2026-09-09 04:37   action_id 2   cpwui 'no'
    33323830039_0_3_vlm_agent.json   2026-09-09 06:44   action_id 3   cpwui 'no'
    33323830039_0_4_vlm_agent.json   2026-09-09 06:45   action_id 4   cpwui 'no'

The app wrote _3_ and _4_ itself during two drives — server.log.2 carries
"Generated recipe data saved to ...33323830039_0_3_vlm_agent.json" and the same
for _4_.  A ONE-action agent became a FOUR-action agent in ~2 hours of driving,
and the three appended actions all carry the VLM writer's constant
``can_perform_without_user_input: 'no'``, which disarms every driver
(_reuse_action_is_autonomous -> False) and ends the turn via [REUSE-NODRIVER].

THE MECHANISM, read end to end across three files:

  WRITER   reuse_recipe.py, inside execute_windows_or_android_command:
               while os.path.exists(f"{base_path}_{action_id_to_use}_vlm_agent.json"):
                   action_id_to_use += 1
           — a FILENAME uniquifier.  It starts at the current action and walks
           until it finds a free slot, so its output is "the next unused number",
           not "the action this recipe is for".

  READER   helper.load_vlm_agent_files:
               action_id = int(parts[2])
               recipe_data["action_id"] = action_id
           — takes that number as the action's IDENTITY.

  CONSUMER reuse_recipe._vlm_merged_actions: no existing action carries that id,
           so it takes the `if not replaced:` APPEND branch and the file becomes
           a NEW action in the agent's ledger.

So a collision-avoidance counter escapes into the action-id namespace.  Nothing
downstream can tell the difference, because by the time the reader sees it, it is
just an integer in a filename.

THE INVARIANT, and it already has a canonical home:
``helper_fun.safe_prompt_path(prompt_id, role_number, action_id, 'vlm_agent')``
builds ``{prompt_id}_{role}_{action}_vlm_agent.json`` — exactly the shape
``load_vlm_agent_files`` parses, and exactly what the DIRECT-READ site a few
lines above the writer already calls to find "this action's file".  Writer and
reader must agree by construction; today only the reader uses the helper.

    python -m pytest tests/unit/test_vlm_writer_action_identity.py --noconftest -q
"""
import ast
import os
import re

import pytest


RR_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'hartos', 'reuse_recipe.py')

# The reader's rule, verbatim from helper.load_vlm_agent_files:
#     parts = file.split('_'); action_id = int(parts[2])
READER_RULE = lambda name: int(name.split('_')[2])  # noqa: E731


@pytest.fixture(scope='module')
def rr_tree():
    with open(RR_PATH, encoding='utf-8') as fh:
        return ast.parse(fh.read())


def _vlm_uniquifier_loops(tree):
    """Every `while os.path.exists(...<vlm_agent>...)` that bumps a counter.

    Structural, not textual: a While whose test calls os.path.exists and whose
    body is a single AugAssign `+= 1`.  That shape IS the defect — it makes the
    written filename depend on what is already on disk.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.While):
            continue
        test_src = ast.dump(node.test)
        if 'exists' not in test_src or 'vlm_agent' not in test_src:
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AugAssign) and isinstance(stmt.op, ast.Add):
                found.append(getattr(node, 'lineno', -1))
    return found


class TestWriterNamesTheActionItBelongsTo:

    def test_no_filename_uniquifier_decides_the_action_id(self, rr_tree):
        """THE DEFECT.  RED before the fix."""
        loops = _vlm_uniquifier_loops(rr_tree)
        assert not loops, (
            f"a `while os.path.exists(...vlm_agent...)` counter still picks the "
            f"filename at line(s) {loops}; helper.load_vlm_agent_files reads that "
            f"number back as the action's identity, so every learned command "
            f"appends a phantom non-autonomous action to the agent's ledger "
            f"(measured live: agent 33323830039, 1 recipe action -> 4)")

    def test_writer_uses_the_canonical_path_helper(self, rr_tree):
        """Same builder the reader already uses — agreement by construction.

        `safe_prompt_path(prompt_id, role, action, 'vlm_agent')` is what the
        direct-read site calls to locate THIS action's file.  A writer that
        concatenates its own string can drift from it; one that calls the same
        helper cannot.
        """
        calls = 0
        for node in ast.walk(rr_tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, 'attr', None) or getattr(fn, 'id', None)
            if name != 'safe_prompt_path':
                continue
            if any(isinstance(a, ast.Constant) and a.value == 'vlm_agent'
                   for a in node.args):
                calls += 1
        assert calls >= 2, (
            "expected the vlm_agent path to come from safe_prompt_path at BOTH "
            f"the read site and the write site; found {calls} such call(s)")


class TestTheInvariantItself:
    """Anti-vacuity: prove the helper really does round-trip, so the assertions
    above are not just banning a code shape they never had to justify."""

    @pytest.mark.parametrize('action_id', [1, 2, 3, 7, 24, 38])
    def test_canonical_path_round_trips_through_the_reader(self, action_id):
        helper = pytest.importorskip('hartos.helper')
        path = helper.safe_prompt_path(
            '33323830039', '0', str(action_id), 'vlm_agent')
        assert READER_RULE(os.path.basename(path)) == action_id, (
            'safe_prompt_path must place the action id where '
            'load_vlm_agent_files reads it (parts[2])')

    def test_the_uniquifier_shape_breaks_that_round_trip(self):
        """The defect, demonstrated on the naming rule alone.

        Written as the writer wrote it: start at the current action, walk past
        whatever exists.  The reader then reports the WALKED id, not the real
        one — which is the whole bug, independent of any file I/O.
        """
        current_action = 1
        already_on_disk = {1, 2, 3}          # the live state at 06:45
        walked = current_action
        while walked in already_on_disk:
            walked += 1
        name = f'33323830039_0_{walked}_vlm_agent.json'
        assert READER_RULE(name) == 4 != current_action, (
            'this is the defect: the reader reports action 4 for a recipe that '
            'belongs to action 1')

    def test_detector_is_not_vacuous(self):
        """The AST detector must FIRE on the pattern it claims to ban."""
        sample = ast.parse(
            "while os.path.exists(f'{base}_{aid}_vlm_agent.json'):\n"
            "    aid += 1\n")
        assert _vlm_uniquifier_loops(sample), (
            'the detector cannot see the very shape it exists to reject')

    def test_detector_ignores_unrelated_while_loops(self):
        """...and must NOT fire on an ordinary loop."""
        sample = ast.parse("while n < 10:\n    n += 1\n")
        assert not _vlm_uniquifier_loops(sample)
