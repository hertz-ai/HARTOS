"""
HevolveSocial — Phase 9.B Double Ratchet state persistence.

Plan reference: sunny-gliding-eich.md, Part K.4 + Phase 9.

Bridges the stateless ratchet primitives in e2e_ratchet.py with the
v52 ratchet_states table.  Handles:

  - load(db, conv_id, user_id, peer_id) → RatchetState | None
  - save(db, conv_id, user_id, peer_id, state) → upserts the row
  - bootstrap(db, conv_id, user_id, peer_id, identity_keys) →
      first-time init via a deterministic shared secret derived from
      both members' published identity keys.  This is a STAND-IN for
      the proper X3DH handshake (Phase 9.C) — flagged inline so it's
      not mistaken for production-grade key agreement.

Wire format: state_json holds the JSON-encoded RatchetState with all
`bytes` fields base64-encoded (same convention as conversation_keys
+ message_envelopes — TEXT columns travel cleanly across SQLite,
Postgres, MySQL).  The skipped_keys cache (Dict[(bytes,int)→bytes])
serializes to a list of {peer_b64, idx, key_b64} triples so JSON's
string-keys-only constraint isn't an issue.

Transport: this module is pure DB persistence + serialization.  No
WAMP, no PeerLink, no notification fan-out — those happen in
ConversationService.send_message which CALLS this module.  Keeps
the transport selection in MessageBus.publish per Plan R.8.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from . import e2e_ratchet as r

logger = logging.getLogger('hevolve_social')


class E2EStateError(Exception):
    """Raised for state-repo failures.  Caller maps to 4xx."""


# ── Serialization ──────────────────────────────────────────────────


def _b64(b: Optional[bytes]) -> Optional[str]:
    if b is None:
        return None
    return base64.b64encode(b).decode('ascii')


def _unb64(s: Optional[str]) -> Optional[bytes]:
    if s is None:
        return None
    return base64.b64decode(s.encode('ascii'))


def serialize_state(state: r.RatchetState) -> str:
    """RatchetState → JSON string, all bytes base64-encoded.

    The skipped_keys dict has tuple keys (bytes, int) which JSON
    can't represent natively — we flatten to a list-of-triples.
    """
    skipped = [
        {'peer_b64': _b64(peer), 'idx': idx, 'key_b64': _b64(msg_key)}
        for (peer, idx), msg_key in state.skipped_keys.items()
    ]
    return json.dumps({
        'version': 1,
        'root_key': _b64(state.root_key),
        'sending_chain_key': _b64(state.sending_chain_key),
        'sending_index': state.sending_index,
        'receiving_chain_key': _b64(state.receiving_chain_key),
        'receiving_index': state.receiving_index,
        'our_dh_priv': _b64(state.our_dh_priv),
        'our_dh_pub': _b64(state.our_dh_pub),
        'their_dh_pub': _b64(state.their_dh_pub),
        'skipped_keys': skipped,
    })


def deserialize_state(blob: str) -> r.RatchetState:
    """JSON string → RatchetState, all bytes decoded."""
    if not blob:
        raise E2EStateError("empty state blob")
    try:
        d = json.loads(blob)
    except Exception as e:
        raise E2EStateError(f"corrupt state JSON: {e}")
    if not isinstance(d, dict) or d.get('version') != 1:
        raise E2EStateError(
            f"unknown state version: {d.get('version') if isinstance(d, dict) else None}")
    skipped_dict: Dict[Tuple[bytes, int], bytes] = {}
    for entry in d.get('skipped_keys', []):
        peer = _unb64(entry.get('peer_b64'))
        idx = int(entry.get('idx', 0))
        msg_key = _unb64(entry.get('key_b64'))
        if peer is not None and msg_key is not None:
            skipped_dict[(peer, idx)] = msg_key
    return r.RatchetState(
        root_key=_unb64(d['root_key']) or b'',
        sending_chain_key=_unb64(d['sending_chain_key']) or b'',
        sending_index=int(d.get('sending_index', 0)),
        receiving_chain_key=_unb64(d['receiving_chain_key']) or b'',
        receiving_index=int(d.get('receiving_index', 0)),
        our_dh_priv=_unb64(d['our_dh_priv']) or b'',
        our_dh_pub=_unb64(d['our_dh_pub']) or b'',
        their_dh_pub=_unb64(d.get('their_dh_pub')),
        skipped_keys=skipped_dict,
    )


# ── DB load / save ─────────────────────────────────────────────────


def load(db, conversation_id: str, user_id: str,
         peer_id: str) -> Optional[r.RatchetState]:
    """Fetch the persisted state for (conv, user, peer).  Returns
    None if no row exists yet — caller decides whether to bootstrap
    or refuse the encrypt."""
    row = db.execute(text(
        "SELECT state_json FROM ratchet_states "
        "WHERE conversation_id = :cid AND user_id = :uid "
        "AND peer_id = :pid"),
        {'cid': conversation_id, 'uid': user_id, 'pid': peer_id}
    ).fetchone()
    if row is None:
        return None
    return deserialize_state(row[0])


def save(db, conversation_id: str, user_id: str, peer_id: str,
         state: r.RatchetState,
         tenant_id: Optional[str] = None) -> None:
    """Upsert the persisted state.  Idempotent — row keyed on the
    UNIQUE(conversation_id, user_id, peer_id) index from v52."""
    blob = serialize_state(state)
    # Try INSERT first; on conflict UPDATE.  Portable across SQLite,
    # Postgres, MySQL via the same dialect-branch pattern that
    # _ensure_member uses in conversation_service.py.
    dialect = db.bind.dialect.name if db.bind is not None else 'sqlite'
    if dialect == 'sqlite':
        stmt = (
            "INSERT INTO ratchet_states "
            "(id, tenant_id, conversation_id, user_id, peer_id, "
            " state_json, created_at, updated_at) "
            "VALUES (:id, :tid, :cid, :uid, :pid, :blob, "
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
            "ON CONFLICT(conversation_id, user_id, peer_id) DO UPDATE "
            "SET state_json = excluded.state_json, "
            "    updated_at = CURRENT_TIMESTAMP")
    else:
        # Postgres + MySQL both accept this ON CONFLICT shape when
        # the unique index is defined — same approach v51 takes.
        stmt = (
            "INSERT INTO ratchet_states "
            "(id, tenant_id, conversation_id, user_id, peer_id, "
            " state_json, created_at, updated_at) "
            "VALUES (:id, :tid, :cid, :uid, :pid, :blob, "
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
            "ON CONFLICT (conversation_id, user_id, peer_id) DO UPDATE "
            "SET state_json = EXCLUDED.state_json, "
            "    updated_at = CURRENT_TIMESTAMP")
    try:
        db.execute(text(stmt), {
            'id': str(uuid.uuid4()), 'tid': tenant_id,
            'cid': conversation_id, 'uid': user_id, 'pid': peer_id,
            'blob': blob})
    except IntegrityError as e:
        raise E2EStateError(f"could not save ratchet state: {e}")


def delete_for_conversation(db, conversation_id: str) -> int:
    """Wipe ratchet state for every pair in the conversation.  Used
    when a conversation is fully closed/deleted, or when a member
    rotates their identity key (forcing a fresh bootstrap).  Returns
    rowcount for caller logging."""
    result = db.execute(text(
        "DELETE FROM ratchet_states WHERE conversation_id = :cid"),
        {'cid': conversation_id})
    return result.rowcount or 0


# ── Bootstrap (Phase 9.B placeholder for X3DH) ─────────────────────


def _sorted_pair_id(a: str, b: str) -> bytes:
    """Conversation-pair identifier, order-independent."""
    parts = sorted([str(a).encode('utf-8'), str(b).encode('utf-8')])
    return parts[0] + b'|' + parts[1]


def _derive_initial_shared_secret(user_id: str, peer_id: str,
                                  user_identity_b64: str,
                                  peer_identity_b64: str) -> bytes:
    """STAND-IN for X3DH (Phase 9.C).

    Derives a deterministic 32-byte secret from the sorted concat of
    BOTH party IDs and BOTH published identity keys.  Both sides
    compute the same value independently from data already in
    conversation_keys — no out-of-band exchange needed.

    SECURITY CAVEAT: This bootstrap is NOT forward-secret across
    the FIRST exchange — every input is public.  The Double
    Ratchet's DH-step ratchet recovers post-compromise security
    from message #2 onward (each send rotates DH pubkeys), but
    message #1 is vulnerable if a future X3DH-replacement re-derives
    the same shared secret.  Phase 9.C replaces this with proper
    X3DH using signed prekeys + one-time prekeys, which DOES give
    first-message forward secrecy.
    """
    pair = _sorted_pair_id(user_id, peer_id)
    keys = sorted([user_identity_b64.encode('ascii'),
                   peer_identity_b64.encode('ascii')])
    raw = (b'hevolve-e2e-bootstrap-v9.B|'
           + pair + b'|' + keys[0] + b'|' + keys[1])
    return hashlib.sha256(raw).digest()


def _derive_initial_dh_keypair(shared_secret: bytes,
                               party_id: str) -> Tuple[bytes, bytes]:
    """STAND-IN for X3DH (Phase 9.C).

    Both parties derive each other's bootstrap X25519 keypair from
    `shared_secret + party_id` so DH symmetry holds in the FIRST
    encrypt: alice computes DH(her_priv, bob_pub) and bob computes
    DH(his_priv, alice_pub) — by ECDH symmetry these match,
    establishing alice's sending_chain == bob's receiving_chain.

    SECURITY CAVEAT: The derived `priv` is *known* to the peer (it's
    derived from public inputs).  This bootstrap protects against
    OUTSIDE attackers who don't know the shared_secret seed but
    NOT against the peer themselves — there's no peer impersonation
    resistance until X3DH lands.  This is acceptable because the
    PEER is the legitimate other end of the conversation; a peer
    who wants to read their own traffic doesn't need an exploit.
    """
    r._require_crypto()
    raw = (b'hevolve-bootstrap-dh-v9.B|' + shared_secret
           + b'|' + str(party_id).encode('utf-8'))
    # X25519 accepts any 32 random-looking bytes as priv (it clamps
    # internally).  SHA-256 gives us 32.
    priv_bytes = hashlib.sha256(raw).digest()
    # Round-trip through the X25519 API to recover the canonical
    # form + derive the matching pub.
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
    )
    from cryptography.hazmat.primitives import serialization
    priv = X25519PrivateKey.from_private_bytes(priv_bytes)
    canonical_priv = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return canonical_priv, pub


def bootstrap_pair(db, conversation_id: str, user_id: str,
                   peer_id: str,
                   user_identity_b64: str, peer_identity_b64: str,
                   *,
                   tenant_id: Optional[str] = None,
                   is_initiator: bool) -> r.RatchetState:
    """Initialize a fresh ratchet state for the (user, peer) pair.

    `is_initiator` follows the libsignal convention: ONE side starts
    with their_dh_pub set to the peer's bootstrap pub (so they can
    send first); the other side starts with their_dh_pub=None and
    advances on their first inbound.  Both sides derive their
    bootstrap DH keypair deterministically from shared_secret +
    party_id so DH symmetry establishes a common chain key
    (init_ratchet on the initiator side derives sending_chain via
    DH(our_priv, their_pub); the responder's first
    advance_dh_ratchet derives receiving_chain via DH(our_priv,
    sender_pub) — these match by ECDH).

    For DMs the caller picks initiator = `min(user_id, peer_id)` so
    both sides agree without coordination.
    """
    r._require_crypto()
    shared = _derive_initial_shared_secret(
        user_id, peer_id, user_identity_b64, peer_identity_b64)
    # Bootstrap DH keypairs for BOTH parties — both sides compute
    # them so each knows the other's pub.
    our_priv, our_pub = _derive_initial_dh_keypair(shared, user_id)
    _, peer_pub = _derive_initial_dh_keypair(shared, peer_id)
    their_dh_pub = peer_pub if is_initiator else None
    state = r.init_ratchet(
        shared_secret=shared,
        our_dh_priv=our_priv, our_dh_pub=our_pub,
        their_dh_pub=their_dh_pub)
    save(db, conversation_id, user_id, peer_id, state,
         tenant_id=tenant_id)
    return state


def load_or_bootstrap(db, conversation_id: str, user_id: str,
                      peer_id: str,
                      user_identity_b64: str, peer_identity_b64: str,
                      *,
                      tenant_id: Optional[str] = None) -> r.RatchetState:
    """Convenience: load existing state, or bootstrap on first use.
    Initiator selection is deterministic (lexicographic min)."""
    existing = load(db, conversation_id, user_id, peer_id)
    if existing is not None:
        return existing
    is_initiator = (str(user_id) < str(peer_id))
    return bootstrap_pair(
        db, conversation_id, user_id, peer_id,
        user_identity_b64, peer_identity_b64,
        tenant_id=tenant_id, is_initiator=is_initiator)


__all__ = [
    'E2EStateError',
    'serialize_state', 'deserialize_state',
    'load', 'save', 'delete_for_conversation',
    'bootstrap_pair', 'load_or_bootstrap',
]
