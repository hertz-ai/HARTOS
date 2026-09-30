"""ResourceGovernor.start(monitor_only=True): a live mode with nothing else.

hart-agent-daemon.service is its own process, and only the backend
(hart_intelligence_entry.py) ever called governor.start(), so in the daemon
get_mode() was the constructor's MODE_ACTIVE for the life of the process:
_idle_only_blocked never let an idle_only goal run there, and the starvation
override, once it honoured the governor (832eece), was suppressed outright
(found 2026-09-23 on the Samsung box).

monitor_only extends the ONE existing start(): the monitor thread runs, so the
mode follows the OS idle probe; the enforcer and the proactive stream do not.
The enforcer is per process and its _unrestrict_llm_affinity pins llama-server
to every core by port lookup, which from a second process would undo the
taskset pin hart-llm.nix applies (measured 2026-09-23: llama-server on cpus
2,3,6,7 with the pin holding).  These tests drive the REAL start() and
_monitor_loop with the per-tick probes stubbed cheap, the same shape as
test_governor_monitor_no_spin.py.  No grep tests.
"""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.resource_governor as rg  # noqa: E402
from core.resource_governor import (  # noqa: E402
    ResourceGovernor, MODE_ACTIVE, MODE_IDLE)


def _wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@pytest.fixture
def live_governor(monkeypatch):
    """A fresh governor whose ONLY input is a controllable OS idle probe.

    Every other per-tick read is pinned calm (no external load, no memory
    pressure, on mains) so the mode is decided by idle alone.  The enforcer
    singleton is replaced by a mock so the test can prove it was not armed
    and so a prior test's armed enforcer cannot leak in.  Yields
    (gov, probe, enforcer); probe['idle_ms'] drives the mode."""
    monkeypatch.setattr(rg, '_MONITOR_INTERVAL_SECONDS', 0.05)
    enforcer = MagicMock()
    monkeypatch.setattr(rg, 'get_enforcer', lambda: enforcer)
    import security.node_watchdog as _nw
    monkeypatch.setattr(_nw, 'get_watchdog', lambda: None)

    gov = ResourceGovernor(idle_threshold_seconds=120)
    probe = {'idle_ms': 0.0}
    monkeypatch.setattr(gov, '_get_os_idle_ms', lambda: probe['idle_ms'])
    monkeypatch.setattr(gov, '_refresh_cpu_attribution', lambda: None)
    monkeypatch.setattr(gov, '_get_memory_pressure', lambda: 0.1)
    monkeypatch.setattr(gov, '_get_battery_status', lambda: (1.0, False))
    monkeypatch.setattr(gov, '_check_gpu_available', lambda: False)
    yield gov, probe, enforcer
    gov.stop()


def test_monitor_only_runs_the_monitor_and_nothing_else(live_governor):
    gov, probe, enforcer = live_governor
    gov.start(monitor_only=True)
    try:
        assert gov._monitor_thread is not None and gov._monitor_thread.is_alive()
        assert gov._proactive_thread is None, (
            "monitor only must start no proactive stream: hive task dispatch "
            "and benchmarks belong to the backend's governor, the one copy")
        assert not enforcer.enforce.called
        assert not enforcer._set_process_priority.called
        assert not enforcer._enforce_cpu.called, (
            "monitor only must not arm the enforcer: from a second process "
            "_unrestrict_llm_affinity would undo hart-llm.nix's taskset pin")
    finally:
        gov.stop()
    assert not gov._monitor_thread and not gov._proactive_thread


def test_monitor_only_mode_follows_the_os_idle_probe(live_governor):
    """The whole point: get_mode() in this process changes with the OS."""
    gov, probe, enforcer = live_governor
    assert gov.get_mode() == MODE_ACTIVE          # the constructor's value
    probe['idle_ms'] = 10 * 60 * 1000             # nobody at the desk
    gov.start(monitor_only=True)
    assert _wait_for(lambda: gov.get_mode() == MODE_IDLE), (
        "with the OS idle past the threshold the monitor must move to IDLE")
    probe['idle_ms'] = 0.0                        # a click
    assert _wait_for(lambda: gov.get_mode() == MODE_ACTIVE), (
        "the next tick after input must move back to ACTIVE")
    # Two transitions happened and the enforcer was never armed by them:
    # _transition_to calls update_caps, which is a no-op while unarmed.
    assert not enforcer.enforce.called
    assert not enforcer._enforce_cpu.called


def test_idle_only_reader_sees_the_live_mode(live_governor, monkeypatch):
    """_idle_only_blocked (the reader the override and idle_only goals share)
    follows the same live mode, through the real singleton accessor."""
    from integrations.agent_engine.agent_daemon import _idle_only_blocked
    gov, probe, _ = live_governor
    monkeypatch.setattr(rg, '_governor', gov)
    probe['idle_ms'] = 10 * 60 * 1000
    gov.start(monitor_only=True)
    assert _wait_for(lambda: gov.get_mode() == MODE_IDLE)
    assert _idle_only_blocked({'idle_only': True}) is False
    probe['idle_ms'] = 0.0
    assert _wait_for(lambda: gov.get_mode() == MODE_ACTIVE)
    assert _idle_only_blocked({'idle_only': True}) is True


def test_start_is_idempotent_across_monitor_only_and_full(live_governor):
    """One governor per process: a second start() of either kind is a no-op,
    so the daemon's monitor-only start can never spawn beside a full one."""
    gov, probe, enforcer = live_governor
    gov.start(monitor_only=True)
    first = gov._monitor_thread
    gov.start()                                   # full start, ignored
    assert gov._monitor_thread is first
    assert gov._proactive_thread is None
    assert not enforcer.enforce.called


def test_full_start_is_unchanged(live_governor):
    """The backend's call site (hart_intelligence_entry.py) still gets the
    enforcer and the proactive stream by default."""
    gov, probe, enforcer = live_governor
    gov.start()
    try:
        assert enforcer.enforce.called
        assert gov._proactive_thread is not None and gov._proactive_thread.is_alive()
    finally:
        gov.stop()
