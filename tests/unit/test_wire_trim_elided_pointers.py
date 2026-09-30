"""Whatever the wire trim elides is saved, and the wire carries a pointer to it.

Owner direction (relayed 2026-09-27): "tool results shd be saved with
pointers and whatever is trimmed needs pointers to memory", and "the pointer
design shd be explicitly understood by the LLM ... and a pointer shd not
influence the context".  The rules pinned here, on the real
``_trim_to_budget`` and the real ``get_data_by_key`` tool:

  * a message the trim shortens carries ``[elided:<id> <n> chars of <kind>]``
    where its middle was; a dropped tool result is listed the same way;
  * the original is saved whole in the agent-data store (namespace
    ``elided``, the store behind get_data_by_key), and
    ``get_data_by_key(key="elided:<id>")`` returns it exactly, a page at a
    time -- also after the process forgets everything (a REUSE replay);
  * the system message the model reads says what a pointer is and how to
    expand it, only when the body carries one;
  * a pointer is inert: the caller's messages -- the history the banker,
    the completion gate and the verifier judge -- are never changed, and a
    pointer fits inside the trim's 64-token floor.
"""
import json
import re

import pytest

import core.llm_outbound_logger as lol
from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS
from core.token_utils import count_tokens_for_text

_MAX_TOKENS = 64
_POINTER = re.compile(r'\[elided:([0-9a-f]{12}) (\d+) chars of ([a-z ]+)\]')


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The agent-data store in a temp dir, as cache_loaders resolves it."""
    import core.cache_loaders as cl
    monkeypatch.setattr(cl, 'AGENT_DATA_DIR', str(tmp_path))
    return tmp_path


def _trim(msgs, budget, monkeypatch):
    per_slot = budget + _MAX_TOKENS + WIRE_TRIM_SAFETY_MARGIN_TOKENS
    monkeypatch.setattr(lol, '_get_budget_per_slot', lambda: per_slot)
    # The call acts for user u1, as with_llm_context binds it for a real
    # CREATE / REUSE turn: the elided text is stored in u1's scope.
    token = lol._user_id_var.set('u1')
    try:
        out, _, _, _, est_after, got = lol._trim_to_budget(
            {'model': 'llama', 'messages': msgs, 'max_tokens': _MAX_TOKENS})
    finally:
        lol._user_id_var.reset(token)
    return out['messages'], est_after, got


def _shape():
    sys_m = {'role': 'system', 'content': 'You are the reuse assistant.'}
    task = {'role': 'user', 'name': 'User', 'content': 'Summarise the page.'}
    call = {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'c1', 'type': 'function',
                            'function': {'name': 'crawl', 'arguments': '{}'}}]}
    page = 'PAGEHEAD ' + ' '.join('row%d' % i for i in range(3000)) + ' PAGETAIL'
    result = {'role': 'tool', 'tool_call_id': 'c1', 'content': page}
    return sys_m, task, call, result, page


def _get_data_by_key(prompt_id='p1'):
    """The real tool closure, built the way the pipelines build it."""
    from unittest import mock
    from core.agent_tools import build_core_tool_closures
    ctx = {'user_id': 'u1', 'prompt_id': prompt_id, 'agent_data': {prompt_id: {}},
           'helper_fun': mock.MagicMock(), 'user_prompt': 'u1_p1',
           'request_id_list': {'u1_p1': 'r1'}, 'recent_file_id': {},
           'scheduler': mock.MagicMock(), 'send_message_to_user1': mock.MagicMock(),
           'retrieve_json': json.loads, 'strip_json_values': lambda x: x,
           'save_conversation_db': mock.MagicMock()}
    tools = {name: fn for name, _, fn in build_core_tool_closures(ctx)}
    return tools['get_data_by_key']


def _read_all(tool, key):
    """Follow the page notes to the end, the way a model would."""
    text, offset = '', 0
    for _ in range(1000):
        page = tool(key=key, offset=offset)
        m = re.search(r'\n\.\.\.\[chars (\d+)-(\d+) of (\d+); call get_data_by_key '
                      r'with offset=(\d+) for the rest\]$', page)
        if not m:
            return text + page
        text += page[:m.start()]
        offset = int(m.group(4))
    raise AssertionError('pages never ended')


def test_a_cut_tool_result_carries_a_pointer_to_its_original(store, monkeypatch):
    sys_m, task, call, result, page = _shape()
    out, est_after, budget = _trim([sys_m, task, call, result], 900, monkeypatch)
    kept = next(m for m in out if m.get('role') == 'tool')
    m = _POINTER.search(kept['content'])
    assert m, kept['content'][:300]
    assert m.group(3) == 'a tool result'
    assert int(m.group(2)) == len(page)
    assert est_after <= budget
    # The pointer fits inside the 64-token floor with room to spare.
    assert count_tokens_for_text(m.group(0), 'llama') <= 32


def test_the_real_tool_returns_the_exact_original_paged(store, monkeypatch):
    sys_m, task, call, result, page = _shape()
    out, _, _ = _trim([sys_m, task, call, result], 900, monkeypatch)
    pid = _POINTER.search(next(m for m in out if m.get('role') == 'tool')
                          ['content']).group(1)
    tool = _get_data_by_key()
    first = tool(key='elided:' + pid)
    assert first.startswith('PAGEHEAD') and 'offset=' in first, first[-200:]
    assert _read_all(tool, 'elided:' + pid) == page


def test_a_pointer_survives_the_process_forgetting(store, monkeypatch):
    """REUSE replays in a later turn, often a later process: the original is
    read back from the store on disk, not from anything held in memory."""
    sys_m, task, call, result, page = _shape()
    out, _, _ = _trim([sys_m, task, call, result], 900, monkeypatch)
    pid = _POINTER.search(next(m for m in out if m.get('role') == 'tool')
                          ['content']).group(1)
    assert list(store.glob('elided_u*_%s_agent_data.json' % pid))
    # Read back in a fresh interpreter: nothing held in this process helps.
    # (Not importlib.reload: that would undo a mutated function for every
    # later test in the session.)
    import os
    import subprocess
    import sys
    code = ('import core.cache_loaders as cl, core.llm_outbound_logger as l;'
            'cl.AGENT_DATA_DIR = %r;'
            'import sys; sys.stdout.write(l.read_elided(%r, l.elision_scope('
            'user_id="u1")) or "")' % (str(store), pid))
    out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                         timeout=300, cwd=os.getcwd(),
                         env=dict(os.environ, PYTHONIOENCODING='utf-8'))
    assert out.stdout.decode('utf-8') == page, out.stderr.decode()[-500:]


def test_the_system_message_explains_a_pointer_only_when_one_is_sent(
        store, monkeypatch):
    sys_m, task, call, result, page = _shape()
    out, _, _ = _trim([sys_m, task, call, result], 900, monkeypatch)
    system = out[0]['content']
    assert system.startswith(sys_m['content'])
    assert 'get_data_by_key' in system and 'elided:' in system
    assert 'not the content' in system
    small = [dict(sys_m), {'role': 'user', 'content': 'hi'}]
    out2, _, _ = _trim(small, 900, monkeypatch)
    assert out2[0]['content'] == sys_m['content']


def test_a_dropped_tool_pair_is_listed_with_a_pointer(store, monkeypatch):
    sys_m, task, call, result, page = _shape()
    old_call = {'role': 'assistant', 'content': 'first try',
                'tool_calls': [{'id': 'c0', 'type': 'function',
                                'function': {'name': 'crawl', 'arguments': '{}'}}]}
    old_result = {'role': 'tool', 'tool_call_id': 'c0',
                  'content': 'OLD ' + 'old ' * 2000}
    newer = {'role': 'tool', 'tool_call_id': 'c1', 'content': 'the new page'}
    msgs = [sys_m, old_call, old_result, task, call, newer]
    out, est_after, budget = _trim(msgs, 900, monkeypatch)
    assert old_result not in out
    pointers = _POINTER.findall(out[0]['content'])
    assert any(kind == 'a tool result' and int(n) == len(old_result['content'])
               for _, n, kind in pointers), out[0]['content'][-400:]
    pid = next(p for p, n, k in pointers if int(n) == len(old_result['content']))
    assert lol.read_elided(pid, lol.elision_scope(user_id='u1')) == old_result['content']
    assert est_after <= budget


def test_the_callers_history_is_never_changed(store, monkeypatch):
    """Inert: the banker, the gate and the verifier's evidence all read the
    conversation the caller holds, which keeps the full result."""
    import copy
    sys_m, task, call, result, page = _shape()
    msgs = [sys_m, task, call, result]
    before = copy.deepcopy(msgs)
    _trim(msgs, 900, monkeypatch)
    assert msgs == before
    assert not any(_POINTER.search(str(m.get('content'))) for m in msgs)


def test_an_unstorable_original_still_cuts_with_the_plain_marker(
        store, monkeypatch):
    """A store that cannot be written must never fail the LLM call: the cut
    falls back to the marker with no pointer."""
    from core.constants import WIRE_TRIM_MARKER
    monkeypatch.setattr(lol, '_save_elided', lambda records: False)
    sys_m, task, call, result, page = _shape()
    out, est_after, budget = _trim([sys_m, task, call, result], 900, monkeypatch)
    kept = next(m for m in out if m.get('role') == 'tool')
    assert WIRE_TRIM_MARKER in kept['content']
    assert not _POINTER.search(kept['content'])
    assert 'get_data_by_key' not in out[0]['content']
    assert est_after <= budget


def test_the_user_seed_warns_and_is_counted(caplog):
    before = lol.user_seed_count()
    msgs = [{'role': 'system', 'content': 's'},
            {'role': 'assistant', 'content': 'a'}]
    with caplog.at_level('WARNING', logger=lol.logger.name):
        assert lol.ensure_user_turn(msgs)
    assert lol.user_seed_count() == before + 1
    assert any('seed' in r.getMessage().lower() for r in caplog.records
               if r.levelname == 'WARNING')


def test_a_rerun_for_the_explanation_starts_from_the_whole_body(
        store, monkeypatch):
    """When the explanation pushes a trimmed body over, the trim runs again
    with that much reserved -- from the body it was given, not from the list
    the first run already cut down.  The shape that exposed it: a body with
    no user turn, which the trim seeds, so the body's list IS the one the
    first run drops from.  Measured: at budget 900 this shape re-runs
    (reserve 195 tokens); with the shared list the re-run counted 0 drops."""
    msgs = [{'role': 'system', 'content': 'You are the reuse assistant.'}]
    for i in range(8):
        msgs.append({'role': 'assistant', 'content': None,
                     'tool_calls': [{'id': 'c%d' % i, 'type': 'function',
                                     'function': {'name': 'crawl',
                                                  'arguments': '{}'}}]})
        msgs.append({'role': 'tool', 'tool_call_id': 'c%d' % i,
                     'content': ('P%d ' % i) + 'row ' * 300})
    msgs.append({'role': 'assistant', 'content': 'done'})
    per_slot = 900 + _MAX_TOKENS + WIRE_TRIM_SAFETY_MARGIN_TOKENS
    monkeypatch.setattr(lol, '_get_budget_per_slot', lambda: per_slot)
    out, n_dropped, _, _, est_after, budget = lol._trim_to_budget(
        {'model': 'llama', 'messages': msgs, 'max_tokens': _MAX_TOKENS})
    kept = out['messages']
    assert est_after <= budget
    # +1: the seeded user turn.
    assert n_dropped == len(msgs) + 1 - len(kept), (n_dropped, len(msgs), len(kept))
    assert _POINTER.findall(kept[0]['content'])


def test_a_pointer_resolves_only_for_the_user_it_was_elided_for(
        store, monkeypatch):
    """Review of f97b6bed8: one shared store let any caller holding an id
    read another user's elided text through get_data_by_key."""
    sys_m, task, call, result, page = _shape()
    out, _, _ = _trim([sys_m, task, call, result], 900, monkeypatch)
    pid = _POINTER.search(next(m for m in out if m.get('role') == 'tool')
                          ['content']).group(1)
    assert lol.read_elided(pid, lol.elision_scope(user_id='u1')) == page
    assert lol.read_elided(pid, lol.elision_scope(user_id='u2')) is None
    other = _get_data_by_key()  # built for user u1
    assert other(key='elided:' + pid).startswith('PAGEHEAD')
    from unittest import mock
    from core.agent_tools import build_core_tool_closures
    ctx = {'user_id': 'u2', 'prompt_id': 'p2', 'agent_data': {'p2': {}},
           'helper_fun': mock.MagicMock(), 'user_prompt': 'u2_p2',
           'request_id_list': {'u2_p2': 'r2'}, 'recent_file_id': {},
           'scheduler': mock.MagicMock(), 'send_message_to_user1': mock.MagicMock(),
           'retrieve_json': json.loads, 'strip_json_values': lambda x: x,
           'save_conversation_db': mock.MagicMock()}
    u2_tool = {n: fn for n, _, fn in build_core_tool_closures(ctx)}['get_data_by_key']
    assert u2_tool(key='elided:' + pid).startswith('Nothing is stored')


def test_each_elided_item_is_its_own_file_and_old_ones_are_evicted(
        store, monkeypatch):
    """No shared file rewritten per elision; the store is bounded."""
    import os
    import time as _time
    sys_m, task, call, result, page = _shape()
    _trim([sys_m, task, call, result], 900, monkeypatch)
    items = list(store.glob('elided_*_agent_data.json'))
    assert items
    stale = items[0]
    old = _time.time() - lol._ELIDED_TTL_S - 60
    os.utime(stale, (old, old))
    lol._evict_elided()
    assert not stale.exists()


def test_a_budget_too_small_for_the_explanation_sends_plain_markers(
        store, monkeypatch):
    """Below _ELIDED_MIN_BUDGET_MULTIPLE times the explanation's cost, the
    explanation would crowd out the text it points at: no pointer, no
    explanation, the plain marker."""
    from core.constants import WIRE_TRIM_MARKER
    sys_m, task, call, result, page = _shape()
    explain = count_tokens_for_text(lol.ELIDED_POINTER_EXPLANATION, 'llama')
    small = explain * lol._ELIDED_MIN_BUDGET_MULTIPLE - 20
    out, est_after, budget = _trim([sys_m, task, call, result], small,
                                   monkeypatch)
    kept = next(m for m in out if m.get('role') == 'tool')
    assert WIRE_TRIM_MARKER in kept['content']
    assert not _POINTER.search(kept['content'])
    assert 'get_data_by_key' not in out[0]['content']
    big = explain * lol._ELIDED_MIN_BUDGET_MULTIPLE + 200
    out2, _, _ = _trim([sys_m, task, call, result], big, monkeypatch)
    assert _POINTER.search(next(m for m in out2 if m.get('role') == 'tool')
                           ['content'])


def test_the_scope_is_the_user_the_entry_point_acts_for():
    """with_llm_context binds the decorated entry point's user_id (recipe /
    chat_agent), and the elided store scopes by it -- the same user id the
    get_data_by_key closure is built with -- also on a worker thread the
    call hands off to with the context copied, as autogen does."""
    import contextvars
    import threading

    @lol.with_llm_context('autogen.test')
    def entry(user_id, text, prompt_id, file_id, request_id):
        seen = {}
        ctx = contextvars.copy_context()
        t = threading.Thread(
            target=lambda: seen.update(scope=ctx.run(lol.elision_scope)))
        t.start()
        t.join()
        return lol.elision_scope(), seen['scope']

    here, worker = entry('u42', 'hi', 'p1', None, 'r9')
    assert here == worker == lol.elision_scope(user_id='u42')
    assert here != lol.elision_scope(request_id='r9')


def test_the_store_is_bounded_by_count_and_by_bytes(store, monkeypatch):
    """Review of e1a1aa233: the item cap was untested and there was no byte
    bound.  Past either, the oldest items go."""
    import os
    import time as _time
    from core.cache_loaders import save_agent_data
    now = _time.time()
    for n in range(6):
        save_agent_data('elided_anon_%012x' % n,
                        {'text': 'x' * 1000, 'at': now})
        path = store / ('elided_anon_%012x_agent_data.json' % n)
        os.utime(path, (now - 100 + n, now - 100 + n))
    monkeypatch.setattr(lol, '_ELIDED_MAX_ITEMS', 4)
    lol._evict_elided()
    left = sorted(p.name for p in store.glob('elided_*_agent_data.json'))
    assert len(left) == 4 and 'elided_anon_%012x_agent_data.json' % 0 not in left
    size = os.path.getsize(store / left[-1])
    monkeypatch.setattr(lol, '_ELIDED_MAX_ITEMS', 100)
    monkeypatch.setattr(lol, '_ELIDED_MAX_BYTES', size * 2)
    lol._evict_elided()
    left2 = sorted(p.name for p in store.glob('elided_*_agent_data.json'))
    assert left2 == left[-2:], left2
