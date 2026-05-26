"""Unit tests for the UserConsent UI blueprint (W0c F3 prereq).

Covers the contract spelled out by orchestrator run aa3ead1:

  1. Auth required — all 3 routes 401 without Bearer token.
  2. POST /consent creates a row, returns the new id.
  3. POST /consent/revoke sets revoked_at on the most-recent active row.
  4. POST /consent again after revoke creates a NEW row; the revoked
     row is preserved with revoked_at intact — both rows visible in
     GET /consent.
  5. GET /consent returns rows newest-first by granted_at.
  6. GET /consent?active_only=true returns only un-revoked rows.
  7. POST /consent/revoke with no active row returns 404 (neutral).
  8. PRIVACY: GET /consent for user A returns only A's rows.

Fixture pattern mirrors tests/unit/test_encounter_api.py: in-memory
SQLite engine + monkey-patched _get_user_from_token resolver so tests
exercise require_auth's real lifecycle without a JWT pipeline.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..'),
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ══════════════════════════════════════════════════════════════════════
# Flask app fixture with consent_bp mounted + auth monkey-patched.
# Fresh sqlalchemy Session per request because require_auth closes the
# session in its after-request cleanup.
# ══════════════════════════════════════════════════════════════════════


_TEST_TOKEN_PREFIX = 'TEST-USER-'  # Authorization: Bearer TEST-USER-<id>


@pytest.fixture
def app(monkeypatch):
    from flask import Flask
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from integrations.social import auth as _auth_mod
    from integrations.social import consent_api
    # Import the model AFTER auth so module-load order matches prod.
    from integrations.social.models import UserConsent  # noqa: F401

    engine = create_engine('sqlite:///:memory:')
    UserConsent.__table__.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)

    def _fake_get_user_from_token(token):
        if not isinstance(token, str) or not token.startswith(
            _TEST_TOKEN_PREFIX,
        ):
            return None, None
        try:
            uid = int(token[len(_TEST_TOKEN_PREFIX):])
        except ValueError:
            return None, None
        return (
            SimpleNamespace(id=uid, is_admin=False, is_moderator=False),
            SessionFactory(),
        )

    monkeypatch.setattr(
        _auth_mod, '_get_user_from_token', _fake_get_user_from_token,
    )

    flask_app = Flask(__name__)
    flask_app.config['TESTING'] = True
    flask_app.register_blueprint(consent_api.consent_bp)
    flask_app.test_engine = engine  # type: ignore[attr-defined]
    yield flask_app
    engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


def _as_user(uid: int) -> dict:
    return {'Authorization': f'Bearer {_TEST_TOKEN_PREFIX}{uid}'}


# ══════════════════════════════════════════════════════════════════════
# 1. Auth required
# ══════════════════════════════════════════════════════════════════════


def test_all_routes_require_auth(client):
    probes = [
        ('POST', '/api/social/consent',
            {'consent_type': 'cloud_capability', 'scope': '*'}),
        ('POST', '/api/social/consent/revoke',
            {'consent_type': 'cloud_capability', 'scope': '*'}),
        ('GET', '/api/social/consent', None),
    ]
    for method, path, body in probes:
        if method == 'GET':
            resp = client.get(path)
        else:
            resp = client.post(path, json=body)
        assert resp.status_code == 401, (
            f'{method} {path} did not 401 (got {resp.status_code})'
        )


# ══════════════════════════════════════════════════════════════════════
# 2. POST /consent creates a row + returns id
# ══════════════════════════════════════════════════════════════════════


def test_grant_creates_row_and_returns_id(client):
    resp = client.post(
        '/api/social/consent',
        json={'consent_type': 'cloud_capability',
              'scope': 'encounter_icebreaker'},
        headers=_as_user(10),
    )
    assert resp.status_code == 201
    data = resp.get_json()['data']
    assert data['id']
    assert data['consent_type'] == 'cloud_capability'
    assert data['scope'] == 'encounter_icebreaker'
    assert data['granted_at']  # ISO string

    # Row landed in the DB.
    list_resp = client.get(
        '/api/social/consent', headers=_as_user(10),
    )
    assert list_resp.status_code == 200
    consents = list_resp.get_json()['data']['consents']
    assert len(consents) == 1
    assert consents[0]['id'] == data['id']
    assert consents[0]['granted'] is True
    assert consents[0]['revoked_at'] is None


def test_grant_requires_consent_type(client):
    resp = client.post(
        '/api/social/consent', json={}, headers=_as_user(10),
    )
    assert resp.status_code == 400
    assert 'consent_type' in resp.get_json()['error']


def test_grant_defaults_scope_to_star(client):
    resp = client.post(
        '/api/social/consent',
        json={'consent_type': 'cloud_capability'},
        headers=_as_user(10),
    )
    assert resp.status_code == 201
    assert resp.get_json()['data']['scope'] == '*'


# ══════════════════════════════════════════════════════════════════════
# 3. POST /consent/revoke sets revoked_at on the active row
# ══════════════════════════════════════════════════════════════════════


def test_revoke_sets_revoked_at_on_active_row(client):
    grant = client.post(
        '/api/social/consent',
        json={'consent_type': 'cloud_capability', 'scope': '*'},
        headers=_as_user(10),
    )
    grant_id = grant.get_json()['data']['id']
    granted_at = grant.get_json()['data']['granted_at']

    resp = client.post(
        '/api/social/consent/revoke',
        json={'consent_type': 'cloud_capability', 'scope': '*'},
        headers=_as_user(10),
    )
    assert resp.status_code == 200
    body = resp.get_json()['data']
    assert body['id'] == grant_id
    assert body['revoked_at']

    # The grant row's granted_at MUST NOT have been touched (audit
    # immutability).
    list_resp = client.get(
        '/api/social/consent', headers=_as_user(10),
    )
    rows = list_resp.get_json()['data']['consents']
    assert len(rows) == 1
    assert rows[0]['id'] == grant_id
    assert rows[0]['granted_at'] == granted_at, (
        'granted_at must be preserved after revoke'
    )
    assert rows[0]['revoked_at'] is not None


# ══════════════════════════════════════════════════════════════════════
# 4. Re-grant after revoke creates a NEW row; revoked row preserved
# ══════════════════════════════════════════════════════════════════════


def test_regrant_after_revoke_appends_new_row(client):
    g1 = client.post(
        '/api/social/consent',
        json={'consent_type': 'cloud_capability', 'scope': '*'},
        headers=_as_user(10),
    )
    id1 = g1.get_json()['data']['id']
    granted_at_1 = g1.get_json()['data']['granted_at']

    client.post(
        '/api/social/consent/revoke',
        json={'consent_type': 'cloud_capability', 'scope': '*'},
        headers=_as_user(10),
    )

    g2 = client.post(
        '/api/social/consent',
        json={'consent_type': 'cloud_capability', 'scope': '*'},
        headers=_as_user(10),
    )
    id2 = g2.get_json()['data']['id']
    assert id2 != id1, 'second grant must be a NEW row'

    # Both rows visible in GET; the revoked one keeps its revoked_at.
    list_resp = client.get(
        '/api/social/consent', headers=_as_user(10),
    )
    rows = list_resp.get_json()['data']['consents']
    assert len(rows) == 2
    by_id = {r['id']: r for r in rows}
    assert by_id[id1]['granted_at'] == granted_at_1
    assert by_id[id1]['revoked_at'] is not None
    assert by_id[id2]['revoked_at'] is None
    assert by_id[id2]['granted'] is True


# ══════════════════════════════════════════════════════════════════════
# 5. GET /consent newest-first
# ══════════════════════════════════════════════════════════════════════


def test_list_returns_newest_first(client):
    import time
    g1 = client.post(
        '/api/social/consent',
        json={'consent_type': 'cloud_capability', 'scope': 'a'},
        headers=_as_user(10),
    )
    # Tiny pause so granted_at differs at the second resolution
    # SQLite's datetime defaults to second precision.
    time.sleep(0.01)
    g2 = client.post(
        '/api/social/consent',
        json={'consent_type': 'cloud_capability', 'scope': 'b'},
        headers=_as_user(10),
    )
    time.sleep(0.01)
    g3 = client.post(
        '/api/social/consent',
        json={'consent_type': 'cloud_capability', 'scope': 'c'},
        headers=_as_user(10),
    )
    list_resp = client.get(
        '/api/social/consent', headers=_as_user(10),
    )
    rows = list_resp.get_json()['data']['consents']
    assert len(rows) == 3
    # Newest first → g3, g2, g1
    assert rows[0]['id'] == g3.get_json()['data']['id']
    assert rows[1]['id'] == g2.get_json()['data']['id']
    assert rows[2]['id'] == g1.get_json()['data']['id']


# ══════════════════════════════════════════════════════════════════════
# 6. GET /consent?active_only=true filters revoked rows
# ══════════════════════════════════════════════════════════════════════


def test_list_active_only_filters_revoked(client):
    client.post(
        '/api/social/consent',
        json={'consent_type': 'cloud_capability', 'scope': 'a'},
        headers=_as_user(10),
    )
    g2 = client.post(
        '/api/social/consent',
        json={'consent_type': 'cloud_capability', 'scope': 'b'},
        headers=_as_user(10),
    )
    # Revoke scope=a
    client.post(
        '/api/social/consent/revoke',
        json={'consent_type': 'cloud_capability', 'scope': 'a'},
        headers=_as_user(10),
    )

    resp = client.get(
        '/api/social/consent?active_only=true', headers=_as_user(10),
    )
    rows = resp.get_json()['data']['consents']
    assert len(rows) == 1
    assert rows[0]['id'] == g2.get_json()['data']['id']
    assert rows[0]['scope'] == 'b'

    # Sanity: without active_only both rows return.
    resp_all = client.get(
        '/api/social/consent', headers=_as_user(10),
    )
    assert len(resp_all.get_json()['data']['consents']) == 2


def test_list_filter_by_consent_type(client):
    client.post(
        '/api/social/consent',
        json={'consent_type': 'cloud_capability', 'scope': '*'},
        headers=_as_user(10),
    )
    client.post(
        '/api/social/consent',
        json={'consent_type': 'data_access', 'scope': '*'},
        headers=_as_user(10),
    )
    resp = client.get(
        '/api/social/consent?consent_type=cloud_capability',
        headers=_as_user(10),
    )
    rows = resp.get_json()['data']['consents']
    assert len(rows) == 1
    assert rows[0]['consent_type'] == 'cloud_capability'


# ══════════════════════════════════════════════════════════════════════
# 7. Revoke with no active row — 404 neutral
# ══════════════════════════════════════════════════════════════════════


def test_revoke_no_active_row_returns_404(client):
    resp = client.post(
        '/api/social/consent/revoke',
        json={'consent_type': 'cloud_capability', 'scope': '*'},
        headers=_as_user(10),
    )
    assert resp.status_code == 404
    body = resp.get_json()
    # Neutral message — must not leak whether ANY rows exist.
    assert body['success'] is False
    err = body['error'].lower()
    assert 'active' in err or 'not found' in err or 'no consent' in err


def test_revoke_after_already_revoked_returns_404(client):
    """Revoking when the only matching row is already revoked must
    404 — there is no ACTIVE row to revoke.  This guards against a
    re-revoke quietly succeeding and hiding a UI bug."""
    client.post(
        '/api/social/consent',
        json={'consent_type': 'cloud_capability', 'scope': '*'},
        headers=_as_user(10),
    )
    r1 = client.post(
        '/api/social/consent/revoke',
        json={'consent_type': 'cloud_capability', 'scope': '*'},
        headers=_as_user(10),
    )
    assert r1.status_code == 200
    r2 = client.post(
        '/api/social/consent/revoke',
        json={'consent_type': 'cloud_capability', 'scope': '*'},
        headers=_as_user(10),
    )
    assert r2.status_code == 404


# ══════════════════════════════════════════════════════════════════════
# 8. PRIVACY: cross-user isolation
# ══════════════════════════════════════════════════════════════════════


def test_user_a_cannot_see_user_b_consents(client):
    # User A grants two consents.
    client.post(
        '/api/social/consent',
        json={'consent_type': 'cloud_capability', 'scope': '*'},
        headers=_as_user(10),
    )
    client.post(
        '/api/social/consent',
        json={'consent_type': 'data_access', 'scope': '*'},
        headers=_as_user(10),
    )
    # User B grants one different consent.
    client.post(
        '/api/social/consent',
        json={'consent_type': 'public_exposure', 'scope': '*'},
        headers=_as_user(20),
    )

    # B's GET sees only B's row, never A's.
    resp_b = client.get(
        '/api/social/consent', headers=_as_user(20),
    )
    rows_b = resp_b.get_json()['data']['consents']
    assert len(rows_b) == 1
    assert rows_b[0]['consent_type'] == 'public_exposure'

    # A's GET sees only A's two rows.
    resp_a = client.get(
        '/api/social/consent', headers=_as_user(10),
    )
    rows_a = resp_a.get_json()['data']['consents']
    assert len(rows_a) == 2
    types_a = {r['consent_type'] for r in rows_a}
    assert types_a == {'cloud_capability', 'data_access'}
    assert 'public_exposure' not in types_a


def test_user_a_cannot_revoke_user_b_consent(client):
    """Even if A revokes against the same (consent_type, scope) that
    B has granted, A's revoke must NOT touch B's row.  Returns 404
    for A and B's row stays active."""
    b_grant = client.post(
        '/api/social/consent',
        json={'consent_type': 'cloud_capability', 'scope': '*'},
        headers=_as_user(20),
    )
    b_id = b_grant.get_json()['data']['id']

    # A tries to revoke that triple — A has no row, must 404.
    resp = client.post(
        '/api/social/consent/revoke',
        json={'consent_type': 'cloud_capability', 'scope': '*'},
        headers=_as_user(10),
    )
    assert resp.status_code == 404

    # B's row is untouched.
    resp_b = client.get(
        '/api/social/consent', headers=_as_user(20),
    )
    rows = resp_b.get_json()['data']['consents']
    assert len(rows) == 1
    assert rows[0]['id'] == b_id
    assert rows[0]['revoked_at'] is None


# ══════════════════════════════════════════════════════════════════════
# Defensive input validation
# ══════════════════════════════════════════════════════════════════════


def test_grant_rejects_oversize_consent_type(client):
    resp = client.post(
        '/api/social/consent',
        json={'consent_type': 'x' * 31, 'scope': '*'},
        headers=_as_user(10),
    )
    assert resp.status_code == 400


def test_grant_rejects_oversize_scope(client):
    resp = client.post(
        '/api/social/consent',
        json={'consent_type': 'cloud_capability', 'scope': 'x' * 101},
        headers=_as_user(10),
    )
    assert resp.status_code == 400
