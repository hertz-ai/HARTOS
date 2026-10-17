"""An agent's memory belongs to one person on one agent (owner ruling 2026-10-17).

save_data_in_memory and get_data_by_key kept ONE store per agent, shared by
everyone who used it, on every topology.  Measured 2026-10-14 on the owner's
desktop: a listed-agents sweep ran tutor agent 54 as the owner's desktop
identity, its save of the key `teach` replaced the whole `teach` entry, and
the creator's stored progress (Quantum Mechanics, chapter 3) was gone
(docs/internal/verification/USER_JOURNEYS_LIVE_2026-10-13.md section 11.1).
The owner's answer to "how should it work": per person + agent, the
`{user_id}_{prompt_id}` key the session already uses.

These tests drive the real tools over the real on-disk format
(core.cache_loaders writes and reads `<id>_agent_data.json`, the format
hartos.helper uses) in a temporary folder, with the same restoring
TTLCache the recipe pipelines hold.

    python -m pytest tests/unit/test_agent_memory_is_per_person.py -q
"""
import json
import os
import re
from types import SimpleNamespace

import pytest

from core import cache_loaders
from core.agent_tools import build_core_tool_closures
from core.session_cache import TTLCache

AGENT = '54'
CREATOR = '10202'
VISITOR = 'd68c9dee-b324-4c04-86c4-1205a836957f'
NOT_FOUND = 'Key not found in stored data.'


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_loaders, 'AGENT_DATA_DIR', str(tmp_path))
    return tmp_path


@pytest.fixture
def creator_config(tmp_path, monkeypatch):
    """Agent 54's definition with its creator, as prompts/54.json holds it
    on the desktop (creator_user_id 10202)."""
    prompts = tmp_path / 'prompts'
    prompts.mkdir()
    (prompts / f'{AGENT}.json').write_text(
        json.dumps({'prompt_id': AGENT, 'creator_user_id': CREATOR}))
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(prompts))
    monkeypatch.setattr(cache_loaders, '_agent_config_cache', {})
    return prompts


class _DiskHelper:
    """The two store calls the memory tools make, on core.cache_loaders'
    file format (hartos.helper's twins need a Flask app context)."""

    def save_agent_data_to_file(self, store_id, store):
        return cache_loaders.save_agent_data(store_id, store.get(store_id, {}))

    def load_agent_data_from_file(self, store_id, store):
        data = cache_loaders.load_agent_data(store_id)
        store[store_id] = data if data is not None else {}
        return data is not None


def _store():
    """What create_recipe / reuse_recipe hold: a TTLCache that restores
    from disk."""
    return TTLCache(ttl_seconds=7200, max_size=500, name='test_agent_data',
                    loader=cache_loaders.load_agent_data)


def _tools(store, user_id, prompt_id=AGENT, helper_fun=None, building=False):
    ctx = {
        'building_agent': building,
        'user_id': user_id, 'prompt_id': prompt_id, 'agent_data': store,
        'helper_fun': helper_fun or _DiskHelper(),
        'user_prompt': f'{user_id}_{prompt_id}',
        'request_id_list': {}, 'recent_file_id': None, 'scheduler': None,
        'send_message_to_user1': lambda *a, **k: None,
        'retrieve_json': json.loads,
        'strip_json_values': lambda d: d,
        'save_conversation_db': lambda *a, **k: None,
    }
    return {name: fn for name, _desc, fn in build_core_tool_closures(ctx)}


def test_another_persons_save_never_erases_mine(store_dir):
    """The 10-14 loss, replayed: the creator's progress sits under
    `teach.<creator>`, another person saves the key `teach` itself."""
    store = _store()
    creator, visitor = _tools(store, CREATOR), _tools(store, VISITOR)
    creator['save_data_in_memory'](
        f'teach.{CREATOR}', {'book': 'Quantum Mechanics', 'chapter': 3})
    visitor['save_data_in_memory']('teach', [{'topic': 'Fractions'}])

    mine = creator['get_data_by_key'](f'teach.{CREATOR}')
    assert 'Quantum Mechanics' in mine


def test_a_person_cannot_read_what_another_saved(store_dir):
    store = _store()
    _tools(store, CREATOR)['save_data_in_memory']('notes', {'secret': 'mine'})
    assert _tools(store, VISITOR)['get_data_by_key']('notes') == NOT_FOUND


def test_a_save_is_written_under_the_person_and_agent_key(store_dir):
    store = _store()
    _tools(store, CREATOR)['save_data_in_memory']('level', 2)
    assert (store_dir / f'{CREATOR}_{AGENT}_agent_data.json').is_file()
    assert not (store_dir / f'{AGENT}_agent_data.json').exists()


def test_a_persons_memory_survives_a_restart(store_dir):
    _tools(_store(), CREATOR)['save_data_in_memory'](
        'progress', {'chapter': 3})
    # A new process: a fresh cache restores from disk.
    after = _store()
    assert '3' in _tools(after, CREATOR)['get_data_by_key']('progress.chapter')
    assert _tools(after, VISITOR)['get_data_by_key']('progress') == NOT_FOUND


def test_a_first_save_after_a_restart_adds_to_the_file(store_dir):
    """A store with no disk loader (a plain dict) still restores the
    person's file before the first save, so the save adds a key instead of
    replacing the file with it."""
    _tools({}, CREATOR)['save_data_in_memory']('a', 1)
    _tools({}, CREATOR)['save_data_in_memory']('b', 2)
    assert cache_loaders.load_agent_data(f'{CREATOR}_{AGENT}') == {'a': 1, 'b': 2}


def test_what_the_agent_already_holds_stays_readable_by_everyone(store_dir):
    """Entries saved before this change, and entries the agent itself keeps
    (CREATE metadata, game sounds), stay in the agent's own store and are
    read under every person's own."""
    cache_loaders.save_agent_data(AGENT, {'receipt_template': 'Bill {amount}'})
    store = _store()
    creator, visitor = _tools(store, CREATOR), _tools(store, VISITOR)
    assert creator['get_data_by_key']('receipt_template') == 'Bill {amount}'
    assert visitor['get_data_by_key']('receipt_template') == 'Bill {amount}'

    creator['save_data_in_memory']('receipt_template', 'My bill {amount}')
    assert creator['get_data_by_key']('receipt_template') == 'My bill {amount}'
    assert visitor['get_data_by_key']('receipt_template') == 'Bill {amount}'
    # The agent's own store is never written by a person's save.
    assert cache_loaders.load_agent_data(AGENT) == {
        'receipt_template': 'Bill {amount}'}


def test_while_building_a_template_can_be_saved_for_everyone(store_dir,
                                                              creator_config):
    """Owner 2026-10-17: what the creator saves while building is theirs,
    "only the template not the data unless it can serve as example" goes
    to everyone."""
    store = _store()
    builder = _tools(store, CREATOR, building=True)
    out = builder['save_data_in_memory'](
        'receipt_template', 'Bill for {service}: {amount}', for_everyone=True)
    assert 'for everyone' in out
    builder['save_data_in_memory']('client', {'name': 'Priya'})

    visitor = _tools(_store(), VISITOR)
    assert visitor['get_data_by_key']('receipt_template') == \
        'Bill for {service}: {amount}'
    assert visitor['get_data_by_key']('client') == NOT_FOUND
    assert cache_loaders.load_agent_data(AGENT) == {
        'receipt_template': 'Bill for {service}: {amount}'}


def test_outside_building_for_everyone_stays_personal(store_dir):
    """A user of a finished agent cannot write the agent's shared store:
    that is the 10-14 erase again."""
    store = _store()
    out = _tools(store, VISITOR)['save_data_in_memory'](
        'teach', [{'topic': 'Fractions'}], for_everyone=True)
    assert 'for this person only' in out
    assert cache_loaders.load_agent_data(AGENT) is None
    assert _tools(store, CREATOR)['get_data_by_key']('teach') == NOT_FOUND


def test_a_non_creator_in_create_cannot_save_for_everyone(store_dir,
                                                          creator_config):
    """Any user lands in CREATE on a half-built agent, so building alone is
    not the creator: the review of 213d10b43 replayed the 10-14 erase with a
    visitor's for_everyone save of `teach` there."""
    cache_loaders.save_agent_data(
        AGENT, {'teach': {CREATOR: {'book': 'Quantum Mechanics'}}})
    store = _store()
    out = _tools(store, VISITOR, building=True)['save_data_in_memory'](
        'teach', [{'topic': 'Fractions'}], for_everyone=True)
    assert 'for this person only' in out
    assert cache_loaders.load_agent_data(AGENT) == {
        'teach': {CREATOR: {'book': 'Quantum Mechanics'}}}
    assert 'Quantum Mechanics' in _tools(_store(), CREATOR)['get_data_by_key'](
        f'teach.{CREATOR}')


def test_the_string_false_shares_nothing(store_dir, creator_config):
    """autogen hands the tool the model's arguments as given, so the flag
    can arrive as the string "false"; bool("false") is True."""
    store = _store()
    _tools(store, CREATOR, building=True)['save_data_in_memory'](
        'client', {'name': 'Priya'}, for_everyone='false')
    assert cache_loaders.load_agent_data(AGENT) is None
    assert _tools(store, VISITOR)['get_data_by_key']('client') == NOT_FOUND


def _function_source(name, end_marker):
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    src = open(os.path.join(root, 'hartos', 'create_recipe.py'),
               encoding='utf-8').read()
    start = src.index(f'\ndef {name}(')
    return src[start:src.index(end_marker, start)]


def test_create_resume_restores_the_creators_own_memory(tmp_path):
    """load_existing_metadata, run from its source (create_recipe cannot be
    imported in a bare test env): an action JSON's metadata is the
    creator's saved-key snapshot, so a resumed CREATE puts it in the
    creator's own memory, never in the agent's store, where it read back as
    values for every other user (review of 7121264b1)."""
    import logging
    action = tmp_path / '54_0_1.json'
    action.write_text(json.dumps({'metadata': {'notes': {'secret': None}}}))
    saved = []
    helper = SimpleNamespace(
        load_agent_data_from_file=lambda pid, store: (store.__setitem__(pid, {}), False)[1],
        safe_prompt_path=lambda pid, flow, aid: str(action),
        save_agent_data_to_file=lambda pid, store: saved.append(pid) or True)
    from core.agent_tools import person_store_id
    ns = {'helper_fun': helper, 'agent_data': {}, 'json': json,
          'current_app': SimpleNamespace(logger=logging.getLogger('t'))}
    exec(_function_source('load_existing_metadata', '\n\n\n'), ns)
    user_prompt = f'{CREATOR}_{AGENT}'
    ns['load_existing_metadata'](AGENT, user_prompt,
                                 {0: {'completed_actions': [1]}})
    own = person_store_id(user_prompt)
    assert ns['agent_data'][own] == {'notes': {'secret': None}}
    assert 'notes' not in ns['agent_data'].get(AGENT, {})
    assert saved == [own]


def test_create_resume_leaves_an_existing_creator_memory_alone(tmp_path):
    """The restore runs only when the creator's memory is empty: a resumed
    CREATE must not lay the stripped snapshot (values None) over the
    creator's real saves."""
    import logging
    action = tmp_path / '54_0_1.json'
    action.write_text(json.dumps({'metadata': {'notes': {'secret': None}}}))
    saved = []
    helper = SimpleNamespace(
        load_agent_data_from_file=lambda pid, store: (store.__setitem__(pid, {}), False)[1],
        safe_prompt_path=lambda pid, flow, aid: str(action),
        save_agent_data_to_file=lambda pid, store: saved.append(pid) or True)
    from core.agent_tools import person_store_id
    user_prompt = f'{CREATOR}_{AGENT}'
    own = person_store_id(user_prompt)
    ns = {'helper_fun': helper, 'agent_data': {own: {'notes': {'secret': 'mine'}}},
          'json': json, 'current_app': SimpleNamespace(logger=logging.getLogger('t'))}
    exec(_function_source('load_existing_metadata', '\n\n\n'), ns)
    ns['load_existing_metadata'](AGENT, user_prompt,
                                 {0: {'completed_actions': [1]}})
    assert ns['agent_data'][own] == {'notes': {'secret': 'mine'}}
    assert saved == []


def test_a_nested_save_keeps_the_agent_entrys_other_fields(store_dir):
    """Agent 54's shape: the agent's own store holds a record saved before
    the ruling; the creator then saves one field of it.  The read is the
    merged view, not the first store that has the key (review of 7121264b1:
    the book vanished from get_data_by_key while get_saved_metadata still
    listed it)."""
    cache_loaders.save_agent_data(
        AGENT, {'teach': {CREATOR: {'book': 'Quantum Mechanics', 'chapter': 3}}})
    creator = _tools(_store(), CREATOR)
    creator['save_data_in_memory'](f'teach.{CREATOR}.chapter', 4)
    record = creator['get_data_by_key'](f'teach.{CREATOR}')
    assert 'Quantum Mechanics' in record and '4' in record and '3' not in record


def test_each_persons_receipt_logo_is_their_own_file(store_dir, tmp_path,
                                                     monkeypatch):
    """The logo's path lives in the person's memory, so its file must too:
    one folder per agent let the next person's logo overwrite the file
    under a path still stored as the first person's."""
    data_dir = tmp_path / 'data'
    monkeypatch.setattr('core.platform_paths.get_data_dir',
                        lambda: str(data_dir))
    store = _store()
    for user, content in ((CREATOR, b'LOGO-A'), (VISITOR, b'LOGO-B')):
        src = tmp_path / f'{content.decode()}.png'
        src.write_bytes(content)
        _tools(store, user)['set_receipt_logo'](file_path=str(src))
    for user, content in ((CREATOR, b'LOGO-A'), (VISITOR, b'LOGO-B')):
        path = _tools(store, user)['get_data_by_key']('receipt_logo_path')
        with open(path, 'rb') as f:
            assert f.read() == content


def test_no_person_store_id_enters_the_elided_namespace():
    """The wire trim keeps elided text under `elided_<scope>_<id>` and
    sweeps those files after a day; a person store id must never be one."""
    from core.agent_tools import person_store_id
    from core.llm_outbound_logger import ELIDED_NAMESPACE
    from core.cache_loaders import is_safe_store_id
    for user_prompt in (f'{ELIDED_NAMESPACE}_u1234_{AGENT}',
                        f'{ELIDED_NAMESPACE.upper()}_u1_{AGENT}',
                        f'{CREATOR}_{AGENT}', '../escape_54', 'a@b.c_54'):
        store_id = person_store_id(user_prompt)
        assert not store_id.lower().startswith(ELIDED_NAMESPACE + '_')
        assert is_safe_store_id(store_id) and store_id == store_id.lower()
    assert person_store_id(f'{CREATOR}_{AGENT}') == f'{CREATOR}_{AGENT}'
    # Windows and macOS file names ignore case: two ids that differ only by
    # case must not share a file.
    assert person_store_id('Abc_54') != person_store_id('abc_54')


def test_source_guard_create_reads_memory_through_memory_view():
    """Source guard (DRY across the CREATE sites, beside the behaviour test
    below): every value CREATE builds from the memory store, the recipe's
    saved-key snapshot, the "Metadata created till this action" line and
    the speaker selector's key list, comes from memory_view.  Those sites
    sit inside closures that cannot run without the pipeline, and the
    review of 7121264b1 found one still reading agent_data[prompt_id]."""
    import ast
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    tree = ast.parse(open(os.path.join(root, 'hartos', 'create_recipe.py'),
                          encoding='utf-8').read())
    sites = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in ('metadata', '_ad')):
            reads_store = any(isinstance(n, ast.Name) and n.id == 'agent_data'
                              for n in ast.walk(node.value))
            if reads_store:
                sites.append((node.lineno, ast.unparse(node.value)))
    assert sites, 'no CREATE metadata site found: re-point this guard'
    stray = [s for s in sites if 'memory_view(' not in s[1]]
    assert not stray, f'CREATE reads the memory store around memory_view: {stray}'
    # ...and every view is the turn's own person, not another session key.
    wrong = [ast.unparse(n) for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == 'memory_view'
             and not (len(n.args) == 3 and isinstance(n.args[2], ast.Name)
                      and n.args[2].id == 'user_prompt')]
    assert not wrong, f'memory_view called for another person: {wrong}'


def _tool_ctx_dicts(relpath):
    """The dict literals a pipeline hands build_core_tool_closures (the
    ones carrying save_conversation_db), as {key: value source}."""
    import ast
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    tree = ast.parse(open(os.path.join(root, relpath), encoding='utf-8').read())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {k.value: ast.unparse(v) for k, v in zip(node.keys, node.values)
                    if isinstance(k, ast.Constant)}
            if 'save_conversation_db' in keys:
                found.append(keys)
    return found


def test_source_guard_only_create_tool_contexts_are_building():
    """Source guard (the flag's wiring, beside the behaviour tests above):
    both CREATE tool contexts mark the agent as being built, and no REUSE
    context does, so a finished agent's turn can never save for everyone."""
    create = _tool_ctx_dicts(os.path.join('hartos', 'create_recipe.py'))
    reuse = _tool_ctx_dicts(os.path.join('hartos', 'reuse_recipe.py'))
    assert len(create) == 2 and reuse, 're-point this guard'
    assert all(c.get('building_agent') == 'True' for c in create), create
    assert all(c.get('building_agent') in (None, 'False') for c in reuse), reuse


def test_saved_metadata_lists_own_and_shared_keys_never_anothers(store_dir):
    cache_loaders.save_agent_data(AGENT, {'receipt_template': 'Bill'})
    store = _store()
    creator, visitor = _tools(store, CREATOR), _tools(store, VISITOR)
    creator['save_data_in_memory']('teach.creator_book', 'Quantum Mechanics')
    visitor['save_data_in_memory']('teach.visitor_book', 'Fractions')

    seen = creator['get_saved_metadata']()
    assert 'receipt_template' in seen
    assert 'creator_book' in seen
    assert 'visitor_book' not in seen


def test_an_unsafe_user_id_never_writes_outside_the_store(tmp_path, monkeypatch):
    """The person's key carries the user id, and hartos.helper joins the id
    it is given into a path unchecked (the agent id it used to get comes
    from the node's own config; a user id can come from a request body).
    A user id like '../escape' still lands inside the store, under a
    digest, and reads back."""
    from flask import Flask
    from hartos import helper
    store_dir = tmp_path / 'agent_data'
    store_dir.mkdir()
    monkeypatch.setattr(helper, 'AGENT_DATA_DIR', str(store_dir))
    monkeypatch.setattr(cache_loaders, 'AGENT_DATA_DIR', str(store_dir))
    with Flask('test_agent_memory').app_context():
        tools = _tools(_store(), '../escape', helper_fun=helper)
        tools['save_data_in_memory']('notes', {'x': 1})
        assert not list(tmp_path.glob('*_agent_data.json'))
        assert len(list(store_dir.glob('*_agent_data.json'))) == 1
        # And after a restart, from disk.
        again = _tools(_store(), '../escape', helper_fun=helper)
        assert again['get_data_by_key']('notes.x') == '1'


def test_memory_view_puts_a_persons_entries_over_the_agents():
    from core.agent_tools import memory_view
    store = {
        AGENT: {'teach': {'shared': 1, 'both': 'agent'}, 'logo': 'a.png'},
        f'{CREATOR}_{AGENT}': {'teach': {'own': 2, 'both': 'mine'}},
        f'{VISITOR}_{AGENT}': {'teach': {'theirs': 3}},
    }
    view = memory_view(store, AGENT, f'{CREATOR}_{AGENT}')
    assert view == {'teach': {'shared': 1, 'both': 'mine', 'own': 2},
                    'logo': 'a.png'}
    # The view is a copy: building it changes neither store.
    assert store[AGENT]['teach'] == {'shared': 1, 'both': 'agent'}
    assert memory_view({}, AGENT, f'{CREATOR}_{AGENT}') == {}


def _recipe_builder():
    """request_recipe_for_action, run from its source: importing
    hartos.create_recipe hangs a bare pytest env (the same extract-and-exec
    as test_recipe_prompt_anchor_and_tool_names.py)."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    src = open(os.path.join(root, 'hartos', 'create_recipe.py'),
               encoding='utf-8').read()
    m = re.search(r'\ndef request_recipe_for_action\(.*?\n    return message\n',
                  src, re.DOTALL)
    assert m, 'request_recipe_for_action not found: re-point this test'
    return m.group(0)


def test_create_shows_the_creator_their_own_saved_keys():
    """CREATE tells the model "Metadata created till this action".  With
    saves in the person's store, that line must read the person's view, or
    the creator's own saves vanish from the prompt that writes the recipe."""
    from core.agent_tools import memory_view
    user_prompt = f'{CREATOR}_{AGENT}'
    task = SimpleNamespace(recipe=True, fallback=True, current_action=1,
                           get_action=lambda i: 'Teach the next chapter')
    ns = {
        'user_tasks': {user_prompt: task},
        'agent_data': {AGENT: {'receipt_template': 'Bill'},
                       user_prompt: {'teach': {'creator_book': 'QM'}},
                       f'{VISITOR}_{AGENT}': {'teach': {'visitor_book': 'F'}}},
        'safe_set_state': lambda *a, **k: None,
        'ActionState': SimpleNamespace(RECIPE_REQUESTED='recipe_requested'),
        'strip_json_values': lambda d: d,
        'memory_view': memory_view,
        'RECIPE_CREATE_PROMPT_PREFIX': 'Write a recipe',
        'recipe_correction_directive': lambda n: '',
    }
    exec(_recipe_builder(), ns)
    message = ns['request_recipe_for_action'](1, AGENT, 'Assistant', user_prompt)
    line = message.split('Metadata created till this action:')[1].split('\n')[0]
    assert 'creator_book' in line and 'receipt_template' in line
    assert 'visitor_book' not in line
