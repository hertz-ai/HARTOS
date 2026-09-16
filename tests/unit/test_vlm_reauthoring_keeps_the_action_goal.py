"""A VLM re-authoring may replace an action's STEPS, never its GOAL.

THE CONTRACT, already written down at ``_vlm_merged_actions`` (reuse_recipe.py
:135) before this guard existed:

    "A ``*_vlm_agent.json`` file re-authors the STEPS of an action; it does not
     reassign whose action it is."

THE LIVE VIOLATION (2026-09-11 02:28-02:48, installed build, agent
88719487304, session 6c2dc0fc-7c93-4fe0-973e-f7466ff63f29_88719487304).
``execute_windows_or_android_command`` re-learns an action and writes
``<agent>_<flow>_<action>_vlm_agent.json`` under the CURRENT action id (the
write site's own comment: "the number in this filename is NOT a uniquifier...
Re-learning an action overwrites that action's file"). The merge then started
from ``dict(vlm_action)`` and restored only the two fields in
``_VLM_PRESERVED_CONTRACT_FIELDS``, so the ``action`` field -- the goal --
came from the re-learning too:

    saved recipe                                 what actually ran
    2 open browser to the top result URL      -> "Create a README.md file..."
    3 write Python script to parse HART OS docs-> "check disk space and show
                                                   partition information"
    4 execute the script -> structured JSON   -> "Restart the server service"
    7 generate a comprehensive research summary-> (faithful; text happened to match)

Per-action and recurring, not a contiguous block. Every substituted one became
an ``execute_windows_or_android_command`` chore, which is the tool narrating
its own work back over the action's goal.

WHAT IT COST, measured on that walk: action 3's fabrication gate demanded the
SUBSTITUTED action's tool, the agent ran it, and the gate released --
"[REUSE] Action 3 TERMINATED, advancing" at 02:35:19 with unrun=[]. Not a
guard force-complete, but a FALSE PASS: writing the parser script, which is
action 3's entire job, never happened. The walk ended on action 7 having
delivered no research report. Also: _0_4 carried
``can_perform_without_user_input: no`` and the manufactured task was
"Restart the server service" (#698 -- consent flag unenforced).

WHY THE FIX IS ONE ENTRY IN AN EXISTING TUPLE. ``action`` was classified as
CONTENT when the contract makes it IDENTITY. ``_VLM_PRESERVED_CONTRACT_FIELDS``
is already the canonical "these survive a re-authoring" list, already consulted
by all three merge call sites (:1153, :1776, :2019). Adding ``action`` to it
enforces the documented rule at the one place that decides -- no second merge,
no new helper.

WHAT THIS GUARD DOES NOT CLAIM: that re-learning is otherwise correct, or that
the agent then reaches its goal. It claims the goal text survives, and that the
re-authored STEPS are still applied -- the feature must keep working.
"""

import io
import os
import re
import unittest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SRC = os.path.join(_HARTOS, 'hartos', 'reuse_recipe.py')


def _load_merge():
    """exec the REAL constant + the REAL merge function together.

    Both, deliberately. Supplying our own _VLM_PRESERVED_CONTRACT_FIELDS would
    make every assertion below pass no matter what the module actually
    declares -- the exact shape of vacuous guard this suite has already been
    bitten by twice. Lifting the tuple out of the source is what makes this
    test able to fail.
    """
    src = io.open(_SRC, encoding='utf-8', errors='replace').read()

    # Close on a `)` at column 0, not on the first `)` anywhere. `[^)]*\)` read
    # fine and truncated the tuple the moment a comment inside it contained a
    # paren (`dict(vlm_action)`), which turned a green run into six failures
    # that pointed at the source instead of at this regex.
    const = re.search(r'^_VLM_PRESERVED_CONTRACT_FIELDS\s*=\s*\(.*?^\)',
                      src, re.M | re.S)
    assert const, '_VLM_PRESERVED_CONTRACT_FIELDS is gone; re-point this guard'

    # Stop at the first TOP-LEVEL statement, not at the next `def`.
    # `(?=^def |\Z)` looked right and was wrong: _vlm_merged_actions is followed
    # by module-level code (the `try: from hartos.helper import PROMPTS_DIR` /
    # `os.makedirs(PROMPTS_DIR)` block at ~:199), not by another def, so the
    # greedy form swallowed it and exec raised NameError: os. Match the def line
    # plus only lines that are blank or indented.
    fn = re.search(r'^def _vlm_merged_actions\(.*\n(?:(?:[ \t].*)?\n)*',
                   src, re.M)
    assert fn, '_vlm_merged_actions is gone; re-point this guard'

    ns = {}
    exec(compile(const.group(0) + '\n\n' + fn.group(0), _SRC, 'exec'), ns)
    return ns['_vlm_merged_actions'], ns['_VLM_PRESERVED_CONTRACT_FIELDS']


# The real shapes, from the 2026-09-11 walk.
_SAVED_ACTION_3 = {
    'action_id': 3,
    'action': "execute_coding_task: 'Write Python script to parse HART OS "
              "documentation text and extract performance metrics'",
    'persona': 'Executor',
    'can_perform_without_user_input': 'yes',
    'recipe': [{'steps': 'write the parser', 'tool_name': 'execute_coding_task'}],
}

_RELEARNED_ACTION_3 = {
    'action_id': 3,
    'action': 'Run the following command to check disk space and show detailed '
              'partition information',
    'persona': 'user6c2dc0fc-7c93-4fe0-973e-f7466ff63f29',
    'can_perform_without_user_input': 'no',
    'recipe': [{'steps': 'Get total and free disk space',
                'tool_name': 'execute_windows_or_android_command'}],
    'fallback_action': 'Perform a Google search using Internet Explorer',
}


class TestReauthoringKeepsTheGoal(unittest.TestCase):
    """RED until 'action' is treated as a contract field."""

    def setUp(self):
        self.merge, self.preserved = _load_merge()

    def test_the_goal_survives_a_relearning(self):
        out = self.merge([dict(_SAVED_ACTION_3)], [dict(_RELEARNED_ACTION_3)])
        self.assertEqual(
            out[0]['action'], _SAVED_ACTION_3['action'],
            "a re-learning overwrote the action's GOAL. Live 2026-09-11 this "
            "turned 'write Python script to parse HART OS docs' into 'check "
            "disk space', the fabrication gate then passed on the WRONG tool, "
            "and the agent advanced without ever doing action 3's job")

    def test_the_relearned_steps_are_still_applied(self):
        """The feature must keep working — this is not a revert."""
        out = self.merge([dict(_SAVED_ACTION_3)], [dict(_RELEARNED_ACTION_3)])
        self.assertEqual(
            out[0]['recipe'], _RELEARNED_ACTION_3['recipe'],
            're-authoring the STEPS is the whole point of a vlm_agent file; '
            'preserving the goal must not also freeze the recipe')

    def test_persona_still_preserved(self):
        """Regression guard: the reason this tuple exists (agent 89555447799)."""
        out = self.merge([dict(_SAVED_ACTION_3)], [dict(_RELEARNED_ACTION_3)])
        self.assertEqual(out[0]['persona'], 'Executor')

    def test_autonomy_flag_still_preserved(self):
        """Regression guard: 'no' in 47 of 47 files cost 99 rounds live."""
        out = self.merge([dict(_SAVED_ACTION_3)], [dict(_RELEARNED_ACTION_3)])
        self.assertEqual(out[0]['can_perform_without_user_input'], 'yes')

    def test_appended_action_keeps_its_own_goal(self):
        """No predecessor to inherit from — nothing to preserve, nothing to lose."""
        out = self.merge([dict(_SAVED_ACTION_3)],
                         [{'action_id': 99, 'action': 'brand new thing'}],
                         flow_persona='Executor')
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]['action'], 'brand new thing')
        self.assertEqual(out[1]['persona'], 'Executor')

    def test_action_is_declared_a_contract_field(self):
        """Pin the mechanism, not just the outcome."""
        self.assertIn(
            'action', self.preserved,
            "the goal must be preserved via _VLM_PRESERVED_CONTRACT_FIELDS -- "
            "the one list all three merge call sites (:1153, :1776, :2019) "
            "already consult -- not by a second rule somewhere else")


if __name__ == '__main__':
    unittest.main()
