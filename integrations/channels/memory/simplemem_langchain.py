"""
SimpleMem-backed LangChain memory — drop-in replacement for ZepMemory.

Zero-latency reads: load_memory_variables() returns in-memory buffer (no network).
Persistent writes: save_context() persists to JSON + feeds SimpleMem for semantic search.
Semantic search: semantic_search(query) uses SimpleMem's adaptive retrieval.

Replaces ZepMemory which required an external Zep server (single point of failure).
"""

import json
import logging
import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import Field

from langchain.memory.chat_memory import BaseChatMemory
from langchain.schema import BaseChatMessageHistory
from langchain.schema.messages import BaseMessage, HumanMessage, AIMessage

logger = logging.getLogger('hevolve_core')

# SimpleMem is optional — buffer-only mode if unavailable
try:
    from integrations.channels.memory.simplemem_store import (
        SimpleMemStore, SimpleMemConfig, HAS_SIMPLEMEM
    )
except ImportError:
    HAS_SIMPLEMEM = False

SIMPLEMEM_DB_ROOT = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), 'simplemem_db')


class PersistentChatHistory(BaseChatMessageHistory):
    """Chat history that persists to a JSON file and optionally feeds SimpleMem."""

    def __init__(self, buffer_file: str, max_messages: int = 24,
                 simplemem_store: Any = None):
        self._buffer_file = buffer_file
        self._max_messages = max_messages
        self._simplemem_store = simplemem_store
        self._messages: List[BaseMessage] = []
        self._lock = threading.Lock()
        self._load_buffer()

    @property
    def messages(self) -> List[BaseMessage]:
        with self._lock:
            return list(self._messages)

    def add_message(self, message: BaseMessage, **kwargs) -> None:
        with self._lock:
            self._messages.append(message)
            # Trim to prevent unbounded growth
            if len(self._messages) > self._max_messages:
                self._messages = self._messages[-self._max_messages:]
            self._save_buffer()

        # Feed SimpleMem in background (non-blocking, zero latency impact)
        if self._simplemem_store is not None:
            speaker = "User" if isinstance(message, HumanMessage) else "Hevolve"
            try:
                self._simplemem_store._system.add_dialogue(
                    speaker, message.content, datetime.now().isoformat()
                )
            except Exception as e:
                logger.debug(f"SimpleMem ingest failed (non-blocking): {e}")

    def add_user_message(self, message: str) -> None:
        self.add_message(HumanMessage(content=message))

    def add_ai_message(self, message: str) -> None:
        self.add_message(AIMessage(content=message))

    def clear(self) -> None:
        with self._lock:
            self._messages = []
            self._save_buffer()

    def _load_buffer(self):
        """Load persisted messages from JSON file."""
        if not os.path.exists(self._buffer_file):
            return
        try:
            with open(self._buffer_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for item in data:
                msg_type = item.get('type', 'HumanMessage')
                content = item.get('content', '')
                if msg_type == 'AIMessage':
                    self._messages.append(AIMessage(content=content))
                else:
                    self._messages.append(HumanMessage(content=content))
            logger.debug(f"Loaded {len(self._messages)} messages from {self._buffer_file}")
        except Exception as e:
            logger.debug(f"Could not load buffer from {self._buffer_file}: {e}")

    def _save_buffer(self):
        """Persist messages to JSON file (called under lock)."""
        try:
            os.makedirs(os.path.dirname(self._buffer_file), exist_ok=True)
            data = [
                {'type': type(m).__name__, 'content': m.content}
                for m in self._messages
            ]
            with open(self._buffer_file, 'w', encoding='utf-8') as f:
                json.dump(data, f)
        except Exception as e:
            logger.debug(f"Could not save buffer to {self._buffer_file}: {e}")

    def semantic_search(self, query: str, max_results: int = 10) -> List[Dict]:
        """Semantic search using SimpleMem's adaptive retrieval."""
        if self._simplemem_store is None:
            return []
        try:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    loop = asyncio.new_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
            results = loop.run_until_complete(
                self._simplemem_store.search(query, max_results=max_results)
            )
            return [{'content': r.content, 'score': r.score} for r in results]
        except Exception as e:
            logger.warning(f"SimpleMem semantic search failed: {e}")
            return []


class SimpleMemChatMemory(BaseChatMemory):
    """
    LangChain memory backed by SimpleMem + persistent message buffer.

    Zero-latency design:
    - load_memory_variables(): returns in-memory list (O(1), no I/O)
    - save_context(): writes to buffer + SimpleMem (fire-and-forget)
    - semantic_search(): SimpleMem vector search for FULL_HISTORY tool
    """

    memory_key: str = "chat_history"
    return_messages: bool = True
    input_key: str = "input"
    max_buffer_size: int = 8

    class Config:
        arbitrary_types_allowed = True

    @property
    def memory_variables(self) -> List[str]:
        return [self.memory_key]

    def load_memory_variables(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Return recent messages — zero latency, pure in-memory."""
        msgs = self.chat_memory.messages
        return {self.memory_key: msgs[-self.max_buffer_size:]}

    def semantic_search(self, query: str, max_results: int = 10) -> List[Dict]:
        """Semantic search for FULL_HISTORY tool."""
        if isinstance(self.chat_memory, PersistentChatHistory):
            return self.chat_memory.semantic_search(query, max_results)
        return []

    @classmethod
    def load_or_create(cls, user_id: int, prompt_id: int = None):
        """
        Factory: creates memory with persistent buffer + optional SimpleMem.

        Args:
            user_id: The user ID
            prompt_id: Optional prompt ID (unused — memory is per-user like Zep was)
        """
        session_id = f"user_{user_id}"
        db_path = os.path.join(SIMPLEMEM_DB_ROOT, session_id)
        buffer_file = os.path.join(db_path, 'buffer.json')

        # Create SimpleMem store if available
        simplemem_store = None
        if HAS_SIMPLEMEM:
            try:
                config = SimpleMemConfig.from_env()
                config.db_path = db_path
                simplemem_store = SimpleMemStore(config)
            except Exception as e:
                logger.debug(f"SimpleMem init failed for {session_id}: {e}")

        chat_history = PersistentChatHistory(
            buffer_file=buffer_file,
            simplemem_store=simplemem_store,
        )

        return cls(
            chat_memory=chat_history,
            return_messages=True,
            input_key="input",
        )
