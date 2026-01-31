"""
Unified Message Pipeline

Combines all queue components into a single processing pipeline:
debounce -> dedupe -> rate_limit -> concurrency -> queue

Ported from HevolveBot's unified message handling approach.

Features:
- Configurable pipeline stages
- Async and sync processing
- Statistics tracking across all stages
- Error handling with retry
- Graceful shutdown
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import (
    Optional,
    Dict,
    List,
    Any,
    Callable,
    TypeVar,
    Generic,
    Awaitable,
    Union,
)

from .debounce import DebounceConfig, InboundDebouncer, SyncDebouncer
from .dedupe import DedupeConfig, DedupeMode, MessageDeduplicator
from .rate_limit import RateLimitConfig, RateLimiter, RateLimitResult
from .concurrency import ConcurrencyLimits, ConcurrencyController
from .message_queue import QueueConfig, QueuePolicy, MessageQueue, QueuedMessage
from .retry import RetryConfig, RetryHandler
from .batching import BatchConfig, MessageBatcher, BatchResult

logger = logging.getLogger(__name__)

T = TypeVar('T')


class PipelineStage(Enum):
    """Stages in the message pipeline."""
    DEBOUNCE = "debounce"
    DEDUPE = "dedupe"
    RATE_LIMIT = "rate_limit"
    CONCURRENCY = "concurrency"
    QUEUE = "queue"
    BATCH = "batch"
    PROCESS = "process"


class PipelineResult(Enum):
    """Result of pipeline processing."""
    PROCESSED = "processed"
    DEBOUNCED = "debounced"  # Held for debouncing
    DUPLICATE = "duplicate"
    RATE_LIMITED = "rate_limited"
    CONCURRENCY_LIMITED = "concurrency_limited"
    QUEUED = "queued"
    BATCHED = "batched"
    REJECTED = "rejected"
    ERROR = "error"


@dataclass
class PipelineConfig:
    """Configuration for the message pipeline."""
    # Stage enablement
    enable_debounce: bool = True
    enable_dedupe: bool = True
    enable_rate_limit: bool = True
    enable_concurrency: bool = True
    enable_queue: bool = True
    enable_batch: bool = False
    enable_retry: bool = True

    # Stage configs
    debounce: DebounceConfig = field(default_factory=DebounceConfig)
    dedupe: DedupeConfig = field(default_factory=DedupeConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    concurrency: ConcurrencyLimits = field(default_factory=ConcurrencyLimits)
    queue: QueueConfig = field(default_factory=QueueConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)


@dataclass
class PipelineStats:
    """Statistics for the pipeline."""
    total_received: int = 0
    total_processed: int = 0
    total_debounced: int = 0
    total_deduplicated: int = 0
    total_rate_limited: int = 0
    total_concurrency_limited: int = 0
    total_queued: int = 0
    total_batched: int = 0
    total_rejected: int = 0
    total_errors: int = 0
    total_retries: int = 0
    last_processed_at: Optional[datetime] = None
    current_in_flight: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert stats to dictionary."""
        return {
            "total_received": self.total_received,
            "total_processed": self.total_processed,
            "total_debounced": self.total_debounced,
            "total_deduplicated": self.total_deduplicated,
            "total_rate_limited": self.total_rate_limited,
            "total_concurrency_limited": self.total_concurrency_limited,
            "total_queued": self.total_queued,
            "total_batched": self.total_batched,
            "total_rejected": self.total_rejected,
            "total_errors": self.total_errors,
            "total_retries": self.total_retries,
            "last_processed_at": self.last_processed_at.isoformat() if self.last_processed_at else None,
            "current_in_flight": self.current_in_flight,
        }


@dataclass
class PipelineMessage(Generic[T]):
    """A message wrapper with pipeline metadata."""
    payload: T
    message_id: str
    channel: str
    chat_id: str
    user_id: str
    content: str = ""
    priority: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    received_at: datetime = field(default_factory=datetime.now)

    # Pipeline state
    current_stage: PipelineStage = PipelineStage.DEBOUNCE
    result: Optional[PipelineResult] = None
    error: Optional[Exception] = None
    retry_count: int = 0


class MessagePipeline(Generic[T]):
    """
    Unified message processing pipeline.

    Combines all queue components in sequence:
    1. Debounce - Collect rapid-fire messages
    2. Dedupe - Filter duplicate messages
    3. Rate Limit - Enforce rate limits
    4. Concurrency - Control concurrent processing
    5. Queue - Buffer messages when needed
    6. Batch (optional) - Batch messages together
    7. Process - Execute message handler

    Usage:
        config = PipelineConfig()
        pipeline = MessagePipeline(config)

        # Set message handler
        async def handle_message(msg):
            print(f"Processing: {msg.content}")

        pipeline.set_handler(handle_message)

        # Process messages
        result = await pipeline.process(message)

        # Get stats
        stats = pipeline.get_stats()
    """

    def __init__(
        self,
        config: PipelineConfig,
        handler: Optional[Callable[[T], Awaitable[Any]]] = None,
    ):
        """
        Initialize the pipeline.

        Args:
            config: Pipeline configuration
            handler: Message handler function
        """
        self.config = config
        self._handler = handler
        self._lock = threading.Lock()
        self._stats = PipelineStats()
        self._shutdown = False

        # Initialize components
        self._debouncer: Optional[InboundDebouncer[PipelineMessage[T]]] = None
        self._deduper: Optional[MessageDeduplicator[PipelineMessage[T]]] = None
        self._rate_limiter: Optional[RateLimiter] = None
        self._concurrency: Optional[ConcurrencyController] = None
        self._queue: Optional[MessageQueue] = None
        self._batcher: Optional[MessageBatcher[PipelineMessage[T]]] = None
        self._retry_handler: Optional[RetryHandler] = None

        self._init_components()

    def _init_components(self) -> None:
        """Initialize pipeline components."""
        if self.config.enable_debounce:
            self._debouncer = InboundDebouncer(
                self.config.debounce,
                on_flush=self._on_debounce_flush,
            )

        if self.config.enable_dedupe:
            self._deduper = MessageDeduplicator(self.config.dedupe)

        if self.config.enable_rate_limit:
            self._rate_limiter = RateLimiter(self.config.rate_limit)

        if self.config.enable_concurrency:
            self._concurrency = ConcurrencyController(self.config.concurrency)

        if self.config.enable_queue:
            self._queue = MessageQueue(self.config.queue)

        if self.config.enable_batch:
            self._batcher = MessageBatcher(
                self.config.batch,
                on_flush=self._on_batch_flush,
            )

        if self.config.enable_retry:
            self._retry_handler = RetryHandler(self.config.retry)

    def set_handler(
        self,
        handler: Callable[[T], Awaitable[Any]],
    ) -> None:
        """
        Set the message handler.

        Args:
            handler: Async function to handle messages
        """
        self._handler = handler

    async def _on_debounce_flush(
        self,
        messages: List[PipelineMessage[T]],
    ) -> None:
        """Handle debounce flush."""
        for msg in messages:
            await self._process_after_debounce(msg)

    def _on_batch_flush(
        self,
        batch: BatchResult[PipelineMessage[T]],
    ) -> None:
        """Handle batch flush - schedule processing."""
        asyncio.create_task(self._process_batch(batch))

    async def _process_batch(
        self,
        batch: BatchResult[PipelineMessage[T]],
    ) -> None:
        """Process a batch of messages."""
        for msg in batch.items:
            await self._execute_handler(msg)

    async def process(
        self,
        message: Union[T, PipelineMessage[T]],
        message_id: Optional[str] = None,
        channel: Optional[str] = None,
        chat_id: Optional[str] = None,
        user_id: Optional[str] = None,
        content: Optional[str] = None,
    ) -> PipelineResult:
        """
        Process a message through the pipeline.

        Args:
            message: Message to process (or PipelineMessage wrapper)
            message_id: Message ID (if not wrapped)
            channel: Channel name (if not wrapped)
            chat_id: Chat ID (if not wrapped)
            user_id: User ID (if not wrapped)
            content: Message content (if not wrapped)

        Returns:
            PipelineResult indicating outcome
        """
        if self._shutdown:
            return PipelineResult.REJECTED

        # Wrap message if needed
        if isinstance(message, PipelineMessage):
            msg = message
        else:
            msg = PipelineMessage(
                payload=message,
                message_id=message_id or str(id(message)),
                channel=channel or getattr(message, 'channel', 'default'),
                chat_id=chat_id or getattr(message, 'chat_id', 'default'),
                user_id=user_id or getattr(message, 'sender_id', getattr(message, 'user_id', 'default')),
                content=content or getattr(message, 'content', ''),
            )

        self._stats.total_received += 1

        try:
            # Stage 1: Debounce
            if self.config.enable_debounce and self._debouncer:
                msg.current_stage = PipelineStage.DEBOUNCE
                result = await self._debouncer.debounce(
                    msg,
                    key=msg.chat_id,
                    channel=msg.channel,
                )
                if result is None:
                    # Message is being debounced
                    self._stats.total_debounced += 1
                    return PipelineResult.DEBOUNCED

            # Continue processing
            return await self._process_after_debounce(msg)

        except Exception as e:
            logger.error(f"Pipeline error: {e}")
            self._stats.total_errors += 1
            msg.error = e
            msg.result = PipelineResult.ERROR
            return PipelineResult.ERROR

    async def _process_after_debounce(
        self,
        msg: PipelineMessage[T],
    ) -> PipelineResult:
        """Process message after debounce stage."""
        try:
            # Stage 2: Dedupe
            if self.config.enable_dedupe and self._deduper:
                msg.current_stage = PipelineStage.DEDUPE
                if self._deduper.check_and_mark(
                    msg,
                    message_id=msg.message_id,
                    content=msg.content,
                ):
                    self._stats.total_deduplicated += 1
                    msg.result = PipelineResult.DUPLICATE
                    return PipelineResult.DUPLICATE

            # Stage 3: Rate Limit
            if self.config.enable_rate_limit and self._rate_limiter:
                msg.current_stage = PipelineStage.RATE_LIMIT
                rate_result = self._rate_limiter.check_and_consume(
                    msg.channel,
                    msg.chat_id,
                )
                if not rate_result.allowed:
                    self._stats.total_rate_limited += 1
                    msg.result = PipelineResult.RATE_LIMITED
                    return PipelineResult.RATE_LIMITED

            # Stage 4: Concurrency
            if self.config.enable_concurrency and self._concurrency:
                msg.current_stage = PipelineStage.CONCURRENCY
                slot_id = await self._concurrency.acquire(
                    msg.channel,
                    msg.chat_id,
                    msg.user_id,
                    wait=self.config.concurrency.queue_when_limited,
                )
                if slot_id is None:
                    self._stats.total_concurrency_limited += 1
                    msg.result = PipelineResult.CONCURRENCY_LIMITED

                    # Queue if enabled
                    if self.config.enable_queue and self._queue:
                        return await self._queue_message(msg)

                    return PipelineResult.CONCURRENCY_LIMITED

                try:
                    return await self._process_with_slot(msg, slot_id)
                finally:
                    self._concurrency.release(slot_id=slot_id)
            else:
                # No concurrency control
                return await self._process_with_slot(msg, None)

        except Exception as e:
            logger.error(f"Pipeline processing error: {e}")
            self._stats.total_errors += 1
            msg.error = e
            msg.result = PipelineResult.ERROR
            return PipelineResult.ERROR

    async def _queue_message(
        self,
        msg: PipelineMessage[T],
    ) -> PipelineResult:
        """Queue a message for later processing."""
        msg.current_stage = PipelineStage.QUEUE

        if not self._queue:
            self._stats.total_rejected += 1
            return PipelineResult.REJECTED

        queued_msg = QueuedMessage(
            message_id=msg.message_id,
            channel=msg.channel,
            chat_id=msg.chat_id,
            sender_id=msg.user_id,
            content=msg.content,
            priority=msg.priority,
            metadata={"pipeline_message": msg},
        )

        if self._queue.enqueue(queued_msg):
            self._stats.total_queued += 1
            msg.result = PipelineResult.QUEUED
            return PipelineResult.QUEUED
        else:
            self._stats.total_rejected += 1
            msg.result = PipelineResult.REJECTED
            return PipelineResult.REJECTED

    async def _process_with_slot(
        self,
        msg: PipelineMessage[T],
        slot_id: Optional[str],
    ) -> PipelineResult:
        """Process message with acquired concurrency slot."""
        # Stage 5: Batch (optional)
        if self.config.enable_batch and self._batcher:
            msg.current_stage = PipelineStage.BATCH
            batch_result = await self._batcher.add(msg, key=msg.chat_id)
            if batch_result is None:
                # Message is batched, will be processed later
                self._stats.total_batched += 1
                msg.result = PipelineResult.BATCHED
                return PipelineResult.BATCHED
            # Batch was flushed - already handled by callback

        # Stage 6: Process
        return await self._execute_handler(msg)

    async def _execute_handler(
        self,
        msg: PipelineMessage[T],
    ) -> PipelineResult:
        """Execute the message handler."""
        msg.current_stage = PipelineStage.PROCESS

        if not self._handler:
            logger.warning("No handler set for pipeline")
            self._stats.total_rejected += 1
            return PipelineResult.REJECTED

        self._stats.current_in_flight += 1

        try:
            if self.config.enable_retry and self._retry_handler:
                await self._retry_handler.with_retry_async(
                    self._handler,
                    msg.payload,
                    on_retry=lambda attempt: self._on_retry(msg, attempt),
                )
            else:
                result = self._handler(msg.payload)
                if asyncio.iscoroutine(result):
                    await result

            self._stats.total_processed += 1
            self._stats.last_processed_at = datetime.now()
            msg.result = PipelineResult.PROCESSED
            return PipelineResult.PROCESSED

        except Exception as e:
            logger.error(f"Handler error: {e}")
            self._stats.total_errors += 1
            msg.error = e
            msg.result = PipelineResult.ERROR
            return PipelineResult.ERROR

        finally:
            self._stats.current_in_flight -= 1

    def _on_retry(self, msg: PipelineMessage[T], attempt: Any) -> None:
        """Handle retry attempt."""
        msg.retry_count += 1
        self._stats.total_retries += 1

    async def process_queued(self, max_items: int = 10) -> int:
        """
        Process queued messages.

        Args:
            max_items: Maximum items to process

        Returns:
            Number of messages processed
        """
        if not self._queue:
            return 0

        processed = 0
        for _ in range(max_items):
            queued = self._queue.dequeue()
            if queued is None:
                break

            msg = queued.metadata.get("pipeline_message")
            if msg:
                await self._process_after_debounce(msg)
                processed += 1

        return processed

    def get_stats(self) -> PipelineStats:
        """Get pipeline statistics."""
        return PipelineStats(
            total_received=self._stats.total_received,
            total_processed=self._stats.total_processed,
            total_debounced=self._stats.total_debounced,
            total_deduplicated=self._stats.total_deduplicated,
            total_rate_limited=self._stats.total_rate_limited,
            total_concurrency_limited=self._stats.total_concurrency_limited,
            total_queued=self._stats.total_queued,
            total_batched=self._stats.total_batched,
            total_rejected=self._stats.total_rejected,
            total_errors=self._stats.total_errors,
            total_retries=self._stats.total_retries,
            last_processed_at=self._stats.last_processed_at,
            current_in_flight=self._stats.current_in_flight,
        )

    def get_component_stats(self) -> Dict[str, Any]:
        """Get statistics from all components."""
        stats = {}

        if self._debouncer:
            stats["debounce"] = self._debouncer.get_stats().__dict__

        if self._deduper:
            stats["dedupe"] = self._deduper.get_stats().__dict__

        if self._rate_limiter:
            stats["rate_limit"] = self._rate_limiter.get_stats().__dict__

        if self._concurrency:
            usage = self._concurrency.get_usage()
            stats["concurrency"] = {
                "current_global": usage.current_global,
                "total_acquired": usage.total_acquired,
                "total_rejected": usage.total_rejected,
            }

        if self._queue:
            stats["queue"] = self._queue.get_stats().to_dict()

        if self._batcher:
            stats["batch"] = self._batcher.get_stats().to_dict()

        if self._retry_handler:
            stats["retry"] = self._retry_handler.get_stats().__dict__

        return stats

    def get_queue_size(self) -> int:
        """Get current queue size."""
        if self._queue:
            return self._queue.size
        return 0

    def get_pending_count(self) -> int:
        """Get total pending messages (debounced + queued + batched)."""
        count = 0

        if self._debouncer:
            count += self._debouncer.get_pending_count()

        if self._queue:
            count += self._queue.size

        if self._batcher:
            count += self._batcher.get_pending_count()

        return count

    async def flush_all(self) -> Dict[str, int]:
        """
        Flush all pending items in the pipeline.

        Returns:
            Dict with counts of flushed items per stage
        """
        flushed = {}

        if self._debouncer:
            result = self._debouncer.flush_all()
            flushed["debounce"] = sum(len(items) for items in result.values())

        if self._batcher:
            results = await self._batcher.flush_all()
            flushed["batch"] = sum(r.batch_size for r in results)

        return flushed

    def reset_stats(self) -> None:
        """Reset all pipeline statistics."""
        self._stats = PipelineStats()

        if self._retry_handler:
            self._retry_handler.reset_stats()

    async def shutdown(self) -> None:
        """Gracefully shutdown the pipeline."""
        self._shutdown = True

        # Flush remaining items
        await self.flush_all()

        # Process queued messages
        if self._queue:
            await self.process_queued(max_items=self._queue.size)

        # Clear components
        if self._debouncer:
            self._debouncer.clear()

        if self._deduper:
            self._deduper.clear()

        if self._concurrency:
            self._concurrency.clear()

        if self._queue:
            self._queue.clear()

        if self._batcher:
            self._batcher.clear()


class SyncMessagePipeline(Generic[T]):
    """
    Synchronous version of MessagePipeline.

    For use in non-async contexts.
    """

    def __init__(
        self,
        config: PipelineConfig,
        handler: Optional[Callable[[T], Any]] = None,
    ):
        self.config = config
        self._handler = handler
        self._lock = threading.Lock()
        self._stats = PipelineStats()
        self._shutdown = False

        # Initialize sync components
        self._debouncer: Optional[SyncDebouncer[PipelineMessage[T]]] = None
        self._deduper: Optional[MessageDeduplicator[PipelineMessage[T]]] = None
        self._rate_limiter: Optional[RateLimiter] = None
        self._concurrency: Optional[ConcurrencyController] = None
        self._queue: Optional[MessageQueue] = None
        self._retry_handler: Optional[RetryHandler] = None

        self._init_components()

    def _init_components(self) -> None:
        """Initialize components."""
        if self.config.enable_debounce:
            self._debouncer = SyncDebouncer(
                self.config.debounce,
                on_flush=self._on_debounce_flush,
            )

        if self.config.enable_dedupe:
            self._deduper = MessageDeduplicator(self.config.dedupe)

        if self.config.enable_rate_limit:
            self._rate_limiter = RateLimiter(self.config.rate_limit)

        if self.config.enable_concurrency:
            self._concurrency = ConcurrencyController(self.config.concurrency)

        if self.config.enable_queue:
            self._queue = MessageQueue(self.config.queue)

        if self.config.enable_retry:
            self._retry_handler = RetryHandler(self.config.retry)

    def set_handler(self, handler: Callable[[T], Any]) -> None:
        """Set message handler."""
        self._handler = handler

    def _on_debounce_flush(self, messages: List[PipelineMessage[T]]) -> None:
        """Handle debounce flush."""
        for msg in messages:
            self._process_after_debounce(msg)

    def process(
        self,
        message: Union[T, PipelineMessage[T]],
        message_id: Optional[str] = None,
        channel: Optional[str] = None,
        chat_id: Optional[str] = None,
        user_id: Optional[str] = None,
        content: Optional[str] = None,
    ) -> PipelineResult:
        """
        Process a message through the pipeline.

        Returns:
            PipelineResult indicating outcome
        """
        if self._shutdown:
            return PipelineResult.REJECTED

        # Wrap message
        if isinstance(message, PipelineMessage):
            msg = message
        else:
            msg = PipelineMessage(
                payload=message,
                message_id=message_id or str(id(message)),
                channel=channel or getattr(message, 'channel', 'default'),
                chat_id=chat_id or getattr(message, 'chat_id', 'default'),
                user_id=user_id or getattr(message, 'sender_id', getattr(message, 'user_id', 'default')),
                content=content or getattr(message, 'content', ''),
            )

        self._stats.total_received += 1

        try:
            # Stage 1: Debounce
            if self.config.enable_debounce and self._debouncer:
                result = self._debouncer.debounce(msg, key=msg.chat_id, channel=msg.channel)
                if result is None:
                    self._stats.total_debounced += 1
                    return PipelineResult.DEBOUNCED

            return self._process_after_debounce(msg)

        except Exception as e:
            logger.error(f"Pipeline error: {e}")
            self._stats.total_errors += 1
            return PipelineResult.ERROR

    def _process_after_debounce(self, msg: PipelineMessage[T]) -> PipelineResult:
        """Process after debounce."""
        # Stage 2: Dedupe
        if self.config.enable_dedupe and self._deduper:
            if self._deduper.check_and_mark(msg, message_id=msg.message_id, content=msg.content):
                self._stats.total_deduplicated += 1
                return PipelineResult.DUPLICATE

        # Stage 3: Rate Limit
        if self.config.enable_rate_limit and self._rate_limiter:
            rate_result = self._rate_limiter.check_and_consume(msg.channel, msg.chat_id)
            if not rate_result.allowed:
                self._stats.total_rate_limited += 1
                return PipelineResult.RATE_LIMITED

        # Stage 4: Concurrency
        if self.config.enable_concurrency and self._concurrency:
            slot_id = self._concurrency.acquire_sync(msg.channel, msg.chat_id, msg.user_id)
            if slot_id is None:
                self._stats.total_concurrency_limited += 1

                if self.config.enable_queue and self._queue:
                    return self._queue_message(msg)

                return PipelineResult.CONCURRENCY_LIMITED

            try:
                return self._execute_handler(msg)
            finally:
                self._concurrency.release(slot_id=slot_id)
        else:
            return self._execute_handler(msg)

    def _queue_message(self, msg: PipelineMessage[T]) -> PipelineResult:
        """Queue a message."""
        if not self._queue:
            self._stats.total_rejected += 1
            return PipelineResult.REJECTED

        queued_msg = QueuedMessage(
            message_id=msg.message_id,
            channel=msg.channel,
            chat_id=msg.chat_id,
            sender_id=msg.user_id,
            content=msg.content,
            priority=msg.priority,
            metadata={"pipeline_message": msg},
        )

        if self._queue.enqueue(queued_msg):
            self._stats.total_queued += 1
            return PipelineResult.QUEUED
        else:
            self._stats.total_rejected += 1
            return PipelineResult.REJECTED

    def _execute_handler(self, msg: PipelineMessage[T]) -> PipelineResult:
        """Execute handler."""
        if not self._handler:
            self._stats.total_rejected += 1
            return PipelineResult.REJECTED

        self._stats.current_in_flight += 1

        try:
            if self.config.enable_retry and self._retry_handler:
                self._retry_handler.with_retry(
                    self._handler,
                    msg.payload,
                    on_retry=lambda attempt: self._on_retry(msg, attempt),
                )
            else:
                self._handler(msg.payload)

            self._stats.total_processed += 1
            self._stats.last_processed_at = datetime.now()
            return PipelineResult.PROCESSED

        except Exception as e:
            logger.error(f"Handler error: {e}")
            self._stats.total_errors += 1
            return PipelineResult.ERROR

        finally:
            self._stats.current_in_flight -= 1

    def _on_retry(self, msg: PipelineMessage[T], attempt: Any) -> None:
        """Handle retry."""
        msg.retry_count += 1
        self._stats.total_retries += 1

    def get_stats(self) -> PipelineStats:
        """Get pipeline statistics."""
        return PipelineStats(
            total_received=self._stats.total_received,
            total_processed=self._stats.total_processed,
            total_debounced=self._stats.total_debounced,
            total_deduplicated=self._stats.total_deduplicated,
            total_rate_limited=self._stats.total_rate_limited,
            total_concurrency_limited=self._stats.total_concurrency_limited,
            total_queued=self._stats.total_queued,
            total_batched=self._stats.total_batched,
            total_rejected=self._stats.total_rejected,
            total_errors=self._stats.total_errors,
            total_retries=self._stats.total_retries,
            last_processed_at=self._stats.last_processed_at,
            current_in_flight=self._stats.current_in_flight,
        )

    def process_queued(self, max_items: int = 10) -> int:
        """Process queued messages."""
        if not self._queue:
            return 0

        processed = 0
        for _ in range(max_items):
            queued = self._queue.dequeue()
            if queued is None:
                break

            msg = queued.metadata.get("pipeline_message")
            if msg:
                self._process_after_debounce(msg)
                processed += 1

        return processed

    def shutdown(self) -> None:
        """Shutdown pipeline."""
        self._shutdown = True

        if self._debouncer:
            self._debouncer.flush_all()

        if self._queue:
            self.process_queued(max_items=self._queue.size)

        if self._concurrency:
            self._concurrency.clear()
