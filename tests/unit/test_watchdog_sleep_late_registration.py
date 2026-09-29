"""security.node_watchdog.sleep_with_heartbeat: a daemon that starts before
the watchdog exists must still be heartbeated once the watchdog appears.

agent_daemon / coding_daemon resolved get_watchdog() ONCE per sleep.  During
early boot it was still None, so the whole boot-grace sleep ran with no
heartbeats; once the watchdog registered the thread it looked frozen and was
force-restarted every ~5 minutes for the rest of the process's life.
"""
import threading
import time


import security.node_watchdog as nw


class _RecordingWatchdog:
    def __init__(self):
        self.beats = []

    def heartbeat(self, name):
        self.beats.append(name)


def test_watchdog_that_appears_mid_sleep_is_heartbeated(monkeypatch):
    wd = _RecordingWatchdog()
    state = {'wd': None}
    monkeypatch.setattr(nw, 'get_watchdog', lambda: state['wd'])

    def appear():
        time.sleep(0.05)
        state['wd'] = wd

    threading.Thread(target=appear, daemon=True).start()
    nw.sleep_with_heartbeat('agent_daemon', 0.3, chunk_seconds=0.02)

    assert wd.beats, 'no heartbeat after the watchdog appeared mid-sleep'
    assert set(wd.beats) == {'agent_daemon'}


def test_no_watchdog_is_just_a_sleep(monkeypatch):
    monkeypatch.setattr(nw, 'get_watchdog', lambda: None)
    start = time.monotonic()
    nw.sleep_with_heartbeat('coding_daemon', 0.1, chunk_seconds=0.02)
    assert time.monotonic() - start >= 0.09


def test_stop_check_ends_the_sleep_early(monkeypatch):
    monkeypatch.setattr(nw, 'get_watchdog', lambda: None)
    start = time.monotonic()
    nw.sleep_with_heartbeat('agent_daemon', 30, chunk_seconds=0.01,
                            stop_check=lambda: time.monotonic() - start > 0.05)
    assert time.monotonic() - start < 1.0


def test_wait_returning_true_ends_the_sleep_at_once(monkeypatch):
    monkeypatch.setattr(nw, 'get_watchdog', lambda: None)
    stop = threading.Event()
    stop.set()
    start = time.monotonic()
    nw.sleep_with_heartbeat('coding_daemon', 30, wait=stop.wait)
    assert time.monotonic() - start < 1.0


def test_agent_daemon_sleep_heartbeats_a_late_watchdog(monkeypatch):
    """The daemon's own _wd_sleep goes through the helper."""
    from integrations.agent_engine.agent_daemon import AgentDaemon

    wd = _RecordingWatchdog()
    monkeypatch.setattr(nw, 'get_watchdog', lambda: wd)
    daemon = AgentDaemon.__new__(AgentDaemon)
    daemon._running = True
    daemon._wd_sleep(0.01)
    assert wd.beats == ['agent_daemon']
