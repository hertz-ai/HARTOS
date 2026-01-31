"""
Message Queue System

Provides queue management for message processing with multiple policies.
Ported from HevolveBot's src/auto-reply/reply/queue/.

Features:
- Multiple queue policies (DROP, LATEST, BACKLOG, PRIORITY, COLLECT)
- Drop policies (OLD, NEW, SUMMARIZE)
- Deduplication
- Per-channel/user queue management
- Message expiration
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
from typing import Optional, Dict, List, Any, Callable, Tuple

logger = logging.getLogger(__name__)


class QueuePolicy(Enum):
    """Queue processing policy."""
    DROP = "drop"           # Drop new messages when busy/at capacity
    LATEST = "latest"       # Keep only latest message, drop older
    BACKLOG = "backlog"     # Process all in order (FIFO)
    PRIORITY = "priority"   # Priority-based ordering
    COLLECT = "collect"     # Collect messages into batches


class DropPolicy(Enum):
    """Policy for handling messages when queue is at capacity."""
    OLD = "old"             # Drop oldest messages
    NEW = "new"             # Drop new incoming messages
    SUMMARIZE = "summarize" # Drop old but keep summary


class DedupeMode(Enum):
    """Deduplication mode for messages."""
    MESSAGE_ID = "message-id"   # Dedupe by platform message ID
    CONTENT = "content"         # Dedupe by content hash
    COMBINED = "combined"       # Both message ID and content
    NONE = "none"               # No deduplication


@dataclass
class QueuedMessage:
    """A message in the queue."""
    message_id: str
    channel: str
    chat_id: str
    sender_id: str
    content: str
    priority: int = 0
    enqueued_at: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    content_hash: str = field(default="")

    def __post_init__(self):
        if not self.content_hash:
            self.content_hash = hashlib.sha256(self.content.encode()).hexdigest()[:16]


@dataclass
class QueueConfig:
    """Configuration for a message queue."""
    policy: QueuePolicy = QueuePolicy.BACKLOG
    drop_policy: DropPolicy = DropPolicy.SUMMARIZE
    dedupe_mode: DedupeMode = DedupeMode.MESSAGE_ID
    max_size: int = 20
    max_age_seconds: int = 300
    debounce_ms: int = 1000
    priority_boost_mentions: bool = True
    priority_boost_replies: bool = True
    collect_batch_size: int = 10


@dataclass
class QueueStats:
    """Statistics for a queue."""
    total_enqueued: int = 0
    total_dequeued: int = 0
    total_dropped: int = 0
    total_expired: int = 0
    total_deduplicated: int = 0
    current_size: int = 0
    dropped_summaries: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_enqueued": self.total_enqueued,
            "total_dequeued": self.total_dequeued,
            "total_dropped": self.total_dropped,
            "total_expired": self.total_expired,
            "total_deduplicated": self.total_deduplicated,
            "current_size": self.current_size,
            "dropped_summaries": self.dropped_summaries[:10],  # Last 10
        }


class MessageQueue:
    """
    Message queue with configurable policies.

    Supports various queue modes ported from HevolveBot:
    - DROP: Reject new messages when at capacity
    - LATEST: Keep only the most recent message
    - BACKLOG: FIFO processing of all messages
    - PRIORITY: Process by priority order
    - COLLECT: Batch messages together

    Usage:
        config = QueueConfig(policy=QueuePolicy.BACKLOG, max_size=100)
        queue = MessageQueue(config)

        # Enqueue
        success = queue.enqueue(message)

        # Dequeue
        msg = queue.dequeue()

        # Get stats
        stats = queue.get_stats()
    """

    def __init__(self, config: QueueConfig):
        self.config = config
        self._items: List[QueuedMessage] = []
        self._seen_ids: OrderedDict[str, datetime] = OrderedDict()
        self._seen_hashes: OrderedDict[str, datetime] = OrderedDict()
        self._stats = QueueStats()
        self._lock = threading.Lock()
        self._last_enqueue_time: Optional[datetime] = None
        self._draining = False

    @property
    def size(self) -> int:
        """Current queue size."""
        return len(self._items)

    @property
    def is_empty(self) -> bool:
        """Check if queue is empty."""
        return len(self._items) == 0

    @property
    def is_full(self) -> bool:
        """Check if queue is at capacity."""
        return len(self._items) >= self.config.max_size

    def _is_duplicate(self, message: QueuedMessage) -> bool:
        """Check if message is a duplicate."""
        if self.config.dedupe_mode == DedupeMode.NONE:
            return False

        # Check by message ID
        if self.config.dedupe_mode in (DedupeMode.MESSAGE_ID, DedupeMode.COMBINED):
            if message.message_id and message.message_id in self._seen_ids:
                return True

        # Check by content hash
        if self.config.dedupe_mode in (DedupeMode.CONTENT, DedupeMode.COMBINED):
            if message.content_hash in self._seen_hashes:
                return True

        return False

    def _mark_seen(self, message: QueuedMessage) -> None:
        """Mark message as seen for deduplication."""
        now = datetime.now()

        if message.message_id:
            self._seen_ids[message.message_id] = now
            # Keep seen IDs bounded
            while len(self._seen_ids) > self.config.max_size * 2:
                self._seen_ids.popitem(last=False)

        self._seen_hashes[message.content_hash] = now
        # Keep seen hashes bounded
        while len(self._seen_hashes) > self.config.max_size * 2:
            self._seen_hashes.popitem(last=False)

    def _create_summary(self, message: QueuedMessage) -> str:
        """Create a summary line for a dropped message."""
        text = message.content.replace('\n', ' ').strip()
        if len(text) > 140:
            text = text[:139].rstrip() + '…'
        return text

    def _apply_drop_policy(self) -> bool:
        """
        Apply drop policy when at capacity.

        Returns:
            True if new message can be added, False otherwise
        """
        if not self.is_full:
            return True

        policy = self.config.drop_policy

        if policy == DropPolicy.NEW:
            # Reject the new message
            return False

        elif policy == DropPolicy.OLD:
            # Drop oldest message
            if self._items:
                self._items.pop(0)
                self._stats.total_dropped += 1
            return True

        elif policy == DropPolicy.SUMMARIZE:
            # Drop oldest but keep summary
            if self._items:
                dropped = self._items.pop(0)
                self._stats.total_dropped += 1
                summary = self._create_summary(dropped)
                self._stats.dropped_summaries.append(summary)
                # Keep summaries bounded
                while len(self._stats.dropped_summaries) > self.config.max_size:
                    self._stats.dropped_summaries.pop(0)
            return True

        return True

    def _clean_expired(self) -> int:
        """Remove expired messages from queue."""
        if self.config.max_age_seconds <= 0:
            return 0

        cutoff = datetime.now() - timedelta(seconds=self.config.max_age_seconds)
        original_size = len(self._items)
        self._items = [m for m in self._items if m.enqueued_at > cutoff]
        expired = original_size - len(self._items)
        self._stats.total_expired += expired
        return expired

    def enqueue(
        self,
        message: QueuedMessage,
        priority: Optional[int] = None,
    ) -> bool:
        """
        Add a message to the queue.

        Args:
            message: Message to enqueue
            priority: Optional priority override

        Returns:
            True if enqueued, False if rejected
        """
        with self._lock:
            # Clean expired first
            self._clean_expired()

            # Check for duplicates
            if self._is_duplicate(message):
                self._stats.total_deduplicated += 1
                logger.debug(f"Duplicate message rejected: {message.message_id}")
                return False

            # Apply priority
            if priority is not None:
                message.priority = priority

            # Handle LATEST policy - replace all with just this message
            if self.config.policy == QueuePolicy.LATEST:
                dropped_count = len(self._items)
                self._items.clear()
                self._stats.total_dropped += dropped_count

            # Handle DROP policy - reject if at capacity
            elif self.config.policy == QueuePolicy.DROP:
                if self.is_full:
                    self._stats.total_dropped += 1
                    return False

            # Apply drop policy for other modes
            else:
                if not self._apply_drop_policy():
                    self._stats.total_dropped += 1
                    return False

            # Mark as seen
            self._mark_seen(message)

            # Add to queue
            self._items.append(message)
            self._last_enqueue_time = datetime.now()
            self._stats.total_enqueued += 1
            self._stats.current_size = len(self._items)

            # Sort by priority if needed
            if self.config.policy == QueuePolicy.PRIORITY:
                self._items.sort(key=lambda m: -m.priority)

            return True

    def dequeue(self) -> Optional[QueuedMessage]:
        """
        Remove and return the next message from the queue.

        Returns:
            Next message or None if empty
        """
        with self._lock:
            # Clean expired first
            self._clean_expired()

            if not self._items:
                return None

            message = self._items.pop(0)
            self._stats.total_dequeued += 1
            self._stats.current_size = len(self._items)
            return message

    def peek(self) -> Optional[QueuedMessage]:
        """
        View the next message without removing it.

        Returns:
            Next message or None if empty
        """
        with self._lock:
            self._clean_expired()
            return self._items[0] if self._items else None

    def collect(self, max_items: Optional[int] = None) -> List[QueuedMessage]:
        """
        Collect and remove multiple messages (for COLLECT mode).

        Args:
            max_items: Maximum items to collect (defaults to config batch size)

        Returns:
            List of collected messages
        """
        with self._lock:
            self._clean_expired()

            limit = max_items or self.config.collect_batch_size
            collected = self._items[:limit]
            self._items = self._items[limit:]

            self._stats.total_dequeued += len(collected)
            self._stats.current_size = len(self._items)

            return collected

    def clear(self) -> int:
        """
        Clear all messages from the queue.

        Returns:
            Number of messages cleared
        """
        with self._lock:
            count = len(self._items)
            self._items.clear()
            self._seen_ids.clear()
            self._seen_hashes.clear()
            self._stats.current_size = 0
            self._stats.dropped_summaries.clear()
            return count

    def get_stats(self) -> QueueStats:
        """Get queue statistics."""
        with self._lock:
            self._stats.current_size = len(self._items)
            return QueueStats(
                total_enqueued=self._stats.total_enqueued,
                total_dequeued=self._stats.total_dequeued,
                total_dropped=self._stats.total_dropped,
                total_expired=self._stats.total_expired,
                total_deduplicated=self._stats.total_deduplicated,
                current_size=self._stats.current_size,
                dropped_summaries=list(self._stats.dropped_summaries),
            )

    def get_dropped_summary(self) -> Optional[str]:
        """
        Get and clear the dropped messages summary.

        Returns:
            Summary text or None if no dropped messages
        """
        with self._lock:
            if not self._stats.dropped_summaries:
                return None

            count = len(self._stats.dropped_summaries)
            title = f"[Queue overflow] Dropped {count} message{'s' if count != 1 else ''} due to cap."
            lines = [title, "Summary:"]
            for summary in self._stats.dropped_summaries:
                lines.append(f"- {summary}")

            self._stats.dropped_summaries.clear()
            return "\n".join(lines)

    def should_debounce(self) -> bool:
        """Check if we should wait for debounce period."""
        if self.config.debounce_ms <= 0:
            return False
        if self._last_enqueue_time is None:
            return False
        elapsed = (datetime.now() - self._last_enqueue_time).total_seconds() * 1000
        return elapsed < self.config.debounce_ms

    def time_until_debounce_complete(self) -> float:
        """
        Get milliseconds until debounce period completes.

        Returns:
            Milliseconds to wait, or 0 if no wait needed
        """
        if not self.should_debounce():
            return 0
        if self._last_enqueue_time is None:
            return 0
        elapsed = (datetime.now() - self._last_enqueue_time).total_seconds() * 1000
        remaining = self.config.debounce_ms - elapsed
        return max(0, remaining)


class QueueManager:
    """
    Manages multiple message queues by channel/chat.

    Usage:
        manager = QueueManager()

        # Get or create queue for a chat
        queue = manager.get_queue("telegram", "chat123")

        # Enqueue message
        queue.enqueue(message)

        # Process all queues
        count = manager.process_all(processor_func)
    """

    def __init__(
        self,
        default_config: Optional[QueueConfig] = None,
        max_queues: int = 1000,
    ):
        self._default_config = default_config or QueueConfig()
        self._max_queues = max_queues
        self._queues: Dict[Tuple[str, str], MessageQueue] = {}
        self._channel_configs: Dict[str, QueueConfig] = {}
        self._lock = threading.Lock()

    def set_channel_config(self, channel: str, config: QueueConfig) -> None:
        """Set custom config for a specific channel."""
        self._channel_configs[channel] = config

    def get_queue(
        self,
        channel: str,
        chat_id: str,
        create: bool = True,
    ) -> Optional[MessageQueue]:
        """
        Get or create a queue for a channel/chat.

        Args:
            channel: Channel name
            chat_id: Chat identifier
            create: Whether to create if not exists

        Returns:
            MessageQueue or None if not found and create=False
        """
        key = (channel, chat_id)

        with self._lock:
            if key in self._queues:
                return self._queues[key]

            if not create:
                return None

            # Check capacity
            if len(self._queues) >= self._max_queues:
                self._cleanup_empty_queues()

            # Get config for channel
            config = self._channel_configs.get(channel, self._default_config)

            queue = MessageQueue(config)
            self._queues[key] = queue
            return queue

    def has_queue(self, channel: str, chat_id: str) -> bool:
        """Check if a queue exists."""
        return (channel, chat_id) in self._queues

    def delete_queue(self, channel: str, chat_id: str) -> bool:
        """Delete a queue."""
        key = (channel, chat_id)
        with self._lock:
            if key in self._queues:
                del self._queues[key]
                return True
            return False

    def list_queues(self, channel: Optional[str] = None) -> List[Tuple[str, str]]:
        """List all queue keys, optionally filtered by channel."""
        with self._lock:
            if channel:
                return [k for k in self._queues.keys() if k[0] == channel]
            return list(self._queues.keys())

    def get_total_size(self) -> int:
        """Get total messages across all queues."""
        with self._lock:
            return sum(q.size for q in self._queues.values())

    def process_all(
        self,
        processor: Callable[[QueuedMessage], None],
        max_per_queue: int = 1,
    ) -> int:
        """
        Process messages from all queues.

        Args:
            processor: Function to process each message
            max_per_queue: Maximum messages to process per queue

        Returns:
            Total messages processed
        """
        total = 0

        with self._lock:
            queues = list(self._queues.items())

        for (channel, chat_id), queue in queues:
            for _ in range(max_per_queue):
                msg = queue.dequeue()
                if msg is None:
                    break
                try:
                    processor(msg)
                    total += 1
                except Exception as e:
                    logger.error(f"Error processing message from {channel}/{chat_id}: {e}")

        return total

    def cleanup_stale(self, max_age_seconds: int = 3600) -> int:
        """
        Remove queues that have been empty for too long.

        Args:
            max_age_seconds: Maximum age of empty queues

        Returns:
            Number of queues removed
        """
        removed = 0
        cutoff = datetime.now() - timedelta(seconds=max_age_seconds)

        with self._lock:
            to_remove = []
            for key, queue in self._queues.items():
                if queue.is_empty and queue._last_enqueue_time:
                    if queue._last_enqueue_time < cutoff:
                        to_remove.append(key)

            for key in to_remove:
                del self._queues[key]
                removed += 1

        return removed

    def _cleanup_empty_queues(self) -> int:
        """Remove empty queues to make room."""
        removed = 0
        to_remove = [k for k, q in self._queues.items() if q.is_empty]
        for key in to_remove:
            del self._queues[key]
            removed += 1
        return removed

    def get_stats(self) -> Dict[str, Any]:
        """Get aggregated statistics for all queues."""
        with self._lock:
            total_enqueued = 0
            total_dequeued = 0
            total_dropped = 0
            total_messages = 0

            for queue in self._queues.values():
                stats = queue.get_stats()
                total_enqueued += stats.total_enqueued
                total_dequeued += stats.total_dequeued
                total_dropped += stats.total_dropped
                total_messages += stats.current_size

            return {
                "queue_count": len(self._queues),
                "total_messages": total_messages,
                "total_enqueued": total_enqueued,
                "total_dequeued": total_dequeued,
                "total_dropped": total_dropped,
            }


# Singleton instance
_queue_manager: Optional[QueueManager] = None


def get_queue_manager() -> QueueManager:
    """Get or create the global queue manager."""
    global _queue_manager
    if _queue_manager is None:
        _queue_manager = QueueManager()
    return _queue_manager
