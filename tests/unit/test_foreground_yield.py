"""Foreground-request preemption (B1): background daemons yield the shared model
to a user chat being served right now.

Without this, the daemon's STARVATION OVERRIDE force-runs background goal
dispatches after 120 s of yielding — even while the user is mid-turn — saturating
the 4B draft model and timing out the reply.  ``core.foreground`` marks a request
in-flight; ``should_yield_to_user`` reports it as the highest-priority yield
reason and the daemon's override is suppressed while it's set.

Behavioural — exercises the real signal + the real gate.  No grep tests.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _clean_foreground():
    """Each test starts and ends with no in-flight foreground requests."""
    from core import foreground
    while foreground.in_flight() > 0:
        foreground.exit_foreground()
    yield
    while foreground.in_flight() > 0:
        foreground.exit_foreground()


def test_signal_balanced_and_floored():
    from core import foreground
    assert foreground.foreground_active() is False
    foreground.enter_foreground()
    assert foreground.foreground_active() is True and foreground.in_flight() == 1
    foreground.enter_foreground()
    assert foreground.in_flight() == 2
    foreground.exit_foreground()
    foreground.exit_foreground()
    assert foreground.foreground_active() is False
    foreground.exit_foreground()  # stray exit must not go negative
    assert foreground.in_flight() == 0


def test_context_manager_balances_on_exception():
    from core.foreground import foreground_request, foreground_active, in_flight
    assert not foreground_active()
    with pytest.raises(ValueError):
        with foreground_request():
            assert foreground_active()
            raise ValueError('boom')
    assert not foreground_active() and in_flight() == 0


def test_should_yield_reports_foreground_as_highest_priority_reason():
    """A request in flight makes should_yield_to_user yield with reason
    'foreground_request' — the #0 reason, ahead of recent-activity/pressure."""
    from integrations.agent_engine import dispatch
    from core.foreground import foreground_request
    with foreground_request():
        assert dispatch.should_yield_to_user() is True
        assert dispatch.get_last_yield_reason() == 'foreground_request'


def test_no_foreground_does_not_force_the_reason():
    """With nothing in flight (and no recent activity), the gate does NOT report
    foreground_request — so background work isn't needlessly blocked when the
    user is away."""
    from integrations.agent_engine import dispatch
    with patch.object(dispatch, 'is_user_recently_active', return_value=False):
        dispatch.should_yield_to_user()
    assert dispatch.get_last_yield_reason() != 'foreground_request'


def test_mark_view_marks_foreground_for_the_call_only():
    """The shared mark_view decorator (used by BOTH the HARTOS /chat route and
    the bundled Nunba chat_route) marks foreground for the call's duration."""
    from core.foreground import mark_view, foreground_active, in_flight

    @mark_view
    def handler():
        assert foreground_active() is True
        return 'ok'

    assert foreground_active() is False
    assert handler() == 'ok'
    assert foreground_active() is False and in_flight() == 0


def test_mark_view_balances_on_exception():
    from core.foreground import mark_view, foreground_active, in_flight

    @mark_view
    def boom():
        raise ValueError('x')

    with pytest.raises(ValueError):
        boom()
    assert foreground_active() is False and in_flight() == 0


# ── The override block itself (agent_daemon._tick) ─────────────────────────
#
# The tests above pin the SIGNAL and the GATE.  These drive the real _tick and
# pin what the STARVATION OVERRIDE does with them: it fires only after the
# starvation window, never during a foreground request, and, since 2026-09-22,
# never while the ResourceGovernor says the machine is not idle.
#
# Measured that day on the Samsung box (NATIVE_OS_PROGRAM.md section 1): the
# override force-ticked every 120 s on 'model_pressure' with the owner at the
# desk, llama-server at 207 percent CPU once a minute, press p50 122 ms against
# a 25 ms budget; with the daemons paused, 12 ms.  A person clicking around the
# desktop is neither a foreground request nor "recently active" (that means
# chatted), so the governor, whose Linux backend reads the compositor's
# input-alive marker, is the reader the override has to honour.  It does so
# through _idle_only_blocked, the one reader every idle_only goal already uses.

import logging
import time
from unittest.mock import MagicMock


@pytest.fixture
def governor_mode():
    """Pin the governor's mode for one test, restoring it afterwards (the
    same idiom tests/unit/test_paper_explanation_goal.py uses)."""
    from core.resource_governor import get_governor
    gov = get_governor()
    prev = gov._mode

    def _set(mode):
        gov._mode = mode

    yield _set
    gov._mode = prev


@pytest.fixture
def starved_daemon(monkeypatch):
    """A daemon whose yield gate has said 'model_pressure' for longer than the
    starvation window.  Everything past the override is stubbed so _tick
    either returns at the gate, or proves it went through by opening the DB
    (the first thing the tick does after the gate)."""
    from integrations.agent_engine import dispatch, agent_daemon
    import integrations.social.models as models
    import security.hive_guardrails as hg
    import integrations.service_tools.model_lifecycle as ml

    monkeypatch.setattr(dispatch, 'should_yield_to_user', lambda: True)
    monkeypatch.setattr(dispatch, 'get_last_yield_reason',
                        lambda: 'model_pressure')
    monkeypatch.setattr(hg.HiveCircuitBreaker, 'is_halted',
                        classmethod(lambda cls: False))
    monkeypatch.setattr(
        ml, 'get_model_lifecycle_manager',
        lambda: MagicMock(get_system_pressure=lambda: {'throttle_factor': 0.05}))

    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    opened = {'n': 0}

    def _get_db():
        opened['n'] += 1
        return db

    monkeypatch.setattr(models, 'get_db', _get_db)

    d = agent_daemon.AgentDaemon()
    d._last_tick_completed_at = time.monotonic() - (d._starvation_s + 60)
    return d, opened


def test_override_fires_when_the_governor_says_idle(starved_daemon, governor_mode):
    from core.resource_governor import MODE_IDLE
    daemon, opened = starved_daemon
    governor_mode(MODE_IDLE)
    daemon._tick()
    assert opened['n'] == 1, (
        "starved, nobody at the desk, no foreground request: the override "
        "must force the tick through to the goal query")


@pytest.mark.parametrize('mode', ['active', 'sleep'])
def test_override_yields_while_the_governor_says_not_idle(
        starved_daemon, governor_mode, caplog, mode):
    daemon, opened = starved_daemon
    governor_mode(mode)
    caplog.set_level(logging.DEBUG, logger='hevolve_social')
    daemon._tick()
    assert opened['n'] == 0, (
        f"governor mode {mode!r}: the override must not force inference "
        f"onto a machine the one idle detector says is in use")
    assert any('starvation override suppressed' in r.getMessage()
               and 'governor' in r.getMessage() for r in caplog.records), (
        "the yield must be logged like its foreground sibling, naming the "
        "governor so the journal says why the queue did not drain")


def test_override_fails_closed_when_the_governor_is_unreadable(starved_daemon):
    daemon, opened = starved_daemon
    with patch('core.resource_governor.get_governor',
               side_effect=RuntimeError('governor down')):
        daemon._tick()
    assert opened['n'] == 0, (
        "same contract as _idle_only_blocked: idle means PROVEN idle; an "
        "unreadable governor is not a licence to run inference at the desk")


def test_override_stays_suppressed_by_a_foreground_request_even_when_idle(
        starved_daemon, governor_mode):
    """B1 is kept: the governor check is added after it, not instead of it."""
    from core.resource_governor import MODE_IDLE
    from core.foreground import foreground_request
    daemon, opened = starved_daemon
    governor_mode(MODE_IDLE)
    with foreground_request():
        daemon._tick()
    assert opened['n'] == 0


def test_no_override_before_the_starvation_window(starved_daemon, governor_mode):
    from core.resource_governor import MODE_IDLE
    daemon, opened = starved_daemon
    governor_mode(MODE_IDLE)
    daemon._last_tick_completed_at = time.monotonic()
    daemon._tick()
    assert opened['n'] == 0, (
        "a gate that has only just started blocking is honoured as is; the "
        "override is for STARVATION, and idle alone does not shorten the window")


# ── The mode the override reads is LIVE in the daemon process ──────────────
#
# hart-agent-daemon.service is its own process and only the backend ever
# started a governor, so the mode above was the constructor's MODE_ACTIVE for
# the life of the daemon (found 2026-09-23).  run_forever, the unit's one entry
# point, now starts the governor monitor only (no enforcer, no proactive
# stream; see ResourceGovernor.start), so the override reads real idle.

def _wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@pytest.fixture
def live_governor(monkeypatch):
    """A fresh governor installed as the process singleton, driven by one
    controllable OS idle probe, every other per-tick read pinned calm
    (the same harness as tests/unit/test_governor_monitor_only.py)."""
    import core.resource_governor as rg
    monkeypatch.setattr(rg, '_MONITOR_INTERVAL_SECONDS', 0.05)
    monkeypatch.setattr(rg, 'get_enforcer', lambda: MagicMock())
    import security.node_watchdog as _nw
    monkeypatch.setattr(_nw, 'get_watchdog', lambda: None)
    gov = rg.ResourceGovernor(idle_threshold_seconds=120)
    probe = {'idle_ms': 0.0}
    monkeypatch.setattr(gov, '_get_os_idle_ms', lambda: probe['idle_ms'])
    monkeypatch.setattr(gov, '_refresh_cpu_attribution', lambda: None)
    monkeypatch.setattr(gov, '_get_memory_pressure', lambda: 0.1)
    monkeypatch.setattr(gov, '_get_battery_status', lambda: (1.0, False))
    monkeypatch.setattr(gov, '_check_gpu_available', lambda: False)
    monkeypatch.setattr(rg, '_governor', gov)
    yield gov, probe
    gov.stop()


def test_run_forever_starts_the_governor_monitor_once(live_governor, monkeypatch):
    from integrations.agent_engine.agent_daemon import AgentDaemon
    gov, probe = live_governor
    d = AgentDaemon()
    # The worker is not under test: leave _thread None so run_forever returns
    # instead of joining forever.
    monkeypatch.setattr(d, 'start', lambda: None)
    d.run_forever()
    assert gov._running and gov._monitor_thread is not None
    assert gov._monitor_thread.is_alive()
    assert gov._proactive_thread is None, (
        "the daemon must start the governor MONITOR ONLY; the proactive "
        "stream and the enforcer stay in the backend")
    first = gov._monitor_thread
    d.run_forever()                                # a supervisor re-entry
    assert gov._monitor_thread is first, "one monitor per process, ever"


def test_run_forever_survives_a_governor_that_will_not_start(monkeypatch):
    """Best-effort: the goal engine must start even if the governor faults;
    the idle reads then fail closed to 'not idle' as before."""
    from integrations.agent_engine.agent_daemon import AgentDaemon
    d = AgentDaemon()
    monkeypatch.setattr(d, 'start', lambda: None)
    with patch('core.resource_governor.get_governor',
               side_effect=RuntimeError('governor down')):
        d.run_forever()                            # must not raise


def test_override_follows_the_live_mode_in_the_daemon_process(
        starved_daemon, live_governor):
    """End to end in one process: the probe says idle, the override fires;
    the probe says a click just happened, the override yields."""
    from core.resource_governor import MODE_ACTIVE, MODE_IDLE
    daemon, opened = starved_daemon
    gov, probe = live_governor
    probe['idle_ms'] = 10 * 60 * 1000
    gov.start(monitor_only=True)
    assert _wait_for(lambda: gov.get_mode() == MODE_IDLE)
    daemon._tick()
    assert opened['n'] == 1, "idle per the live governor: the forced tick runs"

    probe['idle_ms'] = 0.0
    assert _wait_for(lambda: gov.get_mode() == MODE_ACTIVE)
    daemon._last_tick_completed_at = time.monotonic() - (daemon._starvation_s + 60)
    daemon._tick()
    assert opened['n'] == 1, (
        "a click reached the compositor: the override must yield, whatever "
        "the yield gate's own reason is")
