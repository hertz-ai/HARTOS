"""
Reusable Circuit Breaker — extracted from WorldModelBridge.

Three states:
- CLOSED: Requests flow normally. Failures increment counter.
- OPEN: Requests blocked. Timer ticks down cooldown period.
- HALF_OPEN: One probe request allowed. Success → CLOSED, failure → OPEN.

Thread-safe via threading.Lock.

Usage:
    cb = CircuitBreaker(name='hevolve_core', threshold=5, cooldown=60)

    if cb.is_open():
        return fallback_response

    try:
        result = call_external_service()
        cb.record_success()
        return result
    except Exception:
        cb.record_failure()
        raise

Or use the decorator:
    @with_circuit_breaker(cb)
    def call_service():
        ...
"""
import logging
import threading
import time
from enum import Enum
from typing import Optional, Callable, Any

logger = logging.getLogger('hevolve_social')


class CircuitState(Enum):
    CLOSED = 'closed'
    OPEN = 'open'
    HALF_OPEN = 'half_open'


class CircuitBreaker:
    """Thread-safe circuit breaker with configurable threshold and cooldown."""

    def __init__(self, name: str = 'default', threshold: int = 5,
                 cooldown: float = 60.0):
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()
        self._half_open_in_flight = False

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._get_state()

    def _get_state(self) -> CircuitState:
        """Internal state check (caller must hold lock)."""
        if self._failures < self.threshold:
            return CircuitState.CLOSED
        elapsed = time.time() - self._opened_at
        if elapsed > self.cooldown:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    def is_open(self) -> bool:
        """Returns True if requests should be blocked."""
        with self._lock:
            state = self._get_state()
            if state == CircuitState.CLOSED:
                return False
            if state == CircuitState.HALF_OPEN:
                if not self._half_open_in_flight:
                    self._half_open_in_flight = True
                    return False  # Allow one probe
                return True  # Block additional requests during probe
            return True  # OPEN

    def record_success(self):
        """Reset circuit breaker on successful call."""
        with self._lock:
            self._failures = 0
            self._half_open_in_flight = False

    def record_failure(self):
        """Record failure; open circuit at threshold."""
        with self._lock:
            self._failures += 1
            self._half_open_in_flight = False
            if self._failures >= self.threshold:
                self._opened_at = time.time()
                logger.warning(
                    f"[CircuitBreaker:{self.name}] OPEN after "
                    f"{self._failures} failures. Cooldown {self.cooldown}s.")

    def reset(self):
        """Manually reset the circuit breaker."""
        with self._lock:
            self._failures = 0
            self._opened_at = 0.0
            self._half_open_in_flight = False

    def get_stats(self) -> dict:
        with self._lock:
            return {
                'name': self.name,
                'state': self._get_state().value,
                'failures': self._failures,
                'threshold': self.threshold,
                'cooldown': self.cooldown,
            }


class PeerBackoff:
    """Exponential backoff tracker for unreachable peers/endpoints.

    Used by GossipProtocol and FederatedAggregator to avoid hammering
    dead peers. Tracks per-key (next_retry_at, current_delay) tuples.

    Usage:
        backoff = PeerBackoff(initial=10, maximum=300)
        if backoff.is_backed_off('http://peer:6777'):
            return  # skip
        try:
            do_request()
            backoff.record_success('http://peer:6777')
        except ConnectionError:
            backoff.record_failure('http://peer:6777')
    """

    def __init__(self, initial: float = 10.0, maximum: float = 300.0):
        self.initial = initial
        self.maximum = maximum
        self._lock = threading.Lock()
        self._entries: dict = {}  # key → (next_retry_at, current_delay)

    def is_backed_off(self, key: str) -> bool:
        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return False
            return time.time() < entry[0]

    def record_failure(self, key: str):
        with self._lock:
            entry = self._entries.get(key)
            if entry:
                new_delay = min(entry[1] * 2, self.maximum)
            else:
                new_delay = self.initial
            self._entries[key] = (time.time() + new_delay, new_delay)

    def record_success(self, key: str):
        with self._lock:
            self._entries.pop(key, None)

    def prune_expired(self):
        """Remove entries whose backoff period has elapsed."""
        with self._lock:
            now = time.time()
            expired = [k for k, (retry_at, _) in self._entries.items()
                       if now >= retry_at]
            for k in expired:
                del self._entries[k]


class CircuitBreakerOpenError(Exception):
    """Raised when a circuit breaker is open and blocking requests."""
    def __init__(self, name: str):
        super().__init__(f"Circuit breaker '{name}' is open")
        self.breaker_name = name


def with_circuit_breaker(cb: CircuitBreaker,
                         fallback: Optional[Callable] = None):
    """Decorator that wraps a function with circuit breaker protection.

    Args:
        cb: CircuitBreaker instance
        fallback: Optional callable returning fallback value when circuit is open.
                  If None, raises CircuitBreakerOpenError.
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            if cb.is_open():
                if fallback is not None:
                    return fallback(*args, **kwargs)
                raise CircuitBreakerOpenError(cb.name)
            try:
                result = func(*args, **kwargs)
                cb.record_success()
                return result
            except Exception:
                cb.record_failure()
                raise
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        return wrapper
    return decorator
