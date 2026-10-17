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


def _tools(store, user_id, prompt_id=AGENT, helper_fun=None):
    ctx = {
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
