"""/chat honours the desktop gate's verdict on a phone the owner allowed (#111).

security.middleware (bundled branch) admits a phone whose token verifies
against the key in the owner's device_access grant, and marks the request
``g.auth_source == 'device'`` (tests/unit/test_device_access_gate.py).  /chat
then runs its own three-layer gate, which re-derives auth from the bearer
with decode_jwt: an HS256 check the phone's Ed25519-signed token cannot pass
(the phone has no local secret).  So the door admitted the phone and the
room refused it: with HEVOLVE_API_KEY set, 401 "Invalid or expired token";
without it, the verdict was overwritten to 'none' and then 'body', and the
call went through only because the gate had already pinned the body's
user_id.  These drive the REAL /chat handler, not a copy of its gate.
"""
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

from integrations.social.models import Base, get_engine  # noqa: E402
from tests.unit.test_device_access_gate import Phone, pair  # noqa: E402

LAN = {'REMOTE_ADDR': '192.168.0.50'}
OWNER = 'owner-1'


class _Stop(Exception):
    """Raised once the turn has resolved its user: the gate is behind us."""


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry  # noqa: TID251 -- the route under test is on its app
    yield hart_intelligence_entry


@pytest.fixture(autouse=True)
def _desktop_env(monkeypatch):
    monkeypatch.setenv('NUNBA_BUNDLED', '1')
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    for name in ('HEVOLVE_API_KEY', 'HEVOLVE_REQUIRE_AUTH', 'TRUSTED_PROXY',
                 'NUNBA_CI'):
        monkeypatch.delenv(name, raising=False)
    engine = get_engine()
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def phone():
    """A phone the owner allowed as the account its token names (#306)."""
    phone = Phone()
    pair(phone, OWNER)
    return phone


def _body(phone, **kw):
    body = {'user_id': phone.user_id, 'prompt_id': '9001', 'prompt': 'hi',
            'request_id': 'device-r1'}
    body.update(kw)
    return body


def _drive(hie, monkeypatch, body, headers):
    """POST /chat through the real handler from the LAN; stop the turn where
    it seeds the thread-local user and record what the gate resolved."""
    seen = {}

    def stop_at_user(user_id=None, **_k):
        seen['user_id'] = user_id
        seen['auth_source'] = getattr(g, 'auth_source', None)
        seen['token_scope'] = getattr(g, 'token_scope', None)
        raise _Stop()

    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    secrets = {'security.secrets_manager': MagicMock(
        get_secret=lambda name: os.environ.get(name, ''))}
    with patch.dict(sys.modules, secrets), \
            patch.object(hie.thread_local_data, 'set_user_id', stop_at_user), \
            patch.object(hie, '_cleanup_stale_agents', lambda: None):
        try:
            resp = hie.app.test_client().post(
                '/chat', json=body, headers=headers, environ_base=LAN)
            seen['status'] = resp.status_code
            seen['body'] = resp.get_json()
        except _Stop:
            seen['status'] = 'resolved-user'
    return seen


def _bearer(token):
    return {'Authorization': f'Bearer {token}'}


def test_an_allowed_phone_reaches_chat_as_the_tokens_user(hie, phone, monkeypatch):
    seen = _drive(hie, monkeypatch, _body(phone), _bearer(phone.token()))
    assert seen['status'] == 'resolved-user', seen
    assert seen['user_id'] == phone.user_id
    assert seen['auth_source'] == 'device', 'the gate\'s verdict was overwritten'
    assert seen['token_scope'] == 'hive'


def test_an_allowed_phone_is_admitted_with_an_api_key_configured(
        hie, phone, monkeypatch):
    """A hardened desktop (HEVOLVE_API_KEY set) refused the phone with
    'Invalid or expired token' after the gate had admitted it."""
    monkeypatch.setenv('HEVOLVE_API_KEY', 'desktop-key-0123456789')
    seen = _drive(hie, monkeypatch, _body(phone), _bearer(phone.token()))
    assert seen['status'] == 'resolved-user', seen
    assert seen['user_id'] == phone.user_id
    assert seen['auth_source'] == 'device'


def test_the_phone_cannot_chat_as_another_user(hie, phone, monkeypatch):
    seen = _drive(hie, monkeypatch, _body(phone, user_id='someone-else'),
                  _bearer(phone.token()))
    assert seen['status'] == 403, seen
    assert 'user_id' in seen['body']['error']


def test_a_phone_the_owner_has_not_allowed_waits_on_the_gate(hie, monkeypatch):
    stranger = Phone(user_id='40099', username='Guest')
    seen = _drive(hie, monkeypatch, _body(stranger), _bearer(stranger.token()))
    assert seen['status'] == 403, seen
    assert seen['body']['error'] == 'consent_pending'


def test_a_device_verdict_without_its_payload_admits_nobody(hie, phone, monkeypatch):
    """Only the gate sets g.auth_source = 'device', together with the
    verified payload; should the verdict ever arrive without it, the turn
    is refused rather than run as the body's user."""
    monkeypatch.setattr(hie, '_cleanup_stale_agents', lambda: None)
    with hie.app.test_request_context('/chat', method='POST', json=_body(phone),
                                      environ_base=LAN):
        g.auth_source = 'device'
        resp = hie.chat()
    assert resp[1] == 401
    assert resp[0].get_json()['error'] == 'Invalid or expired token.'


def test_a_garbage_bearer_is_refused_as_before(hie, phone, monkeypatch):
    monkeypatch.setenv('HEVOLVE_API_KEY', 'desktop-key-0123456789')
    seen = _drive(hie, monkeypatch, _body(phone), _bearer('not-a-token'))
    assert seen['status'] == 401, seen


# ── the phone is its own account (#306) ────────────────────────────────────

def _same_phone_as(phone, user_id):
    """The allowed phone, same key, minting its token for another account."""
    other = Phone(user_id=user_id, username=phone.username)
    other.key, other.public_hex = phone.key, phone.public_hex
    return other


def test_the_phone_cannot_chat_as_the_owner_by_minting_for_them(hie, phone, monkeypatch):
    """The phone writes its token's user_id itself; it was allowed as its
    own account, so a token it mints for the owner is refused at the gate,
    body and token agreeing or not."""
    as_owner = _same_phone_as(phone, OWNER)
    seen = _drive(hie, monkeypatch, _body(as_owner), _bearer(as_owner.token()))
    assert seen['status'] == 403, seen
    assert seen['body']['error'] == 'not_paired_account'


def _get(hie, path, token):
    secrets = {'security.secrets_manager': MagicMock(
        get_secret=lambda name: os.environ.get(name, ''))}
    with patch.dict(sys.modules, secrets):
        return hie.app.test_client().get(path, headers=_bearer(token),
                                         environ_base=LAN)


def test_the_phones_account_does_not_see_the_owners_private_agent(
        hie, phone, monkeypatch, tmp_path):
    """Owner 2026-11-23: a phone allowed on this desktop is a trusted user
    of its compute as its own account.  The agent list (/prompts, whose
    rule is the agent's creator) shows that account its own agents and
    never the owner's, under any account the phone mints for."""
    import json
    for pid, creator, name in (('7001', OWNER, "the owner's diary"),
                               ('7002', phone.user_id, 'my tutor')):
        (tmp_path / f'{pid}.json').write_text(json.dumps({
            'name': name, 'creator_user_id': creator, 'goal': name,
            'flows': [], 'status': 'completed'}), encoding='utf-8')
    monkeypatch.setattr(hie, 'PROMPTS_DIR', str(tmp_path))
    monkeypatch.setattr(hie, 'CENTRAL_DB_URL', '')

    mine = _get(hie, f'/prompts?user_id={phone.user_id}', phone.token())
    assert mine.status_code == 200, mine.get_data(as_text=True)
    assert [p['prompt_id'] for p in mine.get_json()] == ['7002']
    theirs = _get(hie, f'/prompts?user_id={OWNER}', phone.token())
    assert theirs.status_code == 403
    as_owner = _same_phone_as(phone, OWNER)
    forged = _get(hie, f'/prompts?user_id={OWNER}', as_owner.token())
    assert forged.status_code == 403, forged.get_data(as_text=True)
    assert forged.get_json()['error'] == 'not_paired_account'
