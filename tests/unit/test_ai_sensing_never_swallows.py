"""core.ai_sensing never reports "no camera running" because a name broke, and
never swallows a failure on the screen-privacy socket.

1. The seam to the vision module.  _stop_vision / _running_vision_services
   caught ImportError around `from integrations.vision.vision_service import
   <name>` and returned nothing.  That is how the eye button's camera cut
   stopped nothing for as long as the name it imported (get_vision_service)
   did not exist there -- and the proof said camera_service_running=False,
   a reassurance.  A rename of the new names would bring it back invisibly.
   Now: vision not installed -> silent, nothing running (true: no instance
   can exist); installed but the name missing or the import broken -> ERROR,
   and the proof says 'unknown', never False.

2. The authority socket (the cross-process screen gate the portal asks).
   Five `except Exception` blocks on it swallowed silently: a bind refused,
   the accept loop dying (every later portal query then fails closed with no
   trace of why), a per-connection error, the fail-closed reply not sent, a
   close failing; and query_authority_state turned any connect error into
   UNREACHABLE with no cause -- the shape of the 2026-08-25 incident, where
   a permission bug read as a human's cut.  Each now logs.  The authority
   is AF_UNIX-only, so these drive the real functions through a stand-in
   for the OS socket module (the boundary); nothing else is mocked.

    python -m pytest tests/unit/test_ai_sensing_never_swallows.py -q
"""
import logging
import sys
import threading
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

LOGGER = 'core.ai_sensing'


def _records(caplog, level):
    return [r for r in caplog.records
            if r.name == LOGGER and r.levelno >= level]


# ── 1. the vision seam ──────────────────────────────────────────────────────

def test_both_names_ai_sensing_calls_exist_on_the_vision_module():
    """A rename of either fails here, in CI, not silently in the field."""
    from integrations.vision import vision_service
    for name in ('running_vision_services', 'stop_running_vision_services'):
        assert callable(getattr(vision_service, name, None)), (
            f'integrations.vision.vision_service.{name} is gone: core.ai_sensing '
            'calls it for the eye button\'s camera cut and its proof')


def test_a_renamed_accessor_is_an_error_and_the_proof_says_unknown(
        monkeypatch, caplog):
    from core import ai_sensing
    from integrations.vision import vision_service
    monkeypatch.delattr(vision_service, 'running_vision_services')
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        proof = ai_sensing.status()['proof']
    assert proof['camera_service_running'] == 'unknown', (
        'a broken seam must never read as "no camera running"')
    assert _records(caplog, logging.ERROR), 'the broken seam was not logged'


def test_a_renamed_stop_is_an_error(monkeypatch, caplog):
    from core import ai_sensing
    from integrations.vision import vision_service
    monkeypatch.delattr(vision_service, 'stop_running_vision_services')
    try:
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            ai_sensing.set_sense('camera', True)
        assert _records(caplog, logging.ERROR), (
            'the camera cut reached no stop and said nothing')
    finally:
        ai_sensing.set_sense('camera', False)


def test_vision_not_installed_is_silent_and_nothing_runs(monkeypatch, caplog):
    from core import ai_sensing
    monkeypatch.setitem(sys.modules, 'integrations.vision.vision_service', None)
    try:
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert ai_sensing.status()['proof']['camera_service_running'] is False
            ai_sensing.set_sense('camera', True)
        assert _records(caplog, logging.WARNING) == []
    finally:
        ai_sensing.set_sense('camera', False)


def test_vision_installed_but_unimportable_is_an_error_and_unknown(
        monkeypatch, caplog):
    """A dependency of the module missing is not "vision not installed"."""
    from core import ai_sensing

    class _Broken:
        @staticmethod
        def import_module(name):
            raise ModuleNotFoundError("No module named 'cv2'", name='cv2')

    monkeypatch.setattr(ai_sensing, 'importlib', _Broken)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert ai_sensing.status()['proof']['camera_service_running'] == 'unknown'
    assert _records(caplog, logging.ERROR)


# ── 2. the authority socket ─────────────────────────────────────────────────

class _FakeSocketModule:
    """The OS socket module, standing in on a platform without AF_UNIX."""
    AF_UNIX = 1
    SOCK_STREAM = 1

    def __init__(self, make):
        self._make = make

    def socket(self, *a):
        return self._make()


def _server_with(monkeypatch, tmp_path, accept_script, bind_error=None):
    from core import ai_sensing
    conns = []

    class _Srv:
        def __init__(self):
            self._script = list(accept_script)

        def bind(self, path):
            if bind_error:
                raise bind_error
            open(path, 'w').close()

        def listen(self, n):
            pass

        def accept(self):
            step = self._script.pop(0) if self._script else OSError('closed')
            if isinstance(step, BaseException):
                raise step
            conns.append(step)
            return step, None

    monkeypatch.setattr(ai_sensing, 'socket', _FakeSocketModule(_Srv))
    started = ai_sensing.start_authority_server(str(tmp_path / 'auth.sock'))
    return started, conns


def _until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_a_refused_bind_is_logged(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        started, _ = _server_with(monkeypatch, tmp_path, [],
                                  bind_error=PermissionError(13, 'denied'))
    assert started is False
    assert _records(caplog, logging.WARNING), 'a refused bind left no trace'


def test_the_accept_loop_dying_is_logged(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        started, _ = _server_with(monkeypatch, tmp_path,
                                  [OSError('accept failed')])
        assert started is True
        assert _until(lambda: _records(caplog, logging.ERROR)), (
            'the authority stopped answering and nothing said so')


def test_a_failed_query_answers_no_and_is_logged(monkeypatch, tmp_path, caplog):
    class _Conn:
        def __init__(self, fail_send=False, fail_close=False):
            self.sent, self._fs, self._fc = [], fail_send, fail_close

        def recv(self, n):
            raise OSError('connection reset')

        def sendall(self, b):
            if self._fs:
                raise OSError('broken pipe')
            self.sent.append(b)

        def close(self):
            if self._fc:
                raise OSError('close failed')

    ok_conn = _Conn()
    dead_conn = _Conn(fail_send=True, fail_close=True)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        _server_with(monkeypatch, tmp_path, [ok_conn, dead_conn])
        assert _until(lambda: len([r for r in caplog.records
                                   if r.name == LOGGER]) >= 4)
    assert ok_conn.sent == [b'0'], 'a query that failed was not answered No'
    messages = ' | '.join(r.getMessage() for r in caplog.records
                          if r.name == LOGGER)
    assert 'connection reset' in messages
    assert 'broken pipe' in messages, 'the fail-closed reply not sent, silently'
    assert 'close failed' in messages


def test_an_unreachable_authority_says_why(monkeypatch, caplog):
    from core import ai_sensing

    class _Client:
        def settimeout(self, t):
            pass

        def connect(self, path):
            raise PermissionError(13, 'Permission denied')

    monkeypatch.setattr(ai_sensing, 'socket', _FakeSocketModule(_Client))
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert ai_sensing.query_authority_state('screen', '/x.sock') == \
            ai_sensing.SENSE_UNREACHABLE
    assert any('Permission denied' in r.getMessage()
               for r in _records(caplog, logging.WARNING)), (
        'a permission error read as a plain "unreachable"')
