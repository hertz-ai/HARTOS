"""
Rate Limiting System

Limits request rates to prevent abuse and comply with API limits.
Ported from HevolveBot's src/channels/rate-limit.ts.

Features:
- Sliding window rate limiting
- Per-channel limits
- Burst handling
- Token bucket algorithm
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Dict, Deque, Tuple

logger = logging.getLogger(__name__)


class RateLimitResult(Enum):
    """Result of rate limit check."""
    ALLOWED = "allowed"
    RATE_LIMITED = "rate_limited"
    BURST_EXCEEDED = "burst_exceeded"


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""
    requests_per_minute: int = 60
    requests_per_hour: int = 1000
    burst_limit: int = 10
    burst_window_seconds: int = 1
    per_channel_limits: Dict[str, int] = field(default_factory=dict)


@dataclass
class RateLimitInfo:
    """Information about current rate limit state."""
    allowed: bool
    result: RateLimitResult
    remaining_minute: int
    remaining_hour: int
    remaining_burst: int
    reset_minute_at: datetime
    reset_hour_at: datetime
    retry_after_seconds: Optional[float] = None


@dataclass
class RateLimitStats:
    """Statistics for rate limiter."""
    total_requests: int = 0
    total_allowed: int = 0
    total_rate_limited: int = 0
    total_burst_exceeded: int = 0


class SlidingWindowCounter:
    """Sliding window counter for rate limiting."""

    def __init__(self, window_seconds: int, max_requests: int):
        self.window_seconds = window_seconds
        self.max_requests = max_requests
        self._timestamps: Deque[float] = deque()
        self._lock = threading.Lock()

    def _cleanup(self, now: float) -> None:
        """Remove expired timestamps."""
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def check(self) -> Tuple[bool, int]:
        """
        Check if request is allowed.

        Returns:
            Tuple of (allowed, remaining)
        """
        now = time.time()
        with self._lock:
            self._cleanup(now)
            remaining = max(0, self.max_requests - len(self._timestamps))
            return remaining > 0, remaining

    def consume(self) -> bool:
        """
        Consume one request slot.

        Returns:
            True if consumed, False if at limit
        """
        now = time.time()
        with self._lock:
            self._cleanup(now)
            if len(self._timestamps) >= self.max_requests:
                return False
            self._timestamps.append(now)
            return True

    def get_remaining(self) -> int:
        """Get remaining requests in window."""
        now = time.time()
        with self._lock:
            self._cleanup(now)
            return max(0, self.max_requests - len(self._timestamps))

    def get_reset_time(self) -> float:
        """Get time until window resets (oldest request expires)."""
        now = time.time()
        with self._lock:
            self._cleanup(now)
            if not self._timestamps:
                return 0
            oldest = self._timestamps[0]
            reset_at = oldest + self.window_seconds
            return max(0, reset_at - now)

    def reset(self) -> None:
        """Reset the counter."""
        with self._lock:
            self._timestamps.clear()


class TokenBucket:
    """Token bucket for burst handling."""

    def __init__(self, capacity: int, refill_rate: float):
        """
        Args:
            capacity: Maximum tokens in bucket
            refill_rate: Tokens added per second
        """
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._tokens = float(capacity)
        self._last_refill = time.time()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self._last_refill
        new_tokens = elapsed * self.refill_rate
        self._tokens = min(self.capacity, self._tokens + new_tokens)
        self._last_refill = now

    def consume(self, tokens: int = 1) -> bool:
        """
        Try to consume tokens.

        Returns:
            True if consumed, False if not enough tokens
        """
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def get_tokens(self) -> float:
        """Get current token count."""
        with self._lock:
            self._refill()
            return self._tokens

    def reset(self) -> None:
        """Reset to full capacity."""
        with self._lock:
            self._tokens = float(self.capacity)
            self._last_refill = time.time()


class RateLimiter:
    """
    Rate limiter with multiple windows and burst handling.

    Usage:
        config = RateLimitConfig(requests_per_minute=60, burst_limit=10)
        limiter = RateLimiter(config)

        # Check if request is allowed
        result = limiter.check("telegram", "chat123")
        if result.allowed:
            # Consume the slot
            limiter.consume("telegram", "chat123")
            # Process request
        else:
            # Handle rate limit
            print(f"Retry after {result.retry_after_seconds} seconds")
    """

    def __init__(self, config: RateLimitConfig):
        self.config = config

        # Per-key rate limiters
        self._minute_counters: Dict[str, SlidingWindowCounter] = {}
        self._hour_counters: Dict[str, SlidingWindowCounter] = {}
        self._burst_buckets: Dict[str, TokenBucket] = {}

        self._lock = threading.Lock()
        self._stats = RateLimitStats()

    def _get_key(self, channel: str, chat_id: str) -> str:
        """Get rate limit key."""
        return f"{channel}:{chat_id}"

    def _get_limits(self, channel: str) -> Tuple[int, int, int]:
        """Get limits for a channel."""
        per_minute = self.config.per_channel_limits.get(
            channel,
            self.config.requests_per_minute
        )
        per_hour = self.config.requests_per_hour
        burst = self.config.burst_limit
        return per_minute, per_hour, burst

    def _get_or_create_counters(
        self,
        key: str,
        channel: str,
    ) -> Tuple[SlidingWindowCounter, SlidingWindowCounter, TokenBucket]:
        """Get or create rate limit counters for a key."""
        per_minute, per_hour, burst = self._get_limits(channel)

        with self._lock:
            if key not in self._minute_counters:
                self._minute_counters[key] = SlidingWindowCounter(60, per_minute)
            if key not in self._hour_counters:
                self._hour_counters[key] = SlidingWindowCounter(3600, per_hour)
            if key not in self._burst_buckets:
                # Refill at minute rate
                refill_rate = per_minute / 60.0
                self._burst_buckets[key] = TokenBucket(burst, refill_rate)

            return (
                self._minute_counters[key],
                self._hour_counters[key],
                self._burst_buckets[key],
            )

    def check(self, channel: str, chat_id: str) -> RateLimitInfo:
        """
        Check if a request is allowed.

        Args:
            channel: Channel name
            chat_id: Chat identifier

        Returns:
            RateLimitInfo with result and remaining quotas
        """
        key = self._get_key(channel, chat_id)
        minute_counter, hour_counter, burst_bucket = self._get_or_create_counters(
            key, channel
        )

        self._stats.total_requests += 1

        # Check burst limit
        burst_tokens = int(burst_bucket.get_tokens())
        if burst_tokens <= 0:
            self._stats.total_burst_exceeded += 1
            return RateLimitInfo(
                allowed=False,
                result=RateLimitResult.BURST_EXCEEDED,
                remaining_minute=minute_counter.get_remaining(),
                remaining_hour=hour_counter.get_remaining(),
                remaining_burst=0,
                reset_minute_at=datetime.now() + timedelta(seconds=minute_counter.get_reset_time()),
                reset_hour_at=datetime.now() + timedelta(seconds=hour_counter.get_reset_time()),
                retry_after_seconds=self.config.burst_window_seconds,
            )

        # Check minute limit
        minute_allowed, minute_remaining = minute_counter.check()
        if not minute_allowed:
            self._stats.total_rate_limited += 1
            return RateLimitInfo(
                allowed=False,
                result=RateLimitResult.RATE_LIMITED,
                remaining_minute=0,
                remaining_hour=hour_counter.get_remaining(),
                remaining_burst=burst_tokens,
                reset_minute_at=datetime.now() + timedelta(seconds=minute_counter.get_reset_time()),
                reset_hour_at=datetime.now() + timedelta(seconds=hour_counter.get_reset_time()),
                retry_after_seconds=minute_counter.get_reset_time(),
            )

        # Check hour limit
        hour_allowed, hour_remaining = hour_counter.check()
        if not hour_allowed:
            self._stats.total_rate_limited += 1
            return RateLimitInfo(
                allowed=False,
                result=RateLimitResult.RATE_LIMITED,
                remaining_minute=minute_remaining,
                remaining_hour=0,
                remaining_burst=burst_tokens,
                reset_minute_at=datetime.now() + timedelta(seconds=minute_counter.get_reset_time()),
                reset_hour_at=datetime.now() + timedelta(seconds=hour_counter.get_reset_time()),
                retry_after_seconds=hour_counter.get_reset_time(),
            )

        self._stats.total_allowed += 1
        return RateLimitInfo(
            allowed=True,
            result=RateLimitResult.ALLOWED,
            remaining_minute=minute_remaining,
            remaining_hour=hour_remaining,
            remaining_burst=burst_tokens,
            reset_minute_at=datetime.now() + timedelta(seconds=60),
            reset_hour_at=datetime.now() + timedelta(seconds=3600),
        )

    def consume(self, channel: str, chat_id: str) -> bool:
        """
        Consume a rate limit slot.

        Args:
            channel: Channel name
            chat_id: Chat identifier

        Returns:
            True if consumed, False if at limit
        """
        key = self._get_key(channel, chat_id)
        minute_counter, hour_counter, burst_bucket = self._get_or_create_counters(
            key, channel
        )

        # Try to consume from all
        if not burst_bucket.consume():
            return False
        if not minute_counter.consume():
            return False
        if not hour_counter.consume():
            return False

        return True

    def check_and_consume(self, channel: str, chat_id: str) -> RateLimitInfo:
        """
        Check and consume in one operation.

        Returns:
            RateLimitInfo with result
        """
        info = self.check(channel, chat_id)
        if info.allowed:
            self.consume(channel, chat_id)
        return info

    def get_remaining(self, channel: str, chat_id: str) -> Tuple[int, int, int]:
        """
        Get remaining quotas.

        Returns:
            Tuple of (remaining_minute, remaining_hour, remaining_burst)
        """
        key = self._get_key(channel, chat_id)
        minute_counter, hour_counter, burst_bucket = self._get_or_create_counters(
            key, channel
        )
        return (
            minute_counter.get_remaining(),
            hour_counter.get_remaining(),
            int(burst_bucket.get_tokens()),
        )

    def reset(self, channel: str, chat_id: str) -> None:
        """Reset rate limits for a specific chat."""
        key = self._get_key(channel, chat_id)
        with self._lock:
            if key in self._minute_counters:
                self._minute_counters[key].reset()
            if key in self._hour_counters:
                self._hour_counters[key].reset()
            if key in self._burst_buckets:
                self._burst_buckets[key].reset()

    def reset_all(self) -> None:
        """Reset all rate limits."""
        with self._lock:
            for counter in self._minute_counters.values():
                counter.reset()
            for counter in self._hour_counters.values():
                counter.reset()
            for bucket in self._burst_buckets.values():
                bucket.reset()

    def get_stats(self) -> RateLimitStats:
        """Get rate limiter statistics."""
        return RateLimitStats(
            total_requests=self._stats.total_requests,
            total_allowed=self._stats.total_allowed,
            total_rate_limited=self._stats.total_rate_limited,
            total_burst_exceeded=self._stats.total_burst_exceeded,
        )
