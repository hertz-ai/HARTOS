"""The memory tools' key descriptions suggest no key name.

WHAT WENT WRONG (live, source Nunba on the local 4B, 18 Nov 2026 17:16-22:12
IST, cafe agent 1793372451267, a test account): the agent asked for the
person's name again and again.  Its reads tried 'user.info.name' (30 calls)
and 'user.profile.name' (14), and every one came back "Key not found"; the
only key the agent had ever saved was 'last_tip'.  'user.info.name' is the
example both key descriptions gave ("e.g., 'user.info.name'"), and the 4B
copies a description's example literally.  Nothing writes that key.

The owner's ruling (11-03): the agent names its own keys; the framework adds
no naming rule.  So the descriptions name no key at all: the save says the
name is the agent's choice and is read back under the same key, the read
says it is the key the value was saved under and that get_saved_metadata
lists what is saved.

    python -m pytest tests/unit/test_a_memory_key_description_suggests_no_key.py -q --noconftest
"""
import inspect
import json
import os
import re
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

from core.agent_tools import build_core_tool_closures


def _tools():
    ctx = {
        'building_agent': False, 'user_id': '7', 'prompt_id': '54',
        'agent_data': {}, 'helper_fun': None, 'user_prompt': '7_54',
        'request_id_list': {}, 'recent_file_id': None, 'scheduler': None,
        'send_message_to_user1': lambda *a, **k: None,
        'retrieve_json': json.loads, 'strip_json_values': lambda d: d,
        'save_conversation_db': lambda *a, **k: None,
    }
    return {name: fn for name, _desc, fn in build_core_tool_closures(ctx)}


def _key_description(fn):
    annotation = inspect.signature(fn).parameters['key'].annotation
    return ' '.join(str(m) for m in getattr(annotation, '__metadata__', ()))


#: A quoted dotted name, the shape of an example key the 4B copies.
_EXAMPLE_KEY = re.compile(r"""['"`][A-Za-z_]+(\.[A-Za-z_]+)+['"`]""")


def test_neither_key_description_names_an_example_key():
    tools = _tools()
    for name in ('save_data_in_memory', 'get_data_by_key'):
        text = _key_description(tools[name])
        assert text, f'{name} lost its key description'
        assert 'user.info.name' not in text, (name, text)
        assert not _EXAMPLE_KEY.search(text), (name, text)


def test_the_read_points_at_the_saved_key_and_the_listing():
    text = _key_description(_tools()['get_data_by_key'])
    assert 'saved under' in text and 'get_saved_metadata' in text, text


def test_the_save_says_the_key_is_the_agents_own_and_read_back_by_it():
    text = _key_description(_tools()['save_data_in_memory'])
    assert 'you choose' in text and 'same key' in text, text
