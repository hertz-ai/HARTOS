"""_recipe_is_placebo — reject template-echo recipes (#140/#143 root).

The recipe-request prompt hands the 4B an EXAMPLE JSON with literal placeholder
strings ("Describe the action performed here", "steps here", ...). Small models
sometimes echo those verbatim, banking a junk recipe that stalls every REUSE
replay forever (live: goal 908f4987 banked action="Describe the action
performed here"). The validator must reject ONLY exact template echoes — real
recipes never contain these strings, so a legitimate tool-less / reasoning
action must still pass.

Extract-and-exec (importing create_recipe hangs a bare pytest env).
"""
import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _load():
    src = open(os.path.join(_ROOT, 'hartos/create_recipe.py'), encoding='utf-8').read()
    block = re.search(
        r'_RECIPE_PLACEHOLDER_STRINGS = frozenset\(.*?\n\n\ndef _recipe_is_placebo.*?\n    except Exception:\n        return False\n',
        src, re.DOTALL).group(0)
    ns = {'frozenset': frozenset}
    exec(block, ns)
    return ns['_recipe_is_placebo']


class TestPlaceboReject:
    def setup_method(self):
        self.is_placebo = _load()

    def test_rejects_template_action_echo(self):
        assert self.is_placebo({
            'action': 'Describe the action performed here',
            'recipe': [{'steps': 'x', 'tool_name': 'google_search'}]}) is True

    def test_rejects_template_step_echo(self):
        assert self.is_placebo({
            'action': 'real action',
            'recipe': [{'steps': 'steps here', 'tool_name': ''}]}) is True

    def test_rejects_template_tool_echo(self):
        assert self.is_placebo({
            'action': 'real action',
            'recipe': [{'steps': 'do a thing',
                        'tool_name': 'Only include tool name here if used for this step.'}]}) is True

    def test_accepts_real_recipe_with_tool(self):
        assert self.is_placebo({
            'action': 'Execute market research search',
            'recipe': [{'steps': 'search the web for X',
                        'tool_name': 'google_search'}]}) is False

    def test_accepts_legit_toolless_reasoning_action(self):
        """A real action with no tool (pure reasoning) must NOT be rejected."""
        assert self.is_placebo({
            'action': 'Synthesize the findings into a summary',
            'recipe': [{'steps': 'combine the three data points into a paragraph',
                        'tool_name': ''}]}) is False

    def test_malformed_never_raises(self):
        assert self.is_placebo({}) is False
        assert self.is_placebo({'recipe': 'not-a-list'}) is False
        assert self.is_placebo({'action': None, 'recipe': [None]}) is False
