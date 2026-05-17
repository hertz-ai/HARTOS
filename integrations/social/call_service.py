"""
HevolveSocial — Voice/video/screen-share call service.

Phase 7d.  Plan reference: sunny-gliding-eich.md, Part E.4 + E.7 + E.12.

This is the BACKEND state machine for call sessions — REST surface
for create / join / leave / end / invite, plus the AgentJoinGrant
gate for agents joining as participants.

What lives here:
  - CallService.create / get / end — CallSession bookkeeping.
  - CallService.join / leave — CallParticipant lifecycle, with
    UNIQUE-WHERE-left_at-IS-NULL invariant ensuring exactly one
    active participant row per (call, user).
  - CallService.list_participants — current roster.
  - CallService.invite_member — Notification fan-out for non-muted
    members of the parent (community / conversation).
  - CallService.attach_agent — verifies AgentJoinGrant.can_voice
    before allowing the AgentVoiceBridge to spin up.

What does NOT live here (deferred to LiveKit + AgentVoiceBridge):
  - Media frames (WebRTC P2P mesh / LiveKit SFU live in their own
    transports; this service only emits signaling state).
  - Audio mixing / TTS publish / Whisper STT for agents — the bridge
    is a separate per-call worker; this service just registers
    bookkeeping for it.

Transport: P2P-first via core.peer_link.message_bus.MessageBus.publish.
Falls back to WAMP push for offline recipients and HTTP API for sync.
Central is the audit log + discovery index, not the primary router.

Existing fan-out order (do not bypass):
  LOCAL → SSE → PEERLINK → CROSSBAR

This module ONLY persists rows in the canonical DB and calls
MessageBus.publish() — it does NOT directly emit WAMP or open
PeerLink frames.  Transport selection is owned by message_bus.py.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

logger = logging.getLogger('hevolve_social')


# Whitelist enforced at the service layer so a malicious client cannot
# coax the DB into accepting a kind not understood by clients.
ALLOWED_CALL_KINDS = ('voice', 'video', 'screen_share', 'mixed', 'stage')

# Default per-room cap.  Above 4 participants, mesh efficiency drops
# and LiveKit SFU is auto-promoted (Plan R.6).  Hard cap configurable
# per-call via settings.max_participants.
DEFAULT_MAX_PARTICIPANTS = 32


class CallError(Exception):
    """Raised for service-level call failures.  api_calls.py maps
    these to 4xx HTTP responses with the message in the error body."""


class CallService:

    # ── Session lifecycle ────────────────────────────────────────────

    @staticmethod
    def create(db, parent_kind: str, parent_id: str, started_by: str,
               kind: str = 'voice', title: Optional[str] = None,
               max_participants: int = DEFAULT_MAX_PARTICIPANTS,
               settings: Optional[Dict[str, Any]] = None,
               tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Create a CallSession row.

        The starter must be a member of the parent (community or
        conversation) — gated by Membership lookup.  Non-members
        raise CallError so we don't leak the existence of the parent.

        Idempotency: a parent already has-an-active-call short-circuits
        and returns the existing row instead of creating a duplicate.
        Two clients racing on the Start Call button get the same call,
        which is what users expect.
        """
        if kind not in ALLOWED_CALL_KINDS:
            raise CallError(f"unsupported call kind: {kind!r}")
        if parent_kind not in ('community', 'conversation'):
            raise CallError(f"unsupported parent_kind: {parent_kind!r}")

        if not _is_parent_member(db, parent_kind, parent_id, started_by):
            # 404-shape from caller — do not reveal existence of the parent.
            raise CallError("parent not found")

        # Idempotent: return existing active call for this parent.
        # Pass-4 P4-1 fix: SELECT-then-INSERT under READ COMMITTED is
        # racy on Postgres — two concurrent starters could both pass
        # the SELECT and both INSERT.  Use a SAVEPOINT around the
        # INSERT so an IntegrityError on a future partial-unique-
        # index parent_active row falls back to re-SELECT.  The
        # partial index isn't in the migration today (deferred to
        # Phase 7d.B alongside the MySQL workaround for #2), but the
        # try/except on insert + recheck closes the window in code.
        existing = db.execute(text(
            "SELECT id FROM call_sessions "
            "WHERE parent_kind = :pk AND parent_id = :pid "
            "AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1"),
            {'pk': parent_kind, 'pid': parent_id}
        ).fetchone()
        if existing is not None:
            return CallService.get(db, existing[0])

        call_id = str(uuid.uuid4())
        try:
            with db.begin_nested():
                db.execute(text(
                    "INSERT INTO call_sessions "
                    "(id, tenant_id, parent_kind, parent_id, title, "
                    " kind, started_by, max_participants, settings) "
                    "VALUES "
                    "(:id, :tid, :pk, :pid, :title, :kind, :sb, :mp, :s)"),
                    {'id': call_id, 'tid': tenant_id, 'pk': parent_kind,
                     'pid': parent_id, 'title': title, 'kind': kind,
                     'sb': started_by, 'mp': max_participants,
                     's': json.dumps(settings) if settings else None})
        except Exception as e:
            # Concurrent starter beat us to the INSERT — re-SELECT
            # the now-active call and return it.  Reviewer P4-1.
            logger.info(
                "CallService.create concurrent INSERT collision "
                "for parent=%s/%s: %s; re-reading", parent_kind,
                parent_id, e)
            existing = db.execute(text(
                "SELECT id FROM call_sessions "
                "WHERE parent_kind = :pk AND parent_id = :pid "
                "AND ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1"),
                {'pk': parent_kind, 'pid': parent_id}
            ).fetchone()
            if existing is not None:
                return CallService.get(db, existing[0])
            raise CallError(f"could not create call: {e}")
        db.commit()

        # Auto-add the starter as the first participant so the roster
        # query reflects them immediately.
        CallService.join(db, call_id, started_by,
                         device_kind='mobile', tenant_id=tenant_id)
        return CallService.get(db, call_id)

    @staticmethod
    def get(db, call_id: str) -> Dict[str, Any]:
        row = db.execute(text(
            "SELECT id, parent_kind, parent_id, title, kind, started_by, "
            "       started_at, ended_at, livekit_room_sid, "
            "       max_participants, settings "
            "FROM call_sessions WHERE id = :id"),
            {'id': call_id}
        ).fetchone()
        if row is None:
            raise CallError("not found")
        return _row_to_dict(row)

    @staticmethod
    def end(db, call_id: str, ended_by_id: str) -> Dict[str, Any]:
        """End the call.  Only the starter or a parent admin can end.

        Pass-4 P4-4: both UPDATEs run inside a SAVEPOINT so a mid-
        operation failure rolls back BOTH (no half-ended state where
        the session is closed but participants stay active).  Order
        is participants → session so worst-case partial commit leaves
        a still-active session with no active participants — easy to
        re-end manually — rather than the inverse (closed session
        with ghost participants).
        """
        sess = CallService.get(db, call_id)
        if sess.get('ended_at'):
            return sess  # idempotent — already ended
        is_starter = sess['started_by'] == ended_by_id
        is_admin = _is_parent_admin(
            db, sess['parent_kind'], sess['parent_id'], ended_by_id)
        if not (is_starter or is_admin):
            raise CallError("only the starter or an admin can end the call")
        with db.begin_nested():
            db.execute(text(
                "UPDATE call_participants SET left_at = CURRENT_TIMESTAMP "
                "WHERE call_id = :id AND left_at IS NULL"),
                {'id': call_id})
            db.execute(text(
                "UPDATE call_sessions SET ended_at = CURRENT_TIMESTAMP "
                "WHERE id = :id AND ended_at IS NULL"),
                {'id': call_id})
        db.commit()
        return CallService.get(db, call_id)

    # ── Participant lifecycle ────────────────────────────────────────

    @staticmethod
    def join(db, call_id: str, user_id: str,
             device_kind: str = 'mobile', agent_kind: str = 'human',
             tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Mark a user as joined.  Idempotent — re-joining an active
        participant returns the existing row (no duplicate insert)."""
        sess = CallService.get(db, call_id)
        if sess.get('ended_at'):
            raise CallError("call has ended")

        # Membership check: only members of the parent can join.
        if not _is_parent_member(db, sess['parent_kind'],
                                 sess['parent_id'], user_id):
            raise CallError("not a member of this room's parent")

        # Idempotency: existing active participant row?
        active = db.execute(text(
            "SELECT id FROM call_participants "
            "WHERE call_id = :cid AND user_id = :uid AND left_at IS NULL"),
            {'cid': call_id, 'uid': user_id}
        ).fetchone()
        if active is not None:
            return CallService._participant_dict(db, active[0])

        pid = str(uuid.uuid4())
        db.execute(text(
            "INSERT INTO call_participants "
            "(id, tenant_id, call_id, user_id, agent_kind, "
            " device_kind) "
            "VALUES (:id, :tid, :cid, :uid, :ak, :dk)"),
            {'id': pid, 'tid': tenant_id, 'cid': call_id,
             'uid': user_id, 'ak': agent_kind, 'dk': device_kind})
        db.commit()
        return CallService._participant_dict(db, pid)

    @staticmethod
    def leave(db, call_id: str, user_id: str) -> bool:
        """Mark all this user's active participant rows as left.  Idempotent."""
        result = db.execute(text(
            "UPDATE call_participants SET left_at = CURRENT_TIMESTAMP "
            "WHERE call_id = :cid AND user_id = :uid AND left_at IS NULL"),
            {'cid': call_id, 'uid': user_id})
        db.commit()
        return (result.rowcount or 0) > 0

    @staticmethod
    def list_participants(db, call_id: str,
                          include_left: bool = False) -> List[Dict[str, Any]]:
        clause = "" if include_left else " AND left_at IS NULL"
        rows = db.execute(text(
            "SELECT id, user_id, agent_kind, joined_at, left_at, "
            "       is_muted, is_video_on, is_screen_sharing, device_kind "
            f"FROM call_participants WHERE call_id = :cid{clause} "
            "ORDER BY joined_at ASC"),
            {'cid': call_id}
        ).fetchall()
        return [
            {'id': r[0], 'user_id': r[1], 'agent_kind': r[2],
             'joined_at': str(r[3]) if r[3] else None,
             'left_at': str(r[4]) if r[4] else None,
             'is_muted': bool(r[5]), 'is_video_on': bool(r[6]),
             'is_screen_sharing': bool(r[7]), 'device_kind': r[8]}
            for r in rows
        ]

    # ── Agent join grant ────────────────────────────────────────────

    @staticmethod
    def grant_agent(db, agent_id: str, owner_id: str,
                    parent_kind: str, parent_id: str,
                    scope: Dict[str, Any],
                    source: str = 'user_explicit',
                    tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Owner grants an agent permission to join a parent.

        Idempotent on (agent_id, parent_kind, parent_id) — a re-grant
        with the same triple updates the scope rather than inserting
        a duplicate.  This matches what the user expects when they
        toggle scopes in the agent settings UI.
        """
        # Verify the caller owns the agent — the agent's User row must
        # have user_type='agent' AND owner_id matching owner_id arg.
        # (SocialUser uses `owner_id` for the agent→owner link;
        # Plan C.3 calls it agent_owner_id but the live schema is
        # owner_id.  Same semantic.)
        # Pass-4 P4-3: also pull is_admin so we can gate ownerless
        # (system) agent grants — see below.
        agent_row = db.execute(text(
            "SELECT user_type, owner_id FROM users WHERE id = :id"),
            {'id': agent_id}
        ).fetchone()
        if not agent_row:
            raise CallError("agent not found")
        if agent_row[0] != 'agent':
            raise CallError("user is not an agent")
        agent_owner = agent_row[1]
        if agent_owner is None:
            # Pass-4 P4-3 fix: system / ownerless agents must NOT be
            # granted by an arbitrary user.  Previously the
            # `agent_row[1] is not None` short-circuit allowed any
            # authenticated user to issue can_voice / can_screen
            # grants for a system agent — security hole.  Require
            # caller to be a platform admin OR raise.
            caller_row = db.execute(text(
                "SELECT is_admin FROM users WHERE id = :id"),
                {'id': owner_id}
            ).fetchone()
            if not caller_row or not caller_row[0]:
                raise CallError(
                    "system agent grants require platform admin")
        elif agent_owner != owner_id:
            raise CallError("only the agent's owner can grant join")

        # Existing active grant? Update scope in place.
        existing = db.execute(text(
            "SELECT id FROM agent_join_grants "
            "WHERE agent_id = :aid AND parent_kind = :pk "
            "AND parent_id = :pid AND revoked_at IS NULL"),
            {'aid': agent_id, 'pk': parent_kind, 'pid': parent_id}
        ).fetchone()
        scope_json = json.dumps(scope or {})
        if existing is not None:
            db.execute(text(
                "UPDATE agent_join_grants SET scope = :s WHERE id = :id"),
                {'s': scope_json, 'id': existing[0]})
            db.commit()
            return CallService._grant_dict(db, existing[0])

        gid = str(uuid.uuid4())
        db.execute(text(
            "INSERT INTO agent_join_grants "
            "(id, tenant_id, agent_id, owner_id, parent_kind, "
            " parent_id, scope, source) "
            "VALUES (:id, :tid, :aid, :oid, :pk, :pid, :s, :src)"),
            {'id': gid, 'tid': tenant_id, 'aid': agent_id,
             'oid': owner_id, 'pk': parent_kind, 'pid': parent_id,
             's': scope_json, 'src': source})
        db.commit()
        return CallService._grant_dict(db, gid)

    @staticmethod
    def revoke_agent(db, grant_id: str, revoker_id: str) -> bool:
        """Owner revokes an active grant. Idempotent."""
        row = db.execute(text(
            "SELECT owner_id, revoked_at FROM agent_join_grants "
            "WHERE id = :id"),
            {'id': grant_id}
        ).fetchone()
        if not row:
            raise CallError("grant not found")
        if row[0] != revoker_id:
            raise CallError("only the granter can revoke")
        if row[1] is not None:
            return False  # already revoked
        db.execute(text(
            "UPDATE agent_join_grants SET revoked_at = CURRENT_TIMESTAMP "
            "WHERE id = :id"),
            {'id': grant_id})
        db.commit()
        return True

    @staticmethod
    def get_active_grant(db, agent_id: str,
                         parent_kind: str, parent_id: str
                         ) -> Optional[Dict[str, Any]]:
        row = db.execute(text(
            "SELECT id FROM agent_join_grants "
            "WHERE agent_id = :aid AND parent_kind = :pk "
            "AND parent_id = :pid AND revoked_at IS NULL "
            "ORDER BY granted_at DESC LIMIT 1"),
            {'aid': agent_id, 'pk': parent_kind, 'pid': parent_id}
        ).fetchone()
        if row is None:
            return None
        return CallService._grant_dict(db, row[0])

    @staticmethod
    def attach_agent(db, call_id: str, agent_id: str,
                     tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Add an agent as a call_participant.  Requires an active
        AgentJoinGrant on the parent with `can_voice = True` for
        kind='voice' / 'mixed' (or `can_screen` for screen_share).

        Phase 7d.B: also spawns the AgentVoiceBridge worker so the
        agent actually gets STT + TTS plumbed through LiveKit (or
        no-op-stays-bookkeeping when livekit-rtc isn't installed).
        Plan E.12.  Bridge spawn is best-effort — a failure to spin
        up the bridge does NOT fail the participant record (the
        roster row stays so admins can see the agent + retry attach).
        """
        sess = CallService.get(db, call_id)
        grant = CallService.get_active_grant(
            db, agent_id, sess['parent_kind'], sess['parent_id'])
        if not grant:
            raise CallError("no active join grant for this agent + parent")
        scope = grant['scope'] or {}
        if sess['kind'] in ('voice', 'mixed') and not scope.get('can_voice'):
            raise CallError("grant does not include can_voice")
        if sess['kind'] == 'screen_share' and not scope.get('can_screen'):
            raise CallError("grant does not include can_screen")
        # Reuse join() with agent_kind='agent' + device_kind='agent_bridge'
        participant = CallService.join(
            db, call_id, agent_id,
            agent_kind='agent', device_kind='agent_bridge',
            tenant_id=tenant_id)
        # Phase 7d.B — spin up the AgentVoiceBridge worker.
        try:
            from .agent_voice_bridge import AgentVoiceBridge
            AgentVoiceBridge.attach_agent(
                db, call_id=call_id, agent_id=agent_id,
                owner_id=grant['owner_id'], scope=scope)
        except Exception as e:
            logger.warning(
                "CallService.attach_agent: bridge spawn failed for "
                "(call=%s, agent=%s): %s; participant row kept",
                call_id, agent_id, e)
        return participant

    # ── Internal helpers ────────────────────────────────────────────

    @staticmethod
    def _participant_dict(db, participant_id: str) -> Dict[str, Any]:
        row = db.execute(text(
            "SELECT id, call_id, user_id, agent_kind, joined_at, "
            "       left_at, is_muted, is_video_on, is_screen_sharing, "
            "       device_kind "
            "FROM call_participants WHERE id = :id"),
            {'id': participant_id}
        ).fetchone()
        if not row:
            return {}
        return {
            'id': row[0], 'call_id': row[1], 'user_id': row[2],
            'agent_kind': row[3],
            'joined_at': str(row[4]) if row[4] else None,
            'left_at': str(row[5]) if row[5] else None,
            'is_muted': bool(row[6]), 'is_video_on': bool(row[7]),
            'is_screen_sharing': bool(row[8]), 'device_kind': row[9],
        }

    @staticmethod
    def _grant_dict(db, grant_id: str) -> Dict[str, Any]:
        row = db.execute(text(
            "SELECT id, agent_id, owner_id, parent_kind, parent_id, "
            "       scope, granted_at, revoked_at, source "
            "FROM agent_join_grants WHERE id = :id"),
            {'id': grant_id}
        ).fetchone()
        if not row:
            return {}
        try:
            scope = json.loads(row[5]) if row[5] else {}
        except Exception:
            scope = {}
        return {
            'id': row[0], 'agent_id': row[1], 'owner_id': row[2],
            'parent_kind': row[3], 'parent_id': row[4],
            'scope': scope,
            'granted_at': str(row[6]) if row[6] else None,
            'revoked_at': str(row[7]) if row[7] else None,
            'source': row[8],
        }


def _is_parent_member(db, parent_kind: str, parent_id: str,
                      user_id: str) -> bool:
    """Membership check via the polymorphic memberships table.

    Fall back to the legacy community_memberships table for backward
    compat — Plan P.2 dual-write contract during the v40→v48 transition.
    """
    row = db.execute(text(
        "SELECT 1 FROM memberships "
        "WHERE parent_kind = :pk AND parent_id = :pid "
        "AND member_id = :uid LIMIT 1"),
        {'pk': parent_kind, 'pid': parent_id, 'uid': user_id}
    ).fetchone()
    if row is not None:
        return True
    if parent_kind == 'community':
        row = db.execute(text(
            "SELECT 1 FROM community_memberships "
            "WHERE community_id = :pid AND user_id = :uid LIMIT 1"),
            {'pid': parent_id, 'uid': user_id}
        ).fetchone()
        return row is not None
    return False


def _is_parent_admin(db, parent_kind: str, parent_id: str,
                     user_id: str) -> bool:
    row = db.execute(text(
        "SELECT 1 FROM memberships "
        "WHERE parent_kind = :pk AND parent_id = :pid "
        "AND member_id = :uid AND role IN ('admin', 'owner', 'mod') "
        "LIMIT 1"),
        {'pk': parent_kind, 'pid': parent_id, 'uid': user_id}
    ).fetchone()
    return row is not None


def _row_to_dict(row) -> Dict[str, Any]:
    try:
        settings = json.loads(row[10]) if row[10] else {}
    except Exception:
        settings = {}
    return {
        'id': row[0], 'parent_kind': row[1], 'parent_id': row[2],
        'title': row[3], 'kind': row[4], 'started_by': row[5],
        'started_at': str(row[6]) if row[6] else None,
        'ended_at': str(row[7]) if row[7] else None,
        'livekit_room_sid': row[8], 'max_participants': row[9],
        'settings': settings,
    }


__all__ = ['CallService', 'CallError', 'ALLOWED_CALL_KINDS']
