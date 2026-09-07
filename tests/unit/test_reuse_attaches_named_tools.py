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
