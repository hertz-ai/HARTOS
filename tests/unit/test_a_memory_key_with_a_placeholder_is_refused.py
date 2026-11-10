"""A memory key that still holds a template's placeholder is refused, not stored.

Measured live 2026-11-07 14:07-14:12 IST on the owner's desktop (build67 with
hartos-94's swap), request msg_1794040636536_3y7gjm, agent 54 "Let's continue my
lesson." as 10202.  Agent 54's saved step 3 says "Call save_data_in_memory with
the key teach.<that same user id>".  The model called
save_data_in_memory(key='teach.<user_id>', ...): the progress went under the
literal key part '<user_id>', the learner's own record at teach.10202 was never
updated, and the fabrication gate counted the save as real work because the tool
ran and reported success.  The same shape was read once before:
get_data_by_key('teach.<d68c9dee-b324-4c04-86c4-1205a836957f>') on 2026-10-14
23:10 (logs-dev llm_outbound.jsonl.old), the real id wrapped in brackets, which
can never match the stored teach.<id> key either.

The rule is about the tool's own argument, not the model's words: a '<', then
something other than spaces, then the next '>', anywhere in the key, is a
placeholder nobody filled (the review of this change measured 48 logged
placeholder calls in 5 spellings, all of that shape, and no stored key outside
the incident's own record).  The
tools answer with an "Error: ..." reply, which core.constants.tool_reply_failed
reads as a failed call, so the REUSE fabrication gate re-steers the action
instead of crediting it.

Real build_core_tool_closures over core.cache_loaders' on-disk store in a
temporary folder (the harness of test_agent_memory_is_per_person.py).

    python -m pytest tests/unit/test_a_memory_key_with_a_placeholder_is_refused.py -q
"""
import json

import pytest

from core import cache_loaders
from core.agent_tools import build_core_tool_closures
from core.constants import tool_reply_failed
from core.session_cache import TTLCache

AGENT = '54'
LEARNER = '10202'
PROGRESS = {'book': 'The Art of Computer Programming', 'chapter': 'Introduction',
            'pages_taught': ['page_1', 'page_2'], 'stage': 'continue_learning'}


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_loaders, 'AGENT_DATA_DIR', str(tmp_path))
    prompts = tmp_path / 'prompts'
    prompts.mkdir()
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(prompts))
    monkeypatch.setattr(cache_loaders, '_agent_config_cache', {})
    return tmp_path


class _DiskHelper:
    """The two store calls the memory tools make, on core.cache_loaders'
    file format."""

    def save_agent_data_to_file(self, store_id, store):
        return cache_loaders.save_agent_data(store_id, store.get(store_id, {}))

    def load_agent_data_from_file(self, store_id, store):
        data = cache_loaders.load_agent_data(store_id)
        store[store_id] = data if data is not None else {}
        return data is not None


def _tools(store, user_id=LEARNER):
    ctx = {
        'building_agent': False,
        'user_id': user_id, 'prompt_id': AGENT, 'agent_data': store,
        'helper_fun': _DiskHelper(),
        'user_prompt': f'{user_id}_{AGENT}',
        'request_id_list': {}, 'recent_file_id': None, 'scheduler': None,
        'send_message_to_user1': lambda *a, **k: None,
        'retrieve_json': json.loads,
        'strip_json_values': lambda d: d,
        'save_conversation_db': lambda *a, **k: None,
    }
    return {name: fn for name, _desc, fn in build_core_tool_closures(ctx)}


def _store():
    return TTLCache(ttl_seconds=7200, max_size=500, name='test_agent_data',
                    loader=cache_loaders.load_agent_data)


def _on_disk(store_dir):
    path = store_dir / f'{LEARNER}_{AGENT}_agent_data.json'
    return json.loads(path.read_text()) if path.is_file() else {}


def test_the_live_save_with_a_placeholder_key_is_refused_and_stores_nothing(store_dir):
    """The 14:12 call, replayed: refused as a failed call, nothing written."""
    tools = _tools(_store())
    reply = tools['save_data_in_memory']('teach.<user_id>', PROGRESS)
    assert tool_reply_failed(reply), reply
    assert '<user_id>' in reply                     # names what to fill in
    assert '<user_id>' not in json.dumps(_on_disk(store_dir))


def test_the_learners_real_record_is_left_as_it_was(store_dir):
    """The real teach.<id> record stays as it was after a refused save, and
    no record under the placeholder is created beside it (the stray
    teach.<user_id> entry the live save left in 10202's store)."""
    store = _store()
    tools = _tools(store)
    tools['save_data_in_memory'](f'teach.{LEARNER}', {'stage': 'check_question'})
    reply = tools['save_data_in_memory']('teach.<that same user id>', PROGRESS)
    assert tool_reply_failed(reply), reply
    assert 'check_question' in tools['get_data_by_key'](f'teach.{LEARNER}')
    record = _on_disk(store_dir)            # {prompt_id, saved_at, data}
    teach = (record.get('data') or {}).get('teach', {})
    assert list(teach) == [LEARNER], record


@pytest.mark.parametrize('key, placeholder', [
    ('teach.<user_id>.scores', '<user_id>'),
    ('teach.<user.id>', '<user.id>'),            # 10-10 live, twice
    ('<experiment_id>_state', '<experiment_id>'),  # agent 29023956042's step
    ('teach. <user_id>', '<user_id>'),
    ('notes.<>.<user_id>', '<user_id>'),         # an empty pair is skipped
    ('teach.<<user_id>', '<user_id>'),           # the nearer '<' opens it
])
def test_every_shape_the_model_copies_is_refused_and_named(store_dir, key, placeholder):
    """The placeholder is found wherever it sits in the key, and the reply
    names it on its own (not only inside the echoed key), so the model is
    told which value to fill in."""
    tools = _tools(_store())
    reply = tools['save_data_in_memory'](key, PROGRESS)
    assert tool_reply_failed(reply), reply
    assert f'placeholder {placeholder}' in reply, reply
    assert placeholder not in json.dumps(_on_disk(store_dir))


def test_the_filled_key_saves_as_before(store_dir):
    """The fix touches only placeholders: the key the step meant still saves."""
    tools = _tools(_store())
    reply = tools['save_data_in_memory'](f'teach.{LEARNER}', PROGRESS)
    assert not tool_reply_failed(reply), reply
    assert 'continue_learning' in tools['get_data_by_key'](f'teach.{LEARNER}')


@pytest.mark.parametrize('key', ['teach.<d68c9dee-b324-4c04-86c4-1205a836957f>',
                                 '<user_id>', 'teach.<user_id>.scores'])
def test_a_read_with_a_placeholder_key_is_a_failed_call(store_dir, key):
    """The 10-14 read shape and its neighbours: a failed call the gate
    re-steers, never 'Key not found' (which reads as a real, empty answer)."""
    reply = _tools(_store())['get_data_by_key'](key)
    assert tool_reply_failed(reply), reply


@pytest.mark.parametrize('key', ['teach.10202', 'a<b.c', 'notes.x>y', 'scores.<',
                                 'cmp.x<>y', 'cmp.a< >c', 'gt.b>a<c'])
def test_keys_with_no_filled_bracket_pair_are_untouched(store_dir, key):
    """A lone '<' or '>', an empty or blank '<>', and a '>' before the '<'
    are ordinary keys, not placeholders."""
    tools = _tools(_store())
    reply = tools['save_data_in_memory'](key, 1)
    assert not tool_reply_failed(reply), reply
