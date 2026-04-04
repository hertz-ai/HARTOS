"""
SimpleMem Store - Lifelong memory integration using SimpleMem.

Wraps the SimpleMem library to provide advanced memory capabilities:
- Semantic Structured Compression (atomic fact extraction)
- Structured Multi-View Indexing (LanceDB vectors)
- Complexity-Aware Adaptive Retrieval

Implements the MemorySource interface for seamless integration
with the existing MemorySearch system.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .search import MemorySource, SearchMatch

logger = logging.getLogger(__name__)

try:
    from simplemem import SimpleMemSystem
    HAS_SIMPLEMEM = True
except ImportError:
    HAS_SIMPLEMEM = False


@dataclass
class SimpleMemConfig:
    """Configuration for SimpleMem integration."""

    enabled: bool = True
    api_key: str = ""
    base_url: Optional[str] = None
    model: str = "gpt-4.1-mini"
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    db_path: str = ""  # Resolved at runtime via platform_paths
    window_size: int = 40
    overlap_size: int = 2
    parallel_workers: int = 8
    retrieval_workers: int = 4
    auto_finalize_interval: int = 40  # Finalize every N dialogues

    @classmethod
    def from_env(cls) -> "SimpleMemConfig":
        """Create configuration from environment variables."""
        return cls(
            enabled=os.getenv("SIMPLEMEM_ENABLED", "true").lower() == "true",
            api_key=os.getenv("SIMPLEMEM_API_KEY", os.getenv("OPENAI_API_KEY", "")),
            base_url=os.getenv("SIMPLEMEM_BASE_URL") or None,
            model=os.getenv("SIMPLEMEM_MODEL", "gpt-4.1-mini"),
            embedding_model=os.getenv(
                "SIMPLEMEM_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B"
            ),
            db_path=os.getenv("SIMPLEMEM_DB_PATH", ""),
            window_size=int(os.getenv("SIMPLEMEM_WINDOW_SIZE", "40")),
            overlap_size=int(os.getenv("SIMPLEMEM_OVERLAP_SIZE", "2")),
            parallel_workers=int(os.getenv("SIMPLEMEM_PARALLEL_WORKERS", "8")),
            retrieval_workers=int(os.getenv("SIMPLEMEM_RETRIEVAL_WORKERS", "4")),
            auto_finalize_interval=int(
                os.getenv("SIMPLEMEM_AUTO_FINALIZE_INTERVAL", "40")
            ),
        )


class SimpleMemStore(MemorySource):
    """
    SimpleMem wrapper implementing MemorySource interface.

    Provides lifelong memory with:
    - 43.24% F1 score on memory retrieval
    - 30x token efficiency through semantic compression
    - Three-stage pipeline: Compression -> Indexing -> Adaptive Retrieval
    """

    def __init__(self, config: Optional[SimpleMemConfig] = None):
        if not HAS_SIMPLEMEM:
            raise ImportError(
                "simplemem is required for SimpleMemStore. "
                "Install it with: pip install simplemem"
            )

        self._config = config or SimpleMemConfig.from_env()
        # Resolve db_path to a writable directory (not CWD which may be read-only)
        if not self._config.db_path:
            try:
                from core.platform_paths import get_simplemem_dir
                self._config.db_path = get_simplemem_dir()
            except ImportError:
                self._config.db_path = os.path.join('.', 'simplemem_db')
        self._dialogue_count = 0

        # Build kwargs for SimpleMemSystem
        system_kwargs: Dict[str, Any] = {
            "model": self._config.model,
            "embedding_model": self._config.embedding_model,
            "db_path": self._config.db_path,
        }

        if self._config.api_key:
            system_kwargs["api_key"] = self._config.api_key
        if self._config.base_url:
            system_kwargs["base_url"] = self._config.base_url

        self._system = SimpleMemSystem(**system_kwargs)

    @property
    def name(self) -> str:
        return "simplemem"

    async def add(
        self,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Add a dialogue entry to SimpleMem.

        Args:
            content: The message text.
            metadata: Dict with sender_name, timestamp, channel, chat_id, etc.

        Returns:
            A generated ID for the entry.
        """
        metadata = metadata or {}
        speaker = metadata.get("sender_name", "User")
        timestamp = metadata.get("timestamp", datetime.now().isoformat())

        self._system.add_dialogue(speaker, content, timestamp)
        self._dialogue_count += 1

        # Auto-finalize when buffer reaches window size
        if (
            self._config.auto_finalize_interval > 0
            and self._dialogue_count % self._config.auto_finalize_interval == 0
        ):
            logger.info(
                "Auto-finalizing SimpleMem after %d dialogues",
                self._dialogue_count,
            )
            await self.finalize()

        return str(uuid.uuid4())

    async def finalize(self) -> None:
        """
        Process buffered dialogues into compressed atomic memories.

        This triggers SimpleMem's compression pipeline:
        1. Windowed dialogue grouping
        2. Atomic fact extraction via LLM
        3. Vector indexing into LanceDB
        """
        try:
            self._system.finalize()
            logger.info("SimpleMem finalization complete")
        except Exception as e:
            logger.error("SimpleMem finalization failed: %s", e)

    async def search(
        self,
        query: str,
        max_results: int = 10,
        min_score: float = 0.0,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[SearchMatch]:
        """
        Search using SimpleMem's adaptive retrieval.

        SimpleMem uses complexity-aware retrieval that automatically
        adjusts search depth based on query complexity.

        Args:
            query: Search query.
            max_results: Maximum results to return.
            min_score: Minimum score threshold.
            filters: Optional filters (not used by SimpleMem).

        Returns:
            List of SearchMatch objects.
        """
        try:
            answer = self._system.ask(query)

            if not answer or answer.strip() == "":
                return []

            return [
                SearchMatch(
                    source=self.name,
                    content=answer,
                    score=1.0,
                    match_type="simplemem_adaptive",
                    snippet=answer[:200] if len(answer) > 200 else answer,
                    metadata={"query": query, "retrieval_type": "adaptive"},
                    timestamp=datetime.now(),
                    item_id=str(uuid.uuid4()),
                )
            ]
        except Exception as e:
            logger.error("SimpleMem search failed: %s", e)
            return []

    async def search_semantic(
        self,
        query: str,
        embedding: List[float],
        max_results: int = 10,
        min_score: float = 0.0,
    ) -> List[SearchMatch]:
        """
        Semantic search delegates to SimpleMem's built-in retrieval.

        SimpleMem already uses its own embedding model (Qwen3) internally,
        so we ignore the provided embedding and use the native pipeline.

        Args:
            query: Original query text.
            embedding: Query embedding (ignored - SimpleMem uses its own).
            max_results: Maximum results to return.
            min_score: Minimum similarity threshold.

        Returns:
            List of SearchMatch objects.
        """
        return await self.search(query, max_results, min_score)

    async def get_context(
        self,
        item_id: str,
        window: int = 5,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Get context around an item.

        SimpleMem's atomic facts don't have sequential context,
        so this returns empty lists.
        """
        return [], []

    @property
    def dialogue_count(self) -> int:
        """Get the number of dialogues added since last finalization."""
        return self._dialogue_count

    @property
    def config(self) -> SimpleMemConfig:
        """Get the current configuration."""
        return self._config
