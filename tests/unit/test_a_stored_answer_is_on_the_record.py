"""A password stored by a consent answer is on the record at the node that
stored it: who answered, by which road, from which phone, for which ask.

Owner, 22 Nov 2026: "Ask both, store where asked".  The same ask can now be
answered from the desktop's page, from the owner's phone over the device
link, or with an account token from another machine.  The vault's one audit
event for a store (``credential_stored``) said only the secret's name, so
the record could not tell which of those wrote it.

Now consent_api hands AIKeyVault.store_credential who answered and how
(``answered``), and the event carries it as its detail: answered_by, leg
('local_page', 'phone_device_link', 'cloud'), device (the phone's key
fingerprint, on the device leg only), node_id, ask.  One event per store,
written where it always was, through the audit log's own redaction
(security.immutable_audit_log._redact_sensitive).  Never the value, its
length or a hash of it.

Real: request_credential, ConsentService, the vault (in memory), the consent
blueprint, the API gate and the mobile adapter on the device leg, and the
immutable audit log on this test's database, read back through its own
reader (get_trail).  The signed-in user is set by the test.  Fake values
only; nothing leaves the process.
"""
import hashlib
import json
import logging
import os
import sys
import types

os.environ['HEVOLVE_DB_PATH'] = ':memory:'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

from integrations.mobile_adapter import adapter, aliases  # noqa: E402
from integrations.social.models import Base, db_session, get_engine  # noqa: E402
from security.middleware import _apply_api_auth  # noqa: E402
from tests.unit.module_swap import swap_modules  # noqa: E402
from tests.unit.test_mobile_adapter import (  # noqa: E402
    PHONE_USER, _api, _ask, _body)
from tests.unit.test_peer_link_device_links import (  # noqa: E402,F401
    _accept, _desktop, _hello, _install_real_verifier, phone)

OWNER = PHONE_USER
SECRET = 'fake-Tr0ub4dor&3-horse'
ASK = '{"key_name": "site_password", "label": "Site password", ' \
      '"used_by": "the login step", "description": "Needed to sign in."}'
NAME = 'SITE_PASSWORD'
SCOPE = 'secret:' + NAME
NODE = 'bbbb9d3e55f0a7c2'
CONSENT = f'https://{aliases.CLOUD}/api/social/consent'


@pytest.fixture(autouse=True)
def node(monkeypatch, _desktop):  # noqa: F811
    """A node whose owner is OWNER: its own key, an empty vault, an empty
    database and an audit log of its own on that database."""
    from hartos import ai_key_vault
    from integrations.social import auth, consent_service
    from integrations.social.models import get_db
    from security import immutable_audit_log, key_delegation, node_integrity
    from security.secrets_manager import SecretsManager
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    monkeypatch.setenv('NUNBA_BUNDLED', '1')
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    for name in ('HEVOLVE_API_KEY', 'TRUSTED_PROXY', 'NUNBA_CI',
                 'HEVOLVE_MASTER_KEY', NAME):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(node_integrity, 'get_public_key_hex',
                        lambda: NODE + 'cd' * 24)
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'flat')
    SecretsManager.reset()
    ai_key_vault.AIKeyVault.reset()
    monkeypatch.setattr(ai_key_vault, '_instance', None)
    log = immutable_audit_log.ImmutableAuditLog()
    assert log._use_db, 'the log under test is the database one'
    monkeypatch.setattr(immutable_audit_log, '_audit_log', log)
    emitted = []
    monkeypatch.setattr(consent_service, '_emit',
                        lambda topic, data, msg_id=None:
                        emitted.append((topic, dict(data), msg_id)))
    # The door knows the caller as the owner (the account-token rule is
    # auth.py's own; on the device leg H4 will make it the allowed phone).
    monkeypatch.setattr(auth, '_get_user_from_token', lambda token: (
        types.SimpleNamespace(id=OWNER, is_admin=False, is_moderator=False),
        get_db()))
    yield types.SimpleNamespace(emitted=emitted, log=log)
    adapter.set_host_app(None)
    ai_key_vault.AIKeyVault.reset()
    SecretsManager.reset()
    os.environ.pop(NAME, None)
    Base.metadata.drop_all(engine)


def _owners_link(phone):
    """The device link of a phone this node's owner allowed.  The borrowed
    ``_device_link`` allows the phone for the other file's owner; the node
    here has its own.  Allowed as the account its token names (#306)."""
    from tests.unit.test_device_access_gate import pair
    pair(phone, os.environ['HEVOLVE_OWNER_USER_ID'])
    _install_real_verifier()
    return _accept(_hello(phone, phone.token()))


def _no_api_key():
    """The real secrets module with no API key set for the gate to read (the
    other device-link files swap in a stub with get_secret only; the vault
    under test here needs the rest of the module)."""
    import security.secrets_manager as real
    fake = types.SimpleNamespace(**{k: v for k, v in vars(real).items()
                                    if not k.startswith('__')})
    fake.get_secret = lambda name: ''
    return fake


def _file_ask(node, rejected=False):
    from hartos.ai_key_vault import request_credential
    node.emitted.clear()
    request_credential(ASK[:-1] + ', "rejected": true}' if rejected else ASK,
                       agent_id='42')
    [(_, _, msg_id)] = [e for e in node.emitted if e[0] == 'consent.request']
    return msg_id.split(':', 1)[1]


def _door(gated=False):
    from integrations.social import consent_api
    app = Flask('nunba')
    app.register_blueprint(consent_api.consent_bp)
    if gated:
        _apply_api_auth(app)
    return app


def _answer(app, rid, remote_addr, value=SECRET, node_id=NODE):
    return app.test_client().post(
        '/api/social/consent',
        json={'consent_type': 'credential', 'request_id': rid, 'scope': SCOPE,
              'node_id': node_id, 'user_id': OWNER, 'value': value},
        headers={'Authorization': 'Bearer account-token'},
        environ_base={'REMOTE_ADDR': remote_addr})


def _stored_events(node):
    """Every credential_stored entry, read back from the log itself."""
    return node.log.get_trail(limit=50, event_type='credential_stored')


def _detail(entry):
    return json.loads(entry['detail_json'] or '{}')


def _holds_nothing_of_the_value(entry):
    text = json.dumps(entry, default=str)
    assert SECRET not in text
    for algo in ('md5', 'sha1', 'sha256', 'sha512', 'blake2b'):
        digest = hashlib.new(algo, SECRET.encode()).hexdigest()
        assert digest not in text and digest[:16] not in text, algo
    detail = _detail(entry)
    assert len(SECRET) not in detail.values()
    assert str(len(SECRET)) not in [str(v) for v in detail.values()]
    assert not [k for k in detail if any(w in k.lower() for w in (
        'value', 'len', 'hash', 'digest', 'password', 'secret'))]


# ── each leg ──────────────────────────────────────────────────────────

def test_an_answer_from_this_machines_page_is_recorded_as_local(node):
    rid = _file_ask(node)
    assert _answer(_door(), rid, '127.0.0.1').status_code == 201
    [entry] = _stored_events(node)
    assert entry['action'] == 'stored credential ' + NAME
    assert entry['actor_id'] == 'ai_key_vault'
    assert _detail(entry) == {'answered_by': OWNER, 'leg': 'local_page',
                              'node_id': NODE, 'ask': rid}
    _holds_nothing_of_the_value(entry)


def test_an_answer_with_an_account_token_from_another_machine_is_recorded_as_cloud(node):
    rid = _file_ask(node)
    assert _answer(_door(), rid, '203.0.113.9').status_code == 201
    [entry] = _stored_events(node)
    assert _detail(entry) == {'answered_by': OWNER, 'leg': 'cloud',
                              'node_id': NODE, 'ask': rid}
    _holds_nothing_of_the_value(entry)


def test_an_answer_over_the_device_link_names_the_phone(node, phone):  # noqa: F811
    """The real adapter and gate: the call comes in as a remote caller from
    'peerlink-device/<peer>', marked auth_source 'device' with the phone's
    key, and the record carries that key's fingerprint."""
    from integrations.social.consent_service import device_fingerprint
    rid = _file_ask(node)
    with swap_modules({'security.secrets_manager': _no_api_key()}):
        assert adapter.install(_door(gated=True))
        link = _owners_link(phone)
        reply = _ask(link, _api(phone, CONSENT, body={
            'consent_type': 'credential', 'request_id': rid, 'scope': SCOPE,
            'node_id': NODE, 'user_id': OWNER, 'value': SECRET}))[0]
    assert reply['status'] == 201, _body(reply)
    [entry] = _stored_events(node)
    fingerprint = device_fingerprint(phone.public_hex)
    assert fingerprint and len(fingerprint.replace(' ', '')) == 16
    assert _detail(entry) == {'answered_by': OWNER, 'leg': 'phone_device_link',
                              'device': fingerprint, 'node_id': NODE, 'ask': rid}
    _holds_nothing_of_the_value(entry)


def _door_behind_a_gate(key):
    """The door as it stands behind a gate that admitted a phone: the two
    marks security.middleware leaves for an admitted device (g.auth_source
    'device', g.device_public_key).  /api/social/ is outside that gate
    today, so the marks are set here the way it sets them."""
    from flask import g
    app = _door()

    @app.before_request
    def mark():
        g.auth_source = 'device'
        g.device_public_key = key
    return app


def test_a_call_the_gate_marked_as_a_phone_is_the_device_leg(node):
    """The gate's mark alone, from an address that is not the adapter's."""
    from integrations.social.consent_service import device_fingerprint
    key = 'ab12' * 16
    rid = _file_ask(node)
    assert _answer(_door_behind_a_gate(key), rid, '203.0.113.9').status_code == 201
    [entry] = _stored_events(node)
    assert _detail(entry) == {'answered_by': OWNER, 'leg': 'phone_device_link',
                              'device': device_fingerprint(key),
                              'node_id': NODE, 'ask': rid}


def test_the_key_the_gate_verified_is_the_one_recorded(node, phone):  # noqa: F811
    """Where a gate checked this call's own token, its key is the record's;
    the link's key is for a call no gate saw."""
    from integrations.social.consent_service import device_fingerprint
    key = 'ab12' * 16
    link = _owners_link(phone)
    assert link is not None and phone.public_hex != key
    rid = _file_ask(node)
    assert _answer(_door_behind_a_gate(key), rid,
                   f'peerlink-device/{link.peer_id}').status_code == 201
    [entry] = _stored_events(node)
    assert _detail(entry).get('device') == device_fingerprint(key)


def test_the_adapters_address_alone_marks_the_device_leg(node):
    """A call the mobile adapter carried is the device leg even where no
    gate marked it (Nunba's in-process hop)."""
    rid = _file_ask(node)
    assert _answer(_door(), rid, 'peerlink-device/peer-1').status_code == 201
    detail = _detail(_stored_events(node)[0])
    assert detail.get('leg') == 'phone_device_link'
    assert detail['device'] is None, 'no key was verified, so none is named'
    assert set(detail) == {'answered_by', 'leg', 'device', 'node_id', 'ask'}


# ── one event per store, and none without one ─────────────────────────

def test_the_detail_goes_through_the_logs_own_redaction(node, monkeypatch):
    """ImmutableAuditLog.log_event redacts sensitive keys (_redact_sensitive)
    before it stores or hashes: the record passes through it, and none of
    its five keys is one it withholds."""
    from security import immutable_audit_log
    seen = []
    real = immutable_audit_log._redact_sensitive

    def redact(detail):
        seen.append(detail)
        return real(detail)
    monkeypatch.setattr(immutable_audit_log, '_redact_sensitive', redact)
    rid = _file_ask(node)
    assert _answer(_door(), rid, '127.0.0.1').status_code == 201
    [entry] = _stored_events(node)
    given = [d for d in seen if d and 'leg' in d]
    assert len(given) == 1, 'the detail went through the redaction once'
    assert entry['detail_json'] == real(given[0])
    assert '[REDACTED]' not in entry['detail_json']
    assert real({'answered_by': 1, 'leg': 2, 'device': 3, 'node_id': 4,
                 'ask': 5, 'password': SECRET}) == json.dumps(
        {'answered_by': 1, 'leg': 2, 'device': 3, 'node_id': 4, 'ask': 5,
         'password': '[REDACTED]'}, sort_keys=True)


def test_one_event_per_store(node):
    """Asked again after the site refused the value, answered again: two
    stores, two events, each naming its own answer."""
    first = _file_ask(node)
    assert _answer(_door(), first, '127.0.0.1').status_code == 201
    second = _file_ask(node, rejected=True)
    assert _answer(_door(), second, '203.0.113.9',
                   value='fake-second-value').status_code == 201
    events = _stored_events(node)
    assert [(_detail(e).get('ask'), _detail(e).get('leg')) for e in events] == [
        (first, 'local_page'), (second, 'cloud')]
    for entry in events:
        _holds_nothing_of_the_value(entry)
        assert 'fake-second-value' not in json.dumps(entry, default=str)


def test_a_refused_answer_writes_no_event(node):
    rid = _file_ask(node)
    app = _door()
    assert _answer(app, rid, '127.0.0.1', node_id='aaaa1c0277deb4e1').status_code == 404
    assert _answer(app, rid, '127.0.0.1', value=7).status_code == 400
    assert _answer(app, rid, '127.0.0.1', value='  ').status_code == 400
    assert _stored_events(node) == []


def test_a_second_answer_to_the_same_ask_writes_no_second_event(node):
    rid = _file_ask(node)
    app = _door()
    assert _answer(app, rid, '127.0.0.1').status_code == 201
    assert _answer(app, rid, '127.0.0.1').status_code == 404
    assert len(_stored_events(node)) == 1


def test_a_store_that_is_no_answer_keeps_its_event_as_it_was(node):
    """/api/credentials/submit and every other caller of store_credential:
    the same event with no detail."""
    from hartos.ai_key_vault import get_ai_key_vault
    get_ai_key_vault().store_credential('site_password', SECRET)
    [entry] = _stored_events(node)
    assert entry['action'] == 'stored credential ' + NAME
    assert entry['detail_json'] is None
    _holds_nothing_of_the_value({k: v for k, v in entry.items()
                                 if k != 'detail_json'} | {'detail_json': '{}'})


def test_the_chain_still_verifies(node):
    rid = _file_ask(node)
    assert _answer(_door(), rid, '127.0.0.1').status_code == 201
    ok, why = node.log.verify_chain()
    assert ok, why


def test_the_value_is_in_no_log_line(node, caplog):
    rid = _file_ask(node)
    with caplog.at_level(logging.DEBUG):
        assert _answer(_door(), rid, '203.0.113.9').status_code == 201
    assert SECRET not in caplog.text
