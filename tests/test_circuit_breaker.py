"""
Tests for the reusable Circuit Breaker (core/circuit_breaker.py).
"""
import os
import sys
import time
import threading
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.circuit_breaker import (
    CircuitBreaker, CircuitState, CircuitBreakerOpenError, with_circuit_breaker,
)


class TestCircuitBreakerStates:
    """Test circuit breaker state transitions."""

    def test_starts_closed(self):
        cb = CircuitBreaker(name='test', threshold=3, cooldown=10)
        assert cb.state == CircuitState.CLOSED
        assert not cb.is_open()

    def test_stays_closed_below_threshold(self):
        cb = CircuitBreaker(name='test', threshold=3, cooldown=10)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        assert not cb.is_open()

    def test_opens_at_threshold(self):
        cb = CircuitBreaker(name='test', threshold=3, cooldown=60)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.is_open()

    def test_half_open_after_cooldown(self):
        cb = CircuitBreaker(name='test', threshold=2, cooldown=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_allows_one_probe(self):
        cb = CircuitBreaker(name='test', threshold=2, cooldown=0.1)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        # First call should be allowed (probe)
        assert not cb.is_open()
        # Second call should be blocked (already probing)
        assert cb.is_open()

    def test_success_after_half_open_closes(self):
        cb = CircuitBreaker(name='test', threshold=2, cooldown=0.1)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        cb.is_open()  # Allow probe
        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert not cb.is_open()

    def test_failure_after_half_open_reopens(self):
        cb = CircuitBreaker(name='test', threshold=2, cooldown=0.1)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        cb.is_open()  # Allow probe
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(name='test', threshold=3, cooldown=60)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        # Can fail twice more before opening
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_reset_manual(self):
        cb = CircuitBreaker(name='test', threshold=2, cooldown=60)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert not cb.is_open()


class TestCircuitBreakerStats:
    """Test get_stats() output."""

    def test_stats_closed(self):
        cb = CircuitBreaker(name='myservice', threshold=5, cooldown=30)
        stats = cb.get_stats()
        assert stats['name'] == 'myservice'
        assert stats['state'] == 'closed'
        assert stats['failures'] == 0
        assert stats['threshold'] == 5

    def test_stats_open(self):
        cb = CircuitBreaker(name='myservice', threshold=2, cooldown=30)
        cb.record_failure()
        cb.record_failure()
        stats = cb.get_stats()
        assert stats['state'] == 'open'
        assert stats['failures'] == 2


class TestWithCircuitBreakerDecorator:
    """Test the @with_circuit_breaker decorator."""

    def test_decorator_passes_on_closed(self):
        cb = CircuitBreaker(name='test', threshold=3, cooldown=60)

        @with_circuit_breaker(cb)
        def add(a, b):
            return a + b

        assert add(1, 2) == 3

    def test_decorator_records_success(self):
        cb = CircuitBreaker(name='test', threshold=3, cooldown=60)
        cb.record_failure()

        @with_circuit_breaker(cb)
        def ok():
            return 'ok'

        ok()
        assert cb.get_stats()['failures'] == 0

    def test_decorator_records_failure(self):
        cb = CircuitBreaker(name='test', threshold=3, cooldown=60)

        @with_circuit_breaker(cb)
        def fail():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            fail()
        assert cb.get_stats()['failures'] == 1

    def test_decorator_raises_when_open(self):
        cb = CircuitBreaker(name='test', threshold=1, cooldown=60)
        cb.record_failure()

        @with_circuit_breaker(cb)
        def noop():
            return 'should not run'

        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            noop()
        assert 'test' in str(exc_info.value)

    def test_decorator_uses_fallback_when_open(self):
        cb = CircuitBreaker(name='test', threshold=1, cooldown=60)
        cb.record_failure()

        @with_circuit_breaker(cb, fallback=lambda: 'fallback')
        def noop():
            return 'real'

        assert noop() == 'fallback'

    def test_decorator_preserves_function_name(self):
        cb = CircuitBreaker(name='test', threshold=3, cooldown=60)

        @with_circuit_breaker(cb)
        def my_function():
            pass

        assert my_function.__name__ == 'my_function'


class TestCircuitBreakerThreadSafety:
    """Verify thread safety of circuit breaker operations."""

    def test_concurrent_failures_reach_threshold(self):
        """Multiple threads recording failures should correctly open the circuit."""
        cb = CircuitBreaker(name='threaded', threshold=10, cooldown=60)
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait(timeout=5)
            cb.record_failure()

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert cb.state == CircuitState.OPEN
        assert cb.get_stats()['failures'] == 10
