"""An agent-bound group chat is seeded with THAT agent's conversation only.

Live 2026-09-13, walks of 12165936867 and 20260824301: REUSE was entered
("3 Recipe Json exist Going to reuse"), then
"Seeded autogen with 8 messages from shared history (user c23d388c...)",
one LLM call, one reply, TERMINATE, no action run.  The reply was another
agent's conversation -- "I'll help you activate as the HIVE GROWTH AGENT",
the text a daemon hive goal had written into the same user's buffer at
19:47:11.  The grade-5 fractions coach answered "Hello! How can I assist you
today?" the same way.

The buffer is per-USER by design (SimpleMemChatMemory.load_or_create: "memory
is per-user like Zep was") and PersistentChatHistory already indexes a
prompt_id metadata key, which the LangChain leg stamps (178 of 178 of its
entries on this box).  The autogen writer never stamped it (0 of 30) and the
seed never filtered on it, so every agent of a user was seeded with the last 8
messages of ALL of that user's agents and daemon goals.

    python -m pytest tests/unit/test_shared_history_agent_scope.py -q
"""
import importlib
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def sh(tmp_path, monkeypatch):
    import integrations.channels.memory.simplemem_langchain as sml
    monkeypatch.setattr(sml, 'SIMPLEMEM_DB_ROOT', str(tmp_path))
    if hasattr(sml.SimpleMemChatMemory, '_instances'):
        sml.SimpleMemChatMemory._instances.clear()
    import integrations.channels.memory.shared_history as mod
    importlib.reload(mod)
    return mod


def _buffer_with_two_agents(sh, user):
    """Agent 111 (LangChain + autogen turns) and a daemon goal on agent 222."""
    from langchain_core.messages import AIMessage, HumanMessage
    hist = sh._get_persistent_history(user)
    # LangChain leg: stamps prompt_id as an int, exactly as it does live.
    hist.add_message(HumanMessage(content='coach me on fractions'),
                     metadata={'prompt_id': 111, 'request_Id': 'r1'})
    hist.add_message(AIMessage(content='Sure. What is 1/2 + 1/4?'),
                     metadata={'prompt_id': 111, 'request_Id': 'r1'})
    sh.record_autogen_message(
        hist, {'role': 'assistant', 'content': 'Three quarters, well done.'},
        prompt_id='111')
    sh.record_autogen_message(
        hist, {'role': 'assistant',
               'content': "I'll help you activate as the HIVE GROWTH AGENT"},
        prompt_id=222)
    hist.flush_sync()
    return hist


def test_agent_seed_carries_only_that_agents_turns(sh):
    _buffer_with_two_agents(sh, 'u-scope')
    seed = sh.seed_autogen_from_shared_history(
        'u-scope', max_messages=8, prompt_id=111)
    assert [m['content'] for m in seed] == [
        'coach me on fractions',
        'Sure. What is 1/2 + 1/4?',
        'Three quarters, well done.',
    ]
    assert all(m.get('_from_shared') for m in seed), (
        "seeded messages must keep the _from_shared marker, or the write-back "
        "hook re-writes them into the buffer")


def test_another_agents_turn_never_reaches_this_agent(sh):
    _buffer_with_two_agents(sh, 'u-scope2')
    seed = sh.seed_autogen_from_shared_history(
        'u-scope2', max_messages=8, prompt_id='111')
    assert not any('HIVE GROWTH' in m['content'] for m in seed), (
        "agent 111 was seeded with agent 222's daemon turn -- the live echo")


def test_casual_seed_is_unchanged(sh):
    """No prompt_id is the user's casual chat: still the whole buffer."""
    _buffer_with_two_agents(sh, 'u-casual')
    seed = sh.seed_autogen_from_shared_history('u-casual', max_messages=8)
    assert len(seed) == 4


def test_writeback_stamps_the_agent(sh):
    gc = SimpleNamespace(messages=[])
    assert sh.install_history_writeback(gc, 'u-stamp', prompt_id=333) is True
    gc.messages.append({'role': 'user', 'content': 'remember BLUEFIN9',
                        'name': 'User'})
    time.sleep(0.8)  # the buffer flush is a 150 ms coalesced timer
    seed = sh.seed_autogen_from_shared_history(
        'u-stamp', max_messages=8, prompt_id=333)
    assert [m['content'] for m in seed] == ['remember BLUEFIN9']


_REUSE = (_ROOT / 'hartos' / 'reuse_recipe.py').read_text(encoding='utf-8')
_CREATE = (_ROOT / 'hartos' / 'create_recipe.py').read_text(encoding='utf-8')


def test_every_seed_call_passes_the_agent():
    calls = []
    for name, src in (('reuse_recipe', _REUSE), ('create_recipe', _CREATE)):
        for args in re.findall(r'seed_autogen_from_shared_history\(([^)]*)\)',
                               src):
            calls.append(args)
            assert 'prompt_id=' in args, (
                f"{name}: seed without the agent's prompt_id: ({args})")
    for args in re.findall(r'_seed_messages\(([^)]*)\)', _CREATE):
        calls.append(args)
        assert 'prompt_id' in args, (
            f"create_recipe: _seed_messages without prompt_id: ({args})")
    assert calls, "no seed call found -- the guard would be vacuous"


def test_every_writeback_call_passes_the_agent():
    calls = re.findall(r'install_history_writeback\(([^)]*)\)', _REUSE)
    assert calls, "no write-back call found -- the guard would be vacuous"
    for args in calls:
        assert 'prompt_id=' in args, (
            f"reuse_recipe: write-back without the agent's prompt_id: ({args})")


def test_create_writes_the_buffer_through_the_shared_writer():
    """create_recipe's ingest hook wrote the buffer inline.  It goes through
    shared_history.record_autogen_message so the stamp has one home."""
    assert 'hist.add_message(' not in _CREATE
    assert 'record_autogen_message(' in _CREATE
