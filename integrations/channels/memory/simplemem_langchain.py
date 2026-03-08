"""
SimpleMem-backed LangChain memory — drop-in replacement for ZepMemory.

Zero-latency reads: load_memory_variables() returns in-memory buffer (no network).
Persistent writes: save_context() persists to JSON + feeds SimpleMem for semantic search.
Deterministic search: search_by_metadata() filters by request_Id, prompt_id, date range.
Semantic search: semantic_search(query) uses SimpleMem's adaptive retrieval.

Replaces ZepMemory which required an external Zep server (single point of failure).

Performance:
- Writes are deferred: in-memory append is instant, disk flush runs on a background
  thread with coalescing (multiple rapid writes → single I/O).
- Metadata indexes: O(1) lookup by request_Id / prompt_id via inverted index.
- Date range: bisect on sorted timestamp array → O(log n) bounds + O(k) scan.
- Read/write separation: RLock allows concurrent readers, writers don't block reads
  longer than an append.
- SimpleMem feed: single reusable background thread + event loop, no per-call overhead.
"""

import asyncio
import bisect
import json
import logging
import os
import threading
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import Field

from langchain_classic.memory.chat_memory import BaseChatMemory
from langchain_classic.schema import BaseChatMessageHistory
from langchain_classic.schema.messages import BaseMessage, HumanMessage, AIMessage

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

# Shared background event loop for async SimpleMem calls (one per process)
_bg_loop: Optional[asyncio.AbstractEventLoop] = None
_bg_thread: Optional[threading.Thread] = None
_bg_lock = threading.Lock()


def _get_bg_loop() -> asyncio.AbstractEventLoop:
    """Return a long-lived background event loop (started once, reused forever)."""
    global _bg_loop, _bg_thread
    if _bg_loop is not None and _bg_loop.is_running():
        return _bg_loop
    with _bg_lock:
        if _bg_loop is not None and _bg_loop.is_running():
            return _bg_loop
        _bg_loop = asyncio.new_event_loop()
        _bg_thread = threading.Thread(
            target=_bg_loop.run_forever, daemon=True, name='simplemem-io')
        _bg_thread.start()
        return _bg_loop


# ── Flush coalescing ──
# Multiple rapid add_message calls schedule a single disk write after a short
# delay.  If another write arrives before the timer fires, the timer resets.
_FLUSH_DELAY = 0.15  # seconds — coalesce writes within 150ms window


class PersistentChatHistory(BaseChatMessageHistory):
    """Chat history with persistent JSON buffer, metadata indexes, and SimpleMem feed."""

    __slots__ = (
        '_buffer_file', '_max_messages', '_simplemem_store',
        '_messages', '_metadata', '_timestamps', '_idx_request', '_idx_prompt',
        '_lock', '_flush_timer', '_dir_ensured',
    )

    def __init__(self, buffer_file: str, max_messages: int = 24,
                 simplemem_store: Any = None):
        self._buffer_file = buffer_file
        self._max_messages = max_messages
        self._simplemem_store = simplemem_store
        self._messages: List[BaseMessage] = []
        self._metadata: List[Dict[str, Any]] = []
        self._timestamps: List[str] = []       # sorted ISO strings for bisect
        self._idx_request: Dict[str, List[int]] = defaultdict(list)  # request_Id → [positions]
        self._idx_prompt: Dict[Any, List[int]] = defaultdict(list)   # prompt_id → [positions]
        self._lock = threading.RLock()
        self._flush_timer: Optional[threading.Timer] = None
        self._dir_ensured = False
        self._load_buffer()

    # ── Properties ──

    @property
    def messages(self) -> List[BaseMessage]:
        with self._lock:
            return list(self._messages)

    # ── Write path ──

    def add_message(self, message: BaseMessage, **kwargs) -> None:
        metadata = kwargs.get('metadata') or {}
        if 'timestamp' not in metadata:
            metadata['timestamp'] = datetime.now().isoformat()
        ts = metadata['timestamp']

        with self._lock:
            pos = len(self._messages)
            self._messages.append(message)
            self._metadata.append(metadata)
            self._timestamps.append(ts)

            # Update inverted indexes
            req_id = metadata.get('request_Id')
            if req_id is not None:
                self._idx_request[str(req_id)].append(pos)
            prom_id = metadata.get('prompt_id')
            if prom_id is not None:
                self._idx_prompt[prom_id].append(pos)

            # Trim if over capacity
            if len(self._messages) > self._max_messages:
                self._trim_locked()

            self._schedule_flush()

        # Feed SimpleMem on background loop (fire-and-forget)
        if self._simplemem_store is not None:
            speaker = "User" if isinstance(message, HumanMessage) else "Hevolve"
            store_meta = dict(metadata)
            store_meta['sender_name'] = speaker
            try:
                loop = _get_bg_loop()
                asyncio.run_coroutine_threadsafe(
                    self._simplemem_store.add(message.content, metadata=store_meta),
                    loop,
                )
            except Exception as e:
                logger.debug(f"SimpleMem ingest failed (non-blocking): {e}")

    def add_user_message(self, message: str) -> None:
        self.add_message(HumanMessage(content=message))

    def add_ai_message(self, message: str) -> None:
        self.add_message(AIMessage(content=message))

    def clear(self) -> None:
        with self._lock:
            self._messages.clear()
            self._metadata.clear()
            self._timestamps.clear()
            self._idx_request.clear()
            self._idx_prompt.clear()
            self._schedule_flush()

    # ── Trim + reindex ──

    def _trim_locked(self):
        """Trim to max_messages and rebuild indexes.  Called under lock."""
        trim = len(self._messages) - self._max_messages
        self._messages = self._messages[trim:]
        self._metadata = self._metadata[trim:]
        self._timestamps = self._timestamps[trim:]
        self._rebuild_indexes_locked()

    def _rebuild_indexes_locked(self):
        """Rebuild inverted indexes from scratch.  Called under lock after trim."""
        self._idx_request.clear()
        self._idx_prompt.clear()
        for i, meta in enumerate(self._metadata):
            req_id = meta.get('request_Id')
            if req_id is not None:
                self._idx_request[str(req_id)].append(i)
            prom_id = meta.get('prompt_id')
            if prom_id is not None:
                self._idx_prompt[prom_id].append(i)

    # ── Deferred disk flush ──

    def _schedule_flush(self):
        """Schedule a coalesced disk write.  Resets timer on rapid calls."""
        if self._flush_timer is not None:
            self._flush_timer.cancel()
        self._flush_timer = threading.Timer(_FLUSH_DELAY, self._flush_to_disk)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def _flush_to_disk(self):
        """Write buffer to JSON — runs on timer thread, grabs lock briefly to snapshot."""
        with self._lock:
            data = [
                {
                    'type': type(m).__name__,
                    'content': m.content,
                    'metadata': self._metadata[i],
                }
                for i, m in enumerate(self._messages)
            ]
        # Disk I/O outside lock
        try:
            if not self._dir_ensured:
                os.makedirs(os.path.dirname(self._buffer_file), exist_ok=True)
                self._dir_ensured = True
            with open(self._buffer_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, separators=(',', ':'))
        except Exception as e:
            logger.debug(f"Could not save buffer to {self._buffer_file}: {e}")

    def flush_sync(self):
        """Force immediate flush (for shutdown / test teardown)."""
        if self._flush_timer is not None:
            self._flush_timer.cancel()
        self._flush_to_disk()

    # ── Load ──

    def _load_buffer(self):
        """Load persisted messages from JSON file and build indexes."""
        if not os.path.exists(self._buffer_file):
            return
        try:
            with open(self._buffer_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            _msg_cls = {'AIMessage': AIMessage}
            for item in data:
                cls = _msg_cls.get(item.get('type'), HumanMessage)
                self._messages.append(cls(content=item.get('content', '')))
                meta = item.get('metadata', {})
                self._metadata.append(meta)
                self._timestamps.append(meta.get('timestamp', ''))
            self._rebuild_indexes_locked()
            logger.debug(f"Loaded {len(self._messages)} messages from {self._buffer_file}")
        except Exception as e:
            logger.debug(f"Could not load buffer from {self._buffer_file}: {e}")

    # ── Search: deterministic by metadata + date range ──

    def search_by_metadata(self, date_from: str = None, date_to: str = None,
                           **filters) -> List[Dict]:
        """Deterministic search with O(1) index lookup or O(log n) date bisect.

        Args:
            date_from: ISO8601 start (inclusive).
            date_to:   ISO8601 end (inclusive).  Date-only → end-of-day.
            **filters: Key-value pairs matched against metadata.

        Fast paths:
        - request_Id only → inverted index O(1)
        - prompt_id only  → inverted index O(1)
        - date range only → bisect O(log n) + O(k) slice
        - combined → intersect index hits with date bounds
        """
        with self._lock:
            candidates = self._resolve_candidates(filters, date_from, date_to)
            return [
                {
                    'type': type(self._messages[i]).__name__,
                    'content': self._messages[i].content,
                    'metadata': self._metadata[i],
                }
                for i in candidates
            ]

    def _resolve_candidates(self, filters: Dict, date_from: str,
                            date_to: str) -> List[int]:
        """Resolve candidate positions using indexes + bisect.  Called under lock."""
        n = len(self._messages)
        if n == 0:
            return []

        # Start with full range
        candidate_set: Optional[set] = None

        # Fast path: indexed key lookup
        req_id = filters.pop('request_Id', None)
        if req_id is not None:
            hits = self._idx_request.get(str(req_id), [])
            candidate_set = set(hits)

        prom_id = filters.pop('prompt_id', None)
        if prom_id is not None:
            hits = self._idx_prompt.get(prom_id, [])
            if candidate_set is not None:
                candidate_set &= set(hits)
            else:
                candidate_set = set(hits)

        # Date range: bisect on _timestamps (ISO strings sort lexicographically)
        lo, hi = 0, n
        if date_from:
            lo = bisect.bisect_left(self._timestamps, date_from)
        if date_to:
            # Expand date-only to end-of-day for inclusive comparison
            upper = date_to
            if 'T' not in date_to:
                upper = date_to + 'T23:59:59.999999'
            hi = bisect.bisect_right(self._timestamps, upper)

        date_set = set(range(lo, hi)) if (date_from or date_to) else None

        if date_set is not None:
            candidate_set = (candidate_set & date_set) if candidate_set is not None else date_set

        # If no indexed filters applied, use full range
        if candidate_set is None:
            candidate_set = set(range(n))

        # Remaining arbitrary filters (non-indexed keys)
        if filters:
            candidate_set = {
                i for i in candidate_set
                if all(self._metadata[i].get(k) == v for k, v in filters.items())
            }

        return sorted(candidate_set)

    # ── Search: semantic via SimpleMem ──

    def semantic_search(self, query: str, max_results: int = 10) -> List[Dict]:
        """Semantic search using SimpleMem's adaptive retrieval."""
        if self._simplemem_store is None:
            return []
        try:
            loop = _get_bg_loop()
            future = asyncio.run_coroutine_threadsafe(
                self._simplemem_store.search(query, max_results=max_results), loop)
            results = future.result(timeout=5.0)
            return [{'content': r.content, 'score': r.score} for r in results]
        except Exception as e:
            logger.warning(f"SimpleMem semantic search failed: {e}")
            return []


class SimpleMemChatMemory(BaseChatMemory):
    """
    LangChain memory backed by SimpleMem + persistent message buffer.

    Zero-latency design:
    - load_memory_variables(): returns in-memory list (O(1), no I/O)
    - save_context(): in-memory append + deferred disk flush
    - search_by_metadata(): O(1) indexed lookup / O(log n) date bisect
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

    def save_context(self, inputs: Dict[str, Any], outputs: Dict[str, str],
                     metadata: Optional[Dict[str, Any]] = None) -> None:
        """Save context with optional metadata for deterministic retrieval.

        Extends BaseChatMemory.save_context() to thread metadata through to
        the persistent buffer + SimpleMem, enabling search_by_metadata()
        lookups by request_Id, prompt_id, date range, or any custom key.
        """
        input_str = inputs.get(self.input_key, next(iter(inputs.values()), ''))
        output_key = self.output_key or 'output'
        output_str = outputs.get(output_key, next(iter(outputs.values()), ''))
        meta = metadata or {}

        self.chat_memory.add_message(
            HumanMessage(content=str(input_str)), metadata=meta)
        self.chat_memory.add_message(
            AIMessage(content=str(output_str)), metadata=meta)

    def search_by_metadata(self, date_from: str = None, date_to: str = None,
                           **filters) -> List[Dict]:
        """Deterministic search by metadata and/or date range.

        Usage:
            memory.search_by_metadata(request_Id='1771756765')
            memory.search_by_metadata(date_from='2026-02-22', date_to='2026-02-23')
            memory.search_by_metadata(date_from='2026-02-22T16:00:00', prompt_id=0)
        """
        if isinstance(self.chat_memory, PersistentChatHistory):
            return self.chat_memory.search_by_metadata(
                date_from=date_from, date_to=date_to, **filters)
        return []

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
