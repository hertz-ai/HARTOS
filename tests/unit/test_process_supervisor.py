"""#110: ProcessSupervisor base — the shared spawn/stream/backoff loop the 3
supervisors used to paste. Behavioural: mock subprocess.Popen with a fake child,
drive the real _run loop, and assert it spawns, streams stdout through the hook,
restarts, disables on a circuit-breaker hook, stops, and treats a fatal spawn
error as disabling. No grep tests.
"""
import os
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.process_supervisor as ps_mod  # noqa: E402
from core.process_supervisor import ProcessSupervisor  # noqa: E402


class FakeProc:
    def __init__(self, lines=(b'hello\n',), rc=0, running=False):
        self.stdout = iter(lines)
        self._rc = rc
        self._running = running
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        self._running = False
        return self._rc

    def poll(self):
        return None if self._running else self._rc

    def terminate(self):
        self.terminated = True
        self._running = False

    def kill(self):
        self.killed = True


def _patch_popen(monkeypatch, factory):
    calls = []

    def fake_popen(cmd, **kw):
        calls.append((cmd, kw))
        return factory()
    monkeypatch.setattr(ps_mod.subprocess, 'Popen', fake_popen)
    return calls


def test_spawns_streams_and_disables_on_hook(monkeypatch):
    seen, started = [], []

    class S(ProcessSupervisor):
        name = 'test'

        def _build_popen(self):
            return (['x'], {})

        def _on_started(self, proc):
            started.append(proc)

        def _format_stdout_line(self, line):
            seen.append(line)

        def _on_child_exit(self, rc, uptime):
            return True   # circuit breaker -> disable after one iteration

    calls = _patch_popen(monkeypatch, lambda: FakeProc((b'one\n', b'two\n')))
    s = S()
    t = threading.Thread(target=s._run)
    t.start()
    t.join(timeout=3)
    assert not t.is_alive()            # loop returned (disabled), no infinite spin
    assert len(calls) == 1             # spawned exactly once
    assert started and seen == ['one', 'two']   # _on_started + stdout streamed


def test_restarts_then_stops(monkeypatch):
    calls = _patch_popen(monkeypatch, lambda: FakeProc((b'x\n',)))

    class S(ProcessSupervisor):
        name = 'test'

        def _build_popen(self):
            return (['x'], {})

    s = S()
    t = threading.Thread(target=s._run)
    t.start()
    time.sleep(0.3)                    # ~1 instant iteration, then into backoff wait
    s.stop_event.set()                # interrupt the backoff wait
    t.join(timeout=3)
    assert not t.is_alive()
    assert len(calls) >= 1 and s.restart_count >= 1


def test_fatal_spawn_error_disables(monkeypatch):
    class S(ProcessSupervisor):
        name = 'test'
        fatal_spawn_errors = (FileNotFoundError,)

        def _build_popen(self):
            raise FileNotFoundError('node missing')

    s = S()
    t = threading.Thread(target=s._run)
    t.start()
    t.join(timeout=3)
    assert not t.is_alive()                       # fatal -> no retry loop
    assert 'fatal spawn error' in (s.last_error or '')


def test_stop_terminates_running_child():
    proc = FakeProc(running=True)

    class S(ProcessSupervisor):
        name = 'test'

        def _build_popen(self):
            return (['x'], {})

    s = S()
    s.proc = proc
    s.stop()
    assert s.stop_event.is_set()
    assert proc.terminated


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-q']))
