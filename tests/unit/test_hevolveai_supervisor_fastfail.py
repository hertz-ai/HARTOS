"""Behavioural tests for the hevolveai_supervisor crash-loop guards
(2026-05-29 log review, task #59).

ROOT (frozen_debug.log): on a box without hevolveai installed, the
supervisor spawned a child that exits rc=1 in <1s and looped at the
60s backoff cap forever — 889 'hevolveai exited rc=1' lines + 14
spurious 'AssignProcessToJobObject failed (err=0)' (binding a process
that already died).

TWO guards added:
1. PRIMARY — supervisor_should_run() returns False when hevolveai is
   neither importable nor resolvable via a dev sibling repo, so the
   supervisor never starts (no thread, no crash loop).
2. DEFENSE-IN-DEPTH — the _run loop counts consecutive sub-5s exits
   and disables after 5 in a row (catches a child that IS importable
   but crashes anyway — broken install / missing weights).

These tests pin both guards without spawning real subprocesses.
"""
from __future__ import annotations

import os
import sys
import time

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── Primary guard: supervisor_should_run gates on availability ────

def test_should_run_false_when_hevolveai_unavailable(monkeypatch):
    """When hevolveai is not importable AND no dev sibling repo
    resolves, supervisor_should_run() must return False so
    start_supervisor never spawns the crash-looping child."""
    from integrations.agent_engine import hevolveai_supervisor as hs
    monkeypatch.delenv('HEVOLVE_SKIP_HEVOLVEAI_SPAWN', raising=False)
    monkeypatch.delenv('HEVOLVEAI_API_URL', raising=False)
    monkeypatch.setattr(hs, '_hevolveai_available', lambda: False)
    assert hs.supervisor_should_run() is False


def test_should_run_true_when_available(monkeypatch):
    from integrations.agent_engine import hevolveai_supervisor as hs
    monkeypatch.delenv('HEVOLVE_SKIP_HEVOLVEAI_SPAWN', raising=False)
    monkeypatch.delenv('HEVOLVEAI_API_URL', raising=False)
    monkeypatch.setattr(hs, '_hevolveai_available', lambda: True)
    assert hs.supervisor_should_run() is True


def test_optout_still_wins_over_availability(monkeypatch):
    """The explicit opt-out env must short-circuit even when hevolveai
    IS available."""
    from integrations.agent_engine import hevolveai_supervisor as hs
    monkeypatch.setenv('HEVOLVE_SKIP_HEVOLVEAI_SPAWN', '1')
    monkeypatch.setattr(hs, '_hevolveai_available', lambda: True)
    assert hs.supervisor_should_run() is False


def test_start_supervisor_reports_not_installed_reason(monkeypatch):
    """start_supervisor must return should_run=False with a clear
    'not installed' reason (not the generic opt-out message) so
    operators know why it's disabled — and must NOT create an
    instance / thread."""
    from integrations.agent_engine import hevolveai_supervisor as hs
    monkeypatch.delenv('HEVOLVE_SKIP_HEVOLVEAI_SPAWN', raising=False)
    monkeypatch.delenv('HEVOLVEAI_API_URL', raising=False)
    monkeypatch.setattr(hs, '_hevolveai_available', lambda: False)
    # Ensure no leftover singleton.
    monkeypatch.setattr(hs, '_INSTANCE', None)

    result = hs.start_supervisor()
    assert result['should_run'] is False
    assert 'not installed' in result['reason'].lower()
    # No supervisor instance created.
    assert hs._INSTANCE is None


def test_hevolveai_available_true_when_find_spec_hits(monkeypatch):
    """_hevolveai_available returns True when importlib finds the
    module (the frozen-Nunba-with-bundled-pyd case)."""
    from integrations.agent_engine import hevolveai_supervisor as hs
    import importlib.util as ilu

    class _FakeSpec: ...
    monkeypatch.setattr(ilu, 'find_spec',
                        lambda name: _FakeSpec() if name == 'hevolveai' else None)
    assert hs._hevolveai_available() is True


def test_hevolveai_available_true_via_dev_pythonpath(monkeypatch):
    """Even when not importable in-process, a resolved dev sibling
    repo path means the child CAN import it → available."""
    from integrations.agent_engine import hevolveai_supervisor as hs
    import importlib.util as ilu
    monkeypatch.setattr(ilu, 'find_spec', lambda name: None)
    monkeypatch.setattr(hs, '_resolve_hevolveai_pythonpath',
                        lambda: '/dev/Hevolveai/src')
    assert hs._hevolveai_available() is True


def test_hevolveai_available_false_when_neither(monkeypatch):
    from integrations.agent_engine import hevolveai_supervisor as hs
    import importlib.util as ilu
    monkeypatch.setattr(ilu, 'find_spec', lambda name: None)
    monkeypatch.setattr(hs, '_resolve_hevolveai_pythonpath', lambda: None)
    # 'neither' now includes the repo-mode probe (2026-08-09): no checkout.
    monkeypatch.setattr(hs, '_resolve_repo_root', lambda: None)
    assert hs._hevolveai_available() is False


# ── Defense-in-depth: fast-fail breaker in the _run loop ──────────
#
# CONTRACT CHANGE 2026-08-09: the breaker no longer DISABLES the
# supervisor (the 2026-08-06 trip left the brain dead for 30+ hours with
# nothing to re-arm it). It now COOLS DOWN AND RE-ARMS: on trip it
# resets its counters and requests one long extra wait from the base
# loop (extra_wait_once), then keeps supervising.

def _mk_sup(hs):
    sup = hs._Supervisor.__new__(hs._Supervisor)
    sup.stop_event = __import__('threading').Event()
    sup.lock = __import__('threading').Lock()
    sup.last_error = None
    sup.last_started = None
    sup.restart_count = 0
    sup.port = 8000
    # Fields the _run spawn-log line references.
    sup.pythonpath = None
    sup.python_exe = 'python'
    sup.api_url = 'http://localhost:8000'
    sup.proc = None
    sup.repo_root = None
    sup.repo_python = None
    sup.extra_wait_once = 0.0
    sup._consecutive_fast_fails = 0
    sup._consecutive_unhealthy = 0
    sup._current_pid = None
    return sup


def test_run_loop_cools_down_after_consecutive_fast_fails(monkeypatch):
    """A child that's importable but exits rc=1 in <5s on every spawn
    must trip the breaker after 5 consecutive fast-fails: counters
    reset, ONE long cooldown wait is requested from the base loop, and
    supervision continues (no permanent disable). We drive the real
    _run loop with a fake Popen that returns instantly and stop it at
    the cooldown wait."""
    from integrations.agent_engine import hevolveai_supervisor as hs

    sup = _mk_sup(hs)
    spawn_count = {'n': 0}
    waits = []

    class _InstantDeadProc:
        """Popen stand-in: already-exited child (rc=1), no stdout."""
        def __init__(self):
            self.pid = 4242
            self.stdout = None
            self._handle = 0
        def wait(self):
            return 1  # immediate rc=1

    monkeypatch.setattr(sup, '_popen_kwargs', lambda: {})
    monkeypatch.setattr(sup, '_build_cmd',
                        lambda: ['python', '-c', 'raise SystemExit(1)'])
    monkeypatch.setattr(sup, '_build_env', lambda: {})
    monkeypatch.setattr(sup, '_register_with_governor', lambda pid: None)
    monkeypatch.setattr(sup, '_unregister_from_governor', lambda pid: None)

    def _fake_popen(cmd, env=None, **kw):
        spawn_count['n'] += 1
        return _InstantDeadProc()
    monkeypatch.setattr(hs.subprocess, 'Popen', _fake_popen)

    # Record every backoff wait; STOP the loop the moment the cooldown
    # (>= 1800s) is requested -- that is the observable breaker trip.
    def _record_wait(timeout=None):
        waits.append(timeout)
        return timeout is not None and timeout >= 1800.0
    monkeypatch.setattr(sup.stop_event, 'wait', _record_wait)
    # Keep last_started "now" so uptime is ~0 (<5s fast-fail).
    monkeypatch.setattr(hs.time, 'time', lambda: 1000.0)

    import threading
    t = threading.Thread(target=sup._run, daemon=True)
    t.start()
    t.join(timeout=5)

    assert not t.is_alive(), (
        "the _run loop never requested the cooldown wait — breaker "
        "missing or broken; it would spin at the 60s cap forever")
    # Breaker trips at 5 consecutive fast-fails (5 spawns, then cooldown).
    assert spawn_count['n'] == 5, (
        f"expected exactly 5 spawns before the cooldown, got {spawn_count['n']}")
    assert waits and waits[-1] >= 1800.0, waits
    assert 'cooling down' in (sup.last_error or '')
    # Re-armed: counters reset and the one-shot wait consumed.
    assert sup._consecutive_fast_fails == 0
    assert sup._consecutive_unhealthy == 0
    assert sup.extra_wait_once == 0.0


def test_run_loop_cools_down_after_consecutive_SLOW_crashes(monkeypatch):
    """A child that loads, runs ~15s (NOT a sub-5s fast-fail), then crashes
    EVERY time must ALSO trip — after _UNHEALTHY_LIMIT (6) consecutive
    sub-60s exits — into the same cooldown-and-rearm (the 2026-06-01
    hevolveai 14-22s crash-loop class)."""
    from integrations.agent_engine import hevolveai_supervisor as hs

    sup = _mk_sup(hs)
    spawn_count = {'n': 0}
    waits = []

    class _SlowDeadProc:
        def __init__(self):
            self.pid = 4242
            self.stdout = None
            self._handle = 0
        def wait(self):
            return 1  # crashes — but uptime is ~15s via the time mock below

    monkeypatch.setattr(sup, '_popen_kwargs', lambda: {})
    monkeypatch.setattr(sup, '_build_cmd', lambda: ['python', '-c', 'pass'])
    monkeypatch.setattr(sup, '_build_env', lambda: {})
    monkeypatch.setattr(sup, '_register_with_governor', lambda pid: None)
    monkeypatch.setattr(sup, '_unregister_from_governor', lambda pid: None)

    def _fake_popen(cmd, env=None, **kw):
        spawn_count['n'] += 1
        return _SlowDeadProc()
    monkeypatch.setattr(hs.subprocess, 'Popen', _fake_popen)

    def _record_wait(timeout=None):
        waits.append(timeout)
        return timeout is not None and timeout >= 1800.0
    monkeypatch.setattr(sup.stop_event, 'wait', _record_wait)

    # Monotonic clock advancing 15s per call → each spawn's uptime computes to
    # ~15s: above the 5s fast-fail floor but below the 60s unhealthy floor.
    _clock = {'t': 1000.0}
    def _tick():
        _clock['t'] += 15.0
        return _clock['t']
    monkeypatch.setattr(hs.time, 'time', _tick)

    import threading
    t = threading.Thread(target=sup._run, daemon=True)
    t.start()
    t.join(timeout=5)

    assert not t.is_alive(), (
        "the _run loop never requested the cooldown wait — the SLOW-crash "
        "breaker is missing; a 15s crash-loop would respawn forever")
    # 15s uptime is NOT a fast-fail, so only the unhealthy breaker fires → 6.
    assert spawn_count['n'] == 6, (
        f"expected 6 spawns before the cooldown, got {spawn_count['n']}")
    assert waits and waits[-1] >= 1800.0, waits
    assert 'cooling down' in (sup.last_error or '')
    assert sup._consecutive_unhealthy == 0
    assert sup.extra_wait_once == 0.0
