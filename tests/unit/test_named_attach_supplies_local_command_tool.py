"""The per-turn named attach must be HANDED the tool 28 recipes ask for.

MEASURED LIVE 2026-09-09 on the installed build.  Across every current log,
the named-attach hook resolved 13 times and returned NOTHING 9 of those:

    named attach: action N names ['execute_windows_or_android_command'] -> 0 tools   x5
    named attach: action N names ['send_message_to_user']               -> 1 tools
    named attach: action N names ['google_search']                      -> 1 tools

The ones that resolve are in ``core.agent_tools.build_core_tool_closures``.
``execute_windows_or_android_command`` is not — it is a 334-line nested def
inside ``create_agents_for_user`` (reuse_recipe.py:1683) closing over 33 free
variables (assistant, helper, final_recipe, recipes, user_tasks, prompt_id...),
so it cannot live in the builder without relocating all of that.

WHAT THAT COSTS, watched end to end at 15:13-15:16 on agent 18088688973:

    15:13:47  [TARGET] Action 4: assigned -> in_progress
    15:14:31  reuse-w1-completed: terminal 'completed' verdict for action 4
    15:14:31  [FAB-GUARD] action 4 names ['execute_windows_or_android_command'];
              executed=[]; unrun=['execute_windows_or_android_command']
    15:14:31  [FABRICATED-COMPLETE] refusing to advance action 4
    15:15:59  assistant: "I need to mark action 4 as completed, but I don't have
              the execute_windows_or_android_command tool available."

The fabrication guard is RIGHT and the agent is HONEST.  The action still never
runs, because nothing ever put the tool where the resolver looks.

THE CONTRACT, and it is the one ``attach_for_names`` already documents for
itself (core/agent_tools.py:388-397): this tool is reachable ONLY through the
named attach, deliberately — "execute_windows_or_android_command runs arbitrary
OS commands.  Attaching it only for an action whose own recipe names it keeps
the blast radius at the action that asked for it."  The mechanism shipped; the
tool never arrived.  ``_hart_core_tools`` is assigned at reuse_recipe.py:2392,
INSIDE create_agents_for_user (1023-3159) and AFTER the def at 1683, so the
closure is in scope there and needs no relocation — only to be handed over in
the ``(name, desc, func)`` shape ``attach_for_names`` already iterates (:423).

Scope, measured not assumed: of every locally-defined callable in
create_agents_for_user that any saved recipe names, exactly ONE is missing from
the builder — this one, named in 28 recipes.  So this is a one-tool fix, not a
class of them.

    python -m pytest tests/unit/test_named_attach_supplies_local_command_tool.py -q
"""
import ast
import os

import pytest


RR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'hartos', 'reuse_recipe.py')

TOOL = 'execute_windows_or_android_command'
ATTR = '_hart_core_tools'


def _tree():
    with open(RR, encoding='utf-8') as fh:
        return ast.parse(fh.read())


def _core_tools_assignments(tree):
    """Every `<something>._hart_core_tools = <value>` assignment."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Attribute) and tgt.attr == ATTR:
                out.append(node)
    return out


class TestThePremise:
    """Anti-vacuity: prove the tool really is local and really is unreachable
    from the builder, before asserting anything about the fix."""

    def test_the_tool_is_a_nested_def_inside_create_agents_for_user(self):
        tree = _tree()
        outer = next((n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef)
                      and n.name == 'create_agents_for_user'), None)
        assert outer is not None
        inner = [c for c in ast.walk(outer)
                 if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and c.name == TOOL]
        assert inner, (
            f'{TOOL} is no longer a nested def in create_agents_for_user; '
            f'this file describes a layout that has changed and needs rewriting')

    def test_the_assignment_is_in_the_same_scope_as_the_def(self):
        """The whole reason the fix can be one line and not a relocation."""
        tree = _tree()
        outer = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)
                     and n.name == 'create_agents_for_user')
        deflines = [c.lineno for c in ast.walk(outer)
                    if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and c.name == TOOL]
        assigns = [a.lineno for a in _core_tools_assignments(outer)]
        assert deflines and assigns, (deflines, assigns)
        assert min(deflines) < min(assigns), (
            f'{TOOL} is defined at {deflines} but {ATTR} is assigned at '
            f'{assigns} — the closure must already exist at the assignment')

    def test_the_builder_does_not_carry_it(self):
        """If the builder ever gains it, THIS fix becomes the wrong one."""
        with open(os.path.join(os.path.dirname(RR), '..', 'core',
                               'agent_tools.py'), encoding='utf-8') as fh:
            src = fh.read()
        seg = src[src.index('def build_core_tool_closures'):]
        assert TOOL not in seg, (
            'build_core_tool_closures now defines the tool itself; the local '
            'hand-over this file guards is redundant and should be removed '
            'rather than kept as a second source')


class TestTheWiring:
    """THE DEFECT.  RED before the fix."""

    def test_hart_core_tools_hands_over_the_local_command_tool(self):
        assigns = _core_tools_assignments(_tree())
        assert assigns, f'no {ATTR} assignment found at all'
        carried = [a for a in assigns
                   if TOOL in ast.dump(a)]
        assert carried, (
            f'{ATTR} is assigned at line(s) {[a.lineno for a in assigns]} but '
            f'none of them mention {TOOL}. attach_for_names reads exactly this '
            f'attribute, so the tool 28 saved recipes name resolves to 0 tools '
            f'and every action that needs it wedges (live 2026-09-09 15:14:31: '
            f'FAB-GUARD unrun=[{TOOL!r}], agent replied "I don\'t have the '
            f'{TOOL} tool available").')

    def test_handover_keeps_the_name_desc_func_triple_shape(self):
        """attach_for_names does `for name, desc, func in core_tools`.

        A bare function appended to that list would raise at unpack time on
        the chat hot path — worse than the blindness being fixed.
        """
        assigns = [a for a in _core_tools_assignments(_tree())
                   if TOOL in ast.dump(a)]
        assert assigns, 'nothing to check — the wiring test above is RED'
        dumped = ast.dump(assigns[0])
        assert 'Tuple' in dumped or 'List' in dumped, (
            'the handover must be a (name, desc, func) triple in a list, the '
            'shape attach_for_names unpacks at core/agent_tools.py:423')


class TestTheDetectorIsNotVacuous:

    def test_detector_fires_on_a_bare_handover(self):
        sample = ast.parse('assistant._hart_core_tools = core_tools\n')
        found = _core_tools_assignments(sample)
        assert found and TOOL not in ast.dump(found[0])

    def test_detector_accepts_a_handover_that_includes_the_tool(self):
        sample = ast.parse(
            'assistant._hart_core_tools = core_tools + '
            '[("execute_windows_or_android_command", "d", '
            'execute_windows_or_android_command)]\n')
        found = _core_tools_assignments(sample)
        assert found and TOOL in ast.dump(found[0])
