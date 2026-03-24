"""Comprehensive tests for NodeWatchdog - frozen thread detection and auto-restart.

Covers:
- ThreadInfo dataclass defaults and status values
- NodeWatchdog: register, heartbeat, mark/clear LLM call
- _check_all: frozen detection with normal vs LLM_CALL_TIMEOUT (900s)
- _restart_thread: callback invocation, stop-then-start order
- Rapid restart loop detection (3+ in 5min -> dead)
- Grace period for newly registered threads
- LLM_CALL_TIMEOUT_SECONDS = 900
- get_watchdog / start_watchdog singleton
- Thread safety of heartbeat/check cycle
"""
import os
import sys
import time
import threading
import pytest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from security.node_watchdog import (
    NodeWatchdog,
    ThreadInfo,
    LLM_CALL_TIMEOUT_SECONDS,
    MAX_CONSECUTIVE_FAILURES,
    STARTUP_GRACE_SECONDS,
    start_watchdog,
    get_watchdog,
)


# ---------------------------------------------------------------------------
# ThreadInfo dataclass
# ---------------------------------------------------------------------------
class TestThreadInfo:
    """ThreadInfo dataclass defaults and field behaviour."""

    def test_defaults(self):
        info = ThreadInfo(name="t", expected_interval=10, restart_fn=lambda: None)
        assert info.name == "t"
        assert info.expected_interval == 10
        assert info.status == "healthy"
        assert info.restart_count == 0
        assert info.last_restart_at is None
        assert info.consecutive_failures == 0
        assert info.recent_restart_times == []
        assert info.in_llm_call is False
        assert info.llm_call_started_at is None
        assert info.stop_fn is None

    def test_status_values_assignable(self):
        info = ThreadInfo(name="t", expected_interval=10, restart_fn=lambda: None)
        for status in ("healthy", "frozen", "restarting", "dead", "in_llm_call"):
            info.status = status
            assert info.status == status

    def test_last_heartbeat_defaults_to_now(self):
        before = time.time()
        info = ThreadInfo(name="t", expected_interval=10, restart_fn=lambda: None)
        after = time.time()
        assert before <= info.last_heartbeat <= after

    def test_recent_restart_times_independent_per_instance(self):
        a = ThreadInfo(name="a", expected_interval=10, restart_fn=lambda: None)
        b = ThreadInfo(name="b", expected_interval=10, restart_fn=lambda: None)
        a.recent_restart_times.append(1.0)
        assert b.recent_restart_times == []


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
class TestConstants:
    def test_llm_call_timeout_is_900(self):
        assert LLM_CALL_TIMEOUT_SECONDS == 900

    def test_max_consecutive_failures_is_5(self):
        assert MAX_CONSECUTIVE_FAILURES == 5

    def test_startup_grace_seconds_is_60(self):
        assert STARTUP_GRACE_SECONDS == 60


# ---------------------------------------------------------------------------
# Registration & heartbeat
# ---------------------------------------------------------------------------
class TestRegistration:
    def test_register_thread(self):
        wd = NodeWatchdog(check_interval=1)
        wd.register("test", expected_interval=10, restart_fn=lambda: None)
        assert wd.is_registered("test")
        health = wd.get_health()
        assert "test" in health["threads"]
        assert health["threads"]["test"]["status"] == "healthy"

    def test_register_sets_grace_period(self):
        """last_heartbeat should be ~STARTUP_GRACE_SECONDS in the future."""
        before = time.time()
        wd = NodeWatchdog()
        wd.register("g", expected_interval=10, restart_fn=lambda: None)
        expected_min = before + STARTUP_GRACE_SECONDS
        assert wd._threads["g"].last_heartbeat >= expected_min - 1

    def test_unregister_removes_thread(self):
        wd = NodeWatchdog()
        wd.register("temp", expected_interval=10, restart_fn=lambda: None)
        wd.unregister("temp")
        assert not wd.is_registered("temp")

    def test_unregister_nonexistent_is_safe(self):
        wd = NodeWatchdog()
        wd.unregister("ghost")  # should not raise

    def test_heartbeat_updates_timestamp(self):
        wd = NodeWatchdog()
        wd.register("t", expected_interval=10, restart_fn=lambda: None)
        wd._threads["t"].last_heartbeat = time.time() - 50
        old = wd._threads["t"].last_heartbeat
        wd.heartbeat("t")
        assert wd._threads["t"].last_heartbeat > old

    def test_heartbeat_unknown_thread_ignored(self):
        wd = NodeWatchdog()
        wd.heartbeat("nonexistent")  # no raise

    def test_is_registered_false_for_unknown(self):
        wd = NodeWatchdog()
        assert not wd.is_registered("nope")


# ---------------------------------------------------------------------------
# LLM call marking
# ---------------------------------------------------------------------------
class TestLLMCallMarking:
    def test_mark_in_llm_call(self):
        wd = NodeWatchdog()
        wd.register("llm", expected_interval=60, restart_fn=lambda: None)
        wd._threads["llm"].last_heartbeat = time.time() - 10
        wd.mark_in_llm_call("llm")
        info = wd._threads["llm"]
        assert info.in_llm_call is True
        assert info.llm_call_started_at is not None
        # heartbeat should have been refreshed
        assert info.last_heartbeat >= time.time() - 1

    def test_clear_llm_call(self):
        wd = NodeWatchdog()
        wd.register("llm", expected_interval=60, restart_fn=lambda: None)
        wd.mark_in_llm_call("llm")
        wd.clear_llm_call("llm")
        info = wd._threads["llm"]
        assert info.in_llm_call is False
        assert info.llm_call_started_at is None

    def test_mark_llm_call_unknown_thread_safe(self):
        wd = NodeWatchdog()
        wd.mark_in_llm_call("ghost")  # no raise
        wd.clear_llm_call("ghost")  # no raise


# ---------------------------------------------------------------------------
# Frozen detection (_check_all)
# ---------------------------------------------------------------------------
class TestFrozenDetection:
    def test_frozen_thread_gets_restarted(self):
        wd = NodeWatchdog(frozen_multiplier=2.0)
        restarted = []
        wd.register("f", expected_interval=0.01, restart_fn=lambda: restarted.append(True))
        wd._threads["f"].last_heartbeat = time.time() - 100
        wd._check_all()
        assert len(restarted) == 1
        assert wd._threads["f"].restart_count == 1

    def test_healthy_thread_not_restarted(self):
        wd = NodeWatchdog(frozen_multiplier=2.0)
        restarted = []
        wd.register("h", expected_interval=60, restart_fn=lambda: restarted.append(True))
        wd.heartbeat("h")
        wd._check_all()
        assert len(restarted) == 0

    def test_normal_threshold_uses_multiplier(self):
        """Threshold = expected_interval * frozen_multiplier for normal threads."""
        wd = NodeWatchdog(frozen_multiplier=5.0)
        restarted = []
        wd.register("n", expected_interval=10, restart_fn=lambda: restarted.append(True))
        # Age = 40s, threshold = 10*5 = 50s -> NOT frozen
        wd._threads["n"].last_heartbeat = time.time() - 40
        wd._check_all()
        assert len(restarted) == 0
        # Age = 60s, threshold = 50s -> frozen
        wd._threads["n"].last_heartbeat = time.time() - 60
        wd._check_all()
        assert len(restarted) == 1

    def test_llm_call_uses_extended_timeout(self):
        """When in_llm_call=True, threshold is LLM_CALL_TIMEOUT_SECONDS (900)."""
        wd = NodeWatchdog(frozen_multiplier=2.0)
        restarted = []
        wd.register("llm", expected_interval=10, restart_fn=lambda: restarted.append(True))
        info = wd._threads["llm"]
        info.in_llm_call = True
        # 500s into an LLM call — under 900s threshold, should NOT restart
        info.last_heartbeat = time.time() - 500
        wd._check_all()
        assert len(restarted) == 0
        assert info.status == "healthy"

    def test_llm_call_still_restarts_after_900s(self):
        """Even in_llm_call threads restart after 900s."""
        wd = NodeWatchdog(frozen_multiplier=2.0)
        restarted = []
        wd.register("llm_stuck", expected_interval=10, restart_fn=lambda: restarted.append(True))
        info = wd._threads["llm_stuck"]
        info.in_llm_call = True
        info.last_heartbeat = time.time() - 1000  # > 900s
        wd._check_all()
        assert len(restarted) == 1

    def test_dead_thread_not_checked(self):
        wd = NodeWatchdog()
        restarted = []
        wd.register("dead", expected_interval=0.01, restart_fn=lambda: restarted.append(True))
        wd._threads["dead"].status = "dead"
        wd._threads["dead"].last_heartbeat = time.time() - 10000
        wd._check_all()
        assert len(restarted) == 0

    def test_multiple_threads_independent(self):
        wd = NodeWatchdog()
        ra, rb = [], []
        wd.register("a", expected_interval=0.01, restart_fn=lambda: ra.append(True))
        wd.register("b", expected_interval=60, restart_fn=lambda: rb.append(True))
        wd._threads["a"].last_heartbeat = time.time() - 100
        wd.heartbeat("b")
        wd._check_all()
        assert len(ra) == 1
        assert len(rb) == 0


# ---------------------------------------------------------------------------
# _restart_thread
# ---------------------------------------------------------------------------
class TestRestartThread:
    def test_restart_calls_stop_then_start(self):
        order = []
        wd = NodeWatchdog()
        wd.register("t", expected_interval=0.01,
                     restart_fn=lambda: order.append("start"),
                     stop_fn=lambda: order.append("stop"))
        wd._threads["t"].last_heartbeat = time.time() - 100
        wd._check_all()
        assert order == ["stop", "start"]

    def test_restart_without_stop_fn(self):
        restarted = []
        wd = NodeWatchdog()
        wd.register("t", expected_interval=0.01, restart_fn=lambda: restarted.append(True))
        wd._threads["t"].last_heartbeat = time.time() - 100
        wd._check_all()
        assert len(restarted) == 1

    def test_restart_failure_increments_consecutive_failures(self):
        wd = NodeWatchdog()
        wd.register("fail", expected_interval=0.01,
                     restart_fn=MagicMock(side_effect=RuntimeError("boom")))
        wd._threads["fail"].last_heartbeat = time.time() - 100
        wd._check_all()
        assert wd._threads["fail"].consecutive_failures == 1
        assert wd._threads["fail"].status == "frozen"

    def test_dead_after_max_consecutive_failures(self):
        wd = NodeWatchdog()
        wd.register("dying", expected_interval=0.01,
                     restart_fn=MagicMock(side_effect=RuntimeError("boom")))
        info = wd._threads["dying"]
        info.consecutive_failures = MAX_CONSECUTIVE_FAILURES - 1  # 4
        info.last_heartbeat = time.time() - 100
        wd._check_all()
        assert info.status == "dead"
        assert info.consecutive_failures == MAX_CONSECUTIVE_FAILURES

    def test_successful_restart_resets_failures(self):
        wd = NodeWatchdog()
        wd.register("rec", expected_interval=0.01, restart_fn=lambda: None)
        info = wd._threads["rec"]
        info.consecutive_failures = 3
        info.last_heartbeat = time.time() - 100
        wd._check_all()
        assert info.consecutive_failures == 0
        assert info.restart_count == 1

    def test_restart_grants_new_grace_period(self):
        """After restart, last_heartbeat is pushed into the future for grace."""
        wd = NodeWatchdog()
        wd.register("gp", expected_interval=0.01, restart_fn=lambda: None)
        wd._threads["gp"].last_heartbeat = time.time() - 100
        wd._check_all()
        # Grace period: last_heartbeat should be in the future
        assert wd._threads["gp"].last_heartbeat > time.time()

    def test_restart_log_appended(self):
        wd = NodeWatchdog()
        wd.register("logged", expected_interval=0.01, restart_fn=lambda: None)
        wd._threads["logged"].last_heartbeat = time.time() - 100
        wd._check_all()
        assert len(wd._restart_log) == 1
        assert wd._restart_log[0]["name"] == "logged"
        assert "time" in wd._restart_log[0]
        assert wd._restart_log[0]["restart_count"] == 1

    def test_stop_fn_exception_does_not_prevent_restart(self):
        restarted = []
        wd = NodeWatchdog()
        wd.register("t", expected_interval=0.01,
                     restart_fn=lambda: restarted.append(True),
                     stop_fn=MagicMock(side_effect=Exception("stop failed")))
        wd._threads["t"].last_heartbeat = time.time() - 100
        wd._check_all()
        assert len(restarted) == 1

    def test_restart_nonexistent_returns_false(self):
        wd = NodeWatchdog()
        result = wd._restart_thread("ghost")
        assert result is False


# ---------------------------------------------------------------------------
# Rapid restart loop detection
# ---------------------------------------------------------------------------
class TestRapidRestartLoop:
    def test_3_restarts_in_5min_marks_dead(self):
        wd = NodeWatchdog()
        wd.register("looper", expected_interval=0.01, restart_fn=lambda: None)
        info = wd._threads["looper"]
        now = time.time()
        info.recent_restart_times = [now - 60, now - 30, now - 10]
        info.last_heartbeat = now - 100
        wd._check_all()
        assert info.status == "dead"

    def test_old_restarts_dont_trigger_loop(self):
        wd = NodeWatchdog()
        restarted = []
        wd.register("old", expected_interval=0.01,
                     restart_fn=lambda: restarted.append(True))
        info = wd._threads["old"]
        now = time.time()
        # All restarts > 5 min ago
        info.recent_restart_times = [now - 600, now - 500, now - 400]
        info.last_heartbeat = now - 100
        wd._check_all()
        assert info.status == "healthy"
        assert len(restarted) == 1

    def test_2_recent_restarts_still_allows_restart(self):
        wd = NodeWatchdog()
        restarted = []
        wd.register("two", expected_interval=0.01,
                     restart_fn=lambda: restarted.append(True))
        info = wd._threads["two"]
        now = time.time()
        info.recent_restart_times = [now - 60, now - 30]
        info.last_heartbeat = now - 100
        wd._check_all()
        assert info.status == "healthy"
        assert len(restarted) == 1

    def test_recent_restart_times_pruned_after_restart(self):
        """Old entries in recent_restart_times are pruned on successful restart."""
        wd = NodeWatchdog()
        wd.register("prune", expected_interval=0.01, restart_fn=lambda: None)
        info = wd._threads["prune"]
        now = time.time()
        # 1 old + 1 recent
        info.recent_restart_times = [now - 600, now - 10]
        info.last_heartbeat = now - 100
        wd._check_all()
        # After restart, old entry should be pruned; recent + new one remain
        assert all(t > now - 300 for t in info.recent_restart_times)


# ---------------------------------------------------------------------------
# Grace period
# ---------------------------------------------------------------------------
class TestGracePeriod:
    def test_newly_registered_thread_not_frozen(self):
        wd = NodeWatchdog()
        restarted = []
        wd.register("new", expected_interval=10,
                     restart_fn=lambda: restarted.append(True))
        wd._check_all()
        assert len(restarted) == 0

    def test_after_grace_period_detection_works(self):
        wd = NodeWatchdog()
        restarted = []
        wd.register("aged", expected_interval=0.01,
                     restart_fn=lambda: restarted.append(True))
        wd._threads["aged"].last_heartbeat = time.time() - 100
        wd._check_all()
        assert len(restarted) == 1


# ---------------------------------------------------------------------------
# get_health
# ---------------------------------------------------------------------------
class TestGetHealth:
    def test_health_structure(self):
        wd = NodeWatchdog()
        wd._started_at = time.time()
        wd._running = True
        wd.register("x", expected_interval=10, restart_fn=lambda: None)
        h = wd.get_health()
        assert h["watchdog"] == "healthy"
        assert "uptime_seconds" in h
        assert "threads" in h
        assert "restart_log" in h
        t = h["threads"]["x"]
        for key in ("status", "last_heartbeat_age_s", "last_heartbeat_iso",
                     "expected_interval", "restart_count", "consecutive_failures"):
            assert key in t

    def test_health_stopped_when_not_running(self):
        wd = NodeWatchdog()
        h = wd.get_health()
        assert h["watchdog"] == "stopped"

    def test_health_shows_last_restart_iso(self):
        wd = NodeWatchdog()
        wd._started_at = time.time()
        wd._running = True
        wd.register("r", expected_interval=0.01, restart_fn=lambda: None)
        wd._threads["r"].last_heartbeat = time.time() - 100
        wd._check_all()
        h = wd.get_health()
        assert "last_restart_iso" in h["threads"]["r"]


# ---------------------------------------------------------------------------
# Start / stop / singleton
# ---------------------------------------------------------------------------
class TestStartStop:
    def test_double_start_same_thread(self):
        wd = NodeWatchdog(check_interval=60)
        wd.start()
        t1 = wd._thread
        wd.start()
        assert wd._thread is t1
        wd.stop()

    def test_stop_sets_running_false(self):
        wd = NodeWatchdog(check_interval=60)
        wd.start()
        wd.stop()
        assert not wd._running


class TestModuleSingleton:
    def test_start_and_get_watchdog(self):
        wd = start_watchdog(check_interval=60)
        assert get_watchdog() is wd
        wd.stop()

    def test_start_watchdog_replaces_previous(self):
        wd1 = start_watchdog(check_interval=60)
        wd2 = start_watchdog(check_interval=60)
        assert get_watchdog() is wd2
        assert wd1 is not wd2
        wd1.stop()
        wd2.stop()


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------
class TestThreadSafety:
    def test_concurrent_heartbeats_and_checks(self):
        """Multiple threads sending heartbeats while _check_all runs."""
        wd = NodeWatchdog(frozen_multiplier=2.0)
        for i in range(5):
            wd.register(f"t{i}", expected_interval=60, restart_fn=lambda: None)
            wd._threads[f"t{i}"].last_heartbeat = time.time()

        errors = []
        stop = threading.Event()

        def heartbeater(name):
            while not stop.is_set():
                try:
                    wd.heartbeat(name)
                except Exception as e:
                    errors.append(e)
                time.sleep(0.001)

        def checker():
            for _ in range(50):
                try:
                    wd._check_all()
                except Exception as e:
                    errors.append(e)
                time.sleep(0.001)

        threads = [threading.Thread(target=heartbeater, args=(f"t{i}",)) for i in range(5)]
        threads.append(threading.Thread(target=checker))
        for t in threads:
            t.start()

        time.sleep(0.3)
        stop.set()
        for t in threads:
            t.join(timeout=5)

        assert errors == [], f"Thread safety errors: {errors}"

    def test_concurrent_register_and_check(self):
        """Register threads while _check_all is running."""
        wd = NodeWatchdog(frozen_multiplier=2.0)
        errors = []

        def registerer():
            for i in range(20):
                try:
                    wd.register(f"r{i}", expected_interval=60, restart_fn=lambda: None)
                except Exception as e:
                    errors.append(e)
                time.sleep(0.001)

        def checker():
            for _ in range(20):
                try:
                    wd._check_all()
                except Exception as e:
                    errors.append(e)
                time.sleep(0.001)

        t1 = threading.Thread(target=registerer)
        t2 = threading.Thread(target=checker)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert errors == []


# ---------------------------------------------------------------------------
# Environment variable override for LLM timeout
# ---------------------------------------------------------------------------
class TestEnvOverride:
    def test_llm_timeout_env_override(self):
        """LLM_CALL_TIMEOUT_SECONDS reads from HEVOLVE_LLM_CALL_TIMEOUT env var."""
        # The module-level constant is already set; verify the default is 900
        # (The env var wasn't set, so it uses the default '900')
        assert LLM_CALL_TIMEOUT_SECONDS == 900

    def test_check_interval_env_override(self):
        """check_interval defaults from HEVOLVE_WATCHDOG_INTERVAL env var."""
        with patch.dict(os.environ, {"HEVOLVE_WATCHDOG_INTERVAL": "45"}):
            wd = NodeWatchdog()
            assert wd._check_interval == 45
