"""A person's phone reaches a desktop with a token it signed itself, once the
owner has allowed that phone (#111).

The phone signs the hive token shape nodes exchange (scope 'hive', node_sig
over the canonical payload) with its own PeerLink Ed25519 key and carries the
key in a ``node_public_key`` claim.  The desktop's gate (security.middleware,
bundled branch) admits it only when the owner holds a GRANTED
``device_access`` consent whose scope names exactly that key
(consent_service.device_scope): the key on file, not the claim, is what the
signature is verified against.  A phone the owner has not answered about
gets the ask filed and 403 consent_pending; one the owner said no to gets
403 consent_denied.  Everything admitted today is admitted as before.
"""
import os
import sys
import time
import types
import uuid

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from unittest.mock import patch  # noqa: E402

import jwt as pyjwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives import serialization  # noqa: E402
from flask import Flask, g, jsonify  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.social import consent_service as cs  # noqa: E402
# Imported here, not inside a test: the desktop fixture's patch.dict on
# sys.modules restores the module table on exit, and a module first imported
# inside a test (by the gate) is dropped from it and re-imported fresh next
# time, so a limiter patched in one copy would not be the one the gate uses.
from integrations.social import discovery  # noqa: E402
from integrations.social.consent_service import (  # noqa: E402
    ConsentService, device_fingerprint, device_scope,
)
from integrations.social.models import Base, UserConsent, db_session, get_engine  # noqa: E402
from security.middleware import _apply_api_auth  # noqa: E402
from security.node_integrity import canonical_payload  # noqa: E402

LAN = {'REMOTE_ADDR': '192.168.0.50'}
LOOPBACK = {'REMOTE_ADDR': '127.0.0.1'}
OWNER = 'owner-1'


class Phone:
    """A phone's PeerLink identity and the token it mints for a desktop."""

    def __init__(self, user_id='40021', username='Sathish'):
        self.key = Ed25519PrivateKey.generate()
        self.public_hex = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        self.user_id, self.username = user_id, username

    def token(self, *, claimed_key=None, exp_in=900, signer=None):
        payload = {
            'user_id': self.user_id, 'username': self.username,
            'jti': str(uuid.uuid4()), 'iat': int(time.time()),
            'exp': int(time.time()) + exp_in, 'type': 'access',
            'scope': 'hive', 'node_id': self.public_hex[:16],
            'iss': 'hive:hevolve',
            'node_public_key': claimed_key or self.public_hex,
        }
        signing_key = (signer or self).key
        payload['node_sig'] = signing_key.sign(
            canonical_payload(payload, exclude=('signature',))).hex()
        # The HS256 wrapper is the phone's own secret; the desktop never has
        # it and verify_hive_token reads the payload without it.
        return pyjwt.encode(payload, 'phone-secret', algorithm='HS256')


def _app():
    app = Flask('desktop')

    @app.route('/chat', methods=['GET', 'POST'])
    def chat():
        return jsonify({'ok': True, 'auth': getattr(g, 'auth_source', None)})
    return app


@pytest.fixture(autouse=True)
def _desktop_env(monkeypatch):
    monkeypatch.setenv('NUNBA_BUNDLED', '1')
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    for name in ('HEVOLVE_API_KEY', 'TRUSTED_PROXY', 'NUNBA_CI'):
        monkeypatch.delenv(name, raising=False)
    engine = get_engine()
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def desktop():
    app = _app()
    _apply_api_auth(app)
    secrets = {'security.secrets_manager': types.SimpleNamespace(
        get_secret=lambda name: '')}
    with patch.dict(sys.modules, secrets):
        yield app.test_client()


@pytest.fixture
def phone():
    return Phone()


def _bearer(token):
    return {'Authorization': f'Bearer {token}'}


def _allow(phone):
    with db_session() as db:
        ConsentService.grant_consent(db, OWNER, 'device_access',
                                     scope=device_scope(phone.public_hex))


# ── admission ──────────────────────────────────────────────────────────

def test_an_allowed_phone_is_admitted_as_a_device(desktop, phone):
    _allow(phone)
    r = desktop.post('/chat', environ_base=LAN, headers=_bearer(phone.token()),
                     json={'user_id': phone.user_id, 'text': 'hi'})
    assert r.status_code == 200
    assert r.get_json()['auth'] == 'device'


def test_a_phone_the_owner_has_not_answered_gets_the_ask_and_pending(desktop, phone):
    with patch.object(cs, '_emit') as emit:
        r = desktop.post('/chat', environ_base=LAN, headers=_bearer(phone.token()))
    assert r.status_code == 403
    assert r.get_json()['error'] == 'consent_pending'
    topic, ask = emit.call_args.args
    assert topic == 'consent.request'
    assert ask['user_id'] == OWNER
    assert ask['consent_type'] == 'device_access'
    assert ask['scope'] == device_scope(phone.public_hex)
    assert ask['agent_id'] is None
    assert ask['requester_name'] == 'Sathish'
    assert ask['requester_fingerprint'] == device_fingerprint(phone.public_hex)
    # The name is the phone's own claim: the ask says so, in the card's
    # words, and never states it as fact (phase 2 wording control).
    assert ask['reason'].startswith('A phone calling itself "Sathish"')
    assert "Sathish's phone" not in ask['reason']
    assert phone.public_hex not in ask['reason']
    with db_session() as db:
        row = db.query(UserConsent).filter_by(
            user_id=OWNER, consent_type='device_access').one()
        assert row.granted is False and row.scope == device_scope(phone.public_hex)


def test_the_owner_granting_the_ask_admits_the_phone(desktop, phone):
    """The card posts {consent_type, scope} from the ask; that grant is the
    key on file."""
    desktop.post('/chat', environ_base=LAN, headers=_bearer(phone.token()))
    with db_session() as db:
        ask = db.query(UserConsent).filter_by(consent_type='device_access').one()
        ConsentService.grant_consent(db, OWNER, ask.consent_type, scope=ask.scope)
    r = desktop.post('/chat', environ_base=LAN, headers=_bearer(phone.token()))
    assert r.status_code == 200


def test_revoking_ends_every_grant_for_the_phone(desktop, phone):
    """grant_consent is append-only and agent_id is NULL here, which the
    UNIQUE constraint does not de-duplicate, so two granted rows can exist;
    a revoke must end them all or the phone stays admitted."""
    _allow(phone)
    _allow(phone)
    with db_session() as db:
        assert db.query(UserConsent).filter_by(
            consent_type='device_access', granted=True).count() == 2
    assert desktop.post('/chat', environ_base=LAN,
                        headers=_bearer(phone.token())).status_code == 200
    with db_session() as db:
        ConsentService.revoke_consent(db, OWNER, 'device_access',
                                      scope=device_scope(phone.public_hex))
    with patch.object(cs, '_emit'):
        r = desktop.post('/chat', environ_base=LAN, headers=_bearer(phone.token()))
    assert r.status_code == 403
    assert r.get_json()['error'] == 'consent_denied'


def test_a_phone_the_owner_said_no_to_is_denied_and_not_asked_again(desktop, phone):
    desktop.post('/chat', environ_base=LAN, headers=_bearer(phone.token()))
    with db_session() as db:
        ConsentService.revoke_consent(db, OWNER, 'device_access',
                                      scope=device_scope(phone.public_hex))
    with patch.object(cs, '_emit') as emit:
        r = desktop.post('/chat', environ_base=LAN, headers=_bearer(phone.token()))
    assert r.status_code == 403
    assert r.get_json()['error'] == 'consent_denied'
    emit.assert_not_called()


# ── the key on file, not the claim ─────────────────────────────────────

def test_a_token_signed_by_another_key_claiming_the_allowed_key_is_refused(desktop, phone):
    _allow(phone)
    impostor = Phone(username='Mallory')
    forged = impostor.token(claimed_key=phone.public_hex)
    r = desktop.post('/chat', environ_base=LAN, headers=_bearer(forged))
    assert r.status_code == 401


def test_an_expired_token_from_an_allowed_phone_is_refused(desktop, phone):
    _allow(phone)
    r = desktop.post('/chat', environ_base=LAN,
                     headers=_bearer(phone.token(exp_in=-60)))
    assert r.status_code == 401


def test_a_blanket_grant_admits_no_phone(desktop, phone):
    with db_session() as db:
        ConsentService.grant_consent(db, OWNER, 'device_access', scope='*')
    with patch.object(cs, '_emit'):
        r = desktop.post('/chat', environ_base=LAN, headers=_bearer(phone.token()))
    assert r.status_code == 403
    assert r.get_json()['error'] == 'consent_pending'


def test_the_granted_key_must_be_exactly_the_claimed_key(desktop, phone):
    """A grant for key A admits nothing signed for key A' that differs in
    one character: the lookup is exact, never a prefix or a normalisation.
    If ConsentService.active_grant were ever loosened this goes red."""
    _allow(phone)
    near = Phone(username='Near')
    near.public_hex = phone.public_hex[:-1] + ('0' if phone.public_hex[-1] != '0' else '1')
    # `near` signs with its own key and claims its own (near-identical) key
    with patch.object(cs, '_emit'):
        r = desktop.post('/chat', environ_base=LAN, headers=_bearer(near.token()))
    assert r.status_code in (401, 403)
    assert r.status_code != 200


def test_no_ask_is_filed_for_a_key_the_caller_cannot_sign_for(desktop, phone):
    """Proof of possession before the owner is bothered: a token that names
    somebody else's key (or any key) without a signature from it files
    nothing and is a plain 401, so nobody can raise asks in another
    phone's name."""
    bystander = Phone(username='Bystander')
    forged = bystander.token(claimed_key=phone.public_hex)
    with patch.object(cs, '_emit') as emit:
        r = desktop.post('/chat', environ_base=LAN, headers=_bearer(forged))
    assert r.status_code == 401
    emit.assert_not_called()
    with db_session() as db:
        assert db.query(UserConsent).filter_by(consent_type='device_access').count() == 0


def test_ask_filing_is_rate_limited_per_address(desktop, monkeypatch):
    """An unauthenticated peer minting fresh keys must not flood the owner's
    consent table and cards: filing rides discovery._check_announce_rate."""
    monkeypatch.setattr(discovery, '_ANNOUNCE_RATE', {})
    monkeypatch.setattr(discovery, '_RATE_LIMIT', 3)
    with patch.object(cs, '_emit') as emit:
        for _ in range(6):
            r = desktop.post('/chat', environ_base=LAN,
                             headers=_bearer(Phone(username='Flood').token()))
            assert r.status_code == 403
            assert r.get_json()['error'] == 'consent_pending'
    assert emit.call_count == 3
    with db_session() as db:
        assert db.query(UserConsent).filter_by(consent_type='device_access').count() == 3


def test_an_admitted_phone_must_name_its_user_in_a_json_body(desktop, phone):
    """No user_id in the body is not "any user": a route's default user
    must never stand in for the phone (#57/#58)."""
    _allow(phone)
    r = desktop.post('/chat', environ_base=LAN, headers=_bearer(phone.token()),
                     json={'text': 'hi'})
    assert r.status_code == 403
    assert desktop.get('/chat', environ_base=LAN,
                       headers=_bearer(phone.token())).status_code == 200


def test_a_key_that_is_not_an_ed25519_key_is_not_a_device(desktop, phone):
    with patch.object(cs, '_emit') as emit:
        r = desktop.post('/chat', environ_base=LAN,
                         headers=_bearer(phone.token(claimed_key='../x')))
    assert r.status_code == 401
    emit.assert_not_called()


# ── the device acts only as its own user (#51) ─────────────────────────

def test_an_admitted_phone_cannot_act_as_another_user(desktop, phone):
    _allow(phone)
    r = desktop.post('/chat', environ_base=LAN, headers=_bearer(phone.token()),
                     json={'user_id': '10077', 'text': 'hi'})
    assert r.status_code == 403
    r = desktop.post('/chat', environ_base=LAN, headers=_bearer(phone.token()),
                     json={'user_id': int(phone.user_id), 'text': 'hi'})
    assert r.status_code == 200


# ── nothing admitted today changes ─────────────────────────────────────

def test_this_machine_and_plain_bad_bearers_are_as_before(desktop, phone):
    assert desktop.get('/chat', environ_base=LOOPBACK).status_code == 200
    assert desktop.get('/chat', environ_base=LAN).status_code == 401
    with patch.object(cs, '_emit') as emit:
        r = desktop.post('/chat', environ_base=LAN, headers=_bearer('forged'))
    assert r.status_code == 401
    emit.assert_not_called()


def test_a_desktop_with_no_owner_cannot_be_asked(desktop, phone, monkeypatch):
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID')
    with patch.object(cs, '_emit') as emit:
        r = desktop.post('/chat', environ_base=LAN, headers=_bearer(phone.token()))
    assert r.status_code == 401
    emit.assert_not_called()


def test_device_scope_accepts_only_a_64_hex_key():
    assert device_scope('AB' * 32) == 'device:' + 'ab' * 32
    for bad in ('', None, 'ab' * 31, 'zz' * 32, '../x', 'ab' * 32 + '\n'):
        assert device_scope(bad) is None
