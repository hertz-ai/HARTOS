"""A person at the desk makes every background daemon yield, via the ONE gate.

Measured on the Samsung box, generation 11, 2026-09-24 (agent-7 lane): the
Nunba-hosted governor logged idle -> active at 22:37:26 and held ACTIVE through
seven minutes of continuous synthetic input, and the in-process agent daemon
kept issuing a 278-token llama call every minute regardless (package 94 C,
clock 1.1 GHz, press p50 600-1500 ms against a 25 ms budget). The gate's
'governor_throttle' reason was meant to cover presence (ACTIVE -> 0.05), but
ACTIVE_CPU_LIMIT defaults to 0.50, above the 0.3 floor, so it never fired.

These pin the new reason and its fail-open shape:
  * a governor whose live monitor last saw the person -> yield, 'user_present'
  * the same governor after the monitor saw idle -> gate open
  * a governor with no monitor (never started, or stopped) -> False, so a
    process without one keeps ticking as before instead of stalling
  * a governor of the old shape (no user_present at all) -> gate open
  * chat activity still wins the reason name over presence
"""
from __future__ import annotations

import os
import sys
import time
import types
from unittest.mock import MagicMock

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.resource_governor as rg  # noqa: E402
import integrations.agent_engine.dispatch as d  # noqa: E402
from core.resource_governor import ResourceGovernor  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_gate_globals():
    saved = (d._active_create_sessions, d._last_user_chat_at, d._last_yield_reason)
    yield
    (d._active_create_sessions, d._last_user_chat_at, d._last_yield_reason) = saved


def _quiet_everything_else(monkeypatch):
    """No chat, no CREATE, healthy model pressure, no foreground request."""
    d._active_create_sessions = 0
    d._last_user_chat_at = 0.0
    fake = types.ModuleType('integrations.service_tools.model_lifecycle')

    class _Mgr:
        def get_system_pressure(self):
            return {'throttle_factor': 1.0}

    fake.get_model_lifecycle_manager = lambda: _Mgr()
    monkeypatch.setitem(sys.modules, 'integrations.service_tools.model_lifecycle', fake)
    import core.foreground as fg
    monkeypatch.setattr(fg, 'foreground_active', lambda: False, raising=False)


def _wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@pytest.fixture
def live_governor(monkeypatch):
    """A fresh governor whose only input is a controllable OS idle probe,
    the same shape tests/unit/test_governor_monitor_only.py uses."""
    monkeypatch.setattr(rg, '_MONITOR_INTERVAL_SECONDS', 0.05)
    monkeypatch.setattr(rg, 'get_enforcer', lambda: MagicMock())
    import security.node_watchdog as _nw
    monkeypatch.setattr(_nw, 'get_watchdog', lambda: None)
    gov = ResourceGovernor(idle_threshold_seconds=120)
    probe = {'idle_ms': 0.0}
    monkeypatch.setattr(gov, '_get_os_idle_ms', lambda: probe['idle_ms'])
    monkeypatch.setattr(gov, '_refresh_cpu_attribution', lambda: None)
    monkeypatch.setattr(gov, '_get_memory_pressure', lambda: 0.1)
    monkeypatch.setattr(gov, '_get_battery_status', lambda: (1.0, False))
    monkeypatch.setattr(gov, '_check_gpu_available', lambda: False)
    yield gov, probe
    gov.stop()


def test_a_governor_that_never_started_reports_nobody(live_governor):
    gov, _ = live_governor
    assert gov.user_present() is False


def test_the_live_monitor_reports_the_person_then_their_absence(live_governor):
    gov, probe = live_governor
    gov.start(monitor_only=True)
    assert _wait_for(gov.user_present), 'monitor sampled idle_ms=0 but user_present stayed False'
    probe['idle_ms'] = 10 * 60 * 1000
    assert _wait_for(lambda: not gov.user_present())
    gov.stop()
    probe['idle_ms'] = 0.0
    time.sleep(0.2)
    assert gov.user_present() is False, 'a stopped monitor must not vouch for anyone'


def test_the_gate_yields_to_the_person_with_the_new_reason(monkeypatch, live_governor):
    gov, _ = live_governor
    gov.start(monitor_only=True)
    assert _wait_for(gov.user_present)
    _quiet_everything_else(monkeypatch)
    monkeypatch.setattr(rg, 'get_governor', lambda: gov)
    assert d.should_yield_to_user() is True
    assert d.get_last_yield_reason() == 'user_present'


def test_the_gate_opens_again_when_the_person_leaves(monkeypatch, live_governor):
    gov, probe = live_governor
    gov.start(monitor_only=True)
    probe['idle_ms'] = 10 * 60 * 1000
    assert _wait_for(lambda: gov.get_mode() == rg.MODE_IDLE)
    _quiet_everything_else(monkeypatch)
    monkeypatch.setattr(rg, 'get_governor', lambda: gov)
    assert d.should_yield_to_user() is False
    assert d.get_last_yield_reason() is None


def test_an_old_shape_governor_without_the_method_keeps_the_gate_open(monkeypatch):
    _quiet_everything_else(monkeypatch)

    class _Gov:
        def get_throttle(self):
            return 1.0

    monkeypatch.setattr(rg, 'get_governor', lambda: _Gov())
    assert d.should_yield_to_user() is False


def test_a_mock_governor_cannot_fake_presence(monkeypatch):
    """MagicMock().user_present() is a truthy MagicMock, not True."""
    _quiet_everything_else(monkeypatch)
    gov = MagicMock()
    gov.get_throttle.return_value = 1.0
    monkeypatch.setattr(rg, 'get_governor', lambda: gov)
    assert d.should_yield_to_user() is False


def test_chat_activity_still_names_itself_over_presence(monkeypatch, live_governor):
    gov, _ = live_governor
    gov.start(monitor_only=True)
    assert _wait_for(gov.user_present)
    _quiet_everything_else(monkeypatch)
    d._last_user_chat_at = d._time.time()
    monkeypatch.setattr(rg, 'get_governor', lambda: gov)
    assert d.should_yield_to_user() is True
    assert d.get_last_yield_reason() == 'user_active'
