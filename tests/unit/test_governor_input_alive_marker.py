"""The governor's Linux idle backend reads the compositor's input-alive marker.

Measured 2026-09-22 on the Samsung box (docs/architecture/NATIVE_OS_PROGRAM.md
section 1): press p50 122 ms against a 25 ms budget with the daemons running,
12 ms with them paused.  The driver was the agent daemon's starvation override
forcing CPU inference while a person was at the desk, and the reason it thought
nobody was there is that the governor's only Linux idle source was xprintidle,
an X11 tool, so on Wayland _get_idle_ms_linux returned None and the governor
fell back to a timestamp only a foreground chat request ever touches.

The compositor already writes /run/hart/session/input-alive on input
(comp_core.rs note_input_alive; the session supervisor reads the same path).
These tests drive the REAL _get_idle_ms_linux and _detect_user_idle against a
temp marker named by HART_INPUT_ALIVE_MARKER, with the xprintidle attempt
pinned to the Wayland outcome, so they run the same on Windows, macOS and
Linux.  The Windows and macOS branches are pinned untouched.  No grep tests.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.resource_governor import ResourceGovernor  # noqa: E402

IDLE_S = 120.0


@pytest.fixture
def no_xprintidle(monkeypatch):
    """Pin the xprintidle attempt to what Wayland gives: no such binary."""
    def _absent(*_a, **_k):
        raise FileNotFoundError('xprintidle')
    monkeypatch.setattr(subprocess, 'run', _absent)


@pytest.fixture
def on_linux(monkeypatch):
    """_detect_user_idle dispatches on sys.platform; these tests describe the
    Linux branch and must not fall into GetLastInputInfo on a Windows host."""
    monkeypatch.setattr(sys, 'platform', 'linux')


@pytest.fixture
def marker(tmp_path, monkeypatch):
    path = tmp_path / 'input-alive'
    path.write_bytes(b'1\n')
    monkeypatch.setenv('HART_INPUT_ALIVE_MARKER', str(path))
    return path


def _gov():
    return ResourceGovernor(idle_threshold_seconds=IDLE_S)


def test_fresh_marker_reads_as_active(no_xprintidle, marker, on_linux):
    gov = _gov()
    idle_ms = gov._get_idle_ms_linux()
    assert idle_ms is not None and 0.0 <= idle_ms < IDLE_S * 1000
    assert gov._detect_user_idle() is False, (
        "a marker the compositor just touched means someone is at the desk")


def test_old_marker_reads_as_idle(no_xprintidle, marker, on_linux):
    stale = time.time() - (IDLE_S + 30)
    os.utime(marker, (stale, stale))
    gov = _gov()
    idle_ms = gov._get_idle_ms_linux()
    assert idle_ms is not None and idle_ms >= IDLE_S * 1000
    assert gov._detect_user_idle() is True, (
        "a marker older than the idle threshold means the desk is empty; "
        "today the compositor writes it once per boot, so an old marker is "
        "the expected idle reading until S2 lands the heartbeat")


def test_marker_from_the_future_clamps_to_active(no_xprintidle, marker):
    ahead = time.time() + 3600
    os.utime(marker, (ahead, ahead))
    assert _gov()._get_idle_ms_linux() == 0.0


def test_missing_marker_falls_back_to_the_activity_timestamp(
        no_xprintidle, on_linux, tmp_path, monkeypatch):
    monkeypatch.setenv('HART_INPUT_ALIVE_MARKER', str(tmp_path / 'absent'))
    gov = _gov()
    assert gov._get_idle_ms_linux() is None, (
        "no marker is not idle and not active: it is 'no OS answer', so the "
        "governor keeps using report_user_activity() as it always did")
    # The fallback is the timestamp, in both directions.
    gov._last_user_activity = time.monotonic()
    assert gov._detect_user_idle() is False
    gov._last_user_activity = time.monotonic() - (IDLE_S + 1)
    assert gov._detect_user_idle() is True


def test_xprintidle_still_wins_on_x11(marker, monkeypatch):
    """The X11 answer keeps precedence: the marker is the Wayland fallback,
    not a replacement for a desktop that can answer directly."""
    class _Done:
        returncode = 0
        stdout = '4242\n'
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: _Done())
    assert _gov()._get_idle_ms_linux() == 4242.0


def test_default_marker_path_is_the_compositor_contract(no_xprintidle, monkeypatch):
    """With the env override unset the backend looks where comp_core.rs
    note_input_alive writes and the session supervisor reads; on a box with
    no such file (this test host) that is None, never an exception."""
    monkeypatch.delenv('HART_INPUT_ALIVE_MARKER', raising=False)
    # os.stat is process-wide: a background thread started by an earlier test
    # module (measured: a prompts/iq_*.json writer after hart_intelligence_entry
    # is imported) can stat its own file in between.  Record only this
    # thread's calls.
    import threading
    me = threading.get_ident()
    seen = []
    real_stat = os.stat

    def _stat(path, *a, **k):
        if threading.get_ident() == me:
            seen.append(path)
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, 'stat', _stat)
    result = _gov()._get_idle_ms_linux()
    assert seen and seen[-1] == '/run/hart/session/input-alive'
    assert result is None or result >= 0.0


def test_marker_follows_the_one_session_marker_dir(no_xprintidle, tmp_path,
                                                    monkeypatch):
    """With no full-path override, the marker sits in the dir
    core.foreground.session_marker_dir resolves: the same dir the
    foreground-active and user-chat markers live in, so HART_SESSION_MARKER_DIR
    relocates all three readers at once (a supervisor that moves the run dir,
    a test).  The full-path override still wins over it."""
    monkeypatch.delenv('HART_INPUT_ALIVE_MARKER', raising=False)
    monkeypatch.setenv('HART_SESSION_MARKER_DIR', str(tmp_path))
    gov = _gov()
    assert gov._get_idle_ms_linux() is None, "no marker in that dir yet"
    (tmp_path / 'input-alive').write_bytes(b'1\n')
    idle_ms = gov._get_idle_ms_linux()
    assert idle_ms is not None and idle_ms < IDLE_S * 1000
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.write_bytes(b'1\n')
    stale = time.time() - (IDLE_S + 30)
    os.utime(elsewhere, (stale, stale))
    monkeypatch.setenv('HART_INPUT_ALIVE_MARKER', str(elsewhere))
    assert gov._get_idle_ms_linux() >= IDLE_S * 1000, (
        "the full-path override keeps precedence over the shared dir")


@pytest.mark.parametrize('platform,branch', [
    ('win32', '_get_idle_ms_windows'),
    ('darwin', '_get_idle_ms_macos'),
])
def test_windows_and_macos_branches_do_not_read_the_marker(
        platform, branch, marker, monkeypatch):
    """The marker is Linux only.  A fresh marker in the env must not leak
    into the Windows GetLastInputInfo or macOS ioreg answer."""
    gov = _gov()
    monkeypatch.setattr(sys, 'platform', platform)
    monkeypatch.setattr(gov, branch, lambda: 777.0)
    with patch.object(gov, '_get_idle_ms_linux',
                      side_effect=AssertionError('linux backend called')):
        assert gov._get_os_idle_ms() == 777.0


def test_linux_dispatch_reaches_the_marker(no_xprintidle, marker, on_linux):
    gov = _gov()
    idle_ms = gov._get_os_idle_ms()
    assert idle_ms is not None and idle_ms < IDLE_S * 1000
