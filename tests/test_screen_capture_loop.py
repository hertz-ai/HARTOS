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
import sys
from contextlib import contextmanager
from types import SimpleNamespace

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


# ── The capture thread acts for whoever owns the desktop NOW ──────────────
#
# Nunba now keeps HEVOLVE_OWNER_USER_ID in step with sign-in and sign-out.
# The thread used to read it once at start and close over it, so after a
# sign-in the consent ask kept going to the boot-time guest (no subscriber)
# and frames were filed under the guest.


def _started_capture_callbacks(monkeypatch, owner):
    """Start the REAL wiring with the loop and its boundaries (DB, consent
    service, frame store) faked; return the callbacks it built and what they
    asked and stored."""
    import integrations.vision.vision_service as vsmod
    got, asked, stored = {}, [], []

    def fake_loop(consent_ok, grab, put, yielding, sleep, stop):
        got.update(consent_ok=consent_ok, put=put)

    @contextmanager
    def fake_db_session(commit=False):
        yield object()

    class FakeConsent:
        @staticmethod
        def check_or_request(db, user_id, consent_type):
            asked.append((user_id, consent_type))
            return True

    monkeypatch.setattr(vsmod, 'run_screen_capture_loop', fake_loop)
    monkeypatch.setitem(sys.modules, 'integrations.social.models',
                        SimpleNamespace(db_session=fake_db_session))
    monkeypatch.setitem(sys.modules, 'integrations.social.consent_service',
                        SimpleNamespace(ConsentService=FakeConsent))
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', owner)
    vs = VisionService.__new__(VisionService)
    vs._running = True
    vs.store = SimpleNamespace(
        put_screen_frame=lambda uid, jpeg: stored.append(uid))
    vs._start_screen_capture(interval=0)
    vs._screen_capture_thread.join(timeout=5)
    return got, asked, stored


def test_consent_and_frames_follow_whoever_is_signed_in_now(monkeypatch):
    """RED before the fix: started as the guest, still asked the guest."""
    got, asked, stored = _started_capture_callbacks(monkeypatch, 'g_guest')
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'user-42')   # sign-in

    assert got['consent_ok']() is True
    got['put'](JPEG)

    assert asked == [('user-42', 'screen_capture')]
    assert stored == ['user-42']


def test_owner_gone_mid_run_asks_nobody_and_files_nothing(monkeypatch):
    got, asked, stored = _started_capture_callbacks(monkeypatch, 'g_guest')
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID')

    assert got['consent_ok']() is False
    got['put'](JPEG)

    assert asked == []
    assert stored == []
