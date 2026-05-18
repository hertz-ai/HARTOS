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
import re
import threading
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import Field

from langchain_classic.memory.chat_memory import BaseChatMemory
from langchain_classic.schema import BaseChatMessageHistory
from langchain_classic.schema.messages import (
    BaseMessage, HumanMessage, AIMessage, SystemMessage,
)
try:
    # langchain_core 1.x canonical helper for human/ai-prefixed buffer string.
    # Available in both langchain_classic.schema and langchain_core.messages.
    from langchain_core.messages import get_buffer_string as _lc_get_buffer_string
except Exception:  # pragma: no cover — fallback if helper moves
    _lc_get_buffer_string = None

logger = logging.getLogger('hevolve_core')

# SimpleMem is optional — buffer-only mode if unavailable
try:
    from integrations.channels.memory.simplemem_store import (
        SimpleMemStore, SimpleMemConfig, HAS_SIMPLEMEM
    )
except ImportError:
    HAS_SIMPLEMEM = False

try:
    from core.platform_paths import get_simplemem_dir
    SIMPLEMEM_DB_ROOT = get_simplemem_dir()
except ImportError:
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


# ── Token approximation ──
# tiktoken is heavy + not always present; for prune() we want a cheap upper
# bound, not exact billing.  ~1 token ≈ 4 chars (English) is the rule of
# thumb used in the OpenAI tokenizer cookbook.  If tiktoken IS installed,
# use it for accuracy.
def _approx_token_count(text: str) -> int:
    """Cheap token estimate.  Uses tiktoken if available, else chars/4."""
    try:
        import tiktoken  # type: ignore
        enc = tiktoken.get_encoding('cl100k_base')
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)


# Word-overlap scorer for ranking persisted messages against a query when
# SimpleMem is unavailable or for the keyword-only path.  This is the same
# heuristic ZepMemory used as a baseline when its vector backend was warm
# but empty (cold sessions).  Two helpers: tokenize + overlap ratio.
_WORD_SPLIT = re.compile(r'\w+', re.UNICODE)


def _tokenize_lower(text: str) -> List[str]:
    if not text:
        return []
    return [t.lower() for t in _WORD_SPLIT.findall(text)]


def _keyword_overlap_score(query_tokens: List[str], text: str) -> float:
    """Jaccard-like keyword overlap in [0, 1].  Empty query → 0.0."""
    if not query_tokens:
        return 0.0
    text_tokens = set(_tokenize_lower(text))
    if not text_tokens:
        return 0.0
    q_set = set(query_tokens)
    inter = q_set & text_tokens
    if not inter:
        return 0.0
    return len(inter) / max(1, len(q_set))


class ChatMemorySearchResult:
    """ZepMemory-compatible search result wrapper.

    Drop-in replacement for the ``MemorySearchResult`` objects ZepMemory
    returned from ``chat_memory.search(...)``.  Callers in helper.py
    accessed:

        r.dict(exclude_unset=True)   # pydantic-style serialization
        r.message['content']         # message body
        r.message['role']            # 'human' | 'ai'
        r.message['created_at']      # ISO timestamp
        r.message['metadata']        # arbitrary dict
        r.dist                       # semantic distance (1 - score)
        r.score                      # semantic similarity

    Keeping field names + .dict() shape identical means the migration
    from ZepMemory is a one-line import swap with no caller changes.
    """

    __slots__ = ('message', 'dist', 'score', 'uuid')

    def __init__(self, message: Dict[str, Any], score: float = 1.0,
                 uuid: Optional[str] = None):
        self.message = message
        self.score = float(score)
        self.dist = 1.0 - float(score)
        self.uuid = uuid or ''

    def dict(self, exclude_unset: bool = False) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            'message': dict(self.message),
            'dist': self.dist,
            'score': self.score,
        }
        if self.uuid:
            d['uuid'] = self.uuid
        return d

    # Pydantic v2 compatibility
    def model_dump(self, **kwargs) -> Dict[str, Any]:
        return self.dict(exclude_unset=kwargs.get('exclude_unset', False))

    def __repr__(self) -> str:
        preview = (self.message.get('content', '') or '')[:60]
        return f'<ChatMemorySearchResult score={self.score:.2f} "{preview}">'


def _msg_role(message: BaseMessage) -> str:
    """Return ZepMemory-compatible role string for a BaseMessage."""
    if isinstance(message, HumanMessage):
        return 'human'
    if isinstance(message, AIMessage):
        return 'ai'
    if isinstance(message, SystemMessage):
        return 'system'
    return getattr(message, 'type', 'human')


def _msg_to_dict(message: BaseMessage, metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Build the ``message`` dict that ChatMemorySearchResult exposes."""
    return {
        'content': message.content,
        'role': _msg_role(message),
        'created_at': metadata.get('timestamp', ''),
        'metadata': dict(metadata),
        'uuid': metadata.get('uuid', ''),
    }


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

    # ── BaseChatMessageHistory bulk + async parity ──
    # langchain_core's BaseChatMessageHistory protocol expects:
    #   add_messages(messages)   — bulk synchronous
    #   aadd_messages(messages)  — bulk async
    #   aget_messages()          — async messages fetch
    # The default base-class implementations call add_message in a loop, but
    # default async methods spin up a thread-per-call.  Implement them
    # explicitly so the persistent buffer + SimpleMem ingest happen on the
    # background loop without per-message overhead.

    def add_messages(self, messages: List[BaseMessage]) -> None:
        """Bulk-add messages.  Each message gets its own coalesced flush."""
        for m in messages:
            self.add_message(m)

    async def aadd_messages(self, messages: List[BaseMessage]) -> None:
        """Async bulk add — delegates to the sync path (write is in-memory)."""
        self.add_messages(messages)

    async def aget_messages(self) -> List[BaseMessage]:
        """Async messages fetch — pure in-memory snapshot, no I/O."""
        return self.messages

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

    # ── ZepMemory-compatible unified search ──

    def search(self, query: str, metadata: Optional[Dict[str, Any]] = None,
               limit: int = 10) -> List[ChatMemorySearchResult]:
        """ZepMemory ``chat_memory.search()`` drop-in.

        Combines metadata-based filtering with semantic ranking.  Used by
        helper.py's ``get_time_based_history`` and other callers that
        previously relied on Zep's ``MemorySearchResult`` shape.

        Args:
            query: Free-text query.  When empty, returns date-range hits
                in chronological order.
            metadata: Dict of filters.  Recognized keys:
                - ``start_date`` / ``end_date`` (ISO-8601) — applied as
                  date bounds via the bisect index.
                - ``request_Id`` / ``prompt_id`` — applied via inverted
                  indexes (O(1)).
                - Any other key — exact-match metadata filter.
            limit: Maximum results to return.

        Returns:
            List of ``ChatMemorySearchResult`` ordered by descending
            score.  Each item exposes ``.dict()``, ``.message`` dict
            (content/role/created_at/metadata), ``.dist``, ``.score``
            — identical to ZepMemory's API.
        """
        meta = dict(metadata or {})
        date_from = meta.pop('start_date', None) or meta.pop('date_from', None)
        date_to = meta.pop('end_date', None) or meta.pop('date_to', None)

        with self._lock:
            candidate_idx = self._resolve_candidates(
                dict(meta), date_from, date_to)
            if not candidate_idx:
                # No metadata hits — fall through to pure semantic search
                # via SimpleMem (covers the "find messages about X" path
                # when no date range was given).
                if not (date_from or date_to or meta) and query:
                    return self._semantic_only_search(query, limit)
                return []

            query_tokens = _tokenize_lower(query) if query else []
            scored: List[Tuple[float, int]] = []
            for i in candidate_idx:
                msg = self._messages[i]
                if query_tokens:
                    score = _keyword_overlap_score(query_tokens, msg.content)
                else:
                    # No query → newer-first ordering, normalized to [0,1]
                    score = 0.5 + (i / max(1, len(self._messages))) / 2.0
                scored.append((score, i))

            # Descending score; ties broken by index (newer first)
            scored.sort(key=lambda x: (-x[0], -x[1]))

            out: List[ChatMemorySearchResult] = []
            for score, i in scored[:limit]:
                out.append(ChatMemorySearchResult(
                    message=_msg_to_dict(self._messages[i], self._metadata[i]),
                    score=score,
                    uuid=self._metadata[i].get('uuid', ''),
                ))
            return out

    def _semantic_only_search(self, query: str,
                              limit: int) -> List[ChatMemorySearchResult]:
        """Semantic fallback when no metadata filter applies.

        Uses SimpleMem when available; otherwise scans the buffer with
        the keyword-overlap heuristic so cold sessions still return
        something deterministic instead of an empty list.
        """
        # Try SimpleMem first — adaptive retrieval is more accurate when
        # the buffer is large.
        sm_results = self.semantic_search(query, max_results=limit)
        if sm_results:
            return [
                ChatMemorySearchResult(
                    message={
                        'content': r.get('content', ''),
                        'role': 'ai',  # SimpleMem returns synthesized
                                       # atomic facts attributed to AI
                        'created_at': '',
                        'metadata': {},
                    },
                    score=float(r.get('score', 1.0)),
                )
                for r in sm_results
            ]

        # SimpleMem unavailable / empty — fall back to keyword scan of
        # the persistent buffer.
        with self._lock:
            query_tokens = _tokenize_lower(query)
            if not query_tokens or not self._messages:
                return []
            scored = []
            for i, msg in enumerate(self._messages):
                score = _keyword_overlap_score(query_tokens, msg.content)
                if score > 0:
                    scored.append((score, i))
            scored.sort(key=lambda x: (-x[0], -x[1]))
            return [
                ChatMemorySearchResult(
                    message=_msg_to_dict(self._messages[i], self._metadata[i]),
                    score=score,
                    uuid=self._metadata[i].get('uuid', ''),
                )
                for score, i in scored[:limit]
            ]

    # ── Token-aware pruning ──

    def prune(self, max_tokens: int) -> int:
        """Drop oldest messages until total token estimate is under
        ``max_tokens``.  Returns the number of messages removed.

        ConversationTokenBufferMemory parity — used when the chat
        context budget is fixed (e.g., the 12k-token llama context
        ceiling).  Cheap O(n) sum since the buffer is small (typically
        <100 messages).
        """
        with self._lock:
            removed = 0
            total = sum(
                _approx_token_count(m.content) for m in self._messages)
            while total > max_tokens and len(self._messages) > 1:
                drop = self._messages.pop(0)
                self._metadata.pop(0)
                self._timestamps.pop(0)
                total -= _approx_token_count(drop.content)
                removed += 1
            if removed:
                self._rebuild_indexes_locked()
                self._schedule_flush()
            return removed


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
    # ConversationBufferMemory parity — prefixes for the formatted
    # ``buffer_as_str`` output (langchain default: "Human" / "AI").
    human_prefix: str = "Human"
    ai_prefix: str = "AI"
    # Optional per-instance token budget; when set, save_context() auto
    # -prunes after each turn so the buffer never exceeds it.  Set to 0
    # / None to disable.  Mirrors ConversationTokenBufferMemory.
    max_token_limit: int = 0

    class Config:
        arbitrary_types_allowed = True

    @property
    def memory_variables(self) -> List[str]:
        return [self.memory_key]

    def load_memory_variables(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Return recent messages — zero latency, pure in-memory.

        Honors ``return_messages``: True → list[BaseMessage] (autogen /
        modern langchain chains), False → formatted "Human: x\\nAI: y"
        string (legacy ConversationBufferMemory consumers).
        """
        msgs = self.chat_memory.messages
        window = msgs[-self.max_buffer_size:]
        if self.return_messages:
            return {self.memory_key: window}
        return {self.memory_key: self._format_messages(window)}

    # ── ConversationBufferMemory parity ──

    @property
    def buffer(self) -> Any:
        """Recent buffer.  Shape mirrors ``return_messages`` (messages
        when True, formatted string when False) — drop-in replacement
        for ``ConversationBufferMemory.buffer``.
        """
        return self.buffer_as_messages if self.return_messages else self.buffer_as_str

    @property
    def buffer_as_messages(self) -> List[BaseMessage]:
        """Recent messages as a list of BaseMessage."""
        msgs = self.chat_memory.messages
        return msgs[-self.max_buffer_size:]

    @property
    def buffer_as_str(self) -> str:
        """Recent messages formatted as a prefixed transcript."""
        return self._format_messages(self.buffer_as_messages)

    def _format_messages(self, messages: List[BaseMessage]) -> str:
        """Format messages with human/ai prefixes.  Uses langchain_core's
        canonical helper when available so the output is byte-identical
        to ConversationBufferMemory; falls back to a local impl when
        the helper has moved (defensive for future langchain releases).
        """
        if _lc_get_buffer_string is not None:
            try:
                return _lc_get_buffer_string(
                    messages,
                    human_prefix=self.human_prefix,
                    ai_prefix=self.ai_prefix,
                )
            except Exception:
                pass
        # Local fallback — same shape as get_buffer_string
        out = []
        for m in messages:
            if isinstance(m, HumanMessage):
                role = self.human_prefix
            elif isinstance(m, AIMessage):
                role = self.ai_prefix
            elif isinstance(m, SystemMessage):
                role = 'System'
            else:
                role = getattr(m, 'type', 'Human').capitalize()
            out.append(f'{role}: {m.content}')
        return '\n'.join(out)

    def prune(self, max_tokens: Optional[int] = None) -> int:
        """Drop oldest messages until under ``max_tokens``.  When called
        with no arg, uses ``self.max_token_limit``.  Returns the count
        of messages removed (0 if pruning is disabled).
        """
        budget = max_tokens if max_tokens is not None else self.max_token_limit
        if not budget or budget <= 0:
            return 0
        if isinstance(self.chat_memory, PersistentChatHistory):
            return self.chat_memory.prune(budget)
        return 0

    def clear(self) -> None:
        """Wipe all persisted messages.  Mirrors BaseChatMemory.clear()."""
        self.chat_memory.clear()

    def save_context(self, inputs: Dict[str, Any], outputs: Dict[str, str],
                     metadata: Optional[Dict[str, Any]] = None) -> None:
        """Save context with optional metadata for deterministic retrieval.

        Extends BaseChatMemory.save_context() to thread metadata through to
        the persistent buffer + SimpleMem, enabling search_by_metadata()
        lookups by request_Id, prompt_id, date range, or any custom key.
        Auto-prunes when ``max_token_limit`` is set.
        """
        input_str = inputs.get(self.input_key, next(iter(inputs.values()), ''))
        output_key = self.output_key or 'output'
        output_str = outputs.get(output_key, next(iter(outputs.values()), ''))
        meta = metadata or {}

        self.chat_memory.add_message(
            HumanMessage(content=str(input_str)), metadata=meta)
        self.chat_memory.add_message(
            AIMessage(content=str(output_str)), metadata=meta)

        # Auto-prune to token budget if configured.
        if self.max_token_limit and self.max_token_limit > 0:
            try:
                self.prune()
            except Exception as e:
                logger.debug(f'auto-prune failed (non-blocking): {e}')

    def search(self, query: str, metadata: Optional[Dict[str, Any]] = None,
               limit: int = 10) -> List[ChatMemorySearchResult]:
        """ZepMemory ``memory.search()`` parity — thin delegate to
        ``chat_memory.search()``.  Some callers reach for memory.search
        directly (rather than memory.chat_memory.search) — both work.
        """
        if isinstance(self.chat_memory, PersistentChatHistory):
            return self.chat_memory.search(query, metadata=metadata, limit=limit)
        return []

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
