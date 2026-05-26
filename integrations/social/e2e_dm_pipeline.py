"""
HevolveSocial — Phase 9.B E2E DM encrypt/decrypt pipeline.

Plan reference: sunny-gliding-eich.md, Part K.4 + Phase 9.

Glues the Double Ratchet primitives (e2e_ratchet.py), the per-pair
state repo (e2e_state_repo.py), and the envelope storage
(e2e_key_service.py) into a two-function pipeline:

  - encrypt_for_conversation: sender encrypts plaintext into one
    envelope per active recipient member, advancing each pair's
    ratchet state.  Stores envelopes via E2EKeyService.record_envelope.

  - decrypt_for_recipient: receiver fetches their envelope for the
    message_id, decrypts it via their stored ratchet state, advances
    state, returns plaintext.

When the `e2e_dms` feature flag is OFF or a conversation's
`settings.e2e_enabled` is not True, this module's hot path is never
called — ConversationService.send_message stores plaintext as
before.  Zero regression on the flag-off path.

Initial-secret bootstrap is the deterministic placeholder in
e2e_state_repo._derive_initial_shared_secret; X3DH (Phase 9.C)
will replace it without API change at this layer.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from . import e2e_ratchet as r
from . import e2e_state_repo as state_repo
from .e2e_key_service import E2EKeyService

logger = logging.getLogger('hevolve_social')


# ── Flag / settings resolution ──────────────────────────────────────


def _conv_settings(db, conv_id: str) -> Dict[str, Any]:
    row = db.execute(text(
        "SELECT settings FROM conversations WHERE id = :cid"),
        {'cid': conv_id}
    ).fetchone()
    if row is None or row[0] is None:
        return {}
    try:
        parsed = json.loads(row[0])
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _flag_on() -> bool:
    """Server-side `e2e_dms` flag from g.feature_flags.  Defaults
    False — so opt-in via the explicit flag, not just by setting
    the conversation's settings.e2e_enabled (which a malicious client
    could try to spoof in a JSON payload)."""
    try:
        from flask import g, has_request_context
        if has_request_context():
            flags = getattr(g, 'feature_flags', {}) or {}
            return bool(flags.get('e2e_dms', False))
    except Exception:
        pass
    return False


def should_encrypt(db, conv_id: str) -> bool:
    """Both flag AND settings must agree.  Either off → plaintext.

    Defense in depth: a malicious client editing the conversation
    settings can't force encryption (the server flag gates the path);
    a server with the flag on but a conversation that hasn't opted in
    still stores plaintext (so legacy clients keep working)."""
    if not _flag_on():
        return False
    if not r._HAS_CRYPTO:
        # Bare deploy without `cryptography` — degrade to plaintext.
        # Logged once at warn so operators notice the gap.
        logger.warning(
            "e2e_dms flag is on but `cryptography` package is missing; "
            "falling back to plaintext storage")
        return False
    settings = _conv_settings(db, conv_id)
    return bool(settings.get('e2e_enabled', False))


# ── Recipient resolution ────────────────────────────────────────────


def _list_active_keys(db, conv_id: str,
                      tenant_id: Optional[str] = None
                      ) -> List[Dict[str, Any]]:
    """Active member identity bundles for the conversation.  Only
    members who've published a key get an envelope — list_members
    _without_keys is the inverse for caller UI warnings.  Phase 9.A
    schema already enforces UNIQUE active key per (conv, user)."""
    return E2EKeyService.get_active_keys(db, conv_id, tenant_id=tenant_id)


# ── Encrypt path ────────────────────────────────────────────────────


def encrypt_for_conversation(db, conv_id: str, sender_id: str,
                             message_id: str, plaintext: str,
                             *,
                             tenant_id: Optional[str] = None
                             ) -> List[str]:
    """Encrypt `plaintext` once per active recipient.  Stores one
    envelope per recipient via E2EKeyService.record_envelope, advances
    each pair's ratchet state.  Returns the list of recipient_ids
    that actually got an envelope (excludes sender + members without
    a published key).

    The caller (ConversationService.send_message) replaces the
    plaintext content with a placeholder ('[encrypted]') so legacy
    readers / non-member admin tooling don't crash on null.

    Raises RatchetUnavailable if `cryptography` isn't installed —
    callers SHOULD pre-check via should_encrypt() so this never fires
    in practice.
    """
    r._require_crypto()
    keys = _list_active_keys(db, conv_id, tenant_id=tenant_id)
    sender_keys = [k for k in keys if k['user_id'] == sender_id]
    if not sender_keys:
        raise r.RatchetError(
            f"sender {sender_id} has no published identity key — call "
            f"E2EKeyService.publish_identity_key before sending")
    sender_key_b64 = sender_keys[0]['identity_key_b64']

    plaintext_bytes = plaintext.encode('utf-8')
    delivered: List[str] = []
    for recipient_key in keys:
        rid = recipient_key['user_id']
        if rid == sender_id:
            continue
        # Bootstrap (or load) the SENDER's view of the (sender→recipient) pair.
        try:
            state = state_repo.load_or_bootstrap(
                db, conv_id, sender_id, rid,
                sender_key_b64, recipient_key['identity_key_b64'],
                tenant_id=tenant_id)
        except r.RatchetUnavailable:
            raise
        except Exception as e:
            logger.warning(
                "encrypt_for_conversation: bootstrap failed for "
                "(%s→%s): %s", sender_id, rid, e)
            continue
        # The responder side (sender_id > rid lex) bootstraps with
        # their_dh_pub=None (libsignal convention) — they can't send
        # before they receive.  Phase 9.B integration gates this:
        # if a responder tries to send first, we derive a peer DH
        # pub from the bootstrap (deterministic for both sides) so
        # the first send succeeds.  After the first inbound from the
        # peer, advance_dh_ratchet replaces it with the real ephemeral.
        if state.their_dh_pub is None:
            from . import e2e_state_repo as state_repo_mod
            shared = state_repo_mod._derive_initial_shared_secret(
                sender_id, rid,
                sender_key_b64, recipient_key['identity_key_b64'])
            _, peer_pub = state_repo_mod._derive_initial_dh_keypair(
                shared, rid)
            state = state._replace(their_dh_pub=peer_pub)
        new_state, envelope = r.encrypt_message(state, plaintext_bytes)
        state_repo.save(db, conv_id, sender_id, rid, new_state,
                        tenant_id=tenant_id)
        # Record the envelope for the recipient.  The ratchet header
        # carries the our_pub + idx + nonce so the receiver can
        # rebuild the envelope dict from the wire.  We use the
        # serialize_envelope wire format for this.
        wire = r.serialize_envelope(envelope)
        ct_b64 = base64.b64encode(wire).decode('ascii')
        try:
            E2EKeyService.record_envelope(
                db, message_id, rid, ct_b64,
                tenant_id=tenant_id)
            delivered.append(rid)
        except Exception as e:
            logger.warning(
                "encrypt_for_conversation: record_envelope failed for "
                "(%s, %s): %s", message_id, rid, e)
    return delivered


# ── Decrypt path ────────────────────────────────────────────────────


def decrypt_for_recipient(db, conv_id: str, message_id: str,
                          recipient_id: str,
                          *,
                          tenant_id: Optional[str] = None
                          ) -> Optional[str]:
    """Fetch + decrypt the envelope addressed to `recipient_id` for
    `message_id`.  Returns the plaintext, or None if there's no
    envelope (recipient joined after the send, or sender skipped them
    because they had no published key at send-time).

    Caller (ConversationService.list_messages) substitutes the
    decrypted plaintext into the message dict ONLY for the requesting
    user — other members' list_messages calls fetch their own
    envelopes; the stored `messages.content` placeholder is what
    every non-recipient observer sees.
    """
    r._require_crypto()
    env = E2EKeyService.fetch_envelope(
        db, message_id, recipient_id, tenant_id=tenant_id)
    if env is None:
        return None
    try:
        wire = base64.b64decode(env['ciphertext_b64'].encode('ascii'))
        envelope_dict = r.deserialize_envelope(wire)
    except Exception as e:
        logger.warning(
            "decrypt_for_recipient: malformed envelope for "
            "(%s, %s): %s", message_id, recipient_id, e)
        return None
    # The sender's user_id is the OTHER end of the recipient_id
    # pair in this DM.  We look it up via the conversation_keys
    # table — every published identity has a user_id.  For DMs
    # there's exactly one peer.  For groups (out of scope for 9.B
    # but we keep the shape forward-compatible) the sender_pub in
    # the envelope identifies which peer.
    sender_id = _resolve_sender(db, conv_id, recipient_id,
                                envelope_dict['our_pub'],
                                tenant_id=tenant_id)
    if sender_id is None:
        logger.warning(
            "decrypt_for_recipient: cannot resolve sender for "
            "envelope %s", message_id)
        return None
    state = state_repo.load(db, conv_id, recipient_id, sender_id)
    if state is None:
        # First inbound from this peer — bootstrap responder-side.
        sender_keys = [
            k for k in _list_active_keys(db, conv_id, tenant_id=tenant_id)
            if k['user_id'] == sender_id]
        recipient_keys = [
            k for k in _list_active_keys(db, conv_id, tenant_id=tenant_id)
            if k['user_id'] == recipient_id]
        if not sender_keys or not recipient_keys:
            return None
        state = state_repo.bootstrap_pair(
            db, conv_id, recipient_id, sender_id,
            recipient_keys[0]['identity_key_b64'],
            sender_keys[0]['identity_key_b64'],
            tenant_id=tenant_id,
            is_initiator=(str(recipient_id) < str(sender_id)))
    try:
        new_state, plaintext_bytes = r.decrypt_message(state, envelope_dict)
    except r.RatchetReplayError:
        # Double-delivery — caller already saw this message; safe to
        # ignore.  Return None so the API surface treats it like a
        # missing envelope.
        return None
    except Exception as e:
        logger.warning(
            "decrypt_for_recipient: decrypt failed for (%s, %s): %s",
            message_id, recipient_id, e)
        return None
    state_repo.save(db, conv_id, recipient_id, sender_id, new_state,
                    tenant_id=tenant_id)
    try:
        return plaintext_bytes.decode('utf-8')
    except Exception:
        return plaintext_bytes.decode('utf-8', errors='replace')


def _resolve_sender(db, conv_id: str, recipient_id: str,
                    sender_pub: bytes,
                    tenant_id: Optional[str] = None) -> Optional[str]:
    """Find the user_id whose identity_key (base64 of sender_pub) is
    active in this conversation, excluding the recipient.

    For DMs there's one such user.  For groups, the sender_pub
    distinguishes which peer sent the envelope.

    Falls back to "any non-recipient member" for DMs when the
    sender_pub doesn't match an identity key (which can happen when
    the ratchet has stepped past the initial DH key — sender_pub is
    a rotating ephemeral key, not the identity key).  This fallback
    is safe for DMs because there's exactly one peer; groups need
    a richer mapping (Phase 9.D when groups land).
    """
    keys = _list_active_keys(db, conv_id, tenant_id=tenant_id)
    candidates = [k for k in keys if k['user_id'] != recipient_id]
    if not candidates:
        return None
    # Try identity-key match first (only correct on the very first
    # message before any DH-step has happened).
    try:
        sender_pub_b64 = base64.b64encode(sender_pub).decode('ascii')
        for k in candidates:
            if k['identity_key_b64'] == sender_pub_b64:
                return k['user_id']
    except Exception:
        pass
    # DM fallback: exactly one non-recipient peer.
    if len(candidates) == 1:
        return candidates[0]['user_id']
    # Group case: walk ratchet_states to find a peer whose persisted
    # state's `their_dh_pub` matches sender_pub.  Falls through to
    # None if nothing matches.
    rows = db.execute(text(
        "SELECT peer_id, state_json FROM ratchet_states "
        "WHERE conversation_id = :cid AND user_id = :rid"),
        {'cid': conv_id, 'rid': recipient_id}
    ).fetchall()
    for peer_id, blob in rows:
        try:
            st = state_repo.deserialize_state(blob)
            if st.their_dh_pub == sender_pub:
                return peer_id
        except Exception:
            continue
    return None


__all__ = [
    'should_encrypt',
    'encrypt_for_conversation',
    'decrypt_for_recipient',
]
