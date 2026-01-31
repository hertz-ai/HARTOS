"""
Message Deduplication System

Prevents duplicate messages from being processed multiple times.
Ported from HevolveBot's src/infra/dedupe.ts and src/auto-reply/reply/inbound-dedupe.ts.

Features:
- Multiple deduplication modes (ID, content hash, combined)
- TTL-based expiration
- Thread-safe operation
- Statistics tracking
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Dict, Any, Callable, TypeVar, Generic

logger = logging.getLogger(__name__)

T = TypeVar('T')


class DedupeMode(Enum):
    """Deduplication mode."""
    CONTENT_HASH = "content"    # Hash of message content
    MESSAGE_ID = "id"           # Platform message ID
    COMBINED = "combined"       # Both content + ID
    NONE = "none"               # No deduplication


@dataclass
class DedupeConfig:
    """Configuration for deduplication."""
    mode: DedupeMode = DedupeMode.COMBINED
    ttl_seconds: int = 300
    max_entries: int = 10000
    hash_algorithm: str = "sha256"


@dataclass
class DedupeStats:
    """Statistics for deduplicator."""
    total_checked: int = 0
    total_duplicates: int = 0
    total_unique: int = 0
    total_expired: int = 0
    current_entries: int = 0

    @property
    def duplicate_rate(self) -> float:
        """Calculate duplicate rate as percentage."""
        if self.total_checked == 0:
            return 0.0
        return (self.total_duplicates / self.total_checked) * 100


@dataclass
class DedupeEntry:
    """Entry in the deduplication cache."""
    hash_value: str
    message_id: Optional[str]
    content_hash: Optional[str]
    first_seen: datetime
    last_seen: datetime
    count: int = 1


class MessageDeduplicator(Generic[T]):
    """
    Deduplicates messages based on configurable criteria.

    Supports multiple modes:
    - CONTENT_HASH: Dedupe by hashing message content
    - MESSAGE_ID: Dedupe by platform message ID
    - COMBINED: Dedupe if either content or ID matches
    - NONE: No deduplication

    Usage:
        config = DedupeConfig(mode=DedupeMode.COMBINED, ttl_seconds=300)
        deduper = MessageDeduplicator(config)

        # Check if duplicate
        if deduper.is_duplicate(message, id_func=lambda m: m.id, content_func=lambda m: m.text):
            # Skip duplicate
            return

        # Mark as seen
        deduper.mark_seen(message, id_func=lambda m: m.id, content_func=lambda m: m.text)
    """

    def __init__(self, config: DedupeConfig):
        self.config = config
        self._entries: OrderedDict[str, DedupeEntry] = OrderedDict()
        self._id_to_hash: Dict[str, str] = {}
        self._content_to_hash: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._stats = DedupeStats()

    def _compute_hash(self, content: str) -> str:
        """Compute hash of content."""
        if self.config.hash_algorithm == "sha256":
            return hashlib.sha256(content.encode()).hexdigest()[:32]
        elif self.config.hash_algorithm == "md5":
            return hashlib.md5(content.encode()).hexdigest()
        else:
            return hashlib.sha256(content.encode()).hexdigest()[:32]

    def _compute_content_hash(self, content: str) -> str:
        """Compute normalized content hash."""
        # Normalize whitespace for more robust matching
        normalized = ' '.join(content.split())
        return self._compute_hash(normalized)

    def _is_expired(self, entry: DedupeEntry) -> bool:
        """Check if an entry is expired."""
        if self.config.ttl_seconds <= 0:
            return False
        age = (datetime.now() - entry.last_seen).total_seconds()
        return age > self.config.ttl_seconds

    def _cleanup_expired(self) -> int:
        """Remove expired entries."""
        if self.config.ttl_seconds <= 0:
            return 0

        expired_count = 0
        expired_hashes = []

        for hash_val, entry in list(self._entries.items()):
            if self._is_expired(entry):
                expired_hashes.append(hash_val)

        for hash_val in expired_hashes:
            entry = self._entries.pop(hash_val, None)
            if entry:
                expired_count += 1
                if entry.message_id and entry.message_id in self._id_to_hash:
                    del self._id_to_hash[entry.message_id]
                if entry.content_hash and entry.content_hash in self._content_to_hash:
                    del self._content_to_hash[entry.content_hash]

        self._stats.total_expired += expired_count
        return expired_count

    def _enforce_max_entries(self) -> int:
        """Remove oldest entries if over limit."""
        if len(self._entries) <= self.config.max_entries:
            return 0

        removed = 0
        while len(self._entries) > self.config.max_entries:
            # Remove oldest (first) entry
            hash_val, entry = self._entries.popitem(last=False)
            if entry.message_id and entry.message_id in self._id_to_hash:
                del self._id_to_hash[entry.message_id]
            if entry.content_hash and entry.content_hash in self._content_to_hash:
                del self._content_to_hash[entry.content_hash]
            removed += 1

        return removed

    def is_duplicate(
        self,
        item: T,
        message_id: Optional[str] = None,
        content: Optional[str] = None,
        id_func: Optional[Callable[[T], Optional[str]]] = None,
        content_func: Optional[Callable[[T], Optional[str]]] = None,
    ) -> bool:
        """
        Check if a message is a duplicate.

        Args:
            item: The message item
            message_id: Optional direct message ID
            content: Optional direct content
            id_func: Function to extract message ID from item
            content_func: Function to extract content from item

        Returns:
            True if duplicate, False if new
        """
        if self.config.mode == DedupeMode.NONE:
            return False

        # Extract ID and content
        msg_id = message_id
        if msg_id is None and id_func is not None:
            msg_id = id_func(item)

        msg_content = content
        if msg_content is None and content_func is not None:
            msg_content = content_func(item)

        with self._lock:
            self._stats.total_checked += 1

            # Cleanup expired
            self._cleanup_expired()

            # Check by message ID
            if self.config.mode in (DedupeMode.MESSAGE_ID, DedupeMode.COMBINED):
                if msg_id and msg_id in self._id_to_hash:
                    hash_val = self._id_to_hash[msg_id]
                    if hash_val in self._entries:
                        entry = self._entries[hash_val]
                        if not self._is_expired(entry):
                            entry.last_seen = datetime.now()
                            entry.count += 1
                            # Move to end (most recently used)
                            self._entries.move_to_end(hash_val)
                            self._stats.total_duplicates += 1
                            return True

            # Check by content hash
            if self.config.mode in (DedupeMode.CONTENT_HASH, DedupeMode.COMBINED):
                if msg_content:
                    content_hash = self._compute_content_hash(msg_content)
                    if content_hash in self._content_to_hash:
                        hash_val = self._content_to_hash[content_hash]
                        if hash_val in self._entries:
                            entry = self._entries[hash_val]
                            if not self._is_expired(entry):
                                entry.last_seen = datetime.now()
                                entry.count += 1
                                self._entries.move_to_end(hash_val)
                                self._stats.total_duplicates += 1
                                return True

            self._stats.total_unique += 1
            return False

    def mark_seen(
        self,
        item: T,
        message_id: Optional[str] = None,
        content: Optional[str] = None,
        id_func: Optional[Callable[[T], Optional[str]]] = None,
        content_func: Optional[Callable[[T], Optional[str]]] = None,
    ) -> str:
        """
        Mark a message as seen.

        Args:
            item: The message item
            message_id: Optional direct message ID
            content: Optional direct content
            id_func: Function to extract message ID from item
            content_func: Function to extract content from item

        Returns:
            The hash key for this message
        """
        if self.config.mode == DedupeMode.NONE:
            return ""

        # Extract ID and content
        msg_id = message_id
        if msg_id is None and id_func is not None:
            msg_id = id_func(item)

        msg_content = content
        if msg_content is None and content_func is not None:
            msg_content = content_func(item)

        content_hash = None
        if msg_content:
            content_hash = self._compute_content_hash(msg_content)

        # Create combined hash
        hash_parts = []
        if msg_id:
            hash_parts.append(f"id:{msg_id}")
        if content_hash:
            hash_parts.append(f"content:{content_hash}")

        if not hash_parts:
            return ""

        combined_hash = self._compute_hash("|".join(hash_parts))

        with self._lock:
            now = datetime.now()

            # Create or update entry
            if combined_hash in self._entries:
                entry = self._entries[combined_hash]
                entry.last_seen = now
                entry.count += 1
                self._entries.move_to_end(combined_hash)
            else:
                entry = DedupeEntry(
                    hash_value=combined_hash,
                    message_id=msg_id,
                    content_hash=content_hash,
                    first_seen=now,
                    last_seen=now,
                )
                self._entries[combined_hash] = entry

                # Update lookup indexes
                if msg_id:
                    self._id_to_hash[msg_id] = combined_hash
                if content_hash:
                    self._content_to_hash[content_hash] = combined_hash

            # Enforce max entries
            self._enforce_max_entries()

            return combined_hash

    def check_and_mark(
        self,
        item: T,
        message_id: Optional[str] = None,
        content: Optional[str] = None,
        id_func: Optional[Callable[[T], Optional[str]]] = None,
        content_func: Optional[Callable[[T], Optional[str]]] = None,
    ) -> bool:
        """
        Check if duplicate and mark as seen in one operation.

        Returns:
            True if duplicate (already seen), False if new (and now marked)
        """
        if self.is_duplicate(item, message_id, content, id_func, content_func):
            return True

        self.mark_seen(item, message_id, content, id_func, content_func)
        return False

    def cleanup_expired(self) -> int:
        """
        Manually trigger cleanup of expired entries.

        Returns:
            Number of entries removed
        """
        with self._lock:
            return self._cleanup_expired()

    def clear(self) -> int:
        """
        Clear all entries.

        Returns:
            Number of entries cleared
        """
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            self._id_to_hash.clear()
            self._content_to_hash.clear()
            self._stats.current_entries = 0
            return count

    def get_stats(self) -> DedupeStats:
        """Get deduplication statistics."""
        with self._lock:
            self._stats.current_entries = len(self._entries)
            return DedupeStats(
                total_checked=self._stats.total_checked,
                total_duplicates=self._stats.total_duplicates,
                total_unique=self._stats.total_unique,
                total_expired=self._stats.total_expired,
                current_entries=self._stats.current_entries,
            )

    def get_entry_count(self) -> int:
        """Get current number of entries."""
        with self._lock:
            return len(self._entries)


class SimpleDeduplicator:
    """
    Simple string-based deduplicator.

    For simpler use cases where you just have string keys.
    """

    def __init__(
        self,
        ttl_seconds: int = 300,
        max_entries: int = 10000,
    ):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._seen: OrderedDict[str, datetime] = OrderedDict()
        self._lock = threading.Lock()

    def is_duplicate(self, key: str) -> bool:
        """Check if key was seen recently."""
        with self._lock:
            self._cleanup()

            if key in self._seen:
                # Update timestamp
                self._seen[key] = datetime.now()
                self._seen.move_to_end(key)
                return True

            return False

    def mark_seen(self, key: str) -> None:
        """Mark a key as seen."""
        with self._lock:
            self._seen[key] = datetime.now()
            self._seen.move_to_end(key)
            self._enforce_max()

    def check_and_mark(self, key: str) -> bool:
        """Check if duplicate and mark in one operation."""
        with self._lock:
            self._cleanup()

            if key in self._seen:
                self._seen[key] = datetime.now()
                self._seen.move_to_end(key)
                return True

            self._seen[key] = datetime.now()
            self._enforce_max()
            return False

    def _cleanup(self) -> None:
        """Remove expired entries."""
        if self.ttl_seconds <= 0:
            return

        cutoff = datetime.now() - timedelta(seconds=self.ttl_seconds)
        expired = [k for k, v in self._seen.items() if v < cutoff]
        for k in expired:
            del self._seen[k]

    def _enforce_max(self) -> None:
        """Remove oldest if over limit."""
        while len(self._seen) > self.max_entries:
            self._seen.popitem(last=False)

    def clear(self) -> int:
        """Clear all entries."""
        with self._lock:
            count = len(self._seen)
            self._seen.clear()
            return count

    def get_count(self) -> int:
        """Get current entry count."""
        with self._lock:
            return len(self._seen)
