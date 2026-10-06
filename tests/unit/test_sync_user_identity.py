"""#59 / #65: a user sync must verify its sender against a TRUSTED key, and a
synced profile never grants or removes authority.

Until 2026-09-14 the /api/social/auth/sync-user route verified a hive token
against a `node_public_key` taken FROM THE REQUEST BODY, so any caller could
create or overwrite ANY user, including role 'central', from the open
internet.  The route was fixed that day and deleted on 10-06: nothing ever
called it (0 calls in 52 days of central nginx logs, no client in any repo).
A user profile reaches a node through one ingress, /api/social/hierarchy/sync,
which checks the sender with discovery._verify_sync_sender.

This file pins:
  - _sender_signature_valid is strict and key-on-file only (never a body key);
  - _verify_sync_sender keeps hierarchy_sync's contract (delegates + mode escape);
  - hierarchy_sync applies a signed user sync from a known peer, and refuses an
    unsigned, forged or unknown sender under hard enforcement;
  - a synced profile never confers central/regional/admin/moderator, and never
    demotes a local privileged user.

    python -m pytest tests/unit/test_sync_user_identity.py --noconftest -q
"""
import json
import os
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

import integrations.social.discovery as disc


def _keypair():
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return priv, pub_hex


def _sign(priv, payload):
    """Sign the canonical {payload minus signature}, the surface the verifier
    checks (node_integrity.canonical_payload excludes 'signature')."""
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return priv.sign(canonical.encode()).hex()


class _Peer:
    def __init__(self, pk, node_id='n1'):
        self.public_key = pk
        self.node_id = node_id


class _FakeQ:
    def __init__(self, peer, prefix_peer):
        self._peer = peer
        self._prefix_peer = prefix_peer

    def filter_by(self, **kw):
        self._mode = 'exact'
        return self

    def filter(self, *a):
        self._mode = 'prefix'
        return self

    def first(self):
        return self._prefix_peer if getattr(self, '_mode', 'exact') == 'prefix' else self._peer


class _FakeDB:
    def __init__(self, peer=None, prefix_peer=None):
        self._peer = peer
        self._prefix_peer = prefix_peer

    def query(self, _m):
        return _FakeQ(self._peer, self._prefix_peer)


# ── _sender_signature_valid: strict, key-on-file only ──

def test_valid_signature_from_known_peer_passes():
    priv, pub = _keypair()
    payload = {'node_id': 'n1', 'user_data': {'user_id': 'u1', 'username': 'a'}}
    payload['signature'] = _sign(priv, payload)
    assert disc._sender_signature_valid(_FakeDB(_Peer(pub)), payload) is True


def test_no_signature_is_not_valid():
    assert disc._sender_signature_valid(
        _FakeDB(_Peer('PK')), {'node_id': 'n1', 'user_data': {}}) is False


def test_forged_signature_is_not_valid_regardless_of_mode():
    # A key the caller controls, but NOT the one on file for n1.
    priv, _ = _keypair()
    _, other_pub = _keypair()
    payload = {'node_id': 'n1', 'user_data': {'user_id': 'u1', 'username': 'a'}}
    payload['signature'] = _sign(priv, payload)
    # Peer on file has a DIFFERENT key → signature does not verify → strict False,
    # with NO enforcement-mode escape (unlike _verify_sync_sender).
    assert disc._sender_signature_valid(_FakeDB(_Peer(other_pub)), payload) is False


def test_unknown_node_is_not_valid():
    priv, _ = _keypair()
    payload = {'node_id': 'ghost', 'user_data': {'user_id': 'u1', 'username': 'a'}}
    payload['signature'] = _sign(priv, payload)
    assert disc._sender_signature_valid(_FakeDB(None), payload) is False


def test_tampered_payload_is_not_valid():
    priv, pub = _keypair()
    payload = {'node_id': 'n1', 'user_data': {'user_id': 'u1', 'username': 'a'}}
    payload['signature'] = _sign(priv, payload)
    payload['user_data'] = {'user_id': 'u1', 'username': 'TAMPERED'}
    assert disc._sender_signature_valid(_FakeDB(_Peer(pub)), payload) is False


def test_legacy_prefix_resolves_to_a_key_on_file():
    # An un-upgraded sender declares node_id = public_key[:16]; the strict
    # check still resolves to a key ALREADY ON FILE (never a body key).
    priv, pub = _keypair()
    prefix = pub[:16]
    payload = {'node_id': prefix, 'user_data': {'user_id': 'u1', 'username': 'a'}}
    payload['signature'] = _sign(priv, payload)
    db = _FakeDB(peer=None, prefix_peer=_Peer(pub, node_id='uuid-1'))
    assert disc._sender_signature_valid(db, payload) is True


# ── _verify_sync_sender: hierarchy_sync contract preserved (delegates + escape) ──

def test_verify_sync_sender_still_valid_signature_passes():
    priv, pub = _keypair()
    payload = {'node_id': 'n1', 'items': [{'id': 'x'}]}
    payload['signature'] = _sign(priv, payload)
    assert disc._verify_sync_sender(_FakeDB(_Peer(pub)), payload) is True


def test_verify_sync_sender_unsigned_soft_still_applies(monkeypatch):
    import security.master_key as mk
    monkeypatch.setattr(mk, 'get_enforcement_mode', lambda: 'soft')
    assert disc._verify_sync_sender(_FakeDB(None), {'node_id': 'n1', 'items': []}) is True


def test_verify_sync_sender_unsigned_hard_rejected(monkeypatch):
    import security.master_key as mk
    monkeypatch.setattr(mk, 'get_enforcement_mode', lambda: 'hard')
    assert disc._verify_sync_sender(_FakeDB(None), {'node_id': 'n1', 'items': []}) is False


# ── the live user-sync ingress (hierarchy_sync) + role strip ──

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('HEVOLVE_DB_PATH', ':memory:')
    from flask import Flask
    from integrations.social.models import Base, get_engine, get_db, PeerNode
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(disc.discovery_bp)
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    priv, pub = _keypair()
    db = get_db()
    try:
        db.add(PeerNode(node_id='node-1', url='http://x', public_key=pub))
        db.commit()
    finally:
        db.close()
    yield app.test_client(), priv
    Base.metadata.drop_all(engine)


def _post(client_priv, user_data, node_id='node-1', sign=True, priv_override=None):
    """A user sync the way a child node sends it: one 'sync_user' item in a
    batch signed over {items, node_id} (SyncEngine._signed_send_payload)."""
    client, priv = client_priv
    body = {'items': [{'id': 'item-' + str(user_data.get('user_id')),
                       'operation_type': 'sync_user', 'payload': user_data}],
            'node_id': node_id}
    if sign:
        body['signature'] = _sign(priv_override or priv, body)
    return client.post('/api/social/hierarchy/sync', json=body)


def _get_user(uid):
    from integrations.social.models import get_db, User
    db = get_db()
    try:
        return db.query(User).filter_by(id=uid).first()
    finally:
        db.close()


def _hard(monkeypatch):
    import security.master_key as mk
    monkeypatch.setattr(mk, 'get_enforcement_mode', lambda: 'hard')


def test_hard_enforcement_refuses_an_unsigned_user_sync(client, monkeypatch):
    _hard(monkeypatch)
    resp = _post(client, {'user_id': 'u1', 'username': 'a'}, sign=False)
    assert resp.status_code == 403
    assert _get_user('u1') is None


def test_hard_enforcement_refuses_a_forged_signature(client, monkeypatch):
    _hard(monkeypatch)
    other, _ = _keypair()
    resp = _post(client, {'user_id': 'u1', 'username': 'a'}, priv_override=other)
    assert resp.status_code == 403
    assert _get_user('u1') is None


def test_hard_enforcement_refuses_an_unknown_node(client, monkeypatch):
    _hard(monkeypatch)
    resp = _post(client, {'user_id': 'u1', 'username': 'a'}, node_id='ghost')
    assert resp.status_code == 403
    assert _get_user('u1') is None


def test_a_signed_user_sync_from_a_known_peer_lands(client, monkeypatch):
    _hard(monkeypatch)
    resp = _post(client, {'user_id': 'u1', 'username': 'alice'})
    assert resp.status_code == 200
    assert resp.get_json()['processed'] == ['item-u1']
    u = _get_user('u1')
    assert u is not None and u.username == 'alice'


@pytest.mark.parametrize('role', ['central', 'regional', 'admin', 'moderator'])
def test_a_synced_profile_never_confers_a_privileged_role(client, role):
    resp = _post(client, {'user_id': 'u1', 'username': 'a', 'role': role})
    assert resp.status_code == 200
    u = _get_user('u1')
    assert u.role not in ('central', 'regional', 'admin', 'moderator')


def test_a_non_privileged_role_still_syncs(client):
    resp = _post(client, {'user_id': 'u1', 'username': 'a', 'role': 'flat'})
    assert resp.status_code == 200
    assert _get_user('u1').role == 'flat'


@pytest.mark.parametrize('local_role', ['central', 'regional', 'admin', 'moderator'])
def test_a_sync_does_not_demote_a_local_privileged_user(client, local_role):
    # #65: a signed sync from a known peer sending role='flat' must NOT strip a
    # LOCAL admin/central/regional/moderator down to flat.  Authority is local;
    # a sync neither grants nor removes it.
    from integrations.social.models import get_db, User
    db = get_db()
    try:
        db.add(User(id='adm1', username='adm', display_name='adm',
                    role=local_role, user_type='human', api_token='t-adm'))
        db.commit()
    finally:
        db.close()
    resp = _post(client, {'user_id': 'adm1', 'username': 'adm', 'role': 'flat'})
    assert resp.status_code == 200
    assert _get_user('adm1').role == local_role  # unchanged, not demoted
