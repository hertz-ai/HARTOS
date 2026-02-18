"""Tests for NodeWatchdog - frozen thread detection and auto-restart."""
import os
import sys
import time
import threading
import pytest
from unittest.mock import MagicMock, patch

# Ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


class TestModuleSingleton:
    def test_start_and_get_watchdog(self):
        from security.node_watchdog import start_watchdog, get_watchdog
        wd = start_watchdog(check_interval=60)
        assert get_watchdog() is wd
        wd.stop()
