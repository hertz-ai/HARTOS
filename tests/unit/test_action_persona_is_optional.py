"""A recipe action without a `persona` key must still RUN, not crash or vanish.

THE LIVE FAILURE THIS ENCODES (2026-09-10 18:15:17, installed build,
agent 28160128202 -- the REUSE drive taken right after its CREATE finished):

    1 Recipe Json exist Going to reuse
    this is action persona:Executor
    Some ERROR IN REUSE RECIPE 'persona'
      File "hartos/reuse_recipe.py", line 6422, in chat_agent
      File "hartos/reuse_recipe.py", line 1138, in create_agents_for_user
    KeyError: 'persona'
    LangChain returned error or empty: {'_tier': 'direct'}

/chat then fell through to a toolless LLM which answered "I cannot execute
real-time logistics flows... the following are estimates" and printed a table
of five carriers with invented rates.  Zero recipe actions ran.

WHY THE KEY WAS MISSING: the CREATE pipeline wrote it inconsistently --
    28160128202_0_1..3.json  persona='Executor'
    28160128202_0_4..5.json  <<no persona key at all>>
Across every saved flow recipe on this box: 127 have actions, 2 of them have
at least one action with no persona (4 actions total); 88656227144 has none on
ANY of its actions.  That producer inconsistency is a SEPARATE defect and is
NOT what these tests cover.

WHY DEFAULTING TO `role` IS NOT A NEW RULE: _vlm_merged_actions is handed
`role` as its `flow_persona` (reuse_recipe.py:1127) and assigns exactly that to
an appended action that has no owner -- "the flow's persona is the only correct
owner".  Same question, same answer, one derivation.  Defaulting also keeps the
action in role_actions, so it still RUNS: making the read merely safe would
have traded a loud crash for a silent omission.
"""

import ast
import io
import os
import sys
import unittest

import hartos.reuse_recipe  # noqa: F401  (ensure cached)

rr = sys.modules['hartos.reuse_recipe']
_SRC_PATH = os.path.join(os.path.dirname(rr.__file__), 'reuse_recipe.py')
NL = chr(10)


class TestActionPersonaResolution(unittest.TestCase):
    """ONE derivation for 'who owns this action', used by both call sites."""

    def test_an_explicit_persona_wins(self):
        self.assertEqual(
            rr._action_persona({'persona': 'Helper'}, 'Executor'), 'Helper')

    def test_a_missing_persona_defaults_to_the_running_role(self):
        """THE LIVE SHAPE: actions 4 and 5 had no persona key at all."""
        self.assertEqual(
            rr._action_persona({'action': 'x'}, 'Executor'), 'Executor')

    def test_an_empty_persona_defaults_to_the_running_role(self):
        self.assertEqual(
            rr._action_persona({'persona': ''}, 'Executor'), 'Executor')

    def test_a_non_dict_action_does_not_raise(self):
        self.assertEqual(rr._action_persona('junk', 'Executor'), 'Executor')

    def test_no_role_and_no_persona_is_empty_not_an_exception(self):
        self.assertEqual(rr._action_persona({}, None), '')


class TestTheLiveRecipeSelectsEveryAction(unittest.TestCase):
    """The exact 5-action shape that crashed, filtered the way the loop does."""

    LIVE = [
        {'action_id': 1, 'persona': 'Executor'},
        {'action_id': 2, 'persona': 'Executor'},
        {'action_id': 3, 'persona': 'Executor'},
        {'action_id': 4},                          # create wrote no persona
        {'action_id': 5},                          # create wrote no persona
    ]

    def test_all_five_actions_are_selected_for_the_executor_role(self):
        role = 'Executor'
        picked = [a['action_id'] for a in self.LIVE
                  if rr._action_persona(a, role).lower() == role.lower()]
        self.assertEqual(
            picked, [1, 2, 3, 4, 5],
            'actions 4 and 5 dropped out of role_actions — a persona-less '
            'action must still run, not silently vanish')

    def test_a_different_role_still_excludes_the_explicit_ones(self):
        """Defaulting must not make every action match every role."""
        role = 'Helper'
        picked = [a['action_id'] for a in self.LIVE
                  if rr._action_persona(a, role).lower() == role.lower()]
        self.assertEqual(picked, [4, 5])


class TestNoUnguardedPersonaSubscriptInTheActionsLoop(unittest.TestCase):
    """Drift guard: the crash was the LOG LINE subscripting an optional key."""

    def test_the_actions_loop_uses_the_helper(self):
        lines = io.open(_SRC_PATH, encoding='utf-8',
                        errors='replace').read().splitlines()
        anchor = "for i in recipes[user_prompt]['actions']:"
        starts = [n for n, ln in enumerate(lines) if anchor in ln]
        self.assertTrue(starts, 'anchor line vanished — re-point this guard')
        for n in starts:
            body = NL.join(lines[n:n + 8])
            self.assertNotIn(
                'i["persona"]', body,
                'line %d still subscripts an optional key inside the actions '
                'loop — that is the exact expression that raised '
                "KeyError('persona') live" % (n + 1))
            self.assertNotIn("i['persona']", body,
                             'line %d still subscripts i[\'persona\']' % (n + 1))
            self.assertIn(
                '_action_persona', body,
                'line %d does not resolve the owner through _action_persona' % (n + 1))


if __name__ == '__main__':
    unittest.main()
