"""On a goal turn, get_chat_history tells "read, nothing matched" from "not
read", through the real history reader.

WHAT WENT WRONG (central, 2026-11-20, goal 74416785, rice)
  Action "get_chat_history: pull the last 14 days of chats to find importer
  replies and open requests" got {"res_in_filter": []}.  The StatusVerifier
  answered "pending ... no non-empty tool receipt", and 3 attempts later the
  goal paused needs_help.

  Reviews of 59ae091a and 04ad8cbc (both REJECTED): hartos.helper
  .get_time_based_history answers {"res_in_filter": []} for a store it never
  read too.  SimpleMemChatMemory.load_or_create catches its own store
  failure, and its search answers [] with no store or when the search
  raises, so a store that failed to start, SimpleMem not installed, a search
  that raised, and the dated query raising all read as "nothing matched".
  Told "none found" there, the goal would record "no importer replies yet"
  for an outage.

THE RULE (goal and peer turns: core.agent_tools.recall_runs_on_a_background_turn,
the one rule free_text_recall reads too)
  - the reader, asked strictly, raises when it read nothing, the way
    search_long_term_memory's stores raise: the tool answers
    core.tool_refusal.recall_failed;
  - read and nothing matched: core.tool_refusal.nothing_recalled, the answer
    the recall tools give, naming the dates only when dates were asked;
  - a person's turn and a run with no id keep their exact old strings;
  - a history that holds rows is answered as before.

The four failure shapes are the reviewer's probes
(scratchpad/rv_04ad/probe_history.py), run through the real reader and the
real SimpleMem loader.

    python -m pytest tests/unit/test_a_goal_turn_finds_no_chat_history_and_moves_on.py -q
"""
import importlib
import importlib.util
import json
import sys
import tempfile
import types
from unittest import mock

import pytest

from core.constants import tool_reply_failed, tool_reply_is_step_work
from core.tool_refusal import nothing_recalled, refusal_lets_step_finish

GOAL_TURN = 'daemon_74416785-aaaa'
PERSONS_TURN = 'nunba_req_42'
Q = 'importer replies and open requests'
DATES = ('2026-11-06T00:00:00.000Z', '2026-11-20T00:00:00.000Z')
SML = 'integrations.channels.memory.simplemem_langchain'


def _lc_stand_ins(monkeypatch):
    """Plain stand-ins for the langchain_classic bases SimpleMem's module
    imports, when the test venv lacks langchain_classic (the reviewer's
    shim_lc).  Only the base classes."""
    if importlib.util.find_spec('langchain_classic') is not None:
        return

    class BaseChatMemory:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class BaseMessage:
        def __init__(self, content='', **_kw):
            self.content = content

    mods = {name: types.ModuleType(name) for name in (
        'langchain_classic', 'langchain_classic.memory',
        'langchain_classic.memory.chat_memory', 'langchain_classic.schema',
        'langchain_classic.schema.messages')}
    for name in ('langchain_classic', 'langchain_classic.memory',
                 'langchain_classic.schema'):
        mods[name].__path__ = []
    mods['langchain_classic.memory.chat_memory'].BaseChatMemory = BaseChatMemory
    mods['langchain_classic.schema'].BaseChatMessageHistory = type(
        'BaseChatMessageHistory', (), {})
    msgs = mods['langchain_classic.schema.messages']
    msgs.BaseMessage = BaseMessage
    for role in ('HumanMessage', 'AIMessage', 'SystemMessage'):
        setattr(msgs, role, type(role, (BaseMessage,), {}))
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)


@pytest.fixture()
def sml(monkeypatch):
    """The real SimpleMem chat-memory module, its store root in a temp dir;
    the import is undone after the test."""
    _lc_stand_ins(monkeypatch)
    monkeypatch.delitem(sys.modules, SML, raising=False)
    module = importlib.import_module(SML)
    monkeypatch.setattr(module, 'SIMPLEMEM_DB_ROOT',
                        tempfile.mkdtemp(prefix='hist_sm_'))
    return module


def _tool():
    """get_chat_history over the REAL hartos.helper reader."""
    from core.agent_tools import build_core_tool_closures
    from hartos import helper
    ctx = {k: None for k in (
        'user_id', 'prompt_id', 'agent_data', 'helper_fun', 'user_prompt',
        'request_id_list', 'recent_file_id', 'scheduler',
        'send_message_to_user1', 'retrieve_json', 'strip_json_values',
        'save_conversation_db')}
    ctx.update(user_id='system', prompt_id='daemon_74416785', agent_data={},
               user_prompt='system_daemon_74416785',
               request_id_list={'system_daemon_74416785': GOAL_TURN},
               recent_file_id={}, memory_graph=None, simplemem_store=None,
               helper_fun=helper, send_message_to_user1=mock.Mock(),
               retrieve_json=lambda v: v)
    return {n: f for n, _, f in build_core_tool_closures(ctx)}['get_chat_history']


def _run(turn, dates, langchain=False):
    from core.llm_outbound_logger import request_id_context
    from hartos.threadlocal import thread_local_data
    args = (Q,) + (DATES if dates else ())
    tool = _tool()
    if turn is None:
        return tool(*args)
    if langchain:
        thread_local_data.set_request_id(turn)
        try:
            return tool(*args)
        finally:
            thread_local_data.set_request_id(None)
    with request_id_context(turn):
        return tool(*args)


class _BoomStore:
    def __init__(self, *_a, **_k):
        raise RuntimeError('simplemem init failed (no api key / lancedb)')


class _RaisingSearchStore:
    def __init__(self, *_a, **_k):
        pass

    async def search(self, *_a, **_k):
        raise TimeoutError('simplemem search timed out')


def _db_down():
    raise RuntimeError('database is locked')


class _EmptyQuery:
    def filter(self, *_a, **_k):
        return self

    order_by = limit = filter

    def all(self):
        return []


class _EmptyDB:
    def query(self, *_a, **_k):
        return _EmptyQuery()

    def close(self):
        pass


def _not_read(out):
    assert tool_reply_failed(out), out
    assert 'could not be read' in out, out
    assert out.startswith('Error: the get_chat_history call could not be done:'), out
    assert 'No relevant memories found' not in out, out
    assert refusal_lets_step_finish(out, 'get_chat_history'), out


# ── a goal turn: not read ─────────────────────────────────────────────

def test_1b_store_fails_to_start(sml):
    with mock.patch.object(sml, 'HAS_SIMPLEMEM', True), \
            mock.patch.object(sml, 'SimpleMemStore', _BoomStore, create=True), \
            mock.patch.object(sml, 'SimpleMemConfig', mock.Mock(), create=True):
        _not_read(_run(GOAL_TURN, dates=False))


def test_1c_simplemem_not_installed(sml):
    with mock.patch.object(sml, 'HAS_SIMPLEMEM', False):
        _not_read(_run(GOAL_TURN, dates=False))


def test_1d_its_search_raises(sml):
    with mock.patch.object(sml, 'HAS_SIMPLEMEM', True), \
            mock.patch.object(sml, 'SimpleMemStore', _RaisingSearchStore,
                              create=True), \
            mock.patch.object(sml, 'SimpleMemConfig', mock.Mock(), create=True):
        _not_read(_run(GOAL_TURN, dates=False))


def test_1e_live_14_day_shape_with_the_db_query_raising(sml):
    with mock.patch('integrations.social.models.get_db', _db_down), \
            mock.patch.object(sml, 'HAS_SIMPLEMEM', False):
        _not_read(_run(GOAL_TURN, dates=True))


# ── a goal turn: read, nothing matched ────────────────────────────────

def test_a_readable_empty_window_is_nothing_recalled(sml):
    with mock.patch('integrations.social.models.get_db', lambda: _EmptyDB()):
        out = _run(GOAL_TURN, dates=True)
    assert out == nothing_recalled(f'{Q}, from {DATES[0]} to {DATES[1]}'), out
    assert tool_reply_is_step_work(out, 'get_chat_history'), out


def test_a_langchain_goal_turn_is_a_goal_turn_too(sml):
    with mock.patch('integrations.social.models.get_db', lambda: _EmptyDB()):
        out = _run(GOAL_TURN, dates=True, langchain=True)
    assert out.startswith('No relevant memories found'), out


def test_no_dates_asked_names_no_dates(sml):
    class _EmptyStore:
        def __init__(self, *_a, **_k):
            pass

        async def search(self, *_a, **_k):
            return []
    with mock.patch.object(sml, 'HAS_SIMPLEMEM', True), \
            mock.patch.object(sml, 'SimpleMemStore', _EmptyStore, create=True), \
            mock.patch.object(sml, 'SimpleMemConfig', mock.Mock(), create=True):
        assert _run(GOAL_TURN, dates=False) == nothing_recalled(Q)


# ── a person's turn and a run with no id keep their exact old strings ─

@pytest.mark.parametrize('turn', [PERSONS_TURN, None])
def test_old_strings_kept_off_goal_turns(sml, turn):
    with mock.patch('integrations.social.models.get_db', lambda: _EmptyDB()):
        assert _run(turn, dates=True) == json.dumps({'res_in_filter': []})
    with mock.patch.object(sml, 'HAS_SIMPLEMEM', False):
        assert _run(turn, dates=False) == json.dumps({'res_in_filter': []})
    with mock.patch.dict(sys.modules, {SML: None}):
        assert _run(turn, dates=False) == json.dumps({'res': []})


def test_rows_are_answered_as_before_on_a_goal_turn(sml):
    from types import SimpleNamespace
    from datetime import datetime
    row = SimpleNamespace(content='Please quote 25 t of sona masoori',
                          role='user', created_at=datetime(2026, 11, 12, 10),
                          channel_type='email')

    class _Rows(_EmptyQuery):
        def all(self):
            return [row]

    class _DB(_EmptyDB):
        def query(self, *_a, **_k):
            return _Rows()
    with mock.patch('integrations.social.models.get_db', lambda: _DB()):
        out = _run(GOAL_TURN, dates=True)
    assert json.loads(out)['res_in_filter'][0]['message']['content'] == (
        'Please quote 25 t of sona masoori'), out
