"""The per-turn attach must attach the tool the ACTION NAMES, not only tags.

Measured live 2026-09-06 on agent 89555447799.  Recipe action 1 declares
``tool_name: google_search`` and the seeded message carries it verbatim:

    Perform this action -> Action #1:Search for developer community needs on
    decentralized AI platforms
     follow these steps: [{'Initiate a search query...':
                           {'tool_name': 'google_search', 'code': None}}]

The Tier-1 per-turn attach decided what to attach purely from
``detect_goal_tags(message)`` — a prose keyword scan.  Reproduced off-line
against the real functions:

    detect_goal_tags(that message)  -> ['coding']
    get_tool_tags('coding') et al   -> {coding, computer-use, crawling,
                                        github, hive_embedding, pr, web}

and the live log recorded the result of that choice exactly once in a
26-minute drive:

    2026-09-06 08:41:38  Tier-1 turn attach: +['coding'] -> 0 tools

So the words "developer"/"platforms" inferred the tag `coding`, that tag
attached ZERO tools, and google_search — named outright by the recipe — never
reached the wire.  Of 96 autogen.reuse calls in the window exactly 1 carried a
tools[] block, and `INSIDE google search` fired 0 times.  A model cannot call
a tool it is not offered.

The same gap at population scale: 8,799 ``Error: Function <X> not found``
across the log rotations — send_message_to_user x1618 (the deliverable path),
get_user_details x908, execute_windows_or_android_command x418.  Those tools
ARE defined and registerable (send_message_to_user is core/agent_tools.py:956,
registered at :974); they simply were not attached for that turn.  It also
explains why execute_windows_or_android_command both works and fails: same
tool, different turn, different tag scan.

The recipe is the authority on which tool an action needs.  Tag inference from
prose is a lossy second guess and stays — but only as the fallback for
families nothing names.

    python -m pytest tests/unit/test_reuse_attaches_named_tools.py --noconftest -q
"""
import ast
import os
import re

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_REUSE = os.path.join(_ROOT, 'hartos', 'reuse_recipe.py')
_TOOLS = os.path.join(_ROOT, 'core', 'agent_tools.py')


def _src(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


class TestAttachByNameExists:
    """The primitive: a name-keyed sibling of attach_for_tags."""

    def test_attach_for_names_is_defined(self):
        assert 'def attach_for_names(' in _src(_TOOLS), (
            'core.agent_tools must expose attach_for_names — the name-keyed '
            'sibling of attach_for_tags')

    def test_it_reuses_the_same_attach_primitives(self):
        """Same file, same primitives — not a second attachment mechanism."""
        tree = ast.parse(_src(_TOOLS))
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == 'attach_for_names'), None)
        assert fn is not None, 'attach_for_names not found'
        called = {c.func.id for c in ast.walk(fn)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        attrs = {c.func.attr for c in ast.walk(fn)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
        assert 'register_dual' in called, (
            'must attach through register_dual, the same primitive '
            'attach_for_tags uses — schema on one agent, execution on the other')
        assert 'create_endpoint_function' in attrs, (
            'must build the callable with registry.create_endpoint_function, '
            'like its sibling — no second construction path')

    def test_it_is_idempotent_across_turns(self):
        src = _src(_TOOLS)
        m = re.search(r'def attach_for_names\(.*?(?=\ndef )', src, re.DOTALL)
        assert m, 'attach_for_names body not found'
        body = m.group(0)
        assert 'attached_names' in body and 'add(' in body, (
            'must skip names already in attached_names and update the set in '
            'place, exactly like attach_for_tags — the per-turn hook runs on '
            'every round')


class TestReuseConsultsTheActionsNamedTools:
    """The wiring: the per-turn attach must read the recipe, not only prose."""

    def test_helper_reads_the_actions_declared_tool_name(self):
        src = _src(_REUSE)
        assert 'def _reuse_action_tool_names(' in src, (
            'reuse must expose a helper that reads the CURRENT action\'s '
            'declared tool_name(s) from its recipe steps')
        m = re.search(r'def _reuse_action_tool_names\(.*?(?=\ndef )', src, re.DOTALL)
        assert m, 'helper body not found'
        body = m.group(0)
        assert 'tool_name' in body, (
            "must read the recipe step's 'tool_name' field — that is where the "
            'authoring pipeline records which tool the action needs')

    def test_turn_attach_calls_it(self):
        """The Tier-1 hook must use the named tools, not tags alone."""
        src = _src(_REUSE)
        m = re.search(r'Tier-1 per-turn attach(.*?)except Exception as _e',
                      src, re.DOTALL)
        assert m, 'Tier-1 per-turn attach block not found'
        block = m.group(1)
        assert '_reuse_action_tool_names(' in block, (
            'the per-turn attach must consult the action\'s named tools. Live '
            "2026-09-06 it used only detect_goal_tags(message), which inferred "
            "['coding'] from prose and attached 0 tools while the action named "
            'google_search outright')
        assert 'attach_for_names(' in block, (
            'and must attach them via the name-keyed primitive')
        assert 'detect_goal_tags(' in block, (
            'the tag scan stays — it is the fallback for families nothing '
            'names; this fix is ADDITIVE, not a replacement')


class TestNamedToolExtraction:
    """Behavioural: pull tool_name out of a real recipe-shaped action."""

    def _call(self, recipe_actions, action_id=1):
        rr = pytest.importorskip('hartos.reuse_recipe')
        rr.recipes['probe_names'] = {'actions': recipe_actions}
        try:
            return sorted(rr._reuse_action_tool_names('probe_names', action_id))
        finally:
            rr.recipes.pop('probe_names', None)

    def test_pulls_the_declared_tool(self):
        # exactly the shape of 89555447799_0_recipe.json action 1
        acts = [{'action': 'Search for developer community needs',
                 'recipe': [{'steps': 'Initiate a search query',
                             'tool_name': 'google_search'}]}]
        assert self._call(acts) == ['google_search']

    def test_multiple_steps_multiple_tools(self):
        acts = [{'recipe': [{'tool_name': 'google_search'},
                            {'tool_name': 'send_message_to_user'}]}]
        assert self._call(acts) == ['google_search', 'send_message_to_user']

    def test_prose_action_naming_no_tool_yields_nothing(self):
        """Must not invent tools for prose-only actions."""
        acts = [{'action': 'Think about it', 'recipe': [{'steps': 'ponder'}]}]
        assert self._call(acts) == []

    def test_missing_or_out_of_range_action_is_safe(self):
        assert self._call([], 1) == []
        assert self._call([{'recipe': [{'tool_name': 'google_search'}]}], 9) == []

    def test_absent_session_is_safe(self):
        rr = pytest.importorskip('hartos.reuse_recipe')
        assert rr._reuse_action_tool_names('no_such_session', 1) == []


class TestAuthoredToolNameNormalisation(TestNamedToolExtraction):
    """A REAL tool name with its argument glued on must still resolve.

    ``attach_for_names`` matches EXACTLY (core/agent_tools.py:372,
    ``if fn not in want``).  The authoring model frequently writes the tool and
    its argument into the one field, so the exact match rejects a tool that is
    registered, working, and named by the action.

    Measured 2026-09-07 over all 165 banked recipes in
    ~/Documents/Nunba/data/prompts (1,473 recipe steps, 978 naming a tool):

        identifier-shaped   848
        prose-shaped        130   <- can never match by exact comparison
        files with >=1      37 of 165  (22.4%)

    Splitting those 130 on ':' / ',' recovers a real name for 34 of them, and
    28 of those 34 are ``execute_windows_or_android_command`` — registered at
    reuse_recipe.py:1589 and measured firing 38x live, i.e. a tool that
    demonstrably works was being withheld from the turn that asked for it:

        execute_windows_or_android_command: click the 'Search' button
        execute_windows_or_android_command: type 'vegan pasta' into the search field
        google_search, crawl4ai, retry_logic

    The remaining 96 are not tools at all — the model pasting Python source
    line by line into the field (``ENGINE_REGISTRY = router.ENGINE_REGISTRY``,
    ``for eid in engine_ids``), or the literal string ``N/A``.  Those must
    yield NOTHING rather than a plausible-looking candidate.

    WHY HERE AND NOT IN attach_for_names: this function is the ONE reader of
    the authored field (its own docstring calls itself "the authoritative
    answer to which tool does this action need"), and reuse_recipe.py:3521 is
    its only caller.  attach_for_names is the MATCHER — "given names, attach
    those that exist, ignore the rest" — and teaching a matcher to parse prose
    would be scope creep.  Normalising in the reader also needs no registry
    access: unknown candidates are already discarded for free by the matcher's
    existing exact comparison, which is exactly what should happen to the 96.

    Inherits the whole parent class, so the clean-identifier cases above are
    re-run here as regression cover: normalisation must not disturb them.
    """

    def test_real_tool_with_glued_argument_is_recovered(self):
        """The 28-occurrence case — a working tool withheld by a glued suffix."""
        acts = [{'recipe': [{
            'tool_name': "execute_windows_or_android_command: click the "
                         "'Search' button to trigger web_search"}]}]
        assert self._call(acts) == ['execute_windows_or_android_command']

    def test_comma_separated_list_yields_each_candidate(self):
        """18088688973 action 1 names three tools in one field."""
        acts = [{'recipe': [{'tool_name': 'google_search, crawl4ai, retry_logic'}]}]
        assert self._call(acts) == ['crawl4ai', 'google_search', 'retry_logic']

    def test_pasted_source_code_yields_nothing(self):
        """18895904180 banked Python statements into tool_name."""
        for frag in ('ENGINE_REGISTRY = router.ENGINE_REGISTRY',
                     'for eid in engine_ids',
                     'import integrations.channels.media.tts_router as router',
                     "filters = [spec for spec in ENGINE_REGISTRY "
                     "if spec.install_target == 'venv']"):
            acts = [{'recipe': [{'tool_name': frag}]}]
            assert self._call(acts) == [], f'{frag!r} is not a tool name'

    def test_invented_tool_with_a_path_yields_nothing(self):
        """88761328396 action 1 — the agent this whole walk is blocked on.

        The model invented a tool called "Read file" (its own step text says
        "using the 'Read file' tool") and wrote the action title plus a Windows
        path into the field.  Nothing here may resolve: a drive-letter colon
        must not leave 'C' behind as a candidate.
        """
        acts = [{'recipe': [{
            'tool_name': 'Read file: C:\\Users\\sathi\\Documents\\Nunba'
                         '\\logs\\latest.log'}]}]
        assert self._call(acts) == []

    def test_literal_na_yields_nothing(self):
        acts = [{'recipe': [{'tool_name': 'N/A'}]}]
        assert self._call(acts) == []

    def test_dotted_registry_name_survives(self):
        """tts.package_installer is real and identifier-shaped — 5 uses."""
        acts = [{'recipe': [{'tool_name': 'tts.package_installer'}]}]
        assert self._call(acts) == ['tts.package_installer']

    def test_no_duplicate_candidates(self):
        """1 of the 130 doubles the tool: 'X: X: wait for the cook to confirm'.

        Asserts DEDUPLICATION, not a single element: a trailing bare word like
        'wait' is identifier-shaped and is emitted as a candidate, which is
        correct — attach_for_names discards names that match no registry entry
        (core/agent_tools.py:372), so an unknown candidate costs nothing.  What
        must never happen is the same tool being offered for attachment twice.
        """
        acts = [{'recipe': [{
            'tool_name': 'execute_windows_or_android_command: '
                         'execute_windows_or_android_command: wait'}]}]
        got = self._call(acts)
        assert got.count('execute_windows_or_android_command') == 1
        assert len(got) == len(set(got)), f'duplicate candidates in {got}'
