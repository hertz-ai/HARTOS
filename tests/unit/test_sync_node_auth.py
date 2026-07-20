"""P4: hierarchy_sync node-identity gate. The unauthenticated up-ingress was an
IDOR — any caller could POST a batch claiming to be any node. Now the node SIGNS
its batch and central VERIFIES against the node's registered PeerNode.public_key
before applying. Valid signature always applies; unsigned/invalid applies only
outside 'hard' enforcement (non-breaking migration). Behavioural; boundary mocked.

    python -m pytest tests/unit/test_sync_node_auth.py --noconftest -q
"""
from unittest.mock import patch

import integrations.social.sync_engine as se
import integrations.social.discovery as disc


# ── NODE SIDE: sign the batch ──
def test_node_signs_batch():
    with patch('security.node_integrity.sign_json_payload', return_value='SIG') as s:
        out = se.SyncEngine._signed_send_payload('n1', [{'id': 'x'}])
    assert out['node_id'] == 'n1' and out['items'] == [{'id': 'x'}]
    assert out['signature'] == 'SIG'
    assert s.call_count == 1                         # the batch was signed once


def test_node_signing_is_best_effort():
    with patch('security.node_integrity.sign_json_payload',
               side_effect=Exception('no key')):
        out = se.SyncEngine._signed_send_payload('n1', [])
    assert 'signature' not in out                   # failed signing → unsigned


# ── CENTRAL SIDE: verify the sender ──
class _Peer:
    def __init__(self, pk):
        self.public_key = pk


class _FakeQ:
    def __init__(self, peer):
        self._peer = peer

    def filter_by(self, **kw):
        return self

    def first(self):
        return self._peer


class _FakeDB:
    def __init__(self, peer):
        self._peer = peer

    def query(self, _m):
        return _FakeQ(self._peer)


def test_valid_signature_applies():
    db = _FakeDB(_Peer('PK'))
    with patch('security.node_integrity.verify_json_signature', return_value=True) as v:
        ok = disc._verify_sync_sender(
            db, {'node_id': 'n1', 'signature': 'sig', 'items': []})
    assert ok is True
    assert v.call_args.args[0] == 'PK'              # verified against node's key


def test_invalid_signature_hard_mode_rejected():
    db = _FakeDB(_Peer('PK'))
    with patch('security.node_integrity.verify_json_signature', return_value=False), \
            patch('security.master_key.get_enforcement_mode', return_value='hard'):
        ok = disc._verify_sync_sender(
            db, {'node_id': 'n1', 'signature': 'bad', 'items': []})
    assert ok is False                              # fail-closed in hard mode


def test_invalid_signature_soft_mode_applies():
    db = _FakeDB(_Peer('PK'))
    with patch('security.node_integrity.verify_json_signature', return_value=False), \
            patch('security.master_key.get_enforcement_mode', return_value='soft'):
        ok = disc._verify_sync_sender(
            db, {'node_id': 'n1', 'signature': 'bad', 'items': []})
    assert ok is True                               # migration path (logged)


def test_unsigned_hard_mode_rejected():
    db = _FakeDB(None)
    with patch('security.master_key.get_enforcement_mode', return_value='hard'):
        ok = disc._verify_sync_sender(db, {'node_id': 'n1', 'items': []})
    assert ok is False


def test_unsigned_off_mode_applies():
    db = _FakeDB(None)
    with patch('security.master_key.get_enforcement_mode', return_value='off'):
        ok = disc._verify_sync_sender(db, {'node_id': 'n1', 'items': []})
    assert ok is True


def test_unknown_node_no_registered_key_hard_rejected():
    db = _FakeDB(None)            # node not in registry → no public_key
    with patch('security.node_integrity.verify_json_signature', return_value=True), \
            patch('security.master_key.get_enforcement_mode', return_value='hard'):
        ok = disc._verify_sync_sender(
            db, {'node_id': 'ghost', 'signature': 'sig', 'items': []})
    assert ok is False                              # no key to verify against


def test_undeterminable_enforcement_mode_fails_closed():
    # M1 (review): if get_enforcement_mode() raises, an unverified batch must be
    # REJECTED — this module fails closed, not open.
    db = _FakeDB(None)
    with patch('security.master_key.get_enforcement_mode',
               side_effect=Exception('cannot read')):
        ok = disc._verify_sync_sender(db, {'node_id': 'n1', 'items': []})
    assert ok is False


def test_real_signature_round_trip():
    # N3 (review): non-mocked crypto — a batch signed with a real Ed25519 key
    # verifies against that key (catches canonicalization drift the mocked tests
    # hide); a tampered batch is rejected in hard mode.
    import json
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    payload = {'items': [{'id': 'x'}], 'node_id': 'n1'}
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    payload['signature'] = priv.sign(canonical.encode()).hex()
    db = _FakeDB(_Peer(pub_hex))
    assert disc._verify_sync_sender(db, payload) is True
    payload['items'] = [{'id': 'TAMPERED'}]          # stale signature now
    with patch('security.master_key.get_enforcement_mode', return_value='hard'):
        assert disc._verify_sync_sender(db, payload) is False
