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
reader must agree by construction.  Since the review of 5d6343409 both writers
call ``helper.bank_vlm_learning`` and every reader goes through
``helper.load_vlm_agent_files`` / ``helper.read_vlm_learning``, which also
require the file's ``learned_for`` stamp to name the same action
(tests/unit/test_vlm_learning_must_prove_its_action.py).

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

    def test_source_guard_no_writer_has_a_uniquifier(self):
        """The CREATE twin of the same writer kept the walker after REUSE's
        was removed (5d6343409).  MEASURED: agent 18088688973's 6-action flow
        has orphan files _7/_8/_9_vlm_agent.json (mtimes 2026-09-09 10:50,
        2026-09-17 06:53, 2026-09-09 10:57).  Both writers now call
        helper.bank_vlm_learning, so the helper is guarded too.  Source guard
        for the SHAPE only; the behaviour is driven end to end through
        CREATE's real tool in test_create_vlm_learning_files_its_action.py
        (hartos.create_recipe imports fine in the HARTOS venv)."""
        here = os.path.dirname(RR_PATH)
        for name in ('create_recipe.py', 'helper.py'):
            with open(os.path.join(here, name), encoding='utf-8') as fh:
                loops = _vlm_uniquifier_loops(ast.parse(fh.read()))
            assert not loops, (
                f"{name} walks to the next free vlm_agent slot at line(s) "
                f"{loops}; the reader takes that number as the action id")

    @pytest.mark.parametrize('action_id', [1, 2, 3, 7, 24, 38])
    def test_the_writer_and_the_reader_agree_on_the_id(
            self, action_id, tmp_path, monkeypatch):
        """Agreement by construction, run rather than read: what the one
        writer banks for action N, the one reader returns as action N."""
        import logging
        import types
        helper = pytest.importorskip('hartos.helper')
        monkeypatch.setattr(helper, 'PROMPTS_DIR', str(tmp_path))
        monkeypatch.setattr(helper, 'current_app', types.SimpleNamespace(
            logger=logging.getLogger('test_vlm_writer_identity')))
        path = helper.bank_vlm_learning(
            '33323830039', 0, action_id, 'run', {'action': 'x', 'recipe': []})
        assert READER_RULE(os.path.basename(path)) == action_id
        loaded = helper.load_vlm_agent_files('33323830039', 0)
        assert [v['action_id'] for v in loaded] == [action_id]


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
