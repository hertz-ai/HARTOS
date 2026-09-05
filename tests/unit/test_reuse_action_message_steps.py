"""Guard: the action-message builder must survive a list-valued `steps`.

Live 2026-09-05, Auto Research 18088688973, installed build, gui_app.log:

    14:06:38 [REUSE-SEED] FELL BACK to the bare user message — the opening
             turn will NOT command an action: TypeError("unhashable type: 'list'")

`_build_reuse_action_message` renders each recipe step as a dict KEYED BY the
step's own content:

    steps = [{x['steps']: {...}} for x in recipe_actions[...]['recipe']]

Model-authored recipes do not agree on the type of `steps`.  Measured on the
live recipe prompts/18088688973_0_recipe.json:

    A1      steps = list  -> TypeError, the builder dies
    A2..A6  steps = str   -> renders fine

So the builder crashed on exactly the action the opening turn seeds.  That is
why this agent never received a "Perform this action -> Action #1" command,
while Trading 33204307184 — whose action 1 carries a str — received one 6x on
the wire from the same code.  Same builder, different recipe data.

Blast radius is wider than the seed: the seed has a fail-safe and degrades to
the bare user message, but the ADVANCE path (`_advance_or_steer`) calls the
same builder with no guard, so a list-valued `steps` there takes the whole
turn down.

Fix: coerce the key with str().  Where recipes already work the key is a str
and str(s) is s, so the rendered message is byte-identical — the only
behaviour that changes is the crash.

    python -m pytest tests/unit/test_reuse_action_message_steps.py --noconftest -q
"""
import unittest

import hartos.reuse_recipe as rr


KEY = 'val-user_val-prompt'


class _StubTask:
    """Minimal stand-in for the SmartLedger task the builder reads."""

    def __init__(self, action_text):
        self._action_text = action_text

    def get_action(self, _idx):
        return {'action': self._action_text}


class _Patched:
    """Swap the two module globals the builder reads, then restore."""

    def __init__(self, steps_value):
        self._steps_value = steps_value

    def __enter__(self):
        self._tasks, self._recipes = rr.user_tasks, rr.recipes
        rr.user_tasks = {KEY: _StubTask('Search for the latest advancements')}
        rr.recipes = {KEY: {'actions': [{'recipe': [{
            'steps': self._steps_value,
            'tool_name': 'google_search',
            'generalized_functions': 'def run(): ...',
        }]}]}}
        return self

    def __exit__(self, *exc):
        rr.user_tasks, rr.recipes = self._tasks, self._recipes
        return False


# The exact shape measured in A1 of the live recipe.
LIST_STEPS = [
    {'description': 'List all ingredients needed using placeholders', 'tool_name': None},
    {'description': 'Cross-verify each claim against a second source', 'tool_name': None},
]
STR_STEPS = ("Construct a search query string containing "
             "'autonomous deep research technology 2024'")


class ReuseActionMessageSteps(unittest.TestCase):

    def test_list_steps_does_not_kill_the_builder(self):
        # Pre-fix this raises TypeError("unhashable type: 'list'") and the
        # opening turn silently loses its action command.
        with _Patched(LIST_STEPS):
            msg = rr._build_reuse_action_message(KEY, 1)
        self.assertIn('Perform this action -> Action #1', msg,
                      'a list-valued steps must still produce the action command')
        self.assertIn('Search for the latest advancements', msg,
                      "the action's own text must survive")

    def test_list_steps_content_reaches_the_model(self):
        # Rendering must not silently drop the step content — the model needs
        # it to know what to do.
        with _Patched(LIST_STEPS):
            msg = rr._build_reuse_action_message(KEY, 1)
        self.assertIn('Cross-verify each claim', msg,
                      'step content must be rendered, not swallowed')
        self.assertIn('google_search', msg,
                      'the tool name must still be carried alongside the step')

    def test_str_steps_render_is_unchanged(self):
        # Regression gate: every recipe that works today keys on a str, and
        # str(s) is s, so the rendered message must be EXACTLY as before.
        # Build the expectation with Python's own repr rather than hand-
        # quoting it — the step text contains apostrophes, so repr picks
        # double quotes and a hand-written single-quoted literal would not
        # match even when the behaviour is correct.
        expected_steps = [{STR_STEPS: {'tool_name': 'google_search',
                                       'code': 'def run(): ...'}}]
        expected = ('Perform this action -> Action #1:'
                    'Search for the latest advancements'
                    f'\n follow these steps: {expected_steps}')
        with _Patched(STR_STEPS):
            msg = rr._build_reuse_action_message(KEY, 1)
        self.assertEqual(expected, msg,
                         'str-valued steps must render byte-identically to '
                         'pre-fix — this is the no-regression gate')

    def test_builder_does_not_key_a_dict_on_raw_step_content(self):
        # Root-cause pin: using model-authored content as a dict key is what
        # made the builder type-dependent in the first place.
        src = rr.inspect.getsource(rr._build_reuse_action_message) \
            if hasattr(rr, 'inspect') else None
        if src is None:
            import inspect as _i
            src = _i.getsource(rr._build_reuse_action_message)
        self.assertNotIn("{x['steps']:", src,
                         "raw x['steps'] as a dict key crashes on any "
                         'non-hashable steps value — coerce it')


if __name__ == '__main__':
    unittest.main()
