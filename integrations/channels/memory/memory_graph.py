"""
Memory Graph — Framework-agnostic provenance-aware memory layer.

Builds on top of MemoryStore (SQLite FTS5 + embeddings) and adds:
- Provenance tracking via memory_links table (parent/child chains)
- Registration with auto-linking to recent memories
- Semantic and direct backtrace through memory chains
- Lifecycle event recording for agent status transitions
- Context-aware recall from recent conversation

Zero framework dependencies — works with autogen, LangChain, or any agent framework.
"""

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .memory_store import MemoryStore, MemoryItem, SearchResult

logger = logging.getLogger(__name__)


@dataclass
class MemoryNode:
    """A memory entry with provenance metadata."""

    id: str
    content: str
    memory_type: str = "fact"  # fact, conversation, decision, insight, lifecycle
    source_agent: str = ""  # Which agent/framework created this
    session_id: str = ""  # user_id + prompt_id (scoping key)
    user_id: str = ""
    parent_ids: List[str] = field(default_factory=list)
    context_snapshot: str = ""
    created_at: float = field(default_factory=time.time)
    accessed_at: float = field(default_factory=time.time)
    access_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "memory_type": self.memory_type,
            "source_agent": self.source_agent,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "parent_ids": self.parent_ids,
            "context_snapshot": self.context_snapshot,
            "created_at": self.created_at,
            "accessed_at": self.accessed_at,
            "access_count": self.access_count,
        }

    @classmethod
    def from_memory_item(cls, item: MemoryItem) -> "MemoryNode":
        """Create a MemoryNode from a MemoryStore MemoryItem."""
        meta = item.metadata or {}
        parent_ids_raw = meta.get("parent_ids", "[]")
        if isinstance(parent_ids_raw, str):
            try:
                parent_ids = json.loads(parent_ids_raw)
            except (json.JSONDecodeError, TypeError):
                parent_ids = []
        else:
            parent_ids = parent_ids_raw if isinstance(parent_ids_raw, list) else []

        return cls(
            id=item.id,
            content=item.content,
            memory_type=meta.get("memory_type", "fact"),
            source_agent=meta.get("source_agent", ""),
            session_id=meta.get("session_id", ""),
            user_id=meta.get("user_id", ""),
            parent_ids=parent_ids,
            context_snapshot=meta.get("context_snapshot", ""),
            created_at=item.created_at,
            accessed_at=meta.get("accessed_at", item.created_at),
            access_count=meta.get("access_count", 0),
        )


class MemoryGraph:
    """
    Framework-agnostic provenance-aware memory graph.

    Wraps MemoryStore for persistence and adds:
    - memory_links table for parent/child provenance chains
    - register_conversation() for auto-linked conversation turns
    - register_lifecycle() for agent status transitions
    - backtrace() for direct chain walking
    - backtrace_semantic() for semantic + chain walking
    """

    def __init__(
        self,
        db_path: str,
        user_id: str,
        embedding_fn=None,
    ):
        self._user_id = user_id
        self._db_path = db_path

        # Ensure directory exists
        Path(db_path).mkdir(parents=True, exist_ok=True)
        db_file = str(Path(db_path) / "memory_graph.db")

        self._store = MemoryStore(
            db_path=db_file,
            embedding_fn=embedding_fn,
        )
        self._init_links_table()

    def _init_links_table(self):
        """Create memory_links table and add provenance columns."""
        conn = self._store._ensure_connection()
        with self._store._lock:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_links (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    link_type TEXT DEFAULT 'derived',
                    context TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_links_source ON memory_links(source_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_links_target ON memory_links(target_id)"
            )

    # =========================================================================
    # Registration
    # =========================================================================

    def register(
        self,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        parent_ids: Optional[List[str]] = None,
        context_snapshot: str = "",
    ) -> str:
        """
        Store a memory with provenance. Returns memory_id.

        Args:
            content: The memory content.
            metadata: Dict with memory_type, source_agent, etc.
            parent_ids: IDs of memories that led to this one.
            context_snapshot: Summary of context when created.

        Returns:
            The generated memory ID.
        """
        memory_id = uuid.uuid4().hex[:16]
        metadata = metadata or {}
        parent_ids = parent_ids or []

        # Merge provenance into metadata for MemoryStore storage
        full_metadata = {
            **metadata,
            "memory_type": metadata.get("memory_type", "fact"),
            "source_agent": metadata.get("source_agent", ""),
            "session_id": metadata.get("session_id", ""),
            "user_id": self._user_id,
            "parent_ids": json.dumps(parent_ids),
            "context_snapshot": context_snapshot,
            "accessed_at": time.time(),
            "access_count": 0,
        }

        # Store in MemoryStore (gets FTS5 + optional embedding)
        self._store.add(
            content=content,
            metadata=full_metadata,
            source=metadata.get("memory_type", "fact"),
            item_id=memory_id,
        )

        # Insert provenance links
        if parent_ids:
            conn = self._store._ensure_connection()
            with self._store._lock:
                for pid in parent_ids:
                    link_id = uuid.uuid4().hex[:16]
                    conn.execute(
                        "INSERT OR IGNORE INTO memory_links (id, source_id, target_id, link_type, context) VALUES (?, ?, ?, ?, ?)",
                        (link_id, pid, memory_id, "derived", context_snapshot[:200]),
                    )

        logger.debug(f"Registered memory {memory_id}: {content[:50]}...")
        return memory_id

    def register_conversation(
        self,
        speaker: str,
        content: str,
        session_id: str,
    ) -> str:
        """
        Auto-register a conversation turn, linking to the previous turn.

        Args:
            speaker: Who said this (agent name, 'user', etc.)
            content: The message content.
            session_id: Session scope (e.g. user_id_prompt_id).

        Returns:
            Memory ID.
        """
        # Find the most recent conversation memory in this session
        recent = self._get_latest_session_memory(session_id)
        parent_ids = [recent.id] if recent else []

        return self.register(
            content=content,
            metadata={
                "memory_type": "conversation",
                "source_agent": speaker,
                "session_id": session_id,
            },
            parent_ids=parent_ids,
            context_snapshot=f"Conversation by {speaker} in session {session_id}",
        )

    def register_lifecycle(
        self,
        event: str,
        agent_id: str,
        session_id: str,
        details: str = "",
    ) -> str:
        """
        Record an agent lifecycle transition.

        Args:
            event: Lifecycle status (e.g. 'Creation Mode', 'Review Mode', 'completed').
            agent_id: The agent/user ID.
            session_id: Session scope.
            details: Additional details about the transition.

        Returns:
            Memory ID.
        """
        # Link to previous lifecycle event in this session
        recent = self._get_latest_session_memory(session_id, memory_type="lifecycle")
        parent_ids = [recent.id] if recent else []

        return self.register(
            content=f"[LIFECYCLE] {event}: {details}",
            metadata={
                "memory_type": "lifecycle",
                "source_agent": agent_id,
                "session_id": session_id,
                "lifecycle_event": event,
            },
            parent_ids=parent_ids,
            context_snapshot=f"Agent status: {event}",
        )

    # =========================================================================
    # Recall
    # =========================================================================

    def recall(
        self,
        query: str,
        mode: str = "hybrid",
        top_k: int = 5,
    ) -> List[MemoryNode]:
        """
        Search memories by text, semantic, or hybrid search.

        Args:
            query: Search query.
            mode: 'text', 'semantic', or 'hybrid'.
            top_k: Max results.

        Returns:
            List of MemoryNode results.
        """
        if mode == "text":
            results = self._store.search(query, max_results=top_k)
        elif mode == "semantic":
            results = self._store.search_semantic(query, max_results=top_k)
        else:
            results = self._store.search_hybrid(query, max_results=top_k)

        nodes = []
        for sr in results:
            node = MemoryNode.from_memory_item(sr.item)
            # Update access tracking
            self._update_access(node.id)
            node.access_count += 1
            node.accessed_at = time.time()
            nodes.append(node)

        return nodes

    def context_recall(
        self,
        recent_messages: List[str],
        top_k: int = 3,
    ) -> List[MemoryNode]:
        """
        Auto-recall: combine recent messages into a query and search.

        Args:
            recent_messages: List of recent message strings.
            top_k: Max results.

        Returns:
            List of relevant MemoryNodes.
        """
        if not recent_messages:
            return []

        # Combine recent messages into a single query
        combined = " ".join(msg[:200] for msg in recent_messages[-3:])
        if not combined.strip():
            return []

        return self.recall(combined, mode="hybrid", top_k=top_k)

    def get_session_memories(
        self,
        session_id: str,
        limit: int = 50,
    ) -> List[MemoryNode]:
        """Get all memories from a specific session, ordered by creation time."""
        conn = self._store._ensure_connection()
        with self._store._lock:
            rows = conn.execute(
                """
                SELECT * FROM memory_items
                WHERE json_extract(metadata, '$.session_id') = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()

        nodes = []
        for row in rows:
            item = self._store._row_to_item(row)
            nodes.append(MemoryNode.from_memory_item(item))
        return nodes

    # =========================================================================
    # Backtrace
    # =========================================================================

    def backtrace(self, memory_id: str, depth: int = 10) -> List[MemoryNode]:
        """
        Direct backtrace: walk parent links from memory_id back to origin.

        Returns ordered list: [origin, ..., intermediate, ..., target].
        """
        chain = []
        visited = set()
        current_id = memory_id

        for _ in range(depth):
            if current_id in visited:
                break
            visited.add(current_id)

            item = self._store.get(current_id)
            if not item:
                break

            node = MemoryNode.from_memory_item(item)
            chain.append(node)

            # Find parent via memory_links
            parent_id = self._get_parent_id(current_id)
            if not parent_id:
                break
            current_id = parent_id

        # Reverse so origin comes first
        chain.reverse()
        return chain

    def backtrace_semantic(
        self,
        query: str,
        depth: int = 5,
        top_k: int = 3,
    ) -> List[List[MemoryNode]]:
        """
        Semantic backtrace: find nearest memories, then trace each one back.

        Returns list of chains, one per matching memory.
        """
        matches = self.recall(query, mode="hybrid", top_k=top_k)
        chains = []

        for node in matches:
            chain = self.backtrace(node.id, depth=depth)
            if chain:
                chains.append(chain)

        return chains

    def get_memory_chain(self, memory_id: str) -> Dict[str, Any]:
        """
        Get full chain: parents -> this -> children.

        Returns tree structure with the target memory at center.
        """
        item = self._store.get(memory_id)
        if not item:
            return {"error": f"Memory {memory_id} not found"}

        node = MemoryNode.from_memory_item(item)

        # Walk parents
        parents = self.backtrace(memory_id)
        # Remove the target itself from parents list
        parents = [p for p in parents if p.id != memory_id]

        # Walk children
        children = self._get_children(memory_id)

        return {
            "target": node.to_dict(),
            "parents": [p.to_dict() for p in parents],
            "children": [c.to_dict() for c in children],
        }

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _get_parent_id(self, memory_id: str) -> Optional[str]:
        """Get the parent memory ID from memory_links."""
        conn = self._store._ensure_connection()
        with self._store._lock:
            row = conn.execute(
                "SELECT source_id FROM memory_links WHERE target_id = ? ORDER BY created_at DESC LIMIT 1",
                (memory_id,),
            ).fetchone()
        return row["source_id"] if row else None

    def _get_children(self, memory_id: str) -> List[MemoryNode]:
        """Get direct children of a memory."""
        conn = self._store._ensure_connection()
        with self._store._lock:
            rows = conn.execute(
                "SELECT target_id FROM memory_links WHERE source_id = ? ORDER BY created_at ASC",
                (memory_id,),
            ).fetchall()

        children = []
        for row in rows:
            item = self._store.get(row["target_id"])
            if item:
                children.append(MemoryNode.from_memory_item(item))
        return children

    def _get_latest_session_memory(
        self,
        session_id: str,
        memory_type: Optional[str] = None,
    ) -> Optional[MemoryNode]:
        """Get the most recent memory in a session."""
        conn = self._store._ensure_connection()
        with self._store._lock:
            if memory_type:
                row = conn.execute(
                    """
                    SELECT * FROM memory_items
                    WHERE json_extract(metadata, '$.session_id') = ?
                      AND json_extract(metadata, '$.memory_type') = ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id, memory_type),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM memory_items
                    WHERE json_extract(metadata, '$.session_id') = ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()

        if not row:
            return None
        item = self._store._row_to_item(row)
        return MemoryNode.from_memory_item(item)

    def _update_access(self, memory_id: str):
        """Update accessed_at and access_count for a memory."""
        conn = self._store._ensure_connection()
        with self._store._lock:
            try:
                row = conn.execute(
                    "SELECT metadata FROM memory_items WHERE id = ?",
                    (memory_id,),
                ).fetchone()
                if row and row["metadata"]:
                    meta = json.loads(row["metadata"])
                    meta["accessed_at"] = time.time()
                    meta["access_count"] = meta.get("access_count", 0) + 1
                    conn.execute(
                        "UPDATE memory_items SET metadata = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(meta), time.time(), memory_id),
                    )
            except Exception:
                pass  # Non-blocking

    def close(self):
        """Close the underlying MemoryStore connection."""
        self._store.close()
