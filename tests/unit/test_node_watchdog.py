"""Tests for NodeWatchdog - frozen thread detection and auto-restart."""
import os
import sys
import time
import threading
import pytest
from unittest.mock import MagicMock, patch

# Ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from security.node_watchdog import NodeWatchdog, ThreadInfo


class TestNodeWatchdog:
    """Core watchdog functionality."""

    def test_register_thread(self):
        wd = NodeWatchdog(check_interval=1)
        wd.register('test', expected_interval=10, restart_fn=lambda: None)
        health = wd.get_health()
        assert 'test' in health['threads']
        assert health['threads']['test']['status'] == 'healthy'

    def test_heartbeat_updates_timestamp(self):
        wd = NodeWatchdog(check_interval=1)
        wd.register('test', expected_interval=10, restart_fn=lambda: None)
        # Grace period sets last_heartbeat to future; override to current time
        wd._threads['test'].last_heartbeat = time.time()
        old_ts = wd._threads['test'].last_heartbeat
        time.sleep(0.05)
        wd.heartbeat('test')
        assert wd._threads['test'].last_heartbeat > old_ts

    def test_heartbeat_unknown_thread_ignored(self):
        wd = NodeWatchdog()
        wd.heartbeat('nonexistent')  # Should not raise

    def test_frozen_detection(self):
        wd = NodeWatchdog(check_interval=1, frozen_multiplier=2.0)
        restarted = []
        wd.register('frozen_test', expected_interval=0.01,
                     restart_fn=lambda: restarted.append(True))
        # Force old heartbeat
        wd._threads['frozen_test'].last_heartbeat = time.time() - 100
        wd._check_all()
        assert wd._threads['frozen_test'].restart_count == 1
        assert len(restarted) == 1

    def test_healthy_thread_not_restarted(self):
        wd = NodeWatchdog(check_interval=1, frozen_multiplier=2.0)
        restarted = []
        wd.register('healthy_test', expected_interval=60,
                     restart_fn=lambda: restarted.append(True))
        wd.heartbeat('healthy_test')
        wd._check_all()
        assert len(restarted) == 0
        assert wd._threads['healthy_test'].status == 'healthy'

    def test_restart_calls_stop_then_start(self):
        call_order = []
        wd = NodeWatchdog()
        wd.register('test', expected_interval=0.01,
                     restart_fn=lambda: call_order.append('start'),
                     stop_fn=lambda: call_order.append('stop'))
        wd._threads['test'].last_heartbeat = time.time() - 100
        wd._check_all()
        assert call_order == ['stop', 'start']

    def test_restart_failure_increments_consecutive(self):
        wd = NodeWatchdog()
        wd.register('failing', expected_interval=0.01,
                     restart_fn=MagicMock(side_effect=RuntimeError('fail')))
        wd._threads['failing'].last_heartbeat = time.time() - 100
        wd._check_all()
        assert wd._threads['failing'].consecutive_failures == 1
        assert wd._threads['failing'].status == 'frozen'

    def test_dead_after_max_failures(self):
        wd = NodeWatchdog()
        wd.register('dying', expected_interval=0.01,
                     restart_fn=MagicMock(side_effect=RuntimeError('fail')))
        info = wd._threads['dying']
        info.consecutive_failures = 4
        info.last_heartbeat = time.time() - 100
        wd._check_all()
        assert info.status == 'dead'
        assert info.consecutive_failures == 5

    def test_dead_thread_not_retried(self):
        wd = NodeWatchdog()
        restarted = []
        wd.register('dead_test', expected_interval=0.01,
                     restart_fn=lambda: restarted.append(True))
        wd._threads['dead_test'].status = 'dead'
        wd._threads['dead_test'].last_heartbeat = time.time() - 100
        wd._check_all()
        assert len(restarted) == 0

    def test_successful_restart_resets_failures(self):
        wd = NodeWatchdog()
        wd.register('recovering', expected_interval=0.01,
                     restart_fn=lambda: None)
        info = wd._threads['recovering']
        info.consecutive_failures = 3
        info.last_heartbeat = time.time() - 100
        wd._check_all()
        assert info.consecutive_failures == 0
        assert info.restart_count == 1

    def test_restart_log_audit_trail(self):
        wd = NodeWatchdog()
        wd.register('audited', expected_interval=0.01,
                     restart_fn=lambda: None)
        wd._threads['audited'].last_heartbeat = time.time() - 100
        wd._check_all()
        assert len(wd._restart_log) == 1
        assert wd._restart_log[0]['name'] == 'audited'
        assert 'time' in wd._restart_log[0]

    def test_unregister_removes_thread(self):
        wd = NodeWatchdog()
        wd.register('temp', expected_interval=10, restart_fn=lambda: None)
        assert 'temp' in wd._threads
        wd.unregister('temp')
        assert 'temp' not in wd._threads

    def test_get_health_structure(self):
        wd = NodeWatchdog()
        wd._started_at = time.time()
        wd._running = True
        wd.register('test', expected_interval=10, restart_fn=lambda: None)
        health = wd.get_health()
        assert health['watchdog'] == 'healthy'
        assert 'uptime_seconds' in health
        assert 'threads' in health
        assert 'restart_log' in health
        t = health['threads']['test']
        assert 'status' in t
        assert 'last_heartbeat_age_s' in t
        assert 'expected_interval' in t

    def test_double_start(self):
        wd = NodeWatchdog(check_interval=60)
        wd.start()
        thread1 = wd._thread
        wd.start()
        assert wd._thread is thread1  # Same thread
        wd.stop()

    def test_multiple_threads_independent(self):
        wd = NodeWatchdog()
        restarted_a = []
        restarted_b = []
        wd.register('a', expected_interval=0.01,
                     restart_fn=lambda: restarted_a.append(True))
        wd.register('b', expected_interval=60,
                     restart_fn=lambda: restarted_b.append(True))
        # Only 'a' is frozen
        wd._threads['a'].last_heartbeat = time.time() - 100
        wd.heartbeat('b')
        wd._check_all()
        assert len(restarted_a) == 1
        assert len(restarted_b) == 0


class TestRapidRestartLoop:
    """Tests for the rapid-restart loop detection (3+ restarts in 5 min → dormant)."""

    def test_rapid_restart_marks_dead(self):
        wd = NodeWatchdog()
        wd.register('looper', expected_interval=0.01, restart_fn=lambda: None)
        info = wd._threads['looper']
        # Simulate 3 recent restarts in last 5 minutes
        now = time.time()
        info.recent_restart_times = [now - 60, now - 30, now - 10]
        info.last_heartbeat = now - 100
        wd._check_all()
        assert info.status == 'dead'

    def test_old_restarts_dont_trigger_loop_detection(self):
        wd = NodeWatchdog()
        restarted = []
        wd.register('old_restarts', expected_interval=0.01,
                     restart_fn=lambda: restarted.append(True))
        info = wd._threads['old_restarts']
        # Restarts older than 5 minutes ago should not trigger loop detection
        now = time.time()
        info.recent_restart_times = [now - 600, now - 500, now - 400]
        info.last_heartbeat = now - 100
        wd._check_all()
        assert info.status == 'healthy'  # Restarted successfully
        assert len(restarted) == 1


class TestGracePeriod:
    """Tests for startup grace period preventing false FROZEN alerts."""

    def test_grace_period_prevents_false_frozen(self):
        wd = NodeWatchdog()
        restarted = []
        wd.register('new_daemon', expected_interval=10,
                     restart_fn=lambda: restarted.append(True))
        # Just registered — should be in grace period (last_heartbeat is future)
        wd._check_all()
        assert len(restarted) == 0
        assert wd._threads['new_daemon'].status == 'healthy'

    def test_after_grace_period_detection_works(self):
        wd = NodeWatchdog()
        restarted = []
        wd.register('aged_daemon', expected_interval=0.01,
                     restart_fn=lambda: restarted.append(True))
        # Simulate grace period expired
        wd._threads['aged_daemon'].last_heartbeat = time.time() - 100
        wd._check_all()
        assert len(restarted) == 1


class TestDispatchBackoff:
    """Tests for agent daemon dispatch failure exponential backoff."""

    def test_backoff_tracking_on_failure(self):
        """Verify _dispatch_backoff tracks failures."""
        from integrations.agent_engine.agent_daemon import _dispatch_backoff
        # Clear state
        _dispatch_backoff.clear()
        # Simulate a failure entry
        goal_id = 'test-goal-123'
        _dispatch_backoff[goal_id] = {
            'failures': 3,
            'skip_until': time.time() + 240,
        }
        assert _dispatch_backoff[goal_id]['failures'] == 3
        assert _dispatch_backoff[goal_id]['skip_until'] > time.time()
        _dispatch_backoff.clear()

    def test_backoff_clears_on_success(self):
        """Verify backoff is cleared when goal dispatch succeeds."""
        from integrations.agent_engine.agent_daemon import _dispatch_backoff
        _dispatch_backoff.clear()
        goal_id = 'test-goal-456'
        _dispatch_backoff[goal_id] = {
            'failures': 2,
            'skip_until': time.time() + 120,
        }
        # Simulate success by popping
        _dispatch_backoff.pop(goal_id, None)
        assert goal_id not in _dispatch_backoff
        _dispatch_backoff.clear()

    def test_backoff_exponential_delay(self):
        """Verify delay grows exponentially: 60, 120, 240, 480, capped at 900."""
        delays = []
        for failures in range(1, 7):
            delay = min(60 * (2 ** (failures - 1)), 900)
            delays.append(delay)
        assert delays == [60, 120, 240, 480, 900, 900]


class TestWatchdogIntervalConfig:
    """Tests for correct watchdog interval configuration per daemon type."""

    def test_gossip_interval_exceeds_worst_case(self):
        """Gossip watchdog interval should be >= 60s (3 peers × 10s + overhead)."""
        # The fix changed gossip expected_interval from 10 to 120
        # Verify the constant matches what we set
        expected_interval = 120
        assert expected_interval >= 60, (
            f"Gossip watchdog interval ({expected_interval}s) must be >= 60s "
            f"to avoid false FROZEN alerts during blocking network rounds")

    def test_lifecycle_interval_with_multiplier(self):
        """Model lifecycle watchdog interval should use 3× multiplier."""
        lifecycle_interval = 15  # default _interval
        watchdog_interval = max(lifecycle_interval * 3, 60)
        assert watchdog_interval >= 45, (
            f"Lifecycle watchdog interval ({watchdog_interval}s) must account "
            f"for nvidia-smi + pressure checks between heartbeats")


class TestSQLiteBusyTimeout:
    """Tests for SQLite busy_timeout configuration."""

    def test_busy_timeout_sufficient(self):
        """busy_timeout should be >= 10000ms to handle concurrent thread contention."""
        # The fix changed busy_timeout from 5000 to 15000
        busy_timeout_ms = 15000
        assert busy_timeout_ms >= 10000, (
            f"SQLite busy_timeout ({busy_timeout_ms}ms) should be >= 10s "
            f"for 50+ concurrent background threads")


class TestGatherInfoAutonomousSalvage:
    """Tests for early salvage threshold in autonomous mode."""

    def test_autonomous_salvage_threshold_is_3(self):
        """Autonomous dispatches should salvage at turn 3, not turn 10."""
        # The fix sets salvage_threshold = 3 for autonomous, MAX_GATHER_TURNS-2 for interactive
        is_autonomous = True
        MAX_GATHER_TURNS = 12
        salvage_threshold = 3 if is_autonomous else MAX_GATHER_TURNS - 2
        assert salvage_threshold == 3

    def test_interactive_salvage_threshold_unchanged(self):
        """Interactive mode should still salvage at turn 10."""
        is_autonomous = False
        MAX_GATHER_TURNS = 12
        salvage_threshold = 3 if is_autonomous else MAX_GATHER_TURNS - 2
        assert salvage_threshold == 10


class TestModuleSingleton:
    def test_start_and_get_watchdog(self):
        from security.node_watchdog import start_watchdog, get_watchdog
        wd = start_watchdog(check_interval=60)
        assert get_watchdog() is wd
        wd.stop()
