"""A CREATE step gets the tool its own text names before it is posted.

WHAT WENT WRONG (live, source Nunba on the local 4B, 18 Nov 2026 19:13-19:17
IST, a test account): a new research agent 1795008854159 was created with the
plan "1. google_search ... 2. crawl4ai: Fetch the top result pages ...".  The
build gate kept 0 of 8 service tools (goal_tags=[]), and CREATE has no
per-step named attach: its only per-turn attach (_attach_for_create_turn)
reads the person's words once per turn, while one creation turn walks several
steps.  "Execute Action 2: crawl4ai: ..." therefore ran with no crawl4ai to
call; the model asked the person instead, and the step was banked with an
empty tool.  REUSE attaches an action's named tools as it reaches the action
("Tier-1 named attach", reuse_recipe._attach_named_tools_for_action).

THE RULE
  When CREATE posts a step (both step_instruction sites in get_response_group)
  it first attaches the tools the step's `<tool>: <argument>` text names, with
  the helpers REUSE uses: reuse_recipe._tool_name_candidates and
  core.agent_tool_menu.attach_for_names(names, helper, assistant, registry,
  assistant._hart_attached_tools, core_tools=assistant._hart_core_tools).
  The Helper gets the schema and the Assistant the function; both schemas are
  fitted to the live n_ctx keeping the named tools and the send.

    python -m pytest tests/unit/test_a_create_step_attaches_the_tool_it_names.py -q --noconftest
"""
import ast
import logging
import os
import re
import sys
import types

import hartos.helper as helper_fun
from tests.unit.module_swap import swap_modules

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
CREATE = os.path.join(ROOT, 'hartos', 'create_recipe.py')
REUSE = os.path.join(ROOT, 'hartos', 'reuse_recipe.py')
STEP = "crawl4ai: Fetch the top result pages using markdown extraction to read the content."


def _lift(path, names, ns):
    tree = ast.parse(open(path, encoding='utf-8').read())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == set(names), f'{names} missing from {path}'
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, 'exec'), ns)
    return ns


def _rig(armed=True):
    """The real helper and the real name matcher, with recording stand-ins
    for the attach and the fit."""
    calls, fits = [], []
    real = _lift(REUSE, {'_tool_name_candidates'},
                 {'re': re, '_TOOL_IDENT_RE': helper_fun.TOOL_IDENT_RE})
    reuse = types.ModuleType('hartos.reuse_recipe')
    reuse._tool_name_candidates = real['_tool_name_candidates']
    menu = types.ModuleType('core.agent_tool_menu')
    menu.attach_for_names = (
        lambda names, helper, executor, registry, attached_names, core_tools=None:
        calls.append({'names': list(names), 'helper': helper, 'executor': executor,
                      'registry': registry, 'attached': attached_names,
                      'core_tools': core_tools}) or len(names))
    menu.fit_schema_to_ctx = (
        lambda agent, protect=(), room=None, turn_protect=False:
        fits.append((agent, set(protect), turn_protect)))
    hooks = types.ModuleType('hartos.lifecycle_hooks')
    hooks.SEND_TOOL = 'send_message_to_user'
    svc = types.ModuleType('integrations.service_tools')
    svc.service_tool_registry = object()
    ns = _lift(CREATE, {'_attach_named_for_create_step'},
               {'current_app': types.SimpleNamespace(logger=logging.getLogger('t'))})
    helper, assistant = types.SimpleNamespace(), types.SimpleNamespace()
    if armed:
        assistant._hart_attached_tools = set()
        assistant._hart_core_tools = [('get_chat_history', 'd', object())]
    modules = {'hartos.reuse_recipe': reuse, 'core.agent_tool_menu': menu,
               'hartos.lifecycle_hooks': hooks, 'integrations.service_tools': svc}
    return ns['_attach_named_for_create_step'], helper, assistant, svc, modules, calls, fits


def test_a_step_naming_crawl4ai_attaches_it_to_the_seats_that_propose_and_run():
    attach, helper, assistant, svc, modules, calls, fits = _rig()
    with swap_modules(modules):
        n = attach({'helper': helper, 'assistant': assistant}, STEP, 'u_1')
    assert n == 1
    assert [c['names'] for c in calls] == [['crawl4ai']]
    c = calls[0]
    assert c['helper'] is helper and c['executor'] is assistant
    assert c['registry'] is svc.service_tool_registry
    assert c['attached'] is assistant._hart_attached_tools
    assert c['core_tools'] is assistant._hart_core_tools
    assert [f[0] for f in fits] == [helper, assistant]
    assert all({'crawl4ai', 'send_message_to_user'} <= f[1] and f[2] for f in fits)


def test_a_step_that_names_no_tool_attaches_nothing():
    attach, helper, assistant, _svc, modules, calls, fits = _rig()
    with swap_modules(modules):
        n = attach({'helper': helper, 'assistant': assistant},
                   'Tell the person, in plain words, what was found', 'u_1')
    assert n == 0 and calls == [] and fits == []


def test_a_session_whose_attach_was_never_armed_attaches_nothing():
    attach, helper, assistant, _svc, modules, calls, _fits = _rig(armed=False)
    with swap_modules(modules):
        assert attach({'helper': helper, 'assistant': assistant}, STEP, 'u_1') == 0
    assert calls == []


def test_every_step_post_in_create_attaches_first():
    """Both places get_response_group posts a step (step_instruction) attach
    the step's named tools just before."""
    tree = ast.parse(open(CREATE, encoding='utf-8').read())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == 'get_response_group')
    lines = open(CREATE, encoding='utf-8').read().splitlines()
    posts = [n.lineno for n in ast.walk(fn)
             if isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'step_instruction']
    assert len(posts) == 2, posts
    for ln in posts:
        window = '\n'.join(lines[ln - 4:ln])
        assert '_attach_named_for_create_step(agents_object' in window, (ln, window)
