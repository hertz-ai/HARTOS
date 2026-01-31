"""
Message Queue Package

Provides message queue infrastructure for channel messaging.

Components:
- Message Queue: Queue policies (DROP, LATEST, BACKLOG, PRIORITY, COLLECT)
- Debounce: Collect rapid-fire messages into batches
- Dedupe: Prevent duplicate message processing
- Concurrency: Control concurrent processing limits
- Rate Limit: Prevent abuse and comply with API limits
- Retry: Handle transient failures with backoff
"""

from .message_queue import (
    QueuePolicy,
    DropPolicy,
    DedupeMode as QueueDedupeMode,
    QueuedMessage,
    QueueConfig,
    QueueStats,
    MessageQueue,
    QueueManager,
    get_queue_manager,
)

from .debounce import (
    DebounceConfig,
    DebounceStats,
    DebounceBuffer,
    InboundDebouncer,
    SyncDebouncer,
)

from .dedupe import (
    DedupeMode,
    DedupeConfig,
    DedupeStats,
    DedupeEntry,
    MessageDeduplicator,
    SimpleDeduplicator,
)

from .concurrency import (
    ConcurrencyLimits,
    ConcurrencyStats,
    ConcurrencySlot,
    ConcurrencyController,
)

from .rate_limit import (
    RateLimitResult,
    RateLimitConfig,
    RateLimitInfo,
    RateLimitStats,
    SlidingWindowCounter,
    TokenBucket,
    RateLimiter,
)

from .retry import (
    RetryResult,
    RetryConfig,
    RetryStats,
    RetryAttempt,
    RetryHandler,
    retry_async,
    retry_sync,
)

__all__ = [
    # Message Queue
    "QueuePolicy",
    "DropPolicy",
    "QueueDedupeMode",
    "QueuedMessage",
    "QueueConfig",
    "QueueStats",
    "MessageQueue",
    "QueueManager",
    "get_queue_manager",
    # Debounce
    "DebounceConfig",
    "DebounceStats",
    "DebounceBuffer",
    "InboundDebouncer",
    "SyncDebouncer",
    # Dedupe
    "DedupeMode",
    "DedupeConfig",
    "DedupeStats",
    "DedupeEntry",
    "MessageDeduplicator",
    "SimpleDeduplicator",
    # Concurrency
    "ConcurrencyLimits",
    "ConcurrencyStats",
    "ConcurrencySlot",
    "ConcurrencyController",
    # Rate Limit
    "RateLimitResult",
    "RateLimitConfig",
    "RateLimitInfo",
    "RateLimitStats",
    "SlidingWindowCounter",
    "TokenBucket",
    "RateLimiter",
    # Retry
    "RetryResult",
    "RetryConfig",
    "RetryStats",
    "RetryAttempt",
    "RetryHandler",
    "retry_async",
    "retry_sync",
]
