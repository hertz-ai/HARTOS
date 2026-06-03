"""dispatch.is_transient_deferral — the daemon uses this so a goal isn't
AUTO-PAUSED when dispatch_goal returns None for a TRANSIENT reason (user actively
using the LLM, or the Tier-2 breaker open) rather than a real failure.

Without it, 5 ticks of "user active" deferral auto-paused a perfectly healthy
goal — the "goals stuck / 0 progress" symptom on a machine the user actually
uses. Behavioural: composes the real predicate over its two checks. No grep tests.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_not_transient_when_neither_condition_holds(monkeypatch):
    from integrations.agent_engine import dispatch
    monkeypatch.setattr(dispatch, 'is_user_recently_active', lambda: False)
    monkeypatch.setattr(dispatch, '_cb_is_open', lambda: False)
    # Neither → a real failure → must NOT be treated as transient (so the
    # normal backoff/auto-pause path still applies to genuine failures).
    assert dispatch.is_transient_deferral() is False


def test_transient_when_user_active(monkeypatch):
    from integrations.agent_engine import dispatch
    monkeypatch.setattr(dispatch, 'is_user_recently_active', lambda: True)
    monkeypatch.setattr(dispatch, '_cb_is_open', lambda: False)
    assert dispatch.is_transient_deferral() is True


def test_transient_when_breaker_open(monkeypatch):
    from integrations.agent_engine import dispatch
    monkeypatch.setattr(dispatch, 'is_user_recently_active', lambda: False)
    monkeypatch.setattr(dispatch, '_cb_is_open', lambda: True)
    assert dispatch.is_transient_deferral() is True


def test_failsafe_to_not_transient_on_error(monkeypatch):
    """If a check raises, fall back to NOT transient — never swallow a genuine
    failure (a real None must still be able to reach auto-pause)."""
    from integrations.agent_engine import dispatch

    def _boom():
        raise RuntimeError('check broke')

    monkeypatch.setattr(dispatch, 'is_user_recently_active', _boom)
    assert dispatch.is_transient_deferral() is False
