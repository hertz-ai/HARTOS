"""An answer names the ask it is for and the node that asked, and a node
refuses anything else before it looks at the value.

Owner, 22 Nov 2026: "Ask both, store where asked", and the phone may send a
password over the internet "only to the node that asked".

Until now an answer was tied to its ask by request_id alone.  The id is not a
secret: the ask's note is published on a router other sessions can read.  And
the same secret name is asked for on more than one node (a goal mailbox on
central and on a desktop).  So an id alone could put a typed password in the
vault of a node the person never meant, under a name they never read.

Now POST /api/social/consent and /consent/decline take two more keys with a
request_id: ``scope`` (the scope of the ask the card showed) and ``node_id``
(the node the client chose).  A node answers the same neutral 404 unless the
scope is the row's and the node is itself, decided before the value is read.
Both are required when the answer carries a value; a yes or a no without a
value works without them, as today's callers send it.

GET /api/social/consent?request_id= reads the caller's one open ask (id,
consent_type, scope, agent_id, agent_name, node_id, node_tier, node_name): a
client builds a credential card from that row, never from a push.

Real: request_credential, ConsentService, the vault (in memory), the consent
blueprint, the audit log.  The signed-in user is set by the test (the
account-token rule is auth.py's own and has its own tests).  Fake values
only; nothing leaves the process.
"""
import json
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
SECRET = 'fake-Tr0ub4dor&3-horse'
ASK = '{"key_name": "site_password", "label": "Site password", ' \
      '"used_by": "the login step", "description": "Needed to sign in."}'
OTHER_ASK = '{"key_name": "other_password", "label": "Other password", ' \
            '"used_by": "another step", "description": "Needed elsewhere."}'
NAME, OTHER_NAME = 'SITE_PASSWORD', 'OTHER_PASSWORD'
SCOPE, OTHER_SCOPE = 'secret:' + NAME, 'secret:' + OTHER_NAME
NODE_A = 'aaaa1c0277deb4e1'
NODE_B = 'bbbb9d3e55f0a7c2'
ASK_KEYS = {'id', 'consent_type', 'scope', 'agent_id', 'agent_name',
            'node_id', 'node_tier', 'node_name'}
LIST_KEYS = {'id', 'consent_type', 'scope', 'granted', 'granted_at',
             'revoked_at', 'reopened_at', 'agent_id', 'agent_name',
             'agent_state'}


@pytest.fixture(autouse=True)
def world(monkeypatch):
    """Node B: its own key, an empty vault and database.  Yields what it
    did: ``emitted`` consent events, ``stored`` vault writes (names only),
    ``audited`` audit events, and ``be(node)`` to become another node."""
    from types import SimpleNamespace
    from hartos import ai_key_vault
    from integrations.social import consent_service, fleet_command
    from security import immutable_audit_log, key_delegation, node_integrity
    from security.secrets_manager import SecretsManager
    engine = get_engine()
    Base.metadata.create_all(engine)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    monkeypatch.delenv('HEVOLVE_MASTER_KEY', raising=False)
    for name in (NAME, OTHER_NAME):
        monkeypatch.delenv(name, raising=False)
    here = {'node': NODE_B}
    monkeypatch.setattr(node_integrity, 'get_public_key_hex',
                        lambda: here['node'] + 'cd' * 24)
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'flat')
    monkeypatch.setattr(fleet_command, '_MACHINE', ('MSI', 'desktop'),
                        raising=False)
    SecretsManager.reset()
    ai_key_vault.AIKeyVault.reset()
    monkeypatch.setattr(ai_key_vault, '_instance', None)
    did = SimpleNamespace(emitted=[], stored=[], audited=[],
                          be=lambda node: here.update(node=node))
    monkeypatch.setattr(consent_service, '_emit',
                        lambda topic, data, msg_id=None:
                        did.emitted.append((topic, dict(data), msg_id)))
    # What a yes sets going after its commit is held still: a screen yes
    # starts the feed (admin.api.apply_embodied_answer -> VisionService) on a
    # worker thread of its own, and that must never run from a test.  It was
    # also still at work while the teardown dropped the tables (sqlite:
    # 'database table is locked', about one run in twenty).
    for effect in ('_copilot_switch_from_consent', '_embodied_feed_from_consent',
                   '_mailbox_goals_from_consent'):
        monkeypatch.setattr(consent_service, effect, lambda *a, **k: None)
    real_store = ai_key_vault.AIKeyVault.store_credential

    def store(self, key_name, value, *a, **k):
        did.stored.append(key_name)
        return real_store(self, key_name, value, *a, **k)
    monkeypatch.setattr(ai_key_vault.AIKeyVault, 'store_credential', store)
    audit = immutable_audit_log.get_audit_log()
    real_log = audit.log_event

    def log_event(event_type, *a, **k):
        did.audited.append(event_type)
        return real_log(event_type, *a, **k)
    monkeypatch.setattr(audit, 'log_event', log_event)
    yield did
    ai_key_vault.AIKeyVault.reset()
    SecretsManager.reset()
    for name in (NAME, OTHER_NAME):
        os.environ.pop(name, None)
    Base.metadata.drop_all(engine)


@pytest.fixture
def phone(monkeypatch):
    """A client at the consent door, signed in as the owner or a stranger:
    ``phone(path, body)`` posts, ``phone.read(query)`` gets."""
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

    def read(query='', who='owner'):
        return client.get('/api/social/consent' + query,
                          headers={'Authorization': 'Bearer ' + who})
    post.read = read
    return post


def _ask(world, ask=ASK, agent='42'):
    """File a credential ask on this node; its row id (what the push
    carries as request_id)."""
    from hartos.ai_key_vault import request_credential
    world.emitted.clear()
    request_credential(ask, agent_id=agent)
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


def _value(name=NAME):
    from hartos.ai_key_vault import get_ai_key_vault
    return get_ai_key_vault().get_tool_key(name) or ''


def _open(rid):
    """Whether the ask is still waiting for its answer."""
    from integrations.social.consent_service import open_ask
    with db_session() as db:
        return db.query(UserConsent).filter(
            UserConsent.id == rid, open_ask()).count() == 1


def _grants(scope=SCOPE):
    with db_session() as db:
        return [r.id for r in db.query(UserConsent).filter_by(
            user_id=OWNER, scope=scope).all() if r.granted_at is not None]


def _nothing_happened(world, rid):
    assert world.stored == [], 'the vault was not written'
    assert _value(NAME) == '' and _value(OTHER_NAME) == ''
    assert 'credential_stored' not in world.audited
    assert _open(rid), 'the ask is still open'
    assert _grants() == [] and _grants(OTHER_SCOPE) == []


# ── the answer the phone sends after it read the ask ──────────────────

def test_an_answer_that_names_its_scope_and_its_node_is_stored(world, phone):
    rid = _ask(world)
    resp = phone('', {'consent_type': 'credential', 'request_id': rid,
                      'scope': SCOPE, 'node_id': NODE_B, 'user_id': OWNER,
                      'value': SECRET})
    assert resp.status_code == 201, resp.get_json()
    assert SECRET not in resp.get_data(as_text=True)
    assert _value() == SECRET and world.stored == [NAME]
    assert not _open(rid)


def test_the_answer_is_built_from_the_row_the_node_returned(world, phone):
    """R2's order: read the ask on the node that asked, answer with the
    row's own scope and node_id."""
    rid = _ask(world)
    [row] = phone.read('?request_id=' + rid).get_json()['data']['consents']
    assert (row.get('node_id'), row.get('scope')) == (NODE_B, SCOPE), row
    resp = phone('', {'request_id': rid, 'scope': row['scope'],
                      'node_id': row['node_id'], 'value': SECRET})
    assert resp.status_code == 201, resp.get_json()
    assert _value() == SECRET


# ── a value needs both names ──────────────────────────────────────────

@pytest.mark.parametrize('missing', ['scope', 'node_id', 'both'])
def test_a_value_without_its_scope_or_its_node_is_refused(world, phone, missing):
    """What a phone built before this sends: {consent_type, request_id,
    value}.  Refused whatever rows this node holds, so it can never be
    stored on a node the ask did not come from."""
    rid = _ask(world)
    body = {'consent_type': 'credential', 'request_id': rid,
            'scope': SCOPE, 'node_id': NODE_B, 'value': SECRET}
    for key in (('scope', 'node_id') if missing == 'both' else (missing,)):
        del body[key]
    resp = phone('', body)
    assert resp.status_code == 404
    assert resp.get_json()['error'] == 'no such ask'
    _nothing_happened(world, rid)


@pytest.mark.parametrize('scope', [OTHER_SCOPE, '*', '', 'secret:', SCOPE.lower(),
                                   None, 7, ['x']])
def test_a_wrong_scope_with_a_right_id_is_refused(world, phone, scope):
    rid = _ask(world)
    resp = phone('', {'request_id': rid, 'scope': scope, 'node_id': NODE_B,
                      'value': SECRET})
    assert resp.status_code == 404
    _nothing_happened(world, rid)


@pytest.mark.parametrize('node', [NODE_A, '', 'unknown', NODE_B[:15],
                                  NODE_B + 'cd' * 24, None, 7, [NODE_B]])
def test_a_wrong_node_with_a_right_id_and_scope_is_refused(world, phone, node):
    rid = _ask(world)
    resp = phone('', {'request_id': rid, 'scope': SCOPE, 'node_id': node,
                      'value': SECRET})
    assert resp.status_code == 404
    _nothing_happened(world, rid)


def test_a_node_with_no_id_of_its_own_takes_no_value(world, phone, monkeypatch):
    """'unknown' names no node: two nodes without a key could not be told
    apart, so neither stores a value by request_id."""
    from integrations.social import fleet_command
    rid = _ask(world)
    monkeypatch.setattr(fleet_command, '_get_self_node_id', lambda: 'unknown')
    resp = phone('', {'request_id': rid, 'scope': SCOPE, 'node_id': 'unknown',
                      'value': SECRET})
    assert resp.status_code == 404
    _nothing_happened(world, rid)


def test_the_node_id_is_read_without_case_or_space(world, phone):
    rid = _ask(world)
    resp = phone('', {'request_id': rid, 'scope': ' ' + SCOPE + ' ',
                      'node_id': ' ' + NODE_B.upper() + ' ', 'value': SECRET})
    assert resp.status_code == 201, resp.get_json()
    assert _value() == SECRET


def test_the_refusal_comes_before_the_value_is_read(world, phone, caplog):
    """A value the route would refuse by itself (not text, empty, too long:
    400) gets the neutral 404 when the answer is for another node or scope,
    so the refusal says nothing about the value; and nothing of it is
    logged."""
    rid = _ask(world)
    with caplog.at_level(logging.DEBUG):
        for value in (7, '', '   ', 'x' * 5000, {'a': 1}, SECRET):
            for wrong in ({'node_id': NODE_A}, {'scope': OTHER_SCOPE}):
                body = dict({'request_id': rid, 'scope': SCOPE,
                             'node_id': NODE_B, 'value': value}, **wrong)
                resp = phone('', body)
                assert resp.status_code == 404, (value, wrong)
                assert resp.get_json() == {'success': False, 'error': 'no such ask'}
    assert SECRET not in caplog.text
    _nothing_happened(world, rid)
    # and bound right, the same values get the route's own answers
    assert phone('', {'request_id': rid, 'scope': SCOPE, 'node_id': NODE_B,
                      'value': 7}).status_code == 400


# ── the cross-route: an answer meant for node A reaches node B ────────

def test_an_answer_for_another_node_with_the_same_id_and_scope_is_refused(world, phone):
    """Node A and node B both ask the owner for the same secret name, and
    both rows have the same id.  Only node_id tells them apart.  (One
    database stands for both: the row is filed and read as node A, then this
    process is node B holding that same open row.)"""
    world.be(NODE_A)
    rid = _ask(world)
    [row] = phone.read('?request_id=' + rid).get_json()['data']['consents']
    assert (row.get('node_id'), row.get('scope')) == (NODE_A, SCOPE), row
    meant_for_a = {'consent_type': 'credential', 'request_id': rid,
                   'scope': row['scope'], 'node_id': row['node_id'],
                   'user_id': OWNER, 'value': SECRET}

    world.be(NODE_B)
    resp = phone('', dict(meant_for_a))

    assert resp.status_code == 404
    assert resp.get_json()['error'] == 'no such ask'
    _nothing_happened(world, rid)
    # the same answer on the node it was meant for is taken
    world.be(NODE_A)
    assert phone('', dict(meant_for_a)).status_code == 201
    assert _value() == SECRET


def test_an_answer_for_another_nodes_ask_with_the_same_id_is_refused(world, phone):
    """Same id, another secret name on each node: B stores nothing under
    either name."""
    rid = _ask(world)                              # B's row: SITE_PASSWORD
    resp = phone('', {'consent_type': 'credential', 'request_id': rid,
                      'scope': OTHER_SCOPE, 'node_id': NODE_A,
                      'user_id': OWNER, 'value': SECRET})
    assert resp.status_code == 404
    _nothing_happened(world, rid)


def test_a_no_for_another_node_leaves_this_nodes_ask_open(world, phone):
    rid = _ask(world)
    for wrong in ({'node_id': NODE_A}, {'scope': OTHER_SCOPE},
                  {'node_id': NODE_A, 'scope': SCOPE}):
        resp = phone('/decline', dict({'request_id': rid}, **wrong))
        assert resp.status_code == 404, wrong
        assert resp.get_json()['error'] == 'no such ask'
    _nothing_happened(world, rid)


# ── today's callers: a yes or a no without a value ────────────────────

def test_a_yes_without_a_value_needs_neither_key(world, phone):
    rid = _screen_ask(world)
    resp = phone('', {'consent_type': 'screen_capture', 'request_id': rid})
    assert resp.status_code == 201, resp.get_json()
    assert not _open(rid)


def test_a_no_without_a_value_needs_neither_key(world, phone):
    rid = _ask(world)
    resp = phone('/decline', {'consent_type': 'credential', 'request_id': rid})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['data'] == {'declined': True, 'id': rid}
    assert not _open(rid)


def test_a_yes_and_a_no_that_name_the_ask_and_the_node_are_taken(world, phone):
    rid = _screen_ask(world)
    assert phone('', {'request_id': rid, 'scope': '*',
                      'node_id': NODE_B}).status_code == 201
    rid = _ask(world)
    resp = phone('/decline', {'request_id': rid, 'scope': SCOPE,
                              'node_id': NODE_B})
    assert resp.status_code == 200 and not _open(rid)


@pytest.mark.parametrize('wrong', [{'node_id': NODE_A}, {'scope': 'window:1'}])
def test_a_yes_without_a_value_is_still_held_to_what_it_names(world, phone, wrong):
    rid = _screen_ask(world)
    resp = phone('', dict({'request_id': rid}, **wrong))
    assert resp.status_code == 404
    assert _open(rid)


def test_the_type_and_scope_form_is_untouched(world, phone):
    """The desktop card and the privacy pages grant and decline by type and
    scope; scope there is the consent's own, and no node is named."""
    resp = phone('', {'consent_type': 'screen_capture', 'scope': 'window:1'})
    assert resp.status_code == 201
    assert set(resp.get_json()['data']) == {'id', 'consent_type', 'scope',
                                            'granted_at'}
    resp = phone('', {'consent_type': 'credential', 'scope': SCOPE,
                      'node_id': NODE_B, 'value': SECRET})
    assert resp.status_code == 400, 'a value still needs its ask'
    assert resp.get_json()['error'] == 'a value answers an ask: request_id required'
    assert world.stored == []
    rid = _ask(world)
    resp = phone('/decline', {'consent_type': 'credential', 'scope': SCOPE,
                              'agent_id': '42'})
    assert resp.status_code == 200 and not _open(rid)


def test_a_credential_ask_still_needs_its_value(world, phone):
    rid = _ask(world)
    resp = phone('', {'request_id': rid, 'scope': SCOPE, 'node_id': NODE_B})
    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'a credential ask is answered with its value'
    assert _open(rid)


# ── the read of one ask ───────────────────────────────────────────────

def test_the_read_returns_the_callers_one_open_ask(world, phone):
    _ask(world, OTHER_ASK, agent=None)
    rid = _ask(world)
    resp = phone.read('?request_id=' + rid)
    assert resp.status_code == 200
    data = resp.get_json()['data']
    assert data['count'] == 1
    [row] = data['consents']
    assert set(row) == ASK_KEYS
    assert row == {'id': rid, 'consent_type': 'credential', 'scope': SCOPE,
                   'agent_id': '42', 'agent_name': row['agent_name'],
                   'node_id': NODE_B, 'node_tier': 'flat',
                   'node_name': 'MSI desktop bbbb'}


def test_the_read_names_no_agent_for_an_ask_no_agent_filed(world, phone):
    rid = _ask(world, agent=None)
    [row] = phone.read('?request_id=' + rid).get_json()['data']['consents']
    assert row['agent_id'] is None and row['agent_name'] is None
    assert set(row) == ASK_KEYS


def test_the_read_returns_nothing_for_another_persons_ask(world, phone):
    rid = _ask(world)
    resp = phone.read('?request_id=' + rid, who='stranger')
    assert resp.status_code == 200
    assert resp.get_json()['data'] == {'consents': [], 'count': 0}


def test_the_read_returns_nothing_for_an_unknown_or_empty_id(world, phone):
    """Never the list: a caller that names a request_id gets one ask or
    nothing, even when the id is empty."""
    _ask(world)
    for query in ('?request_id=no-such-row', '?request_id=', '?request_id=%20',
                  '?request_id=&consent_type=credential'):
        resp = phone.read(query)
        assert resp.status_code == 200, query
        assert resp.get_json()['data'] == {'consents': [], 'count': 0}, query


def test_the_read_returns_nothing_once_the_ask_is_answered_or_declined(world, phone):
    rid = _ask(world)
    assert phone('', {'request_id': rid, 'scope': SCOPE, 'node_id': NODE_B,
                      'value': SECRET}).status_code == 201
    resp = phone.read('?request_id=' + rid)
    assert resp.get_json()['data'] == {'consents': [], 'count': 0}
    assert SECRET not in resp.get_data(as_text=True)
    rid = _ask(world, OTHER_ASK)
    assert phone('/decline', {'request_id': rid}).status_code == 200
    assert phone.read('?request_id=' + rid).get_json()['data']['count'] == 0


def test_the_read_never_returns_a_value(world, phone):
    """An ask re-sent after its value was stored (the site refused it) is
    open again while the vault holds the old value: the read still returns
    the ask's names only."""
    rid = _ask(world)
    assert phone('', {'request_id': rid, 'scope': SCOPE, 'node_id': NODE_B,
                      'value': SECRET}).status_code == 201
    rid = _ask(world, ASK[:-1] + ', "rejected": true}')
    assert _value() == SECRET
    resp = phone.read('?request_id=' + rid)
    rows = resp.get_json()['data']['consents']
    assert len(rows) == 1, 'the one open ask, not the list'
    row = rows[0]
    assert set(row) == ASK_KEYS and row['id'] == rid
    assert SECRET not in resp.get_data(as_text=True)
    assert SECRET not in json.dumps(row)


def test_the_read_needs_a_signed_in_caller(world, phone):
    rid = _ask(world)
    assert phone.read('?request_id=' + rid, who='nobody').status_code == 401


def test_without_the_parameter_the_list_is_todays(world, phone):
    rid = _ask(world)
    phone('', {'consent_type': 'screen_capture', 'scope': 'window:1'})
    resp = phone.read()
    data = resp.get_json()['data']
    assert data['count'] == 2 and len(data['consents']) == 2
    for row in data['consents']:
        assert set(row) == LIST_KEYS, 'no node keys on the list rows'
    assert {r['id'] for r in data['consents']} >= {rid}
    filtered = phone.read('?consent_type=credential&active_only=false')
    assert [r['id'] for r in filtered.get_json()['data']['consents']] == [rid]
