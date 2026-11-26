"""A saved text keeps its kind: a flag, or an object that wraps the key with
other values beside it, never replaces it (#282, #295).

WHAT WENT WRONG (speech agent 95568353503, CREATE on the local 4B, 25-26 Nov
2026, agent_system.log): the child said "தமிழ்" and the model saved
home_language true; the parent said "Her name is Zoey. At home she speaks
Tamil." and it saved child_name {"child_name": "Zoey", "home_language":
"Tamil"}.  REUSE then read `True` and an object where a language and a name
had been, and step 4 asked the parent for both again.

THE RULE (core.agent_tools._saved_text_refusal, read by save_data_in_memory
before it writes): when the key holds non-empty text, a value that is true or
false, an object with one entry holding exactly that text (child_name
{"value": "Zoey"}, live 20:35 IST on the repaired store), or an object (or a
list of objects) holding the key's own name, is not written, in memory, on
disk or in the memory graph.  The reply says what is saved and offers the
model's own values as calls, only for keys that hold nothing or a bare flag.
Every other save is untouched: text over text, any value where no text is,
and text replaced by an object that adds something or a list (real upgrades,
measured).

    python -m pytest tests/unit/test_saved_text_keeps_its_kind.py -q --noconftest
"""
import json
import os
import threading

import pytest

import core.agent_tools as agent_tools
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


class _Graph:
    """The memory graph's [KV] mirror, as save_data_in_memory writes it."""

    def __init__(self):
        self.rows = []

    def register(self, content, metadata=None):
        self.rows.append(content)


class _SyncThreading:
    """threading as core.agent_tools sees it, with Thread run at once, so the
    memory graph row a save writes is there when the call returns."""

    class Thread:
        def __init__(self, target=None, daemon=None, **_kw):
            self._target = target

        def start(self):
            self._target()

    def __getattr__(self, name):
        return getattr(threading, name)


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_loaders, 'AGENT_DATA_DIR', str(tmp_path))
    store = TTLCache(ttl_seconds=7200, max_size=500, name='test_saved_text_kind',
                     loader=cache_loaders.load_agent_data)
    graph = _Graph()
    ctx = {
        'building_agent': False, 'user_id': PERSON, 'prompt_id': AGENT,
        'agent_data': store, 'helper_fun': _DiskHelper(),
        'user_prompt': f'{PERSON}_{AGENT}', 'request_id_list': {},
        'recent_file_id': None, 'scheduler': None, 'memory_graph': graph,
        'send_message_to_user1': lambda *a, **k: None,
        'retrieve_json': json.loads, 'strip_json_values': lambda d: d,
        'save_conversation_db': lambda *a, **k: None,
    }
    tools = {name: fn for name, _desc, fn in build_core_tool_closures(ctx)}
    monkeypatch.setattr(agent_tools, 'threading', _SyncThreading())
    file = os.path.join(str(tmp_path), f'{PERSON}_{AGENT}_agent_data.json')
    return tools['save_data_in_memory'], tools['get_data_by_key'], graph, file


def _refused(reply):
    return str(reply).startswith('Error:')


def test_a_flag_never_replaces_saved_text(box):
    save, read, _graph, _file = box
    assert not _refused(save('home_language', 'Tamil'))
    reply = save('home_language', True)
    assert _refused(reply), reply
    assert '"Tamil"' in reply and 'flag' in reply, reply
    assert read('home_language') == 'Tamil'


def test_an_object_holding_its_own_key_never_replaces_saved_text(box):
    save, read, _graph, _file = box
    save('child_name', 'Zoey')
    save('home_language', False)
    reply = save('child_name', {'child_name': 'Zoey', 'home_language': 'Tamil'})
    assert _refused(reply), reply
    # The model's own value, offered for the key that held only a flag.
    assert 'save_data_in_memory(key="home_language", value="Tamil")' in reply, reply
    # Never offered over the text already saved under the key itself.
    assert 'key="child_name"' not in reply, reply
    assert read('child_name') == 'Zoey'
    assert read('home_language') == 'False'


def test_a_template_list_never_replaces_saved_text(box):
    save, read, _graph, _file = box
    save('child_name', 'Zoey')
    reply = save('child_name', [{'child_name': 'child_name',
                                 'home_language': 'home_language'}])
    assert _refused(reply), reply
    assert 'save_data_in_memory(key=' not in reply, reply
    assert read('child_name') == 'Zoey'


def test_the_recipes_blank_form_is_never_offered_back(box):
    """The recipe's blank form, its angle blanks unfilled, saved over the
    name before anyone answered: no blank is offered back as a value, not
    even for a key that holds only a flag, where a real value would be
    (hartos-17's reading of 2d54140ab: the offer skipped only a value equal
    to its own key, so "<home_language>" came back as a call to save)."""
    save, read, _graph, _file = box
    save('child_name', 'Zoey')
    save('home_language', True)
    reply = save('child_name', {'child_name': '<child_name>',
                                'home_language': '<home_language>'})
    assert _refused(reply), reply
    assert 'save_data_in_memory(key=' not in reply, reply
    assert '<home_language>' not in reply and '<child_name>' not in reply, reply
    assert read('child_name') == 'Zoey'
    assert read('home_language') == 'True'


def test_a_filled_value_beside_a_blank_is_still_offered(box):
    save, read, _graph, _file = box
    save('child_name', 'Zoey')
    save('home_language', True)
    reply = save('child_name', {'child_name': '<child_name>', 'home_language': 'Tamil'})
    assert _refused(reply), reply
    assert 'save_data_in_memory(key="home_language", value="Tamil")' in reply, reply
    assert '<child_name>' not in reply, reply
    assert read('child_name') == 'Zoey'


@pytest.mark.parametrize('wrapper', [{'value': 'Zoey'}, {'name': 'Zoey'},
                                     {'Zoey': 'Zoey'}])
def test_an_object_that_only_wraps_the_saved_text_never_replaces_it(box, wrapper):
    """Live 2026-11-26 20:35 IST, step 4 on the repaired store: the parent's
    reply re-saved child_name as {"value": "Zoey"}, and every later read of
    the name returned an object."""
    save, read, _graph, _file = box
    save('child_name', 'Zoey')
    reply = save('child_name', wrapper)
    assert _refused(reply), reply
    assert 'already saved' in reply, reply
    assert read('child_name') == 'Zoey'


@pytest.mark.parametrize('value', [
    True, {'child_name': 'Vikram', 'home_language': 'English'}, {'value': 'Zoey'}])
def test_a_refused_save_writes_no_file_and_no_memory_row(box, value):
    save, _read, graph, file = box
    save('child_name', 'Zoey')
    before = (os.stat(file).st_mtime_ns, open(file, 'rb').read(), list(graph.rows))
    assert graph.rows, 'the first save writes its memory graph row'
    assert _refused(save('child_name', value))
    after = (os.stat(file).st_mtime_ns, open(file, 'rb').read(), list(graph.rows))
    assert after == before


def test_a_wrapped_invented_record_is_never_split_over_real_values(box):
    """last_attempt as the 4B saved it on 26 Nov 00:18: a split would have
    written child_name "Liam" and home_language "English" over the parent's
    answers; the reply names what they hold and offers no call for them."""
    save, read, _graph, _file = box
    save('child_name', 'Zoey')
    save('home_language', 'Tamil')
    save('last_attempt', 'ball')
    reply = save('last_attempt', {'child_name': 'Liam', 'home_language': 'English',
                                  'last_attempt': 'ball'})
    assert _refused(reply), reply
    assert 'key="child_name"' not in reply and 'key="home_language"' not in reply
    assert 'child_name already holds "Zoey"' in reply, reply
    assert (read('child_name'), read('home_language'), read('last_attempt')) == (
        'Zoey', 'Tamil', 'ball')


def test_a_nested_key_follows_the_same_rule(box):
    save, read, _graph, _file = box
    save('child.daily_routine', 'Same as before: ball and blocks, Tamil rhymes.')
    assert _refused(save('child.daily_routine', True))
    reply = save('child.daily_routine', {'daily_routine': 'played', 'voice': 'no'})
    assert _refused(reply), reply
    assert 'save_data_in_memory(key="child.voice", value="no")' in reply, reply
    assert 'Tamil rhymes' in read('child.daily_routine')


def test_text_still_replaces_text(box):
    save, read, _graph, _file = box
    save('home_language', 'English')
    assert not _refused(save('home_language', 'Tamil'))
    assert read('home_language') == 'Tamil'


@pytest.mark.parametrize('first, then', [
    (None, True),                                            # nothing saved yet
    ({'last_attempt': 'ball'}, {'last_attempt': 'ball', 'heard': 'pan-du'}),
    ('cap-puh-CHIN-o', {'text': 'cap-puh-CHIN-o', 'word': 'cappuccino'}),
    ('ball', {'word': 'ball', 'syllables': 1}),
    ('use her family photos', ['use her family photos', 'say it twice']),
    ('ball', ['ball']),
    ('3', 3),
])
def test_every_other_save_is_written_as_before(box, first, then):
    save, read, _graph, _file = box
    if first is not None:
        save('last_attempt', first)
    reply = save('last_attempt', then)
    assert not _refused(reply), reply
    assert read('last_attempt') == str(then)
