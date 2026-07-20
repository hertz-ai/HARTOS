"""
Behavioural tests for #167 — "accept the name" must never be a dead no-op.

Accepting the generated HART name must:
  1. seal the name into the profile (HARTNameRegistry.seal_name), and
  2. advance the shell to the desktop (goto_desktop signal),
  3. idempotently — a re-sent / double-clicked accept (or an already-onboarded
     user whose session was cleaned up) still resolves to the sealed identity
     and the desktop, never a silent no-op that leaves the client on 'reveal',
  4. without regressing the post-seal companion / skip terminal.

These drive the REAL route handler (onboarding_routes._onboarding_advance) and
the REAL onboarding FSM (hart_onboarding.HARTOnboardingSession.advance) through
Flask's test client. The only mocked boundary is the DB-backed seal (seal_name +
the has_hart_name / get_hart_profile readers), replaced by a stateful in-memory
fake so we can assert the observable seal side-effect and the idempotency — NOT
grep the source.
"""

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import hart_onboarding as ho


class _FakeSeal:
    """Stateful in-memory stand-in for the DB-backed HART name registry."""

    def __init__(self):
        self.sealed = {}       # user_id -> profile dict
        self.seal_calls = []   # every seal_name(**kw) invocation

    def seal_name(self, **kw):
        self.seal_calls.append(kw)
        uid = kw['user_id']
        # First seal wins (mirrors the real "already has a handle" guard).
        if uid in self.sealed:
            return False
        self.sealed[uid] = {
            'name': kw['name'],
            'hart_tag': '@' + kw['name'],
            'emoji_combo': kw.get('emoji_combo', ''),
        }
        return True

    def has_hart_name(self, uid):
        return uid in self.sealed

    def get_hart_profile(self, uid):
        return self.sealed.get(uid)


@pytest.fixture
def fake_seal(monkeypatch):
    fake = _FakeSeal()
    monkeypatch.setattr(ho.HARTNameRegistry, 'seal_name', fake.seal_name)
    monkeypatch.setattr(ho, 'has_hart_name', fake.has_hart_name)
    monkeypatch.setattr(ho, 'get_hart_profile', fake.get_hart_profile)
    # The FSM kicks off a background AppImage pre-fetch on a successful seal;
    # keep it a no-op so tests never touch the network / spawn a thread.
    monkeypatch.setattr(ho, 'start_companion_download',
                        lambda uid, force=False: None)
    return fake


@pytest.fixture
def client():
    flask = pytest.importorskip('flask')
    from integrations.agent_engine.onboarding_routes import (
        register_onboarding_routes)
    app = flask.Flask(__name__)
    app.config['TESTING'] = True
    register_onboarding_routes(app)
    return app.test_client()


def _at_reveal(uid):
    """Put a live session at the reveal phase with a generated name, exactly as
    the ceremony would after the pre-reveal -> reveal transition."""
    s = ho.get_or_create_session(uid)
    s.phase = 'reveal'
    s.language = 'en'
    s.locale = 'en_US'
    s.passion_key = 'music_art'
    s.escape_key = 'quiet_alone'
    s.generated_name = {
        'name': 'auren', 'dimensions': {'creative': 0.9},
        'emoji_combo': 'XY', 'element': 'neon', 'spirit': 'owl',
    }
    return s


def _accept(client, uid):
    return client.post('/api/onboarding/advance',
                       json={'user_id': uid, 'action': 'accept_name',
                             'data': {}})


# ── 1. reveal -> accept seals the name AND advances to the desktop ──────────

def test_accept_seals_name_and_advances_to_desktop(client, fake_seal):
    uid = 'u_accept_ok'
    _at_reveal(uid)

    r = _accept(client, uid)
    assert r.status_code == 200
    body = r.get_json()

    # It resolved off the reveal (not a dead no-op).
    assert body['success'] is True
    assert body.get('name_sealed') is True
    assert body.get('goto_desktop') is True

    # The generated name was sealed into the profile (observable side-effect).
    assert fake_seal.has_hart_name(uid) is True
    assert len(fake_seal.seal_calls) == 1
    assert fake_seal.seal_calls[0]['name'] == 'auren'

    # The sealed profile is surfaced for the desktop hand-off.
    assert body['profile']['name'] == 'auren'
    assert body.get('hart_name') == 'auren' or body['profile']['name'] == 'auren'

    ho.remove_session(uid)


# ── 2. accept is idempotent — a re-sent accept still succeeds, never re-seals ─

def test_accept_is_idempotent(client, fake_seal):
    uid = 'u_accept_idem'
    _at_reveal(uid)

    first = _accept(client, uid).get_json()
    assert first.get('name_sealed') is True
    assert len(fake_seal.seal_calls) == 1

    # Re-send accept (double-click / retry). The session may have been cleaned
    # up; the user is already sealed. It must resolve to the same sealed
    # identity + desktop, and must NOT attempt a second seal.
    second = _accept(client, uid)
    assert second.status_code == 200
    body = second.get_json()
    assert body['success'] is True
    assert body.get('name_sealed') is True
    assert body.get('sealed') is True
    assert body.get('goto_desktop') is True
    assert body['profile']['name'] == 'auren'
    assert 'error' not in body
    assert len(fake_seal.seal_calls) == 1   # idempotent: no re-seal

    # Idempotent branch cleans up any leftover in-memory session.
    assert uid not in ho._sessions

    ho.remove_session(uid)


# ── 3. an already-onboarded user with no session goes straight to desktop ────

def test_accept_when_already_onboarded_no_session(client, fake_seal):
    uid = 'u_already'
    # Pre-seal directly, then drop the session (a completed prior ceremony).
    fake_seal.seal_name(user_id=uid, name='vetrix', dimensions={},
                        emoji_combo='ZZ')
    ho.remove_session(uid)
    assert uid not in ho._sessions

    r = _accept(client, uid)
    assert r.status_code == 200
    body = r.get_json()
    assert body.get('goto_desktop') is True
    assert body.get('name_sealed') is True
    assert body['profile']['name'] == 'vetrix'
    # No spurious fresh-session FSM walk was left behind.
    assert uid not in ho._sessions
    # No second seal attempt for the already-sealed user.
    assert len(fake_seal.seal_calls) == 1


# ── 4. the skip (companion) terminal still works (zero regression) ──────────

def test_skip_companion_still_reaches_sealed(client, fake_seal):
    uid = 'u_skip'
    # Land the session on the post-seal companion step, as accept_name leaves it.
    s = ho.get_or_create_session(uid)
    s.phase = 'setup_companion'

    r = client.post('/api/onboarding/advance',
                    json={'user_id': uid, 'action': 'skip_companion',
                          'data': {}})
    assert r.status_code == 200
    body = r.get_json()
    assert body['success'] is True
    assert body['sealed'] is True
    assert body['companion']['status'] == 'skipped'
    # The route cleans up the session on the sealed terminal.
    assert uid not in ho._sessions


# ── 5. a failed seal is NOT a false success (no goto_desktop, error surfaced) ─

def test_accept_seal_failure_is_not_a_false_desktop_advance(client, monkeypatch):
    uid = 'u_seal_fail'
    _at_reveal(uid)

    # Seal fails (e.g. handle taken at seal time); the user is NOT onboarded.
    monkeypatch.setattr(ho.HARTNameRegistry, 'seal_name', lambda **k: False)
    monkeypatch.setattr(ho, 'has_hart_name', lambda u: False)
    monkeypatch.setattr(ho, 'get_hart_profile', lambda u: None)
    monkeypatch.setattr(ho, 'start_companion_download',
                        lambda uid, force=False: None)

    r = _accept(client, uid)
    assert r.status_code == 200
    body = r.get_json()
    assert not body.get('name_sealed')
    assert not body.get('goto_desktop')
    assert body.get('error')
    # Session stays on reveal so the user can retry / try another.
    assert ho._sessions[uid].phase == 'reveal'

    ho.remove_session(uid)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
