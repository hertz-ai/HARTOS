"""The owner answers a credential ask on the phone, and the value reaches the
node that asked.

Owner, 21 Nov 2026: "Why hartos is not asking me on android via central /
The registration@mcgroce.com password / ... Fix the defects".

Before this, the phone's consent overlay had Yes / No only: no field to type
the password into, and its answer went to a WAMP topic no HARTOS code reads,
so a credential ask filed on central (a goal's mailbox) could not be answered
from the phone at all.  The desktop card stores the value in Nunba's local
vault (/api/vault/store) and then grants; the phone has no local vault.

Now the overlay answers through the same consent route the desktop card uses
(POST /api/social/consent, /consent/decline), naming the ask by the
request_id its push carried (consent_prompt_push), and a credential answer
carries the typed value, which the route stores in the asking node's vault
before granting: only for that node's owner, only while the ask is open,
never a node secret, never logged or returned.

These tests run the real request_credential, ConsentService, vault and
consent blueprint on an in-memory database.
"""
import logging
import os
import sys

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

import pytest  # noqa: E402

from integrations.social.models import Base, UserConsent, db_session, get_engine  # noqa: E402

OWNER = 'owner-phone'
STRANGER = 'stranger-phone'
SECRET = 'Tr0ub4dor&3-horse'
ASK = '{"key_name": "site_password", "label": "Site password", ' \
      '"used_by": "the login step", "description": "Needed to sign in."}'
NAME = 'SITE_PASSWORD'
SCOPE = 'secret:' + NAME


@pytest.fixture(autouse=True)
def world(monkeypatch):
    engine = get_engine()
    Base.metadata.create_all(engine)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    monkeypatch.delenv('HEVOLVE_MASTER_KEY', raising=False)
    monkeypatch.delenv(NAME, raising=False)
    from security.secrets_manager import SecretsManager
    from hartos import ai_key_vault
    SecretsManager.reset()
    ai_key_vault.AIKeyVault.reset()
    monkeypatch.setattr(ai_key_vault, '_instance', None)
    emitted = []
    from integrations.social import consent_service
    monkeypatch.setattr(consent_service, '_emit',
                        lambda topic, data, msg_id=None:
                        emitted.append((topic, dict(data), msg_id)))
    yield emitted
    ai_key_vault.AIKeyVault.reset()
    SecretsManager.reset()
    os.environ.pop(NAME, None)
    Base.metadata.drop_all(engine)


@pytest.fixture
def phone(monkeypatch):
    """The overlay's door: the /api/social consent blueprint, signed in with
    the account token the phone holds (owner or a stranger)."""
    from types import SimpleNamespace
    from flask import Flask
    from integrations.social import auth, consent_api
    from integrations.social.models import get_db
    app = Flask(__name__)
    app.register_blueprint(consent_api.consent_bp)
    users = {'owner': OWNER, 'stranger': STRANGER}
    monkeypatch.setattr(auth, '_get_user_from_token', lambda token: (
        (SimpleNamespace(id=users[token], is_admin=False, is_moderator=False),
         get_db()) if token in users else (None, None)))
    client = app.test_client()

    def post(path, body, who='owner'):
        return client.post('/api/social/consent' + path, json=body,
                           headers={'Authorization': 'Bearer ' + who})
    return post


def _pushed_request_id(emitted):
    """The request_id the phone got: the push consent_prompt_push builds
    for the ask's own consent.request (msg_id 'consent.request:<row id>')."""
    from integrations.social.consent_service import consent_prompt_push
    asks = [(d, m) for t, d, m in emitted if t == 'consent.request']
    assert asks, 'no ask was filed'
    data, msg_id = asks[-1]
    push = consent_prompt_push(data, msg_id.split(':', 1)[-1])
    assert push['data']['consent_type'] == 'credential'
    return push['data']['request_id']


def _value():
    from hartos.ai_key_vault import get_ai_key_vault
    return get_ai_key_vault().owner_credential(NAME)


def _grants():
    with db_session() as db:
        return [r for r in db.query(UserConsent).filter_by(
            user_id=OWNER, consent_type='credential', scope=SCOPE).all()
            if r.granted_at is not None]


@pytest.mark.parametrize('agent', ['42', None])
def test_the_password_typed_on_the_phone_reaches_the_asking_node(world, phone, agent):
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id=agent)
    rid = _pushed_request_id(world)

    resp = phone('', {'consent_type': 'credential', 'request_id': rid,
                      'value': SECRET})

    assert resp.status_code == 201, resp.get_json()
    assert SECRET not in resp.get_data(as_text=True), 'never returned'
    assert _value() == SECRET
    world.clear()
    out = request_credential(ASK, agent_id=agent)
    assert 'is stored on this computer' in out
    assert SECRET not in out
    assert [t for t, _, _ in world if t == 'consent.request'] == [], \
        'answered: the card is not shown again'


def test_the_value_is_never_logged(world, phone, caplog):
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id='42')
    rid = _pushed_request_id(world)
    with caplog.at_level(logging.DEBUG):
        assert phone('', {'request_id': rid, 'value': SECRET}).status_code == 201
    assert SECRET not in caplog.text


def test_someone_else_cannot_answer_the_owners_ask(world, phone):
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id='42')
    rid = _pushed_request_id(world)

    resp = phone('', {'request_id': rid, 'value': SECRET}, who='stranger')

    assert resp.status_code == 404, 'not theirs: the same answer as no ask'
    assert _value() == '' and _grants() == []


def test_only_the_owner_of_the_node_puts_a_value_in_its_vault(world, phone, monkeypatch):
    """The ask is the signed-in user's, but this node belongs to someone
    else: its vault is not theirs to fill."""
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id='42')
    rid = _pushed_request_id(world)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'someone-else')

    resp = phone('', {'request_id': rid, 'value': SECRET})

    assert resp.status_code == 403
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    assert _value() == '' and _grants() == []


def test_a_value_needs_an_open_ask(world, phone):
    resp = phone('', {'request_id': 'no-such-row', 'value': SECRET})
    assert resp.status_code == 404
    resp = phone('', {'consent_type': 'credential', 'scope': SCOPE,
                      'value': SECRET})
    assert resp.status_code == 400, 'a value without its ask is refused'
    assert _value() == '' and _grants() == []


def test_an_ask_the_owner_said_no_to_takes_no_value(world, phone):
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id='42')
    rid = _pushed_request_id(world)
    assert phone('/decline', {'request_id': rid}).status_code == 200

    resp = phone('', {'request_id': rid, 'value': SECRET})

    assert resp.status_code == 404
    assert _value() == '' and _grants() == []


@pytest.mark.parametrize('value', [None, '', '   ', 'x' * 5000])
def test_a_credential_yes_carries_a_usable_value(world, phone, value):
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id='42')
    rid = _pushed_request_id(world)
    body = {'request_id': rid}
    if value is not None:
        body['value'] = value

    assert phone('', body).status_code == 400
    assert _value() == '' and _grants() == []


def test_a_node_secret_is_never_taken_from_a_card(world, phone):
    from hartos.env_secrets_manifest import NODE_SECRETS
    from integrations.social.consent_service import ConsentService
    node_secret = sorted(NODE_SECRETS)[0]
    with db_session(commit=True) as db:
        row = ConsentService.request_consent(db, OWNER, 'credential',
                                             scope='secret:' + node_secret)
        rid = row.id
    before = os.environ.get(node_secret)

    resp = phone('', {'request_id': rid, 'value': SECRET})

    assert resp.status_code == 400
    assert os.environ.get(node_secret) == before
    with db_session() as db:
        assert db.query(UserConsent).filter(
            UserConsent.scope == 'secret:' + node_secret,
            UserConsent.granted_at.isnot(None)).first() is None


@pytest.mark.parametrize('agent', ['42', None])
def test_no_on_the_phone_ends_the_asking(world, phone, agent):
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id=agent)
    rid = _pushed_request_id(world)

    resp = phone('/decline', {'consent_type': 'credential', 'request_id': rid})

    assert resp.status_code == 200, resp.get_json()
    world.clear()
    assert 'said no' in request_credential(ASK, agent_id=agent)
    assert [t for t, _, _ in world if t == 'consent.request'] == []


def test_the_desktop_card_still_grants_without_a_request_id(world, phone):
    """The desktop card stores locally first and grants by type and scope;
    that door is unchanged."""
    from hartos.ai_key_vault import get_ai_key_vault, request_credential
    request_credential(ASK, agent_id=None)
    get_ai_key_vault().store_credential('site_password', SECRET)
    resp = phone('', {'consent_type': 'credential', 'scope': SCOPE})
    assert resp.status_code == 201
    assert _value() == SECRET
