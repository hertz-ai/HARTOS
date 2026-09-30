"""#686 — reuse role-group turns must persist to the shared chat history.

Live 2026-08-23 (installed build, user validate-0823b, agent 90916249292):
the 11:43 role-group turn replied "I have saved the codename BLUEFIN6 to my
memory for this conversation", yet simplemem_db/user_validate-0823b/
buffer.json holds only the langchain turns (hi / weather) — the group's
exchange was never written.  At 12:14 the agent truthfully answered
"I don't have access to our past conversations".

Root cause: reuse_recipe seeds the role group FROM the shared buffer
(seed_autogen_from_shared_history, :796) but installs no write-back hook —
that hook exists only on the visual group family (:3059-3120), where
"autogen writes go to the SAME PersistentChatHistory" is implemented
inline.  create_autogen_history_hook's own docstring usage (assigning to
list.append) raises AttributeError on a plain list, which is why the
working install needs a wrapper list.

    python -m pytest tests/unit/test_reuse_history_writeback.py --noconftest -q
"""
import importlib
from pathlib import Path
from types import SimpleNamespace

_REUSE_SRC = (Path(__file__).resolve().parents[2] /
              'hartos/reuse_recipe.py').read_text(encoding='utf-8')


def test_install_history_writeback_persists_appends(tmp_path, monkeypatch):
    import integrations.channels.memory.simplemem_langchain as sml
    monkeypatch.setattr(sml, 'SIMPLEMEM_DB_ROOT', str(tmp_path))
    # a fresh instance per user_id — clear any cross-test cache
    if hasattr(sml.SimpleMemChatMemory, '_instances'):
        sml.SimpleMemChatMemory._instances.clear()

    import integrations.channels.memory.shared_history as sh
    importlib.reload(sh)  # rebind after monkeypatch so lazy roots agree

    gc = SimpleNamespace(messages=[{'role': 'user', 'content': 'seeded',
                                    '_from_shared': True}])
    installed = sh.install_history_writeback(gc, 'wbtest-user')
    assert installed is True

    gc.messages.append({'role': 'user', 'content': 'codename BLUEFIN7',
                        'name': 'User'})
    gc.messages.append({'role': 'assistant', 'content': 'TERMINATE'})
    gc.messages.append({'role': 'user', 'content': 'seed echo',
                        '_from_shared': True})

    # The buffer flush is a 150ms coalesced Timer (_FLUSH_DELAY); the next
    # turn reads via a FRESH load_or_create instance, so assert the same
    # disk round-trip the production read performs.
    import time
    time.sleep(0.8)
    hist = sh._get_persistent_history('wbtest-user')
    contents = [m.content for m in hist.messages]
    assert 'codename BLUEFIN7' in contents, (
        "appended group message did not reach PersistentChatHistory — the "
        "write-back is the missing half of the seed/write contract")
    assert 'TERMINATE' not in contents
    assert 'seed echo' not in contents, "_from_shared seeds must not re-write"
    # the raw list still accumulates everything for autogen itself
    assert len(gc.messages) == 4


# ── #104: the write-back does not store a read of what is already stored ──
# Live on central 2026-09-14: every tool result the create group produced was
# registered as a MemoryGraph row, including reads of the stores themselves
# (save_data_in_memory echoed the whole agent_data store, search_long_term_memory
# joined five rows). Each read came back larger than the last: Guardian
# Convergence's graph reached 28.6M chars and one recall 3,386,616.

_STORE = "{'hive': {'scheduler': {'jobs': [{'name': 'guardian_convergence_monitor'}]}}}"


def _tc(call_id, name):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': '{}'}}


def _group_with_graph(monkeypatch, tools):
    """A group whose one seat registers ``tools``, written back to a mock graph
    only (the shared buffer is left out of these tests)."""
    from unittest import mock
    import integrations.channels.memory.shared_history as sh
    monkeypatch.setattr(sh, '_get_persistent_history', lambda _uid: None)
    gc = SimpleNamespace(messages=[],
                         agents=[SimpleNamespace(function_map=tools)])
    graph = mock.Mock()
    assert sh.install_history_writeback(
        gc, 'u1', extra_sinks=[sh.graph_conversation_sink(graph, 's1')])
    return gc, graph


def _stored(graph):
    return [c.args[1] for c in graph.register_conversation.call_args_list]


def _tools(sh):
    from core.tool_traits import reads_persisted_state

    @reads_persisted_state
    def get_data_by_key(key):
        return _STORE

    def google_search(text):
        return 'Nosana GPU rental prices fell this week'
    return {'get_data_by_key': get_data_by_key,
            'get_data_from_memory': get_data_by_key,
            'google_search': google_search}


def test_a_persisted_read_is_not_stored_again_but_a_new_finding_is(monkeypatch):
    import integrations.channels.memory.shared_history as sh
    gc, graph = _group_with_graph(monkeypatch, _tools(sh))
    gc.messages.append({'role': 'assistant', 'name': 'Helper', 'content': '',
                        'tool_calls': [_tc('c1', 'get_data_by_key'),
                                       _tc('c2', 'google_search')]})
    reply = {'role': 'tool', 'name': 'Assistant',
             'content': _STORE + '\n\n' + 'Nosana GPU rental prices fell this week',
             'tool_responses': [
                 {'tool_call_id': 'c1', 'role': 'tool', 'content': _STORE},
                 {'tool_call_id': 'c2', 'role': 'tool',
                  'content': 'Nosana GPU rental prices fell this week'}]}
    gc.messages.append(reply)
    assert _stored(graph) == ['Nosana GPU rental prices fell this week']
    assert reply['content'].startswith("{'hive'"), "the group's own message was edited"
    assert len(reply['tool_responses']) == 2


def test_a_reply_that_is_only_persisted_reads_is_not_stored(monkeypatch):
    import integrations.channels.memory.shared_history as sh
    gc, graph = _group_with_graph(monkeypatch, _tools(sh))
    gc.messages.append({'role': 'assistant', 'content': '',
                        'tool_calls': [_tc('c3', 'get_data_from_memory')]})
    gc.messages.append({'role': 'tool', 'tool_call_id': 'c3', 'content': _STORE})
    assert _stored(graph) == [], 'the alias must carry the property too'


def test_the_property_survives_autogen_registration():
    """autogen registers a tool through _wrap_function; the flag must still be
    readable from function_map, where the write-back looks it up."""
    from autogen import ConversableAgent
    import integrations.channels.memory.shared_history as sh
    seat = ConversableAgent('Executor', llm_config=False)
    seat.register_for_execution(name='get_data_by_key')(
        _tools(sh)['get_data_by_key'])
    seat.register_for_execution(name='google_search')(_tools(sh)['google_search'])
    gc = SimpleNamespace(messages=[], agents=[seat])
    assert sh._tool_reads_persisted_state(gc, 'get_data_by_key') is True
    assert sh._tool_reads_persisted_state(gc, 'google_search') is False


def test_what_is_stored_is_bounded_and_the_group_keeps_the_whole(monkeypatch):
    from core.constants import MEMORY_ITEM_MAX_CHARS
    import integrations.channels.memory.shared_history as sh
    gc, graph = _group_with_graph(monkeypatch, _tools(sh))
    long_reply = 'z' * (3 * MEMORY_ITEM_MAX_CHARS)
    gc.messages.append({'role': 'user', 'name': 'Assistant', 'content': long_reply})
    assert len(_stored(graph)[0]) <= MEMORY_ITEM_MAX_CHARS
    assert gc.messages[-1]['content'] == long_reply


def test_a_reply_whose_call_was_never_seen_is_kept_but_bounded(monkeypatch):
    """The fail-open path: the write-back was installed after the tool_calls
    message streamed (a seeded history, a re-install), so the reply's
    tool_call_id is unknown and its result is kept. The row bound is what
    stops that from growing the next recall."""
    from unittest import mock
    from core.constants import MEMORY_ITEM_MAX_CHARS
    import integrations.channels.memory.shared_history as sh
    monkeypatch.setattr(sh, '_get_persistent_history', lambda _uid: None)
    gc = SimpleNamespace(
        messages=[{'role': 'assistant', 'content': '',
                   'tool_calls': [_tc('c9', 'get_data_by_key')]}],
        agents=[SimpleNamespace(function_map=_tools(sh))])
    graph = mock.Mock()
    assert sh.install_history_writeback(
        gc, 'u1', extra_sinks=[sh.graph_conversation_sink(graph, 's1')])
    huge = _STORE * (MEMORY_ITEM_MAX_CHARS // len(_STORE) + 2)
    gc.messages.append({'role': 'tool', 'tool_call_id': 'c9', 'content': huge})
    stored = _stored(graph)
    assert len(stored) == 1
    assert len(stored[0]) <= MEMORY_ITEM_MAX_CHARS
    assert stored[0].endswith(' ...[cut]')


def test_the_core_tools_that_read_stored_state_carry_the_mark():
    """Each core tool whose result is a read of state HARTOS already keeps is
    marked where it is registered; a tool that brings in new information is not."""
    from unittest import mock
    from core.agent_tools import build_core_tool_closures
    from core.tool_traits import READS_PERSISTED_STATE, has_trait
    ctx = {k: None for k in (
        'user_id', 'prompt_id', 'agent_data', 'helper_fun', 'user_prompt',
        'request_id_list', 'recent_file_id', 'scheduler',
        'send_message_to_user1', 'retrieve_json', 'strip_json_values',
        'save_conversation_db')}
    ctx.update(user_id=1, prompt_id='p1', agent_data={}, user_prompt='s1',
               request_id_list={'s1': 'r1'}, memory_graph=mock.Mock(),
               simplemem_store=None, helper_fun=mock.Mock(),
               send_message_to_user1=mock.Mock(), retrieve_json=lambda v: v)
    tools = {n: f for n, _, f in build_core_tool_closures(ctx)}
    for name in ('save_data_in_memory', 'get_saved_metadata', 'get_data_by_key',
                 'get_data_from_memory', 'get_chat_history', 'search_visual_history',
                 'search_long_term_memory', 'get_user_details'):
        assert has_trait(tools[name], READS_PERSISTED_STATE), name
    for name in ('google_search', 'send_message_to_user'):
        assert not has_trait(tools[name], READS_PERSISTED_STATE), name


def test_create_and_reuse_write_back_through_one_graph_sink():
    """The two builders carried identical private _graph_sink copies; each now
    calls the shared sink and defines no graph sink of its own. Source
    structure is the subject here, so this reads the parsed source."""
    import ast
    root = Path(__file__).resolve().parents[2]
    for rel in ('hartos/create_recipe.py', 'hartos/reuse_recipe.py'):
        tree = ast.parse((root / rel).read_text(encoding='utf-8'))
        defs = {n.name for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        called = {n.func.id if isinstance(n.func, ast.Name)
                  else getattr(n.func, 'attr', None)
                  for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert '_graph_sink' not in defs, f'{rel} still defines its own graph sink'
        assert 'graph_conversation_sink' in called, f'{rel} does not call the shared sink'


def test_role_group_installs_writeback():
    """The seeded role group in create_agents_for_role must install the
    canonical write-back — seeding without write-back is a one-way valve
    that loses every reuse conversation (live 11:43 turn)."""
    import re
    m = re.search(
        r"seed_autogen_from_shared_history\(user_id.*?"
        r"return assistant, user_proxy, group_chat, manager, helper, False",
        _REUSE_SRC, re.DOTALL)
    assert m, "seeded role-group region not found in reuse_recipe"
    assert 'install_history_writeback(' in m.group(0), (
        "role group seeds FROM the shared buffer but never writes back — "
        "install the canonical shared_history.install_history_writeback")
