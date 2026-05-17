"""
HevolveSocial — Phase 9 E2E DM key envelope service (schema-only stub).

Plan reference: sunny-gliding-eich.md, Part K.4 + Phase 9.

This is the SCHEMA-ONLY layer of the optional E2E DM stack.  The
actual libsignal-style double-ratchet implementation lives in a
follow-up.  This module ships:

  - publish_identity_key(user, conversation, identity_key_b64,
                         signed_prekey_b64, signed_prekey_sig_b64)
      Member uploads their public identity bundle for a conversation.

  - rotate_identity_key(user, conversation)
      Soft-marks the existing key as `rotated_at = now()` and accepts
      a fresh one via publish_identity_key.

  - get_active_keys(conversation)
      Returns each active member's current public bundle so a sender
      can build envelopes.

  - record_envelope(message_id, recipient_id, ciphertext_b64,
                    ratchet_header_b64)
      Persists one envelope row per recipient.

  - fetch_envelope(message_id, recipient_id)
      Pulls the envelope a recipient should decrypt.

What this module DOES NOT do (deferred to crypto follow-up):
  - Generate / verify signatures
  - Run the double-ratchet KDF chain
  - Manage one-time prekeys (X3DH initial handshake)
  - Key backup / escrow

The schema is stable; the crypto can land later without further
migration churn.

Transport: same as the rest of HARTOS social — P2P-first via
MessageBus.publish.  Envelope distribution at message-send time
flows through the existing Conversation send_message path; this
module just stores the row.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .sync_service import _tenant_predicate

logger = logging.getLogger('hevolve_social')


class E2EKeyError(Exception):
    """Raised for service-level E2E key failures.  Caller maps to
    4xx HTTP responses."""


def _resolve_tenant(arg_tenant: Optional[str]) -> Optional[str]:
    """Pass-5 F9 fix: prefer explicit `tenant_id` arg, fall back to
    `g.tenant_id` from the active Flask request, then None.  Closes
    the foot-gun where a caller forgets to pass tenant_id and the
    row gets stamped untenanted (invisible in strict mode = bricked).
    """
    if arg_tenant is not None:
        return arg_tenant
    try:
        from flask import g, has_request_context
        if has_request_context():
            return getattr(g, 'tenant_id', None)
    except Exception:
        pass
    return None


class E2EKeyService:

    @staticmethod
    def publish_identity_key(db, conversation_id: str, user_id: str,
                             identity_key_b64: str,
                             *,
                             signed_prekey_b64: Optional[str] = None,
                             signed_prekey_signature_b64: Optional[str] = None,
                             key_algorithm: str = 'x25519-ed25519',
                             tenant_id: Optional[str] = None
                             ) -> Dict[str, Any]:
        """Upload (or replace) this user's public identity bundle for
        the given conversation.  Idempotent on `(conversation_id,
        user_id)` — re-publishing rotates the previous key.

        Pass-5 F1 + F9: tenant_id is resolved from g if not passed,
        and the rotate UPDATE is gated by tenant_id so we can't
        accidentally rotate a row in another tenant.  The INSERT is
        wrapped in a SAVEPOINT (F2) so a concurrent publish race
        recovers gracefully.
        """
        if not identity_key_b64:
            raise E2EKeyError("identity_key_b64 required")
        tid = _resolve_tenant(tenant_id)
        tenant_sql, tenant_params = _tenant_predicate(tid)

        # Soft-rotate existing active row(s).  Tenant clause prevents
        # cross-tenant mutation if the caller knows the (conv, user)
        # but lives in a different tenant.
        rotate_params = {'cid': conversation_id, 'uid': user_id}
        rotate_params.update(tenant_params)
        db.execute(text(
            "UPDATE conversation_keys SET rotated_at = CURRENT_TIMESTAMP "
            "WHERE conversation_id = :cid AND user_id = :uid "
            "AND rotated_at IS NULL"
            f"{tenant_sql}"),
            rotate_params)

        kid = str(uuid.uuid4())
        try:
            with db.begin_nested():
                db.execute(text(
                    "INSERT INTO conversation_keys "
                    "(id, tenant_id, conversation_id, user_id, "
                    " identity_key_b64, signed_prekey_b64, "
                    " signed_prekey_signature_b64, key_algorithm) "
                    "VALUES (:id, :tid, :cid, :uid, :ik, :spk, :sig, :algo)"),
                    {'id': kid, 'tid': tid, 'cid': conversation_id,
                     'uid': user_id, 'ik': identity_key_b64,
                     'spk': signed_prekey_b64,
                     'sig': signed_prekey_signature_b64,
                     'algo': key_algorithm})
        except IntegrityError as e:
            # Pass-5 F2: concurrent publish raced us — retry the
            # rotate (the other publisher's row is now active) and
            # re-INSERT.  One retry is enough; if it races again,
            # surface as E2EKeyError so the caller can choose.
            logger.info(
                "publish_identity_key concurrent race for (conv=%s,"
                " user=%s); retrying rotate+insert", conversation_id, user_id)
            db.execute(text(
                "UPDATE conversation_keys SET rotated_at = CURRENT_TIMESTAMP "
                "WHERE conversation_id = :cid AND user_id = :uid "
                "AND rotated_at IS NULL"
                f"{tenant_sql}"),
                rotate_params)
            try:
                db.execute(text(
                    "INSERT INTO conversation_keys "
                    "(id, tenant_id, conversation_id, user_id, "
                    " identity_key_b64, signed_prekey_b64, "
                    " signed_prekey_signature_b64, key_algorithm) "
                    "VALUES (:id, :tid, :cid, :uid, :ik, :spk, :sig, :algo)"),
                    {'id': kid, 'tid': tid, 'cid': conversation_id,
                     'uid': user_id, 'ik': identity_key_b64,
                     'spk': signed_prekey_b64,
                     'sig': signed_prekey_signature_b64,
                     'algo': key_algorithm})
            except IntegrityError as e2:
                raise E2EKeyError(
                    f"could not publish identity key: {e2}")
        db.commit()
        return E2EKeyService._key_dict(db, kid)

    @staticmethod
    def rotate_identity_key(db, conversation_id: str, user_id: str,
                            *,
                            tenant_id: Optional[str] = None) -> bool:
        """Soft-mark the user's current key as rotated.  Returns True
        iff a row was actually rotated.  Tenant-scoped (Pass-5 F1)."""
        tid = _resolve_tenant(tenant_id)
        tenant_sql, tenant_params = _tenant_predicate(tid)
        params = {'cid': conversation_id, 'uid': user_id}
        params.update(tenant_params)
        result = db.execute(text(
            "UPDATE conversation_keys SET rotated_at = CURRENT_TIMESTAMP "
            "WHERE conversation_id = :cid AND user_id = :uid "
            "AND rotated_at IS NULL"
            f"{tenant_sql}"),
            params)
        db.commit()
        return (result.rowcount or 0) > 0

    @staticmethod
    def get_active_keys(db, conversation_id: str,
                        *,
                        tenant_id: Optional[str] = None
                        ) -> List[Dict[str, Any]]:
        """Return one dict per active member key.  A sender uses
        these to build per-recipient envelopes.  Members without an
        active key are SKIPPED — see list_members_without_keys (F8
        follow-up) for the inverse list.  Tenant-scoped (Pass-5 F1)."""
        tid = _resolve_tenant(tenant_id)
        tenant_sql, tenant_params = _tenant_predicate(tid)
        params = {'cid': conversation_id}
        params.update(tenant_params)
        rows = db.execute(text(
            "SELECT id, user_id, identity_key_b64, signed_prekey_b64, "
            "       signed_prekey_signature_b64, key_algorithm, "
            "       created_at "
            "FROM conversation_keys "
            "WHERE conversation_id = :cid AND rotated_at IS NULL"
            f"{tenant_sql} "
            "ORDER BY created_at ASC"),
            params
        ).fetchall()
        return [
            {'id': r[0], 'user_id': r[1],
             'identity_key_b64': r[2],
             'signed_prekey_b64': r[3],
             'signed_prekey_signature_b64': r[4],
             'key_algorithm': r[5],
             'created_at': str(r[6]) if r[6] else None}
            for r in rows
        ]

    @staticmethod
    def list_members_without_keys(db, conversation_id: str,
                                  *,
                                  tenant_id: Optional[str] = None
                                  ) -> List[str]:
        """Pass-5 F8 helper: return user_ids of conversation members
        who have NOT published an active identity key.  A sender
        building envelopes uses this to surface a UI warning ("3
        members can't read this message yet") before sending.

        Returns user_ids only — caller joins to user metadata as
        needed.  Tenant-scoped via memberships table.
        """
        tid = _resolve_tenant(tenant_id)
        tenant_sql_m, tenant_params_m = _tenant_predicate(tid, alias='m')
        tenant_sql_k, tenant_params_k = _tenant_predicate(tid, alias='k')
        params = {'cid': conversation_id}
        params.update(tenant_params_m)
        # Note: tenant params for both aliases share `:tid` — that's
        # the same param, fine to merge.  But be explicit:
        for k, v in tenant_params_k.items():
            params[k] = v
        rows = db.execute(text(
            "SELECT m.member_id FROM memberships m "
            "LEFT JOIN conversation_keys k "
            "  ON k.conversation_id = m.parent_id "
            "  AND k.user_id = m.member_id "
            "  AND k.rotated_at IS NULL"
            f"{tenant_sql_k} "
            "WHERE m.parent_kind = 'conversation' "
            "AND m.parent_id = :cid "
            "AND k.id IS NULL"
            f"{tenant_sql_m}"),
            params
        ).fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def record_envelope(db, message_id: str, recipient_id: str,
                        ciphertext_b64: str,
                        *,
                        ratchet_header_b64: Optional[str] = None,
                        tenant_id: Optional[str] = None
                        ) -> Dict[str, Any]:
        """Persist a per-recipient encrypted payload.  Append-only;
        UNIQUE(message_id, recipient_id) enforces one envelope per
        (message, recipient).

        Pass-5 F7: a duplicate (message, recipient) raises a clean
        E2EKeyError instead of leaking SQLAlchemy's IntegrityError.
        Pass-5 F1: tenant_id resolves from g if not passed.
        """
        if not ciphertext_b64:
            raise E2EKeyError("ciphertext_b64 required")
        tid = _resolve_tenant(tenant_id)
        eid = str(uuid.uuid4())
        try:
            with db.begin_nested():
                db.execute(text(
                    "INSERT INTO message_envelopes "
                    "(id, tenant_id, message_id, recipient_id, "
                    " ciphertext_b64, ratchet_header_b64) "
                    "VALUES (:id, :tid, :mid, :rid, :ct, :hdr)"),
                    {'id': eid, 'tid': tid, 'mid': message_id,
                     'rid': recipient_id, 'ct': ciphertext_b64,
                     'hdr': ratchet_header_b64})
        except IntegrityError:
            raise E2EKeyError(
                "envelope already recorded for this (message, recipient)")
        db.commit()
        return E2EKeyService._envelope_dict(db, eid)

    @staticmethod
    def fetch_envelope(db, message_id: str, recipient_id: str,
                       *,
                       tenant_id: Optional[str] = None
                       ) -> Optional[Dict[str, Any]]:
        """Return the envelope `(message_id, recipient_id)` should
        decrypt, or None if no envelope exists for that pair (e.g.
        the recipient joined the conversation after this message,
        or the sender skipped them — see get_active_keys).

        Pass-5 F1: tenant-scoped — a tenant-A user querying for a
        tenant-B envelope by guessed id gets None, identical to
        a missing pair (no enumeration leak)."""
        tid = _resolve_tenant(tenant_id)
        tenant_sql, tenant_params = _tenant_predicate(tid)
        params = {'mid': message_id, 'rid': recipient_id}
        params.update(tenant_params)
        row = db.execute(text(
            "SELECT id FROM message_envelopes "
            "WHERE message_id = :mid AND recipient_id = :rid"
            f"{tenant_sql}"),
            params
        ).fetchone()
        if row is None:
            return None
        return E2EKeyService._envelope_dict(db, row[0])

    # ── Internal helpers ────────────────────────────────────────────

    @staticmethod
    def _key_dict(db, key_id: str) -> Dict[str, Any]:
        row = db.execute(text(
            "SELECT id, conversation_id, user_id, identity_key_b64, "
            "       signed_prekey_b64, signed_prekey_signature_b64, "
            "       key_algorithm, created_at, rotated_at "
            "FROM conversation_keys WHERE id = :id"),
            {'id': key_id}
        ).fetchone()
        if not row:
            return {}
        return {
            'id': row[0], 'conversation_id': row[1], 'user_id': row[2],
            'identity_key_b64': row[3],
            'signed_prekey_b64': row[4],
            'signed_prekey_signature_b64': row[5],
            'key_algorithm': row[6],
            'created_at': str(row[7]) if row[7] else None,
            'rotated_at': str(row[8]) if row[8] else None,
        }

    @staticmethod
    def _envelope_dict(db, env_id: str) -> Dict[str, Any]:
        row = db.execute(text(
            "SELECT id, message_id, recipient_id, ciphertext_b64, "
            "       ratchet_header_b64, created_at "
            "FROM message_envelopes WHERE id = :id"),
            {'id': env_id}
        ).fetchone()
        if not row:
            return {}
        return {
            'id': row[0], 'message_id': row[1], 'recipient_id': row[2],
            'ciphertext_b64': row[3],
            'ratchet_header_b64': row[4],
            'created_at': str(row[5]) if row[5] else None,
        }


__all__ = ['E2EKeyService', 'E2EKeyError']
