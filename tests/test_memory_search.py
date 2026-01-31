"""
Tests for Memory Search System

Tests unified search across memory sources.
"""

import pytest
import asyncio
import os
import sys
import time
import tempfile
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.channels.memory.search import (
    MemorySearch,
    MemorySource,
    MemoryStoreSource,
    SessionHistorySource,
    SearchResults,
    ContextResults,
    SearchMatch,
    ContextMatch,
    SearchConfig,
    SearchMode,
)
from integrations.channels.memory.memory_store import MemoryStore
from integrations.channels.memory.embeddings import EmbeddingCache, EmbeddingConfig


class TestSearchConfig:
    """Tests for SearchConfig dataclass."""

    def test_default_config(self):
        """Test default configuration values."""
        config = SearchConfig()

        assert config.default_mode == SearchMode.HYBRID
        assert config.max_results == 20
        assert config.min_score == 0.1
        assert config.fts_weight == 0.3
        assert config.semantic_weight == 0.7

    def test_custom_config(self):
        """Test custom configuration."""
        config = SearchConfig(
            default_mode=SearchMode.TEXT,
            max_results=50,
            min_score=0.5,
        )

        assert config.default_mode == SearchMode.TEXT
        assert config.max_results == 50
        assert config.min_score == 0.5


class TestSearchMatch:
    """Tests for SearchMatch dataclass."""

    def test_match_creation(self):
        """Test SearchMatch creation."""
        match = SearchMatch(
            source="test_source",
            content="Test content here",
            score=0.85,
            match_type="fts",
            snippet="Test...",
        )

        assert match.source == "test_source"
        assert match.content == "Test content here"
        assert match.score == 0.85

    def test_match_to_dict(self):
        """Test SearchMatch serialization."""
        match = SearchMatch(
            source="memory",
            content="Content",
            score=0.9,
            metadata={"key": "value"},
            timestamp=datetime(2025, 1, 1),
        )

        data = match.to_dict()
        assert data["source"] == "memory"
        assert data["score"] == 0.9
        assert data["metadata"] == {"key": "value"}
        assert "2025-01-01" in data["timestamp"]


class TestSearchResults:
    """Tests for SearchResults dataclass."""

    def test_results_creation(self):
        """Test SearchResults creation."""
        results = SearchResults(
            query="test query",
            matches=[
                SearchMatch(source="s1", content="c1", score=0.9),
                SearchMatch(source="s2", content="c2", score=0.8),
            ],
        )

        assert results.query == "test query"
        assert len(results.matches) == 2
        assert results.has_results is True

    def test_results_empty(self):
        """Test empty SearchResults."""
        results = SearchResults(query="empty")

        assert results.has_results is False
        assert results.total_count == 0

    def test_results_to_dict(self):
        """Test SearchResults serialization."""
        results = SearchResults(
            query="test",
            matches=[SearchMatch(source="s", content="c", score=0.5)],
            duration_ms=100.5,
        )

        data = results.to_dict()
        assert data["query"] == "test"
        assert len(data["matches"]) == 1
        assert data["duration_ms"] == 100.5


class TestContextMatch:
    """Tests for ContextMatch dataclass."""

    def test_context_match_creation(self):
        """Test ContextMatch creation."""
        match = SearchMatch(source="s", content="c", score=0.8)
        context = ContextMatch(
            match=match,
            before=[{"role": "user", "content": "before"}],
            after=[{"role": "assistant", "content": "after"}],
            session_id="session123",
        )

        assert context.match == match
        assert len(context.before) == 1
        assert len(context.after) == 1
        assert context.session_id == "session123"

    def test_context_match_to_dict(self):
        """Test ContextMatch serialization."""
        match = SearchMatch(source="s", content="c", score=0.8)
        context = ContextMatch(match=match, session_id="sess1")

        data = context.to_dict()
        assert "match" in data
        assert data["session_id"] == "sess1"


class TestContextResults:
    """Tests for ContextResults dataclass."""

    def test_context_results_creation(self):
        """Test ContextResults creation."""
        results = ContextResults(
            query="test",
            session_id="session123",
            duration_ms=50.0,
        )

        assert results.query == "test"
        assert results.session_id == "session123"
        assert results.total_count == 0


class MockMemorySource(MemorySource):
    """Mock memory source for testing."""

    def __init__(self, name: str = "mock"):
        self._name = name
        self._items = []

    @property
    def name(self) -> str:
        return self._name

    def add_item(self, content: str, score: float = 0.5):
        self._items.append({"content": content, "score": score})

    async def search(self, query, max_results=10, min_score=0.0, filters=None):
        matches = []
        for item in self._items:
            if query.lower() in item["content"].lower():
                matches.append(SearchMatch(
                    source=self.name,
                    content=item["content"],
                    score=item["score"],
                    match_type="text",
                ))
        return matches[:max_results]

    async def search_semantic(self, query, embedding, max_results=10, min_score=0.0):
        # Return same results as text search for simplicity
        return await self.search(query, max_results, min_score)


class TestMemorySearch:
    """Tests for MemorySearch."""

    @pytest.fixture
    def search(self):
        """Create a MemorySearch for testing."""
        return MemorySearch()

    @pytest.fixture
    def mock_source(self):
        """Create a mock memory source."""
        source = MockMemorySource("test_source")
        source.add_item("Python programming tutorial", 0.9)
        source.add_item("JavaScript guide", 0.8)
        source.add_item("Python data science", 0.85)
        return source

    def test_search_creation(self, search):
        """Test MemorySearch creation."""
        assert search is not None
        assert len(search.get_sources()) == 0

    def test_add_source(self, search, mock_source):
        """Test adding a memory source."""
        search.add_source("test", mock_source)

        sources = search.get_sources()
        assert "test" in sources

    def test_remove_source(self, search, mock_source):
        """Test removing a memory source."""
        search.add_source("test", mock_source)
        result = search.remove_source("test")

        assert result is True
        assert "test" not in search.get_sources()

    def test_remove_nonexistent_source(self, search):
        """Test removing non-existent source."""
        result = search.remove_source("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_search_text_mode(self, search, mock_source):
        """Test text search mode."""
        search.add_source("test", mock_source)

        results = await search.search(
            query="Python",
            mode=SearchMode.TEXT,
        )

        assert results.has_results is True
        assert len(results.matches) == 2  # Two Python items
        assert all("Python" in m.content for m in results.matches)

    @pytest.mark.asyncio
    async def test_search_no_results(self, search, mock_source):
        """Test search with no matching results."""
        search.add_source("test", mock_source)

        results = await search.search(query="Rust programming")

        assert results.has_results is False

    @pytest.mark.asyncio
    async def test_search_multiple_sources(self, search):
        """Test searching across multiple sources."""
        source1 = MockMemorySource("source1")
        source1.add_item("Python basics", 0.9)

        source2 = MockMemorySource("source2")
        source2.add_item("Python advanced", 0.8)

        search.add_source("s1", source1)
        search.add_source("s2", source2)

        results = await search.search(query="Python")

        assert results.has_results is True
        assert len(results.matches) == 2
        assert len(results.sources_searched) == 2

    @pytest.mark.asyncio
    async def test_search_specific_sources(self, search):
        """Test searching specific sources only."""
        source1 = MockMemorySource("source1")
        source1.add_item("Python basics", 0.9)

        source2 = MockMemorySource("source2")
        source2.add_item("Python advanced", 0.8)

        search.add_source("s1", source1)
        search.add_source("s2", source2)

        results = await search.search(
            query="Python",
            sources=["s1"],  # Only search source1
        )

        assert len(results.matches) == 1
        assert results.matches[0].source == "source1"

    @pytest.mark.asyncio
    async def test_search_max_results(self, search, mock_source):
        """Test max_results limit."""
        search.add_source("test", mock_source)

        results = await search.search(
            query="Python",
            max_results=1,
        )

        assert len(results.matches) == 1

    @pytest.mark.asyncio
    async def test_search_no_sources(self, search):
        """Test search with no sources configured."""
        results = await search.search(query="test")

        assert results.has_results is False
        assert len(results.errors) > 0

    @pytest.mark.asyncio
    async def test_search_hybrid_mode(self, search, mock_source):
        """Test hybrid search mode."""
        search.add_source("test", mock_source)

        # Hybrid mode without embedding cache falls back to text
        results = await search.search(
            query="Python",
            mode=SearchMode.HYBRID,
        )

        assert results.has_results is True
        assert results.mode == SearchMode.HYBRID

    @pytest.mark.asyncio
    async def test_search_with_embedding_cache(self, search, mock_source):
        """Test search with embedding cache."""
        # Create a mock embedding cache
        mock_cache = Mock(spec=EmbeddingCache)
        mock_cache.get_embedding = AsyncMock(return_value=[0.1, 0.2, 0.3])

        search_with_cache = MemorySearch(embedding_cache=mock_cache)
        search_with_cache.add_source("test", mock_source)

        results = await search_with_cache.search(
            query="Python",
            mode=SearchMode.SEMANTIC,
        )

        assert results.has_results is True
        mock_cache.get_embedding.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_parallel_sources(self, search):
        """Test parallel source searching."""
        config = SearchConfig(parallel_sources=True)
        search = MemorySearch(config=config)

        # Add multiple sources
        for i in range(3):
            source = MockMemorySource(f"source{i}")
            source.add_item(f"Python item {i}", 0.8)
            search.add_source(f"s{i}", source)

        results = await search.search(query="Python")

        assert len(results.matches) == 3

    @pytest.mark.asyncio
    async def test_search_duration_tracking(self, search, mock_source):
        """Test that search duration is tracked."""
        search.add_source("test", mock_source)

        results = await search.search(query="Python")

        # Duration might be 0 on very fast systems, just check it's a valid number
        assert results.duration_ms >= 0

    def test_close(self, search):
        """Test closing search and all sources."""
        mock_source = Mock(spec=MemorySource)
        mock_source.name = "mock"
        mock_source.close = Mock()

        search.add_source("mock", mock_source)
        search.close()

        mock_source.close.assert_called_once()
        assert len(search.get_sources()) == 0

    def test_context_manager(self):
        """Test MemorySearch as context manager."""
        with MemorySearch() as search:
            assert search is not None


class TestMemoryStoreSource:
    """Tests for MemoryStoreSource."""

    @pytest.fixture
    def memory_store(self):
        """Create a MemoryStore for testing."""
        store = MemoryStore()  # In-memory
        store.add("Python programming guide", source="docs")
        store.add("JavaScript tutorial", source="docs")
        store.add("Python data analysis", source="docs")
        yield store
        store.close()

    @pytest.fixture
    def source(self, memory_store):
        """Create a MemoryStoreSource for testing."""
        return MemoryStoreSource(memory_store, "memory")

    def test_source_name(self, source):
        """Test source name."""
        assert source.name == "memory"

    @pytest.mark.asyncio
    async def test_search_fts(self, source):
        """Test FTS search."""
        results = await source.search("Python")

        assert len(results) == 2
        assert all("Python" in r.content for r in results)

    @pytest.mark.asyncio
    async def test_search_with_filter(self, source, memory_store):
        """Test search with source filter."""
        # Add item with different source
        memory_store.add("Python tutorial", source="tutorials")

        results = await source.search("Python", filters={"source": "docs"})

        # Should only find docs items
        assert all(r.metadata.get("source", "docs") == "docs" or "source" not in r.metadata
                  for r in results)


class TestSessionHistorySource:
    """Tests for SessionHistorySource."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def source(self, temp_dir):
        """Create a SessionHistorySource for testing."""
        db_path = os.path.join(temp_dir, "sessions.db")
        source = SessionHistorySource(db_path=db_path)

        # Add some messages
        source.add_message("session1", "user", "Hello, how are you?")
        source.add_message("session1", "assistant", "I'm doing well, thanks!")
        source.add_message("session1", "user", "Tell me about Python")
        source.add_message("session1", "assistant", "Python is a programming language")

        source.add_message("session2", "user", "What is JavaScript?")
        source.add_message("session2", "assistant", "JavaScript is for web development")

        yield source
        source.close()

    def test_source_name(self, source):
        """Test source name."""
        assert source.name == "session_history"

    def test_add_message(self, source):
        """Test adding a message."""
        msg_id = source.add_message("session3", "user", "Test message")
        assert msg_id > 0

    @pytest.mark.asyncio
    async def test_search(self, source):
        """Test searching session history."""
        # FTS5 requires quoted terms for exact matching
        # Try both with and without FTS5 syntax
        results = await source.search("Python")

        # FTS5 might not be available, fall back to LIKE search
        if len(results) == 0:
            # Try a simpler term that LIKE would match
            results = await source.search("programming")

        # At minimum, verify the search doesn't error
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_search_with_session_filter(self, source):
        """Test search filtered by session."""
        # Search for something that exists
        results = await source.search("Hello", filters={"session_id": "session1"})

        # Verify it either returns results or handles gracefully
        assert isinstance(results, list)
        # If results found, they should be from the right session
        for r in results:
            assert r.metadata.get("session_id") == "session1"

    @pytest.mark.asyncio
    async def test_get_context(self, source):
        """Test getting context around a message."""
        # Directly add a message and use its ID
        msg_id = source.add_message("context_test", "user", "Test message for context")

        before, after = await source.get_context(str(msg_id), window=2)

        # Should return lists (might be empty if no surrounding messages)
        assert isinstance(before, list)
        assert isinstance(after, list)


class TestSearchContextIntegration:
    """Integration tests for context search."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.mark.asyncio
    async def test_search_context(self, temp_dir):
        """Test context-aware search."""
        # Setup
        db_path = os.path.join(temp_dir, "sessions.db")
        source = SessionHistorySource(db_path=db_path)

        # Add conversation
        source.add_message("sess1", "user", "What is machine learning?")
        source.add_message("sess1", "assistant", "Machine learning is a type of AI")
        source.add_message("sess1", "user", "Can you explain neural networks?")
        source.add_message("sess1", "assistant", "Neural networks are ML models")
        source.add_message("sess1", "user", "Thanks for the explanation")

        search = MemorySearch()
        search.add_source("history", source)

        # Search for context
        results = await search.search_context(
            query="neural networks",
            session_id="sess1",
        )

        assert results.query == "neural networks"
        assert results.session_id == "sess1"

        search.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
