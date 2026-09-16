"""
Shared conversation history bridge — single buffer for both LangChain and AutoGen.

Eliminates redundancy: both frameworks read from and write to the same
PersistentChatHistory (buffer.json + SimpleMem). AutoGen's GroupChat starts
seeded with recent messages, and its new messages are written back.

Usage in reuse_recipe.py / create_recipe.py.  An agent-bound chat passes its
prompt_id to both halves, so it is seeded with that agent's turns only:

    from integrations.channels.memory.shared_history import (
        seed_autogen_from_shared_history,
        install_history_writeback,
    )

    seed_messages = seed_autogen_from_shared_history(
        user_id, max_messages=8, prompt_id=prompt_id)
    group_chat = autogen.GroupChat(agents=[...], messages=seed_messages, ...)
    install_history_writeback(group_chat, user_id, prompt_id=prompt_id)
"""

import logging
import os
from datetime import datetime
from typing import List, Dict, Any, Optional, Callable

logger = logging.getLogger(__name__)

# Canonical buffer root — same as simplemem_langchain.py
SIMPLEMEM_DB_ROOT = os.path.join(
    os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'simplemem')


def _get_persistent_history(user_id: int):
    """Get the PersistentChatHistory instance for a user (same one LangChain uses)."""
    try:
        from integrations.channels.memory.simplemem_langchain import SimpleMemChatMemory
        memory = SimpleMemChatMemory.load_or_create(user_id)
        if hasattr(memory, 'chat_memory'):
            return memory.chat_memory
    except Exception as e:
        logger.debug("Could not load PersistentChatHistory for user %s: %s", user_id, e)
    return None


def _same_prompt(a, b) -> bool:
    """prompt_id reaches the buffer as an int from the LangChain leg and as an
    int or a str from the autogen callers; compare them as text."""
    return a is not None and b is not None and str(a) == str(b)


def _seed_for_prompt(history, prompt_id, max_messages, user_id):
    """The last ``max_messages`` buffer entries stamped with ``prompt_id``,
    oldest first, as seed messages."""
    entries = [
        e for e in history.search_by_metadata()
        if _same_prompt((e.get('metadata') or {}).get('prompt_id'), prompt_id)
    ]
    seed = []
    for e in entries[-max_messages:]:
        if e.get('type') == 'HumanMessage':
            role, name = 'user', 'User'
        elif e.get('type') == 'AIMessage':
            role, name = 'assistant', 'assistant'
        else:
            continue
        seed.append({
            "role": role,
            "name": name,
            "content": e.get('content', ''),
            "_ts": (e.get('metadata') or {}).get('timestamp'),
            "_from_shared": True,
        })
    logger.info("Seeded autogen with %d messages from shared history "
                "(user %s, prompt %s)", len(seed), user_id, prompt_id)
    return seed


def seed_autogen_from_shared_history(
    user_id: int,
    max_messages: int = 8,
    prompt_id=None,
) -> List[Dict[str, Any]]:
    """Load recent messages from the shared buffer as autogen GroupChat seed messages.

    Returns a list of autogen-formatted message dicts:
        [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]

    ``prompt_id`` scopes the seed to ONE agent's conversation.  The buffer is
    per-user by design, so without it an agent-bound chat was seeded with the
    last N messages of every agent and daemon goal of that user: on
    2026-09-13 agent 12165936867 answered a direct request with a hive-growth
    daemon's turn and never ran an action.  Every agent-bound chat passes it;
    only the user's casual chat omits it and keeps the whole buffer.

    Deduplication: messages include a `_ts` key with the original timestamp.
    AutoGen won't re-write these to the buffer (the hook checks `_ts`).
    """
    history = _get_persistent_history(user_id)
    if not history:
        return []

    try:
        if prompt_id is not None:
            return _seed_for_prompt(history, prompt_id, max_messages, user_id)

        from langchain_core.messages import HumanMessage, AIMessage
        raw_msgs = history.messages  # thread-safe property copy
        timestamps = getattr(history, '_timestamps', [])

        # Take the last N messages
        recent = raw_msgs[-max_messages:]
        recent_ts = timestamps[-max_messages:] if timestamps else []

        seed = []
        for i, msg in enumerate(recent):
            if isinstance(msg, HumanMessage):
                role = "user"
                name = "User"
            elif isinstance(msg, AIMessage):
                role = "assistant"
                name = "assistant"
            else:
                continue

            ts = recent_ts[i] if i < len(recent_ts) else None
            seed.append({
                "role": role,
                "name": name,
                "content": msg.content,
                "_ts": ts,  # marker to prevent re-write
                "_from_shared": True,  # marker for dedup
            })

        logger.info("Seeded autogen with %d messages from shared history (user %s)",
                     len(seed), user_id)
        return seed
    except Exception as e:
        logger.warning("Failed to seed autogen from shared history: %s", e)
        return []


def record_autogen_message(history, msg, prompt_id=None) -> bool:
    """Write one autogen GroupChat message into the shared buffer.

    The one autogen writer of PersistentChatHistory.  create_recipe's ingest
    hook and create_autogen_history_hook both come through here, so the
    prompt_id stamp that seed_autogen_from_shared_history filters on has a
    single home.  Skips empty and TERMINATE messages, and a message already
    among the last five.  Returns True when a message was written.
    """
    try:
        content = msg.get('content', '') if isinstance(msg, dict) else str(msg)
        role = msg.get('role', 'assistant') if isinstance(msg, dict) else 'assistant'

        if not content or content == 'TERMINATE':
            return False

        from langchain_core.messages import HumanMessage, AIMessage
        if role == 'user':
            lc_msg = HumanMessage(content=content)
        else:
            lc_msg = AIMessage(content=content)

        # Dedup: skip content already among the last 5 messages
        existing = history.messages[-5:] if history.messages else []
        for ex in existing:
            if ex.content == content:
                return False  # already in buffer — skip

        metadata = {
            'timestamp': datetime.now().isoformat(),
            'source': 'autogen',
        }
        if prompt_id is not None:
            metadata['prompt_id'] = prompt_id
        history.add_message(lc_msg, metadata=metadata)
        return True
    except Exception as e:
        logger.debug("Autogen→shared history write failed: %s", e)
        return False


def create_autogen_history_hook(
    user_id: int,
    simplemem_store=None,
    simplemem_metadata: Optional[Dict[str, Any]] = None,
    prompt_id=None,
) -> Optional[Callable]:
    """Create a hook that writes autogen messages back to the shared buffer.

    Returns a wrapper for GroupChat.messages.append that also writes to
    PersistentChatHistory — only for NEW messages (skips seeded ones).
    ``prompt_id`` stamps each write so the agent's next seed can find it.
    Install it with install_history_writeback: assigning to
    ``group_chat.messages.append`` raises AttributeError on a plain list.
    """
    history = _get_persistent_history(user_id)
    if not history:
        return None

    def _make_hook(orig_append):
        def hooked_append(msg):
            # Call original append first
            orig_append(msg)

            # Skip seeded messages (already in buffer)
            if isinstance(msg, dict) and msg.get('_from_shared'):
                return

            # SimpleMem ingest.  This function has ALWAYS taken
            # simplemem_store and never used it, so every caller that passed
            # a store got silent no-ingest — which is exactly why
            # reuse_recipe hand-rolled its own hook instead of calling
            # install_history_writeback, and that copy then grew the
            # orphaned-append + loop-scoped-class defects.  Honour the
            # parameter here so the canonical helper is a real superset.
            if simplemem_store is not None:
                try:
                    content = msg.get('content', '') if isinstance(msg, dict) else str(msg)
                    if content and len(content.strip()) > 5 and content != 'TERMINATE':
                        from core.event_loop import get_or_create_event_loop
                        speaker = msg.get('name', 'Agent') if isinstance(msg, dict) else 'Agent'
                        _meta = {'sender_name': speaker, 'user_id': user_id}
                        if prompt_id is not None:
                            _meta['prompt_id'] = prompt_id
                        _meta.update(simplemem_metadata or {})
                        loop = get_or_create_event_loop()
                        loop.run_until_complete(simplemem_store.add(content, _meta))
                except Exception:
                    logger.debug("SimpleMem ingest skipped", exc_info=True)

            # Write new autogen messages to the shared buffer
            record_autogen_message(history, msg, prompt_id=prompt_id)

        return hooked_append

    return _make_hook


# ─── What the write-back stores ──────────────────────────────────────────
# A tool whose result is a READ of state HARTOS already persists (MemoryGraph,
# SimpleMem, agent_data, the chat history) is marked reads_persisted_state
# where it is registered (core.tool_traits). The group chat's write-back does
# not store such a result again: the store already holds it, and writing a
# read back makes the next read return more. Live on central 2026-09-14
# (#104): Guardian Convergence's graph reached 28.6M chars that way, and one
# recall came back at 3,386,616 chars.


def _tool_reads_persisted_state(group_chat, name):
    """Whether the tool registered as ``name`` in this group carries the mark.

    An unknown name counts as not a persisted read, so its result is kept.
    That fails open on purpose (nothing is dropped on a guess), and it is
    safe only because every stored row is bounded to MEMORY_ITEM_MAX_CHARS
    (_bounded_for_storage here, MemoryGraph.register there): an unmatched
    read is stored once, bounded, and cannot grow the next recall. Keep the
    bound if you change this.
    """
    from core.tool_traits import READS_PERSISTED_STATE, has_trait
    if not name:
        return False
    for agent in getattr(group_chat, 'agents', None) or []:
        fmap = getattr(agent, 'function_map', None)
        fn = fmap.get(name) if isinstance(fmap, dict) else None
        if fn is not None:
            return has_trait(fn, READS_PERSISTED_STATE)
    return False


def _bounded_for_storage(msg):
    """``msg`` with its text bounded to MEMORY_ITEM_MAX_CHARS, as a copy.

    The content and every tool_responses entry, with the bound MemoryGraph.
    register applies, so a row cut here and a row cut there read the same.
    """
    from core.constants import MEMORY_ITEM_MAX_CHARS
    from core.token_utils import bound_text

    def _over(text):
        return isinstance(text, str) and len(text) > MEMORY_ITEM_MAX_CHARS

    out = msg
    if _over(msg.get('content')):
        out = dict(out)
        out['content'] = bound_text(msg['content'], MEMORY_ITEM_MAX_CHARS)
    responses = msg.get('tool_responses')
    if isinstance(responses, list) and any(
            isinstance(r, dict) and _over(r.get('content')) for r in responses):
        out = dict(out)
        out['tool_responses'] = [
            {**r, 'content': bound_text(r['content'], MEMORY_ITEM_MAX_CHARS)}
            if isinstance(r, dict) and _over(r.get('content')) else r
            for r in responses]
    return out


def _make_storage_view(group_chat):
    """``view(msg)``: the message as the write-back sinks may store it.

    Returns None when nothing in it is to be stored, else a copy with the
    persisted-state reads taken out of a tool reply and the text bounded.
    Never edits ``msg``: run_chat broadcasts the dict it appends to every
    seat. Tool calls are matched to their replies by tool_call_id, so a
    bundled reply keeps the results of the other tools it carries; an id is
    dropped once its reply is seen, since reuse keeps a group for the whole
    session.
    """
    calls = {}  # tool_call_id -> function name, until its reply arrives

    def view(msg):
        if not isinstance(msg, dict):
            return msg
        for tc in msg.get('tool_calls') or []:
            if isinstance(tc, dict) and tc.get('id'):
                calls[tc['id']] = (tc.get('function') or {}).get('name') or ''
        if (msg.get('role') or '') != 'tool':
            return _bounded_for_storage(msg)
        bundled = isinstance(msg.get('tool_responses'), list) and msg['tool_responses']
        responses = msg['tool_responses'] if bundled else [msg]
        kept = [r for r in responses if isinstance(r, dict)
                and not _tool_reads_persisted_state(
                    group_chat, calls.pop(r.get('tool_call_id'), ''))]
        if not kept:
            return None
        if len(kept) == len(responses):
            return _bounded_for_storage(msg)
        out = dict(msg)
        out['tool_responses'] = kept
        out['content'] = '\n\n'.join(str(r.get('content') or '') for r in kept)
        return _bounded_for_storage(out)

    return view


def graph_conversation_sink(memory_graph, session_id):
    """The one MemoryGraph sink for a group chat's write-back.

    Each stored message becomes a conversation row under ``session_id``,
    spoken by the message's name. create_agents and reuse's group builder
    carried identical private copies of this; both use this one now.
    """
    def _sink(msg):
        content = msg.get('content', '') if isinstance(msg, dict) else str(msg)
        speaker = msg.get('name', 'Agent') if isinstance(msg, dict) else 'Agent'
        if content and len(content.strip()) > 5:
            memory_graph.register_conversation(speaker, content, session_id)
    return _sink


class HookedMessageList(list):
    """A list whose append also feeds a per-message hook.

    autogen's GroupChat.messages is a plain list, and ``list.append`` is a
    read-only slot — the usage shown on create_autogen_history_hook
    (assigning to messages.append) raises AttributeError.  Replacing the
    list with this subclass is the working install (first proven at the
    visual-group site in reuse_recipe)."""

    def __init__(self, data, hook):
        super().__init__(data)
        self._hook = hook

    def append(self, msg):
        super().append(msg)
        try:
            self._hook(msg)
        except Exception:
            logger.debug("history write-back hook failed", exc_info=True)


def install_history_writeback(group_chat, user_id, simplemem_store=None,
                              extra_sinks=None, simplemem_metadata=None,
                              prompt_id=None):
    """Wrap group_chat.messages so every appended message reaches every sink:
    the shared PersistentChatHistory (dedup-aware, seeds skipped), SimpleMem
    when a store is given, and any ``extra_sinks`` callables (e.g. MemoryGraph
    ingest).  ``prompt_id`` stamps the buffer writes with the agent they came
    from, the half of the contract seed_autogen_from_shared_history's
    agent-scoped seed reads.

    This is the write half of the seed/write contract: a group seeded via
    seed_autogen_from_shared_history but never write-back-hooked loses its
    whole conversation (#686 — live 2026-08-23, the role group's BLUEFIN6
    exchange never reached the buffer the next turn read).

    ONE wrap, composed here.  Wrapping twice (a second subclass around the
    first) silently kills the inner hook: the outer append calls plain
    ``list.append``, never the inner subclass's ``append``.  reuse_recipe's
    hand-rolled copy did exactly that — its ``isinstance`` re-check compared
    against a class redefined inside a ``for`` loop, so it was False for two
    of three group chats and their shared-history + SimpleMem write-back was
    dead.  Extra sinks therefore compose into this single hook rather than
    stacking another list subclass.

    Returns True when at least one sink is active, False when there is
    nothing to write to."""
    factory = create_autogen_history_hook(user_id, simplemem_store,
                                          simplemem_metadata,
                                          prompt_id=prompt_id)
    sinks = []
    if factory is not None:
        # The factory wraps an append callable; the list subclass already did
        # the append, so hand it a no-op and keep only the write-back side.
        sinks.append(factory(lambda _msg: None))
    for _s in (extra_sinks or []):
        if callable(_s):
            sinks.append(_s)
    if not sinks:
        return False

    # Every sink stores the same view of a message: persisted-state reads left
    # out, content bounded (see _make_storage_view).
    storage_view = _make_storage_view(group_chat)

    def _fanout(msg):
        stored = storage_view(msg)
        if stored is None:
            return
        for _sink in sinks:
            try:
                _sink(stored)
            except Exception:
                logger.debug("history sink failed", exc_info=True)

    group_chat.messages = HookedMessageList(group_chat.messages, _fanout)
    return True
