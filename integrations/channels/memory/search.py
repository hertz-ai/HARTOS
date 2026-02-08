"""
Memory Search - Unified search across memory sources.

Provides a unified interface for searching across multiple memory sources
including file content, embeddings, session history, and custom sources.
Designed for Docker environments with container-compatible storage.
"""

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from .memory_store import MemoryStore, MemoryItem, SearchResult
from .embeddings import EmbeddingCache, EmbeddingConfig


class SearchMode(Enum):
    """Search modes available."""
    TEXT = "text"           # Full-text search using FTS5
    SEMANTIC = "semantic"   # Vector similarity search
    HYBRID = "hybrid"       # Combined FTS + semantic
    EXACT = "exact"         # Exact string matching


@dataclass
class SearchConfig:
    """Configuration for memory search."""

    # Search settings
    default_mode: SearchMode = SearchMode.HYBRID
    max_results: int = 20
    min_score: float = 0.1

    # Hybrid search weights
    fts_weight: float = 0.3
    semantic_weight: float = 0.7

    # Context search settings
    context_window: int = 5  # Messages before/after match
    include_metadata: bool = True

    # Performance settings
    timeout_seconds: float = 30.0
    parallel_sources: bool = True


@dataclass
class SearchMatch:
    """A single search match."""

    source: str
    content: str
    score: float
    match_type: str = "text"
    snippet: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: Optional[datetime] = None
    item_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "source": self.source,
            "content": self.content,
            "score": self.score,
            "match_type": self.match_type,
            "snippet": self.snippet,
            "metadata": self.metadata,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "item_id": self.item_id,
        }


@dataclass
class SearchResults:
    """Results from a search operation."""

    query: str
    matches: List[SearchMatch] = field(default_factory=list)
    total_count: int = 0
    sources_searched: List[str] = field(default_factory=list)
    duration_ms: float = 0.0
    mode: SearchMode = SearchMode.HYBRID
    errors: List[str] = field(default_factory=list)

    @property
    def has_results(self) -> bool:
        """Check if there are any results."""
        return len(self.matches) > 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "query": self.query,
            "matches": [m.to_dict() for m in self.matches],
            "total_count": self.total_count,
            "sources_searched": self.sources_searched,
            "duration_ms": self.duration_ms,
            "mode": self.mode.value,
            "errors": self.errors,
        }


@dataclass
class ContextMatch:
    """A match with surrounding context."""

    match: SearchMatch
    before: List[Dict[str, Any]] = field(default_factory=list)
    after: List[Dict[str, Any]] = field(default_factory=list)
    session_id: str = ""
    position: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "match": self.match.to_dict(),
            "before": self.before,
            "after": self.after,
            "session_id": self.session_id,
            "position": self.position,
        }


@dataclass
class ContextResults:
    """Results from a context-aware search."""

    query: str
    session_id: str
    context_matches: List[ContextMatch] = field(default_factory=list)
    total_count: int = 0
    duration_ms: float = 0.0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "query": self.query,
            "session_id": self.session_id,
            "context_matches": [cm.to_dict() for cm in self.context_matches],
            "total_count": self.total_count,
            "duration_ms": self.duration_ms,
            "errors": self.errors,
        }


class MemorySource(ABC):
    """
    Abstract base class for memory sources.

    Implement this to add custom searchable memory sources.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this source."""
        pass

    @abstractmethod
    async def search(
        self,
        query: str,
        max_results: int = 10,
        min_score: float = 0.0,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[SearchMatch]:
        """
        Search this memory source.

        Args:
            query: Search query.
            max_results: Maximum results to return.
            min_score: Minimum score threshold.
            filters: Optional filters (source-specific).

        Returns:
            List of SearchMatch objects.
        """
        pass

    @abstractmethod
    async def search_semantic(
        self,
        query: str,
        embedding: List[float],
        max_results: int = 10,
        min_score: float = 0.0,
    ) -> List[SearchMatch]:
        """
        Perform semantic search using embeddings.

        Args:
            query: Original query text.
            embedding: Query embedding vector.
            max_results: Maximum results to return.
            min_score: Minimum similarity threshold.

        Returns:
            List of SearchMatch objects.
        """
        pass

    async def get_context(
        self,
        item_id: str,
        window: int = 5,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Get context around an item.

        Args:
            item_id: Item identifier.
            window: Number of items before/after.

        Returns:
            Tuple of (before, after) context lists.
        """
        return [], []


class MemoryStoreSource(MemorySource):
    """Memory source backed by MemoryStore."""

    def __init__(self, store: MemoryStore, source_name: str = "memory_store"):
        """
        Initialize with a MemoryStore.

        Args:
            store: MemoryStore instance.
            source_name: Name for this source.
        """
        self._store = store
        self._source_name = source_name

    @property
    def name(self) -> str:
        return self._source_name

    async def search(
        self,
        query: str,
        max_results: int = 10,
        min_score: float = 0.0,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[SearchMatch]:
        """Search using FTS5."""
        source_filter = filters.get("source") if filters else None
        results = self._store.search(
            query=query,
            max_results=max_results,
            min_score=min_score,
            source_filter=source_filter,
        )

        return [
            SearchMatch(
                source=self.name,
                content=r.item.content,
                score=r.score,
                match_type="fts",
                snippet=r.snippet,
                metadata=r.item.metadata,
                timestamp=datetime.fromtimestamp(r.item.created_at),
                item_id=r.item.id,
            )
            for r in results
        ]

    async def search_semantic(
        self,
        query: str,
        embedding: List[float],
        max_results: int = 10,
        min_score: float = 0.0,
    ) -> List[SearchMatch]:
        """Search using embeddings."""
        results = self._store.search_semantic(
            query=query,
            max_results=max_results,
            min_score=min_score,
        )

        return [
            SearchMatch(
                source=self.name,
                content=r.item.content,
                score=r.score,
                match_type="semantic",
                snippet=r.snippet,
                metadata=r.item.metadata,
                timestamp=datetime.fromtimestamp(r.item.created_at),
                item_id=r.item.id,
            )
            for r in results
        ]


class MemoryGraphSource(MemorySource):
    """Memory source backed by MemoryGraph — adds backtrace context to search results."""

    def __init__(self, graph, source_name: str = "memory_graph"):
        self._graph = graph
        self._source_name = source_name

    @property
    def name(self) -> str:
        return self._source_name

    async def search(
        self,
        query: str,
        max_results: int = 10,
        min_score: float = 0.0,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[SearchMatch]:
        nodes = self._graph.recall(query, mode='text', top_k=max_results)
        return [
            SearchMatch(
                source=self.name,
                content=n.content,
                score=1.0,
                match_type="fts",
                snippet=n.content[:200],
                metadata={"memory_type": n.memory_type, "source_agent": n.source_agent, "session_id": n.session_id},
                timestamp=datetime.fromtimestamp(n.created_at),
                item_id=n.id,
            )
            for n in nodes
        ]

    async def search_semantic(
        self,
        query: str,
        embedding: List[float],
        max_results: int = 10,
        min_score: float = 0.0,
    ) -> List[SearchMatch]:
        nodes = self._graph.recall(query, mode='hybrid', top_k=max_results)
        return [
            SearchMatch(
                source=self.name,
                content=n.content,
                score=1.0,
                match_type="semantic",
                snippet=n.content[:200],
                metadata={"memory_type": n.memory_type, "source_agent": n.source_agent, "session_id": n.session_id},
                timestamp=datetime.fromtimestamp(n.created_at),
                item_id=n.id,
            )
            for n in nodes
        ]

    async def get_context(
        self,
        item_id: str,
        window: int = 5,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Use backtrace for 'before' context (parent chain) and children for 'after'."""
        chain_data = self._graph.get_memory_chain(item_id)
        if "error" in chain_data:
            return [], []

        before = [
            {"id": p["id"], "role": p.get("source_agent", ""), "content": p["content"], "timestamp": p.get("created_at", 0)}
            for p in chain_data.get("parents", [])[-window:]
        ]
        after = [
            {"id": c["id"], "role": c.get("source_agent", ""), "content": c["content"], "timestamp": c.get("created_at", 0)}
            for c in chain_data.get("children", [])[:window]
        ]
        return before, after


class SessionHistorySource(MemorySource):
    """Memory source for session/conversation history."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        source_name: str = "session_history",
    ):
        """
        Initialize session history source.

        Args:
            db_path: Path to SQLite database.
            source_name: Name for this source.
        """
        self._source_name = source_name
        self._lock = threading.RLock()

        # Determine database path
        if db_path:
            self.db_path = db_path
        else:
            if os.path.exists("/app/data"):
                db_dir = "/app/data"
            elif os.path.exists("/tmp"):
                db_dir = "/tmp/session_history"
            else:
                db_dir = os.path.join(os.path.abspath("."), ".session_history")

            os.makedirs(db_dir, exist_ok=True)
            self.db_path = os.path.join(db_dir, "session_history.db")

        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    def _ensure_connection(self) -> sqlite3.Connection:
        """Ensure database connection."""
        if self._conn is None:
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
            self._conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                isolation_level=None,
                timeout=30.0,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _ensure_schema(self) -> None:
        """Create database schema."""
        conn = self._ensure_connection()
        with self._lock:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    metadata TEXT DEFAULT '{}'
                )
            """)

            # Create FTS5 table
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                        content,
                        session_id UNINDEXED,
                        content=messages,
                        content_rowid=id
                    )
                """)
                self._fts_available = True
            except sqlite3.OperationalError:
                self._fts_available = False

            conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_time ON messages(timestamp)")

    @property
    def name(self) -> str:
        return self._source_name

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Add a message to session history.

        Args:
            session_id: Session identifier.
            role: Message role (user, assistant, system).
            content: Message content.
            metadata: Optional metadata.

        Returns:
            Message ID.
        """
        conn = self._ensure_connection()
        with self._lock:
            cursor = conn.execute(
                """
                INSERT INTO messages (session_id, role, content, timestamp, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, role, content, time.time(), json.dumps(metadata or {}))
            )
            return cursor.lastrowid

    async def search(
        self,
        query: str,
        max_results: int = 10,
        min_score: float = 0.0,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[SearchMatch]:
        """Search session history using FTS5."""
        conn = self._ensure_connection()
        session_filter = filters.get("session_id") if filters else None

        with self._lock:
            if self._fts_available:
                sql = """
                    SELECT m.*, bm25(messages_fts) as score,
                           snippet(messages_fts, 0, '<b>', '</b>', '...', 32) as snippet
                    FROM messages_fts f
                    JOIN messages m ON f.rowid = m.id
                    WHERE messages_fts MATCH ?
                """
                params: List[Any] = [query]

                if session_filter:
                    sql += " AND m.session_id = ?"
                    params.append(session_filter)

                sql += " ORDER BY score LIMIT ?"
                params.append(max_results)

                rows = conn.execute(sql, params).fetchall()
            else:
                sql = "SELECT *, 1.0 as score, '' as snippet FROM messages WHERE content LIKE ?"
                params = [f"%{query}%"]

                if session_filter:
                    sql += " AND session_id = ?"
                    params.append(session_filter)

                sql += " LIMIT ?"
                params.append(max_results)

                rows = conn.execute(sql, params).fetchall()

        return [
            SearchMatch(
                source=self.name,
                content=row["content"],
                score=abs(row["score"]) if row["score"] else 0.5,
                match_type="fts",
                snippet=row["snippet"] if row["snippet"] else row["content"][:200],
                metadata={
                    "session_id": row["session_id"],
                    "role": row["role"],
                    **json.loads(row["metadata"] or "{}"),
                },
                timestamp=datetime.fromtimestamp(row["timestamp"]),
                item_id=str(row["id"]),
            )
            for row in rows
        ]

    async def search_semantic(
        self,
        query: str,
        embedding: List[float],
        max_results: int = 10,
        min_score: float = 0.0,
    ) -> List[SearchMatch]:
        """Semantic search not directly supported; falls back to FTS."""
        return await self.search(query, max_results, min_score)

    async def get_context(
        self,
        item_id: str,
        window: int = 5,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Get messages before and after the specified message."""
        conn = self._ensure_connection()
        msg_id = int(item_id)

        with self._lock:
            # Get the message to find its session
            row = conn.execute(
                "SELECT session_id, timestamp FROM messages WHERE id = ?",
                (msg_id,)
            ).fetchone()

            if not row:
                return [], []

            session_id = row["session_id"]
            timestamp = row["timestamp"]

            # Get messages before
            before_rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE session_id = ? AND timestamp < ?
                ORDER BY timestamp DESC LIMIT ?
                """,
                (session_id, timestamp, window)
            ).fetchall()

            # Get messages after
            after_rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE session_id = ? AND timestamp > ?
                ORDER BY timestamp ASC LIMIT ?
                """,
                (session_id, timestamp, window)
            ).fetchall()

        before = [
            {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "timestamp": r["timestamp"],
            }
            for r in reversed(before_rows)
        ]

        after = [
            {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "timestamp": r["timestamp"],
            }
            for r in after_rows
        ]

        return before, after

    def close(self) -> None:
        """Close database connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None


class MemorySearch:
    """
    Unified search across memory sources.

    Provides a single interface for searching across multiple memory backends
    including file content, embeddings, session history, and custom sources.

    Features:
    - Multiple search modes (text, semantic, hybrid)
    - Pluggable memory sources
    - Context-aware search for sessions
    - Parallel source searching
    """

    def __init__(
        self,
        config: Optional[SearchConfig] = None,
        embedding_cache: Optional[EmbeddingCache] = None,
        enable_simplemem: Optional[bool] = None,
    ):
        """
        Initialize the memory search.

        Args:
            config: Search configuration.
            embedding_cache: Optional embedding cache for semantic search.
            enable_simplemem: Explicitly enable/disable SimpleMem. If None,
                uses SIMPLEMEM_ENABLED env var (default: false for auto-register).
        """
        self.config = config or SearchConfig()
        self.embedding_cache = embedding_cache

        self._lock = threading.RLock()
        self._sources: Dict[str, MemorySource] = {}

        # Auto-register SimpleMem if available and enabled
        should_enable = enable_simplemem
        if should_enable is None:
            should_enable = os.getenv("SIMPLEMEM_ENABLED", "false").lower() == "true"

        if should_enable:
            try:
                from .simplemem_store import SimpleMemStore, SimpleMemConfig
                simplemem_config = SimpleMemConfig.from_env()
                if simplemem_config.enabled and simplemem_config.api_key:
                    self.add_source("simplemem", SimpleMemStore(simplemem_config))
            except ImportError:
                pass
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    "Failed to auto-register SimpleMem: %s", e
                )

    def add_source(self, name: str, source: MemorySource) -> None:
        """
        Add a memory source.

        Args:
            name: Unique name for the source.
            source: MemorySource implementation.
        """
        with self._lock:
            self._sources[name] = source

    def remove_source(self, name: str) -> bool:
        """
        Remove a memory source.

        Args:
            name: Name of source to remove.

        Returns:
            True if source was removed, False if not found.
        """
        with self._lock:
            if name in self._sources:
                del self._sources[name]
                return True
            return False

    def get_sources(self) -> List[str]:
        """Get list of registered source names."""
        with self._lock:
            return list(self._sources.keys())

    async def search(
        self,
        query: str,
        sources: Optional[List[str]] = None,
        mode: Optional[SearchMode] = None,
        max_results: Optional[int] = None,
        min_score: Optional[float] = None,
        filters: Optional[Dict[str, Any]] = None,
    ) -> SearchResults:
        """
        Search across memory sources.

        Args:
            query: Search query.
            sources: Optional list of sources to search. Searches all if None.
            mode: Search mode (text, semantic, hybrid).
            max_results: Maximum results to return.
            min_score: Minimum score threshold.
            filters: Optional filters passed to sources.

        Returns:
            SearchResults object.
        """
        start_time = time.time()
        mode = mode or self.config.default_mode
        max_results = max_results or self.config.max_results
        min_score = min_score if min_score is not None else self.config.min_score

        results = SearchResults(query=query, mode=mode)

        # Determine which sources to search
        with self._lock:
            if sources:
                target_sources = {k: v for k, v in self._sources.items() if k in sources}
            else:
                target_sources = dict(self._sources)

        if not target_sources:
            results.errors.append("No sources available for search")
            return results

        results.sources_searched = list(target_sources.keys())

        # Get query embedding if needed
        query_embedding: Optional[List[float]] = None
        if mode in (SearchMode.SEMANTIC, SearchMode.HYBRID) and self.embedding_cache:
            try:
                query_embedding = await self.embedding_cache.get_embedding(query)
            except Exception as e:
                results.errors.append(f"Failed to get query embedding: {e}")
                if mode == SearchMode.SEMANTIC:
                    return results

        # Search each source
        all_matches: List[SearchMatch] = []

        if self.config.parallel_sources:
            # Search sources in parallel
            tasks = []
            for name, source in target_sources.items():
                tasks.append(self._search_source(
                    source, query, query_embedding, mode, max_results * 2, min_score, filters
                ))

            source_results = await asyncio.gather(*tasks, return_exceptions=True)

            for i, result in enumerate(source_results):
                if isinstance(result, Exception):
                    results.errors.append(f"Source error: {result}")
                else:
                    all_matches.extend(result)
        else:
            # Search sources sequentially
            for name, source in target_sources.items():
                try:
                    matches = await self._search_source(
                        source, query, query_embedding, mode, max_results * 2, min_score, filters
                    )
                    all_matches.extend(matches)
                except Exception as e:
                    results.errors.append(f"Source {name} error: {e}")

        # Sort by score and limit results
        all_matches.sort(key=lambda m: m.score, reverse=True)
        results.matches = all_matches[:max_results]
        results.total_count = len(all_matches)
        results.duration_ms = (time.time() - start_time) * 1000

        return results

    async def _search_source(
        self,
        source: MemorySource,
        query: str,
        embedding: Optional[List[float]],
        mode: SearchMode,
        max_results: int,
        min_score: float,
        filters: Optional[Dict[str, Any]],
    ) -> List[SearchMatch]:
        """Search a single source based on mode."""
        if mode == SearchMode.TEXT or mode == SearchMode.EXACT:
            return await source.search(query, max_results, min_score, filters)

        elif mode == SearchMode.SEMANTIC:
            if embedding:
                return await source.search_semantic(query, embedding, max_results, min_score)
            return []

        elif mode == SearchMode.HYBRID:
            # Get both FTS and semantic results
            fts_results = await source.search(query, max_results, min_score, filters)

            semantic_results = []
            if embedding:
                semantic_results = await source.search_semantic(query, embedding, max_results, min_score)

            # Merge results
            return self._merge_hybrid_results(
                fts_results,
                semantic_results,
                self.config.fts_weight,
                self.config.semantic_weight,
            )

        return []

    def _merge_hybrid_results(
        self,
        fts_results: List[SearchMatch],
        semantic_results: List[SearchMatch],
        fts_weight: float,
        semantic_weight: float,
    ) -> List[SearchMatch]:
        """Merge FTS and semantic results with weighted scores."""
        scores: Dict[str, Dict[str, Any]] = {}

        # Process FTS results
        for match in fts_results:
            key = f"{match.source}:{match.item_id or match.content[:50]}"
            scores[key] = {
                "match": match,
                "fts_score": match.score,
                "semantic_score": 0.0,
            }

        # Process semantic results
        for match in semantic_results:
            key = f"{match.source}:{match.item_id or match.content[:50]}"
            if key in scores:
                scores[key]["semantic_score"] = match.score
            else:
                scores[key] = {
                    "match": match,
                    "fts_score": 0.0,
                    "semantic_score": match.score,
                }

        # Compute combined scores
        merged = []
        for data in scores.values():
            combined = (fts_weight * data["fts_score"]) + (semantic_weight * data["semantic_score"])
            match = data["match"]
            match.score = combined
            match.match_type = "hybrid"
            merged.append(match)

        return merged

    async def search_context(
        self,
        query: str,
        session_id: str,
        sources: Optional[List[str]] = None,
        max_results: Optional[int] = None,
    ) -> ContextResults:
        """
        Search with surrounding context for a specific session.

        Args:
            query: Search query.
            session_id: Session to search within.
            sources: Optional list of sources to search.
            max_results: Maximum results to return.

        Returns:
            ContextResults with matches and surrounding context.
        """
        start_time = time.time()
        max_results = max_results or self.config.max_results

        results = ContextResults(query=query, session_id=session_id)

        # First do a regular search filtered by session
        filters = {"session_id": session_id}
        search_results = await self.search(
            query=query,
            sources=sources,
            max_results=max_results,
            filters=filters,
        )

        results.errors = search_results.errors

        # Get context for each match
        for match in search_results.matches:
            if not match.item_id:
                continue

            # Find the source
            source = self._sources.get(match.source)
            if not source:
                continue

            try:
                before, after = await source.get_context(
                    match.item_id,
                    window=self.config.context_window,
                )

                context_match = ContextMatch(
                    match=match,
                    before=before,
                    after=after,
                    session_id=session_id,
                )
                results.context_matches.append(context_match)

            except Exception as e:
                results.errors.append(f"Failed to get context: {e}")

        results.total_count = len(results.context_matches)
        results.duration_ms = (time.time() - start_time) * 1000

        return results

    def close(self) -> None:
        """Close all sources that support it."""
        with self._lock:
            for source in self._sources.values():
                if hasattr(source, "close"):
                    try:
                        source.close()
                    except Exception:
                        pass
            self._sources.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
