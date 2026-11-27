"""The phone reaches its own desktop's consent door over the device link.

Owner, 22 Nov 2026: the phone may send a password over the internet, "only
to the node that asked", and never store-and-forward through central.

A desktop files a credential ask; the phone's overlay answers with POST
https://azurekong.hertzai.com/api/social/consent (and /consent/decline), the
call it already makes.  Until now no row in the mobile adapter covered those
calls, so the desktop answered ``not_here`` and the only place the phone
could send a typed password was the cloud, which does not hold the desktop's
ask.  Now three rows map the answer, the no, and the read of one ask (GET
/api/social/consent?request_id=) to the desktop's own consent route, and the
handshake names them.

Real: the handshake, the receive loop, the API gate (security.middleware),
the adapter, the consent blueprint and its auth (social.auth.require_auth),
the vault (in memory).  Fake values only; nothing leaves the process.
"""
import os
import sys
import types

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.peer_link.link import PeerLink  # noqa: E402
from integrations.mobile_adapter import adapter, aliases  # noqa: E402
from integrations.social.models import Base, UserConsent, db_session, get_engine  # noqa: E402
from security.middleware import _apply_api_auth  # noqa: E402
from tests.unit.module_swap import swap_modules  # noqa: E402
from tests.unit.test_mobile_adapter import (  # noqa: E402
    PHONE_USER, _api, _ask, _body)
from tests.unit.test_peer_link_device_links import (  # noqa: E402,F401
    _accept, _desktop, _hello, _install_real_verifier, phone)

CLOUD = aliases.CLOUD
CONSENT = f'https://{CLOUD}/api/social/consent'
SECRET = 'fake-Tr0ub4dor&3-horse'
ASK = '{"key_name": "site_password", "label": "Site password", ' \
      '"used_by": "the login step", "description": "Needed to sign in."}'
NAME = 'SITE_PASSWORD'
SCOPE = 'secret:' + NAME
NODE = 'bbbb9d3e55f0a7c2'
ROWS = [f'POST {CLOUD} /api/social/consent 15',
        f'POST {CLOUD} /api/social/consent/decline 15',
        f'GET {CLOUD} /api/social/consent 15']


@pytest.fixture
def desktop(monkeypatch, _desktop):  # noqa: F811
    """A desktop whose owner is the phone's user: the consent blueprint
    behind the real API gate, the adapter installed, an ask on file."""
    from hartos import ai_key_vault
    from integrations.social import consent_api, consent_service
    from security import key_delegation, node_integrity
    from security.secrets_manager import SecretsManager
    monkeypatch.setenv('NUNBA_BUNDLED', '1')
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', PHONE_USER)
    for name in ('HEVOLVE_API_KEY', 'TRUSTED_PROXY', 'NUNBA_CI',
                 'HEVOLVE_MASTER_KEY', NAME):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(node_integrity, 'get_public_key_hex',
                        lambda: NODE + 'cd' * 24)
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'flat')
    Base.metadata.create_all(get_engine())
    SecretsManager.reset()
    ai_key_vault.AIKeyVault.reset()
    monkeypatch.setattr(ai_key_vault, '_instance', None)
    emitted = []
    monkeypatch.setattr(consent_service, '_emit',
                        lambda topic, data, msg_id=None:
                        emitted.append((topic, dict(data), msg_id)))
    ai_key_vault.request_credential(ASK, agent_id='42')
    [(_, _, msg_id)] = [e for e in emitted if e[0] == 'consent.request']
    app = Flask('nunba')
    app.register_blueprint(consent_api.consent_bp)
    _apply_api_auth(app)
    yield types.SimpleNamespace(app=app, ask=msg_id.split(':', 1)[1])
    adapter.set_host_app(None)
    ai_key_vault.AIKeyVault.reset()
    SecretsManager.reset()
    os.environ.pop(NAME, None)


def _no_api_key():
    """The real secrets module with no API key set for the gate to read (the
    other device-link files swap in a stub with get_secret only; the vault
    under test here needs the rest of the module)."""
    import security.secrets_manager as real
    fake = types.SimpleNamespace(**{k: v for k, v in vars(real).items()
                                    if not k.startswith('__')})
    fake.get_secret = lambda name: ''
    return fake


def _installed(desktop):
    return swap_modules({'security.secrets_manager': _no_api_key()})


def _owners_link(phone):
    """The device link of a phone this node's owner allowed.  The borrowed
    ``_device_link`` allows the phone for the other file's owner; the node
    here has its own.  Allowed as the account its token names (#306)."""
    from tests.unit.test_device_access_gate import pair
    pair(phone, os.environ['HEVOLVE_OWNER_USER_ID'])
    _install_real_verifier()
    return _accept(_hello(phone, phone.token()))


def _open(rid):
    from integrations.social.consent_service import open_ask
    with db_session() as db:
        return db.query(UserConsent).filter(
            UserConsent.id == rid, open_ask()).count() == 1


def _value():
    from hartos.ai_key_vault import get_ai_key_vault
    return get_ai_key_vault().get_tool_key(NAME) or ''


def test_the_three_consent_calls_resolve_to_the_desktops_own_route():
    assert aliases.resolve('POST', CONSENT) == '/api/social/consent'
    assert aliases.resolve('POST', CONSENT + '/decline') == '/api/social/consent/decline'
    assert aliases.resolve('GET', CONSENT + '?request_id=row-1') == \
        '/api/social/consent?request_id=row-1'
    # and nothing beside them
    for method, url in (('POST', CONSENT + '/revoke'), ('POST', CONSENT + '/reopen'),
                        ('DELETE', CONSENT), ('PUT', CONSENT),
                        ('GET', CONSENT + '/decline'),
                        ('POST', 'https://elsewhere.example/api/social/consent')):
        assert aliases.resolve(method, url) is None, (method, url)


def test_the_handshake_names_the_consent_rows(desktop):
    with _installed(desktop):
        assert adapter.install(desktop.app)
        paths = PeerLink._get_local_capabilities()['mobile_paths']
    assert paths == ROWS, 'only the routes this app serves, the consent three'
    assert [r for r in aliases.served() if '/api/social/consent' in r] == ROWS


def test_the_rows_that_were_there_are_still_there():
    served = aliases.served()
    for row in (f'POST {CLOUD} /chat/teachme2 150', f'POST {CLOUD} /chat/custom_gpt 150',
                f'POST {CLOUD} /api/agent/approval 15', f'POST {CLOUD} /api/vision/frame 5',
                # 6a1f8c006: PersonAsk.read, the ask a tapped Firebase banner names
                f'GET {CLOUD} /api/agent/approval 15'):
        assert row in served
    assert len(served) == 8


def test_a_desktop_without_the_consent_route_claims_none_of_them():
    app = Flask('bare')
    assert [r for r in aliases.on_host(app) if 'consent' in r.path] == []


def test_the_phones_answer_reaches_the_real_consent_door(desktop, phone, monkeypatch):  # noqa: F811
    """The device link carries the call into the desktop's own route, through
    the gate, as a remote caller.  The door's own sign-in rule then decides:
    it knows an account token, and a phone's device token is not one yet
    (design step H4, built after the device-token user check lands), so the
    answer is its 401 and nothing is stored.  ``not_here`` would have sent
    the password to the cloud; this never does."""
    from integrations.social import auth
    asked = []
    real = auth._get_user_from_token
    monkeypatch.setattr(auth, '_get_user_from_token',
                        lambda token: asked.append(token) or real(token))
    with _installed(desktop):
        assert adapter.install(desktop.app)
        link = _owners_link(phone)
        token = phone.token()
        replies = _ask(link, _api(phone, CONSENT, token=token, body={
            'consent_type': 'credential', 'request_id': desktop.ask,
            'scope': SCOPE, 'node_id': NODE, 'user_id': PHONE_USER,
            'value': SECRET}))
    assert len(replies) == 1
    reply = replies[0]
    assert reply['type'] == 'api_reply', 'claimed: never not_here'
    assert asked == [token], "the consent route's own sign-in was asked"
    assert reply.get('status') == 401
    assert SECRET not in str(_body(reply))
    assert _value() == '' and _open(desktop.ask)


def test_with_the_owner_known_the_answer_over_the_link_is_stored_on_the_desktop(
        desktop, phone, monkeypatch):  # noqa: F811
    """The same call once the door knows the caller as the owner (stood in
    for here; H4 will make the door take the owner's allowed phone): the
    real route binds the answer to its ask and this node, stores the value
    in this desktop's vault, and closes the ask."""
    from integrations.social import auth
    from integrations.social.models import get_db
    monkeypatch.setattr(auth, '_get_user_from_token', lambda token: (
        types.SimpleNamespace(id=PHONE_USER, is_admin=False, is_moderator=False),
        get_db()))
    with _installed(desktop):
        assert adapter.install(desktop.app)
        link = _owners_link(phone)
        read = _ask(link, _api(phone, CONSENT + '?request_id=' + desktop.ask,
                               method='GET'))[0]
        assert read.get('status') == 200
        [row] = _body(read)['data']['consents']
        assert (row['id'], row['scope'], row['node_id']) == (desktop.ask, SCOPE, NODE)
        wrong = _ask(link, _api(phone, CONSENT, body={
            'request_id': desktop.ask, 'scope': row['scope'],
            'node_id': 'aaaa1c0277deb4e1', 'user_id': PHONE_USER,
            'value': SECRET}), rid='a-2')[0]
        assert wrong.get('status') == 404 and _value() == ''
        reply = _ask(link, _api(phone, CONSENT, body={
            'consent_type': 'credential', 'request_id': desktop.ask,
            'scope': row['scope'], 'node_id': row['node_id'],
            'user_id': PHONE_USER, 'value': SECRET}), rid='a-3')[0]
    assert reply.get('status') == 201, reply
    assert SECRET not in str(_body(reply))
    assert _value() == SECRET and not _open(desktop.ask)


def test_a_no_over_the_link_runs_the_real_decline_route(desktop, phone, monkeypatch):  # noqa: F811
    from integrations.social import auth
    from integrations.social.models import get_db
    monkeypatch.setattr(auth, '_get_user_from_token', lambda token: (
        types.SimpleNamespace(id=PHONE_USER, is_admin=False, is_moderator=False),
        get_db()))
    with _installed(desktop):
        assert adapter.install(desktop.app)
        link = _owners_link(phone)
        reply = _ask(link, _api(phone, CONSENT + '/decline', body={
            'consent_type': 'credential', 'request_id': desktop.ask,
            'user_id': PHONE_USER}))[0]
    assert reply.get('status') == 200
    assert _body(reply)['data'] == {'declined': True, 'id': desktop.ask}
    assert not _open(desktop.ask) and _value() == ''


def test_an_answer_under_another_users_token_never_reaches_the_door(
        desktop, phone, monkeypatch):  # noqa: F811
    """/api/social/ is outside the API gate, so on this road the adapter's
    own rule is the first hold: a call whose token names another user than
    the link's is refused there, and the consent route is never run."""
    from integrations.social import auth
    from tests.unit.test_device_access_gate import Phone
    asked = []
    monkeypatch.setattr(auth, '_get_user_from_token',
                        lambda token: asked.append(token) or (None, None))
    other = Phone(user_id='10077', username='Someone else')
    with _installed(desktop):
        assert adapter.install(desktop.app)
        link = _owners_link(phone)
        reply = _ask(link, _api(phone, CONSENT, token=other.token(), body={
            'consent_type': 'credential', 'request_id': desktop.ask,
            'scope': SCOPE, 'node_id': NODE, 'user_id': '10077',
            'value': SECRET}))[0]
    assert reply.get('status') == 403, reply
    assert asked == [], 'the route was never run'
    assert SECRET not in str(_body(reply))
    assert _value() == '' and _open(desktop.ask)


def test_the_typed_value_is_in_no_log_line_on_the_device_link_path(
        desktop, phone, monkeypatch, caplog):  # noqa: F811
    import logging
    from integrations.social import auth
    from integrations.social.models import get_db
    monkeypatch.setattr(auth, '_get_user_from_token', lambda token: (
        types.SimpleNamespace(id=PHONE_USER, is_admin=False, is_moderator=False),
        get_db()))
    with caplog.at_level(logging.DEBUG), _installed(desktop):
        assert adapter.install(desktop.app)
        link = _owners_link(phone)
        for node in ('aaaa1c0277deb4e1', NODE):
            _ask(link, _api(phone, CONSENT, body={
                'request_id': desktop.ask, 'scope': SCOPE, 'node_id': node,
                'user_id': PHONE_USER, 'value': SECRET}), rid='a-' + node[:4])
    assert _value() == SECRET
    assert SECRET not in caplog.text
    assert caplog.records, 'the path did log (shape only)'
