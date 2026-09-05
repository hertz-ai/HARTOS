"""The recipe-authoring prompt must name its action and constrain tool_name.

Two defects lived in ONE prompt (request_recipe_for_action), and together they
are why REUSE could never do real work.  Both were reproduced first-hand on
2026-09-05 by driving agent 88601674818 ("Relay", a GitHub release monitor)
through the live CREATE flow:

1. NO GOAL ANCHOR.  The example JSON carried the literal
   `"action": "Describe the action performed here"` — the prompt never said
   WHICH action it was writing a recipe for, so the only strong word in it was
   "recipe".  Relay's saved flow recipe therefore contains, verbatim:

       "Generate a detailed recipe for preparing a classic chocolate cake
        using standard kitchen tools and ingredients"

   ...with steps about sifting flour and cocoa powder.  The sibling builder
   request_recipe_for_action_last ALREADY injects the real action text; this
   one did not.  Same class of asymmetry as the main-leg tool filter.

2. NO TOOL CATALOG.  `tool_name` said only "Only include tool name here if
   used for this step" — never "choose from the tools you were given".  The
   47 real tools ARE attached to that same request, but the model was never
   told the field had to come from them, so it invented all 20 distinct names
   Relay authored: Baking Pan, Mixing Bowl, Oven, Sifter, Wire Rack,
   GitHub API Client, github_api_client, filtering_tool, nlp_parser_tool,
   output_terminal, ...  None resolve, so no action can execute at REUSE.

Extract-and-exec, matching test_recipe_placebo_reject.py — importing
create_recipe hangs a bare pytest env.

    python -m pytest tests/unit/test_recipe_prompt_anchor_and_tool_names.py --noconftest -q
"""
import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_SRC = open(os.path.join(_ROOT, 'hartos/create_recipe.py'), encoding='utf-8').read()


def _builder(name):
    """Source text of one recipe-prompt builder function."""
    m = re.search(r'\ndef %s\(.*?\n    return message\n' % re.escape(name),
                  _SRC, re.DOTALL)
    assert m, f'{name} not found — re-point this guard'
    return m.group(0)


def _placebo_fn():
    block = re.search(
        r'_RECIPE_PLACEHOLDER_STRINGS = frozenset\(.*?\n\n\ndef _recipe_is_placebo'
        r'.*?\n    except Exception:\n        return False\n', _SRC, re.DOTALL)
    assert block, 'placebo block not found — re-point this guard'
    ns = {'frozenset': frozenset}
    exec(block.group(0), ns)
    return ns['_recipe_is_placebo'], ns['_RECIPE_PLACEHOLDER_STRINGS']


BUILDERS = ('request_recipe_for_action', 'request_recipe_for_action_last')


class TestPromptAnchor:

    # ---- defect 1: the chocolate cake ----------------------------------
    def test_action_is_named_not_placeholdered(self):
        """Both builders must inject the REAL action text into the example."""
        for name in BUILDERS:
            src = _builder(name)
            assert 'Describe the action performed here' not in src, (
                f'{name} still sends the placeholder instead of naming the '
                'action. That leaves "recipe" as the only strong word in the '
                'prompt and the model writes a FOOD recipe — agent '
                '88601674818 banked a chocolate cake (2026-09-05).')
            assert 'get_action(' in src, (
                f'{name} must inject the current action text, the way '
                'request_recipe_for_action_last already did.')

    # ---- defect 2: invented tool names ---------------------------------
    def test_tool_name_is_constrained_to_real_tools(self):
        for name in BUILDERS:
            src = _builder(name)
            assert 'Do not invent a name' in src, (
                f'{name} does not constrain tool_name. The model invents '
                'names like "Baking Pan" / "nlp_parser_tool" that resolve to '
                'nothing, so no action can execute at REUSE.')
            assert 'Only include tool name here if used for this step.' not in src, (
                f'{name} still carries the unconstrained tool_name wording.')

    # ---- the guard must not go vacuous when the template changes -------
    def test_placeholder_set_is_in_sync_with_the_templates(self):
        """_RECIPE_PLACEHOLDER_STRINGS must cover the CURRENT template text.

        The set's own comment says "Keep this set in sync with the example
        JSON in request_recipe_for_action" — nothing enforced it, so editing
        the prompt silently left the echo-detector hunting for dead wording.
        """
        _fn, placeholders = _placebo_fn()
        for name in BUILDERS:
            src = _builder(name)
            for field in ('steps', 'tool_name'):
                for val in re.findall(r'"%s"\s*:\s*"([^"]+)"' % field, src):
                    assert val.strip().lower() in placeholders, (
                        f'{name}: template {field}={val[:60]!r} is NOT in '
                        '_RECIPE_PLACEHOLDER_STRINGS, so an echo of it would '
                        'be banked as a real recipe. Keep them in sync.')

    def test_guard_rejects_an_echo_of_the_current_tool_name_text(self):
        """Behavioural: the detector actually fires on the NEW wording."""
        is_placebo, _ph = _placebo_fn()
        src = _builder('request_recipe_for_action')
        tool_text = re.findall(r'"tool_name"\s*:\s*"([^"]+)"', src)[0]
        assert is_placebo({'action': 'Fetch releases',
                           'recipe': [{'steps': 'call the api',
                                       'tool_name': tool_text}]}) is True

    # ---- no regression in what the guard already caught ----------------
    def test_guard_still_rejects_the_legacy_echoes(self):
        is_placebo, _ph = _placebo_fn()
        assert is_placebo({'action': 'Describe the action performed here',
                           'recipe': []}) is True
        assert is_placebo({'action': 'Fetch releases',
                           'recipe': [{'steps': 'steps here'}]}) is True

    def test_guard_still_passes_a_real_recipe(self):
        is_placebo, _ph = _placebo_fn()
        assert is_placebo({
            'action': 'Fetch release history from the repository',
            'recipe': [{'steps': 'Call google_search for the release page',
                        'tool_name': 'google_search'}]}) is False
