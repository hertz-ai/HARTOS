"""mark_view must NOT mark foreground for the daemon's own /chat dispatches.

The daemon reuses /chat for autonomous goal dispatch (request_id 'daemon_<goal>').
Before this gate, mark_view entered foreground for EVERY /chat — so the daemon's
own dispatch tripped the 0->1 foreground edge, the user's real "hi" arrived as the
1->2 edge, and _fire_cancellables() never fired => the flywheel's in-flight 4B
calls were never aborted and the user's turn was starved (2026-06-13 dig). The fix
makes mark_view consult the SAME discriminator the monkeypatch already uses
(is_genuine_user_request), via a registered check so the single mark_view source
serves both the HARTOS /chat and the bundled Nunba chat_route.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.foreground as fg  # noqa: E402


def _reset():
    fg.set_genuine_check(None)
    with fg._cancel_lock:
        fg._cancellables.clear()
    # drain any leftover foreground count
    while fg.in_flight() > 0:
        fg.exit_foreground()


class TestMarkViewGenuineGate:
    def teardown_method(self):
        _reset()

    def test_default_no_check_marks_foreground(self):
        # back-compat: with no check registered, every view marks foreground.
        _reset()
        seen = {}

        @fg.mark_view
        def handler():
            seen['fg'] = fg.foreground_active()
        handler()
        assert seen['fg'] is True
        assert fg.in_flight() == 0  # balanced exit

    def test_non_genuine_skips_foreground(self):
        _reset()
        fg.set_genuine_check(lambda: False)  # daemon request
        seen = {}

        @fg.mark_view
        def handler():
            seen['fg'] = fg.foreground_active()
        handler()
        assert seen['fg'] is False  # daemon /chat does NOT mark foreground
        assert fg.in_flight() == 0

    def test_genuine_marks_foreground(self):
        _reset()
        fg.set_genuine_check(lambda: True)  # user request
        seen = {}

        @fg.mark_view
        def handler():
            seen['fg'] = fg.foreground_active()
        handler()
        assert seen['fg'] is True
        assert fg.in_flight() == 0

    def test_daemon_does_not_consume_the_abort_edge(self):
        # THE fix: a daemon call must not fire/consume the 0->1 edge, so a later
        # genuine user call still triggers the abort of background work.
        _reset()
        fired = {'n': 0}
        fg.register_cancellable(lambda: fired.__setitem__('n', fired['n'] + 1))

        fg.set_genuine_check(lambda: False)  # daemon

        @fg.mark_view
        def daemon_handler():
            return None
        daemon_handler()
        assert fired['n'] == 0, "daemon /chat wrongly fired the foreground abort"

        fg.set_genuine_check(lambda: True)  # now a real user turn

        @fg.mark_view
        def user_handler():
            return None
        user_handler()
        assert fired['n'] == 1, "genuine user turn failed to fire the abort edge"

    def test_check_exception_fails_open_to_foreground(self):
        # never starve a real user turn: a broken check is treated as genuine.
        _reset()
        def boom():
            raise RuntimeError('discriminator unavailable')
        fg.set_genuine_check(boom)
        seen = {}

        @fg.mark_view
        def handler():
            seen['fg'] = fg.foreground_active()
        handler()
        assert seen['fg'] is True  # fail-open
