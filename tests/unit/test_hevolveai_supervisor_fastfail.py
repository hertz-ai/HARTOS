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
    assert hs._hevolveai_available() is False


# ── Defense-in-depth: fast-fail breaker in the _run loop ──────────

def test_run_loop_disables_after_consecutive_fast_fails(monkeypatch):
    """A child that's importable but exits rc=1 in <5s on every spawn
    must DISABLE the supervisor after 5 consecutive fast-fails instead
    of looping at the 60s cap forever.  We drive the real _run loop
    with a fake Popen that returns instantly."""
    from integrations.agent_engine import hevolveai_supervisor as hs

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

    spawn_count = {'n': 0}

    class _InstantDeadProc:
        """Popen stand-in: already-exited child (rc=1), no stdout."""
        def __init__(self):
            self.pid = 4242
            self.stdout = None
            self._handle = 0
        def wait(self):
            return 1  # immediate rc=1

    def _fake_popen_kwargs():
        return {}
    def _fake_build_cmd():
        return ['python', '-c', 'raise SystemExit(1)']
    def _fake_build_env():
        return {}

    monkeypatch.setattr(sup, '_popen_kwargs', _fake_popen_kwargs)
    monkeypatch.setattr(sup, '_build_cmd', _fake_build_cmd)
    monkeypatch.setattr(sup, '_build_env', _fake_build_env)
    monkeypatch.setattr(sup, '_register_with_governor', lambda pid: None)
    monkeypatch.setattr(sup, '_unregister_from_governor', lambda pid: None)

    def _fake_popen(cmd, env=None, **kw):
        spawn_count['n'] += 1
        return _InstantDeadProc()
    monkeypatch.setattr(hs.subprocess, 'Popen', _fake_popen)

    # Make stop_event.wait() a no-op (don't actually sleep the backoff)
    # but still return False so the loop continues until the breaker.
    monkeypatch.setattr(sup.stop_event, 'wait', lambda timeout=None: False)
    # Keep last_started "now" so uptime is ~0 (<5s fast-fail).
    monkeypatch.setattr(hs.time, 'time', lambda: 1000.0)

    # Run with a hard cap so a broken breaker can't hang the test.
    import threading
    t = threading.Thread(target=sup._run, daemon=True)
    t.start()
    t.join(timeout=5)

    assert not t.is_alive(), (
        "the _run loop did not terminate — fast-fail breaker missing or "
        "broken; it would spin forever on a permanently-crashing child")
    # Breaker trips at 5 consecutive fast-fails (spawns 5, then returns).
    assert spawn_count['n'] == 5, (
        f"expected exactly 5 spawns before disabling, got {spawn_count['n']}")
    assert 'DISABLING' in (sup.last_error or '')
