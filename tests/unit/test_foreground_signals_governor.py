"""NEW-GOV: enter_foreground signals the ResourceGovernor on the 0->1 edge.

WHY: this session's py-spy showed the autonomous daemon swarm pegging all 16
cores while the user was present, because the ResourceGovernor only flips to
ACTIVE off its own periodic CPU-attribution poll -- there was no DIRECT "a user
turn just started" signal. ``core/resource_governor.report_user_activity`` is
the single canonical mode-flip method but had ZERO callers (verified: only a
definition + a comment). The fix wires the one foreground 0->1 edge
(``enter_foreground``, which a GENUINE user /chat turn owns via mark_view's
_genuine_check) to that single method -- so the daemons back off immediately
instead of after the governor's poll.

Behavioral (no grep): import the real module, mock the governor boundary, call
enter_foreground, assert the observable effect (report_user_activity called).
Covers: fires once on the 0->1 edge only (not nested re-entries); fires again on
each fresh turn; fail-open (a raising governor never breaks the foreground gate
or cancellable firing).
"""
import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.foreground as fg  # noqa: E402


def _reset():
    """Drain any residual foreground count so each test starts at 0."""
    for _ in range(fg.in_flight()):
        fg.exit_foreground()


def test_zero_to_one_edge_signals_governor_once():
    _reset()
    gov = MagicMock()
    try:
        with patch('core.resource_governor.get_governor', return_value=gov):
            fg.enter_foreground()   # 0 -> 1: MUST signal the governor
            fg.enter_foreground()   # 1 -> 2: nested, MUST NOT signal again
        assert gov.report_user_activity.call_count == 1, (
            "report_user_activity must fire exactly once on the 0->1 edge, "
            f"got {gov.report_user_activity.call_count}")
    finally:
        _reset()


def test_each_fresh_turn_signals_again():
    _reset()
    gov = MagicMock()
    try:
        with patch('core.resource_governor.get_governor', return_value=gov):
            fg.enter_foreground(); fg.exit_foreground()   # turn 1 (0->1->0)
            fg.enter_foreground(); fg.exit_foreground()   # turn 2 (0->1->0)
        assert gov.report_user_activity.call_count == 2
    finally:
        _reset()


def test_governor_failure_does_not_break_foreground():
    _reset()
    fired = []
    cb = lambda: fired.append(True)  # noqa: E731
    fg.register_cancellable(cb)
    gov = MagicMock()
    gov.report_user_activity.side_effect = RuntimeError('boom')
    try:
        with patch('core.resource_governor.get_governor', return_value=gov):
            fg.enter_foreground()   # governor raises -> must NOT propagate
        # The cancellable (the foreground gate's real job) still fired, and the
        # governor signal was attempted -- fail-open, no regression.
        assert fired == [True], "cancellables must fire even if the governor signal raises"
        assert gov.report_user_activity.call_count == 1
    finally:
        fg.unregister_cancellable(cb)
        _reset()
