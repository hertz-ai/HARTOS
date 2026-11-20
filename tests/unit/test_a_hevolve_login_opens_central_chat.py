"""Central /chat admits the phone's Hevolve login token, checked once.

Measured 2026-11-19/20 on central: the phone sends every agent turn with the
token its Hevolve OTP login returned, an opaque Kong OAuth2 token.  Kong does
not check it on any route that reaches HARTOS (17 routes, no auth plugin), so
HARTOS's own gate is the only check, and that gate accepted nothing but a
HARTOS JWT: the phone's turn got 401 "Invalid or expired Bearer token".

link-hevolve already proves who such a token belongs to by asking Kong
(kong_identity.email_for_token) and finds or creates that person's account.
The gate now asks the same question through the same helper
(auth.user_for_hevolve_token), and /chat runs the turn as the account the
token proves, never as the body's user_id.  No header is trusted.

These drive the REAL /chat handler behind the real gate, central tier.
"""
import logging
import os
import sys
import tempfile

os.environ['HEVOLVE_DB_PATH'] = ':memory:'
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from flask import g  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.social import kong_identity  # noqa: E402
from integrations.social.auth import generate_jwt  # noqa: E402
from integrations.social.models import Base, User, db_session, get_engine  # noqa: E402

PHONE = {'REMOTE_ADDR': '172.21.0.2'}   # Kong's address on central
OWNER = 'owner@example.com'
TOKEN = 'kong-opaque-token'


class _Stop(Exception):
    """Raised once the turn has resolved its user: the gate is behind us."""


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry  # noqa: TID251 -- the route under test is on its app
    yield hart_intelligence_entry


@pytest.fixture(autouse=True)
def _central_env(monkeypatch):
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    for name in ('NUNBA_BUNDLED', 'HEVOLVE_API_KEY', 'HEVOLVE_REQUIRE_AUTH',
                 'TRUSTED_PROXY', 'NUNBA_CI', 'HEVOLVE_TRUST_KONG',
                 'HEVOLVE_CLOUD_MODE'):
        monkeypatch.delenv(name, raising=False)
    engine = get_engine()
    Base.metadata.create_all(engine)
    with db_session() as db:
        db.add(User(username='owner', display_name='Owner', email=OWNER,
                    user_type='human', api_token='t-owner'))
    yield
    Base.metadata.drop_all(engine)


def _owner_id():
    with db_session(commit=False) as db:
        return str(db.query(User).filter(User.email == OWNER).first().id)


def _body(**kw):
    # The phone names its legacy Hevolve id in the body; the turn must not
    # run as whatever the body says.
    body = {'user_id': '10202', 'prompt_id': '9001', 'prompt': 'hi',
            'request_id': 'phone-r1'}
    body.update(kw)
    return body


def _drive(hie, monkeypatch, body, token, proves=OWNER):
    """POST /chat through the real gate and handler with Kong answering
    ``proves`` for the token; stop where the turn seeds its user."""
    seen = {}

    def stop_at_user(user_id=None, **_k):
        seen['user_id'] = user_id
        seen['auth_source'] = getattr(g, 'auth_source', None)
        raise _Stop()

    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    secrets = {'security.secrets_manager': MagicMock(
        get_secret=lambda name: os.environ.get(name, ''))}
    with patch.dict(sys.modules, secrets), \
            patch.object(kong_identity, 'email_for_token',
                         return_value=proves) as asked, \
            patch.object(hie.thread_local_data, 'set_user_id', stop_at_user), \
            patch.object(hie, '_cleanup_stale_agents', lambda: None):
        try:
            resp = hie.app.test_client().post(
                '/chat', json=body, environ_base=PHONE,
                headers={'Authorization': f'Bearer {token}'})
            seen['status'] = resp.status_code
            seen['body'] = resp.get_json()
        except _Stop:
            seen['status'] = 'resolved-user'
    seen['kong_asked'] = asked.call_count
    return seen


def test_the_phones_login_token_runs_the_turn_as_its_account(hie, monkeypatch):
    seen = _drive(hie, monkeypatch, _body(), TOKEN)
    assert seen['status'] == 'resolved-user', seen
    assert seen['user_id'] == _owner_id(), 'the turn ran as the body\'s user_id'
    assert seen['auth_source'] == 'hevolve'


def test_a_token_kong_does_not_vouch_for_is_refused(hie, monkeypatch):
    seen = _drive(hie, monkeypatch, _body(), TOKEN, proves=None)
    assert seen['status'] == 401, seen


def test_a_banned_account_is_refused(hie, monkeypatch):
    with db_session() as db:
        db.query(User).filter(User.email == OWNER).first().is_banned = True
    seen = _drive(hie, monkeypatch, _body(), TOKEN)
    assert seen['status'] == 401, seen


def test_a_proven_email_with_no_account_gets_one_as_link_hevolve_does(
        hie, monkeypatch):
    seen = _drive(hie, monkeypatch, _body(), TOKEN, proves='student@example.com')
    assert seen['status'] == 'resolved-user', seen
    with db_session(commit=False) as db:
        made = db.query(User).filter(User.email == 'student@example.com').first()
        assert made is not None
        assert seen['user_id'] == str(made.id)


def test_a_hartos_jwt_is_still_admitted_without_asking_kong(hie, monkeypatch):
    jwt = generate_jwt(_owner_id(), 'owner')
    seen = _drive(hie, monkeypatch, _body(user_id=_owner_id()), jwt)
    assert seen['status'] == 'resolved-user', seen
    assert seen['auth_source'] == 'jwt'
    assert seen['kong_asked'] == 0


def test_no_bearer_is_refused_on_central(hie, monkeypatch):
    with patch.object(hie, '_cleanup_stale_agents', lambda: None):
        resp = hie.app.test_client().post('/chat', json=_body(),
                                          environ_base=PHONE)
    assert resp.status_code == 401


def test_a_forged_kong_consumer_header_admits_nobody(hie, monkeypatch):
    """No header is an identity: the gate asks Kong about the TOKEN."""
    with patch.object(hie, '_cleanup_stale_agents', lambda: None):
        resp = hie.app.test_client().post(
            '/chat', json=_body(), environ_base=PHONE,
            headers={'X-Consumer-Custom-ID': OWNER,
                     'X-Consumer-Username': OWNER})
    assert resp.status_code == 401


def test_a_hevolve_verdict_without_its_payload_admits_nobody(hie, monkeypatch):
    monkeypatch.setattr(hie, '_cleanup_stale_agents', lambda: None)
    with hie.app.test_request_context('/chat', method='POST', json=_body(),
                                      environ_base=PHONE):
        g.auth_source = 'hevolve'
        resp = hie.chat()
    assert resp[1] == 401


def test_a_password_account_holding_the_email_is_never_the_login(hie, monkeypatch):
    """Review rv_e97b, must-fix 1 of f5e4358e2: /auth/register takes any
    well-formed email with a password and proves none of it, so a stranger can
    hold someone's email before they ever log in.  The login Kong proves for
    that email must not run as the stranger's account."""
    from integrations.social.services import UserService
    victim = 'victim@example.com'
    with db_session() as db:
        mallory = UserService.register(db, 'mallory', 'Str0ng!Passw0rd', victim)
        mallory_id = str(mallory.id)
    seen = _drive(hie, monkeypatch, _body(), TOKEN, proves=victim)
    assert seen['status'] == 401, seen
    assert seen.get('user_id') != mallory_id


def test_the_login_helper_binds_only_an_account_whose_email_kong_proved(hie):
    """The one helper link-hevolve and the gate share: a row made by
    /auth/register (a password beside a typed email) is never bound; the row
    the helper itself made for the proven email still is."""
    from integrations.social.auth import user_for_hevolve_token
    from integrations.social.services import UserService
    victim = 'victim@example.com'
    with db_session() as db:
        UserService.register(db, 'mallory', 'Str0ng!Passw0rd', victim)
    with db_session() as db, patch.object(kong_identity, 'email_for_token',
                                          return_value=victim):
        assert user_for_hevolve_token(db, TOKEN) == (None, False)
    with db_session() as db, patch.object(kong_identity, 'email_for_token',
                                          return_value=OWNER):
        user, created = user_for_hevolve_token(db, TOKEN)
        assert (str(user.id), created) == (_owner_id(), False)


def test_a_refused_jwt_never_reaches_kong(hie, monkeypatch):
    """Review hartos-52 M1 of f5e4358e2: an expired or foreign JWT went into
    Kong's admin URL (and so its log) before the 401."""
    monkeypatch.setenv('KONG_ADMIN_URL', 'http://kong-admin:8101')
    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    for kept in ('_answers', '_refused'):  # parent's store, this one's
        getattr(kong_identity, kept, {}).clear()
    secrets = {'security.secrets_manager': MagicMock(
        get_secret=lambda name: os.environ.get(name, ''))}
    foreign = 'eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiMSJ9.c2ln'
    with patch.dict(sys.modules, secrets), \
            patch.object(kong_identity.requests, 'get') as get, \
            patch.object(hie, '_cleanup_stale_agents', lambda: None):
        resp = hie.app.test_client().post(
            '/chat', json=_body(), environ_base=PHONE,
            headers={'Authorization': f'Bearer {foreign}'})
    assert resp.status_code == 401
    get.assert_not_called()


def test_a_paced_login_check_answers_busy_not_invalid(hie, monkeypatch, caplog):
    """Review rv_5local of 787964a11: when the lookup budget refused, not
    Kong, the phone was told 401 "Invalid or expired Bearer token", which
    names the wrong cause.  Nothing is known about the token then: the gate
    answers that the check is busy and when to retry, and logs no refused
    credential.  A token Kong does not vouch for is still a refusal, logged
    once, which also shows the log is being read."""
    busy = getattr(kong_identity, 'KongBusy', None)
    assert busy is not None, 'the gate has no busy answer to give'
    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    secrets = {'security.secrets_manager': MagicMock(
        get_secret=lambda name: os.environ.get(name, ''))}
    headers = {'Authorization': f'Bearer {TOKEN}'}
    with (patch.dict(sys.modules, secrets),
          patch.object(hie, '_cleanup_stale_agents', lambda: None),
          caplog.at_level(logging.WARNING)):
        with patch.object(kong_identity, 'email_for_token',
                          side_effect=busy(20)):
            paced = hie.app.test_client().post(
                '/chat', json=_body(), environ_base=PHONE, headers=headers)
        with patch.object(kong_identity, 'email_for_token', return_value=None):
            refused = hie.app.test_client().post(
                '/chat', json=_body(), environ_base=PHONE, headers=headers)
    assert paced.status_code == 503, (paced.status_code, paced.get_json())
    assert paced.headers.get('Retry-After') == '20'
    # Installed apps show `error` as it is (DiscoverableTogglePanel renders
    # "Failed: <error>"), so it is a sentence with the wait in it; the
    # machine code is its own field.
    body = paced.get_json()
    assert body['code'] == 'login_check_busy'
    assert body['error'] != body['code']
    assert ' ' in body['error'] and '20 seconds' in body['error']
    assert body['error'] == busy(20).message  # the one sentence (KongBusy)
    assert refused.status_code == 401
    lines = [r.getMessage() for r in caplog.records
             if 'Refused the credential' in r.getMessage()]
    assert len(lines) == 1, lines
