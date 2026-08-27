"""The desktop's own screen finally has a producer (#701).

The screen channel's server side (WS ingest, _description_loop for both
channels, triggers, world-model record) was complete from day one, but
nothing ever produced screen frames: the SPA hook carries the
'screen_start' handshake yet only captures getUserMedia, and nothing
mounts channel='screen'.  run_screen_capture_loop captures where the
screen lives, gated on the canonical ConsentService flow (a denied tick
files the pending ask) and on the user-yield gate (#687).  Same
injected-callback style as test_goal_seed_loop / test_commit_ceiling_loop.
"""
import pytest

from integrations.vision.vision_service import (
    VisionService,
    run_screen_capture_loop,
)

JPEG = b'\xff\xd8fakejpeg'


def _stop_after(n_ticks):
    ticks = {'n': 0}

    def stop():
        ticks['n'] += 1
        return ticks['n'] > n_ticks

    return stop


def _drive(consent, yielding, frames, n_ticks):
    """Run the loop with canned callback behavior; return (grabs, puts)."""
    seq = iter(frames)
    grabs, puts = [], []

    def grab():
        f = next(seq)
        grabs.append(f)
        return f

    run_screen_capture_loop(
        lambda: consent, grab, puts.append, lambda: yielding,
        sleep=lambda: None, stop=_stop_after(n_ticks))
    return grabs, puts


def test_no_consent_never_captures():
    """Denied consent must stop the tick BEFORE any screen bytes exist."""
    grabs, puts = _drive(consent=False, yielding=False,
                         frames=[JPEG, JPEG], n_ticks=2)
    assert grabs == []
    assert puts == []


def test_granted_consent_captures_and_stores_each_tick():
    grabs, puts = _drive(consent=True, yielding=False,
                         frames=[JPEG, JPEG], n_ticks=2)
    assert puts == [JPEG, JPEG]


def test_yielding_user_pauses_capture():
    """User mid-chat: no fresh frames, so the describe backend never
    gets pulled onto the shared GPU by this channel (#687)."""
    grabs, puts = _drive(consent=True, yielding=True,
                         frames=[JPEG], n_ticks=3)
    assert grabs == []
    assert puts == []


def test_failed_grab_stores_nothing_and_retries():
    grabs, puts = _drive(consent=True, yielding=False,
                         frames=[None, JPEG], n_ticks=2)
    assert puts == [JPEG]


def test_raising_consent_check_survives_to_next_tick():
    calls = {'n': 0}

    def consent_ok():
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('db busy')
        return True

    puts = []
    run_screen_capture_loop(
        consent_ok, lambda: JPEG, puts.append, lambda: False,
        sleep=lambda: None, stop=_stop_after(2))
    assert puts == [JPEG]


def test_start_without_owner_identity_spawns_no_thread(monkeypatch):
    """No HEVOLVE_OWNER_USER_ID means nobody to ask or file frames
    under -- the wiring must not start a capture thread at all."""
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
    vs = VisionService.__new__(VisionService)
    vs._running = True

    vs._start_screen_capture()

    assert getattr(vs, '_screen_capture_thread', None) is None
