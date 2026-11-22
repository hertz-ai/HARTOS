"""Every ask and every consent event says which node it happened on.

Owner, 20 Nov 2026: every ask names the node that asked.  Owner, 22 Nov 2026:
"Ask both, store where asked": a credential ask shows on the phone and the
desktop, and the password is saved only on the node that asked.  A client can
do that only when the ask says which node filed it.  Until now no key on any
consent event named a node, so a phone could not tell its own desktop's ask
from central's, and a yes on central looked like a yes on the desktop.

Now the one builder (fleet_command.self_node_keys) stamps node_id, node_tier
and node_name on the ask's note and push, on consent.granted and
consent.revoked, and on the ask.answered drop; the ask's note also carries
its request_id.

node_id is fleet_command._get_self_node_id and nothing else: the first 16
hex of the node's public key, which is the key a paired phone remembers from
the PeerLink handshake.  node_name is the maker's brand, the kind of machine
and 4 hex of node_id, through the egress scrub, and never the hostname.

Real: request_credential, ConsentService, _emit, consent_prompt_push,
person_ask.drop_everywhere, realtime.on_notification, the MessageBus and its
egress scrub, fcm_sync.send_fcm_push and hand_push_to_central, edge_privacy.
Read where each leg leaves the node: the bus's Crossbar transport (the WAMP
note and the push handed to central's relay), the SSE broadcast, and the
Firebase post.  Nothing is sent anywhere; the node key, the vault and the
database are this test's own.
"""
import json
import os
import sys
from types import SimpleNamespace

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

import pytest  # noqa: E402

from integrations.social.models import Base, db_session, get_engine  # noqa: E402

OWNER = '10202'
KEY = '3f9a1c0277deb4e1' + 'ab' * 24          # a fake node public key
NODE = '3f9a1c0277deb4e1'
ASK = '{"key_name": "site_password", "label": "Site password", ' \
      '"used_by": "the login step", "description": "Needed to sign in."}'
NAME = 'SITE_PASSWORD'
SCOPE = 'secret:' + NAME
PERSON = 'JOHN-SMITH-PC'
NODE_KEYS = {'node_id': NODE, 'node_tier': 'flat', 'node_name': 'MSI desktop 3f9a'}


class _Now:
    """The push leg runs on the caller's thread here."""

    def __init__(self, target=None, args=(), **kw):
        self._run = lambda: target(*args)

    def start(self):
        self._run()


def _keys():
    from integrations.social import fleet_command
    if not hasattr(fleet_command, 'self_node_keys'):
        return {}
    return fleet_command.self_node_keys()


@pytest.fixture(autouse=True)
def node(monkeypatch):
    """A node of this test's own: a fake key, tier flat, an MSI desktop
    whose hostname holds a person's name, an empty in-memory vault and
    database."""
    import platform
    import socket
    from hartos import ai_key_vault
    from integrations.social import fleet_command
    from security import key_delegation, node_integrity
    from security.secrets_manager import SecretsManager
    engine = get_engine()
    Base.metadata.create_all(engine)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    monkeypatch.delenv('HEVOLVE_MASTER_KEY', raising=False)
    monkeypatch.delenv(NAME, raising=False)
    monkeypatch.setenv('COMPUTERNAME', PERSON)
    monkeypatch.setenv('HOSTNAME', PERSON)
    monkeypatch.setattr(socket, 'gethostname', lambda: PERSON)
    monkeypatch.setattr(platform, 'node', lambda: PERSON)
    monkeypatch.setattr(node_integrity, 'get_public_key_hex', lambda: KEY)
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'flat')
    monkeypatch.setattr(fleet_command, '_maker_reported',
                        lambda: ['Micro-Star International Co., Ltd.'],
                        raising=False)
    monkeypatch.setattr(fleet_command, '_machine_kind', lambda: 'desktop',
                        raising=False)
    monkeypatch.setattr(fleet_command, '_MACHINE', None, raising=False)
    SecretsManager.reset()
    ai_key_vault.AIKeyVault.reset()
    monkeypatch.setattr(ai_key_vault, '_instance', None)
    yield monkeypatch
    ai_key_vault.AIKeyVault.reset()
    SecretsManager.reset()
    os.environ.pop(NAME, None)
    Base.metadata.drop_all(engine)


@pytest.fixture
def legs(monkeypatch):
    """What leaves this node, by leg.  ``wamp``: (uri, payload) the bus hands
    its Crossbar transport, after its egress scrub; ``sse``; ``fcm``: the
    Firebase posts (only when ``direct()`` gave the node a credential)."""
    from core import fcm_sync
    from core.peer_link import message_bus
    from core.platform import events
    from integrations.social import realtime
    out = SimpleNamespace(wamp=[], sse=[], fcm=[])
    monkeypatch.setitem(sys.modules, 'hartos.crossbar_server',
                        SimpleNamespace(wamp_session=None))
    message_bus.reset_message_bus()
    bus = message_bus.get_message_bus()
    bus._http_transport = lambda uri, payload: out.wamp.append(
        (uri, json.loads(payload)))
    monkeypatch.setattr(events, 'broadcast_sse_safe',
                        lambda kind, data, user_id=None: out.sse.append(
                            (kind, dict(data), user_id)) or True)
    monkeypatch.setattr(realtime, '_pushes_allowed', lambda: True)
    monkeypatch.setattr(realtime, 'threading', SimpleNamespace(Thread=_Now))
    realtime._PUSHED.clear()
    monkeypatch.setattr(fcm_sync, '_fcm_credential', lambda: (None, None))
    monkeypatch.setattr(fcm_sync, 'resolve_central_id', lambda uid: OWNER)

    def direct():
        monkeypatch.setattr(fcm_sync, '_fcm_credential',
                            lambda: ('fake-access', 'fake-project'))
        monkeypatch.setattr(fcm_sync, 'get_local_fcm_token',
                            lambda uid: 'fake-token')
        monkeypatch.setattr(
            fcm_sync, '_post_fcm_message',
            lambda access, project, token, title, body, data, timeout:
            out.fcm.append((title, body, dict(data or {}))) or True)
    out.direct = direct
    out.relay = lambda: monkeypatch.setattr(fcm_sync, '_fcm_credential',
                                            lambda: (None, None))
    out.notes = lambda kind: [p for uri, p in out.wamp
                              if uri == 'com.hertzai.hevolve.social.' + OWNER
                              and p.get('type') == kind]
    out.sse_notes = lambda kind: [d for k, d, uid in out.sse
                                  if k == 'notification' and uid == OWNER
                                  and d.get('type') == kind]
    out.relayed = lambda: [p for uri, p in out.wamp
                           if uri == 'com.hertzai.hevolve.confirmation']
    yield out
    realtime._PUSHED.clear()
    message_bus.reset_message_bus()


def _file_ask():
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id='42')


def _row(legs):
    """The ask's row id, from its note's msg_id ('consent.request:<id>')."""
    return legs.notes('consent.request')[0]['msg_id'].split(':', 1)[1]


def _grant():
    from integrations.social.consent_service import ConsentService
    with db_session() as db:
        ConsentService.grant_consent(db, OWNER, 'credential', SCOPE)


def _revoke():
    from integrations.social.consent_service import ConsentService
    with db_session() as db:
        ConsentService.revoke_consent(db, OWNER, 'credential', SCOPE)


# ── the one builder ───────────────────────────────────────────────────

def test_the_node_id_is_the_key_prefix_a_paired_phone_remembers():
    """The PeerLink hello carries get_public_key_hex() as ed25519_public and
    the phone stores it; node_id is its first 16 hex, from
    _get_self_node_id."""
    from integrations.social import fleet_command
    assert fleet_command._get_self_node_id() == KEY[:16] == NODE
    assert _keys().get('node_id') == NODE


def test_the_node_id_comes_from_get_self_node_id_and_nowhere_else(node):
    from integrations.social import fleet_command
    node.setattr(fleet_command, '_get_self_node_id', lambda: 'feedfacefeedface')
    assert _keys().get('node_id') == 'feedfacefeedface'
    assert _keys().get('node_name') == 'MSI desktop feed'


def test_a_node_with_no_key_names_no_node_id(node):
    from integrations.social import fleet_command
    node.setattr(fleet_command, '_get_self_node_id', lambda: 'unknown')
    keys = _keys()
    assert 'node_id' not in keys
    assert keys.get('node_tier') == 'flat'
    assert keys.get('node_name') == 'MSI desktop'
    assert fleet_command.self_node_id() is None


def test_a_desktop_is_named_by_brand_kind_and_four_hex():
    assert _keys() == NODE_KEYS


@pytest.mark.parametrize('said, kind, name', [
    (['LENOVO'], 'laptop', 'Lenovo laptop 3f9a'),
    (['System manufacturer', 'ASUSTeK COMPUTER INC.'], 'desktop', 'ASUS desktop 3f9a'),
    (['Hewlett-Packard'], 'laptop', 'HP laptop 3f9a'),
    (['Dell Inc.'], 'computer', 'Dell computer 3f9a'),
    # A maker that is not listed is free text: none of it is sent.
    (['John Smith Custom PCs'], 'desktop', 'Desktop 3f9a'),
    (['To Be Filled By O.E.M.'], 'laptop', 'Laptop 3f9a'),
    ([], 'computer', 'Computer 3f9a'),
])
def test_the_name_holds_only_a_listed_brand(node, said, kind, name):
    from integrations.social import fleet_command
    node.setattr(fleet_command, '_maker_reported', lambda: said, raising=False)
    node.setattr(fleet_command, '_machine_kind', lambda: kind, raising=False)
    assert _keys().get('node_name') == name


@pytest.mark.parametrize('tier, name', [('central', 'Central'),
                                        ('regional', 'Regional')])
def test_central_and_regional_nodes_are_named_by_role(node, tier, name):
    from security import key_delegation
    node.setattr(key_delegation, 'get_node_tier', lambda: tier)
    assert _keys() == {'node_id': NODE, 'node_tier': tier, 'node_name': name}


def test_the_name_passes_the_egress_scrub(node):
    from security import edge_privacy
    seen = []
    node.setattr(edge_privacy, 'scrub_text',
                 lambda text: seen.append(text) or 'scrubbed')
    assert _keys().get('node_name') == 'scrubbed'
    assert seen == ['MSI desktop 3f9a']


def test_a_name_that_cannot_be_scrubbed_stays_on_the_node(node):
    from security import edge_privacy

    def broken(text):
        raise ImportError('no scrubber')
    node.setattr(edge_privacy, 'scrub_text', broken)
    assert _keys() == {'node_id': NODE, 'node_tier': 'flat'}


def test_a_tier_that_cannot_be_read_is_left_out(node):
    from security import key_delegation

    def broken():
        raise RuntimeError('no tier')
    node.setattr(key_delegation, 'get_node_tier', broken)
    assert _keys() == {'node_id': NODE, 'node_name': 'MSI desktop 3f9a'}


def test_the_machine_is_read_once(node):
    from integrations.social import fleet_command
    reads = []
    node.setattr(fleet_command, '_maker_reported',
                 lambda: reads.append(1) or ['LENOVO'], raising=False)
    assert [_keys().get('node_name') for _ in range(3)] == ['Lenovo desktop 3f9a'] * 3
    assert len(reads) == 1


# ── the ask ───────────────────────────────────────────────────────────

def test_the_asks_note_names_its_node_and_its_request_id(legs):
    _file_ask()
    [wamp] = legs.notes('consent.request')
    [sse] = legs.sse_notes('consent.request')
    for note in (wamp, sse):
        assert {k: note.get(k) for k in NODE_KEYS} == NODE_KEYS
        assert note.get('request_id') and note['msg_id'] == (
            'consent.request:' + note['request_id'])
        # what the note already said
        assert note['consent_type'] == 'credential' and note['scope'] == SCOPE
        assert note['agent_id'] == '42' and 'reason' in note


def test_the_asks_push_names_its_node_on_the_direct_leg(legs):
    legs.direct()
    _file_ask()
    [(title, body, data)] = legs.fcm
    row = _row(legs)
    assert title and body
    assert data == dict(NODE_KEYS, type='consent_prompt', request_id=row,
                        user_id=OWNER, topic_reply='com.hertzai.pupit.' + OWNER,
                        consent_type='credential', title=title, body=body)


def test_the_asks_push_names_its_node_on_the_leg_through_central(legs):
    """No Firebase credential here: the push is handed to central's relay,
    whose allowlist takes consent_type and the node keys from push_data
    (chatbot_pipeline relay_push.RELAY_PUSH_DATA_KEYS)."""
    _file_ask()
    [message] = legs.relayed()
    row = _row(legs)
    assert message['request_id'] == row and message['action'] == 'consent_prompt'
    push_data = message['push_data']
    assert {k: push_data.get(k) for k in NODE_KEYS} == NODE_KEYS
    assert push_data['consent_type'] == 'credential'


def test_the_secrets_name_never_rides_a_push(legs):
    """A credential's scope spells what it is for (a mailbox's address in a
    goal mailbox's): the phone reads it from the node that asked."""
    legs.direct()
    _file_ask()
    assert 'scope' not in legs.fcm[0][2]
    from integrations.social import realtime
    realtime._PUSHED.clear()
    legs.relay()
    _file_ask()
    [message] = legs.relayed()
    assert 'scope' not in message and 'scope' not in message['push_data']


# ── the answer, the no, the drop ──────────────────────────────────────

def test_a_grant_names_the_node_it_was_granted_on(legs):
    _file_ask()
    _grant()
    [wamp] = legs.notes('consent.granted')
    [sse] = legs.sse_notes('consent.granted')
    for note in (wamp, sse):
        assert {k: note.get(k) for k in NODE_KEYS} == NODE_KEYS
        assert note['consent_type'] == 'credential' and note['scope'] == SCOPE
        assert 'request_id' not in note, 'a grant is not an ask'


def test_a_revoke_names_the_node_it_was_revoked_on(legs):
    _file_ask()
    _grant()
    _revoke()
    [wamp] = legs.notes('consent.revoked')
    [sse] = legs.sse_notes('consent.revoked')
    for note in (wamp, sse):
        assert {k: note.get(k) for k in NODE_KEYS} == NODE_KEYS
        assert note['consent_type'] == 'credential' and note['scope'] == SCOPE


def test_the_drop_names_the_node_the_ask_was_answered_on(legs):
    legs.direct()
    _file_ask()
    row = _row(legs)
    legs.fcm.clear()
    _grant()
    [wamp] = legs.notes('ask.answered')
    [sse] = legs.sse_notes('ask.answered')
    for note in (wamp, sse):
        assert {k: note.get(k) for k in NODE_KEYS} == NODE_KEYS
        assert note['ask_id'] == row
    [(title, body, data)] = legs.fcm
    assert (title, body) == ('', ''), 'a drop shows nothing'
    assert data == dict(NODE_KEYS, type='ask.answered', request_id=row, ask_id=row)


def test_central_events_say_central(node, legs):
    from security import key_delegation
    node.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    _file_ask()
    _grant()
    for kind in ('consent.request', 'consent.granted', 'ask.answered'):
        [note] = legs.notes(kind)
        assert (note.get('node_tier'), note.get('node_name'), note.get('node_id')) == (
            'central', 'Central', NODE), kind
    assert legs.relayed()[0]['push_data'].get('node_tier') == 'central'


# ── the hostname ──────────────────────────────────────────────────────

def test_no_leg_that_leaves_the_node_carries_the_hostname(node, legs):
    """The hostname holds a person's name, and so does what the board says
    made it: neither is in anything that leaves, on any leg, with or without
    a Firebase credential."""
    from integrations.social import fleet_command, realtime
    node.setattr(fleet_command, '_maker_reported',
                 lambda: ['John Smith Custom PCs'], raising=False)
    _file_ask()                                   # relayed through central
    _grant()
    _revoke()
    legs.direct()
    realtime._PUSHED.clear()
    with db_session() as db:
        from integrations.social.consent_service import ConsentService
        ConsentService.request_consent(db, OWNER, 'screen_capture',
                                       agent_id='42', reason='to look')
    assert legs.fcm and legs.relayed() and legs.notes('consent.revoked')
    left = json.dumps([legs.wamp, legs.sse, legs.fcm]).lower()
    for word in ('john', 'smith'):
        assert word not in left
    names = {p.get('node_name') for _, p in legs.wamp if 'node_name' in p}
    assert names == {'Desktop 3f9a'}
