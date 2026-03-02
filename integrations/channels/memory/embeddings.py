"""
Embedding Cache - Cache embeddings with TTL.

Provides persistent caching of text embeddings with configurable TTL,
batch operations, and optional Redis backend for distributed setups.
Designed for Docker environments with container-compatible storage.
"""

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


@dataclass
class EmbeddingConfig:
    """Configuration for the embedding cache."""

    # Storage settings
    db_path: Optional[str] = None
    redis_url: Optional[str] = None  # e.g., "redis://localhost:6379/0"

    # Cache settings
    ttl_days: int = 30
    max_entries: int = 100000

    # Embedding settings
    default_model: str = "default"
    embedding_dims: int = 384

    # Performance settings
    batch_size: int = 32
    cleanup_interval_hours: int = 24


@dataclass
class EmbeddingResult:
    """Result of an embedding lookup or computation."""

    text_hash: str
    embedding: List[float]
    model: str
    cached: bool = False
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "text_hash": self.text_hash,
            "embedding": self.embedding,
            "model": self.model,
            "cached": self.cached,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass
class CacheStats:
    """Statistics about the embedding cache."""

    total_entries: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    total_lookups: int = 0
    expired_entries: int = 0
    storage_bytes: int = 0
    oldest_entry: Optional[datetime] = None
    newest_entry: Optional[datetime] = None

    @property
    def hit_rate(self) -> float:
        """Calculate cache hit rate."""
        if self.total_lookups == 0:
            return 0.0
        return self.cache_hits / self.total_lookups

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "total_entries": self.total_entries,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "total_lookups": self.total_lookups,
            "hit_rate": self.hit_rate,
            "expired_entries": self.expired_entries,
            "storage_bytes": self.storage_bytes,
            "oldest_entry": self.oldest_entry.isoformat() if self.oldest_entry else None,
            "newest_entry": self.newest_entry.isoformat() if self.newest_entry else None,
        }


class RedisBackend:
    """Redis backend for distributed embedding cache."""

    def __init__(self, redis_url: str, ttl_days: int = 30, prefix: str = "emb:"):
        """
        Initialize Redis backend.

        Args:
            redis_url: Redis connection URL.
            ttl_days: TTL for cached embeddings.
            prefix: Key prefix for namespacing.
        """
        if not REDIS_AVAILABLE:
            raise ImportError("redis package is required for Redis backend")

        self.client = redis.from_url(redis_url)
        self.ttl_seconds = ttl_days * 24 * 3600
        self.prefix = prefix

    def _key(self, text_hash: str, model: str) -> str:
        """Generate Redis key."""
        return f"{self.prefix}{model}:{text_hash}"

    def get(self, text_hash: str, model: str) -> Optional[List[float]]:
        """Get cached embedding."""
        data = self.client.get(self._key(text_hash, model))
        if data:
            return json.loads(data)
        return None

    def set(self, text_hash: str, model: str, embedding: List[float]) -> None:
        """Set cached embedding."""
        self.client.setex(
            self._key(text_hash, model),
            self.ttl_seconds,
            json.dumps(embedding),
        )

    def delete(self, text_hash: str, model: str) -> bool:
        """Delete cached embedding."""
        return self.client.delete(self._key(text_hash, model)) > 0

    def exists(self, text_hash: str, model: str) -> bool:
        """Check if embedding exists."""
        return self.client.exists(self._key(text_hash, model)) > 0

    def get_batch(self, keys: List[Tuple[str, str]]) -> Dict[str, List[float]]:
        """Get multiple embeddings at once."""
        if not keys:
            return {}

        redis_keys = [self._key(h, m) for h, m in keys]
        values = self.client.mget(redis_keys)

        result = {}
        for (text_hash, model), value in zip(keys, values):
            if value:
                result[text_hash] = json.loads(value)
        return result

    def cleanup(self) -> int:
        """Redis handles TTL automatically, so this is a no-op."""
        return 0


class SQLiteBackend:
    """SQLite backend for local embedding cache."""

    # Default paths for Docker environments
    DEFAULT_DATA_DIR = "/app/data"
    DEFAULT_TEMP_DIR = "/tmp/embeddings"

    def __init__(self, db_path: Optional[str] = None, ttl_days: int = 30):
        """
        Initialize SQLite backend.

        Args:
            db_path: Path to SQLite database.
            ttl_days: TTL for cached embeddings.
        """
        if db_path:
            self.db_path = db_path
        else:
            # Use appropriate path for Docker vs local
            if os.path.exists("/tmp"):
                db_dir = self.DEFAULT_TEMP_DIR
            elif os.path.exists(self.DEFAULT_DATA_DIR):
                db_dir = os.path.join(self.DEFAULT_DATA_DIR, ".embeddings")
            else:
                db_dir = os.path.join(os.path.abspath("."), ".embeddings")

            os.makedirs(db_dir, exist_ok=True)
            self.db_path = os.path.join(db_dir, "embeddings.db")

        self.ttl_days = ttl_days
        self._lock = threading.RLock()
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

            # Enable WAL mode for better concurrency
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")

        return self._conn

    def _ensure_schema(self) -> None:
        """Create database schema."""
        conn = self._ensure_connection()
        with self._lock:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text_hash TEXT NOT NULL,
                    model TEXT NOT NULL,
                    embedding TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    access_count INTEGER DEFAULT 0,
                    last_accessed REAL,
                    UNIQUE(text_hash, model)
                )
            """)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_emb_hash ON embeddings(text_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_emb_model ON embeddings(model)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_emb_expires ON embeddings(expires_at)")

    def get(self, text_hash: str, model: str) -> Optional[List[float]]:
        """Get cached embedding."""
        conn = self._ensure_connection()
        now = time.time()

        with self._lock:
            row = conn.execute(
                """
                SELECT embedding FROM embeddings
                WHERE text_hash = ? AND model = ? AND expires_at > ?
                """,
                (text_hash, model, now)
            ).fetchone()

            if row:
                # Update access stats
                conn.execute(
                    """
                    UPDATE embeddings
                    SET access_count = access_count + 1, last_accessed = ?
                    WHERE text_hash = ? AND model = ?
                    """,
                    (now, text_hash, model)
                )
                return json.loads(row["embedding"])

        return None

    def set(self, text_hash: str, model: str, embedding: List[float]) -> None:
        """Set cached embedding."""
        conn = self._ensure_connection()
        now = time.time()
        expires = now + (self.ttl_days * 24 * 3600)

        with self._lock:
            conn.execute(
                """
                INSERT OR REPLACE INTO embeddings
                (text_hash, model, embedding, created_at, expires_at, last_accessed)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (text_hash, model, json.dumps(embedding), now, expires, now)
            )

    def delete(self, text_hash: str, model: str = None) -> bool:
        """Delete cached embedding."""
        conn = self._ensure_connection()
        with self._lock:
            if model:
                cursor = conn.execute(
                    "DELETE FROM embeddings WHERE text_hash = ? AND model = ?",
                    (text_hash, model)
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM embeddings WHERE text_hash = ?",
                    (text_hash,)
                )
            return cursor.rowcount > 0

    def exists(self, text_hash: str, model: str) -> bool:
        """Check if embedding exists."""
        conn = self._ensure_connection()
        now = time.time()
        with self._lock:
            row = conn.execute(
                """
                SELECT 1 FROM embeddings
                WHERE text_hash = ? AND model = ? AND expires_at > ?
                """,
                (text_hash, model, now)
            ).fetchone()
            return row is not None

    def get_batch(self, keys: List[Tuple[str, str]]) -> Dict[str, List[float]]:
        """Get multiple embeddings at once."""
        if not keys:
            return {}

        conn = self._ensure_connection()
        now = time.time()
        result = {}

        with self._lock:
            # Use OR conditions — SQLite doesn't support multi-column IN with params
            conditions = " OR ".join(["(text_hash = ? AND model = ?)"] * len(keys))
            params = []
            for text_hash, model in keys:
                params.extend([text_hash, model])
            params.append(now)

            rows = conn.execute(
                f"""
                SELECT text_hash, embedding FROM embeddings
                WHERE ({conditions}) AND expires_at > ?
                """,
                params
            ).fetchall()

            for row in rows:
                result[row["text_hash"]] = json.loads(row["embedding"])

        return result

    def cleanup(self, max_age_days: Optional[int] = None) -> int:
        """Remove expired entries."""
        conn = self._ensure_connection()
        now = time.time()

        with self._lock:
            if max_age_days is not None:
                cutoff = now - (max_age_days * 24 * 3600)
                cursor = conn.execute(
                    "DELETE FROM embeddings WHERE created_at < ?",
                    (cutoff,)
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM embeddings WHERE expires_at < ?",
                    (now,)
                )
            return cursor.rowcount

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        conn = self._ensure_connection()
        now = time.time()

        with self._lock:
            total = conn.execute("SELECT COUNT(*) as cnt FROM embeddings").fetchone()["cnt"]
            expired = conn.execute(
                "SELECT COUNT(*) as cnt FROM embeddings WHERE expires_at < ?",
                (now,)
            ).fetchone()["cnt"]

            dates = conn.execute(
                "SELECT MIN(created_at) as oldest, MAX(created_at) as newest FROM embeddings"
            ).fetchone()

            # Estimate storage size
            size_row = conn.execute(
                "SELECT SUM(LENGTH(embedding)) as total FROM embeddings"
            ).fetchone()
            storage = size_row["total"] or 0

        return {
            "total_entries": total,
            "expired_entries": expired,
            "storage_bytes": storage,
            "oldest_entry": datetime.fromtimestamp(dates["oldest"]) if dates["oldest"] else None,
            "newest_entry": datetime.fromtimestamp(dates["newest"]) if dates["newest"] else None,
        }

    def close(self) -> None:
        """Close database connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None


class EmbeddingCache:
    """
    Cache embeddings with TTL.

    Provides efficient caching of text embeddings with configurable storage
    backend (SQLite for local, Redis for distributed).

    Features:
    - Automatic TTL-based expiration
    - Batch operations for efficiency
    - Support for multiple embedding models
    - Optional Redis backend for distributed setups
    """

    def __init__(
        self,
        config: Optional[EmbeddingConfig] = None,
        embedding_fn: Optional[Callable[[str], List[float]]] = None,
        async_embedding_fn: Optional[Callable[[str], List[float]]] = None,
    ):
        """
        Initialize the embedding cache.

        Args:
            config: Cache configuration.
            embedding_fn: Synchronous function to compute embeddings.
            async_embedding_fn: Async function to compute embeddings.
        """
        self.config = config or EmbeddingConfig()
        self.embedding_fn = embedding_fn
        self.async_embedding_fn = async_embedding_fn

        # Initialize backend
        if self.config.redis_url and REDIS_AVAILABLE:
            self._backend = RedisBackend(
                self.config.redis_url,
                ttl_days=self.config.ttl_days,
            )
            self._backend_type = "redis"
        else:
            self._backend = SQLiteBackend(
                db_path=self.config.db_path,
                ttl_days=self.config.ttl_days,
            )
            self._backend_type = "sqlite"

        # Stats tracking
        self._lock = threading.RLock()
        self._stats = CacheStats()
        self._last_cleanup = time.time()

    @staticmethod
    def _compute_hash(text: str) -> str:
        """Compute SHA256 hash of text."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    async def get_embedding(
        self,
        text: str,
        model: Optional[str] = None,
    ) -> List[float]:
        """
        Get embedding for text, computing if not cached.

        Args:
            text: Text to get embedding for.
            model: Optional model identifier.

        Returns:
            Embedding vector as list of floats.

        Raises:
            ValueError: If no embedding function is configured.
        """
        model = model or self.config.default_model
        text_hash = self._compute_hash(text)

        # Check cache
        cached = self._backend.get(text_hash, model)
        with self._lock:
            self._stats.total_lookups += 1
            if cached:
                self._stats.cache_hits += 1
                return cached
            self._stats.cache_misses += 1

        # Compute embedding
        if self.async_embedding_fn:
            embedding = await self.async_embedding_fn(text)
        elif self.embedding_fn:
            embedding = self.embedding_fn(text)
        else:
            raise ValueError("No embedding function configured")

        # Store in cache
        self._backend.set(text_hash, model, embedding)

        # Periodic cleanup
        self._maybe_cleanup()

        return embedding

    async def get_batch(
        self,
        texts: List[str],
        model: Optional[str] = None,
    ) -> List[List[float]]:
        """
        Get embeddings for multiple texts.

        Args:
            texts: List of texts to get embeddings for.
            model: Optional model identifier.

        Returns:
            List of embedding vectors.
        """
        model = model or self.config.default_model

        # Compute hashes
        hashes = [self._compute_hash(text) for text in texts]

        # Batch lookup
        cached = self._backend.get_batch([(h, model) for h in hashes])

        with self._lock:
            self._stats.total_lookups += len(texts)
            self._stats.cache_hits += len(cached)
            self._stats.cache_misses += len(texts) - len(cached)

        # Find missing embeddings
        results: List[Optional[List[float]]] = [None] * len(texts)
        to_compute: List[Tuple[int, str]] = []

        for i, (text, text_hash) in enumerate(zip(texts, hashes)):
            if text_hash in cached:
                results[i] = cached[text_hash]
            else:
                to_compute.append((i, text))

        # Compute missing embeddings
        if to_compute:
            for i, text in to_compute:
                if self.async_embedding_fn:
                    embedding = await self.async_embedding_fn(text)
                elif self.embedding_fn:
                    embedding = self.embedding_fn(text)
                else:
                    raise ValueError("No embedding function configured")

                results[i] = embedding
                self._backend.set(hashes[i], model, embedding)

        # Periodic cleanup
        self._maybe_cleanup()

        return results  # type: ignore

    def get_embedding_sync(
        self,
        text: str,
        model: Optional[str] = None,
    ) -> List[float]:
        """
        Synchronous version of get_embedding.

        Args:
            text: Text to get embedding for.
            model: Optional model identifier.

        Returns:
            Embedding vector as list of floats.
        """
        model = model or self.config.default_model
        text_hash = self._compute_hash(text)

        # Check cache
        cached = self._backend.get(text_hash, model)
        with self._lock:
            self._stats.total_lookups += 1
            if cached:
                self._stats.cache_hits += 1
                return cached
            self._stats.cache_misses += 1

        # Compute embedding
        if not self.embedding_fn:
            raise ValueError("No synchronous embedding function configured")

        embedding = self.embedding_fn(text)

        # Store in cache
        self._backend.set(text_hash, model, embedding)

        return embedding

    def invalidate(self, text_hash: str) -> bool:
        """
        Invalidate a cached embedding by its hash.

        Args:
            text_hash: SHA256 hash of the text.

        Returns:
            True if entry was removed, False if not found.
        """
        return self._backend.delete(text_hash)

    def invalidate_text(self, text: str, model: Optional[str] = None) -> bool:
        """
        Invalidate a cached embedding by text content.

        Args:
            text: Original text.
            model: Optional model identifier.

        Returns:
            True if entry was removed, False if not found.
        """
        text_hash = self._compute_hash(text)
        model = model or self.config.default_model
        return self._backend.delete(text_hash, model)

    def cleanup(self, max_age_days: int = 30) -> int:
        """
        Remove expired or old cache entries.

        Args:
            max_age_days: Maximum age of entries to keep.

        Returns:
            Number of entries removed.
        """
        count = self._backend.cleanup(max_age_days)
        with self._lock:
            self._stats.expired_entries += count
            self._last_cleanup = time.time()
        return count

    def _maybe_cleanup(self) -> None:
        """Run cleanup if enough time has passed."""
        if time.time() - self._last_cleanup > (self.config.cleanup_interval_hours * 3600):
            # Run cleanup in background
            self._backend.cleanup()
            with self._lock:
                self._last_cleanup = time.time()

    def get_stats(self) -> CacheStats:
        """
        Get cache statistics.

        Returns:
            CacheStats object with current statistics.
        """
        if self._backend_type == "sqlite":
            backend_stats = self._backend.get_stats()
        else:
            backend_stats = {}

        with self._lock:
            stats = CacheStats(
                total_entries=backend_stats.get("total_entries", 0),
                cache_hits=self._stats.cache_hits,
                cache_misses=self._stats.cache_misses,
                total_lookups=self._stats.total_lookups,
                expired_entries=self._stats.expired_entries + backend_stats.get("expired_entries", 0),
                storage_bytes=backend_stats.get("storage_bytes", 0),
                oldest_entry=backend_stats.get("oldest_entry"),
                newest_entry=backend_stats.get("newest_entry"),
            )
        return stats

    def clear(self) -> int:
        """
        Clear all cached embeddings.

        Returns:
            Number of entries removed.
        """
        if self._backend_type == "sqlite":
            conn = self._backend._ensure_connection()
            with self._backend._lock:
                cursor = conn.execute("DELETE FROM embeddings")
                return cursor.rowcount
        else:
            # Redis: use scan to find and delete keys
            count = 0
            for key in self._backend.client.scan_iter(f"{self._backend.prefix}*"):
                self._backend.client.delete(key)
                count += 1
            return count

    def close(self) -> None:
        """Close the cache and release resources."""
        if self._backend_type == "sqlite":
            self._backend.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
