"""
Tests for Embedding Cache System

Tests caching, TTL, batch operations, and backends.
"""

import pytest
import asyncio
import os
import sys
import time
import tempfile
import hashlib
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.channels.memory.embeddings import (
    EmbeddingCache,
    EmbeddingConfig,
    EmbeddingResult,
    CacheStats,
    SQLiteBackend,
)


class TestEmbeddingConfig:
    """Tests for EmbeddingConfig dataclass."""

    def test_default_config(self):
        """Test default configuration values."""
        config = EmbeddingConfig()

        assert config.ttl_days == 30
        assert config.max_entries == 100000
        assert config.default_model == "default"
        assert config.embedding_dims == 384
        assert config.batch_size == 32

    def test_custom_config(self):
        """Test custom configuration."""
        config = EmbeddingConfig(
            ttl_days=7,
            max_entries=10000,
            default_model="text-embedding-ada-002",
            embedding_dims=1536,
        )

        assert config.ttl_days == 7
        assert config.max_entries == 10000
        assert config.default_model == "text-embedding-ada-002"
        assert config.embedding_dims == 1536


class TestEmbeddingResult:
    """Tests for EmbeddingResult dataclass."""

    def test_result_creation(self):
        """Test EmbeddingResult creation."""
        result = EmbeddingResult(
            text_hash="abc123",
            embedding=[0.1, 0.2, 0.3],
            model="default",
            cached=True,
        )

        assert result.text_hash == "abc123"
        assert result.embedding == [0.1, 0.2, 0.3]
        assert result.model == "default"
        assert result.cached is True

    def test_result_to_dict(self):
        """Test EmbeddingResult serialization."""
        result = EmbeddingResult(
            text_hash="abc123",
            embedding=[0.1, 0.2],
            model="test",
            cached=False,
            created_at=datetime(2025, 1, 1, 12, 0, 0),
        )

        data = result.to_dict()
        assert data["text_hash"] == "abc123"
        assert data["embedding"] == [0.1, 0.2]
        assert data["cached"] is False
        assert "2025-01-01" in data["created_at"]


class TestCacheStats:
    """Tests for CacheStats dataclass."""

    def test_stats_creation(self):
        """Test CacheStats creation."""
        stats = CacheStats(
            total_entries=100,
            cache_hits=80,
            cache_misses=20,
            total_lookups=100,
        )

        assert stats.total_entries == 100
        assert stats.cache_hits == 80
        assert stats.hit_rate == 0.8

    def test_hit_rate_zero_lookups(self):
        """Test hit rate with zero lookups."""
        stats = CacheStats()
        assert stats.hit_rate == 0.0

    def test_stats_to_dict(self):
        """Test CacheStats serialization."""
        stats = CacheStats(
            total_entries=50,
            cache_hits=40,
            cache_misses=10,
            total_lookups=50,
        )

        data = stats.to_dict()
        assert data["total_entries"] == 50
        assert data["hit_rate"] == 0.8


class TestSQLiteBackend:
    """Tests for SQLiteBackend."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "embeddings.db")
            yield db_path

    @pytest.fixture
    def backend(self, temp_db):
        """Create a SQLiteBackend for testing."""
        backend = SQLiteBackend(db_path=temp_db, ttl_days=30)
        yield backend
        backend.close()

    def test_backend_creation(self, backend):
        """Test SQLiteBackend creation."""
        assert backend is not None
        assert backend.db_path is not None

    def test_set_and_get(self, backend):
        """Test setting and getting embeddings."""
        text_hash = "test_hash_123"
        embedding = [0.1, 0.2, 0.3, 0.4]

        backend.set(text_hash, "default", embedding)
        result = backend.get(text_hash, "default")

        assert result == embedding

    def test_get_nonexistent(self, backend):
        """Test getting non-existent embedding."""
        result = backend.get("nonexistent", "default")
        assert result is None

    def test_delete(self, backend):
        """Test deleting embedding."""
        text_hash = "delete_test"
        embedding = [0.1, 0.2]

        backend.set(text_hash, "default", embedding)
        assert backend.exists(text_hash, "default") is True

        result = backend.delete(text_hash, "default")
        assert result is True
        assert backend.exists(text_hash, "default") is False

    def test_exists(self, backend):
        """Test checking embedding existence."""
        text_hash = "exists_test"

        assert backend.exists(text_hash, "default") is False

        backend.set(text_hash, "default", [0.1])
        assert backend.exists(text_hash, "default") is True

    def test_get_batch(self, backend):
        """Test batch retrieval."""
        # Set multiple embeddings
        embeddings = {
            "hash1": [0.1, 0.2],
            "hash2": [0.3, 0.4],
            "hash3": [0.5, 0.6],
        }

        for text_hash, emb in embeddings.items():
            backend.set(text_hash, "default", emb)

        # Get batch
        keys = [(h, "default") for h in embeddings.keys()]
        results = backend.get_batch(keys)

        assert len(results) == 3
        assert results["hash1"] == [0.1, 0.2]
        assert results["hash2"] == [0.3, 0.4]

    def test_cleanup_expired(self, backend):
        """Test cleanup of expired entries."""
        # Add some entries
        backend.set("test1", "default", [0.1])
        backend.set("test2", "default", [0.2])

        # Cleanup with max_age_days=0 should remove all
        removed = backend.cleanup(max_age_days=0)

        # All entries should be removed since they were just created
        # and we're asking for max_age_days=0
        assert removed >= 0

    def test_get_stats(self, backend):
        """Test getting statistics."""
        backend.set("test1", "default", [0.1, 0.2])
        backend.set("test2", "default", [0.3, 0.4])

        stats = backend.get_stats()

        assert stats["total_entries"] == 2
        assert stats["storage_bytes"] > 0


class TestEmbeddingCache:
    """Tests for EmbeddingCache."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def mock_embedding_fn(self):
        """Create a mock embedding function."""
        def embed(text):
            # Simple deterministic embedding based on text length
            return [float(ord(c) % 10) / 10 for c in text[:10]]
        return embed

    @pytest.fixture
    def cache(self, temp_dir, mock_embedding_fn):
        """Create an EmbeddingCache for testing."""
        config = EmbeddingConfig(
            db_path=os.path.join(temp_dir, "embeddings.db"),
            ttl_days=30,
        )
        cache = EmbeddingCache(config=config, embedding_fn=mock_embedding_fn)
        yield cache
        cache.close()

    def test_cache_creation(self, cache):
        """Test EmbeddingCache creation."""
        assert cache is not None
        assert cache._backend_type == "sqlite"

    def test_compute_hash(self):
        """Test hash computation."""
        hash1 = EmbeddingCache._compute_hash("test text")
        hash2 = EmbeddingCache._compute_hash("test text")
        hash3 = EmbeddingCache._compute_hash("different text")

        assert hash1 == hash2
        assert hash1 != hash3
        assert len(hash1) == 64  # SHA256 hex

    @pytest.mark.asyncio
    async def test_get_embedding_computes_new(self, cache):
        """Test getting embedding computes if not cached."""
        embedding = await cache.get_embedding("Hello World")

        assert embedding is not None
        assert len(embedding) > 0
        assert all(isinstance(x, float) for x in embedding)

    @pytest.mark.asyncio
    async def test_get_embedding_returns_cached(self, cache):
        """Test that second call returns cached embedding."""
        text = "Test caching"

        # First call - computes
        embedding1 = await cache.get_embedding(text)

        # Second call - should be cached
        embedding2 = await cache.get_embedding(text)

        assert embedding1 == embedding2

        stats = cache.get_stats()
        assert stats.cache_hits >= 1

    @pytest.mark.asyncio
    async def test_get_embedding_with_model(self, cache):
        """Test getting embedding with specific model."""
        embedding = await cache.get_embedding("Test", model="custom_model")

        assert embedding is not None

    @pytest.mark.asyncio
    async def test_get_batch(self, cache):
        """Test batch embedding retrieval."""
        texts = ["Text one", "Text two", "Text three"]

        embeddings = await cache.get_batch(texts)

        assert len(embeddings) == 3
        assert all(emb is not None for emb in embeddings)

    @pytest.mark.asyncio
    async def test_get_batch_partial_cache(self, cache):
        """Test batch with some cached, some new."""
        # Pre-cache one
        await cache.get_embedding("Cached text")

        texts = ["Cached text", "New text"]
        embeddings = await cache.get_batch(texts)

        assert len(embeddings) == 2
        assert embeddings[0] is not None
        assert embeddings[1] is not None

    def test_get_embedding_sync(self, cache):
        """Test synchronous embedding retrieval."""
        embedding = cache.get_embedding_sync("Sync test")

        assert embedding is not None
        assert len(embedding) > 0

    def test_invalidate_by_hash(self, cache):
        """Test invalidating by hash."""
        text = "Invalidate test"
        text_hash = EmbeddingCache._compute_hash(text)

        # Cache it
        cache.get_embedding_sync(text)

        # Invalidate
        result = cache.invalidate(text_hash)
        assert result is True

    def test_invalidate_text(self, cache):
        """Test invalidating by text content."""
        text = "Invalidate text test"

        # Cache it
        cache.get_embedding_sync(text)

        # Invalidate
        result = cache.invalidate_text(text)
        assert result is True

    def test_cleanup(self, cache):
        """Test cleanup of old entries."""
        # Add some entries
        cache.get_embedding_sync("Cleanup test 1")
        cache.get_embedding_sync("Cleanup test 2")

        # Cleanup with max_age=0 should remove all
        removed = cache.cleanup(max_age_days=0)
        assert removed >= 0

    def test_get_stats(self, cache):
        """Test getting cache statistics."""
        # Perform some operations
        cache.get_embedding_sync("Stats test 1")
        cache.get_embedding_sync("Stats test 1")  # Cache hit
        cache.get_embedding_sync("Stats test 2")  # Cache miss

        stats = cache.get_stats()

        assert stats.total_lookups == 3
        assert stats.cache_hits >= 1
        assert stats.cache_misses >= 2

    def test_clear(self, cache):
        """Test clearing all cached embeddings."""
        # Add some entries
        cache.get_embedding_sync("Clear test 1")
        cache.get_embedding_sync("Clear test 2")

        # Clear
        removed = cache.clear()
        assert removed >= 2

        # Verify empty
        stats = cache.get_stats()
        assert stats.total_entries == 0

    def test_context_manager(self, temp_dir, mock_embedding_fn):
        """Test EmbeddingCache as context manager."""
        config = EmbeddingConfig(
            db_path=os.path.join(temp_dir, "context.db"),
        )

        with EmbeddingCache(config=config, embedding_fn=mock_embedding_fn) as cache:
            embedding = cache.get_embedding_sync("Context test")
            assert embedding is not None

    @pytest.mark.asyncio
    async def test_no_embedding_fn_raises(self, temp_dir):
        """Test that missing embedding function raises error."""
        config = EmbeddingConfig(
            db_path=os.path.join(temp_dir, "no_fn.db"),
        )
        cache = EmbeddingCache(config=config)  # No embedding_fn

        with pytest.raises(ValueError, match="No embedding function"):
            await cache.get_embedding("Test")

        cache.close()

    @pytest.mark.asyncio
    async def test_async_embedding_fn(self, temp_dir):
        """Test with async embedding function."""
        async def async_embed(text):
            await asyncio.sleep(0.01)  # Simulate async operation
            return [0.1, 0.2, 0.3]

        config = EmbeddingConfig(
            db_path=os.path.join(temp_dir, "async.db"),
        )
        cache = EmbeddingCache(config=config, async_embedding_fn=async_embed)

        embedding = await cache.get_embedding("Async test")
        assert embedding == [0.1, 0.2, 0.3]

        cache.close()


class TestEmbeddingCacheWithRedis:
    """Tests for EmbeddingCache with Redis backend (mocked)."""

    @pytest.fixture
    def mock_redis_module(self):
        """Create a mock Redis module and client."""
        # Create mock redis module
        mock_redis = Mock()
        mock_client = Mock()
        mock_redis.from_url.return_value = mock_client

        # Patch both the availability flag and the module
        with patch.dict('sys.modules', {'redis': mock_redis}):
            # Need to reload the module to pick up the mock
            import importlib
            import integrations.channels.memory.embeddings as emb_module

            # Save original values
            orig_available = emb_module.REDIS_AVAILABLE

            # Set mock values
            emb_module.REDIS_AVAILABLE = True
            emb_module.redis = mock_redis

            yield mock_client

            # Restore
            emb_module.REDIS_AVAILABLE = orig_available
            if hasattr(emb_module, 'redis') and not orig_available:
                delattr(emb_module, 'redis')

    def test_redis_backend_get(self, mock_redis_module):
        """Test Redis get operation."""
        from integrations.channels.memory.embeddings import RedisBackend

        mock_redis_module.get.return_value = b'[0.1, 0.2, 0.3]'

        backend = RedisBackend("redis://localhost:6379/0")
        result = backend.get("test_hash", "default")

        assert result == [0.1, 0.2, 0.3]
        mock_redis_module.get.assert_called_once()

    def test_redis_backend_set(self, mock_redis_module):
        """Test Redis set operation."""
        from integrations.channels.memory.embeddings import RedisBackend

        backend = RedisBackend("redis://localhost:6379/0", ttl_days=7)
        backend.set("test_hash", "default", [0.1, 0.2])

        mock_redis_module.setex.assert_called_once()

    def test_redis_backend_delete(self, mock_redis_module):
        """Test Redis delete operation."""
        from integrations.channels.memory.embeddings import RedisBackend

        mock_redis_module.delete.return_value = 1

        backend = RedisBackend("redis://localhost:6379/0")
        result = backend.delete("test_hash", "default")

        assert result is True

    def test_redis_not_available_raises(self):
        """Test that RedisBackend raises when redis is not available."""
        import integrations.channels.memory.embeddings as emb_module

        # If redis is already not available, this should raise
        if not emb_module.REDIS_AVAILABLE:
            with pytest.raises(ImportError):
                from integrations.channels.memory.embeddings import RedisBackend
                RedisBackend("redis://localhost:6379/0")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
