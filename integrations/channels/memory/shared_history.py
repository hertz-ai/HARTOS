"""
Shared conversation history bridge — single buffer for both LangChain and AutoGen.

Eliminates redundancy: both frameworks read from and write to the same
PersistentChatHistory (buffer.json + SimpleMem). AutoGen's GroupChat starts
seeded with recent messages, and its new messages are written back.

Usage in reuse_recipe.py / create_recipe.py:

    from integrations.channels.memory.shared_history import (
        seed_autogen_from_shared_history,
        create_autogen_history_hook,
    )

    # Before GroupChat creation:
    seed_messages = seed_autogen_from_shared_history(user_id, max_messages=8)
    group_chat = autogen.GroupChat(agents=[...], messages=seed_messages, ...)

    # After GroupChat creation:
    hook = create_autogen_history_hook(user_id)
    if hook:
        group_chat.messages.append = hook(group_chat.messages.append)
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


def seed_autogen_from_shared_history(
    user_id: int,
    max_messages: int = 8,
) -> List[Dict[str, Any]]:
    """Load recent messages from the shared buffer as autogen GroupChat seed messages.

    Returns a list of autogen-formatted message dicts:
        [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]

    Deduplication: messages include a `_ts` key with the original timestamp.
    AutoGen won't re-write these to the buffer (the hook checks `_ts`).
    """
    history = _get_persistent_history(user_id)
    if not history:
        return []

    try:
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


def create_autogen_history_hook(
    user_id: int,
    simplemem_store=None,
) -> Optional[Callable]:
    """Create a hook that writes autogen messages back to the shared buffer.

    Returns a wrapper for GroupChat.messages.append that also writes to
    PersistentChatHistory — only for NEW messages (skips seeded ones).

    Usage:
        hook = create_autogen_history_hook(user_id)
        if hook:
            original_append = group_chat.messages.append
            group_chat.messages.append = hook(original_append)
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

            # Write new autogen messages to the shared buffer
            try:
                content = msg.get('content', '') if isinstance(msg, dict) else str(msg)
                role = msg.get('role', 'assistant') if isinstance(msg, dict) else 'assistant'

                if not content or content == 'TERMINATE':
                    return

                from langchain_core.messages import HumanMessage, AIMessage
                if role == 'user':
                    lc_msg = HumanMessage(content=content)
                else:
                    lc_msg = AIMessage(content=content)

                # Dedup: check if the exact same content+timestamp already exists
                # (within last 5 messages to keep it fast)
                existing = history.messages[-5:] if history.messages else []
                for ex in existing:
                    if ex.content == content:
                        return  # already in buffer — skip

                history.add_message(lc_msg, metadata={
                    'timestamp': datetime.now().isoformat(),
                    'source': 'autogen',
                })
            except Exception as e:
                logger.debug("Autogen→shared history write failed: %s", e)

        return hooked_append

    return _make_hook
