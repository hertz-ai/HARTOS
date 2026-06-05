"""Behavioural tests for should_yield_to_user() reason observability
(task #60, 2026-05-30).

The yield gate used to return True silently for any of FOUR reasons,
logging none of them.  That silence cost ~3 days of guessing on 2026-05-29
(the fix 18e59c0 patched the wrong reason) because nothing said WHICH of
the gate's conditions was actually blocking.  These tests pin that
should_yield_to_user() now records the specific blocking reason, exposed
via get_last_yield_reason(), so the daemon log + status probes can name it.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import integrations.agent_engine.dispatch as d  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_gate_globals():
    """Snapshot + restore the gate's module globals so tests don't leak
    user-activity / create-session state into one another."""
    saved = (d._active_create_sessions, d._last_user_chat_at, d._last_yield_reason)
    yield
    (d._active_create_sessions, d._last_user_chat_at, d._last_yield_reason) = saved


def _neutralize_model_pressure(monkeypatch):
    """Inject a fake model_lifecycle whose pressure is healthy so reason #2
    never fires (keeps the governor-reason tests deterministic regardless
    of whether the real module imports in this env)."""
    fake = types.ModuleType('integrations.service_tools.model_lifecycle')

    class _Mgr:
        def get_system_pressure(self):
            return {'throttle_factor': 1.0}

    fake.get_model_lifecycle_manager = lambda: _Mgr()
    monkeypatch.setitem(
        sys.modules, 'integrations.service_tools.model_lifecycle', fake)


def _set_governor_throttle(monkeypatch, value):
    import core.resource_governor as rg

    class _Gov:
        def get_throttle(self):
            return value

    monkeypatch.setattr(rg, 'get_governor', lambda: _Gov())


def test_create_in_flight_reason(monkeypatch):
    d._active_create_sessions = 1
    assert d.should_yield_to_user() is True
    assert d.get_last_yield_reason() == 'create_in_flight'


def test_user_active_reason(monkeypatch):
    d._active_create_sessions = 0
    d._last_user_chat_at = d._time.time()   # chatted right now
    assert d.should_yield_to_user() is True
    assert d.get_last_yield_reason() == 'user_active'


def test_governor_throttle_reason(monkeypatch):
    d._active_create_sessions = 0
    d._last_user_chat_at = 0.0              # epoch → not recently active
    _neutralize_model_pressure(monkeypatch)
    _set_governor_throttle(monkeypatch, 0.05)   # below the 0.3 gate
    assert d.should_yield_to_user() is True
    assert d.get_last_yield_reason() == 'governor_throttle'


def test_gate_open_returns_false_and_clears_reason(monkeypatch):
    d._active_create_sessions = 0
    d._last_user_chat_at = 0.0
    _neutralize_model_pressure(monkeypatch)
    _set_governor_throttle(monkeypatch, 1.0)    # healthy → gate open
    assert d.should_yield_to_user() is False
    assert d.get_last_yield_reason() is None


def test_user_active_takes_priority_over_governor(monkeypatch):
    """Reason ordering: an active user is reported as 'user_active' even if
    the governor is also throttled — the most user-relevant reason wins."""
    d._active_create_sessions = 0
    d._last_user_chat_at = d._time.time()
    _neutralize_model_pressure(monkeypatch)
    _set_governor_throttle(monkeypatch, 0.05)
    assert d.should_yield_to_user() is True
    assert d.get_last_yield_reason() == 'user_active'


def test_reason_transition_is_logged(monkeypatch, caplog):
    """Closing then opening the gate emits one INFO line each (transition
    only — no per-tick spam)."""
    import logging
    d._active_create_sessions = 0
    d._last_user_chat_at = 0.0
    _neutralize_model_pressure(monkeypatch)

    with caplog.at_level(logging.INFO, logger='hevolve_social'):
        _set_governor_throttle(monkeypatch, 0.05)
        d._last_yield_reason = None          # force a transition into CLOSED
        d.should_yield_to_user()
        _set_governor_throttle(monkeypatch, 1.0)
        d.should_yield_to_user()             # transition back to OPEN

    msgs = ' | '.join(r.getMessage() for r in caplog.records)
    assert 'governor_throttle' in msgs
    assert 'yield gate OPEN' in msgs
