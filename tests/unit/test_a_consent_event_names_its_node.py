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
    monkeypatch.delenv('HARTOS_PUBLIC_URL', raising=False)
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
    """With no public URL set, the keys are exactly as before node_url."""
    from security import key_delegation
    node.setattr(key_delegation, 'get_node_tier', lambda: tier)
    assert _keys() == {'node_id': NODE, 'node_tier': tier, 'node_name': name}


PUBLIC = 'https://api.hevolve.ai'


@pytest.mark.parametrize('tier', ['central', 'regional'])
def test_central_and_regional_nodes_say_where_they_answer(node, tier):
    """A phone answers a central or regional ask at the node that asked
    (cointinue's card, 11-23): node_url is that node's public https base,
    HARTOS_PUBLIC_URL (core.public_url, the one reader), without its
    trailing slash."""
    from security import key_delegation
    node.setattr(key_delegation, 'get_node_tier', lambda: tier)
    node.setenv('HARTOS_PUBLIC_URL', PUBLIC + '/')
    assert _keys().get('node_url') == PUBLIC


def test_a_desktop_never_says_a_url(node):
    """A flat node is answered over the phone's device link, never at a URL,
    whatever its environment holds."""
    node.setenv('HARTOS_PUBLIC_URL', PUBLIC)
    assert 'node_url' not in _keys()


@pytest.mark.parametrize('url', [
    'http://api.hevolve.ai',          # not https
    'https://localhost:6777',         # this machine only
    'https://192.168.0.9',            # a LAN address
    'https://deepbox.local',          # a LAN name
    'api.hevolve.ai',                 # no scheme
])
def test_only_a_public_https_url_is_said(node, url):
    from security import key_delegation
    node.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    node.setenv('HARTOS_PUBLIC_URL', url)
    keys = _keys()
    assert 'node_url' not in keys
    assert keys.get('node_name') == 'Central'


def test_the_url_passes_the_egress_scrub_unchanged_or_stays(node):
    """The URL leaves the node through the same scrub as the name.  A URL
    the scrub would change is left out, never sent half-masked."""
    from security import edge_privacy, key_delegation
    node.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    node.setenv('HARTOS_PUBLIC_URL', PUBLIC)
    seen = []
    node.setattr(edge_privacy, 'scrub_text',
                 lambda text: seen.append(text) or text)
    assert _keys().get('node_url') == PUBLIC
    assert PUBLIC in seen
    node.setattr(edge_privacy, 'scrub_text',
                 lambda text: text.replace('hevolve', '[x]'))
    assert 'node_url' not in _keys()


def test_a_central_asks_push_carries_its_url(node):
    """The phone's push carries what the one builder says (it spreads it
    into data), so the card knows where to answer."""
    from integrations.social.consent_service import consent_prompt_push
    from security import key_delegation
    node.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    node.setenv('HARTOS_PUBLIC_URL', PUBLIC)
    push = consent_prompt_push({'user_id': OWNER, 'consent_type': 'credential',
                                'reason': 'Bot token'}, 7)
    assert push['data']['node_url'] == PUBLIC
    assert push['data']['node_tier'] == 'central'


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


# A goal mailbox's ask, as its agent files it: the secret's name holds the
# mailbox's address, and the reason spells the label, the address and the
# placeholder the agent uses.
MAILBOX_ASK = json.dumps({
    'key_name': 'email_password_sales_example_com',
    'label': 'Password for sales@example.com',
    'used_by': 'the outreach mailbox sales@example.com',
    'description': 'Sign in with {{secret:EMAIL_PASSWORD_SALES_EXAMPLE_COM}}.'})
MAILBOX_NAME = 'EMAIL_PASSWORD_SALES_EXAMPLE_COM'
# Every way the secret's name or what it is for can be spelled.
SPELLINGS = ('email_password', 'sales_example_com', 'sales@example.com',
             'example.com', '{{secret', 'secret:', 'outreach mailbox')
SAYS_ON = ' asks for a password or key on MSI desktop 3f9a: '
FAKE_PASSWORD = 'fake-Tr0ub4dor&3-horse'
FAKE_TOKEN = 'ghp_' + 'fAkE0' * 8


def _file_mailbox_ask():
    from hartos.ai_key_vault import request_credential
    request_credential(MAILBOX_ASK, agent_id='42')


def _spelled(thing):
    text = json.dumps(thing).lower()
    return [word for word in SPELLINGS if word in text]


def test_the_reason_does_spell_the_secret(legs):
    """The ask's reason, as the card on the node that asked shows it, names
    the secret and its mailbox."""
    _file_mailbox_ask()
    [note] = legs.sse_notes('consent.request')
    assert note['scope'] == 'secret:' + MAILBOX_NAME
    assert set(_spelled(note['reason'])) >= {
        'sales@example.com', '{{secret', 'email_password'}


# Until the phone build that reads the ask from its node (R2) ships, the
# phone's card is built from the push alone, so a credential ask's push says
# which credential again: who asks, on which computer, then the ask's reason
# (its label first), through the egress scrub, which masks values and the
# mailbox's address.  A temporary restore of b168e3bc1's parent, owner 11-22:
# "functionalities shd work, forget about security for now".

def test_a_credential_push_says_who_where_and_which_credential(legs):
    """No scope or reason key is added: the words are in the title and body
    (the body is in the data too)."""
    from security.edge_privacy import scrub_text
    legs.direct()
    _file_mailbox_ask()
    [note] = legs.sse_notes('consent.request')
    [(title, body, data)] = legs.fcm
    who = title[:-len(' needs your OK')]
    assert title == who + ' needs your OK' and who
    assert body.startswith(who + SAYS_ON + 'Password for '), body
    assert body == scrub_text(who + SAYS_ON + note['reason'])[:240]
    assert 'sales@example.com' not in json.dumps([title, body, data])
    assert data['body'] == body
    assert 'scope' not in data and 'reason' not in data


def test_the_leg_through_central_carries_the_same_words(legs):
    """The whole message handed to central's relay: the same text, no scope
    key, and the address masked."""
    legs.relay()
    _file_mailbox_ask()
    [message] = legs.relayed()
    assert 'scope' not in message and 'scope' not in message['push_data']
    [text] = message['text']
    who = message['push_title'][:-len(' needs your OK')]
    assert message['push_title'] == who + ' needs your OK' and who
    assert text.startswith(who + SAYS_ON + 'Password for '), text
    assert 'sales@example.com' not in json.dumps(message)
    assert message['push_data']['consent_type'] == 'credential'


def test_a_credential_push_gives_its_reason_after_who_and_where(legs):
    legs.direct()
    _file_ask()
    [(title, body, data)] = legs.fcm
    who = title[:-len(' needs your OK')]
    assert body.startswith(
        who + SAYS_ON + 'Site password is needed for the login step. '
        'Needed to sign in.'), body
    assert data['body'] == body


def test_a_credential_push_never_carries_a_value(legs):
    """Values an agent wrote into its ask's words are masked by the scrub
    before the push leaves: the label is there, the values are not."""
    from hartos.ai_key_vault import request_credential
    legs.direct()
    request_credential(json.dumps({
        'key_name': 'site_password', 'label': 'Site password',
        'used_by': 'the login step',
        'description': f'The old one (password: {FAKE_PASSWORD} and token '
                       f'{FAKE_TOKEN}) stopped working.'}), agent_id='42')
    [(title, body, data)] = legs.fcm
    sent = json.dumps([title, body, data])
    assert 'Site password is needed for the login step.' in body, body
    assert FAKE_PASSWORD not in sent and FAKE_TOKEN not in sent


def test_a_stored_value_is_never_on_the_push_that_asks_again(legs):
    """The site refused the stored value and the agent asks again: the push
    says which credential was rejected, never the value."""
    from hartos.ai_key_vault import get_ai_key_vault, request_credential
    get_ai_key_vault().store_credential(NAME, FAKE_PASSWORD, granted_only=True)
    _grant()
    legs.direct()
    request_credential(ASK[:-1] + ', "rejected": true}', agent_id='42')
    [(title, body, data)] = legs.fcm
    assert 'Site password was rejected when the login step tried it' in body, body
    assert FAKE_PASSWORD not in json.dumps([title, body, data])


def test_the_credential_words_with_no_node_name_or_no_reason(node):
    from integrations.social import consent_service, fleet_command
    node.setattr(fleet_command, 'self_node_keys', lambda: {})
    push = consent_service.consent_prompt_push(
        {'user_id': OWNER, 'consent_type': 'credential',
         'reason': 'Site password is needed for the login step.'}, 'row-1')
    assert push['body'] == ('An agent asks for a password or key: '
                            'Site password is needed for the login step.')
    node.setattr(fleet_command, 'self_node_keys', lambda: dict(NODE_KEYS))
    push = consent_service.consent_prompt_push(
        {'user_id': OWNER, 'consent_type': 'credential', 'reason': '  '}, 'row-2')
    assert push['body'] == 'An agent asks for a password or key on MSI desktop 3f9a.'
    assert push['data']['body'] == push['body']


def test_a_credential_push_from_a_node_with_no_name_says_only_who(node, legs):
    """The scrub cannot run, so the node's name stays on the node
    (self_node_keys) and the text falls back to the plain one."""
    from security import edge_privacy

    def broken(text):
        raise RuntimeError('scrub down')
    node.setattr(edge_privacy, 'scrub_text', broken)
    legs.direct()
    _file_mailbox_ask()
    [(title, body, data)] = legs.fcm
    assert 'node_name' not in data
    assert _spelled([title, body, data]) == []
    who = title[:-len(' needs your OK')]
    assert body == who + ' asks for credential.'


def test_another_asks_push_still_gives_its_reason(legs):
    """A screen ask's push says why the agent asks, as it did: its reason
    alone, with no who-and-where in front."""
    from integrations.social.consent_service import ConsentService
    legs.direct()
    with db_session() as db:
        ConsentService.request_consent(db, OWNER, 'screen_capture',
                                       agent_id='42', reason='to read the invoice on screen')
    [(title, body, data)] = legs.fcm
    assert body == 'to read the invoice on screen' and data['body'] == body
    assert data['consent_type'] == 'screen_capture'
    assert {k: data.get(k) for k in NODE_KEYS} == NODE_KEYS


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
