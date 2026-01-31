"""
Tests for Rate Limiting System
"""

import pytest
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.channels.queue.rate_limit import (
    RateLimitResult,
    RateLimitConfig,
    RateLimitInfo,
    RateLimitStats,
    SlidingWindowCounter,
    TokenBucket,
    RateLimiter,
)


class TestSlidingWindowCounter:
    """Tests for SlidingWindowCounter."""

    def test_check_under_limit(self):
        counter = SlidingWindowCounter(window_seconds=60, max_requests=10)
        allowed, remaining = counter.check()
        assert allowed is True
        assert remaining == 10

    def test_consume_decreases_remaining(self):
        counter = SlidingWindowCounter(window_seconds=60, max_requests=10)
        counter.consume()
        allowed, remaining = counter.check()
        assert allowed is True
        assert remaining == 9

    def test_at_limit_blocks(self):
        counter = SlidingWindowCounter(window_seconds=60, max_requests=2)
        counter.consume()
        counter.consume()
        result = counter.consume()
        assert result is False

    def test_reset(self):
        counter = SlidingWindowCounter(window_seconds=60, max_requests=2)
        counter.consume()
        counter.consume()
        counter.reset()
        assert counter.get_remaining() == 2


class TestTokenBucket:
    """Tests for TokenBucket."""

    def test_consume_from_full(self):
        bucket = TokenBucket(capacity=10, refill_rate=1.0)
        assert bucket.consume() is True
        assert bucket.get_tokens() == 9.0

    def test_consume_multiple(self):
        bucket = TokenBucket(capacity=10, refill_rate=1.0)
        assert bucket.consume(5) is True
        assert bucket.get_tokens() == 5.0

    def test_consume_at_empty(self):
        bucket = TokenBucket(capacity=2, refill_rate=0.1)
        bucket.consume(2)
        assert bucket.consume() is False

    def test_reset(self):
        bucket = TokenBucket(capacity=10, refill_rate=1.0)
        bucket.consume(10)
        bucket.reset()
        assert bucket.get_tokens() == 10.0


class TestRateLimiter:
    """Tests for RateLimiter."""

    @pytest.fixture
    def limiter(self):
        config = RateLimitConfig(
            requests_per_minute=10,
            requests_per_hour=100,
            burst_limit=5,
        )
        return RateLimiter(config)

    def test_check_under_limit(self, limiter):
        info = limiter.check("telegram", "chat1")
        assert info.allowed is True
        assert info.result == RateLimitResult.ALLOWED

    def test_consume(self, limiter):
        result = limiter.consume("telegram", "chat1")
        assert result is True
        remaining = limiter.get_remaining("telegram", "chat1")
        assert remaining[0] == 9  # minute remaining

    def test_check_and_consume(self, limiter):
        info = limiter.check_and_consume("telegram", "chat1")
        assert info.allowed is True
        remaining = limiter.get_remaining("telegram", "chat1")
        assert remaining[0] == 9

    def test_burst_limit(self, limiter):
        # Exhaust burst
        for _ in range(5):
            limiter.consume("telegram", "chat1")
        info = limiter.check("telegram", "chat1")
        assert info.allowed is False
        assert info.result == RateLimitResult.BURST_EXCEEDED

    def test_per_channel_limits(self):
        config = RateLimitConfig(
            requests_per_minute=10,
            per_channel_limits={"slow_channel": 2},
        )
        limiter = RateLimiter(config)

        # Slow channel has limit of 2
        limiter.consume("slow_channel", "chat1")
        limiter.consume("slow_channel", "chat1")
        info = limiter.check("slow_channel", "chat1")
        assert info.remaining_minute == 0

    def test_reset(self, limiter):
        for _ in range(3):
            limiter.consume("telegram", "chat1")
        limiter.reset("telegram", "chat1")
        remaining = limiter.get_remaining("telegram", "chat1")
        assert remaining[0] == 10

    def test_get_stats(self, limiter):
        limiter.check("telegram", "chat1")
        limiter.check("telegram", "chat2")
        stats = limiter.get_stats()
        assert stats.total_requests == 2
        assert stats.total_allowed == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
