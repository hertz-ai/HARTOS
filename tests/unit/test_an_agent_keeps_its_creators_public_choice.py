"""An agent keeps its creator's public/private choice (owner 28 Nov, "Bring it
back now").

Nunba's create form asks "Make this agent public"; the page names the answer
on each creation turn, as it names the face; the savers of an agent record keep
it (_stamp_agent_public); GET /prompts/public leaves out an agent its creator
made private; and the agent's row to central carries it
(sync_engine.list_agent_on_central).  A record with no choice is public, as
before.

hart_intelligence_entry cannot be imported in the test venv (it starts the
whole backend), so the REAL pieces are taken from the source by AST, as
tests/unit/test_an_agent_made_with_a_face_keeps_it.py does.
"""
import ast
import functools
import io
import json
import os
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_HIE = os.path.join(_ROOT, 'hart_intelligence_entry.py')
_PID = 1793359802508


@functools.lru_cache(maxsize=1)
def _tree():
    return ast.parse(io.open(_HIE, encoding='utf-8', errors='replace').read())


def _stamp(prompts_dir):
    fns = [n for n in _tree().body
           if isinstance(n, ast.FunctionDef) and n.name == '_stamp_agent_public']
    module = ast.Module(body=fns, type_ignores=[])
    ast.fix_missing_locations(module)
    ns = {'os': os, 'json': json, 'PROMPTS_DIR': str(prompts_dir)}
    exec(compile(module, _HIE, 'exec'), ns)
    return ns['_stamp_agent_public']


def _write_record(prompts_dir, record):
    with open(os.path.join(prompts_dir, f'{_PID}.json'), 'w', encoding='utf-8') as f:
        json.dump(record, f)


def test_the_creation_turns_choice_is_saved(tmp_path):
    assert _stamp(tmp_path)({'name': 'Maxwell'}, _PID, False)['is_public'] is False
    assert _stamp(tmp_path)({'name': 'Maxwell'}, _PID, True)['is_public'] is True


def test_a_save_without_a_choice_keeps_the_records(tmp_path):
    _write_record(tmp_path, {'is_public': False})
    assert _stamp(tmp_path)({'status': 'completed'}, _PID)['is_public'] is False


def test_the_turns_choice_replaces_the_records(tmp_path):
    _write_record(tmp_path, {'is_public': False})
    assert _stamp(tmp_path)({}, _PID, True)['is_public'] is True


def test_no_choice_anywhere_writes_none(tmp_path):
    assert _stamp(tmp_path)({'name': 'Coach'}, _PID) == {'name': 'Coach'}


def test_only_a_true_bool_is_a_choice(tmp_path):
    stamp = _stamp(tmp_path)
    for not_a_choice in ('false', 0, 1, None, 'yes', [False]):
        assert 'is_public' not in stamp({}, _PID, not_a_choice), not_a_choice
    _write_record(tmp_path, {'is_public': 'false'})
    assert 'is_public' not in stamp({}, _PID)


def test_a_choice_in_the_models_config_is_not_kept(tmp_path):
    assert 'is_public' not in _stamp(tmp_path)({'is_public': False}, _PID)


def _record_saves():
    """Every statement list that writes an agent record (as in the face test)."""
    saves = []
    for node in ast.walk(_tree()):
        for field in ('body', 'orelse', 'finalbody'):
            stmts = getattr(node, field, None)
            if not isinstance(stmts, list):
                continue
            for i, st in enumerate(stmts):
                if not (isinstance(st, ast.Assign) and len(st.targets) == 1
                        and isinstance(st.targets[0], ast.Name)):
                    continue
                src = ast.unparse(st.value)
                if not (src.startswith('os.path.join(PROMPTS_DIR')
                        and "{prompt_id}.json'" in src):
                    continue
                var = st.targets[0].id
                if any(isinstance(later, ast.With) and any(
                        ast.unparse(item.context_expr) in (
                            f"open({var}, 'w')", f'open({var}, "w")')
                        for item in later.items) for later in stmts[i + 1:]):
                    saves.append((stmts, i))
    return saves


def test_every_saver_of_an_agent_record_keeps_the_choice():
    saves = _record_saves()
    assert len(saves) == 3, len(saves)
    for stmts, i in saves:
        before = [ast.unparse(s) for s in stmts[:i]]
        assert any(s.startswith('_stamp_agent_public(') for s in before), before[-4:]


def test_the_choice_is_read_only_from_a_creation_body():
    chat = next(n for n in _tree().body
                if isinstance(n, ast.FunctionDef) and n.name == '_chat_view')
    reads = [n for n in ast.walk(chat) if isinstance(n, ast.Assign)
             and ast.unparse(n.targets[0]) == '_turn_public']
    assert len(reads) == 1
    read = compile(ast.Expression(reads[0].value), _HIE, 'eval')
    assert eval(read, {'data': {'create_agent': True, 'is_public': False}}) is False
    assert eval(read, {'data': {'is_public': False}}) is None
    assert eval(read, {'data': {'create_agent': 'true', 'is_public': False}}) is None
    saver = next(n for n in ast.walk(chat) if isinstance(n, ast.FunctionDef)
                 and n.name == '_save_and_enter_review')
    calls = [ast.unparse(n) for n in ast.walk(saver) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == '_stamp_agent_public']
    assert calls == ['_stamp_agent_public(agent_config, prompt_id, _turn_public)']


def test_the_public_list_leaves_out_a_private_agent():
    """GET /prompts/public's loop over the local records, run as written over
    three records: private, public, and one that names no choice."""
    route = next(n for n in _tree().body
                 if isinstance(n, ast.FunctionDef) and n.name == 'get_public_prompts')
    loop = next(n for n in route.body if isinstance(n, ast.For))
    fn = ast.parse('def _run(records):\n    prompts = []\n    return prompts\n').body[0]
    loop.iter = ast.Name(id='records', ctx=ast.Load())
    fn.body.insert(1, loop)
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    ns = {'os': os, 'PROMPTS_DIR': '/nowhere', 'app': MagicMock()}
    exec(compile(module, _HIE, 'exec'), ns)
    listed = ns['_run']([(1, {'name': 'Private', 'is_public': False}),
                         (2, {'name': 'Public', 'is_public': True}),
                         (3, {'name': 'Older'})])
    assert [p['prompt_id'] for p in listed] == [2, 3]


def test_the_row_to_central_carries_the_choice():
    from integrations.social import sync_engine
    sent = []

    def _post(url, json=None, **kw):
        sent.append(json['listprompts'][0])
        resp = MagicMock(status_code=200)
        resp.json.return_value = {'prompt_ids': [345]}
        return resp

    with patch('core.config_cache.get_central_db_url', return_value='https://central.test'), \
         patch('core.http_pool.pooled_post', side_effect=_post):
        for record in ({'name': 'M', 'goal': 'g', 'is_public': False},
                       {'name': 'M', 'goal': 'g', 'is_public': True},
                       {'name': 'M', 'goal': 'g'},
                       {'name': 'M', 'goal': 'g', 'is_public': 'no'}):
            assert sync_engine.list_agent_on_central(record, '7', 'login') == 345
    assert [row.get('is_public', 'absent') for row in sent] == [False, True, 'absent', 'absent']


# ── Review of the public choice (should-fix 2, 4): every place a private agent
#    could still go out ──────────────────────────────────────────────────────

def test_made_private_reads_only_an_explicit_false(tmp_path):
    from core.prompt_files import made_private
    for pid, record in (('1', {'is_public': False}), ('2', {'is_public': True}),
                        ('3', {}), ('4', ['not', 'a', 'record'])):
        with open(os.path.join(tmp_path, f'{pid}.json'), 'w', encoding='utf-8') as f:
            json.dump(record, f)
    with open(os.path.join(tmp_path, '5.json'), 'w', encoding='utf-8') as f:
        f.write('{broken')
    assert made_private(str(tmp_path), '1') is True
    for pid in ('2', '3', '4', '5', 'missing', None, ''):
        assert made_private(str(tmp_path), pid) is False, pid


def test_a_private_agent_is_offered_to_no_one_whatever_the_consent(tmp_path):
    """The agent gate sends a discoverable agent to central and peers on the
    owner's public_exposure consent, which is on whenever autonomous
    marketing is; a private agent now never passes it."""
    from types import SimpleNamespace
    from integrations.social import sync_engine
    with open(os.path.join(tmp_path, '77.json'), 'w', encoding='utf-8') as f:
        json.dump({'is_public': False}, f)
    with open(os.path.join(tmp_path, '78.json'), 'w', encoding='utf-8') as f:
        json.dump({'name': 'older, no choice'}, f)
    # The resolver the savers write through (hart_intelligence_entry PROMPTS_DIR);
    # get_prompts_dir differs from it on a dev or Docker HARTOS (re-review).
    with patch('core.platform_paths.get_recipe_prompts_dir', return_value=str(tmp_path)), \
         patch('integrations.social.consent_service.ConsentService.check_consent',
               return_value=True) as consent:
        assert sync_engine._agent_gate(None, SimpleNamespace(agent_id='77', owner_id='u'), None) is False
        consent.assert_not_called()
        assert sync_engine._agent_gate(None, SimpleNamespace(agent_id='78', owner_id='u'), None) is True
        assert sync_engine._agent_gate(None, SimpleNamespace(agent_id=None, owner_id='u'), None) is True


def test_every_savers_list_row_carries_the_choice():
    """The three savers' own createpromptlist rows (to DB_URL: central on a
    standalone HARTOS) send the record's choice, so a private agent is not
    public there between its save and its completion."""
    rows = [n for n in ast.walk(_tree()) if isinstance(n, ast.Dict)
            and any(isinstance(k, ast.Constant) and k.value == 'image_url' for k in n.keys)
            and any(isinstance(k, ast.Constant) and k.value == 'is_active' for k in n.keys)
            and any(ast.unparse(v).startswith(("parsed.get('image_url'",
                                               "agent_config.get('image_url'"))
                    for v in n.values)]
    assert len(rows) == 3, len(rows)
    for row in rows:
        spreads = [ast.unparse(v) for k, v in zip(row.keys, row.values) if k is None]
        assert any("'is_public'" in s for s in spreads), ast.unparse(row)[:200]
    # and the spread is right: only a true bool goes out
    spread = next(v for k, v in zip(rows[0].keys, rows[0].values) if k is None)
    run = compile(ast.Expression(spread), _HIE, 'eval')
    assert eval(run, {'parsed': {'is_public': False}}) == {'is_public': False}
    assert eval(run, {'parsed': {}}) == {}
    assert eval(run, {'parsed': {'is_public': 'no'}}) == {}
