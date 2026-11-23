"""The phone app in the field answers a consent ask by its request_id alone,
and the node takes that answer as it always did.

Owner, 22 Nov 2026: "functionalities shd work, forget about security for now
... all should work the way its supposed to work during demo".

The Android overlay (Hevolve_React_Native android/.../service/ConsentAnswer.kt,
ConsentAnswer.body) answers with exactly {consent_type, request_id} for a no
or a yes, and {consent_type, request_id, value} for a credential's yes.  It
sends no scope and no node_id.  d22df004e refused every answer that carried a
value without both, so a person answering a password card on the phone got
"This request is no longer open" and nothing was stored.

Now an answer that names neither scope nor node_id is taken by request_id
alone (the caller's own open ask with that id), exactly as d22df004e's parent
took it, and one log line names that road.  An answer that names either is
still held to both (test_an_answer_is_bound_to_its_ask_and_its_node.py).

Real: request_credential, ConsentService, the vault (in memory), the consent
blueprint.  The signed-in user is set by the test.  Fake values only; nothing
leaves the process.
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
SECRET = 'fake-c0rrect-h0rse-battery'
ASK = '{"key_name": "site_password", "label": "Site password", ' \
      '"used_by": "the login step", "description": "Needed to sign in."}'
NAME = 'SITE_PASSWORD'
SCOPE = 'secret:' + NAME
NODE = 'bbbb9d3e55f0a7c2'
OLD_ROAD = 'names neither its scope nor its node'


@pytest.fixture(autouse=True)
def world(monkeypatch):
    """This node: its own key, an empty vault and database.  Yields what it
    did: ``emitted`` consent events and ``stored`` vault writes (names
    only)."""
    from types import SimpleNamespace
    from hartos import ai_key_vault
    from integrations.social import consent_service
    from security import key_delegation, node_integrity
    from security.secrets_manager import SecretsManager
    engine = get_engine()
    Base.metadata.create_all(engine)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    monkeypatch.delenv('HEVOLVE_MASTER_KEY', raising=False)
    monkeypatch.delenv(NAME, raising=False)
    monkeypatch.setattr(node_integrity, 'get_public_key_hex',
                        lambda: NODE + 'cd' * 24)
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'flat')
    SecretsManager.reset()
    ai_key_vault.AIKeyVault.reset()
    monkeypatch.setattr(ai_key_vault, '_instance', None)
    monkeypatch.setattr(ai_key_vault, '_REFUSED_ANSWERS', {}, raising=False)
    did = SimpleNamespace(emitted=[], stored=[])
    monkeypatch.setattr(consent_service, '_emit',
                        lambda topic, data, msg_id=None:
                        did.emitted.append((topic, dict(data), msg_id)))
    # What a yes sets going after its commit is held still (the screen feed,
    # the copilot switch, a mailbox goal): never from a test.
    for effect in ('_copilot_switch_from_consent', '_embodied_feed_from_consent',
                   '_mailbox_goals_from_consent'):
        monkeypatch.setattr(consent_service, effect, lambda *a, **k: None,
                            raising=False)
    real_store = ai_key_vault.AIKeyVault.store_credential

    def store(self, key_name, value, *a, **k):
        did.stored.append(key_name)
        return real_store(self, key_name, value, *a, **k)
    monkeypatch.setattr(ai_key_vault.AIKeyVault, 'store_credential', store)
    yield did
    ai_key_vault.AIKeyVault.reset()
    SecretsManager.reset()
    os.environ.pop(NAME, None)
    Base.metadata.drop_all(engine)


@pytest.fixture
def phone(monkeypatch):
    """The phone at the consent door, signed in as the owner or a stranger:
    ``phone(path, body)`` posts."""
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


@pytest.fixture
def said():
    """What the consent route logged: (level, text) per line."""
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append((record.levelno, record.getMessage()))
    log = logging.getLogger('hevolve_social')
    handler, level = Keep(level=logging.DEBUG), log.level
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    yield records
    log.removeHandler(handler)
    log.setLevel(level)


def android_body(consent_type, request_id, yes, value=None):
    """ConsentAnswer.body, line for line: consent_type and request_id, and
    the typed value on a credential's yes.  Nothing else."""
    body = {'consent_type': consent_type.strip(), 'request_id': request_id}
    if yes and consent_type.strip() == 'credential':
        body['value'] = value
    return body


def _ask(world):
    """The agent asks for a credential on this node; the row id its push
    carries as request_id."""
    from hartos.ai_key_vault import request_credential
    world.emitted.clear()
    request_credential(ASK, agent_id='42')
    [(_, _, msg_id)] = [e for e in world.emitted if e[0] == 'consent.request']
    return msg_id.split(':', 1)[1]


def _screen_ask(world):
    from integrations.social.consent_service import ConsentService
    world.emitted.clear()
    with db_session() as db:
        ConsentService.request_consent(db, OWNER, 'screen_capture',
                                       agent_id='42', reason='to look')
    [(_, _, msg_id)] = [e for e in world.emitted if e[0] == 'consent.request']
    return msg_id.split(':', 1)[1]


def _value():
    from hartos.ai_key_vault import get_ai_key_vault
    return get_ai_key_vault().get_tool_key(NAME) or ''


def _open(rid):
    from integrations.social.consent_service import open_ask
    with db_session() as db:
        return db.query(UserConsent).filter(
            UserConsent.id == rid, open_ask()).count() == 1


def _grants():
    with db_session() as db:
        return [r.id for r in db.query(UserConsent).filter_by(
            user_id=OWNER, scope=SCOPE).all() if r.granted_at is not None]


# ── the password card on the phone ────────────────────────────────────

def test_the_phones_password_answer_is_stored(world, phone):
    rid = _ask(world)
    body = android_body('credential', rid, True, SECRET)
    assert set(body) == {'consent_type', 'request_id', 'value'}

    resp = phone('', body)

    assert resp.status_code == 201, resp.get_json()
    data = resp.get_json()['data']
    assert set(data) == {'id', 'consent_type', 'scope', 'granted_at'}
    assert (data['consent_type'], data['scope']) == ('credential', SCOPE)
    assert SECRET not in resp.get_data(as_text=True)
    assert _value() == SECRET and world.stored == [NAME]
    assert not _open(rid), 'the ask is answered'
    assert len(_grants()) == 1


def test_the_agent_then_hears_its_credential_is_stored(world, phone):
    from hartos.ai_key_vault import request_credential
    rid = _ask(world)
    assert phone('', android_body('credential', rid, True, SECRET)).status_code == 201
    out = request_credential(ASK, agent_id='42')
    assert "'Site password' is stored on this computer" in out, out
    assert 'not taken' not in out and SECRET not in out


def test_explicit_empty_names_are_names_not_given(world, phone):
    """A body whose scope and node_id are both null names neither, the same
    as one without the keys."""
    rid = _ask(world)
    resp = phone('', dict(android_body('credential', rid, True, SECRET),
                          scope=None, node_id=None))
    assert resp.status_code == 201, resp.get_json()
    assert _value() == SECRET


def test_a_node_with_no_id_of_its_own_takes_the_phones_answer(world, phone, monkeypatch):
    """The answer names no node, so this node's own id is not asked for."""
    from integrations.social import fleet_command
    monkeypatch.setattr(fleet_command, '_get_self_node_id', lambda: 'unknown')
    rid = _ask(world)
    assert phone('', android_body('credential', rid, True, SECRET)).status_code == 201
    assert _value() == SECRET


def test_the_road_is_logged_once_and_the_value_never(world, phone, said):
    rid = _ask(world)
    del said[:]
    assert phone('', android_body('credential', rid, True, SECRET)).status_code == 201
    road = [line for level, line in said
            if OLD_ROAD in line and level >= logging.INFO]
    assert len(road) == 1, said
    assert rid in road[0] and 'with a value' in road[0]
    assert all(SECRET not in line for _, line in said)


# ── what stays as it was ──────────────────────────────────────────────

def test_the_phones_no_is_taken(world, phone):
    rid = _ask(world)
    body = android_body('credential', rid, False, 'typed then denied')
    assert body == {'consent_type': 'credential', 'request_id': rid}
    resp = phone('/decline', body)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['data'] == {'declined': True, 'id': rid}
    assert not _open(rid) and world.stored == []


def test_the_phones_yes_to_another_kind_of_ask_is_taken(world, phone):
    rid = _screen_ask(world)
    resp = phone('', android_body('screen_capture', rid, True))
    assert resp.status_code == 201, resp.get_json()
    assert not _open(rid)


def test_a_second_answer_to_an_answered_ask_is_refused(world, phone):
    rid = _ask(world)
    assert phone('', android_body('credential', rid, True, SECRET)).status_code == 201
    resp = phone('', android_body('credential', rid, True, 'fake-second-try'))
    assert resp.status_code == 404
    assert resp.get_json()['error'] == 'no such ask'
    assert _value() == SECRET and world.stored == [NAME]


def test_another_persons_ask_is_not_answered(world, phone):
    rid = _ask(world)
    resp = phone('', android_body('credential', rid, True, SECRET), who='stranger')
    assert resp.status_code == 404
    assert resp.get_json()['error'] == 'no such ask'
    assert world.stored == [] and _value() == ''
    assert _open(rid) and _grants() == []


def test_an_unknown_id_is_the_neutral_404(world, phone):
    _ask(world)
    resp = phone('', android_body('credential', 'no-such-row', True, SECRET))
    assert resp.status_code == 404
    assert resp.get_json() == {'success': False, 'error': 'no such ask'}
    assert world.stored == []
