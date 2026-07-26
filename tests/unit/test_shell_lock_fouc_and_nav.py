"""Served-shell guards for the boot-lock FOUC fix (#166) and hartNav (#169).

Both are render+fetch behavioural tests, not source-shape checks: they start the
REAL Flask app from ``_create_flask_app()`` and exercise what the browser would
actually receive at first paint.

#166 — Boot FOUC + security: on a boot where a shell lock PASSWORD is set, the
opaque #lock-screen overlay must be ACTIVE (covering the desktop) in the very
HTML the server sends, so the desktop can never paint for a frame before the
deferred lock JS runs. render_desktop_shell() reads the SAME server-backed
session blob (shell_session.json -> lock_pw_hash) the JS lock owns, and seeds
`.active` only when a password exists. With no password (fresh install) it must
NOT seed `.active` (normal boot).

#169 — hartNav: the unified navigation module must be BOTH referenced by the
served shell (a <script src="/shell/static/hartNav.js">) AND actually served by
the app (200, non-empty) — the exact dead-husk failure mode a missing include or
static route would cause.
"""
import json
import os

import pytest

from integrations.agent_engine.liquid_ui_service import LiquidUIService


def _svc_with_data_dir(tmp_path):
    svc = LiquidUIService()
    # Point the service at an isolated data dir so we control shell_session.json;
    # render_desktop_shell + the /api/shell/session-state route both read _data_dir.
    svc._data_dir = str(tmp_path)
    return svc


def _write_session(tmp_path, blob):
    with open(os.path.join(str(tmp_path), 'shell_session.json'), 'w') as f:
        json.dump(blob, f)


# ── #166: boot-lock FOUC ────────────────────────────────────────────────────

def test_boot_locked_seeds_active_lock_overlay(tmp_path):
    """A lock password on disk => the served HTML boots with #lock-screen ACTIVE
    (covering) from the first paint — no desktop flash."""
    svc = _svc_with_data_dir(tmp_path)
    _write_session(tmp_path, {'lock_pw_hash': 'deadbeef', 'lock_salt': 'abc'})
    html = svc.render_desktop_shell()
    # The overlay div carries the seeded `active` class...
    assert 'class="lock-screen active"' in html, \
        'boot-locked render must seed #lock-screen.active so it covers frame 1'
    # ...and the overlay is a full-viewport OPAQUE cover that `.active` reveals
    # (position:fixed, inset:0, high z-index, display flipped by .active) — this
    # is what guarantees "covering", not DOM order.
    assert '.lock-screen{' in html
    assert 'position:fixed' in html and 'z-index:9999' in html and 'inset:0' in html
    assert '.lock-screen.active{' in html and 'display:flex' in html


def test_unlocked_boot_does_not_seed_active(tmp_path):
    """No password (fresh install) => the overlay stays hidden (display:none),
    so a normal boot goes straight to the desktop — the lock is not forced."""
    svc = _svc_with_data_dir(tmp_path)
    _write_session(tmp_path, {'wallpaper': 'aurora'})   # a blob with NO lock_pw_hash
    html = svc.render_desktop_shell()
    assert 'id="lock-screen"' in html, 'the overlay element is always present'
    assert 'class="lock-screen active"' not in html, \
        'without a lock password the overlay must NOT be seeded active'


def test_missing_session_file_boots_unlocked(tmp_path):
    """No shell_session.json at all (first ever boot) must not raise and must not
    force a lock."""
    svc = _svc_with_data_dir(tmp_path)   # empty dir, no session file
    html = svc.render_desktop_shell()
    assert 'class="lock-screen active"' not in html


# ── #169: hartNav is referenced AND served ──────────────────────────────────

def test_hartnav_referenced_and_served(tmp_path):
    """hartNav.js is in the shell's <script> includes AND fetchable at
    /shell/static/hartNav.js (Gate-6 registration)."""
    svc = _svc_with_data_dir(tmp_path)
    html = svc.render_desktop_shell()
    assert 'src="/shell/static/hartNav.js"' in html, \
        'hartNav.js must be a <script defer> include in the served shell'

    app = svc._create_flask_app()
    app.testing = True
    client = app.test_client()
    r = client.get('/shell/static/hartNav.js')
    assert r.status_code == 200, f'hartNav.js -> {r.status_code} (unregistered static route)'
    assert r.data, 'hartNav.js served empty'
    # It really is the nav module (its public surface), not some 200 placeholder.
    body = r.data.decode('utf-8', 'ignore')
    assert 'HartNavCore' in body and 'HartNav' in body


if __name__ == '__main__':
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        import pathlib
        p = pathlib.Path(d)
        test_boot_locked_seeds_active_lock_overlay(p)
        test_unlocked_boot_does_not_seed_active(p)
        test_missing_session_file_boots_unlocked(p)
        test_hartnav_referenced_and_served(p)
    print('RESULT: ALL PASS')
