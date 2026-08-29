"""A dead peer link must retire itself, never spin a CPU core.

THE BUG, measured on the live node 2026-08-29. `hart-discovery` burned 98.8% of
one core continuously. Exactly one thread was hot; 400 out of 400 samples of
/proc/<tid>/syscall read "running" -- pure userspace, ZERO syscalls -- and the
service logged nothing at all in 20 minutes.

The loop was `PeerLink._receive_loop`:

    def _ws_recv(...):
        try:
            return self._ws.recv(timeout=timeout)
        except Exception:
            return None            # <-- swallowed ConnectionClosed

    def _receive_loop(self):
        while self._state == LinkState.CONNECTED and self._ws is not None:
            try:
                raw = self._ws_recv(timeout=60)
                if raw is None:
                    continue       # <-- no sleep, no bound
                ...
            except Exception:
                self._handle_disconnect()   # <-- unreachable for this class
                break

Once the peer's socket closed, `recv` raised ConnectionClosed on every call. The
inner swallow turned it into None; the loop did `continue`; and the `while`
condition never went false because the ONLY thing that clears `_state` is
`_handle_disconnect()`, which lives in the handler the swallow was stealing
from. It made no syscalls because the websockets sync client answers a closed
connection from its own drained buffer without touching the socket -- so there
was no socket read to observe and no sleep to observe.

Nothing else would have stopped it: `PeerLinkManager.start()`, whose
`_prune_idle_links` would have reaped an idle link after 300s, has no caller
anywhere in the repo, so the maintenance loop has never run in any process.

Two independent guarantees are asserted below:
  1. `_ws_recv` propagates everything except a genuine timeout, so the existing
     outer handler retires the link (the fix);
  2. `_receive_loop` bounds consecutive empty returns regardless, so no future
     swallow anywhere can recreate a free-running core (the class guard).

Run:
  pytest tests/unit/test_peer_link_recv_loop.py -v
"""

import os
import sys
import threading
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from core.peer_link.link import (  # noqa: E402
    LinkState, PeerLink, _is_recv_timeout, _MAX_EMPTY_RECVS)


class FakeClosedWS:
    """A socket whose recv always raises the way a closed WebSocket does.

    Returns INSTANTLY and makes no syscall, which is the property that turned
    the old `continue` into a busy-loop.
    """

    def __init__(self, exc=None):
        self.calls = 0
        self._exc = exc or ConnectionResetError('sent 1000 (OK); then received 1000 (OK)')

    def recv(self, timeout=None):
        self.calls += 1
        raise self._exc


class FakeTimingOutWS:
    """A socket that times out the way an idle-but-healthy one does."""

    def __init__(self, limit=None):
        self.calls = 0
        self._limit = limit

    def recv(self, timeout=None):
        self.calls += 1
        if self._limit is not None and self.calls > self._limit:
            raise ConnectionResetError('closed after the burst')
        raise TimeoutError('no message')


def make_link(ws):
    link = PeerLink.__new__(PeerLink)          # no connect(), no network
    link.peer_id = 'abcdef1234567890'
    link._ws = ws
    link._state = LinkState.CONNECTED
    link._session_key = None
    link._messages_received = 0
    link._bytes_received = 0
    link._last_activity = time.time()
    link._message_handlers = {}
    link._pending_responses = {}
    link._response_data = {}
    from core.peer_link.link import TrustLevel
    link.trust = TrustLevel.UNTRUSTED if hasattr(TrustLevel, 'UNTRUSTED') else list(TrustLevel)[0]
    return link


# ── the timeout / real-error split (the fix) ────────────────────────────────

def test_a_timeout_is_a_timeout():
    """The normal idle case. Must keep the loop alive."""
    assert _is_recv_timeout(TimeoutError('no message'))


def test_socket_timeout_is_recognised():
    """socket.timeout is an alias of TimeoutError on 3.10+, so the builtin
    check covers it -- asserted rather than assumed."""
    import socket
    assert _is_recv_timeout(socket.timeout('timed out'))


def test_websocket_client_timeout_is_recognised_by_name():
    """websocket-client raises its own type. It is matched by NAME because the
    library may be absent on a node that only carries `websockets`, and
    importing it here to build an isinstance tuple would fail there."""
    exc = type('WebSocketTimeoutException', (Exception,), {})('timed out')
    assert _is_recv_timeout(exc)


@pytest.mark.parametrize('exc', [
    ConnectionResetError('sent 1000 (OK); then received 1000 (OK)'),
    ConnectionAbortedError('abnormal closure'),
    OSError('bad file descriptor'),
    ValueError('garbage frame'),
    RuntimeError('anything else at all'),
])
def test_everything_else_is_not_a_timeout(exc):
    """THE BUG. Every one of these used to become None and spin the loop."""
    assert not _is_recv_timeout(exc)


def test_ws_recv_propagates_a_close_instead_of_swallowing_it():
    link = make_link(FakeClosedWS())
    with pytest.raises(ConnectionResetError):
        link._ws_recv(timeout=0.01)


def test_ws_recv_still_returns_none_on_a_timeout():
    """The idle contract the caller relies on must not change."""
    link = make_link(FakeTimingOutWS())
    assert link._ws_recv(timeout=0.01) is None


def test_ws_recv_returns_none_without_a_socket():
    link = make_link(None)
    assert link._ws_recv(timeout=0.01) is None


# ── the loop terminates (the observable bug) ────────────────────────────────

def test_the_loop_retires_a_closed_link_instead_of_spinning():
    """The regression test for the burned core: run the real loop against a
    socket that raises on every recv, and require it to EXIT."""
    ws = FakeClosedWS()
    link = make_link(ws)

    done = threading.Event()

    def run():
        link._receive_loop()
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert done.wait(timeout=5), (
        'the receive loop did not exit on a closed socket -- this is the '
        'busy-loop that burned a CPU core for hours')
    assert link._state == LinkState.DISCONNECTED
    assert ws.calls < 50, (
        'the loop called recv %d times before giving up; a closed socket must '
        'be recognised on the first error, not retried' % ws.calls)


def test_the_loop_survives_ordinary_timeouts():
    """A healthy idle link times out repeatedly and must NOT be retired --
    otherwise the fix would disconnect every quiet peer."""
    ws = FakeTimingOutWS(limit=5)
    link = make_link(ws)
    t = threading.Thread(target=link._receive_loop, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()
    assert ws.calls == 6, (
        'expected 5 tolerated timeouts then the close, got %d' % ws.calls)
    assert link._state == LinkState.DISCONNECTED


def test_empty_returns_are_bounded_even_if_something_swallows_again():
    """THE CLASS GUARD. Simulate a future regression that reintroduces the
    swallow: _ws_recv patched to always return None. The loop must still stop
    rather than run free, and must say why."""
    link = make_link(FakeClosedWS())
    link._ws_recv = lambda timeout=None: None      # the old broken behaviour

    done = threading.Event()

    def run():
        link._receive_loop()
        done.set()

    threading.Thread(target=run, daemon=True).start()
    assert done.wait(timeout=10), (
        'an always-None recv must be bounded by the empty-return guard')
    assert link._state == LinkState.DISCONNECTED


def test_the_guard_is_far_above_normal_idle_behaviour():
    """With a 60s recv timeout a healthy link produces one empty return per
    minute, so the bound must never be reachable in ordinary operation."""
    assert _MAX_EMPTY_RECVS >= 50


def test_a_received_message_resets_the_empty_counter():
    """Traffic must clear accumulated timeouts, or a long-lived chatty link
    would eventually trip the guard."""
    class Intermittent:
        def __init__(self):
            self.calls = 0

        def recv(self, timeout=None):
            self.calls += 1
            # timeout, timeout, a real message, then close
            if self.calls in (1, 2):
                raise TimeoutError('idle')
            if self.calls == 3:
                return b'{"ch":"control","id":"x","d":{}}'
            raise ConnectionResetError('closed')

    ws = Intermittent()
    link = make_link(ws)
    t = threading.Thread(target=link._receive_loop, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()
    assert link._messages_received == 1
    assert link._state == LinkState.DISCONNECTED
