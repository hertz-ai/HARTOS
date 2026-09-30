"""A stored memory is bounded, whoever writes it (#104).

Live on central 2026-09-14: nothing bounded a MemoryGraph row, and the tool
results the group chat wrote back were recalled and written back again.
Guardian Convergence's graph reached 1,358 rows and 28.6M chars, 76 of them
over 100k, the largest 3,960,333. MemoryGraph.register is the one write entry
every writer reaches (conversation, lifecycle, the KV mirror, the long-term
memory tools, the chat reply indexer), so the bound lives there.

    python -m pytest tests/unit/test_memory_graph_row_is_bounded.py --noconftest -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.constants import MEMORY_ITEM_MAX_CHARS  # noqa: E402
from integrations.channels.memory.memory_graph import MemoryGraph  # noqa: E402


@pytest.fixture
def graph(tmp_path):
    # tmp_path, not shutil.rmtree: the venv's shutil comes from a newer stdlib
    # and its rmtree fails on this os module (#35).
    g = MemoryGraph(db_path=str(tmp_path), user_id='u1')
    yield g
    g._store.close()


def _stored(graph, memory_id):
    return graph._store.get(memory_id).content


def test_a_conversation_row_is_bounded(graph):
    mid = graph.register_conversation(
        'Assistant', "{'hive': " + 'x' * (3 * MEMORY_ITEM_MAX_CHARS), 's1')
    content = _stored(graph, mid)
    assert len(content) <= MEMORY_ITEM_MAX_CHARS
    assert content.startswith("{'hive': ")


def test_a_row_from_any_writer_is_bounded(graph):
    mid = graph.register('z' * (MEMORY_ITEM_MAX_CHARS + 1), {'memory_type': 'fact'})
    assert len(_stored(graph, mid)) <= MEMORY_ITEM_MAX_CHARS


def test_an_ordinary_memory_is_stored_whole(graph):
    text = 'the user prefers teal'
    mid = graph.register_conversation('user', text, 's1')
    assert _stored(graph, mid) == text
