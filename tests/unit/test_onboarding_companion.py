"""
Behavioural tests for the post-seal companion (Nunba) onboarding step.

After the HART name is sealed, the onboarding state machine enters a new
'setup_companion' phase that pre-fetches the Nunba desktop AppImage in the
background (to the SAME cache path the nunba.nix launcher stub expects) and
reports determinate progress on /api/onboarding/advance polls.

These drive the REAL functions in hart_onboarding (no grep tests): the boundary
that's mocked is the download itself (subprocess.run + the HEAD size probe), and
we assert the OBSERVABLE state transitions the UI renders against — done /
offline / error / timeout / skipped, live percent from the partial file, the
already-cached fast path, and the session-machine wiring (accept_name ->
setup_companion, skip/retry/poll, sealed-on-terminal).
"""

import os
import subprocess
import sys
import types

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import hart_onboarding as ho


def _fake_curl_writes(nbytes):
    """A subprocess.run replacement that 'downloads' nbytes to the -o target
    file and reports success (returncode 0)."""
    def _run(cmd, timeout=None, capture_output=False):
        out = None
        for i, a in enumerate(cmd):
            if a == '-o' and i + 1 < len(cmd):
                out = cmd[i + 1]
                break
        if out:
            with open(out, 'wb') as f:
                f.write(b'\x00' * nbytes)
        return types.SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
    return _run


def _clear(uid):
    ho._companion_state.pop(uid, None)
    ho.remove_session(uid)


# ── URL + cache path (DRY + nunba.nix lockstep) ────────────────────────────

def test_cache_path_matches_nunba_nix(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    p = ho._companion_target_path()
    assert p == os.path.join(str(tmp_path), 'hart', 'nunba',
                             'Nunba-x86_64.AppImage')


def test_url_from_install_links_with_env_override(monkeypatch):
    monkeypatch.delenv('NUNBA_APPIMAGE_URL', raising=False)
    url = ho._companion_url()
    # canonical source (core.install_links) or the literal fallback both land
    # on the same Linux AppImage asset.
    assert url.endswith('Nunba-x86_64.AppImage')
    assert 'github.com/hertz-ai/Nunba' in url
    monkeypatch.setenv('NUNBA_APPIMAGE_URL', 'http://mirror.local/x.AppImage')
    assert ho._companion_url() == 'http://mirror.local/x.AppImage'


# ── _run_companion_download: the download worker ───────────────────────────

def test_run_download_happy_path(monkeypatch, tmp_path):
    target = str(tmp_path / 'Nunba-x86_64.AppImage')
    monkeypatch.setattr(ho, '_probe_total_size', lambda url, timeout=20: 1000)
    monkeypatch.setattr(subprocess, 'run', _fake_curl_writes(1000))
    uid = 'happy'
    _clear(uid)
    ho._run_companion_download(uid, url='http://x/app', target=target, timeout=5)
    st = ho.companion_progress(uid)
    assert st['status'] == 'done'
    assert st['percent'] == 100
    assert os.path.isfile(target)                 # .part promoted to final
    assert not os.path.exists(target + '.part')   # partial cleaned up
    _clear(uid)


def test_run_download_offline_on_bad_returncode(monkeypatch, tmp_path):
    target = str(tmp_path / 'app.AppImage')
    monkeypatch.setattr(ho, '_probe_total_size', lambda url, timeout=20: 0)

    def _fail(cmd, timeout=None, capture_output=False):
        return types.SimpleNamespace(returncode=22, stdout=b'', stderr=b'404')

    monkeypatch.setattr(subprocess, 'run', _fail)
    uid = 'offline'
    _clear(uid)
    ho._run_companion_download(uid, url='http://x', target=target, timeout=5)
    st = ho.companion_progress(uid)
    assert st['status'] == 'offline'
    assert not os.path.exists(target)
    _clear(uid)


def test_run_download_error_when_curl_missing(monkeypatch, tmp_path):
    target = str(tmp_path / 'app.AppImage')
    monkeypatch.setattr(ho, '_probe_total_size', lambda url, timeout=20: 0)

    def _missing(cmd, timeout=None, capture_output=False):
        raise FileNotFoundError('curl')

    monkeypatch.setattr(subprocess, 'run', _missing)
    uid = 'nocurl'
    _clear(uid)
    ho._run_companion_download(uid, url='http://x', target=target, timeout=5)
    assert ho.companion_progress(uid)['status'] == 'error'   # graceful, no raise
    _clear(uid)


def test_run_download_error_on_timeout(monkeypatch, tmp_path):
    target = str(tmp_path / 'app.AppImage')
    monkeypatch.setattr(ho, '_probe_total_size', lambda url, timeout=20: 0)

    def _to(cmd, timeout=None, capture_output=False):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(subprocess, 'run', _to)
    uid = 'to'
    _clear(uid)
    ho._run_companion_download(uid, url='http://x', target=target, timeout=1)
    assert ho.companion_progress(uid)['status'] == 'error'
    _clear(uid)


# ── companion_progress: live determinate percent ──────────────────────────

def test_progress_determinate_percent_from_partfile(tmp_path):
    part = str(tmp_path / 'app.part')
    with open(part, 'wb') as f:
        f.write(b'\x00' * 500)
    uid = 'det'
    ho._companion_state[uid] = {'status': 'downloading', 'part': part,
                                'total': 1000, 'percent': 0, 'message': 'x'}
    assert ho.companion_progress(uid)['percent'] == 50
    # never reports 100 while still downloading (100 is reserved for promoted)
    with open(part, 'wb') as f:
        f.write(b'\x00' * 5000)
    assert ho.companion_progress(uid)['percent'] == 99
    _clear(uid)


def test_progress_idle_when_no_state():
    uid = 'never_seen'
    _clear(uid)
    st = ho.companion_progress(uid)
    assert st['status'] == 'idle'
    assert st['percent'] is None


# ── start_companion_download: gating + idempotency ─────────────────────────

def test_start_already_cached_skips_download(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    target = ho._companion_target_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, 'wb') as f:
        f.write(b'\x00' * 10)
    called = {'n': 0}
    monkeypatch.setattr(subprocess, 'run',
                        lambda *a, **k: called.__setitem__('n', called['n'] + 1))
    uid = 'cached'
    _clear(uid)
    st = ho.start_companion_download(uid)
    assert st['status'] == 'done'
    assert called['n'] == 0                # no curl spawned for a cached app
    _clear(uid)


def test_start_unsupported_platform_skips(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))   # nothing cached here
    monkeypatch.setattr(ho, '_companion_supported', lambda: False)
    uid = 'unsup'
    _clear(uid)
    st = ho.start_companion_download(uid)
    assert st['status'] == 'skipped'
    _clear(uid)


# ── Session state machine wiring ───────────────────────────────────────────

def test_accept_name_enters_setup_companion(monkeypatch):
    s = ho.HARTOnboardingSession('u_accept')
    s.phase = 'reveal'
    s.generated_name = {'name': 'auren', 'dimensions': {'creative': 0.9},
                        'emoji_combo': 'XY', 'element': 'neon', 'spirit': 'owl'}
    monkeypatch.setattr(ho.HARTNameRegistry, 'seal_name', lambda **k: True)
    started = {'n': 0}
    monkeypatch.setattr(
        ho, 'start_companion_download',
        lambda uid, force=False: started.__setitem__('n', started['n'] + 1))
    resp = s.advance(action='accept_name', data={})
    assert s.phase == 'setup_companion'
    assert resp.get('begin_companion') is True
    assert resp.get('name_sealed') is True
    assert not resp.get('sealed')           # session stays alive for the polls
    assert started['n'] == 1                # the download was kicked off once


def test_companion_skip_terminates_sealed():
    s = ho.HARTOnboardingSession('u_skip')
    s.phase = 'setup_companion'
    resp = s.advance(action='skip_companion', data={})
    assert resp.get('sealed') is True
    assert resp['companion']['status'] == 'skipped'
    assert s.phase == 'sealed'
    _clear('u_skip')


def test_companion_poll_inflight_not_sealed():
    uid = 'u_poll'
    ho._companion_state[uid] = {'status': 'downloading', 'percent': 10,
                                'message': 'x', 'total': 0}
    s = ho.HARTOnboardingSession(uid)
    s.phase = 'setup_companion'
    resp = s.advance(action='companion_progress', data={})
    assert not resp.get('sealed')
    assert resp['companion']['status'] == 'downloading'
    _clear(uid)


def test_companion_poll_done_seals():
    uid = 'u_done'
    ho._companion_state[uid] = {'status': 'done', 'percent': 100,
                                'message': 'ready', 'total': 100}
    s = ho.HARTOnboardingSession(uid)
    s.phase = 'setup_companion'
    resp = s.advance(action='companion_progress', data={})
    assert resp.get('sealed') is True
    assert resp['companion']['status'] == 'done'
    assert s.phase == 'sealed'
    _clear(uid)


def test_companion_retry_forces_restart(monkeypatch):
    uid = 'u_retry'
    calls = {'force': None}
    monkeypatch.setattr(
        ho, 'start_companion_download',
        lambda u, force=False: calls.__setitem__('force', force))
    ho._companion_state[uid] = {'status': 'error', 'percent': None,
                                'message': 'x', 'total': 0}
    s = ho.HARTOnboardingSession(uid)
    s.phase = 'setup_companion'
    resp = s.advance(action='retry_companion', data={})
    assert calls['force'] is True
    assert 'companion' in resp
    _clear(uid)


# ── Route pass-through (the /advance handler cleans up on sealed) ──────────

def test_route_advance_companion_skip_removes_session():
    try:
        from flask import Flask
    except ImportError:
        pytest.skip('flask not installed')
    from integrations.agent_engine.onboarding_routes import (
        register_onboarding_routes)
    app = Flask(__name__)
    register_onboarding_routes(app)
    client = app.test_client()
    uid = 'u_route_comp'
    s = ho.get_or_create_session(uid)
    s.phase = 'setup_companion'
    r = client.post('/api/onboarding/advance',
                    json={'user_id': uid, 'action': 'skip_companion', 'data': {}})
    assert r.status_code == 200
    body = r.get_json()
    assert body['sealed'] is True
    assert body['companion']['status'] == 'skipped'
    assert uid not in ho._sessions        # route removed it on the sealed terminal


def test_native_consumer_sees_seal_success(monkeypatch):
    """Regression: the GTK4 native ceremony (integrations/agent_engine/
    native_onboarding.py) drives this SAME FSM directly and decides "did the
    seal succeed?" from the accept_name response. When the companion step was
    added, accept_name stopped returning the bare 'sealed' flag (that is now the
    route's session-cleanup signal) and signals success via 'name_sealed'
    instead. A consumer that only checked 'sealed' would mistake a SUCCESSFUL
    seal for an error and loop forever. This locks the contract the native
    consumer keys off.
    """
    s = ho.HARTOnboardingSession('u_native')
    s.phase = 'reveal'
    s.generated_name = {'name': 'auren', 'dimensions': {'creative': 0.9},
                        'emoji_combo': 'XY', 'element': 'neon', 'spirit': 'owl'}
    monkeypatch.setattr(ho.HARTNameRegistry, 'seal_name', lambda **k: True)
    monkeypatch.setattr(ho, 'start_companion_download',
                        lambda uid, force=False: None)
    resp = s.advance(action='accept_name', data={})

    # The native consumer's success condition (name_sealed OR sealed) must be
    # truthy on a successful seal, so it shows the sealed page (not the error
    # loop). hart_name must be present for the sealed page to render the name.
    assert (resp.get('name_sealed') or resp.get('sealed'))
    assert resp.get('hart_name') == 'auren'
    # Document the regression: the pre-fix bare 'sealed' check is now falsy on a
    # successful seal (so the old native code would have shown "Something went
    # wrong" and re-revealed). name_sealed is the signal that fixes it.
    assert not resp.get('sealed')
    assert resp.get('name_sealed') is True


def test_accept_name_seal_failure_is_not_success(monkeypatch):
    """Boundary: when seal_name fails (e.g. handle already taken), accept_name
    returns an error with NEITHER name_sealed NOR sealed, so the native
    consumer correctly falls through to its error branch (no false success)."""
    s = ho.HARTOnboardingSession('u_native_fail')
    s.phase = 'reveal'
    s.generated_name = {'name': 'auren', 'dimensions': {'creative': 0.9},
                        'emoji_combo': 'XY', 'element': 'neon', 'spirit': 'owl'}
    monkeypatch.setattr(ho.HARTNameRegistry, 'seal_name', lambda **k: False)
    started = {'n': 0}
    monkeypatch.setattr(
        ho, 'start_companion_download',
        lambda uid, force=False: started.__setitem__('n', started['n'] + 1))
    resp = s.advance(action='accept_name', data={})
    assert not (resp.get('name_sealed') or resp.get('sealed'))
    assert resp.get('error')
    assert s.phase == 'reveal'           # stays on reveal, no companion step
    assert started['n'] == 0             # no download kicked off on a failed seal


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
