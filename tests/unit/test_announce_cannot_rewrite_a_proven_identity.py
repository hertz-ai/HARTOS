"""Only a node can change what the network knows about it (#140 part B).

4b0b005eb binds a challenge answer to the key STORED for its target. That
binding is only as strong as the writer of the stored key, and the writer was
"whoever spoke last": _merge_peer copied the announce's public_key onto an
existing row, and a RELAYED hint (a third party republishing a row, with no
signature at all) could set it too, and move the row's url. So anyone could:
  1. announce as node X with their own key, or relay a row for X, which
     rewrote X's stored key and url;
  2. answer the challenges now delivered to their address, signed with their
     key, and pass as X (or, with A's first draft, fail and frame X).

The contract these tests pin:
  a row with a stored key changes only through a DIRECT announce whose
  signature verifies against THAT key. A relayed hint, an unsigned announce, or
  an announce signed by any other key changes nothing: not the key, not the
  url, not last_seen.
  a row with no stored key takes its first key from a direct signed announce
  only, never from hearsay, so a relayer cannot plant X's key before X speaks.

The pair (cc12): 4b0b005eb leaves a proven node's 'verified' untouched while
its address answers with a foreign key, which is right against framing only
because this writer stops a foreign announce from moving the address.

Real in-memory sqlite, real Ed25519 keys, the real gossip._merge_peer.

    pytest tests/unit/test_announce_cannot_rewrite_a_proven_identity.py -q
"""
import os
import sys
from unittest.mock import patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

X = 'node-x-0001'
HOME = 'http://10.9.9.9:5000'
ELSEWHERE = 'http://10.6.6.6:5000'


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return priv, pub


def _signed(priv, pub, url):
    from security.node_integrity import canonical_payload
    payload = {'node_id': X, 'url': url, 'name': 'x', 'version': '1.0.0',
               'agent_count': 1, 'post_count': 0, 'timestamp': 1,
               'public_key': pub}
    payload['signature'] = priv.sign(
        canonical_payload(payload, exclude=('signature',))).hex()
    return payload


@pytest.fixture
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from integrations.social.models import Base
    engine = create_engine('sqlite://', poolclass=StaticPool,
                           connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def _merge(db, payload, relayed=False):
    from integrations.social.peer_discovery import gossip
    with patch('security.master_key.get_enforcement_mode', return_value='soft'), \
            patch.object(gossip, '_observed_url_for', return_value=''):
        gossip._merge_peer(db, dict(payload), relayed=relayed)
    db.commit()


def _row(db):
    from integrations.social.models import PeerNode
    return db.query(PeerNode).filter_by(node_id=X).one()


def _seed_proven(db, pub):
    from datetime import datetime, timedelta
    from integrations.social.models import PeerNode
    db.add(PeerNode(node_id=X, url=HOME, name='x', status='active',
                    public_key=pub, integrity_status='verified',
                    last_seen=datetime.utcnow() - timedelta(minutes=5)))
    db.commit()
    return _row(db).last_seen


def test_the_node_itself_still_updates_its_own_row(db):
    priv, pub = _keypair()
    _seed_proven(db, pub)
    _merge(db, _signed(priv, pub, ELSEWHERE))
    row = _row(db)
    assert row.url == ELSEWHERE
    assert row.public_key == pub


def test_an_announce_signed_by_another_key_changes_nothing(db):
    """The hijack: announce as X with your own key, pointing X at yourself."""
    _, x_pub = _keypair()
    before = _seed_proven(db, x_pub)
    evil_priv, evil_pub = _keypair()
    _merge(db, _signed(evil_priv, evil_pub, ELSEWHERE))
    row = _row(db)
    assert row.public_key == x_pub, 'a foreign announce replaced the stored key'
    assert row.url == HOME, "a foreign announce redirected X's challenges"
    assert row.last_seen == before
    assert row.integrity_status == 'verified'


def test_a_relayed_hint_cannot_move_a_keyed_row(db):
    _, x_pub = _keypair()
    _seed_proven(db, x_pub)
    _, other_pub = _keypair()
    hint = {'node_id': X, 'url': ELSEWHERE, 'public_key': other_pub}
    _merge(db, hint, relayed=True)
    row = _row(db)
    assert row.public_key == x_pub
    assert row.url == HOME


def test_an_unsigned_announce_cannot_move_a_keyed_row(db):
    _, x_pub = _keypair()
    _seed_proven(db, x_pub)
    _merge(db, {'node_id': X, 'url': ELSEWHERE, 'name': 'x', 'version': '1.0.0'})
    row = _row(db)
    assert row.url == HOME
    assert row.public_key == x_pub


def test_a_first_key_comes_from_the_node_not_from_hearsay(db):
    """A relayer cannot plant X's key before X speaks for itself."""
    _, planted = _keypair()
    _merge(db, {'node_id': X, 'url': HOME, 'public_key': planted}, relayed=True)
    assert (_row(db).public_key or '') == ''
    priv, pub = _keypair()
    _merge(db, _signed(priv, pub, HOME))
    assert _row(db).public_key == pub


def test_a_keyless_row_takes_its_key_from_its_first_signed_announce(db):
    from integrations.social.models import PeerNode
    db.add(PeerNode(node_id=X, url=HOME, name='x', status='active',
                    public_key='', integrity_status='unverified'))
    db.commit()
    priv, pub = _keypair()
    _merge(db, _signed(priv, pub, HOME))
    assert _row(db).public_key == pub




def test_binding_a_first_key_restarts_standing_and_keeps_the_record(db):
    """B2: a keyless row was never proven by a key (4b0b005eb), so whatever
    standing it carried does not survive a key being bound to it; its fraud
    history does (#141)."""
    from integrations.social.models import PeerNode
    db.add(PeerNode(node_id=X, url=HOME, name='x', status='active',
                    public_key='', integrity_status='verified', fraud_score=40.0))
    db.commit()
    priv, pub = _keypair()
    _merge(db, _signed(priv, pub, HOME))
    row = _row(db)
    assert row.public_key == pub
    assert row.integrity_status == 'unverified'
    assert row.fraud_score == 40.0


# -- the node that is refused: told why, never re-identified by the reply --

def test_a_direct_foreign_signed_announce_is_told_key_conflict(db):
    from integrations.social.peer_discovery import gossip, KEY_CONFLICT
    _, x_pub = _keypair()
    _seed_proven(db, x_pub)
    priv2, pub2 = _keypair()
    reasons = []
    with patch('security.master_key.get_enforcement_mode', return_value='soft'), \
            patch.object(gossip, '_observed_url_for', return_value=''):
        gossip._merge_peer(db, _signed(priv2, pub2, HOME), reasons=reasons)
    assert reasons and reasons[0].startswith(KEY_CONFLICT)


def test_hearsay_and_unsigned_announces_are_not_told_anything(db):
    """Only a node that PROVES it holds another key learns of the conflict."""
    from integrations.social.peer_discovery import gossip
    _, x_pub = _keypair()
    _seed_proven(db, x_pub)
    for payload, relayed in (({'node_id': X, 'url': ELSEWHERE}, False),
                             ({'node_id': X, 'url': ELSEWHERE,
                               'public_key': x_pub}, True)):
        reasons = []
        with patch('security.master_key.get_enforcement_mode', return_value='soft'), \
                patch.object(gossip, '_observed_url_for', return_value=''):
            gossip._merge_peer(db, payload, reasons=reasons, relayed=relayed)
        assert reasons == []


SEED = 'https://central.example'
PLAIN_SEED = 'http://seed.plain'
SEED_ID = 'seed-node-0001'
LEGACY_ID = '46329c87-cbb6-4ca1-bad5-816f6007b6a0'


class _Resp:
    def __init__(self, body, url=SEED + '/api/social/peers/announce'):
        self._body, self.url = body, url

    def json(self):
        return self._body


def _conflict(held_pub, **extra):
    from integrations.social.peer_discovery import KEY_CONFLICT
    body = {'success': True, 'accepted': False, 'node_id': SEED_ID,
            'reason': (f'{KEY_CONFLICT}: node_id 46329c87 is held under another '
                       f'key (held_key_fp={held_pub[:16]})')}
    body.update(extra)
    return body


def _signed_by(priv, pub, body):
    from security.node_integrity import canonical_payload
    body = dict(body, public_key=pub)
    body['signature'] = priv.sign(
        canonical_payload(body, exclude=('signature',))).hex()
    return body


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """Keys in data/ AND agent_data/, a legacy node_id.json, and this process
    running on agent_data/: the owner's desktop shape, all in tmp_path."""
    import types
    import core.platform_paths as pp
    import security.node_integrity as ni
    root = tmp_path / 'root'
    for var in ('HEVOLVE_KEY_DIR', 'HEVOLVE_DB_PATH'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(pp, 'get_identity_data_dir', lambda: str(root))
    pubs = {}
    for sub in ('data', 'agent_data'):
        d = root / sub
        d.mkdir(parents=True)
        monkeypatch.setattr(ni, '_KEY_DIR', str(d))
        monkeypatch.setattr(ni, '_private_key', None)
        monkeypatch.setattr(ni, '_public_key', None)
        pubs[sub] = ni.get_public_key_hex()
    (root / 'node_id.json').write_text('{"node_id": "%s"}' % LEGACY_ID)
    # This process keeps its DB in agent_data/, so the resolver itself picks
    # agent_data/ (as a process with that DB_PATH would at import) and the
    # signing and x25519 keys start in the same, wrong, dir.
    monkeypatch.setenv('HEVOLVE_DB_PATH', str(root / 'agent_data' / 'hevolve.db'))
    assert ni._resolve_key_dir() == str(root / 'agent_data')
    monkeypatch.setattr(ni, '_KEY_DIR', str(root / 'agent_data'))
    monkeypatch.setattr(ni, '_key_dir_override', None)
    monkeypatch.setattr(ni, '_private_key', None)
    monkeypatch.setattr(ni, '_public_key', None)
    monkeypatch.setattr(ni, 'identity_state', 'legacy_provisional')
    import security.channel_encryption as ce
    ce.reset_keypair_cache()
    return types.SimpleNamespace(root=root, pubs=pubs, ni=ni)


def _node(monkeypatch, held_seed_key=''):
    from integrations.social.peer_discovery import GossipProtocol
    node = GossipProtocol.__new__(GossipProtocol)
    node.seed_peers = [SEED, PLAIN_SEED]
    node.node_id = LEGACY_ID
    node.node_name = f'hevolve-{LEGACY_ID[:8]}'
    node._seed_key_held = lambda nid: held_seed_key
    return node


@pytest.fixture
def minted(monkeypatch):
    import security.node_integrity as ni
    calls = []
    monkeypatch.setattr(ni, 'take_new_node_identity',
                        lambda reason: calls.append(reason) or 'new-uuid-5678')
    return calls


def test_a_key_already_on_this_machine_is_recovered_not_replaced(
        machine, monkeypatch, minted):
    """A1, the desktop case: the seed holds 46329c87 under the data/ key while
    this process runs on agent_data/.  Same id, no mint, and signing AND
    x25519 both move to data/ (one cache owner)."""
    import security.channel_encryption as ce
    x_before = ce.get_x25519_public_hex()      # cached from agent_data/
    assert (machine.root / 'agent_data' / 'node_x25519_public.key').exists()
    node = _node(monkeypatch)
    node._consume_key_reply(SEED, _Resp(_conflict(machine.pubs['data'])), 'n1')

    assert node.node_id == LEGACY_ID and minted == []
    assert machine.ni._KEY_DIR == str(machine.root / 'data')
    assert machine.ni.get_public_key_hex() == machine.pubs['data']
    x_after = ce.get_x25519_public_hex()
    assert (machine.root / 'data' / 'node_x25519_public.key').exists()
    assert x_after != x_before, 'encryption still uses the old dir after the switch'
    assert not node._blocked_for_key_conflict(SEED)


def test_an_explicit_key_dir_is_never_switched_away_from(
        machine, monkeypatch, minted):
    monkeypatch.setenv('HEVOLVE_KEY_DIR', str(machine.root / 'agent_data'))
    node = _node(monkeypatch)
    node._consume_key_reply(SEED, _Resp(_conflict(machine.pubs['data'])), 'n1')
    assert machine.ni._KEY_DIR == str(machine.root / 'agent_data')
    assert len(minted) == 1   # A1 skipped; A2 applied (verified https, no held key)


def test_a_verified_https_conflict_with_no_local_key_takes_a_new_identity(
        machine, monkeypatch, minted):
    """A2: a reinstall.  No local key matches, the reply came over verified
    HTTPS from the seed's own host, and this node holds no key for the seed."""
    node = _node(monkeypatch)
    node._consume_key_reply(SEED, _Resp(_conflict('ab' * 32)), 'n1')
    assert node.node_id == 'new-uuid-5678' and len(minted) == 1
    assert node.node_name == 'hevolve-new-uuid'


def test_an_unauthenticated_conflict_never_re_identifies_and_retries_later(
        machine, monkeypatch, minted, caplog):
    import integrations.social.peer_discovery as pd
    node = _node(monkeypatch)
    node._consume_key_reply(PLAIN_SEED, _Resp(_conflict('ab' * 32),
                                              url=PLAIN_SEED + '/x'), 'n1')

    assert node.node_id == LEGACY_ID and minted == []
    assert 'unverifiable key conflict' in caplog.text
    monkeypatch.setattr(pd, 'pooled_post', lambda *a, **k: pytest.fail('announced'))
    assert node._announce_to_peer(PLAIN_SEED) is False
    node._key_conflict_until[PLAIN_SEED] = 0      # the backoff has passed
    assert node._blocked_for_key_conflict(PLAIN_SEED) is False


def test_a_conflict_redirected_off_the_seed_is_not_authenticated(
        machine, monkeypatch, minted):
    node = _node(monkeypatch)
    for final in ('http://central.example/api/social/peers/announce',
                  'https://evil.example/api/social/peers/announce'):
        node._consume_key_reply(SEED, _Resp(_conflict('ab' * 32), url=final), 'n1')
        node._key_conflict_until = {}
    assert node.node_id == LEGACY_ID and minted == []


def test_with_the_seeds_key_held_only_a_fresh_signed_reply_counts(
        machine, monkeypatch, minted):
    priv, pub = _keypair()
    node = _node(monkeypatch, held_seed_key=pub)
    now = {'node_id': LEGACY_ID, 'nonce': 'n-now'}

    node._consume_key_reply(SEED, _Resp(_conflict('ab' * 32, reply_to=now)), 'n-now')
    assert minted == [], 'an unsigned reply counted although the seed key is held'

    for reply_to, why in (({'node_id': LEGACY_ID, 'nonce': 'n-old'}, 'a replayed reply'),
                          ({'node_id': 'someone-else', 'nonce': 'n-now'},
                           "a reply to another node's announce")):
        node._key_conflict_until = {}
        node._consume_key_reply(SEED, _Resp(_signed_by(
            priv, pub, _conflict('ab' * 32, reply_to=reply_to))), 'n-now')
        assert minted == [], f'{why} counted'

    node._key_conflict_until = {}
    forged = _conflict('ab' * 32, reply_to=now, public_key=pub, signature='00' * 64)
    node._consume_key_reply(PLAIN_SEED, _Resp(forged, url=PLAIN_SEED + '/x'), 'n-now')
    assert minted == [], 'a reply with a forged signature counted'

    node._key_conflict_until = {}
    fresh = _signed_by(priv, pub, _conflict('ab' * 32, reply_to=now))
    node._consume_key_reply(PLAIN_SEED, _Resp(fresh, url=PLAIN_SEED + '/x'), 'n-now')
    assert node.node_id == 'new-uuid-5678', 'a fresh signed reply over plain http did not count'


def test_a_stranger_conflict_changes_nothing(machine, monkeypatch, minted):
    node = _node(monkeypatch)
    stranger = 'http://10.1.1.1:5000'
    node._consume_key_reply(stranger, _Resp(_conflict(machine.pubs['data']),
                                            url=stranger + '/x'), 'n1')
    assert node.node_id == LEGACY_ID and minted == []
    assert machine.ni._KEY_DIR == str(machine.root / 'agent_data')
    assert not node._blocked_for_key_conflict(stranger)


def test_a_seeds_acceptance_confirms_a_provisional_legacy_id(monkeypatch):
    import security.node_integrity as ni
    confirmed = []
    monkeypatch.setattr(ni, 'confirm_node_identity',
                        lambda nid: confirmed.append(nid) or nid)
    accepted = _Resp({'success': True, 'accepted': True})

    monkeypatch.setattr(ni, 'identity_state', 'legacy_provisional')
    _node(monkeypatch)._consume_key_reply('http://10.1.1.1:5000', accepted, 'n')
    assert confirmed == [], "a stranger's acceptance confirmed an identity"

    _node(monkeypatch)._consume_key_reply(SEED, accepted, 'n')
    assert confirmed == [LEGACY_ID]

    monkeypatch.setattr(ni, 'identity_state', 'recorded')
    _node(monkeypatch)._consume_key_reply(SEED, accepted, 'n')
    assert confirmed == [LEGACY_ID]


def test_the_seed_signs_its_reply_and_binds_it_to_the_announce():
    """Server half: the reply echoes the announcer's node_id and nonce and is
    signed by this node's key, so neither can be swapped or the reply reused."""
    from flask import Flask
    from integrations.social.discovery import discovery_bp
    from integrations.social.peer_discovery import gossip
    from security.node_integrity import get_public_key_hex, verify_json_signature
    app = Flask(__name__)
    app.register_blueprint(discovery_bp)

    # A node announcing itself is refused before any DB query.
    resp = app.test_client().post('/api/social/peers/announce', json={
        'node_id': gossip.node_id, 'url': 'http://192.0.2.10:5000', 'nonce': 'n-xyz'})
    body = resp.get_json()

    assert body['reply_to'] == {'node_id': gossip.node_id, 'nonce': 'n-xyz'}
    assert body['public_key'] == get_public_key_hex()
    assert verify_json_signature(body['public_key'], body, body['signature'])
    tampered = dict(body, reason='key_conflict: forged')
    assert not verify_json_signature(body['public_key'], tampered, body['signature'])
