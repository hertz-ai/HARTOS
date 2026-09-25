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


# ── The override block itself (agent_daemon._tick) ─────────────────────────
#
# The tests above pin the SIGNAL and the GATE.  These drive the real _tick and
# pin what the STARVATION OVERRIDE does with them: it fires only after the
# starvation window, never during a foreground request, and, since 2026-09-22,
# never while the ResourceGovernor says the machine is not idle.
#
# Measured that day on the Samsung box (NATIVE_OS_PROGRAM.md section 1): the
# override force-ticked every 120 s on 'model_pressure' with the owner at the
# desk, llama-server at 207 percent CPU once a minute, press p50 122 ms against
# a 25 ms budget; with the daemons paused, 12 ms.  A person clicking around the
# desktop is neither a foreground request nor "recently active" (that means
# chatted), so the governor, whose Linux backend reads the compositor's
# input-alive marker, is the reader the override has to honour.  It does so
# through _idle_only_blocked, the one reader every idle_only goal already uses.

import logging
import time
from unittest.mock import MagicMock


@pytest.fixture
def governor_mode():
    """Pin the governor's mode for one test, restoring it afterwards (the
    same idiom tests/unit/test_paper_explanation_goal.py uses)."""
    from core.resource_governor import get_governor
    gov = get_governor()
    prev = gov._mode

    def _set(mode):
        gov._mode = mode

    yield _set
    gov._mode = prev


@pytest.fixture
def starved_daemon(monkeypatch):
    """A daemon whose yield gate has said 'model_pressure' for longer than the
    starvation window.  Everything past the override is stubbed so _tick
    either returns at the gate, or proves it went through by opening the DB
    (the first thing the tick does after the gate)."""
    from integrations.agent_engine import dispatch, agent_daemon
    import integrations.social.models as models
    import security.hive_guardrails as hg
    import integrations.service_tools.model_lifecycle as ml

    monkeypatch.setattr(dispatch, 'should_yield_to_user', lambda: True)
    monkeypatch.setattr(dispatch, 'get_last_yield_reason',
                        lambda: 'model_pressure')
    monkeypatch.setattr(hg.HiveCircuitBreaker, 'is_halted',
                        classmethod(lambda cls: False))
    monkeypatch.setattr(
        ml, 'get_model_lifecycle_manager',
        lambda: MagicMock(get_system_pressure=lambda: {'throttle_factor': 0.05}))

    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    opened = {'n': 0}

    def _get_db():
        opened['n'] += 1
        return db

    monkeypatch.setattr(models, 'get_db', _get_db)

    d = agent_daemon.AgentDaemon()
    d._last_tick_completed_at = time.monotonic() - (d._starvation_s + 60)
    return d, opened


def test_override_fires_when_the_governor_says_idle(starved_daemon, governor_mode):
    from core.resource_governor import MODE_IDLE
    daemon, opened = starved_daemon
    governor_mode(MODE_IDLE)
    daemon._tick()
    assert opened['n'] == 1, (
        "starved, nobody at the desk, no foreground request: the override "
        "must force the tick through to the goal query")


@pytest.mark.parametrize('mode', ['active', 'sleep'])
def test_override_yields_while_the_governor_says_not_idle(
        starved_daemon, governor_mode, caplog, mode):
    daemon, opened = starved_daemon
    governor_mode(mode)
    caplog.set_level(logging.DEBUG, logger='hevolve_social')
    daemon._tick()
    assert opened['n'] == 0, (
        f"governor mode {mode!r}: the override must not force inference "
        f"onto a machine the one idle detector says is in use")
    assert any('starvation override suppressed' in r.getMessage()
               and 'governor' in r.getMessage() for r in caplog.records), (
        "the yield must be logged like its foreground sibling, naming the "
        "governor so the journal says why the queue did not drain")


def test_override_fails_closed_when_the_governor_is_unreadable(starved_daemon):
    daemon, opened = starved_daemon
    with patch('core.resource_governor.get_governor',
               side_effect=RuntimeError('governor down')):
        daemon._tick()
    assert opened['n'] == 0, (
        "same contract as _idle_only_blocked: idle means PROVEN idle; an "
        "unreadable governor is not a licence to run inference at the desk")


def test_override_stays_suppressed_by_a_foreground_request_even_when_idle(
        starved_daemon, governor_mode):
    """B1 is kept: the governor check is added after it, not instead of it."""
    from core.resource_governor import MODE_IDLE
    from core.foreground import foreground_request
    daemon, opened = starved_daemon
    governor_mode(MODE_IDLE)
    with foreground_request():
        daemon._tick()
    assert opened['n'] == 0


def test_no_override_before_the_starvation_window(starved_daemon, governor_mode):
    from core.resource_governor import MODE_IDLE
    daemon, opened = starved_daemon
    governor_mode(MODE_IDLE)
    daemon._last_tick_completed_at = time.monotonic()
    daemon._tick()
    assert opened['n'] == 0, (
        "a gate that has only just started blocking is honoured as is; the "
        "override is for STARVATION, and idle alone does not shorten the window")


# ── The mode the override reads is LIVE in the daemon process ──────────────
#
# hart-agent-daemon.service is its own process and only the backend ever
# started a governor, so the mode above was the constructor's MODE_ACTIVE for
# the life of the daemon (found 2026-09-23).  run_forever, the unit's one entry
# point, now starts the governor monitor only (no enforcer, no proactive
# stream; see ResourceGovernor.start), so the override reads real idle.

def _wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@pytest.fixture
def live_governor(monkeypatch):
    """A fresh governor installed as the process singleton, driven by one
    controllable OS idle probe, every other per-tick read pinned calm
    (the same harness as tests/unit/test_governor_monitor_only.py)."""
    import core.resource_governor as rg
    monkeypatch.setattr(rg, '_MONITOR_INTERVAL_SECONDS', 0.05)
    monkeypatch.setattr(rg, 'get_enforcer', lambda: MagicMock())
    import security.node_watchdog as _nw
    monkeypatch.setattr(_nw, 'get_watchdog', lambda: None)
    gov = rg.ResourceGovernor(idle_threshold_seconds=120)
    probe = {'idle_ms': 0.0}
    monkeypatch.setattr(gov, '_get_os_idle_ms', lambda: probe['idle_ms'])
    monkeypatch.setattr(gov, '_refresh_cpu_attribution', lambda: None)
    monkeypatch.setattr(gov, '_get_memory_pressure', lambda: 0.1)
    monkeypatch.setattr(gov, '_get_battery_status', lambda: (1.0, False))
    monkeypatch.setattr(gov, '_check_gpu_available', lambda: False)
    monkeypatch.setattr(rg, '_governor', gov)
    yield gov, probe
    gov.stop()


def test_run_forever_starts_the_governor_monitor_once(live_governor, monkeypatch):
    from integrations.agent_engine.agent_daemon import AgentDaemon
    gov, probe = live_governor
    d = AgentDaemon()
    # The worker is not under test: leave _thread None so run_forever returns
    # instead of joining forever.
    monkeypatch.setattr(d, 'start', lambda: None)
    d.run_forever()
    assert gov._running and gov._monitor_thread is not None
    assert gov._monitor_thread.is_alive()
    assert gov._proactive_thread is None, (
        "the daemon must start the governor MONITOR ONLY; the proactive "
        "stream and the enforcer stay in the backend")
    first = gov._monitor_thread
    d.run_forever()                                # a supervisor re-entry
    assert gov._monitor_thread is first, "one monitor per process, ever"


def test_run_forever_survives_a_governor_that_will_not_start(monkeypatch):
    """Best-effort: the goal engine must start even if the governor faults;
    the idle reads then fail closed to 'not idle' as before."""
    from integrations.agent_engine.agent_daemon import AgentDaemon
    d = AgentDaemon()
    monkeypatch.setattr(d, 'start', lambda: None)
    with patch('core.resource_governor.get_governor',
               side_effect=RuntimeError('governor down')):
        d.run_forever()                            # must not raise


def test_override_follows_the_live_mode_in_the_daemon_process(
        starved_daemon, live_governor):
    """End to end in one process: the probe says idle, the override fires;
    the probe says a click just happened, the override yields."""
    from core.resource_governor import MODE_ACTIVE, MODE_IDLE
    daemon, opened = starved_daemon
    gov, probe = live_governor
    probe['idle_ms'] = 10 * 60 * 1000
    gov.start(monitor_only=True)
    assert _wait_for(lambda: gov.get_mode() == MODE_IDLE)
    daemon._tick()
    assert opened['n'] == 1, "idle per the live governor: the forced tick runs"

    probe['idle_ms'] = 0.0
    assert _wait_for(lambda: gov.get_mode() == MODE_ACTIVE)
    daemon._last_tick_completed_at = time.monotonic() - (daemon._starvation_s + 60)
    daemon._tick()
    assert opened['n'] == 1, (
        "a click reached the compositor: the override must yield, whatever "
        "the yield gate's own reason is")


# ── The signal crosses the process boundary ────────────────────────────────
#
# hart-agent-daemon.service is its own process (S1 finding, 2026-09-24), so
# in-process _count is the DAEMON'S count there and could never fire.  The
# 0->1 edge now also holds a foreground-active.<pid> marker in the session
# marker dir (the same idiom as the governor's input-alive reader) and the
# 1->0 edge removes it; foreground_active() falls back to the marker only when
# the in-process count is zero.  The tests above are unchanged: with no marker
# dir configured (a dev checkout) the in-process behaviour is the whole story.

import subprocess
import threading


@pytest.fixture
def marker_dir(tmp_path, monkeypatch):
    """A private marker dir for one test, on any platform: HART_SESSION_MARKER_DIR
    is the env override the resolver honours first (a temp dir on Windows is
    the bundled-desktop shape, a file in the data dir)."""
    d = tmp_path / 'session'
    d.mkdir()
    monkeypatch.setenv('HART_SESSION_MARKER_DIR', str(d))
    return d


def _markers(d, name):
    return sorted(p.name for p in d.iterdir() if p.name.startswith(name + '.'))


def _dead_pid():
    """The pid of a process that has already exited."""
    p = subprocess.Popen([sys.executable, '-c', 'pass'])
    p.wait(timeout=60)
    return p.pid


def _child(script, marker_dir):
    """A second process running the SAME module files this process loaded.

    The prelude loads core.foreground and dispatch from this process's own
    ``__file__`` paths rather than importing them by name, because on the
    deepbox Linux harness the tests run from a scratch dir with the changed
    modules overlaid into sys.modules by path: a plain import in the child
    would find the deployed tree's older copies (or nothing at all) and the
    test would describe the harness, not the code."""
    import core
    import core.foreground as fg
    import integrations.agent_engine.dispatch as dispatch
    app_root = os.path.dirname(os.path.dirname(os.path.abspath(core.__file__)))
    prelude = (
        'import importlib.util, os, sys\n'
        f'sys.path.insert(0, {app_root!r})\n'
        'def _load(name, path):\n'
        '    spec = importlib.util.spec_from_file_location(name, path)\n'
        '    mod = importlib.util.module_from_spec(spec)\n'
        '    sys.modules[name] = mod\n'
        '    spec.loader.exec_module(mod)\n'
        'import core, integrations.agent_engine\n'
        f'_load("core.foreground", {os.path.abspath(fg.__file__)!r})\n'
        f'_load("integrations.agent_engine.dispatch", '
        f'{os.path.abspath(dispatch.__file__)!r})\n'
    )
    env = dict(os.environ, HART_SESSION_MARKER_DIR=str(marker_dir))
    return subprocess.Popen([sys.executable, '-c', prelude + script], env=env,
                            cwd=app_root, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)


_CHILD_HOLDS_FOREGROUND = '''
from core.foreground import enter_foreground, exit_foreground
enter_foreground()
print("held", flush=True)
sys.stdin.readline()
exit_foreground()
print("released", flush=True)
'''

_CHILD_SERVES_A_CHAT = '''
from integrations.agent_engine.dispatch import mark_user_chat_activity
mark_user_chat_activity()
print("chatted", flush=True)
'''


def test_marker_held_on_the_edges_only(marker_dir):
    """Created on 0->1, kept across nesting, removed on the last exit."""
    from core import foreground
    mine = f'foreground-active.{os.getpid()}'
    assert _markers(marker_dir, 'foreground-active') == []
    foreground.enter_foreground()
    assert _markers(marker_dir, 'foreground-active') == [mine]
    foreground.enter_foreground()
    foreground.exit_foreground()
    assert _markers(marker_dir, 'foreground-active') == [mine], (
        "one still in flight: held")
    foreground.exit_foreground()
    assert _markers(marker_dir, 'foreground-active') == [], "last exit removes"


def test_own_marker_is_not_evidence_and_count_zero_reads_inactive(marker_dir):
    """The reader ignores its own pid's marker: its own count is the authority
    for its own turns, and a same-process leftover must not read as a
    foreign turn."""
    from core import foreground
    (marker_dir / f'foreground-active.{os.getpid()}').write_bytes(b'')
    assert foreground.foreground_active() is False


def test_another_process_turn_is_visible_and_gates_the_daemon(marker_dir):
    """The daemon's shape: THIS process has count 0 (it is the daemon), a
    second process is mid-turn.  foreground_active() is True, the yield gate
    says foreground_request (the reason the override honours), and when the
    other process exits its turn the daemon is released."""
    from core import foreground
    from integrations.agent_engine import dispatch
    child = _child(_CHILD_HOLDS_FOREGROUND, marker_dir)
    try:
        assert child.stdout.readline().strip() == 'held'
        assert foreground.in_flight() == 0
        assert foreground.foreground_active() is True
        assert dispatch.should_yield_to_user() is True
        assert dispatch.get_last_yield_reason() == 'foreground_request'
        child.stdin.write('\n')
        child.stdin.flush()
        assert child.stdout.readline().strip() == 'released'
        assert foreground.foreground_active() is False
    finally:
        child.stdin.close()
        child.wait(timeout=60)


def test_another_process_chat_is_visible_to_the_daemon(marker_dir, monkeypatch):
    """The user-chat marker, same shape: THIS process never stamped
    _last_user_chat_at (it is the daemon); another process served a genuine
    chat and has since exited (a backend restart).  The gate engages here
    with the same reason it would have had in the backend.  Unlike a
    foreground marker the writer need not be alive: the chat is a fact
    about the person."""
    from integrations.agent_engine import dispatch
    monkeypatch.setattr(dispatch, '_last_user_chat_at', 0.0)
    monkeypatch.setattr(dispatch, '_active_create_sessions', 0)
    child = _child(_CHILD_SERVES_A_CHAT, marker_dir)
    try:
        assert child.stdout.readline().strip() == 'chatted'
    finally:
        child.stdin.close()
        child.wait(timeout=60)
    assert dispatch._last_user_chat_at == 0.0
    assert dispatch.is_user_recently_active() is True
    assert dispatch.should_yield_to_user() is True
    assert dispatch.get_last_yield_reason() == 'user_active'


def test_dead_writer_leftover_releases_the_daemon_at_once(marker_dir):
    """A process that crashed mid-turn leaves its marker behind.  Its pid is
    gone, so the marker is a leftover, not a turn, and it must not pin the
    daemon for the whole age bound."""
    from core import foreground
    (marker_dir / f'foreground-active.{_dead_pid()}').write_bytes(b'')
    assert foreground.foreground_active() is False


def test_stale_marker_of_a_live_writer_reads_inactive(marker_dir):
    """The age bound: a live pid whose marker is older than
    FOREGROUND_MARKER_MAX_AGE_S (a reused pid after a crash, or a hung
    turn) reads as no turn.  A fresh one from the same live pid reads as a
    turn, so the bound and not the liveness probe is what decided."""
    from core import foreground
    from core.foreground import FOREGROUND_MARKER_MAX_AGE_S
    parent = os.getppid()  # alive for the whole test, not our own pid
    p = marker_dir / f'foreground-active.{parent}'
    p.write_bytes(b'')
    stale = time.time() - (FOREGROUND_MARKER_MAX_AGE_S + 30)
    os.utime(p, (stale, stale))
    assert foreground.foreground_active() is False
    os.utime(p, None)
    assert foreground.foreground_active() is True


def test_age_bound_matches_the_chat_cooldown():
    """Past the bound the coarser gate has let go of the same turn too; the
    two windows moving apart would protect a long turn by one signal and
    not the other."""
    from core.foreground import FOREGROUND_MARKER_MAX_AGE_S
    from integrations.agent_engine.dispatch import _USER_CHAT_COOLDOWN
    assert FOREGROUND_MARKER_MAX_AGE_S == _USER_CHAT_COOLDOWN == 600


def test_racing_edges_leave_the_marker_matching_the_count(marker_dir):
    """Many threads entering and exiting at once: whatever the interleaving
    of the file writes, the marker ends up matching the final count."""
    from core import foreground

    def turn():
        for _ in range(20):
            with foreground.foreground_request():
                pass

    ts = [threading.Thread(target=turn) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert foreground.in_flight() == 0
    assert _markers(marker_dir, 'foreground-active') == []


def test_unwritable_marker_dir_never_breaks_the_gate(tmp_path, monkeypatch, caplog):
    """Best-effort, but LOUD once: a unit whose ReadWritePaths does not
    include the marker dir must show up in the journal, not vanish."""
    from core import foreground
    monkeypatch.setenv('HART_SESSION_MARKER_DIR', str(tmp_path / 'a-file'))
    (tmp_path / 'a-file').write_bytes(b'not a dir')
    foreground._marker_warned.clear()
    caplog.set_level(logging.WARNING, logger='core.foreground')
    with foreground.foreground_request():
        assert foreground.foreground_active() is True
    with foreground.foreground_request():
        pass
    warned = [r for r in caplog.records if 'not writable' in r.getMessage()]
    assert len(warned) == 1, "logged once per process per marker, not per turn"
    assert foreground.foreground_active() is False


# ── The resolver: one dir, the governor's reader idiom ─────────────────────

def test_resolver_env_override_wins(monkeypatch, tmp_path):
    from core.foreground import session_marker_dir
    monkeypatch.setenv('HART_SESSION_MARKER_DIR', str(tmp_path))
    assert session_marker_dir() == str(tmp_path)


def test_resolver_prefers_the_session_run_dir_on_hart_os(monkeypatch):
    """/run/hart/session: the 0770 hart:hart tmpfs dir the compositor's
    input-alive marker lives in, which is where the governor reads."""
    import core.foreground as fg
    monkeypatch.delenv('HART_SESSION_MARKER_DIR', raising=False)
    monkeypatch.setattr(fg.os.path, 'isdir',
                        lambda p: p == '/run/hart/session')
    assert fg.session_marker_dir() == '/run/hart/session'


def test_resolver_uses_the_data_dir_on_the_bundled_desktop(monkeypatch, tmp_path):
    """Nunba on Windows: a file in the data dir.  HARTOS_DATA_DIR and
    NUNBA_DATA_DIR are resolved by core.platform_paths.get_data_dir, which is
    reused, not restated; and the writer creates the session subdir."""
    import core.foreground as fg
    import core.platform_paths as pp
    monkeypatch.delenv('HART_SESSION_MARKER_DIR', raising=False)
    monkeypatch.setattr(fg.os.path, 'isdir', lambda p: False)
    monkeypatch.setenv('NUNBA_BUNDLED', '1')
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(pp, '_cached_data_dir', None)
    assert fg.session_marker_dir() == os.path.join(str(tmp_path), 'session')
    monkeypatch.setattr(fg.os.path, 'isdir', os.path.isdir)
    with fg.foreground_request():
        assert (tmp_path / 'session' / f'foreground-active.{os.getpid()}').exists()
    assert not (tmp_path / 'session' / f'foreground-active.{os.getpid()}').exists()


def test_resolver_is_none_on_a_plain_checkout(monkeypatch):
    """A data dir env ALONE is not a deployment: the deepbox test container
    sets NUNBA_DATA_DIR and runs several agents' tests concurrently, and a
    shared marker dir there would cross-pollute them.  None means the
    readers keep their in-process answer, which every test above relies on."""
    import core.foreground as fg
    monkeypatch.delenv('HART_SESSION_MARKER_DIR', raising=False)
    monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    monkeypatch.setenv('NUNBA_DATA_DIR', '/app/data')
    monkeypatch.setattr(fg.os.path, 'isdir', lambda p: False)
    assert fg.session_marker_dir() is None
    assert fg.touch_marker('foreground-active') is False
    assert fg.marker_age_s('foreground-active', 600) is None
