"""
Inbound Debouncing System

Collects rapid-fire messages and batches them together before processing.
Ported from HevolveBot's src/auto-reply/inbound-debounce.ts.

Features:
- Configurable debounce windows per channel
- Automatic flush on timeout
- Manual flush support
- Max messages limit
- Thread-safe operation
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, List, Any, Callable, TypeVar, Generic

logger = logging.getLogger(__name__)

T = TypeVar('T')


@dataclass
class DebounceConfig:
    """Configuration for debouncing."""
    window_ms: int = 1000
    max_messages: int = 10
    channel_overrides: Dict[str, int] = field(default_factory=dict)


@dataclass
class DebounceStats:
    """Statistics for debouncer."""
    total_received: int = 0
    total_flushed: int = 0
    total_batches: int = 0
    current_pending: int = 0


class DebounceBuffer(Generic[T]):
    """Buffer for collecting messages during debounce window."""

    def __init__(self):
        self.items: List[T] = []
        self.timer_task: Optional[asyncio.Task] = None
        self.created_at: datetime = datetime.now()
        self.last_added: datetime = datetime.now()

    def add(self, item: T) -> None:
        """Add an item to the buffer."""
        self.items.append(item)
        self.last_added = datetime.now()

    def clear(self) -> List[T]:
        """Clear and return all items."""
        items = self.items
        self.items = []
        return items

    def cancel_timer(self) -> None:
        """Cancel the flush timer if running."""
        if self.timer_task and not self.timer_task.done():
            self.timer_task.cancel()
            self.timer_task = None


class InboundDebouncer(Generic[T]):
    """
    Debounces inbound messages by collecting them into batches.

    Messages arriving within the debounce window are collected together
    and flushed as a batch when the window expires or max messages reached.

    Usage:
        config = DebounceConfig(window_ms=1000, max_messages=10)
        debouncer = InboundDebouncer(config)

        # Async usage
        async def handle_message(msg):
            result = await debouncer.debounce(msg, key_func=lambda m: m.chat_id)
            if result:
                # Process batch
                for m in result:
                    process(m)

        # Or with callback
        debouncer = InboundDebouncer(
            config,
            on_flush=lambda items: process_batch(items)
        )
        await debouncer.debounce(msg, key_func=lambda m: m.chat_id)
    """

    def __init__(
        self,
        config: DebounceConfig,
        on_flush: Optional[Callable[[List[T]], Any]] = None,
        on_error: Optional[Callable[[Exception, List[T]], None]] = None,
    ):
        self.config = config
        self.on_flush = on_flush
        self.on_error = on_error
        self._buffers: Dict[str, DebounceBuffer[T]] = {}
        self._lock = threading.Lock()
        self._stats = DebounceStats()

    def _get_debounce_ms(self, channel: Optional[str] = None) -> int:
        """Get debounce window for a channel."""
        if channel and channel in self.config.channel_overrides:
            return max(0, self.config.channel_overrides[channel])
        return max(0, self.config.window_ms)

    async def debounce(
        self,
        item: T,
        key: Optional[str] = None,
        key_func: Optional[Callable[[T], str]] = None,
        channel: Optional[str] = None,
        should_debounce: bool = True,
    ) -> Optional[List[T]]:
        """
        Add an item to the debounce buffer.

        Args:
            item: The item to debounce
            key: Buffer key (e.g., chat_id)
            key_func: Function to extract key from item
            channel: Channel name for per-channel debounce settings
            should_debounce: Whether to actually debounce (False = immediate)

        Returns:
            List of items if flushed immediately, None if buffered
        """
        # Determine the key
        if key is None and key_func is not None:
            key = key_func(item)

        if key is None:
            key = "default"

        debounce_ms = self._get_debounce_ms(channel)

        self._stats.total_received += 1

        # If debouncing disabled or not requested, flush immediately
        if debounce_ms <= 0 or not should_debounce:
            # First flush any pending items for this key
            pending = await self._flush_key(key)

            # Return all items including the new one
            result = pending + [item]
            self._stats.total_flushed += len(result)
            self._stats.total_batches += 1

            if self.on_flush:
                try:
                    await self._call_flush(result)
                except Exception as e:
                    if self.on_error:
                        self.on_error(e, result)
                return None

            return result

        # Add to buffer
        with self._lock:
            if key not in self._buffers:
                self._buffers[key] = DebounceBuffer()

            buffer = self._buffers[key]
            buffer.add(item)
            self._stats.current_pending += 1

            # Check max messages
            if len(buffer.items) >= self.config.max_messages:
                # Flush immediately
                items = buffer.clear()
                buffer.cancel_timer()
                del self._buffers[key]
                self._stats.current_pending -= len(items)
                self._stats.total_flushed += len(items)
                self._stats.total_batches += 1

                if self.on_flush:
                    try:
                        await self._call_flush(items)
                    except Exception as e:
                        if self.on_error:
                            self.on_error(e, items)
                    return None

                return items

            # Schedule or reschedule timer
            buffer.cancel_timer()
            buffer.timer_task = asyncio.create_task(
                self._timer_flush(key, debounce_ms)
            )

        return None

    async def _timer_flush(self, key: str, delay_ms: int) -> None:
        """Timer callback to flush buffer."""
        try:
            await asyncio.sleep(delay_ms / 1000.0)
            await self._flush_key(key, from_timer=True)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in debounce timer flush: {e}")

    async def _flush_key(self, key: str, from_timer: bool = False) -> List[T]:
        """Flush a specific buffer key."""
        with self._lock:
            if key not in self._buffers:
                return []

            buffer = self._buffers[key]
            items = buffer.clear()

            if not from_timer:
                buffer.cancel_timer()

            del self._buffers[key]

            self._stats.current_pending -= len(items)

        if items:
            self._stats.total_flushed += len(items)
            self._stats.total_batches += 1

            if self.on_flush:
                try:
                    await self._call_flush(items)
                except Exception as e:
                    if self.on_error:
                        self.on_error(e, items)
                    return []

        return items

    async def _call_flush(self, items: List[T]) -> None:
        """Call the flush callback."""
        if self.on_flush:
            result = self.on_flush(items)
            if asyncio.iscoroutine(result):
                await result

    def flush(self, key: str) -> List[T]:
        """
        Synchronously flush a specific buffer.

        Args:
            key: Buffer key to flush

        Returns:
            List of flushed items
        """
        with self._lock:
            if key not in self._buffers:
                return []

            buffer = self._buffers[key]
            items = buffer.clear()
            buffer.cancel_timer()
            del self._buffers[key]

            self._stats.current_pending -= len(items)
            self._stats.total_flushed += len(items)
            if items:
                self._stats.total_batches += 1

        return items

    def flush_all(self) -> Dict[str, List[T]]:
        """
        Flush all buffers synchronously.

        Returns:
            Dict mapping keys to their flushed items
        """
        result = {}

        with self._lock:
            keys = list(self._buffers.keys())

        for key in keys:
            items = self.flush(key)
            if items:
                result[key] = items

        return result

    def get_pending_count(self) -> int:
        """Get total number of pending items across all buffers."""
        with self._lock:
            return sum(len(b.items) for b in self._buffers.values())

    def get_pending_keys(self) -> List[str]:
        """Get list of keys with pending items."""
        with self._lock:
            return list(self._buffers.keys())

    def get_stats(self) -> DebounceStats:
        """Get debouncer statistics."""
        with self._lock:
            self._stats.current_pending = sum(len(b.items) for b in self._buffers.values())
        return DebounceStats(
            total_received=self._stats.total_received,
            total_flushed=self._stats.total_flushed,
            total_batches=self._stats.total_batches,
            current_pending=self._stats.current_pending,
        )

    def clear(self) -> int:
        """
        Clear all buffers without flushing.

        Returns:
            Number of items cleared
        """
        with self._lock:
            total = sum(len(b.items) for b in self._buffers.values())
            for buffer in self._buffers.values():
                buffer.cancel_timer()
            self._buffers.clear()
            self._stats.current_pending = 0
        return total


class SyncDebouncer(Generic[T]):
    """
    Synchronous debouncer for non-async contexts.

    Uses threading for timer-based flushing.
    """

    def __init__(
        self,
        config: DebounceConfig,
        on_flush: Optional[Callable[[List[T]], None]] = None,
    ):
        self.config = config
        self.on_flush = on_flush
        self._buffers: Dict[str, List[T]] = {}
        self._timers: Dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._stats = DebounceStats()

    def _get_debounce_ms(self, channel: Optional[str] = None) -> int:
        """Get debounce window for a channel."""
        if channel and channel in self.config.channel_overrides:
            return max(0, self.config.channel_overrides[channel])
        return max(0, self.config.window_ms)

    def debounce(
        self,
        item: T,
        key: str,
        channel: Optional[str] = None,
    ) -> Optional[List[T]]:
        """
        Add an item to the debounce buffer.

        Args:
            item: The item to debounce
            key: Buffer key
            channel: Channel for debounce settings

        Returns:
            List of items if immediately flushed, None if buffered
        """
        debounce_ms = self._get_debounce_ms(channel)

        self._stats.total_received += 1

        # If debouncing disabled
        if debounce_ms <= 0:
            pending = self.flush(key)
            result = pending + [item]
            self._stats.total_flushed += len(result)
            self._stats.total_batches += 1

            if self.on_flush:
                self.on_flush(result)
                return None
            return result

        with self._lock:
            # Cancel existing timer
            if key in self._timers:
                self._timers[key].cancel()
                del self._timers[key]

            # Add to buffer
            if key not in self._buffers:
                self._buffers[key] = []
            self._buffers[key].append(item)
            self._stats.current_pending += 1

            # Check max messages
            if len(self._buffers[key]) >= self.config.max_messages:
                items = self._buffers.pop(key)
                self._stats.current_pending -= len(items)
                self._stats.total_flushed += len(items)
                self._stats.total_batches += 1

                if self.on_flush:
                    self.on_flush(items)
                    return None
                return items

            # Schedule timer
            timer = threading.Timer(
                debounce_ms / 1000.0,
                self._timer_flush,
                args=[key],
            )
            timer.daemon = True
            timer.start()
            self._timers[key] = timer

        return None

    def _timer_flush(self, key: str) -> None:
        """Timer callback."""
        items = self.flush(key)
        if items and self.on_flush:
            self.on_flush(items)

    def flush(self, key: str) -> List[T]:
        """Flush a specific buffer."""
        with self._lock:
            if key in self._timers:
                self._timers[key].cancel()
                del self._timers[key]

            if key not in self._buffers:
                return []

            items = self._buffers.pop(key)
            self._stats.current_pending -= len(items)
            self._stats.total_flushed += len(items)
            if items:
                self._stats.total_batches += 1
            return items

    def flush_all(self) -> Dict[str, List[T]]:
        """Flush all buffers."""
        result = {}
        with self._lock:
            keys = list(self._buffers.keys())

        for key in keys:
            items = self.flush(key)
            if items:
                result[key] = items
        return result

    def get_pending_count(self) -> int:
        """Get pending item count."""
        with self._lock:
            return sum(len(items) for items in self._buffers.values())

    def get_stats(self) -> DebounceStats:
        """Get statistics."""
        with self._lock:
            self._stats.current_pending = sum(len(items) for items in self._buffers.values())
        return DebounceStats(
            total_received=self._stats.total_received,
            total_flushed=self._stats.total_flushed,
            total_batches=self._stats.total_batches,
            current_pending=self._stats.current_pending,
        )
