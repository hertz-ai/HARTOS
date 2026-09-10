"""The attach must offer the tool the action's own TITLE names.

THE LIVE DEADLOCK THIS ENCODES (2026-09-10, agent 88719487304 action 3,
rid d58b-230854, on the installed build with c5a2035cb deployed):

    23:12:20  [FAB-GUARD] action 3 names tool(s) ['google_search',
                'execute_coding_task']; executed=['save_data_in_memory'];
                unrun=['google_search', 'execute_coding_task']
    23:13:03  [FABRICATED-COMPLETE] refusing to advance action 3: its tool(s)
                ['google_search', 'execute_coding_task'] produced no real result
    ... four 'completed' verdicts refused, google_search eventually ran ...
    23:18:19  [REUSE-ROUNDS] while1 action 3/9 used its 12 rounds without
                completing — ending turn (turn spend 12/108)

Zero "Tier-1 named attach" lines in that whole window, while the model called
``request_tools`` three times hunting for the capability it lacked.

THE TWO DERIVATIONS DISAGREED.  c5a2035cb taught the fabrication gate to read
the action's TEXT, so it now sees ``execute_coding_task``.  The per-turn attach
reads something else -- ``_reuse_action_tool_names``, which returns only the
``tool_name`` field of each recipe STEP.  For this action those are:

    action.action = "execute_coding_task: 'Write Python script to parse HART
                     OS documentation text and extract performance metrics,
                     architecture details, and system specifications into a
                     structured JSON format'"
    step[0].tool_name = 'google_search'
    step[1].tool_name = ''
    step[2].tool_name = ''

So the gate demands a tool the attach will never attach, and the action is held
until its round budget runs out.  Before c5a2035cb the two agreed by being
equally blind; the gate fix made the disagreement load-bearing.

WHY THE TITLE IS AN AUTHORING SITE, NOT PROSE.  ``_reuse_action_tool_names``
already refuses to trust the raw field, because "the authored value is
frequently ``<real tool>: <its argument>``" -- it runs ``_tool_name_candidates``
to peel that apart.  The action TITLE is written in exactly that convention by
the same authoring pipeline; measured on this recipe, ``execute_coding_task``
appears as a step ``tool_name`` elsewhere in the file AND as the title of
action 3.  The docstring's claim that the step field is "the authoritative
answer" is therefore false for this recipe, and this test pins the correction.

Same extractor, one more field.  Not a second name source: the fix reuses
``_tool_name_candidates``, so junk ('N/A', pasted source, prose) still yields
nothing, and a title that names no tool still returns [].
"""

import unittest

import hartos.reuse_recipe as rr
from hartos.reuse_recipe import _reuse_action_tool_names

_KEY = '__test_attach_sees_title__'

# Verbatim from ~/Documents/Nunba/data/prompts/88719487304_0_recipe.json.
_ACTION_3_TITLE = ("execute_coding_task: 'Write Python script to parse HART OS "
                   "documentation text and extract performance metrics, "
                   "architecture details, and system specifications into a "
                   "structured JSON format'")


class _Recipe(object):
    """Install/remove one synthetic recipe in the module-level store."""

    def __init__(self, actions):
        self.actions = actions

    def __enter__(self):
        rr.recipes[_KEY] = {'actions': self.actions}
        return self

    def __exit__(self, *exc):
        rr.recipes.pop(_KEY, None)
        return False


class TestTitleNamedToolIsOffered(unittest.TestCase):
    """RED until the title is read with the same extractor as the field."""

    def test_the_tool_in_the_action_title_is_returned(self):
        with _Recipe([{'action': _ACTION_3_TITLE,
                       'recipe': [{'tool_name': 'google_search'},
                                  {'tool_name': ''},
                                  {'tool_name': ''}]}]):
            names = _reuse_action_tool_names(_KEY, 1)
        self.assertIn(
            'execute_coding_task', names,
            'the attach reads only step tool_name fields, so the tool the '
            'action TITLE names is never attached -- the fabrication gate '
            'demands it and the action burns its round budget held '
            '(agent 88719487304 action 3, 2026-09-10 23:12:20-23:18:19)')

    def test_step_tool_names_are_still_returned(self):
        """The existing source must keep working, and stay first."""
        with _Recipe([{'action': _ACTION_3_TITLE,
                       'recipe': [{'tool_name': 'google_search'},
                                  {'tool_name': ''},
                                  {'tool_name': ''}]}]):
            names = _reuse_action_tool_names(_KEY, 1)
        self.assertIn('google_search', names)


class TestNoNewNoise(unittest.TestCase):
    """Reusing _tool_name_candidates means junk still yields nothing."""

    def test_a_prose_title_naming_no_tool_returns_nothing_new(self):
        with _Recipe([{'action': 'Summarise the findings for the user in '
                                 'three bullet points',
                       'recipe': [{'tool_name': ''}]}]):
            self.assertEqual(_reuse_action_tool_names(_KEY, 1), [])

    def test_an_unknown_word_in_the_title_is_not_treated_as_a_tool(self):
        with _Recipe([{'action': 'analyze httpx client configuration within '
                                 'generate_reply for missing timeout',
                       'recipe': [{'tool_name': ''}]}]):
            self.assertEqual(_reuse_action_tool_names(_KEY, 1), [])

    def test_no_duplicates_when_title_and_step_name_the_same_tool(self):
        with _Recipe([{'action': "google_search: 'find the docs'",
                       'recipe': [{'tool_name': 'google_search'}]}]):
            names = _reuse_action_tool_names(_KEY, 1)
        self.assertEqual(names.count('google_search'), 1,
                         'the same tool named in both places must appear once')

    def test_unknown_session_and_out_of_range_still_return_empty(self):
        """The hook runs every round and must never raise."""
        self.assertEqual(_reuse_action_tool_names('__no_such_session__', 1), [])
        with _Recipe([{'action': _ACTION_3_TITLE, 'recipe': []}]):
            self.assertEqual(_reuse_action_tool_names(_KEY, 99), [])
            self.assertEqual(_reuse_action_tool_names(_KEY, 0), [])


if __name__ == '__main__':
    unittest.main()
