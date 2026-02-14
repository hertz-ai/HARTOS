"""
Retry Logic with Exponential Backoff

Provides retry functionality for transient failures.
Ported from HevolveBot's src/infra/retry.ts.

Features:
- Exponential backoff
- Jitter to prevent thundering herd
- Configurable retry conditions
- Async and sync support
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Callable, Any, TypeVar, List, Type, Union

logger = logging.getLogger(__name__)

T = TypeVar('T')


class RetryResult(Enum):
    """Result of a retry operation."""
    SUCCESS = "success"
    EXHAUSTED = "exhausted"  # Max retries reached
    NON_RETRYABLE = "non_retryable"  # Error is not retryable


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""
    max_retries: int = 3
    initial_delay_ms: int = 1000
    max_delay_ms: int = 30000
    exponential_base: float = 2.0
    jitter: bool = True
    jitter_factor: float = 0.1
    retryable_exceptions: List[Type[Exception]] = field(default_factory=lambda: [Exception])
    non_retryable_exceptions: List[Type[Exception]] = field(default_factory=list)


@dataclass
class RetryStats:
    """Statistics for retry operations."""
    total_attempts: int = 0
    total_successes: int = 0
    total_failures: int = 0
    total_retries: int = 0
    last_error: Optional[str] = None
    last_attempt_at: Optional[datetime] = None


@dataclass
class RetryAttempt:
    """Information about a retry attempt."""
    attempt: int
    delay_ms: int
    error: Optional[Exception] = None
    timestamp: datetime = field(default_factory=datetime.now)


class RetryHandler:
    """
    Handles retry logic with exponential backoff.

    Usage:
        config = RetryConfig(max_retries=3, initial_delay_ms=1000)
        handler = RetryHandler(config)

        # Async usage
        result = await handler.with_retry_async(some_async_function, arg1, arg2)

        # Sync usage
        result = handler.with_retry(some_function, arg1, arg2)

        # With custom should_retry
        result = await handler.with_retry_async(
            some_function,
            should_retry=lambda e, attempt: isinstance(e, TimeoutError)
        )
    """

    def __init__(self, config: RetryConfig):
        self.config = config
        self._stats = RetryStats()

    def calculate_delay(self, attempt: int) -> int:
        """
        Calculate delay for a retry attempt.

        Args:
            attempt: Attempt number (0-indexed)

        Returns:
            Delay in milliseconds
        """
        # Exponential backoff
        delay = self.config.initial_delay_ms * (self.config.exponential_base ** attempt)

        # Cap at max delay
        delay = min(delay, self.config.max_delay_ms)

        # Add jitter if enabled
        if self.config.jitter:
            jitter_range = delay * self.config.jitter_factor
            jitter = random.uniform(-jitter_range, jitter_range)
            delay = max(0, delay + jitter)

        return int(delay)

    def should_retry(
        self,
        error: Exception,
        attempt: int,
        custom_check: Optional[Callable[[Exception, int], bool]] = None,
    ) -> bool:
        """
        Determine if an error should be retried.

        Args:
            error: The exception that occurred
            attempt: Current attempt number
            custom_check: Optional custom retry check function

        Returns:
            True if should retry, False otherwise
        """
        # Check max retries
        if attempt >= self.config.max_retries:
            return False

        # Check custom function first
        if custom_check is not None:
            return custom_check(error, attempt)

        # Check non-retryable exceptions
        for exc_type in self.config.non_retryable_exceptions:
            if isinstance(error, exc_type):
                return False

        # Check retryable exceptions
        for exc_type in self.config.retryable_exceptions:
            if isinstance(error, exc_type):
                return True

        return False

    async def with_retry_async(
        self,
        func: Callable[..., Any],
        *args,
        should_retry: Optional[Callable[[Exception, int], bool]] = None,
        on_retry: Optional[Callable[[RetryAttempt], None]] = None,
        **kwargs,
    ) -> Any:
        """
        Execute a function with retry logic (async).

        Args:
            func: Function to execute (can be sync or async)
            *args: Function arguments
            should_retry: Optional custom retry check
            on_retry: Optional callback on each retry
            **kwargs: Function keyword arguments

        Returns:
            Function result

        Raises:
            Last exception if all retries exhausted
        """
        last_error: Optional[Exception] = None

        for attempt in range(self.config.max_retries + 1):
            self._stats.total_attempts += 1
            self._stats.last_attempt_at = datetime.now()

            try:
                # Execute function
                result = func(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    result = await result

                self._stats.total_successes += 1
                return result

            except Exception as e:
                last_error = e
                self._stats.last_error = str(e)

                # Check if should retry
                if not self.should_retry(e, attempt, should_retry):
                    self._stats.total_failures += 1
                    raise

                # Calculate delay
                delay_ms = self.calculate_delay(attempt)

                # Create attempt info
                retry_attempt = RetryAttempt(
                    attempt=attempt + 1,
                    delay_ms=delay_ms,
                    error=e,
                )

                # Call on_retry callback
                if on_retry:
                    on_retry(retry_attempt)

                logger.debug(
                    f"Retry attempt {attempt + 1}/{self.config.max_retries}: "
                    f"waiting {delay_ms}ms after error: {e}"
                )

                self._stats.total_retries += 1

                # Wait before retry
                await asyncio.sleep(delay_ms / 1000.0)

        # All retries exhausted
        self._stats.total_failures += 1
        if last_error:
            raise last_error
        raise RuntimeError("Retry exhausted without error")

    def with_retry(
        self,
        func: Callable[..., T],
        *args,
        should_retry: Optional[Callable[[Exception, int], bool]] = None,
        on_retry: Optional[Callable[[RetryAttempt], None]] = None,
        **kwargs,
    ) -> T:
        """
        Execute a function with retry logic (sync).

        Args:
            func: Function to execute
            *args: Function arguments
            should_retry: Optional custom retry check
            on_retry: Optional callback on each retry
            **kwargs: Function keyword arguments

        Returns:
            Function result

        Raises:
            Last exception if all retries exhausted
        """
        last_error: Optional[Exception] = None

        for attempt in range(self.config.max_retries + 1):
            self._stats.total_attempts += 1
            self._stats.last_attempt_at = datetime.now()

            try:
                result = func(*args, **kwargs)
                self._stats.total_successes += 1
                return result

            except Exception as e:
                last_error = e
                self._stats.last_error = str(e)

                if not self.should_retry(e, attempt, should_retry):
                    self._stats.total_failures += 1
                    raise

                delay_ms = self.calculate_delay(attempt)

                retry_attempt = RetryAttempt(
                    attempt=attempt + 1,
                    delay_ms=delay_ms,
                    error=e,
                )

                if on_retry:
                    on_retry(retry_attempt)

                logger.debug(
                    f"Retry attempt {attempt + 1}/{self.config.max_retries}: "
                    f"waiting {delay_ms}ms after error: {e}"
                )

                self._stats.total_retries += 1
                time.sleep(delay_ms / 1000.0)

        self._stats.total_failures += 1
        if last_error:
            raise last_error
        raise RuntimeError("Retry exhausted without error")

    def get_stats(self) -> RetryStats:
        """Get retry statistics."""
        return RetryStats(
            total_attempts=self._stats.total_attempts,
            total_successes=self._stats.total_successes,
            total_failures=self._stats.total_failures,
            total_retries=self._stats.total_retries,
            last_error=self._stats.last_error,
            last_attempt_at=self._stats.last_attempt_at,
        )

    def reset_stats(self) -> None:
        """Reset statistics."""
        self._stats = RetryStats()


def retry_async(
    max_retries: int = 3,
    initial_delay_ms: int = 1000,
    exponential_base: float = 2.0,
    jitter: bool = True,
):
    """
    Decorator for async functions with retry.

    Usage:
        @retry_async(max_retries=3)
        async def my_function():
            ...
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        config = RetryConfig(
            max_retries=max_retries,
            initial_delay_ms=initial_delay_ms,
            exponential_base=exponential_base,
            jitter=jitter,
        )
        handler = RetryHandler(config)

        async def wrapper(*args, **kwargs) -> T:
            return await handler.with_retry_async(func, *args, **kwargs)

        return wrapper
    return decorator


def retry_sync(
    max_retries: int = 3,
    initial_delay_ms: int = 1000,
    exponential_base: float = 2.0,
    jitter: bool = True,
):
    """
    Decorator for sync functions with retry.

    Usage:
        @retry_sync(max_retries=3)
        def my_function():
            ...
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        config = RetryConfig(
            max_retries=max_retries,
            initial_delay_ms=initial_delay_ms,
            exponential_base=exponential_base,
            jitter=jitter,
        )
        handler = RetryHandler(config)

        def wrapper(*args, **kwargs) -> T:
            return handler.with_retry(func, *args, **kwargs)

        return wrapper
    return decorator
