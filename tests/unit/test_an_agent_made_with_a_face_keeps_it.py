"""An agent made with a face keeps it (owner 28 Nov: "Create locally, sync up").

MEASURED live on build #7, 28 Nov (finding F11-a): of 5589 agent records on
the desktop, none had a teacher_avatar_id, so no agent opened in video and no
idle face was ever made.  Nunba's create form now starts the local
conversational creation with the face it uploaded, and names that face on
every creation turn, as a client names the goal tags.  The savers that write
an agent's record keep it: the face the saving turn names, else the one the
record already has.  GET /prompts lists the record's teacher_avatar_id.

hart_intelligence_entry cannot be imported in the test venv (it starts the
whole backend), so the REAL helper is taken from the source by AST and run
over a temporary prompts folder, as
tests/unit/test_an_unfinished_agent_never_takes_the_persons_words.py does.
"""
import ast
import functools
import io
import json
import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_HIE = os.path.join(_ROOT, 'hart_intelligence_entry.py')
_PID = 1793359802508


@functools.lru_cache(maxsize=1)
def _tree():
    return ast.parse(io.open(_HIE, encoding='utf-8', errors='replace').read())


def _stamp(prompts_dir):
    """The REAL _stamp_agent_face, bound to ``prompts_dir``."""
    fns = [n for n in _tree().body
           if isinstance(n, ast.FunctionDef) and n.name == '_stamp_agent_face']
    module = ast.Module(body=fns, type_ignores=[])
    ast.fix_missing_locations(module)
    ns = {'os': os, 'json': json, 'PROMPTS_DIR': str(prompts_dir)}
    exec(compile(module, _HIE, 'exec'), ns)
    return ns['_stamp_agent_face']


def _write_record(prompts_dir, record):
    with open(os.path.join(prompts_dir, f'{_PID}.json'), 'w', encoding='utf-8') as f:
        json.dump(record, f)


def test_the_face_the_creation_turn_names_is_saved(tmp_path):
    config = _stamp(tmp_path)({'name': 'Maxwell', 'flows': []}, _PID, 1000000017)
    assert config['teacher_avatar_id'] == 1000000017


def test_a_face_sent_as_a_numeric_string_is_saved_as_a_number(tmp_path):
    # Clients send a number or a numeric string (core.teacher_avatar.avatar_id_from).
    config = _stamp(tmp_path)({'name': 'Maxwell'}, _PID, '1000000017')
    assert config['teacher_avatar_id'] == 1000000017


def test_a_save_without_a_face_keeps_the_one_the_record_has(tmp_path):
    # A later save (a re-review, the autonomous save) names no face: the
    # record it writes over keeps the face it had.
    _write_record(tmp_path, {'name': 'Maxwell', 'teacher_avatar_id': 1000000017})
    config = _stamp(tmp_path)({'name': 'Maxwell', 'status': 'completed'}, _PID)
    assert config['teacher_avatar_id'] == 1000000017


def test_the_turns_face_replaces_the_records(tmp_path):
    _write_record(tmp_path, {'teacher_avatar_id': 1000000003})
    config = _stamp(tmp_path)({}, _PID, 1000000017)
    assert config['teacher_avatar_id'] == 1000000017


def test_an_agent_made_without_a_face_is_saved_as_before(tmp_path):
    config = _stamp(tmp_path)({'name': 'Coach', 'goal': 'g'}, _PID)
    assert config == {'name': 'Coach', 'goal': 'g'}


def test_a_face_in_the_models_config_is_not_kept(tmp_path):
    # Only the client or the record names a face: a model that writes an id
    # into its config would make the agent speak as someone else's avatar.
    config = _stamp(tmp_path)({'name': 'Coach', 'teacher_avatar_id': 7}, _PID)
    assert 'teacher_avatar_id' not in config


def test_what_is_no_avatar_id_falls_back_to_the_record(tmp_path):
    _write_record(tmp_path, {'teacher_avatar_id': 1000000017})
    stamp = _stamp(tmp_path)
    for bad in (True, 0, -4, '0', 'maxwell', {'id': 5}, ''):
        assert stamp({}, _PID, bad)['teacher_avatar_id'] == 1000000017, bad


def test_a_record_that_cannot_be_read_saves_no_face(tmp_path):
    with open(os.path.join(tmp_path, f'{_PID}.json'), 'w', encoding='utf-8') as f:
        f.write('{not json')
    assert 'teacher_avatar_id' not in _stamp(tmp_path)({}, _PID)
    _write_record(tmp_path, ['a', 'list'])
    assert 'teacher_avatar_id' not in _stamp(tmp_path)({}, _PID)


def _record_saves():
    """Every statement list that writes an agent record: an assignment of
    os.path.join(PROMPTS_DIR, f'{prompt_id}.json') followed, in the same
    list, by a `with open(<that name>, 'w')`."""
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
                for later in stmts[i + 1:]:
                    if isinstance(later, ast.With) and any(
                            ast.unparse(item.context_expr) in (
                                f"open({var}, 'w')", f'open({var}, "w")')
                            for item in later.items):
                        saves.append((stmts, i))
                        break
    return saves


def test_every_saver_of_an_agent_record_keeps_its_face():
    saves = _record_saves()
    # _save_and_enter_review and the two saves of _autonomous_gather_info.
    assert len(saves) == 3, len(saves)
    for stmts, i in saves:
        before = [ast.unparse(s) for s in stmts[:i]]
        assert any(s.startswith('_stamp_agent_face(') for s in before), before[-4:]


def test_the_interactive_saver_gets_the_face_this_request_names():
    chat = next(n for n in _tree().body
                if isinstance(n, ast.FunctionDef) and n.name == '_chat_view')
    reads = [n for n in ast.walk(chat) if isinstance(n, ast.Assign)
             and ast.unparse(n.targets[0]) == '_turn_avatar']
    assert len(reads) == 1
    # Run the real read against request bodies: only a body that says it
    # creates an agent names the agent's face (review of the first draft, S3:
    # on any other turn the key is the avatar one reply is spoken as).
    read = compile(ast.Expression(reads[0].value), _HIE, 'eval')
    face = 1000000017
    assert eval(read, {'data': {'create_agent': True, 'teacher_avatar_id': face}}) == face
    assert eval(read, {'data': {'teacher_avatar_id': face}}) is None
    assert eval(read, {'data': {'create_agent': False, 'teacher_avatar_id': face}}) is None
    assert eval(read, {'data': {'create_agent': 'yes', 'teacher_avatar_id': face}}) is None
    saver = next(n for n in ast.walk(chat) if isinstance(n, ast.FunctionDef)
                 and n.name == '_save_and_enter_review')
    calls = [ast.unparse(n) for n in ast.walk(saver) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == '_stamp_agent_face']
    assert calls == ['_stamp_agent_face(agent_config, prompt_id, _turn_avatar)']


def _pulled(tmp_path, record, prompt_id='345'):
    """``record`` as another node's bundle writes it here (the one writer of
    pulled bundles: core.recipe_sync.write_envelope_files)."""
    from core.recipe_sync import write_envelope_files
    files = {f'{prompt_id}.json': json.dumps(record),
             f'{prompt_id}_0_recipe.json': json.dumps({'teacher_avatar_id': 1000000017})}
    assert write_envelope_files(str(tmp_path), {'prompt_id': prompt_id, 'files': files}) == 2
    with open(os.path.join(tmp_path, f'{prompt_id}.json'), encoding='utf-8') as f:
        return json.load(f)


def test_another_desktops_own_face_id_is_not_written_here(tmp_path):
    # Each desktop numbers its own avatars from 1,000,000,000 up, so on this
    # node that id is a different avatar or none (review S1).
    written = _pulled(tmp_path, {'prompt_id': 345, 'name': 'Maxwell',
                                 'teacher_avatar_id': 1000000017})
    assert written == {'prompt_id': 345, 'name': 'Maxwell'}


def test_a_central_face_id_comes_with_the_agent(tmp_path):
    written = _pulled(tmp_path, {'prompt_id': 345, 'teacher_avatar_id': 2759})
    assert written['teacher_avatar_id'] == 2759


def test_only_the_agent_file_is_read_for_a_face(tmp_path):
    _pulled(tmp_path, {'prompt_id': 345})
    with open(os.path.join(tmp_path, '345_0_recipe.json'), encoding='utf-8') as f:
        assert json.load(f) == {'teacher_avatar_id': 1000000017}


def test_an_agent_file_that_is_not_a_record_is_written_as_it_came(tmp_path):
    from core.recipe_sync import write_envelope_files
    write_envelope_files(str(tmp_path), {'prompt_id': '9', 'files': {'9.json': '[1, 2]'}})
    with open(os.path.join(tmp_path, '9.json'), encoding='utf-8') as f:
        assert f.read() == '[1, 2]'
