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
from flask import Flask, g, jsonify, request  # noqa: E402

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

    @app.route('/upload', methods=['POST'])
    def upload():
        # What an upload route behind the gate finds: the form and the file.
        sent = request.files.get('file')
        return jsonify({'auth': getattr(g, 'auth_source', None),
                        'user_id': request.form.get('user_id'),
                        'request_id': request.form.get('request_id'),
                        'file': sent.read().hex() if sent else None})

    @app.route('/forward', methods=['POST'])
    def forward():
        # A route that passes the body on as sent (Nunba's /api/social
        # proxy): it reads the body and never the form.
        return jsonify({'raw': request.get_data().hex()})
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
    assert ask['reason'].startswith(
        'A phone (it says its name is "Sathish")')
    assert 'Allow it only if the phone is yours' in ask['reason']
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


# A user named beside the body is held to the token as the body's is.  The
# camera frame route (Nunba /api/vision/frame: a JPEG body, the user in the
# query or X-User-Id) held this itself; the gate holds it for every route.

_JPEG = b'\xff\xd8\xff\xe0' + b'frame' * 40 + b'\xff\xd9'


def _frame(desktop, phone, query='', headers=None):
    return desktop.post(f'/chat{query}', environ_base=LAN, data=_JPEG,
                        content_type='image/jpeg',
                        headers={**_bearer(phone.token()), **(headers or {})})


def test_an_admitted_phone_cannot_name_another_user_in_the_query(desktop, phone):
    _allow(phone)
    r = _frame(desktop, phone, '?user_id=10077&channel=camera')
    assert r.status_code == 403
    assert 'drop user_id' in r.get_json()['error']      # says what to do
    r = desktop.get('/chat?user_id=10077', environ_base=LAN,
                    headers=_bearer(phone.token()))
    assert r.status_code == 403


def test_an_admitted_phone_cannot_name_another_user_in_the_header(desktop, phone):
    _allow(phone)
    r = _frame(desktop, phone, headers={'X-User-Id': '10077'})
    assert r.status_code == 403
    assert 'drop user_id' in r.get_json()['error']
    # its own user in the query does not excuse another in the header
    r = _frame(desktop, phone, f'?user_id={phone.user_id}',
               headers={'X-User-Id': '10077'})
    assert r.status_code == 403


def test_an_admitted_phone_naming_its_own_user_or_nobody_is_let_in(desktop, phone):
    """What the phone sends today (Android DesktopCameraFrames.frameUrl:
    ?user_id=<its user>&channel=camera) keeps working, named or not."""
    _allow(phone)
    for query, headers in (
            (f'?user_id={phone.user_id}&channel=camera', None),
            ('?channel=camera', None),
            ('', None),
            ('?user_id=', None),
            ('', {'X-User-Id': phone.user_id}),
            (f'?user_id={phone.user_id}', {'X-User-Id': phone.user_id})):
        r = _frame(desktop, phone, query, headers)
        assert r.status_code == 200, (query, headers, r.get_data(as_text=True))
        assert r.get_json()['auth'] == 'device'


def test_a_json_body_does_not_excuse_another_user_beside_it(desktop, phone):
    _allow(phone)
    own = {'user_id': phone.user_id, 'text': 'hi'}
    r = desktop.post('/chat?user_id=10077', environ_base=LAN,
                     headers=_bearer(phone.token()), json=own)
    assert r.status_code == 403
    r = desktop.post('/chat', environ_base=LAN, json=own,
                     headers={**_bearer(phone.token()), 'X-User-Id': '10077'})
    assert r.status_code == 403
    r = desktop.post(f'/chat?user_id={phone.user_id}', environ_base=LAN,
                     headers=_bearer(phone.token()), json=own)
    assert r.status_code == 200


def test_a_token_naming_no_user_cannot_borrow_one_from_the_query(desktop):
    nobody = Phone(user_id=None)
    _allow(nobody)
    assert _frame(desktop, nobody, '?user_id=10077').status_code == 403
    assert _frame(desktop, nobody, headers={'X-User-Id': '10077'}).status_code == 403
    # ... and naming no user it is admitted as before; a route that acts for
    # a person then finds no user on the token and says so itself.
    assert _frame(desktop, nobody).status_code == 200


# A form names its user in a field.  An upload is one: Nunba's /upload/file,
# /upload/image and /upload/audio and its /voice/transcribe and
# /voice/diarize read the form's user_id, and the gate read no form, so an
# allowed phone could file a book, a teacher picture or a voice sample under
# another user (review of 1b04d59ec).

_OTHER = '10077'
_MULTIPART, _URLENCODED = 'multipart/form-data', 'application/x-www-form-urlencoded'


def _form(desktop, phone, fields, kind=_MULTIPART, query='', env=LAN):
    """A form as an upload sends it: multipart with the file beside the
    fields, or the fields alone urlencoded.  ``fields`` is a list of pairs,
    so a name can be sent twice."""
    if kind == _MULTIPART:
        from io import BytesIO
        from werkzeug.datastructures import MultiDict
        body = {'data': MultiDict(list(fields) + [('file', (BytesIO(_JPEG), 'f.jpg'))]),
                'content_type': _MULTIPART}
    else:
        from urllib.parse import urlencode
        body = {'data': urlencode(fields), 'content_type': _URLENCODED}
    headers = _bearer(phone.token()) if phone is not None else {}
    return desktop.post(f'/upload{query}', environ_base=env, headers=headers,
                        **body)


@pytest.mark.parametrize('kind', [_MULTIPART, _URLENCODED])
def test_an_admitted_phone_cannot_name_another_user_in_a_form(desktop, phone, kind):
    _allow(phone)
    r = _form(desktop, phone, [('user_id', _OTHER), ('request_id', 'r1')], kind)
    assert r.status_code == 403, r.get_data(as_text=True)
    assert 'drop user_id' in r.get_json()['error']      # says what to do


@pytest.mark.parametrize('kind', [_MULTIPART, _URLENCODED])
@pytest.mark.parametrize('fields', [
    [('user_id', '40021'), ('user_id', _OTHER)],     # its own, then another
    [('user_id', _OTHER), ('user_id', '40021')],
    [('user_id', ''), ('user_id', _OTHER)],          # an empty one first
    [('user_id', ' ' + _OTHER)],                     # padded
    [('user_id', _OTHER + ' ')],
    [('user_id', '40021 ')],        # its own, padded: not the token's spelling
    [('user_id', '40021\x00')],
    [('user_id', '040021')],
    [('user_id', '40021.0')],
    [('request_id', 'r1'), ('user_id', _OTHER)],     # after another field
])
def test_no_spelling_of_another_user_in_a_form_gets_in(desktop, phone, kind, fields):
    _allow(phone)
    r = _form(desktop, phone, fields, kind)
    assert r.status_code == 403, (fields, r.get_data(as_text=True))


def test_a_percent_spelled_user_in_a_form_is_read_as_the_route_reads_it(desktop, phone):
    _allow(phone)
    other = desktop.post('/upload', environ_base=LAN, headers=_bearer(phone.token()),
                         data='user_id=%31%30%30%37%37', content_type=_URLENCODED)
    assert other.status_code == 403
    own = desktop.post('/upload', environ_base=LAN, headers=_bearer(phone.token()),
                       data='user_id=%34%30%30%32%31', content_type=_URLENCODED)
    assert own.status_code == 200 and own.get_json()['user_id'] == '40021'


@pytest.mark.parametrize('kind', [_MULTIPART, _URLENCODED])
def test_a_form_does_not_excuse_another_user_beside_it(desktop, phone, kind):
    _allow(phone)
    own = [('user_id', phone.user_id)]
    assert _form(desktop, phone, own, kind, f'?user_id={_OTHER}').status_code == 403
    assert _form(desktop, phone, [('user_id', _OTHER)], kind,
                 f'?user_id={phone.user_id}').status_code == 403


@pytest.mark.parametrize('kind', [_MULTIPART, _URLENCODED])
def test_an_admitted_phone_naming_its_own_user_or_nobody_in_a_form_is_let_in(
        desktop, phone, kind):
    """What the phone sends today keeps working: Android's one form to a
    desktop is the call audio it posts to /api/voice/transcribe
    (DesktopTranscriber: parts ``audio`` and ``source``, no user_id).  A form
    naming the phone's own user passes too, and the route still finds the
    fields, the file and the body the phone sent."""
    _allow(phone)
    for fields, named in (
            ([('request_id', 'r1')], None),
            ([('user_id', phone.user_id), ('request_id', 'r1')], phone.user_id),
            ([('user_id', ''), ('request_id', 'r1')], ''),
            ([('user_id', phone.user_id), ('user_id', phone.user_id),
              ('request_id', 'r1')], phone.user_id)):
        r = _form(desktop, phone, fields, kind)
        assert r.status_code == 200, (fields, r.get_data(as_text=True))
        got = r.get_json()
        assert got['auth'] == 'device'
        assert got['user_id'] == named and got['request_id'] == 'r1'
        if kind == _MULTIPART:
            assert got['file'] == _JPEG.hex()   # the file as sent


def test_a_token_whose_user_is_a_number_matches_the_form_text(desktop):
    numbered = Phone(user_id=40021)
    _allow(numbered)
    assert _form(desktop, numbered, [('user_id', '40021')]).status_code == 200
    assert _form(desktop, numbered, [('user_id', _OTHER)]).status_code == 403


def test_a_name_sent_twice_in_the_query_is_two_names(desktop, phone):
    _allow(phone)
    own = phone.user_id
    assert _frame(desktop, phone, f'?user_id={own}&user_id={_OTHER}').status_code == 403
    assert _frame(desktop, phone, f'?user_id={_OTHER}&user_id={own}').status_code == 403
    assert _frame(desktop, phone, f'?user_id={own}&user_id={own}').status_code == 200
    assert _frame(desktop, phone, f'?user_id=&user_id={own}').status_code == 200


def test_a_route_that_passes_the_body_on_still_gets_it(desktop, phone):
    """The gate reads the form of every admitted call, and reading a form
    uses the body up.  A route that forwards the body as sent (Nunba's
    /api/social proxy) still gets every byte, form or not."""
    _allow(phone)

    def forwarded(body, content_type):
        r = desktop.post('/forward', environ_base=LAN, data=body,
                         content_type=content_type,
                         headers=_bearer(phone.token()))
        return bytes.fromhex(r.get_json()['raw']) if r.status_code == 200 else r.status_code

    fields = f'user_id={phone.user_id}&request_id=r1'.encode()
    assert forwarded(fields, _URLENCODED) == fields
    mark = 'xYzZy'
    upload = (f'--{mark}\r\nContent-Disposition: form-data; name="user_id"\r\n\r\n'
              f'{phone.user_id}\r\n--{mark}\r\nContent-Disposition: form-data; '
              f'name="file"; filename="f.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'
              ).encode() + _JPEG + f'\r\n--{mark}--\r\n'.encode()
    assert forwarded(upload, f'{_MULTIPART}; boundary={mark}') == upload
    # ... and the same bytes naming another user never reach it
    assert forwarded(upload.replace(phone.user_id.encode(), _OTHER.encode()),
                     f'{_MULTIPART}; boundary={mark}') == 403
    assert forwarded(_JPEG, 'image/jpeg') == _JPEG


@pytest.mark.parametrize('kind', [_MULTIPART, _URLENCODED])
def test_this_machine_may_name_any_user_in_a_form(desktop, kind):
    """Only an admitted phone is held: this machine's own page uploads for
    the user it is showing, as before."""
    r = _form(desktop, None, [('user_id', _OTHER), ('request_id', 'r1')], kind,
              env=LOOPBACK)
    assert r.status_code == 200
    assert r.get_json()['auth'] != 'device' and r.get_json()['user_id'] == _OTHER


@pytest.mark.parametrize('no_user', [None, ''])
@pytest.mark.parametrize('name', ['None', 'none', 'null', '0'])
def test_a_token_naming_no_user_matches_no_name_at_all(desktop, no_user, name):
    """A token with no user was compared as the text "None", so a call that
    named the user "None" was let in as that user (review of 1b04d59ec).
    Whatever such a token's call names, it is refused, in every place a
    name is read."""
    nobody = Phone(user_id=no_user)
    _allow(nobody)
    assert _frame(desktop, nobody, f'?user_id={name}').status_code == 403
    assert _frame(desktop, nobody, headers={'X-User-Id': name}).status_code == 403
    for kind in (_MULTIPART, _URLENCODED):
        assert _form(desktop, nobody, [('user_id', name)], kind).status_code == 403
    r = desktop.post('/chat', environ_base=LAN, headers=_bearer(nobody.token()),
                     json={'user_id': name, 'text': 'hi'})
    assert r.status_code == 403
    assert r.get_json() == {'error': "user_id must be the token's user"}


@pytest.mark.parametrize('no_user', [None, ''])
def test_a_token_naming_no_user_has_no_json_user_to_carry(desktop, no_user):
    nobody = Phone(user_id=no_user)
    _allow(nobody)
    for body in ({'user_id': None, 'text': 'hi'}, {'user_id': '', 'text': 'hi'},
                 {'text': 'hi'}):
        r = desktop.post('/chat', environ_base=LAN, json=body,
                         headers=_bearer(nobody.token()))
        assert r.status_code == 403, body


@pytest.mark.parametrize('no_user', [None, ''])
def test_a_token_naming_no_user_is_admitted_when_its_call_names_none(desktop, no_user):
    """Kept: a route that acts for a person then finds no user on the token
    and says so itself."""
    nobody = Phone(user_id=no_user)
    _allow(nobody)
    assert _frame(desktop, nobody).status_code == 200
    assert _frame(desktop, nobody, '?channel=camera').status_code == 200
    assert _frame(desktop, nobody, '?user_id=').status_code == 200
    assert desktop.get('/chat', environ_base=LAN,
                       headers=_bearer(nobody.token())).status_code == 200
    for kind in (_MULTIPART, _URLENCODED):
        r = _form(desktop, nobody, [('request_id', 'r1')], kind)
        assert r.status_code == 200 and r.get_json()['auth'] == 'device'


def test_a_user_whose_id_reads_none_is_still_a_user(desktop):
    """Only a token with no user is held as nobody: a token that names a
    user, whatever its text, still matches that text and nothing else."""
    for user in ('None', 0, '0'):
        named = Phone(user_id=user)
        _allow(named)
        assert _frame(desktop, named, f'?user_id={user}').status_code == 200
        assert _frame(desktop, named, f'?user_id={_OTHER}').status_code == 403
        r = desktop.post('/chat', environ_base=LAN, headers=_bearer(named.token()),
                         json={'user_id': user, 'text': 'hi'})
        assert r.status_code == 200


def test_the_body_rule_reads_as_before(desktop, phone):
    _allow(phone)
    r = desktop.post('/chat', environ_base=LAN, headers=_bearer(phone.token()),
                     json={'user_id': '10077', 'text': 'hi'})
    assert r.status_code == 403
    assert r.get_json() == {'error': "user_id must be the token's user"}


def test_this_machine_may_name_any_user_beside_the_body(desktop):
    """Only an admitted phone is held: this machine's own page names the
    user it is showing, as before."""
    r = desktop.post('/chat?user_id=10077', environ_base=LOOPBACK, data=_JPEG,
                     content_type='image/jpeg', headers={'X-User-Id': '20404'})
    assert r.status_code == 200
    assert r.get_json()['auth'] != 'device'


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
