"""
Memory Store using SQLite FTS5 + embeddings for semantic search.

Provides persistent storage with full-text search and vector similarity capabilities.
"""

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union


@dataclass
class MemoryItem:
    """Represents a single memory item stored in the memory store."""

    id: str
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[List[float]] = None
    source: str = "memory"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    hash: str = ""

    def __post_init__(self):
        if not self.hash:
            self.hash = self._compute_hash()

    def _compute_hash(self) -> str:
        """Compute SHA256 hash of content."""
        return hashlib.sha256(self.content.encode('utf-8')).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            'id': self.id,
            'content': self.content,
            'metadata': self.metadata,
            'embedding': self.embedding,
            'source': self.source,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'hash': self.hash,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MemoryItem':
        """Create from dictionary representation."""
        return cls(
            id=data['id'],
            content=data['content'],
            metadata=data.get('metadata', {}),
            embedding=data.get('embedding'),
            source=data.get('source', 'memory'),
            created_at=data.get('created_at', time.time()),
            updated_at=data.get('updated_at', time.time()),
            hash=data.get('hash', ''),
        )


@dataclass
class SearchResult:
    """Represents a search result from the memory store."""

    item: MemoryItem
    score: float
    match_type: str = "fts"  # "fts", "semantic", or "hybrid"
    snippet: str = ""


class MemoryStore:
    """
    Memory store with SQLite FTS5 for full-text search and embedding support
    for semantic search.

    Features:
    - Full-text search using SQLite FTS5
    - Semantic search using embeddings with cosine similarity
    - Hybrid search combining FTS5 and semantic scores
    - Thread-safe operations
    - Automatic schema management
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        embedding_fn: Optional[Callable[[str], List[float]]] = None,
        embedding_dims: int = 384,
    ):
        """
        Initialize the memory store.

        Args:
            db_path: Path to SQLite database file. Uses in-memory if None.
            embedding_fn: Optional function to compute embeddings.
            embedding_dims: Dimension of embedding vectors.
        """
        self.db_path = str(db_path) if db_path else ":memory:"
        self.embedding_fn = embedding_fn
        self.embedding_dims = embedding_dims
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_connection()
        self._ensure_schema()

    def _ensure_connection(self) -> sqlite3.Connection:
        """Ensure database connection is open."""
        if self._conn is None:
            self._conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                isolation_level=None,
            )
            self._conn.row_factory = sqlite3.Row
            # Enable FTS5 if available
            try:
                self._conn.execute("SELECT fts5()")
            except sqlite3.OperationalError:
                pass  # FTS5 not available, will use fallback
        return self._conn

    def _ensure_schema(self):
        """Create database schema if not exists."""
        conn = self._ensure_connection()
        with self._lock:
            # Main memory items table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_items (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    metadata TEXT DEFAULT '{}',
                    embedding TEXT,
                    source TEXT DEFAULT 'memory',
                    created_at REAL,
                    updated_at REAL,
                    hash TEXT
                )
            """)

            # Create FTS5 virtual table for full-text search
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                        content,
                        id UNINDEXED,
                        source UNINDEXED,
                        content=memory_items,
                        content_rowid=rowid
                    )
                """)
                self._fts_available = True
            except sqlite3.OperationalError:
                # FTS5 not available, create fallback table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS memory_fts_fallback (
                        rowid INTEGER PRIMARY KEY,
                        content TEXT,
                        id TEXT,
                        source TEXT
                    )
                """)
                self._fts_available = False

            # Create triggers for FTS sync
            if self._fts_available:
                conn.executescript("""
                    CREATE TRIGGER IF NOT EXISTS memory_items_ai AFTER INSERT ON memory_items BEGIN
                        INSERT INTO memory_fts(rowid, content, id, source)
                        VALUES (NEW.rowid, NEW.content, NEW.id, NEW.source);
                    END;

                    CREATE TRIGGER IF NOT EXISTS memory_items_ad AFTER DELETE ON memory_items BEGIN
                        INSERT INTO memory_fts(memory_fts, rowid, content, id, source)
                        VALUES('delete', OLD.rowid, OLD.content, OLD.id, OLD.source);
                    END;

                    CREATE TRIGGER IF NOT EXISTS memory_items_au AFTER UPDATE ON memory_items BEGIN
                        INSERT INTO memory_fts(memory_fts, rowid, content, id, source)
                        VALUES('delete', OLD.rowid, OLD.content, OLD.id, OLD.source);
                        INSERT INTO memory_fts(rowid, content, id, source)
                        VALUES (NEW.rowid, NEW.content, NEW.id, NEW.source);
                    END;
                """)

            # Create indexes
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_source ON memory_items(source)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_hash ON memory_items(hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_updated ON memory_items(updated_at)")

            # Schema version table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ('version', str(self.SCHEMA_VERSION))
            )

    def add(
        self,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        source: str = "memory",
        item_id: Optional[str] = None,
        compute_embedding: bool = True,
    ) -> MemoryItem:
        """
        Add a new memory item to the store.

        Args:
            content: The content to store.
            metadata: Optional metadata dictionary.
            source: Source identifier for the memory.
            item_id: Optional custom ID. Auto-generated if not provided.
            compute_embedding: Whether to compute embedding for semantic search.

        Returns:
            The created MemoryItem.
        """
        now = time.time()
        item_id = item_id or hashlib.sha256(
            f"{content}:{now}".encode('utf-8')
        ).hexdigest()[:16]

        embedding = None
        if compute_embedding and self.embedding_fn:
            try:
                embedding = self.embedding_fn(content)
            except Exception:
                pass

        item = MemoryItem(
            id=item_id,
            content=content,
            metadata=metadata or {},
            embedding=embedding,
            source=source,
            created_at=now,
            updated_at=now,
        )

        conn = self._ensure_connection()
        with self._lock:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_items
                (id, content, metadata, embedding, source, created_at, updated_at, hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.content,
                    json.dumps(item.metadata),
                    json.dumps(item.embedding) if item.embedding else None,
                    item.source,
                    item.created_at,
                    item.updated_at,
                    item.hash,
                )
            )

        # Broadcast memory addition to EventBus
        try:
            from core.platform.events import emit_event
            emit_event('memory.item_added', {
                'id': item.id, 'source': item.source,
                'content_length': len(item.content),
            })
        except Exception:
            pass

        return item

    def add_batch(
        self,
        items: List[Tuple[str, Optional[Dict[str, Any]]]],
        source: str = "memory",
        compute_embedding: bool = True,
    ) -> List[MemoryItem]:
        """
        Add multiple memory items in a batch.

        Args:
            items: List of (content, metadata) tuples.
            source: Source identifier for all items.
            compute_embedding: Whether to compute embeddings.

        Returns:
            List of created MemoryItems.
        """
        results = []
        conn = self._ensure_connection()

        with self._lock:
            conn.execute("BEGIN")
            try:
                for content, metadata in items:
                    item = self.add(
                        content=content,
                        metadata=metadata,
                        source=source,
                        compute_embedding=compute_embedding,
                    )
                    results.append(item)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return results

    def get(self, item_id: str) -> Optional[MemoryItem]:
        """
        Get a memory item by ID.

        Args:
            item_id: The item ID.

        Returns:
            The MemoryItem if found, None otherwise.
        """
        conn = self._ensure_connection()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM memory_items WHERE id = ?",
                (item_id,)
            ).fetchone()

        if not row:
            return None

        return self._row_to_item(row)

    def search(
        self,
        query: str,
        max_results: int = 10,
        min_score: float = 0.0,
        source_filter: Optional[str] = None,
    ) -> List[SearchResult]:
        """
        Search memories using FTS5 full-text search.

        Args:
            query: The search query.
            max_results: Maximum number of results.
            min_score: Minimum score threshold.
            source_filter: Optional source to filter by.

        Returns:
            List of SearchResult objects sorted by score.
        """
        if not query.strip():
            return []

        conn = self._ensure_connection()
        results = []

        with self._lock:
            if self._fts_available:
                # Build FTS5 query
                fts_query = self._build_fts_query(query)
                if not fts_query:
                    return []

                sql = """
                    SELECT m.*, bm25(memory_fts) as score,
                           snippet(memory_fts, 0, '<b>', '</b>', '...', 32) as snippet
                    FROM memory_fts f
                    JOIN memory_items m ON f.id = m.id
                    WHERE memory_fts MATCH ?
                """
                params: List[Any] = [fts_query]

                if source_filter:
                    sql += " AND m.source = ?"
                    params.append(source_filter)

                sql += " ORDER BY score LIMIT ?"
                params.append(max_results * 2)  # Get extra for filtering

                rows = conn.execute(sql, params).fetchall()
            else:
                # Fallback to LIKE search
                sql = """
                    SELECT *, 1.0 as score, '' as snippet
                    FROM memory_items
                    WHERE content LIKE ?
                """
                params = [f"%{query}%"]

                if source_filter:
                    sql += " AND source = ?"
                    params.append(source_filter)

                sql += " LIMIT ?"
                params.append(max_results * 2)

                rows = conn.execute(sql, params).fetchall()

        for row in rows:
            # Convert BM25 rank to score (higher is better)
            raw_score = abs(row['score']) if row['score'] else 0
            score = self._bm25_to_score(raw_score)

            if score >= min_score:
                item = self._row_to_item(row)
                results.append(SearchResult(
                    item=item,
                    score=score,
                    match_type="fts",
                    snippet=row['snippet'] if row['snippet'] else "",
                ))

        return sorted(results, key=lambda x: x.score, reverse=True)[:max_results]

    def search_semantic(
        self,
        query: str,
        max_results: int = 10,
        min_score: float = 0.0,
        source_filter: Optional[str] = None,
    ) -> List[SearchResult]:
        """
        Search memories using semantic similarity with embeddings.

        Args:
            query: The search query.
            max_results: Maximum number of results.
            min_score: Minimum similarity score threshold.
            source_filter: Optional source to filter by.

        Returns:
            List of SearchResult objects sorted by similarity score.
        """
        if not query.strip() or not self.embedding_fn:
            return []

        try:
            query_embedding = self.embedding_fn(query)
        except Exception:
            return []

        conn = self._ensure_connection()
        results = []

        with self._lock:
            sql = "SELECT * FROM memory_items WHERE embedding IS NOT NULL"
            params: List[Any] = []

            if source_filter:
                sql += " AND source = ?"
                params.append(source_filter)

            rows = conn.execute(sql, params).fetchall()

        for row in rows:
            item = self._row_to_item(row)
            if item.embedding:
                score = self._cosine_similarity(query_embedding, item.embedding)
                if score >= min_score:
                    snippet = item.content[:200] + "..." if len(item.content) > 200 else item.content
                    results.append(SearchResult(
                        item=item,
                        score=score,
                        match_type="semantic",
                        snippet=snippet,
                    ))

        return sorted(results, key=lambda x: x.score, reverse=True)[:max_results]

    def search_hybrid(
        self,
        query: str,
        max_results: int = 10,
        min_score: float = 0.0,
        source_filter: Optional[str] = None,
        fts_weight: float = 0.3,
        semantic_weight: float = 0.7,
    ) -> List[SearchResult]:
        """
        Hybrid search combining FTS5 and semantic search.

        Args:
            query: The search query.
            max_results: Maximum number of results.
            min_score: Minimum combined score threshold.
            source_filter: Optional source to filter by.
            fts_weight: Weight for FTS5 scores (0-1).
            semantic_weight: Weight for semantic scores (0-1).

        Returns:
            List of SearchResult objects sorted by combined score.
        """
        # Get results from both search methods
        candidates = max_results * 3
        fts_results = self.search(query, candidates, 0.0, source_filter)
        semantic_results = self.search_semantic(query, candidates, 0.0, source_filter)

        # Merge results
        scores_by_id: Dict[str, Dict[str, Any]] = {}

        for result in fts_results:
            scores_by_id[result.item.id] = {
                'item': result.item,
                'fts_score': result.score,
                'semantic_score': 0.0,
                'snippet': result.snippet,
            }

        for result in semantic_results:
            if result.item.id in scores_by_id:
                scores_by_id[result.item.id]['semantic_score'] = result.score
                if result.snippet and not scores_by_id[result.item.id]['snippet']:
                    scores_by_id[result.item.id]['snippet'] = result.snippet
            else:
                scores_by_id[result.item.id] = {
                    'item': result.item,
                    'fts_score': 0.0,
                    'semantic_score': result.score,
                    'snippet': result.snippet,
                }

        # Compute combined scores
        results = []
        for data in scores_by_id.values():
            combined_score = (
                fts_weight * data['fts_score'] +
                semantic_weight * data['semantic_score']
            )
            if combined_score >= min_score:
                results.append(SearchResult(
                    item=data['item'],
                    score=combined_score,
                    match_type="hybrid",
                    snippet=data['snippet'],
                ))

        return sorted(results, key=lambda x: x.score, reverse=True)[:max_results]

    def delete(self, item_id: str) -> bool:
        """
        Delete a memory item by ID.

        Args:
            item_id: The item ID to delete.

        Returns:
            True if item was deleted, False if not found.
        """
        conn = self._ensure_connection()
        with self._lock:
            cursor = conn.execute(
                "DELETE FROM memory_items WHERE id = ?",
                (item_id,)
            )
            deleted = cursor.rowcount > 0

        if deleted:
            try:
                from core.platform.events import emit_event
                emit_event('memory.item_deleted', {'id': item_id})
            except Exception:
                pass

        return deleted

    def delete_by_source(self, source: str) -> int:
        """
        Delete all memory items from a specific source.

        Args:
            source: The source identifier.

        Returns:
            Number of items deleted.
        """
        conn = self._ensure_connection()
        with self._lock:
            cursor = conn.execute(
                "DELETE FROM memory_items WHERE source = ?",
                (source,)
            )
            return cursor.rowcount

    def clear(self) -> int:
        """
        Clear all memory items from the store.

        Returns:
            Number of items deleted.
        """
        conn = self._ensure_connection()
        with self._lock:
            cursor = conn.execute("DELETE FROM memory_items")
            return cursor.rowcount

    def count(self, source: Optional[str] = None) -> int:
        """
        Count memory items in the store.

        Args:
            source: Optional source to filter by.

        Returns:
            Number of items.
        """
        conn = self._ensure_connection()
        with self._lock:
            if source:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM memory_items WHERE source = ?",
                    (source,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) as cnt FROM memory_items").fetchone()
            return row['cnt'] if row else 0

    def list_sources(self) -> List[str]:
        """
        List all unique sources in the store.

        Returns:
            List of source identifiers.
        """
        conn = self._ensure_connection()
        with self._lock:
            rows = conn.execute(
                "SELECT DISTINCT source FROM memory_items ORDER BY source"
            ).fetchall()
            return [row['source'] for row in rows]

    def update_embedding(self, item_id: str) -> bool:
        """
        Recompute and update the embedding for an item.

        Args:
            item_id: The item ID.

        Returns:
            True if updated, False if item not found or no embedding function.
        """
        if not self.embedding_fn:
            return False

        item = self.get(item_id)
        if not item:
            return False

        try:
            embedding = self.embedding_fn(item.content)
        except Exception:
            return False

        conn = self._ensure_connection()
        with self._lock:
            conn.execute(
                "UPDATE memory_items SET embedding = ?, updated_at = ? WHERE id = ?",
                (json.dumps(embedding), time.time(), item_id)
            )

        return True

    def close(self):
        """Close the database connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    def _row_to_item(self, row: sqlite3.Row) -> MemoryItem:
        """Convert a database row to a MemoryItem."""
        embedding = None
        if row['embedding']:
            try:
                embedding = json.loads(row['embedding'])
            except (json.JSONDecodeError, TypeError):
                pass

        metadata = {}
        if row['metadata']:
            try:
                metadata = json.loads(row['metadata'])
            except (json.JSONDecodeError, TypeError):
                pass

        return MemoryItem(
            id=row['id'],
            content=row['content'],
            metadata=metadata,
            embedding=embedding,
            source=row['source'] or 'memory',
            created_at=row['created_at'] or time.time(),
            updated_at=row['updated_at'] or time.time(),
            hash=row['hash'] or '',
        )

    def _build_fts_query(self, raw: str) -> Optional[str]:
        """Build an FTS5 query from raw input."""
        import re
        tokens = re.findall(r'[A-Za-z0-9_]+', raw)
        if not tokens:
            return None
        # Quote each token and join with AND
        quoted = [f'"{t}"' for t in tokens if t]
        return " AND ".join(quoted)

    def _bm25_to_score(self, rank: float) -> float:
        """Convert BM25 rank to a 0-1 score (higher is better)."""
        # BM25 returns negative scores, more negative = better match
        normalized = max(0, rank) if rank >= 0 else abs(rank)
        return 1 / (1 + normalized) if normalized > 0 else 1.0

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """Compute cosine similarity between two vectors."""
        if not a or not b:
            return 0.0

        length = min(len(a), len(b))
        dot = sum(a[i] * b[i] for i in range(length))
        norm_a = sum(x * x for x in a[:length]) ** 0.5
        norm_b = sum(x * x for x in b[:length]) ** 0.5

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot / (norm_a * norm_b)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
