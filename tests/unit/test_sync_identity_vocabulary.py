"""Sync must speak the SAME node identity as the rest of the system.

Proven live 2026-08-08 against azurekong. SyncEngine stamped and sent
``node_id = get_public_key_hex()[:16]`` — a public-key PREFIX — while
PeerNode (and gossip, follows, bans, federation) key on the gossip UUID.
``_verify_sync_sender`` resolves the sender with
``PeerNode.filter_by(node_id=<declared>)``, so central searched for
'25cedaa441302f25' while its row for us was
'46329c87-cbb6-4ca1-bad5-816f6007b6a0'. No row -> no public_key -> hard
enforcement -> 403 on every signed batch, from every node, forever.
Central had the correct key stored on the UUID row the entire time.

Two independent failures from the one root cause:
  1. CROSS-NODE: 65 sync_queue rows dead with "Max retries exceeded: HTTP 403".
  2. LOCAL: the prefix changes when the keypair does, so 40 queued rows were
     stamped with three dead prefixes and the drain filter could never match
     them again — a queue orphaned against itself.

This is the one-name-two-vocabularies pattern
(memory/feedback_one_name_two_vocabularies.md).
"""
from unittest import mock

import pytest


UUID_ID = '46329c87-cbb6-4ca1-bad5-816f6007b6a0'
PUBKEY = '25cedaa441302f25387a912257c7bb85' + '0' * 32
LEGACY_ID = PUBKEY[:16]          # '25cedaa441302f25'


# ─── sender: one canonical identity ──────────────────────────────────────────

def test_canonical_node_id_is_the_gossip_uuid():
    from integrations.social.sync_engine import SyncEngine
    fake = mock.Mock()
    fake.node_id = UUID_ID
    with mock.patch('integrations.social.peer_discovery.gossip', fake):
        assert SyncEngine.canonical_node_id() == UUID_ID


def test_canonical_node_id_is_not_a_key_prefix():
    """The regression itself: a 16-hex key prefix must never be the identity
    while gossip can answer."""
    from integrations.social.sync_engine import SyncEngine
    fake = mock.Mock()
    fake.node_id = UUID_ID
    with mock.patch('integrations.social.peer_discovery.gossip', fake):
        nid = SyncEngine.canonical_node_id()
    assert nid != LEGACY_ID
    assert len(nid) != 16, 'identity regressed to a public-key prefix'


def test_canonical_node_id_falls_back_when_gossip_unavailable():
    """Degraded boot must still produce SOMETHING the receiver can resolve."""
    from integrations.social.sync_engine import SyncEngine
    fake = mock.Mock()
    fake.node_id = ''
    with mock.patch('integrations.social.peer_discovery.gossip', fake), \
         mock.patch('security.node_integrity.get_public_key_hex',
                    return_value=PUBKEY):
        assert SyncEngine.canonical_node_id() == LEGACY_ID


def test_queue_and_drain_agree_on_one_identity():
    """AST guard: neither the producer nor the drain may re-derive an identity
    of its own — both must call the single resolver. They disagreeing is what
    orphaned the queue."""
    import ast
    import inspect
    import textwrap
    import integrations.social.sync_engine as se
    from integrations.social.sync_engine import SyncEngine

    for fn_name in ('queue',):
        src = textwrap.dedent(inspect.getsource(getattr(SyncEngine, fn_name)))
        assert 'canonical_node_id' in src, f'{fn_name} bypasses the resolver'
        assert 'get_public_key_hex()[:16]' not in src, \
            f'{fn_name} still derives a key-prefix identity'

    whole = inspect.getsource(se)
    tree = ast.parse(whole)
    drains = [n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == '_do_sync_drain']
    assert drains, '_do_sync_drain not found'
    drain_src = ast.get_source_segment(whole, drains[0]) or ''
    assert 'canonical_node_id' in drain_src, \
        'drain bypasses the resolver — rows would orphan again'
    assert 'get_public_key_hex()[:16]' not in drain_src


# ─── receiver: resolves BOTH vocabularies during migration ───────────────────

def _peer(node_id, public_key):
    p = mock.Mock()
    p.node_id = node_id
    p.public_key = public_key
    return p


def _db_where(by_node_id=None, by_prefix=None):
    """db whose filter_by(node_id=...) and filter(startswith) return distinct
    rows, mirroring the real two-lookup shape."""
    db = mock.MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = by_node_id
    db.query.return_value.filter.return_value.first.return_value = by_prefix
    return db


def test_modern_uuid_sender_verifies():
    from integrations.social.discovery import _verify_sync_sender
    db = _db_where(by_node_id=_peer(UUID_ID, PUBKEY))
    with mock.patch('security.node_integrity.verify_json_signature',
                    return_value=True):
        assert _verify_sync_sender(
            db, {'items': [], 'node_id': UUID_ID, 'signature': 'ok'}) is True


def test_legacy_key_prefix_sender_is_resolved_not_rejected():
    """The live 403: node absent by node_id, but its public_key starts with
    the declared prefix. Must resolve and verify, not fail closed."""
    from integrations.social.discovery import _verify_sync_sender
    db = _db_where(by_node_id=None, by_prefix=_peer(UUID_ID, PUBKEY))
    with mock.patch('security.node_integrity.verify_json_signature',
                    return_value=True):
        assert _verify_sync_sender(
            db, {'items': [], 'node_id': LEGACY_ID, 'signature': 'sig'}) is True


def test_unknown_sender_still_fails_closed_in_hard_mode():
    """ZERO-REGRESSION PIN: the legacy branch must not become a bypass. An
    id matching no peer at all is still rejected under hard enforcement."""
    from integrations.social.discovery import _verify_sync_sender
    db = _db_where(by_node_id=None, by_prefix=None)
    with mock.patch('security.master_key.get_enforcement_mode',
                    return_value='hard'):
        assert _verify_sync_sender(
            db, {'items': [{'x': 1}], 'node_id': 'deadbeefdeadbeef',
                 'signature': 'sig'}) is False


def test_bad_signature_fails_even_when_peer_resolves():
    """Resolution is not authorisation — a found key with a bad signature is
    still rejected in hard mode."""
    from integrations.social.discovery import _verify_sync_sender
    db = _db_where(by_node_id=_peer(UUID_ID, PUBKEY))
    with mock.patch('security.node_integrity.verify_json_signature',
                    return_value=False), \
         mock.patch('security.master_key.get_enforcement_mode',
                    return_value='hard'):
        assert _verify_sync_sender(
            db, {'items': [{'x': 1}], 'node_id': UUID_ID,
                 'signature': 'bad'}) is False


def test_non_hex_id_does_not_hit_the_prefix_lookup():
    """The legacy branch is gated to a 16-char hex shape so arbitrary strings
    can't be used to fish for a peer by prefix."""
    from integrations.social.discovery import _verify_sync_sender
    db = _db_where(by_node_id=None, by_prefix=_peer(UUID_ID, PUBKEY))
    with mock.patch('security.master_key.get_enforcement_mode',
                    return_value='hard'):
        assert _verify_sync_sender(
            db, {'items': [{'x': 1}], 'node_id': 'not-hex-at-all!!',
                 'signature': 'sig'}) is False
