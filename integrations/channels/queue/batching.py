"""
Message Batching System

Collects messages by key (chat_id, user_id, or channel) and batches them
together for efficient processing.

Ported from HevolveBot's src/auto-reply/reply/batch.ts.

Features:
- Collect messages by configurable key
- Max batch size limit
- Max wait time before auto-flush
- Manual and automatic flush methods
- Thread-safe operation
- Statistics tracking
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import (
    Optional,
    Dict,
    List,
    Any,
    Callable,
    TypeVar,
    Generic,
    Tuple,
    Union,
)

logger = logging.getLogger(__name__)

T = TypeVar('T')


class BatchKeyType(Enum):
    """Type of key used for batching messages."""
    CHAT_ID = "chat_id"
    USER_ID = "user_id"
    CHANNEL = "channel"
    CUSTOM = "custom"


@dataclass
class BatchConfig:
    """Configuration for message batching."""
    max_batch_size: int = 10
    max_wait_ms: int = 5000
    key_type: BatchKeyType = BatchKeyType.CHAT_ID
    auto_flush: bool = True
    flush_on_shutdown: bool = True


@dataclass
class BatchStats:
    """Statistics for message batcher."""
    total_received: int = 0
    total_batched: int = 0
    total_flushed: int = 0
    total_batches_created: int = 0
    total_auto_flushes: int = 0
    total_manual_flushes: int = 0
    total_size_flushes: int = 0
    current_pending: int = 0
    current_batch_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert stats to dictionary."""
        return {
            "total_received": self.total_received,
            "total_batched": self.total_batched,
            "total_flushed": self.total_flushed,
            "total_batches_created": self.total_batches_created,
            "total_auto_flushes": self.total_auto_flushes,
            "total_manual_flushes": self.total_manual_flushes,
            "total_size_flushes": self.total_size_flushes,
            "current_pending": self.current_pending,
            "current_batch_count": self.current_batch_count,
        }


@dataclass
class Batch(Generic[T]):
    """A batch of collected messages."""
    key: str
    items: List[T] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    last_added: datetime = field(default_factory=datetime.now)
    flush_timer: Optional[asyncio.Task] = field(default=None, repr=False)
    sync_timer: Optional[threading.Timer] = field(default=None, repr=False)

    def add(self, item: T) -> None:
        """Add an item to the batch."""
        self.items.append(item)
        self.last_added = datetime.now()

    def clear(self) -> List[T]:
        """Clear and return all items."""
        items = self.items
        self.items = []
        return items

    def size(self) -> int:
        """Get batch size."""
        return len(self.items)

    def age_ms(self) -> float:
        """Get batch age in milliseconds."""
        return (datetime.now() - self.created_at).total_seconds() * 1000

    def cancel_timer(self) -> None:
        """Cancel any pending flush timer."""
        if self.flush_timer and not self.flush_timer.done():
            self.flush_timer.cancel()
            self.flush_timer = None
        if self.sync_timer:
            self.sync_timer.cancel()
            self.sync_timer = None


@dataclass
class BatchResult(Generic[T]):
    """Result of a batch flush operation."""
    key: str
    items: List[T]
    batch_size: int
    wait_time_ms: float
    flush_reason: str  # "size", "time", "manual", "shutdown"


class MessageBatcher(Generic[T]):
    """
    Collects messages into batches by key.

    Messages are grouped by a key (chat_id, user_id, channel, or custom)
    and batched together until either:
    - Max batch size is reached
    - Max wait time expires
    - Manual flush is called

    Usage:
        config = BatchConfig(max_batch_size=10, max_wait_ms=5000)
        batcher = MessageBatcher(config)

        # Add messages
        result = await batcher.add(message, key="chat123")
        if result:
            # Batch was flushed
            process_batch(result.items)

        # Manual flush
        batch = batcher.flush("chat123")

        # Flush all
        batches = batcher.flush_all()
    """

    def __init__(
        self,
        config: BatchConfig,
        key_extractor: Optional[Callable[[T], str]] = None,
        on_flush: Optional[Callable[[BatchResult[T]], Any]] = None,
        on_error: Optional[Callable[[Exception, BatchResult[T]], None]] = None,
    ):
        """
        Initialize the message batcher.

        Args:
            config: Batching configuration
            key_extractor: Function to extract key from message
            on_flush: Callback when batch is flushed
            on_error: Callback on flush error
        """
        self.config = config
        self.key_extractor = key_extractor
        self.on_flush = on_flush
        self.on_error = on_error
        self._batches: Dict[str, Batch[T]] = {}
        self._lock = threading.Lock()
        self._stats = BatchStats()
        self._shutdown = False

    def _get_key(
        self,
        item: T,
        key: Optional[str] = None,
    ) -> str:
        """
        Get the batching key for an item.

        Args:
            item: The item to get key for
            key: Optional explicit key

        Returns:
            The batching key
        """
        if key is not None:
            return key

        if self.key_extractor is not None:
            return self.key_extractor(item)

        # Try to extract from item attributes based on key_type
        if self.config.key_type == BatchKeyType.CHAT_ID:
            if hasattr(item, 'chat_id'):
                return str(getattr(item, 'chat_id'))
        elif self.config.key_type == BatchKeyType.USER_ID:
            if hasattr(item, 'user_id') or hasattr(item, 'sender_id'):
                return str(getattr(item, 'user_id', None) or getattr(item, 'sender_id', ''))
        elif self.config.key_type == BatchKeyType.CHANNEL:
            if hasattr(item, 'channel'):
                return str(getattr(item, 'channel'))

        return "default"

    async def add(
        self,
        item: T,
        key: Optional[str] = None,
    ) -> Optional[BatchResult[T]]:
        """
        Add an item to a batch.

        Args:
            item: The item to add
            key: Optional explicit key (otherwise extracted from item)

        Returns:
            BatchResult if batch was flushed, None if buffered
        """
        batch_key = self._get_key(item, key)

        self._stats.total_received += 1

        with self._lock:
            # Get or create batch
            if batch_key not in self._batches:
                self._batches[batch_key] = Batch(key=batch_key)
                self._stats.total_batches_created += 1
                self._stats.current_batch_count = len(self._batches)

            batch = self._batches[batch_key]
            batch.add(item)
            self._stats.total_batched += 1
            self._stats.current_pending += 1

            # Check if batch is full
            if batch.size() >= self.config.max_batch_size:
                # Flush immediately
                return await self._flush_batch(batch_key, "size")

            # Schedule auto-flush timer if enabled
            if self.config.auto_flush and self.config.max_wait_ms > 0:
                batch.cancel_timer()
                batch.flush_timer = asyncio.create_task(
                    self._timer_flush(batch_key)
                )

        return None

    async def _timer_flush(self, key: str) -> None:
        """Timer callback for auto-flush."""
        try:
            await asyncio.sleep(self.config.max_wait_ms / 1000.0)
            await self._flush_batch(key, "time")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in batch timer flush: {e}")

    async def _flush_batch(
        self,
        key: str,
        reason: str,
    ) -> Optional[BatchResult[T]]:
        """
        Flush a specific batch.

        Args:
            key: Batch key
            reason: Reason for flush

        Returns:
            BatchResult with flushed items
        """
        with self._lock:
            if key not in self._batches:
                return None

            batch = self._batches[key]
            items = batch.clear()
            wait_time_ms = batch.age_ms()

            batch.cancel_timer()
            del self._batches[key]

            self._stats.current_pending -= len(items)
            self._stats.current_batch_count = len(self._batches)

            if not items:
                return None

            self._stats.total_flushed += len(items)

            if reason == "time":
                self._stats.total_auto_flushes += 1
            elif reason == "size":
                self._stats.total_size_flushes += 1
            elif reason == "manual":
                self._stats.total_manual_flushes += 1

        result = BatchResult(
            key=key,
            items=items,
            batch_size=len(items),
            wait_time_ms=wait_time_ms,
            flush_reason=reason,
        )

        # Call flush callback
        if self.on_flush:
            try:
                callback_result = self.on_flush(result)
                if asyncio.iscoroutine(callback_result):
                    await callback_result
            except Exception as e:
                if self.on_error:
                    self.on_error(e, result)
                logger.error(f"Error in batch flush callback: {e}")

        return result

    async def flush(self, key: str) -> Optional[BatchResult[T]]:
        """
        Manually flush a specific batch.

        Args:
            key: Batch key to flush

        Returns:
            BatchResult with flushed items, or None if no batch
        """
        return await self._flush_batch(key, "manual")

    def flush_sync(self, key: str) -> Optional[BatchResult[T]]:
        """
        Synchronously flush a specific batch.

        Args:
            key: Batch key to flush

        Returns:
            BatchResult with flushed items
        """
        with self._lock:
            if key not in self._batches:
                return None

            batch = self._batches[key]
            items = batch.clear()
            wait_time_ms = batch.age_ms()

            batch.cancel_timer()
            del self._batches[key]

            self._stats.current_pending -= len(items)
            self._stats.current_batch_count = len(self._batches)
            self._stats.total_flushed += len(items)
            self._stats.total_manual_flushes += 1

        if not items:
            return None

        return BatchResult(
            key=key,
            items=items,
            batch_size=len(items),
            wait_time_ms=wait_time_ms,
            flush_reason="manual",
        )

    async def flush_all(self) -> List[BatchResult[T]]:
        """
        Flush all batches.

        Returns:
            List of BatchResults for each flushed batch
        """
        with self._lock:
            keys = list(self._batches.keys())

        results = []
        for key in keys:
            result = await self.flush(key)
            if result:
                results.append(result)

        return results

    def flush_all_sync(self) -> List[BatchResult[T]]:
        """
        Synchronously flush all batches.

        Returns:
            List of BatchResults
        """
        with self._lock:
            keys = list(self._batches.keys())

        results = []
        for key in keys:
            result = self.flush_sync(key)
            if result:
                results.append(result)

        return results

    def get_batch(self, key: str) -> Optional[List[T]]:
        """
        Get items in a batch without flushing.

        Args:
            key: Batch key

        Returns:
            List of items or None if no batch
        """
        with self._lock:
            if key not in self._batches:
                return None
            return list(self._batches[key].items)

    def get_batch_size(self, key: str) -> int:
        """
        Get size of a specific batch.

        Args:
            key: Batch key

        Returns:
            Number of items in batch
        """
        with self._lock:
            if key not in self._batches:
                return 0
            return self._batches[key].size()

    def get_pending_count(self) -> int:
        """Get total pending items across all batches."""
        with self._lock:
            return sum(b.size() for b in self._batches.values())

    def get_batch_count(self) -> int:
        """Get number of active batches."""
        with self._lock:
            return len(self._batches)

    def get_batch_keys(self) -> List[str]:
        """Get list of active batch keys."""
        with self._lock:
            return list(self._batches.keys())

    def get_stats(self) -> BatchStats:
        """Get batching statistics."""
        with self._lock:
            self._stats.current_pending = sum(b.size() for b in self._batches.values())
            self._stats.current_batch_count = len(self._batches)

        return BatchStats(
            total_received=self._stats.total_received,
            total_batched=self._stats.total_batched,
            total_flushed=self._stats.total_flushed,
            total_batches_created=self._stats.total_batches_created,
            total_auto_flushes=self._stats.total_auto_flushes,
            total_manual_flushes=self._stats.total_manual_flushes,
            total_size_flushes=self._stats.total_size_flushes,
            current_pending=self._stats.current_pending,
            current_batch_count=self._stats.current_batch_count,
        )

    def clear(self) -> int:
        """
        Clear all batches without flushing.

        Returns:
            Number of items cleared
        """
        with self._lock:
            total = 0
            for batch in self._batches.values():
                total += batch.size()
                batch.cancel_timer()
            self._batches.clear()
            self._stats.current_pending = 0
            self._stats.current_batch_count = 0
        return total

    async def shutdown(self) -> List[BatchResult[T]]:
        """
        Shutdown the batcher, flushing remaining batches if configured.

        Returns:
            List of flushed BatchResults
        """
        self._shutdown = True

        if self.config.flush_on_shutdown:
            return await self.flush_all()

        self.clear()
        return []


class SyncMessageBatcher(Generic[T]):
    """
    Synchronous version of MessageBatcher.

    Uses threading.Timer for auto-flush instead of asyncio.

    Usage:
        config = BatchConfig(max_batch_size=10, max_wait_ms=5000)
        batcher = SyncMessageBatcher(config)

        # Add messages
        result = batcher.add(message, key="chat123")
        if result:
            process_batch(result.items)
    """

    def __init__(
        self,
        config: BatchConfig,
        key_extractor: Optional[Callable[[T], str]] = None,
        on_flush: Optional[Callable[[BatchResult[T]], None]] = None,
    ):
        self.config = config
        self.key_extractor = key_extractor
        self.on_flush = on_flush
        self._batches: Dict[str, Batch[T]] = {}
        self._lock = threading.Lock()
        self._stats = BatchStats()

    def _get_key(self, item: T, key: Optional[str] = None) -> str:
        """Get batching key for item."""
        if key is not None:
            return key
        if self.key_extractor is not None:
            return self.key_extractor(item)
        if hasattr(item, 'chat_id'):
            return str(getattr(item, 'chat_id'))
        return "default"

    def add(
        self,
        item: T,
        key: Optional[str] = None,
    ) -> Optional[BatchResult[T]]:
        """
        Add an item to a batch.

        Args:
            item: The item to add
            key: Optional explicit key

        Returns:
            BatchResult if flushed, None if buffered
        """
        batch_key = self._get_key(item, key)

        self._stats.total_received += 1

        with self._lock:
            if batch_key not in self._batches:
                self._batches[batch_key] = Batch(key=batch_key)
                self._stats.total_batches_created += 1

            batch = self._batches[batch_key]
            batch.add(item)
            self._stats.total_batched += 1
            self._stats.current_pending += 1

            # Check if full
            if batch.size() >= self.config.max_batch_size:
                return self._flush_batch_locked(batch_key, "size")

            # Schedule timer
            if self.config.auto_flush and self.config.max_wait_ms > 0:
                batch.cancel_timer()
                timer = threading.Timer(
                    self.config.max_wait_ms / 1000.0,
                    self._timer_flush,
                    args=[batch_key],
                )
                timer.daemon = True
                timer.start()
                batch.sync_timer = timer

        return None

    def _timer_flush(self, key: str) -> None:
        """Timer callback."""
        result = self.flush(key, reason="time")
        if result and self.on_flush:
            self.on_flush(result)

    def _flush_batch_locked(
        self,
        key: str,
        reason: str,
    ) -> Optional[BatchResult[T]]:
        """Flush batch while holding lock."""
        if key not in self._batches:
            return None

        batch = self._batches[key]
        items = batch.clear()
        wait_time_ms = batch.age_ms()

        batch.cancel_timer()
        del self._batches[key]

        self._stats.current_pending -= len(items)
        self._stats.current_batch_count = len(self._batches)

        if not items:
            return None

        self._stats.total_flushed += len(items)
        if reason == "time":
            self._stats.total_auto_flushes += 1
        elif reason == "size":
            self._stats.total_size_flushes += 1
        elif reason == "manual":
            self._stats.total_manual_flushes += 1

        result = BatchResult(
            key=key,
            items=items,
            batch_size=len(items),
            wait_time_ms=wait_time_ms,
            flush_reason=reason,
        )

        if self.on_flush and reason != "time":  # Timer calls on_flush itself
            self.on_flush(result)

        return result

    def flush(
        self,
        key: str,
        reason: str = "manual",
    ) -> Optional[BatchResult[T]]:
        """
        Flush a specific batch.

        Args:
            key: Batch key
            reason: Reason for flush

        Returns:
            BatchResult or None
        """
        with self._lock:
            return self._flush_batch_locked(key, reason)

    def flush_all(self) -> List[BatchResult[T]]:
        """Flush all batches."""
        with self._lock:
            keys = list(self._batches.keys())

        results = []
        for key in keys:
            result = self.flush(key)
            if result:
                results.append(result)

        return results

    def get_pending_count(self) -> int:
        """Get total pending items."""
        with self._lock:
            return sum(b.size() for b in self._batches.values())

    def get_batch_count(self) -> int:
        """Get number of active batches."""
        with self._lock:
            return len(self._batches)

    def get_batch_keys(self) -> List[str]:
        """Get active batch keys."""
        with self._lock:
            return list(self._batches.keys())

    def get_stats(self) -> BatchStats:
        """Get statistics."""
        with self._lock:
            self._stats.current_pending = sum(b.size() for b in self._batches.values())
            self._stats.current_batch_count = len(self._batches)

        return BatchStats(
            total_received=self._stats.total_received,
            total_batched=self._stats.total_batched,
            total_flushed=self._stats.total_flushed,
            total_batches_created=self._stats.total_batches_created,
            total_auto_flushes=self._stats.total_auto_flushes,
            total_manual_flushes=self._stats.total_manual_flushes,
            total_size_flushes=self._stats.total_size_flushes,
            current_pending=self._stats.current_pending,
            current_batch_count=self._stats.current_batch_count,
        )

    def clear(self) -> int:
        """Clear all batches without flushing."""
        with self._lock:
            total = sum(b.size() for b in self._batches.values())
            for batch in self._batches.values():
                batch.cancel_timer()
            self._batches.clear()
            self._stats.current_pending = 0
            self._stats.current_batch_count = 0
        return total


class BatchAggregator(Generic[T]):
    """
    Aggregates multiple batches into larger groups.

    Useful for combining batches from multiple sources before processing.
    """

    def __init__(
        self,
        max_aggregate_size: int = 100,
        max_sources: int = 10,
    ):
        self.max_aggregate_size = max_aggregate_size
        self.max_sources = max_sources
        self._pending: Dict[str, List[BatchResult[T]]] = {}
        self._lock = threading.Lock()

    def add_batch(
        self,
        batch: BatchResult[T],
        aggregate_key: str = "default",
    ) -> Optional[List[BatchResult[T]]]:
        """
        Add a batch to the aggregator.

        Args:
            batch: BatchResult to add
            aggregate_key: Key for grouping batches

        Returns:
            List of batches if aggregate threshold reached
        """
        with self._lock:
            if aggregate_key not in self._pending:
                self._pending[aggregate_key] = []

            self._pending[aggregate_key].append(batch)

            # Check if we should flush
            total_items = sum(b.batch_size for b in self._pending[aggregate_key])
            if total_items >= self.max_aggregate_size or len(self._pending[aggregate_key]) >= self.max_sources:
                batches = self._pending.pop(aggregate_key)
                return batches

        return None

    def flush(self, aggregate_key: str) -> List[BatchResult[T]]:
        """Flush a specific aggregate."""
        with self._lock:
            return self._pending.pop(aggregate_key, [])

    def flush_all(self) -> Dict[str, List[BatchResult[T]]]:
        """Flush all aggregates."""
        with self._lock:
            result = dict(self._pending)
            self._pending.clear()
        return result

    def get_pending_count(self, aggregate_key: Optional[str] = None) -> int:
        """Get pending batch count."""
        with self._lock:
            if aggregate_key:
                return len(self._pending.get(aggregate_key, []))
            return sum(len(batches) for batches in self._pending.values())
