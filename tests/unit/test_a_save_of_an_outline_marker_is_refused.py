"""A save whose value is an outline's mark is refused; the real value stays.

WHAT WENT WRONG (rv_88d7's review of HARTOS 88d7e3aac, 18 Nov 2026): after
88d7e3aac a memory outline shows every saved text as '<saved_str>'.  The
model reads that outline (get_saved_metadata, the seat prompts) and a 4B can
copy the mark back into a save: save_data_in_memory('email_brief.body',
'<saved_str>') answered "Saved", and the real body was gone.  Saves refused
placeholder KEYS only, never values; before 88d7e3aac the same hole took a
copied None.

THE RULE: a value that carries the outline's mark (core.constants.
OUTLINE_MARKER_RE, the one definition strip_json_values writes through) is
refused before anything is written, and the refusal says to read the saved
value with get_data_by_key first.  Only that mark: a template the agent
saves on purpose may hold other fill-in marks ("<parent_name>").

    python -m pytest tests/unit/test_a_save_of_an_outline_marker_is_refused.py -q --noconftest
"""
import json

import pytest

from core import cache_loaders
from core.agent_tools import build_core_tool_closures
from core.session_cache import TTLCache

AGENT = '54'
PERSON = '10202'


class _DiskHelper:
    def save_agent_data_to_file(self, store_id, store):
        return cache_loaders.save_agent_data(store_id, store.get(store_id, {}))

    def load_agent_data_from_file(self, store_id, store):
        data = cache_loaders.load_agent_data(store_id)
        store[store_id] = data if data is not None else {}
        return data is not None


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_loaders, 'AGENT_DATA_DIR', str(tmp_path))
    store = TTLCache(ttl_seconds=7200, max_size=500, name='test_outline_save',
                     loader=cache_loaders.load_agent_data)
    ctx = {
        'building_agent': False, 'user_id': PERSON, 'prompt_id': AGENT,
        'agent_data': store, 'helper_fun': _DiskHelper(),
        'user_prompt': f'{PERSON}_{AGENT}', 'request_id_list': {},
        'recent_file_id': None, 'scheduler': None,
        'send_message_to_user1': lambda *a, **k: None,
        'retrieve_json': json.loads, 'strip_json_values': lambda d: d,
        'save_conversation_db': lambda *a, **k: None,
    }
    return {name: fn for name, _desc, fn in build_core_tool_closures(ctx)}


def _refused(reply):
    return (str(reply).startswith('Error:') and 'the outline' in str(reply)
            and 'get_data_by_key' in str(reply))


def test_a_copied_marker_does_not_overwrite_the_real_value(tools):
    save, read = tools['save_data_in_memory'], tools['get_data_by_key']
    save('email_brief.body', 'Hi, may I take Friday off?')
    reply = save('email_brief.body', '<saved_str>')
    assert _refused(reply), reply
    assert 'Friday off' in read('email_brief.body'), read('email_brief.body')


@pytest.mark.parametrize('value', [
    {'subject': 'Friday', 'body': '<saved_str>'},
    '{"subject": "Friday", "body": "<saved_str>"}',
    ['<saved_int>'],
])
def test_a_marker_anywhere_in_the_value_is_refused(tools, value):
    assert _refused(tools['save_data_in_memory']('email_brief', value))


def test_a_template_with_its_own_fill_in_mark_is_saved(tools):
    reply = tools['save_data_in_memory']('greeting', 'Hello <parent_name>!')
    assert not str(reply).startswith('Error:'), reply
    assert '<parent_name>' in tools['get_data_by_key']('greeting')


def test_the_outline_writes_the_mark_the_refusal_reads():
    from core.constants import OUTLINE_MARKER_RE
    from hartos.helper import strip_json_values
    out = strip_json_values({'a': 'text', 'b': 3, 'c': [True]})
    for leaf in (out['a'], out['b'], out['c'][0]):
        assert OUTLINE_MARKER_RE.fullmatch(leaf), leaf
