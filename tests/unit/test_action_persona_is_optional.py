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

WHY DEFAULTING TO `role` IS NOT A NEW RULE: every action of a flow runs as
that flow's role, so "who owns an action with no owner" has the same answer
the flow itself gives.  (_vlm_merged_actions once took a `flow_persona` for the
actions it appended; since 3d008fc9e it appends nothing -- a re-learning whose
id names no flow action is dropped as an orphan -- and the parameter is gone.)
Defaulting also keeps the action in role_actions, so it still RUNS: making the
read merely safe would have traded a loud crash for a silent omission.
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


class TestTheAgentsDeclaredPersonasDecideWhatIsAPersona(unittest.TestCase):
    """The `persona` field has THREE producers with three vocabularies.

    MEASURED live 2026-10-30 17:09:43 on the installed build, agent
    1793359802508 ("Syllable Coach", one flow, persona 'Speech Companion',
    nine banked actions): the trace bank stamps 'Executor' (create_recipe
    _bank_action_recipe_from_trace), the VLM path stamps 'user<id>', and the
    model-written recipe carries the flow persona.  Reuse kept the actions
    whose persona equalled the role, so 8 x 'Executor' dropped out and the
    ledger ran ONE action ("[REUSE-LEDGER] tasks=1 actions=1"); the
    len(role_actions)==0 fallback only fires when nothing matches.

    A persona is a name the agent record DECLARES (its `personas` list).
    Anything else in that field is a producer's bookkeeping, and the action
    belongs to the flow's role, exactly as a missing persona does.  Call sites
    that do not pass the declared list keep the old behaviour.
    """

    DECLARED = ['Speech Companion']

    def test_a_create_group_agent_name_is_not_a_persona(self):
        self.assertEqual(
            rr._action_persona({'persona': 'Executor'}, 'Speech Companion',
                               self.DECLARED),
            'Speech Companion')

    def test_a_vlm_user_id_is_not_a_persona(self):
        self.assertEqual(
            rr._action_persona({'persona': 'usercf125371-aa'}, 'Speech Companion',
                               self.DECLARED),
            'Speech Companion')

    def test_a_declared_persona_still_wins(self):
        self.assertEqual(
            rr._action_persona({'persona': 'Parent Helper'}, 'Speech Companion',
                               ['Speech Companion', 'Parent Helper']),
            'Parent Helper')

    def test_declared_names_match_without_regard_to_case(self):
        self.assertEqual(
            rr._action_persona({'persona': 'speech companion'}, 'Speech Companion',
                               self.DECLARED),
            'speech companion')

    def test_an_empty_declared_list_means_unknown_and_keeps_the_old_rule(self):
        """A legacy record with no `personas` must not turn every stamp into
        the role; without a declaration there is nothing to check against."""
        self.assertEqual(
            rr._action_persona({'persona': 'Executor'}, 'Speech Companion', []),
            'Executor')

    def test_without_the_declared_list_the_old_rule_holds(self):
        self.assertEqual(
            rr._action_persona({'persona': 'Executor'}, 'Speech Companion'),
            'Executor')

    def test_the_syllable_coach_recipe_selects_all_nine(self):
        """The live 17:09:43 shape: 8 trace-banked + 1 model-written action."""
        live = [{'action_id': n, 'persona': 'Executor'} for n in (1, 2)]
        live.append({'action_id': 3, 'persona': 'Speech Companion'})
        live += [{'action_id': n, 'persona': 'Executor'} for n in range(4, 10)]
        role = 'Speech Companion'
        picked = [a['action_id'] for a in live
                  if rr._action_persona(a, role, self.DECLARED).lower()
                  == role.lower()]
        self.assertEqual(picked, list(range(1, 10)),
                         'reuse must run the whole flow, not the one action '
                         'whose producer happened to write the flow persona')


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
            self.assertIn(
                'declared_personas', body,
                'line %d resolves the owner without the agent\'s declared '
                'personas, so a trace-banked \'Executor\' drops out of the '
                'ledger again (8 of 9 Syllable Coach actions, 2026-10-30)' % (n + 1))


if __name__ == '__main__':
    unittest.main()
