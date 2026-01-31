"""
Memory System for Phase 10 - File Tracking, Embeddings, and Search.

This module provides:
- FileTracker: Monitor and index file changes
- EmbeddingCache: Cache embeddings with TTL
- MemorySearch: Unified search across memory sources
- MemoryStore: SQLite FTS5 + embeddings storage
"""

from .memory_store import MemoryStore, MemoryItem, SearchResult
from .file_tracker import (
    FileTracker,
    FileChange,
    SyncResult,
    FileWatcher,
    WatchConfig,
)
from .embeddings import (
    EmbeddingCache,
    EmbeddingConfig,
    EmbeddingResult,
    CacheStats,
)
from .search import (
    MemorySearch,
    MemorySource,
    SearchResults,
    ContextResults,
    SearchConfig,
)

# SimpleMem (optional - requires simplemem package)
try:
    from .simplemem_store import SimpleMemStore, SimpleMemConfig
    from .simplemem_store import HAS_SIMPLEMEM
except ImportError:
    HAS_SIMPLEMEM = False

__all__ = [
    # Memory Store
    "MemoryStore",
    "MemoryItem",
    "SearchResult",
    # File Tracker
    "FileTracker",
    "FileChange",
    "SyncResult",
    "FileWatcher",
    "WatchConfig",
    # Embeddings
    "EmbeddingCache",
    "EmbeddingConfig",
    "EmbeddingResult",
    "CacheStats",
    # Search
    "MemorySearch",
    "MemorySource",
    "SearchResults",
    "ContextResults",
    "SearchConfig",
    # SimpleMem (optional)
    "SimpleMemStore",
    "SimpleMemConfig",
    "HAS_SIMPLEMEM",
]
