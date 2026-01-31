"""
Tests for Retry Logic System
"""

import pytest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.channels.queue.retry import (
    RetryResult,
    RetryConfig,
    RetryStats,
    RetryAttempt,
    RetryHandler,
)

# Configure pytest-asyncio
pytestmark = pytest.mark.asyncio(loop_scope="function")


class TestRetryConfig:
    """Tests for RetryConfig."""

    def test_default_config(self):
        config = RetryConfig()
        assert config.max_retries == 3
        assert config.initial_delay_ms == 1000
        assert config.exponential_base == 2.0
        assert config.jitter is True

    def test_custom_config(self):
        config = RetryConfig(max_retries=5, initial_delay_ms=500)
        assert config.max_retries == 5
        assert config.initial_delay_ms == 500


class TestRetryHandler:
    """Tests for RetryHandler."""

    @pytest.fixture
    def handler(self):
        config = RetryConfig(
            max_retries=3,
            initial_delay_ms=10,  # Fast for tests
            max_delay_ms=100,
            jitter=False,
        )
        return RetryHandler(config)

    def test_success_first_try(self, handler):
        def success_func():
            return "success"

        result = handler.with_retry(success_func)
        assert result == "success"
        stats = handler.get_stats()
        assert stats.total_successes == 1
        assert stats.total_retries == 0

    def test_retry_on_failure(self, handler):
        attempts = [0]

        def fail_then_succeed():
            attempts[0] += 1
            if attempts[0] < 2:
                raise ValueError("Transient error")
            return "success"

        result = handler.with_retry(fail_then_succeed)
        assert result == "success"
        assert attempts[0] == 2
        stats = handler.get_stats()
        assert stats.total_retries == 1

    def test_max_retries_exhausted(self, handler):
        def always_fail():
            raise ValueError("Always fails")

        with pytest.raises(ValueError):
            handler.with_retry(always_fail)

        stats = handler.get_stats()
        assert stats.total_failures == 1
        assert stats.total_retries == 3

    def test_non_retryable_error(self):
        config = RetryConfig(
            max_retries=3,
            initial_delay_ms=10,
            non_retryable_exceptions=[TypeError],
        )
        handler = RetryHandler(config)

        def raise_type_error():
            raise TypeError("Non-retryable")

        with pytest.raises(TypeError):
            handler.with_retry(raise_type_error)

        stats = handler.get_stats()
        assert stats.total_retries == 0  # No retries for non-retryable

    def test_custom_should_retry(self, handler):
        attempts = [0]

        def sometimes_fail():
            attempts[0] += 1
            if attempts[0] < 3:
                raise ValueError("Retry this")
            return "success"

        # Only retry ValueError
        result = handler.with_retry(
            sometimes_fail,
            should_retry=lambda e, a: isinstance(e, ValueError)
        )
        assert result == "success"

    def test_on_retry_callback(self, handler):
        retries = []

        def fail_once():
            if len(retries) < 1:
                raise ValueError("Fail")
            return "success"

        def on_retry(attempt: RetryAttempt):
            retries.append(attempt)

        handler.with_retry(fail_once, on_retry=on_retry)
        assert len(retries) == 1
        assert retries[0].attempt == 1

    def test_calculate_delay(self, handler):
        # With exponential base 2 and initial 10ms
        delay0 = handler.calculate_delay(0)
        delay1 = handler.calculate_delay(1)
        delay2 = handler.calculate_delay(2)

        assert delay0 == 10
        assert delay1 == 20
        assert delay2 == 40

    def test_delay_capped_at_max(self, handler):
        delay = handler.calculate_delay(10)  # Would be very large
        assert delay <= handler.config.max_delay_ms


class TestRetryHandlerAsync:
    """Tests for async retry operations."""

    @pytest.fixture
    def handler(self):
        config = RetryConfig(
            max_retries=3,
            initial_delay_ms=10,
            jitter=False,
        )
        return RetryHandler(config)

    @pytest.mark.asyncio
    async def test_async_success(self, handler):
        async def async_success():
            return "async_success"

        result = await handler.with_retry_async(async_success)
        assert result == "async_success"

    @pytest.mark.asyncio
    async def test_async_retry_on_failure(self, handler):
        attempts = [0]

        async def fail_then_succeed():
            attempts[0] += 1
            if attempts[0] < 2:
                raise ValueError("Transient")
            return "success"

        result = await handler.with_retry_async(fail_then_succeed)
        assert result == "success"
        assert attempts[0] == 2

    @pytest.mark.asyncio
    async def test_async_exhausted(self, handler):
        async def always_fail():
            raise ValueError("Always fails")

        with pytest.raises(ValueError):
            await handler.with_retry_async(always_fail)


class TestRetryStats:
    """Tests for RetryStats."""

    def test_default_stats(self):
        stats = RetryStats()
        assert stats.total_attempts == 0
        assert stats.total_successes == 0
        assert stats.total_failures == 0

    def test_reset_stats(self):
        config = RetryConfig(max_retries=1, initial_delay_ms=1)
        handler = RetryHandler(config)

        handler.with_retry(lambda: "ok")
        handler.reset_stats()

        stats = handler.get_stats()
        assert stats.total_attempts == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
