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
