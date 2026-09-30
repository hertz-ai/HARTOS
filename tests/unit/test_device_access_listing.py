"""The owner's list of trusted phones is the consent API, and it can say no
(#111 phase 2).

A device_access row's scope is the phone's public key, which nobody can
read, so the listing (GET /api/social/consent) carries the ``label`` the
phone signed into its first ask and the key's ``fingerprint``
(consent_service.device_fingerprint: first 16 hex, four groups), the thing
the owner matches against the phone.  The label is self-asserted: it is
written once and survives grant, revoke and re-allow, but a later ask
cannot rename the row.  A phone is allowed per key, never by a blanket.
The API stays the owner's: a phone's own credential is refused on it.
"""
import os
import sys
from types import SimpleNamespace

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.social import auth as auth_mod  # noqa: E402
from integrations.social import consent_api  # noqa: E402
from integrations.social import consent_service as cs  # noqa: E402
from integrations.social.consent_service import (  # noqa: E402
    ConsentService, device_fingerprint, device_scope,
)
from integrations.social.models import Base, db_session, get_engine  # noqa: E402
from tests.unit.test_device_access_gate import Phone  # noqa: E402

OWNER = 7
TOKEN = 'TEST-USER-'


@pytest.fixture(autouse=True)
def _tables():
    engine = get_engine()
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def app(monkeypatch):
    """The consent blueprint behind require_auth, with the token resolver
    stood in as tests/unit/test_consent_api.py does, on the shared
    in-memory social DB (so the service and the API see one table)."""
    from integrations.social.models import get_db

    def fake_get_user_from_token(token):
        if not isinstance(token, str) or not token.startswith(TOKEN):
            return None, get_db()
        uid = int(token[len(TOKEN):])
        return SimpleNamespace(id=uid, is_admin=False, is_moderator=False), get_db()

    monkeypatch.setattr(auth_mod, '_get_user_from_token', fake_get_user_from_token)
    flask_app = Flask(__name__)
    flask_app.config['TESTING'] = True
    flask_app.register_blueprint(consent_api.consent_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def asks(monkeypatch):
    """Every consent event the service emits, by topic."""
    seen = []
    monkeypatch.setattr(cs, '_emit', lambda topic, data, msg_id=None: seen.append((topic, dict(data))))
    return seen


def _owner():
    return {'Authorization': f'Bearer {TOKEN}{OWNER}'}


def _phone_asks(phone, name):
    with db_session() as db:
        ConsentService.request_consent(
            db, str(OWNER), 'device_access', scope=device_scope(phone.public_hex),
            reason=f"A phone calling itself \"{name}\" asks to use "
                   "this computer's agents from the network.",
            requester_name=name)


def _device_rows(client):
    resp = client.get('/api/social/consent?consent_type=device_access', headers=_owner())
    assert resp.status_code == 200
    return resp.get_json()['data']['consents']


# ── the fingerprint ───────────────────────────────────────────────────────


def test_fingerprint_is_the_first_16_hex_in_four_groups():
    key = '3F9A1C0277DEB4E1' + 'ab' * 24
    assert device_fingerprint(key) == '3f9a 1c02 77de b4e1'
    assert device_fingerprint(device_scope(key)) == '3f9a 1c02 77de b4e1'
    assert device_fingerprint('*') is None
    assert device_fingerprint('device:not-a-key') is None
    assert device_fingerprint(None) is None


# ── the listing ───────────────────────────────────────────────────────────


def test_a_phones_ask_lists_with_its_label_and_fingerprint(client):
    phone = Phone(user_id='40021', username='Sathish')
    _phone_asks(phone, 'Sathish')
    rows = _device_rows(client)
    assert len(rows) == 1
    row = rows[0]
    assert row['scope'] == device_scope(phone.public_hex)
    assert row['label'] == 'Sathish'
    assert row['fingerprint'] == device_fingerprint(phone.public_hex)
    assert row['granted'] is False and row['revoked_at'] is None  # pending


def test_the_ask_carries_the_fingerprint_beside_the_name(asks):
    phone = Phone(username='Sathish')
    _phone_asks(phone, 'Sathish')
    topic, ask = asks[-1]
    assert topic == 'consent.request'
    assert ask['requester_name'] == 'Sathish'
    assert ask['requester_fingerprint'] == device_fingerprint(phone.public_hex)


def test_rows_of_other_types_keep_their_shape(client):
    resp = client.post('/api/social/consent',
                       json={'consent_type': 'cloud_capability', 'scope': 'vision'},
                       headers=_owner())
    assert resp.status_code == 201
    rows = client.get('/api/social/consent', headers=_owner()).get_json()['data']['consents']
    assert 'label' not in rows[0] and 'fingerprint' not in rows[0]


# ── the lifecycle: pending -> allowed -> blocked -> allowed again ────────


def test_label_survives_grant_revoke_and_re_allow(client):
    phone = Phone(username='Sathish')
    scope = device_scope(phone.public_hex)
    _phone_asks(phone, 'Sathish')

    # Always allow (the card, or the privacy page)
    resp = client.post('/api/social/consent',
                       json={'consent_type': 'device_access', 'scope': scope},
                       headers=_owner())
    assert resp.status_code == 201
    with db_session() as db:
        allowed = ConsentService.active_grant(db, str(OWNER), 'device_access', scope=scope)
        assert allowed is not None and allowed.label == 'Sathish'

    # Block it
    resp = client.post('/api/social/consent/revoke',
                       json={'consent_type': 'device_access', 'scope': scope},
                       headers=_owner())
    assert resp.status_code == 200
    with db_session() as db:
        assert ConsentService.active_grant(db, str(OWNER), 'device_access', scope=scope) is None
        assert ConsentService.declined(db, str(OWNER), 'device_access', scope=scope)
    rows = _device_rows(client)
    assert all(r['label'] == 'Sathish' for r in rows), rows

    # Allow it again: the label the ask was filed under still stands
    resp = client.post('/api/social/consent',
                       json={'consent_type': 'device_access', 'scope': scope},
                       headers=_owner())
    assert resp.status_code == 201
    with db_session() as db:
        again = ConsentService.active_grant(db, str(OWNER), 'device_access', scope=scope)
        assert again is not None and again.label == 'Sathish'
    assert all(r['label'] == 'Sathish' for r in _device_rows(client))


def test_a_later_ask_cannot_rename_the_phone(client, asks):
    phone = Phone(username='Sathish')
    _phone_asks(phone, 'Sathish')
    _phone_asks(phone, 'Not Sathish')
    rows = _device_rows(client)
    assert [r['label'] for r in rows] == ['Sathish']
    # the re-ask still names what the phone claims now, with the fingerprint
    assert asks[-1][1]['requester_name'] == 'Not Sathish'
    assert asks[-1][1]['requester_fingerprint'] == device_fingerprint(phone.public_hex)


def test_blocking_one_phone_leaves_another_allowed(client):
    a, b = Phone(username='A'), Phone(username='B')
    for phone, name in ((a, 'A'), (b, 'B')):
        _phone_asks(phone, name)
        client.post('/api/social/consent',
                    json={'consent_type': 'device_access', 'scope': device_scope(phone.public_hex)},
                    headers=_owner())
    client.post('/api/social/consent/revoke',
                json={'consent_type': 'device_access', 'scope': device_scope(a.public_hex)},
                headers=_owner())
    with db_session() as db:
        assert ConsentService.active_grant(db, str(OWNER), 'device_access',
                                           scope=device_scope(a.public_hex)) is None
        assert ConsentService.active_grant(db, str(OWNER), 'device_access',
                                           scope=device_scope(b.public_hex)) is not None


# ── what the API refuses ─────────────────────────────────────────────────


def test_a_blanket_device_grant_is_refused(client):
    for scope in ('*', 'device:*', 'device:not-a-key'):
        resp = client.post('/api/social/consent',
                           json={'consent_type': 'device_access', 'scope': scope},
                           headers=_owner())
        assert resp.status_code == 400, scope
    assert _device_rows(client) == []


def test_a_phones_own_credential_cannot_use_the_owners_api():
    """The real require_auth: a phone-signed token is no local JWT and no API
    token, so the listing, grant, revoke and decline are the owner's only.

    Not redundant, a tripwire (hartos-3e review): there is no device guard
    in consent_api because require_auth refuses the token by construction;
    the day require_auth starts honouring g.auth_source == 'device' the way
    /chat's gate does (1b3e875f9), this goes red and the guard is due."""
    flask_app = Flask(__name__)
    flask_app.config['TESTING'] = True
    flask_app.register_blueprint(consent_api.consent_bp)
    c = flask_app.test_client()
    phone = Phone(username='Sathish')
    bearer = {'Authorization': f'Bearer {phone.token()}'}
    body = {'consent_type': 'device_access', 'scope': device_scope(phone.public_hex)}
    assert c.get('/api/social/consent', headers=bearer).status_code == 401
    assert c.post('/api/social/consent', json=body, headers=bearer).status_code == 401
    assert c.post('/api/social/consent/revoke', json=body, headers=bearer).status_code == 401
    assert c.post('/api/social/consent/decline', json=body, headers=bearer).status_code == 401
