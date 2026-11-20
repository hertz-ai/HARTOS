"""A memory that holds a value at the old example key is still steered to it.

WHAT 28a9001b0 FIXED (live, cafe agent, 18 Nov 2026): both memory key
descriptions gave the example 'user.info.name', the 4B copied it, read it
where nothing was saved, and asked for the person's name again and again.  The
descriptions now name no key (the owner's 11-03 ruling: the agent names its
own keys).

WHAT IT COST (rv_28a9 S2): the model had also SAVED under that example.  14
stores in 13 agents on the owner's desktop (7 people's own stores, 7 agents'
own stores) hold a value at 'user.info.name', and with the example gone their
next sessions were no longer steered to their own record.

THE RULE
  * a person whose own memory holds a value at 'user.info.name' is shown the
    example again, in the save, the read and the read's alias;
  * so is the agent's creator when the agent's own store holds it (where the
    creator's saves went before the 2026-10-17 ruling);
  * nobody else: a person whose memory lacks it is shown no key name (the
    28a9001b0 fix), and a non-creator is never steered to the agent's own
    store, which can hold another person's record (key_not_found_reply's
    rule, review of 2ddf102da);
  * building the tools changes no store.

    python -m pytest tests/unit/test_a_memory_that_holds_the_old_example_key_is_still_steered_to_it.py -q --noconftest
"""
import copy
import inspect
import json
import os
import re
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

import core.agent_tools as agent_tools
from core.agent_tools import build_core_tool_closures, person_store_id

USER, AGENT = '7', '54'
SESSION = f'{USER}_{AGENT}'
OWN = person_store_id(SESSION)
_EXAMPLE_KEY = re.compile(r"""['"`][A-Za-z_]+(\.[A-Za-z_]+)+['"`]""")


def _tools(agent_data):
    ctx = {
        'building_agent': False, 'user_id': USER, 'prompt_id': AGENT,
        'agent_data': agent_data, 'helper_fun': None, 'user_prompt': SESSION,
        'request_id_list': {}, 'recent_file_id': None, 'scheduler': None,
        'send_message_to_user1': lambda *a, **k: None,
        'retrieve_json': json.loads, 'strip_json_values': lambda d: d,
        'save_conversation_db': lambda *a, **k: None,
    }
    return {name: fn for name, _desc, fn in build_core_tool_closures(ctx)}


def _key_description(fn):
    annotation = inspect.signature(fn).parameters['key'].annotation
    return ' '.join(str(m) for m in getattr(annotation, '__metadata__', ()))


def _descriptions(agent_data):
    tools = _tools(agent_data)
    return {name: _key_description(tools[name])
            for name in ('save_data_in_memory', 'get_data_by_key',
                         'get_data_from_memory')}


def _creator_is(monkeypatch, who):
    monkeypatch.setattr(agent_tools, 'is_agent_creator',
                        lambda user_id, prompt_id: str(user_id) == who)


def test_a_person_who_saved_at_the_example_key_is_steered_to_it(monkeypatch):
    _creator_is(monkeypatch, 'nobody')
    texts = _descriptions({OWN: {'user': {'info': {'name': 'Asha'}}}})
    for name, text in texts.items():
        assert "'user.info.name'" in text, (name, text)


def test_a_person_who_saved_other_keys_is_shown_no_key_name(monkeypatch):
    _creator_is(monkeypatch, 'nobody')
    texts = _descriptions({OWN: {'last_tip': 'x', 'user': {'id': 3}}})
    for name, text in texts.items():
        assert 'user.info.name' not in text, (name, text)
        assert not _EXAMPLE_KEY.search(text), (name, text)


def test_a_new_person_is_shown_no_key_name(monkeypatch):
    _creator_is(monkeypatch, 'nobody')
    for name, text in _descriptions({}).items():
        assert not _EXAMPLE_KEY.search(text), (name, text)


def test_another_persons_record_in_the_agents_store_steers_nobody_else(monkeypatch):
    _creator_is(monkeypatch, '1')
    texts = _descriptions({AGENT: {'user': {'info': {'name': 'the creator'}}},
                           OWN: {'last_tip': 'x'}})
    for name, text in texts.items():
        assert 'user.info.name' not in text, (name, text)


def test_the_creator_whose_old_save_is_in_the_agents_store_is_steered(monkeypatch):
    _creator_is(monkeypatch, USER)
    texts = _descriptions({AGENT: {'user': {'info': {'name': 'Asha'}}}})
    for name, text in texts.items():
        assert "'user.info.name'" in text, (name, text)


def test_a_saved_none_is_no_value_to_be_steered_to(monkeypatch):
    _creator_is(monkeypatch, 'nobody')
    texts = _descriptions({OWN: {'user': {'info': {'name': None}}}})
    for name, text in texts.items():
        assert 'user.info.name' not in text, (name, text)


def test_building_the_tools_changes_no_store(monkeypatch):
    _creator_is(monkeypatch, USER)
    data = {AGENT: {'user': {'info': {'name': 'Asha'}}},
            OWN: {'user': {'info': {'name': 'Asha'}}}}
    before = copy.deepcopy(data)
    _tools(data)
    assert data == before
