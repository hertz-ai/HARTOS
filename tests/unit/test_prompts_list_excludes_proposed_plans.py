"""GET /prompts and /prompts/public list agents, not plans awaiting review.

The CREATE path stages every proposed plan beside the agents in the prompts
dir (``{pid}.proposed.r{n}.json`` per review round, ``{pid}.proposed.json``
for the one handed to a peer reviewer).  The listing's old filter,
``'_' not in fname``, let every one of them through as an agent: measured
2026-09-26, the owner's list grew from 20 to 26 with rows such as
``79991757345.proposed.r0``; the live prompts dir holds 503 such files next
to 852 agents.

The writer and the reader now share ONE naming contract in
``core.prompt_files``; these tests write with the writer's own names and
read back with the listing both routes call.
"""
import json
import os

import pytest

from core import prompt_files as pf


def _write(d, name, data):
    with open(os.path.join(d, name), 'w', encoding='utf-8') as f:
        json.dump(data, f)


@pytest.fixture
def prompts_dir(tmp_path):
    d = str(tmp_path)
    agent = {'name': 'A', 'creator_user_id': 'u1', 'status': 'completed'}
    _write(d, '79991757345.json', agent)
    _write(d, 'c38e8b7c-ccbc-4127-a0a4-7604f69f9203.json', {'name': 'U'})
    for r in range(3):
        _write(d, pf.proposed_plan_filename('79991757345', r),
               {'name': 'A', 'creator_user_id': 'u1', 'flows': []})
    _write(d, pf.proposed_plan_filename('79991757345'), {'name': 'A'})
    _write(d, '79991757345_0_recipe.json', {})
    _write(d, '79991757345_personality.json', {})
    with open(os.path.join(d, '5.json'), 'w') as f:
        f.write('{not json')
    _write(d, '6.json', ['not', 'an', 'agent'])
    with open(os.path.join(d, 'notes.txt'), 'w') as f:
        f.write('x')
    return d


def test_listing_holds_agents_only(prompts_dir):
    ids = sorted(pid for pid, _ in pf.local_agent_prompts(prompts_dir))
    assert ids == ['79991757345', 'c38e8b7c-ccbc-4127-a0a4-7604f69f9203']


def test_listing_hands_back_the_agent_record(prompts_dir):
    rows = dict(pf.local_agent_prompts(prompts_dir))
    assert rows['79991757345'] == {
        'name': 'A', 'creator_user_id': 'u1', 'status': 'completed'}


def test_listing_of_a_missing_dir_is_empty(tmp_path):
    assert pf.local_agent_prompts(str(tmp_path / 'nope')) == []


@pytest.mark.parametrize('pid', ['79991757345', 7, 'c38e8b7c-ccbc-4127'])
@pytest.mark.parametrize('rnd', [None, 0, 2, 11])
def test_every_name_the_writer_makes_is_a_proposed_plan(pid, rnd):
    name = pf.proposed_plan_filename(pid, rnd)
    assert name.endswith('.json')
    assert name.startswith(f'{pid}.proposed')
    assert pf.is_proposed_plan_filename(name)


@pytest.mark.parametrize('name', [
    '79991757345.json', 'c38e8b7c-ccbc-4127-a0a4-7604f69f9203.json',
    '79991757345_0_recipe.json', 'proposed.json', '12.proposedx.json',
    '12.proposed.rx.json', '12.proposed.r0.json.bak',
])
def test_other_names_are_not_proposed_plans(name):
    assert not pf.is_proposed_plan_filename(name)


def test_round_names_are_distinct_per_round():
    names = {pf.proposed_plan_filename('9', r) for r in (None, 0, 1, 2)}
    assert len(names) == 4


# --- the two routes, run from their shipping source -------------------------
# hart_intelligence_entry cannot be imported here (it boots the whole
# runtime), so the two route functions are lifted out of it by AST and run
# on a fresh Flask app with the module-level names they read supplied.

_ENTRY = os.path.join(os.path.dirname(__file__), '..', '..',
                      'hart_intelligence_entry.py')


def _routes_client(prompts_dir):
    import ast
    import flask
    with open(_ENTRY, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    wanted = {'get_prompts', 'get_public_prompts'}
    nodes = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert {n.name for n in nodes} == wanted
    app = flask.Flask('prompts_routes_test')
    ns = {'app': app, 'request': flask.request, 'jsonify': flask.jsonify,
          'os': os, 'json': json, 'PROMPTS_DIR': prompts_dir,
          'CENTRAL_DB_URL': None}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), _ENTRY, 'exec'), ns)
    return app.test_client()


def test_get_prompts_lists_the_owners_agents_not_their_plans(prompts_dir):
    rows = _routes_client(prompts_dir).get('/prompts?user_id=u1').get_json()
    assert sorted(r['prompt_id'] for r in rows) == [
        '79991757345', 'c38e8b7c-ccbc-4127-a0a4-7604f69f9203']
    agent = next(r for r in rows if r['prompt_id'] == '79991757345')
    assert agent['is_active'] is True and agent['user_id'] == 'u1'


def test_get_public_prompts_lists_agents_not_plans(prompts_dir):
    rows = _routes_client(prompts_dir).get('/prompts/public').get_json()
    assert sorted(r['prompt_id'] for r in rows) == [
        '79991757345', 'c38e8b7c-ccbc-4127-a0a4-7604f69f9203']


def test_source_guard_entry_names_plans_only_through_prompt_files():
    """The writer's names come from proposed_plan_filename, so the reader's
    is_proposed_plan_filename is guaranteed to know them."""
    with open(_ENTRY, encoding='utf-8') as f:
        src = f.read()
    assert ".proposed" not in src.replace(
        'core.prompt_files', '').replace('proposed_plan_filename', '')
    assert src.count('proposed_plan_filename(prompt_id') == 2


# --- the other two agent listings: the router's catalog and MCP list_agents --

_AGENTS = ['79991757345', 'c38e8b7c-ccbc-4127-a0a4-7604f69f9203']


def test_router_catalog_lists_agents_not_plans(prompts_dir, monkeypatch):
    """agentic_router._build_agent_catalog skipped only '_recipe' names, so
    every staged plan (and every action/personality file) was offered to the
    matcher LLM as an agent it could route the user's task to."""
    import sys
    from integrations.agentic_router import _build_agent_catalog
    for other_source in ('integrations.expert_agents.registry',
                         'integrations.agent_engine.federated_aggregator',
                         'integrations.google_a2a.dynamic_agent_registry'):
        monkeypatch.setitem(sys.modules, other_source, None)
    rows = [r for r in _build_agent_catalog(prompts_dir)
            if r['source'] == 'recipe']
    assert sorted(r['id'] for r in rows) == _AGENTS
    assert next(r for r in rows if r['id'] == '79991757345')['name'] == 'A'


def test_mcp_list_agents_lists_agents_not_plans(prompts_dir, monkeypatch):
    """MCP list_agents globbed every prompts JSON as a 'dynamic' agent:
    plans, recipes, action and personality files alike."""
    from integrations.mcp import _tool_impls as tools

    class _NoExperts:
        agents = {}

    def _no_db():
        raise RuntimeError('no social DB in this test')

    monkeypatch.setattr(tools, 'get_recipe_prompts_dir', lambda: prompts_dir)
    monkeypatch.setattr(tools, '_get_registry', lambda: _NoExperts())
    monkeypatch.setattr(tools, '_get_db', _no_db)
    out = json.loads(tools.list_agents())
    assert sorted(d['agent_id'] for d in out['dynamic']) == _AGENTS
    assert out['dynamic_agents'] == 2


# --- source guard: every enumeration of the prompts dir is accounted for ----

#: Shipped code that enumerates the prompts dir WITHOUT local_agent_prompts,
#: each for a reason that is not "list the agents".  A new enumeration fails
#: test_source_guard_every_prompts_dir_listing_is_accounted_for until it
#: either reads through local_agent_prompts or is added here with its reason.
_NOT_AGENT_LISTINGS = {
    ('core/prompt_files.py', 'local_agent_prompts'): 'the one agent reader',
    ('core/flow_recipe_reconcile.py', '_flows_needing_reconcile'):
        'flow recipe files',
    ('core/prompts_backup.py', 'snapshot_prompts'): 'backs up every file',
    ('core/recipe_sync.py', '_files_for_prompt'): "one prompt id's files",
    ('hartos/create_recipe.py',
     'create_agents.execute_windows_or_android_command'):
        "one prompt id's VLM files",
    ('hartos/reuse_recipe.py',
     'create_agents_for_user.execute_windows_or_android_command'):
        "one prompt id's VLM files",
    ('hartos/helper.py', 'load_vlm_agent_files'): "one prompt id's VLM files",
    ('hartos/hart_cli.py', 'recipe_list'): '*_recipe.json only',
    ('hartos/hart_cli.py', 'recipe_show'): '*_recipe.json only',
    ('hartos/hart_cli.py', 'a2a_agents'): '*_recipe.json only',
    ('integrations/agent_engine/ip_service.py', 'IPService.measure_moat_depth'):
        '*_recipe.json only',
    ('integrations/google_a2a/dynamic_agent_registry.py',
     'DynamicAgentDiscovery._load_prompt_definitions'):
        'definitions by int stem; its agents are *_*_recipe.json',
    ('integrations/mcp/_tool_impls.py', 'list_recipes'):
        'lists every file by name, by design',
    # OPEN, not a blessing: counts every non-recipe JSON as a prompt, so
    # staged plans inflate recipe_adoption.total_prompts.  Reported with the
    # fix of the two listings above; routed to the owner (review of 77a7191a7).
    ('integrations/agent_engine/ip_service.py', 'IPService.get_loop_health'):
        'OPEN: counts plans as prompts',
}

_ENUMERATORS = {'listdir', 'scandir', 'glob', 'iglob', 'iterdir', 'walk',
                'rglob'}


def _prompts_dir_enumerations():
    """(file, def path) of every call in shipped code that enumerates a
    directory named after the prompts dir (PROMPTS_DIR, prompts_dir,
    get_recipe_prompts_dir(), 'prompts/...').  Enumerates by the question,
    not by a symbol: a listing that never mentions local_agent_prompts is
    exactly the one to find.  Blind spot, stated: a prompts dir held in a
    variable whose name does not say 'prompt'."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    shipped = [root / 'hart_intelligence_entry.py']
    for pkg in ('core', 'hartos', 'integrations', 'security'):
        shipped += [p for p in (root / pkg).rglob('*.py')
                    if '__pycache__' not in p.parts]
    found = set()

    def visit(node, owner, rel):
        for child in ast.iter_child_nodes(node):
            here = owner
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                here = child.name if owner is None else f'{owner}.{child.name}'
            if isinstance(child, ast.Call):
                fn = child.func
                name = (fn.id if isinstance(fn, ast.Name)
                        else fn.attr if isinstance(fn, ast.Attribute) else None)
                if name in _ENUMERATORS:
                    text = ' '.join(ast.unparse(a) for a in child.args)
                    if isinstance(fn, ast.Attribute):
                        text += ' ' + ast.unparse(fn.value)
                    if 'prompt' in text.lower():
                        found.add((rel, here or '<module>'))
            visit(child, here, rel)

    for path in shipped:
        rel = path.relative_to(root).as_posix()
        visit(ast.parse(path.read_text(encoding='utf-8')), None, rel)
    return found


def test_source_guard_every_prompts_dir_listing_is_accounted_for():
    found = _prompts_dir_enumerations()
    unaccounted = found - set(_NOT_AGENT_LISTINGS)
    assert not unaccounted, (
        f'{sorted(unaccounted)} enumerate the prompts dir without '
        'core.prompt_files.local_agent_prompts; an agent listing must read '
        'through it (or staged plans are listed as agents), anything else '
        'belongs in _NOT_AGENT_LISTINGS with its reason')
    stale = set(_NOT_AGENT_LISTINGS) - found
    assert not stale, f'allow-list entries that no longer enumerate: {sorted(stale)}'
