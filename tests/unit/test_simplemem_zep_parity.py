"""SimpleMemChatMemory ↔ ZepMemory parity regression tests.

helper.py used to import ``ZepMemory`` and call:

    memory.chat_memory.add_message(HumanMessage(...), metadata={...})
    memory.chat_memory.search(prompt, metadata={'start_date': ..., 'end_date': ...})
    memory.chat_memory.search(prompt)
    memory.save_context(inputs, outputs, metadata={...})
    memory.load_memory_variables(inputs)

This file pins down the contract the SimpleMem-backed replacement must
satisfy so a future refactor cannot silently drop a method or change
the return shape (which is what caused the prior compaction-cycle
breakage when the migration was tried piecemeal).

Tests deliberately do NOT depend on SimpleMem being installed — the
buffer-only fallback path is exercised so the suite stays green on
slim CI runners.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

import pytest

from integrations.channels.memory.simplemem_langchain import (
    SimpleMemChatMemory, PersistentChatHistory, ChatMemorySearchResult,
    _approx_token_count, _keyword_overlap_score, _tokenize_lower,
)
from langchain_classic.schema.messages import (
    HumanMessage, AIMessage, SystemMessage,
)


@pytest.fixture
def tmp_buffer(tmp_path):
    """Per-test buffer file so writes don't bleed between tests."""
    return str(tmp_path / 'buffer.json')


@pytest.fixture
def history(tmp_buffer):
    """Fresh PersistentChatHistory with no SimpleMem backend."""
    return PersistentChatHistory(
        buffer_file=tmp_buffer, simplemem_store=None, max_messages=50)


@pytest.fixture
def memory(tmp_buffer):
    """Fresh SimpleMemChatMemory wrapping the persistent history."""
    h = PersistentChatHistory(
        buffer_file=tmp_buffer, simplemem_store=None, max_messages=50)
    return SimpleMemChatMemory(
        chat_memory=h, return_messages=True, input_key='input')


# ─── Add/get parity ───────────────────────────────────────────────────

def test_add_user_and_ai_messages(history):
    history.add_user_message('hi')
    history.add_ai_message('hello')
    msgs = history.messages
    assert len(msgs) == 2
    assert isinstance(msgs[0], HumanMessage) and msgs[0].content == 'hi'
    assert isinstance(msgs[1], AIMessage) and msgs[1].content == 'hello'


def test_add_messages_bulk(history):
    history.add_messages([
        HumanMessage(content='q1'),
        AIMessage(content='a1'),
        HumanMessage(content='q2'),
    ])
    assert [m.content for m in history.messages] == ['q1', 'a1', 'q2']


def test_messages_persist_across_instances(tmp_buffer):
    h1 = PersistentChatHistory(buffer_file=tmp_buffer, simplemem_store=None)
    h1.add_user_message('persisted')
    h1.flush_sync()

    h2 = PersistentChatHistory(buffer_file=tmp_buffer, simplemem_store=None)
    assert any(m.content == 'persisted' for m in h2.messages)


def test_clear_empties_buffer_and_indexes(history):
    history.add_user_message('a')
    history.add_ai_message('b')
    history.clear()
    assert history.messages == []
    # Indexes also cleared so post-clear search returns nothing
    assert history.search('a') == []


# ─── search() ZepMemory parity ────────────────────────────────────────

def test_search_returns_wrapped_objects_with_dict(history):
    """ZepMemory contract: each result has .dict(exclude_unset=True)
    that produces {'message': {...}, 'dist': float, 'score': float}.
    """
    history.add_message(HumanMessage(content='question about pizza'),
                        metadata={'prompt_id': 1, 'request_Id': 'r1'})
    history.add_message(AIMessage(content='answer about pizza'),
                        metadata={'prompt_id': 1, 'request_Id': 'r1'})

    results = history.search('pizza')
    assert results, 'expected at least one match for "pizza"'
    r = results[0]

    # Has the ZepMemory wrapper API
    assert isinstance(r, ChatMemorySearchResult)
    d = r.dict(exclude_unset=True)
    assert 'message' in d
    assert 'dist' in d
    assert 'score' in d

    # message dict has the legacy ZepMemory keys
    msg = d['message']
    assert 'content' in msg and 'role' in msg
    assert 'created_at' in msg and 'metadata' in msg
    assert msg['role'] in ('human', 'ai')
    assert 'pizza' in msg['content']


def test_search_metadata_filter_by_request_id(history):
    history.add_message(HumanMessage(content='one'),
                        metadata={'request_Id': 'r1', 'prompt_id': 1})
    history.add_message(HumanMessage(content='two'),
                        metadata={'request_Id': 'r2', 'prompt_id': 1})

    results = history.search('', metadata={'request_Id': 'r1'})
    assert len(results) == 1
    assert results[0].message['content'] == 'one'


def test_search_date_range_via_start_end_keys(history):
    """ZepMemory accepted ``metadata={'start_date': ..., 'end_date': ...}``
    — our drop-in must honor those exact keys (helper.py:1633-1637).
    """
    history.add_message(HumanMessage(content='old'),
                        metadata={'timestamp': '2025-01-01T10:00:00'})
    history.add_message(HumanMessage(content='mid'),
                        metadata={'timestamp': '2025-06-15T10:00:00'})
    history.add_message(HumanMessage(content='new'),
                        metadata={'timestamp': '2026-12-01T10:00:00'})

    results = history.search('', metadata={
        'start_date': '2025-04-01',
        'end_date': '2025-09-30',
    })
    assert len(results) == 1
    assert results[0].message['content'] == 'mid'


def test_search_dict_serialization_matches_zep_consumer_shape(history):
    """Mirrors the parse pattern in helper.py:1654-1668:

        serialized_result = result.dict(exclude_unset=True)
        message = serialized_result['message']
        filtered_message = {
            'content': message.get('content'),
            'role': message.get('role'),
            'created_at': message.get('created_at'),
            'request_id': message.get('metadata', {}).get('request_id'),
        }
    """
    history.add_message(HumanMessage(content='hello world'),
                        metadata={'request_id': 'req-42'})
    results = history.search('hello')
    assert results

    for r in results:
        d = r.dict(exclude_unset=True)
        assert isinstance(d['message'], dict)
        msg = d['message']
        # Every key the legacy consumer reaches for is present
        assert msg.get('content') == 'hello world'
        assert msg.get('role') in ('human', 'ai')
        assert isinstance(msg.get('metadata', {}), dict)
        assert msg['metadata'].get('request_id') == 'req-42'


def test_search_limit_clamps_results(history):
    for i in range(20):
        history.add_user_message(f'msg about apple {i}')
    results = history.search('apple', limit=5)
    assert len(results) == 5


def test_search_empty_history_returns_empty(history):
    assert history.search('anything') == []
    assert history.search('', metadata={'request_Id': 'r1'}) == []


def test_search_results_sorted_by_score_descending(history):
    history.add_user_message('cat dog mouse')        # 3 overlap
    history.add_user_message('cat')                  # 1 overlap
    history.add_user_message('completely unrelated')  # 0 overlap
    history.add_user_message('cat dog')              # 2 overlap

    results = history.search('cat dog mouse')
    # All results have score in [0,1], scores monotonically non-increasing
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


# ─── ConversationBufferMemory parity ──────────────────────────────────

def test_buffer_returns_messages_when_return_messages_true(memory):
    memory.chat_memory.add_user_message('hi')
    memory.chat_memory.add_ai_message('hello')

    buf = memory.buffer
    assert isinstance(buf, list)
    assert len(buf) == 2
    assert isinstance(buf[0], HumanMessage)


def test_buffer_returns_str_when_return_messages_false(tmp_buffer):
    h = PersistentChatHistory(buffer_file=tmp_buffer, simplemem_store=None)
    m = SimpleMemChatMemory(
        chat_memory=h, return_messages=False, input_key='input')
    h.add_user_message('hi')
    h.add_ai_message('hello')

    buf = m.buffer
    assert isinstance(buf, str)
    assert 'Human: hi' in buf
    assert 'AI: hello' in buf


def test_buffer_as_str_respects_custom_prefixes(tmp_buffer):
    h = PersistentChatHistory(buffer_file=tmp_buffer, simplemem_store=None)
    m = SimpleMemChatMemory(
        chat_memory=h, return_messages=False,
        human_prefix='User', ai_prefix='Bot', input_key='input')
    h.add_user_message('q')
    h.add_ai_message('a')

    assert 'User: q' in m.buffer_as_str
    assert 'Bot: a' in m.buffer_as_str
    assert 'Human:' not in m.buffer_as_str


def test_load_memory_variables_messages_mode(memory):
    memory.chat_memory.add_user_message('hi')
    memory.chat_memory.add_ai_message('hello')
    vars_out = memory.load_memory_variables({'input': 'next'})
    assert memory.memory_key in vars_out
    out = vars_out[memory.memory_key]
    assert isinstance(out, list)
    assert all(hasattr(m, 'content') for m in out)


def test_load_memory_variables_string_mode(tmp_buffer):
    h = PersistentChatHistory(buffer_file=tmp_buffer, simplemem_store=None)
    m = SimpleMemChatMemory(
        chat_memory=h, return_messages=False, input_key='input')
    h.add_user_message('q')
    h.add_ai_message('a')
    vars_out = m.load_memory_variables({'input': 'next'})
    out = vars_out[m.memory_key]
    assert isinstance(out, str)
    assert 'Human: q' in out and 'AI: a' in out


# ─── save_context parity ──────────────────────────────────────────────

def test_save_context_writes_human_ai_pair_with_metadata(memory):
    meta = {'request_Id': 'r-abc', 'prompt_id': 7}
    memory.save_context(
        inputs={'input': 'question'},
        outputs={'output': 'answer'},
        metadata=meta,
    )
    msgs = memory.chat_memory.messages
    assert [type(m).__name__ for m in msgs] == ['HumanMessage', 'AIMessage']
    assert msgs[0].content == 'question'
    assert msgs[1].content == 'answer'

    # metadata round-trips to search
    hits = memory.chat_memory.search('', metadata={'request_Id': 'r-abc'})
    assert len(hits) == 2


def test_save_context_auto_prunes_when_max_token_limit_set(tmp_buffer):
    h = PersistentChatHistory(buffer_file=tmp_buffer, simplemem_store=None)
    m = SimpleMemChatMemory(
        chat_memory=h, return_messages=True, input_key='input',
        max_token_limit=20,  # very tight budget → forces eviction
    )

    # Each turn ≈ 20 tokens of content; budget of 20 → only most recent stays
    for i in range(5):
        m.save_context(
            inputs={'input': 'x' * 50},
            outputs={'output': 'y' * 50},
        )

    # After the saves + auto-prune, total tokens should be near budget
    total = sum(_approx_token_count(msg.content) for msg in h.messages)
    assert total <= 60  # allow slack for the last turn that triggered prune


# ─── prune() parity ───────────────────────────────────────────────────

def test_prune_drops_oldest_until_under_budget(memory):
    for i in range(10):
        memory.chat_memory.add_user_message('x' * 40)  # ~10 tokens each

    removed = memory.prune(max_tokens=30)
    assert removed > 0
    total = sum(
        _approx_token_count(m.content) for m in memory.chat_memory.messages)
    assert total <= 60  # tolerant: prune stops at >1 message remaining


def test_prune_with_zero_budget_is_noop(memory):
    memory.chat_memory.add_user_message('preserved')
    assert memory.prune(0) == 0
    assert len(memory.chat_memory.messages) == 1


# ─── Async parity (BaseChatMessageHistory in langchain_core 1.x) ──────

@pytest.mark.asyncio
async def test_aadd_messages_and_aget_messages(history):
    await history.aadd_messages([
        HumanMessage(content='q1'),
        AIMessage(content='a1'),
    ])
    msgs = await history.aget_messages()
    assert [m.content for m in msgs] == ['q1', 'a1']


# ─── Helpers ──────────────────────────────────────────────────────────

def test_approx_token_count_is_positive_for_nonempty_text():
    assert _approx_token_count('hello world') >= 1


def test_tokenize_lower_normalizes_and_extracts_words():
    assert _tokenize_lower('Hello, World!') == ['hello', 'world']
    assert _tokenize_lower('') == []


def test_keyword_overlap_score_bounds():
    s = _keyword_overlap_score(['cat', 'dog'], 'the cat sat')
    assert 0.0 <= s <= 1.0
    assert _keyword_overlap_score([], 'anything') == 0.0
    assert _keyword_overlap_score(['anything'], '') == 0.0
