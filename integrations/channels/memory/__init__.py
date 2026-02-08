"""
Memory System — File Tracking, Embeddings, Search, and Memory Graph.

This module provides:
- MemoryStore: SQLite FTS5 + embeddings storage
- MemoryGraph: Provenance-aware memory graph with backtrace
- Agent Memory Tools: Framework-agnostic tools + adapters (autogen, LangChain)
- FileTracker: Monitor and index file changes
- EmbeddingCache: Cache embeddings with TTL
- MemorySearch: Unified search across memory sources
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
    MemoryGraphSource,
    SearchResults,
    ContextResults,
    SearchConfig,
)

# Memory Graph (provenance + backtrace)
from .memory_graph import MemoryGraph, MemoryNode
from .agent_memory_tools import (
    create_memory_tools,
    register_autogen_tools,
    create_langchain_tools,
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
    "MemoryGraphSource",
    "SearchResults",
    "ContextResults",
    "SearchConfig",
    # Memory Graph
    "MemoryGraph",
    "MemoryNode",
    "create_memory_tools",
    "register_autogen_tools",
    "create_langchain_tools",
    # SimpleMem (optional)
    "SimpleMemStore",
    "SimpleMemConfig",
    "HAS_SIMPLEMEM",
]
