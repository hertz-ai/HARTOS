"""The node watchdog reports its OWN health from the thread, not an intent flag.

2026-07-24 audit (docs/audit/ux_degrading_design_choices_2026-07-24.md #1.2): the
monitor at the root of the whole recovery tree reported ``watchdog: 'healthy'``
straight from ``self._running`` (only ever toggled in start()/stop()), and its
``_check_loop`` ran ``_check_all()`` with no guard. So any exception escaping a
check pass killed the single monitor thread while ``_running`` stayed True — the
dashboard showed "healthy" forever while ZERO daemons were monitored or restarted.

The fix: ``get_health`` derives the verdict from ``self._thread.is_alive()`` + the
age of the last COMPLETED pass, and ``_check_loop`` guards ``_check_all()`` so one
bad pass can't kill the loop.

Behavioural: constructs the REAL NodeWatchdog, drives the real ``get_health`` /
``_check_loop``, asserts observable verdicts and that the loop survives a raising
check pass. Imports clean (no full-app boot).

Run (dev box, targeted):
    python -m pytest tests/unit/test_node_watchdog_liveness.py -v \
        --noconftest -p no:cacheprovider
"""
import threading
import time

import pytest

from security.node_watchdog import NodeWatchdog


def _fresh(interval=0.1):
    # Pass a concrete interval so the verdict does not depend on the env default,
    # then tighten it for fast loop tests.
    wd = NodeWatchdog(check_interval=1)
    wd._check_interval = interval
    return wd


def _alive_thread():
    """A thread that stays alive until released, to stand in for a live loop."""
    gate = threading.Event()
    t = threading.Thread(target=lambda: gate.wait(3.0), daemon=True)
    t.start()
    return t, gate


def test_stopped_before_start():
    assert _fresh().get_health()['watchdog'] == 'stopped'


def test_dead_when_loop_thread_is_not_alive():
    # THE false-healthy bug: we still intend to run (_running=True) but the loop
    # thread is not alive. Old code said 'healthy'; the fix says 'dead'.
    wd = _fresh()
    wd._running = True
    wd._started_at = time.time()
    wd._thread = threading.Thread(target=lambda: None)  # never started -> not alive
    assert wd.get_health()['watchdog'] == 'dead'


def test_healthy_when_running_thread_alive_and_recent_check():
    wd = _fresh()
    wd._running = True
    wd._started_at = time.time()
    wd._thread, gate = _alive_thread()
    wd._last_check_at = time.time()
    try:
        assert wd.get_health()['watchdog'] == 'healthy'
    finally:
        gate.set()


def test_stalled_when_last_check_is_stale():
    # Alive but no pass completed within 2x the interval -> livelocked, report it.
    wd = _fresh(interval=0.1)
    wd._running = True
    wd._started_at = time.time() - 100
    wd._thread, gate = _alive_thread()
    wd._last_check_at = time.time() - 100
    try:
        assert wd.get_health()['watchdog'] == 'stalled'
    finally:
        gate.set()


def test_check_loop_survives_a_raising_check_pass(monkeypatch):
    # A _check_all() that raises every pass must NOT kill the loop thread. The old
    # unguarded loop died on the first exception; the guard logs and keeps looping.
    wd = _fresh(interval=0.05)
    calls = {'n': 0}

    def boom():
        calls['n'] += 1
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(wd, '_check_all', boom)
    wd.start()
    try:
        time.sleep(0.4)  # several intervals
        assert wd._thread.is_alive(), "loop thread died on a raising check pass"
        assert calls['n'] >= 2, "check pass was not retried after it raised"
        # Honest verdict: alive but no pass ever COMPLETED -> 'stalled', never a
        # crash and never a false 'healthy'/'dead'.
        assert wd.get_health()['watchdog'] == 'stalled'
    finally:
        wd.stop()


def test_last_check_age_exposed_for_staleness_alerts():
    wd = _fresh()
    # Never checked -> None (honest "unknown"), not a fabricated 0.
    assert wd.get_health()['last_check_age_seconds'] is None
    wd._last_check_at = time.time() - 5
    age = wd.get_health()['last_check_age_seconds']
    assert age is not None and age >= 4.5
