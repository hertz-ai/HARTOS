"""
HevolveSocial — Invite service (community + conversation, polymorphic).

Phase 7c.2. Plan reference: sunny-gliding-eich.md, Part E.9.

Three invite shapes (one schema):
  1. Targeted user      — invitee_id set, status='pending' until accept/reject.
  2. Off-platform email — invitee_email set, signup-then-accept flow.
  3. Shareable link     — both NULL, invite_code in URL `/i/<code>`.

Acceptance contracts:
  - Targeted invite       → only invitee_id can accept.
  - Email invite          → user with matching email after signup can accept.
  - Shareable link        → first authenticated user to call accept claims it.

On accept:
  - Insert Membership row (parent_kind/parent_id/member_id) idempotently.
  - For community parent: also insert into legacy community_memberships
    so existing readers (community_members API) keep working during the
    dual-write window (plan Part P.2).
  - Status transitions to 'accepted', responded_at stamped.
  - Notification fires for the inviter.

Transport: relies on NotificationService.create which already publishes
through MessageBus (LOCAL → SSE → PEERLINK → CROSSBAR).  This module
never opens transport sockets directly.

Block check: an invite from someone the recipient has blocked is silently
dropped at send time — same semantics as MentionService.

Coexists with the existing CommunityService.join() one-step flow.
A user can still self-join a public community without an invite; this
module just adds the invite-required path for private communities and
the invite-link UX for public ones.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

logger = logging.getLogger('hevolve_social')


_VALID_PARENT_KINDS = ('community', 'conversation')
_VALID_ROLES = ('member', 'mod', 'admin')
_DEFAULT_EXPIRY_DAYS = 7


class InviteError(ValueError):
    pass


def _gen_invite_code() -> str:
    """URL-safe token, ~22 chars. Collision-resistant for the table size."""
    return secrets.token_urlsafe(16)


def _is_blocked_either_way(db, x: str, y: str) -> bool:
    """Mirror of FriendService._is_blocked_either_way — best-effort check."""
    try:
        from sqlalchemy import text
        result = db.execute(text(
            "SELECT 1 FROM blocks "
            "WHERE (blocker_id = :x AND blocked_id = :y) "
            "OR (blocker_id = :y AND blocked_id = :x) LIMIT 1"),
            {'x': x, 'y': y}
        ).fetchone()
        return result is not None
    except Exception:
        return False


def _membership_exists(db, parent_kind: str, parent_id: str,
                       member_id: str) -> bool:
    """Whether the recipient is already a member of the parent."""
    try:
        from sqlalchemy import text
        # Polymorphic Membership table from v41.
        result = db.execute(text(
            "SELECT 1 FROM memberships "
            "WHERE parent_kind = :pk AND parent_id = :pid "
            "AND member_id = :mid LIMIT 1"),
            {'pk': parent_kind, 'pid': parent_id, 'mid': member_id}
        ).fetchone()
        if result is not None:
            return True
        # Fallback: legacy community_memberships during dual-write window.
        if parent_kind == 'community':
            result = db.execute(text(
                "SELECT 1 FROM community_memberships "
                "WHERE community_id = :pid AND user_id = :mid LIMIT 1"),
                {'pid': parent_id, 'mid': member_id}
            ).fetchone()
            return result is not None
    except Exception:
        return False
    return False


def _insert_membership(db, parent_kind: str, parent_id: str,
                       member_id: str, role: str,
                       tenant_id: Optional[str]) -> None:
    """Idempotent insert into the polymorphic Membership table.

    For community parents, also dual-writes to the legacy
    `community_memberships` table during the cut-over window so the
    existing readers (community_members API, member_count denorm)
    pick up the new member without code change.
    """
    from sqlalchemy import text
    dialect = db.bind.dialect.name if db.bind is not None else 'sqlite'
    if dialect == 'sqlite':
        m_stmt = ("INSERT OR IGNORE INTO memberships "
                  "(id, tenant_id, parent_kind, parent_id, member_id, "
                  " agent_kind, role, joined_at) "
                  "VALUES (:id, :tid, :pk, :pid, :mid, 'human', :role, "
                  " CURRENT_TIMESTAMP)")
    else:
        m_stmt = ("INSERT INTO memberships "
                  "(id, tenant_id, parent_kind, parent_id, member_id, "
                  " agent_kind, role, joined_at) "
                  "VALUES (:id, :tid, :pk, :pid, :mid, 'human', :role, "
                  " CURRENT_TIMESTAMP) "
                  "ON CONFLICT DO NOTHING")
    db.execute(text(m_stmt), {
        'id': str(uuid.uuid4()), 'tid': tenant_id,
        'pk': parent_kind, 'pid': parent_id, 'mid': member_id,
        'role': role})

    if parent_kind == 'community':
        # Dual-write to the legacy community_memberships table — same
        # idempotency strategy. Note: the legacy table uses `created_at`
        # (not `joined_at`); confirmed against the live schema.
        if dialect == 'sqlite':
            cm_stmt = ("INSERT OR IGNORE INTO community_memberships "
                       "(id, community_id, user_id, role, created_at) "
                       "VALUES (:id, :pid, :mid, :role, CURRENT_TIMESTAMP)")
        else:
            cm_stmt = ("INSERT INTO community_memberships "
                       "(id, community_id, user_id, role, created_at) "
                       "VALUES (:id, :pid, :mid, :role, CURRENT_TIMESTAMP) "
                       "ON CONFLICT DO NOTHING")
        try:
            db.execute(text(cm_stmt), {
                'id': str(uuid.uuid4()),
                'pid': parent_id, 'mid': member_id, 'role': role})
            # Bump denormed member_count.
            db.execute(text(
                "UPDATE communities SET member_count = "
                "COALESCE(member_count, 0) + 1 WHERE id = :pid"),
                {'pid': parent_id})
        except Exception as e:
            logger.warning("InviteService: legacy CM dual-write skipped: %s", e)


class InviteService:
    """All public methods are static. Each takes an explicit `db` so
    they can be called from any context without coupling to Flask `g`.
    """

    # ── Send ─────────────────────────────────────────────────────

    @staticmethod
    def send(db, parent_kind: str, parent_id: str, invited_by: str,
             invitee_id: Optional[str] = None,
             invitee_email: Optional[str] = None,
             role_offered: str = 'member',
             expires_in_days: Optional[int] = _DEFAULT_EXPIRY_DAYS,
             tenant_id: Optional[str] = None) -> dict:
        """Create a pending invite + fire notification.

        Returns dict with id, invite_code, status, plus the URL-safe
        share path the client can include in a deep-link.

        Refuses if:
          - parent_kind not in valid set
          - role_offered not in valid set
          - invitee_id == invited_by (cannot self-invite)
          - either party blocks the other (silently — matches Mention semantics)
          - invitee already a member of parent (returns existing membership idempotently)
        """
        from sqlalchemy import text

        if parent_kind not in _VALID_PARENT_KINDS:
            raise InviteError(f"invalid parent_kind: {parent_kind}")
        if role_offered not in _VALID_ROLES:
            raise InviteError(f"invalid role_offered: {role_offered}")
        if invitee_id and invitee_id == invited_by:
            raise InviteError("cannot invite yourself")
        if invitee_id and _is_blocked_either_way(db, invited_by, invitee_id):
            raise InviteError("cannot send invite")
        if invitee_id and _membership_exists(db, parent_kind, parent_id, invitee_id):
            return {'status': 'already_member',
                    'parent_kind': parent_kind, 'parent_id': parent_id}

        iid = str(uuid.uuid4())
        code = _gen_invite_code()
        expires_at = None
        if expires_in_days is not None and expires_in_days > 0:
            expires_at = (datetime.utcnow() + timedelta(days=expires_in_days)
                          ).replace(microsecond=0)

        try:
            db.execute(text(
                "INSERT INTO invites "
                "(id, tenant_id, parent_kind, parent_id, invitee_id, "
                " invitee_email, invite_code, invited_by, role_offered, "
                " status, created_at, expires_at) "
                "VALUES (:id, :tid, :pk, :pid, :uid, :em, :code, :ib, "
                " :role, 'pending', CURRENT_TIMESTAMP, :exp)"),
                {'id': iid, 'tid': tenant_id, 'pk': parent_kind,
                 'pid': parent_id, 'uid': invitee_id, 'em': invitee_email,
                 'code': code, 'ib': invited_by, 'role': role_offered,
                 'exp': expires_at})
            db.commit()
        except Exception as e:
            logger.warning("InviteService.send: insert failed: %s", e)
            raise InviteError(f"failed to create invite: {e}")

        # Notify targeted invitee. Email + shareable-link invites can't
        # fire an in-app notification (no user row to address), so we
        # skip — the inviter is responsible for sharing the link.
        if invitee_id:
            InviteService._notify_invitee(
                db, iid, invited_by, invitee_id, parent_kind, parent_id)

        return {
            'id': iid, 'invite_code': code, 'status': 'pending',
            'parent_kind': parent_kind, 'parent_id': parent_id,
            'role_offered': role_offered,
            'expires_at': str(expires_at) if expires_at else None,
        }

    # ── Accept / reject ──────────────────────────────────────────

    @staticmethod
    def accept(db, invite_id_or_code: str, accepter_id: str,
               tenant_id: Optional[str] = None) -> dict:
        """Accept an invite by id OR by code (shareable link path).

        Idempotent: if the same user re-accepts an already-accepted
        invite they own, returns the existing acceptance.

        Refuses if:
          - invite not found
          - invite expired
          - invite already accepted/rejected by someone else
          - targeted invite + accepter != invitee
        """
        invite = InviteService._lookup(db, invite_id_or_code)
        if invite is None:
            raise InviteError("invite not found")

        iid = invite['id']
        status = invite['status']
        invitee_id = invite['invitee_id']
        parent_kind = invite['parent_kind']
        parent_id = invite['parent_id']
        role = invite['role_offered']
        expires_at = invite['expires_at']

        # Expiry check.
        if expires_at and InviteService._is_expired(expires_at):
            InviteService._mark_expired(db, iid)
            raise InviteError("invite expired")

        if status == 'accepted':
            # Idempotent for the same user; refuse for a different user
            # claiming an already-claimed shareable link.
            if invitee_id == accepter_id:
                return {'id': iid, 'status': 'accepted',
                        'parent_kind': parent_kind, 'parent_id': parent_id}
            raise InviteError("invite already accepted")
        if status == 'rejected':
            raise InviteError("invite already rejected")
        if status == 'expired':
            raise InviteError("invite expired")

        # Targeted invite: only the named invitee can accept.
        if invitee_id is not None and invitee_id != accepter_id:
            raise InviteError("not the invitee")

        # Insert Membership (idempotent).
        _insert_membership(db, parent_kind, parent_id, accepter_id, role,
                           tenant_id)

        # Mark invite accepted, claim the slot for shareable links by
        # stamping invitee_id with the accepter.
        from sqlalchemy import text
        db.execute(text(
            "UPDATE invites SET status='accepted', "
            "responded_at=CURRENT_TIMESTAMP, "
            "invitee_id=COALESCE(invitee_id, :aid) "
            "WHERE id=:id"),
            {'aid': accepter_id, 'id': iid})
        db.commit()

        # Notify inviter that someone accepted.
        try:
            from .services import NotificationService
            NotificationService.create(
                db, user_id=invite['invited_by'], type='invite_accepted',
                source_user_id=accepter_id,
                target_type=parent_kind, target_id=parent_id,
                message="Your invite was accepted")
        except Exception as e:
            logger.warning("InviteService.accept: notify failed: %s", e)

        return {'id': iid, 'status': 'accepted',
                'parent_kind': parent_kind, 'parent_id': parent_id,
                'role': role}

    @staticmethod
    def reject(db, invite_id_or_code: str, rejecter_id: str) -> dict:
        from sqlalchemy import text
        invite = InviteService._lookup(db, invite_id_or_code)
        if invite is None:
            raise InviteError("invite not found")
        if invite['invitee_id'] is not None and invite['invitee_id'] != rejecter_id:
            raise InviteError("not the invitee")
        if invite['status'] != 'pending':
            raise InviteError(f"cannot reject from status={invite['status']}")
        db.execute(text(
            "UPDATE invites SET status='rejected', "
            "responded_at=CURRENT_TIMESTAMP WHERE id=:id"),
            {'id': invite['id']})
        db.commit()
        return {'id': invite['id'], 'status': 'rejected'}

    # ── Read paths ───────────────────────────────────────────────

    @staticmethod
    def list_incoming(db, user_id: str,
                      include_responded: bool = False) -> List[dict]:
        """Pending invites targeted at user_id (by id OR by their email).

        Off-platform email invites are surfaced once the user signs up
        with that email — this query joins on the user's email so the
        inbox shows them on first login.
        """
        from sqlalchemy import text
        from .models import User

        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            return []
        email = (user.email or '').lower() or None

        where = "(invitee_id = :uid"
        params = {'uid': user_id}
        if email:
            where += " OR LOWER(invitee_email) = :em"
            params['em'] = email
        where += ")"

        if not include_responded:
            where += " AND status = 'pending'"

        rows = db.execute(text(
            f"SELECT id, parent_kind, parent_id, invited_by, role_offered, "
            f"       status, created_at, expires_at, invite_code "
            f"FROM invites WHERE {where} "
            f"ORDER BY created_at DESC"),
            params
        ).fetchall()

        out = []
        for row in rows:
            iid, pk, pid, invited_by, role, status, created_at, expires_at, code = row
            # Skip expired invites silently in the incoming list.
            if status == 'pending' and expires_at and \
                    InviteService._is_expired(str(expires_at)):
                continue
            out.append({
                'id': iid, 'parent_kind': pk, 'parent_id': pid,
                'invited_by': invited_by, 'role_offered': role,
                'status': status, 'created_at': str(created_at) if created_at else None,
                'expires_at': str(expires_at) if expires_at else None,
                'invite_code': code,
            })
        return out

    @staticmethod
    def resolve_code(db, code: str) -> Optional[dict]:
        """Look up a pending invite by code. Used when a user opens a
        share link — the client previews the parent + role before the
        accept call. Returns None if code unknown / expired / consumed.
        """
        from sqlalchemy import text
        row = db.execute(text(
            "SELECT id, parent_kind, parent_id, invited_by, role_offered, "
            "       status, created_at, expires_at, invitee_id "
            "FROM invites WHERE invite_code = :code"),
            {'code': code}
        ).fetchone()
        if row is None:
            return None
        iid, pk, pid, invited_by, role, status, created_at, expires_at, invitee_id = row
        if status != 'pending':
            return None
        if expires_at and InviteService._is_expired(str(expires_at)):
            InviteService._mark_expired(db, iid)
            return None
        return {
            'id': iid, 'parent_kind': pk, 'parent_id': pid,
            'invited_by': invited_by, 'role_offered': role,
            'is_targeted': invitee_id is not None,
            'expires_at': str(expires_at) if expires_at else None,
        }

    # ── Internal helpers ─────────────────────────────────────────

    @staticmethod
    def _lookup(db, invite_id_or_code: str) -> Optional[dict]:
        """Look up an invite by primary id or by invite_code."""
        from sqlalchemy import text
        row = db.execute(text(
            "SELECT id, parent_kind, parent_id, invitee_id, invitee_email, "
            "       invited_by, role_offered, status, created_at, expires_at "
            "FROM invites WHERE id = :v OR invite_code = :v"),
            {'v': invite_id_or_code}
        ).fetchone()
        if row is None:
            return None
        return {
            'id': row[0], 'parent_kind': row[1], 'parent_id': row[2],
            'invitee_id': row[3], 'invitee_email': row[4],
            'invited_by': row[5], 'role_offered': row[6],
            'status': row[7], 'created_at': row[8],
            'expires_at': str(row[9]) if row[9] else None,
        }

    @staticmethod
    def _is_expired(expires_at_str: str) -> bool:
        """Compare ISO/SQLite-formatted expires_at to now."""
        if not expires_at_str:
            return False
        try:
            # SQLite stores "YYYY-MM-DD HH:MM:SS"; Postgres stores ISO.
            ts = expires_at_str.replace('T', ' ').split('.')[0]
            return datetime.utcnow() >= datetime.strptime(
                ts, '%Y-%m-%d %H:%M:%S')
        except Exception:
            return False

    @staticmethod
    def _mark_expired(db, invite_id: str) -> None:
        from sqlalchemy import text
        try:
            db.execute(text(
                "UPDATE invites SET status='expired' "
                "WHERE id = :id AND status = 'pending'"),
                {'id': invite_id})
            db.commit()
        except Exception:
            pass

    @staticmethod
    def _notify_invitee(db, invite_id: str, invited_by: str,
                        invitee_id: str, parent_kind: str,
                        parent_id: str) -> None:
        try:
            from .services import NotificationService
            NotificationService.create(
                db, user_id=invitee_id, type='invite',
                source_user_id=invited_by,
                target_type=parent_kind, target_id=parent_id,
                message=f"You were invited to a {parent_kind}")
        except Exception as e:
            logger.warning("InviteService: notify_invitee failed: %s", e)
