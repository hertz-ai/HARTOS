"""A sidecar's announced port must be listening before anyone is handed it.

RuntimeToolManager learns a sidecar's port from a ``PORT=NNNNN`` line on its
stdout.  A GPU sidecar has to print that line BEFORE its heavy imports --
MEASURED 2026-09-21, `import torch` alone overran both the 30 s this
originally waited and the 180 s it waits now, on a box running other agents'
builds -- so the line is a RESERVATION, not a service: nothing is listening
on the port for as long as the import takes.

Registering in that window is what produced the live log line
"Registered service tool: ltx2 [unhealthy (registered anyway)]", and the
first agent call would have taken a ConnectionRefused against a tool
_start_sidecar had just reported as running.  _wait_for_listen closes that
window.  These tests drive it against real sockets and a stubbed process
handle.
"""
import socket
import threading
import time

import pytest

from integrations.service_tools.runtime_manager import RuntimeToolManager


class _FakeProc:
    """Stands in for subprocess.Popen: only poll()/returncode are read."""

    def __init__(self, alive=True, returncode=None):
        self._alive = alive
        self.returncode = returncode

    def poll(self):
        return None if self._alive else self.returncode


@pytest.fixture
def rtm():
    return RuntimeToolManager()


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_returns_true_once_the_socket_accepts(rtm):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(('127.0.0.1', 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert rtm._wait_for_listen('probe', port, _FakeProc(), timeout=5) is True
    finally:
        srv.close()


def test_waits_for_a_late_bind(rtm):
    """The real case: the port is announced, then bound seconds later."""
    port = _free_port()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ready = threading.Event()

    def _bind_later():
        time.sleep(1.5)
        srv.bind(('127.0.0.1', port))
        srv.listen(1)
        ready.set()

    t = threading.Thread(target=_bind_later, daemon=True)
    t.start()
    try:
        started = time.time()
        assert rtm._wait_for_listen('probe', port, _FakeProc(), timeout=20) is True
        assert ready.is_set(), "returned True before the bind actually happened"
        assert time.time() - started >= 1.0, "did not actually wait"
    finally:
        t.join(timeout=10)
        srv.close()


def test_gives_up_when_nothing_ever_listens(rtm):
    """Never fatal: it reports False and lets the caller carry on."""
    port = _free_port()
    started = time.time()
    assert rtm._wait_for_listen('probe', port, _FakeProc(), timeout=2) is False
    assert time.time() - started >= 2


def test_fails_fast_when_the_process_died(rtm):
    """A sidecar that crashed while loading must not cost the full timeout."""
    port = _free_port()
    started = time.time()
    assert rtm._wait_for_listen(
        'probe', port, _FakeProc(alive=False, returncode=1), timeout=30) is False
    assert time.time() - started < 5, "should detect the dead process at once"
