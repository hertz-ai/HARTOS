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
