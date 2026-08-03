"""
Tests for the reusable Circuit Breaker (core/circuit_breaker.py).
"""
import os
import sys
import time
import threading
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.circuit_breaker import (
    CircuitBreaker, CircuitState, CircuitBreakerOpenError, with_circuit_breaker,
    PeerBackoff,
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


# ═══════════════════════════════════════════════════════════════
# Clock steps (task #24)
# ═══════════════════════════════════════════════════════════════
# THE REAL INCIDENT: on a Windows dual-boot node the RTC holds LOCAL time while
# NixOS reads it as UTC, so the box ran +5:30 wrong until wifi came up — then
# NTP yanked the wall clock BACKWARDS by 19800s, immediately before the desktop
# hung. hart-installer.nix:117 fixes the CAUSE where hart-install runs; it does
# not make the runtime survive a step, and steps have other causes (live-USB on
# the same hardware, dead CMOS battery, VM restored from snapshot, first NTP
# sync after a long power-off).
#
# WHAT BROKE: `_get_state` computed `elapsed = time.time() - self._opened_at`.
# After -19800s, elapsed is about -19800, so `elapsed > cooldown` is False and
# the breaker reports OPEN — and keeps reporting OPEN until the wall clock
# climbs back. A 60-second cooldown became a 5.5-HOUR outage of whatever it
# guards. PeerBackoff had the same bug from the other side: it stores
# `time.time() + delay` as a deadline.
#
# Both are pure elapsed-time arithmetic, never displayed and never persisted
# (get_stats does not expose them), so time.monotonic() is correct here and not
# merely safer.

#: The steward's actual offset: IST is UTC+5:30.
IST_STEP = 5.5 * 3600


class _FakeClock:
    """A monotonic-looking clock the test can advance."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestBackwardsClockStep:

    @staticmethod
    def _trip(breaker):
        for _ in range(breaker.threshold):
            breaker.record_failure()

    def test_breaker_leaves_open_after_cooldown(self, monkeypatch):
        """Cooldown is measured on a clock that cannot be stepped backwards."""
        clock = _FakeClock()
        monkeypatch.setattr('core.circuit_breaker.time.monotonic', clock)
        cb = CircuitBreaker(name='t', threshold=2, cooldown=60.0)
        self._trip(cb)
        assert cb.state == CircuitState.OPEN
        clock.t += 61
        assert cb.state == CircuitState.HALF_OPEN,             "breaker did not leave OPEN after its cooldown"

    def test_wall_clock_would_have_stranded_it(self):
        """Demonstrates the ORIGINAL bug so the fix is not taken on faith."""
        opened_at, cooldown = 1_000_000.0, 60.0
        elapsed = (opened_at + 5 - IST_STEP) - opened_at
        assert elapsed < 0, "precondition: the step makes elapsed negative"
        assert not elapsed > cooldown,             "the old wall-clock check reported OPEN — this is the bug"
        assert IST_STEP > cooldown * 300      # and stays wrong for hours

    def test_backoff_deadline_expires_normally(self, monkeypatch):
        clock = _FakeClock()
        monkeypatch.setattr('core.circuit_breaker.time.monotonic', clock)
        bo = PeerBackoff(initial=1.0, maximum=30.0)
        bo.record_failure('peer-a')
        assert bo.is_backed_off('peer-a')
        clock.t += 2
        assert not bo.is_backed_off('peer-a'),             "key still backed off after its delay elapsed"

    def test_backoff_prune_shares_the_deadline_clock(self, monkeypatch):
        """A prune on a different clock than the deadline would never expire."""
        clock = _FakeClock()
        monkeypatch.setattr('core.circuit_breaker.time.monotonic', clock)
        bo = PeerBackoff(initial=1.0, maximum=30.0)
        bo.record_failure('peer-b')
        clock.t += 5
        bo.prune_expired()
        assert not bo.is_backed_off('peer-b')

    def test_source_guard_module_uses_no_wall_clock(self):
        """Labelled source check: the bug is an ABSENCE, across the whole file.

        Paired with the behavioural tests above, per feedback_no_grep_tests —
        no single behavioural test can prove a call does not exist anywhere.
        """
        import inspect
        import core.circuit_breaker as mod
        offenders = [ln.strip() for ln in inspect.getsource(mod).splitlines()
                     if 'time.time()' in ln and not ln.strip().startswith('#')]
        assert offenders == [], (
            f"wall-clock time is back in circuit_breaker.py: {offenders}. An "
            f"NTP step (or a misread RTC on a dual-boot box) makes elapsed "
            f"negative and strands the breaker for the whole offset.")
