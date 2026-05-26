"""
HevolveSocial — Friendship + Block service.

Phase 7c.1. Plan reference: sunny-gliding-eich.md, Part E.8 + Part R.5.

Coexists with the existing one-direction `FollowService` (services.py).
This service adds a SYMMETRIC, STATEFUL relationship layer:

    pending ─accept─→ active ─block─→ blocked
       │              │                  ↑
       └─reject──→ rejected              │
       └─cancel──→ deleted               │
                  active ─block (anytime)┘
                  any ─unblock──→ active or deleted

Existing Follow rows are untouched. On accept(), reciprocal Follow rows
are auto-created so legacy code reading the follow graph (feed
ranking, recommendations) continues to work without change.

Transport: P2P-first via NotificationService.create which already
publishes on the existing social.{user_id} WAMP topic and fans out
through MessageBus (plan Part R). When two users become friends the
PeerLink trust ratchet upgrades automatically (link_manager.upgrade_peer)
the next time both come online.
"""

from __future__ import annotations

import logging
import uuid
from typing import List, Optional, Tuple

logger = logging.getLogger('hevolve_social')


def _sorted_pair(a: str, b: str) -> Tuple[str, str]:
    """Friendship rows are stored with (min, max) of the pair so each
    relationship has exactly one canonical row regardless of who
    initiated."""
    return (a, b) if a < b else (b, a)


def _is_blocked_either_way(db, x: str, y: str) -> bool:
    """True if x blocks y OR y blocks x. Used to gate friend requests."""
    from sqlalchemy import text
    try:
        result = db.execute(text(
            "SELECT 1 FROM blocks "
            "WHERE (blocker_id = :x AND blocked_id = :y) "
            "OR (blocker_id = :y AND blocked_id = :x) LIMIT 1"),
            {'x': x, 'y': y}
        ).fetchone()
        return result is not None
    except Exception:
        return False


class FriendError(ValueError):
    pass


class FriendService:
    """All public methods are static. Each takes an explicit `db` so
    they can be called from any context without coupling to Flask `g`.
    """

    # ── Sending / responding ─────────────────────────────────────

    @staticmethod
    def send_request(db, from_user_id: str, to_user_id: str,
                     tenant_id: Optional[str] = None) -> dict:
        """Create a pending Friendship row and notify the recipient.

        Idempotent: if a row already exists, returns its current
        state without creating a duplicate. Refuses if either party
        has blocked the other (silently — no leak whether the block
        is on which side).
        """
        from sqlalchemy import text
        from .services import NotificationService

        if from_user_id == to_user_id:
            raise FriendError("cannot friend yourself")
        if _is_blocked_either_way(db, from_user_id, to_user_id):
            raise FriendError("cannot send request")

        a, b = _sorted_pair(from_user_id, to_user_id)
        # Look up existing row.
        existing = db.execute(text(
            "SELECT id, status, initiator_id FROM friendships "
            "WHERE user_a_id = :a AND user_b_id = :b"),
            {'a': a, 'b': b}
        ).fetchone()
        if existing:
            fid, status, initiator = existing[0], existing[1], existing[2]
            if status == 'active':
                return {'id': fid, 'status': 'active'}
            if status == 'pending':
                # Idempotent — same initiator re-sending = no change;
                # OTHER user re-sending = auto-accept.
                if initiator == from_user_id:
                    return {'id': fid, 'status': 'pending'}
                # Auto-accept: the other side already requested.
                return FriendService._accept_internal(
                    db, fid, accepting_user_id=from_user_id)
            if status == 'rejected':
                # Reset to pending — give them another shot.
                db.execute(text(
                    "UPDATE friendships SET status='pending', "
                    "initiator_id=:i, created_at=CURRENT_TIMESTAMP, "
                    "accepted_at=NULL "
                    "WHERE id=:fid"),
                    {'i': from_user_id, 'fid': fid})
                db.commit()
                FriendService._notify_request(db, fid, from_user_id, to_user_id)
                return {'id': fid, 'status': 'pending'}
            if status == 'blocked':
                raise FriendError("cannot send request")

        # No prior row → insert pending.
        fid = str(uuid.uuid4())
        db.execute(text(
            "INSERT INTO friendships "
            "(id, tenant_id, user_a_id, user_b_id, status, initiator_id, "
            " created_at) "
            "VALUES "
            "(:id, :tid, :a, :b, 'pending', :i, CURRENT_TIMESTAMP)"),
            {'id': fid, 'tid': tenant_id, 'a': a, 'b': b,
             'i': from_user_id})
        db.commit()
        FriendService._notify_request(db, fid, from_user_id, to_user_id)
        return {'id': fid, 'status': 'pending'}

    @staticmethod
    def accept(db, friendship_id: str, accepting_user_id: str) -> dict:
        return FriendService._accept_internal(db, friendship_id, accepting_user_id)

    @staticmethod
    def _accept_internal(db, friendship_id: str,
                         accepting_user_id: str) -> dict:
        from sqlalchemy import text
        from .services import NotificationService

        row = db.execute(text(
            "SELECT user_a_id, user_b_id, status, initiator_id "
            "FROM friendships WHERE id = :id"),
            {'id': friendship_id}
        ).fetchone()
        if row is None:
            raise FriendError("not found")
        a, b, status, initiator = row[0], row[1], row[2], row[3]
        if accepting_user_id not in (a, b):
            raise FriendError("not a participant")
        if accepting_user_id == initiator and status == 'pending':
            raise FriendError("initiator cannot self-accept")
        if status == 'active':
            return {'id': friendship_id, 'status': 'active'}
        if status not in ('pending', 'rejected'):
            raise FriendError(f"cannot accept from status={status}")

        db.execute(text(
            "UPDATE friendships SET status='active', "
            "accepted_at=CURRENT_TIMESTAMP WHERE id = :id"),
            {'id': friendship_id})

        # Auto-create reciprocal Follow rows so downstream code
        # reading the follow graph (feed ranking, recommendations)
        # treats friends as mutual followers without needing a
        # parallel code path.
        #
        # Idempotent. We dispatch on the live dialect rather than
        # try/except — on Postgres a failed INSERT leaves the tx in
        # pending-rollback, so the second try would fail with
        # "current transaction is aborted" instead of silently
        # ignoring the duplicate.
        dialect = db.bind.dialect.name if db.bind is not None else 'sqlite'
        if dialect == 'sqlite':
            stmt = ("INSERT OR IGNORE INTO follows "
                    "(id, follower_id, following_id, created_at) "
                    "VALUES (:id, :f, :t, CURRENT_TIMESTAMP)")
        else:
            # Postgres + MySQL 8+ both accept ON CONFLICT DO NOTHING.
            stmt = ("INSERT INTO follows "
                    "(id, follower_id, following_id, created_at) "
                    "VALUES (:id, :f, :t, CURRENT_TIMESTAMP) "
                    "ON CONFLICT DO NOTHING")
        for follower, followed in ((a, b), (b, a)):
            db.execute(text(stmt), {'id': str(uuid.uuid4()),
                                    'f': follower, 't': followed})

        db.commit()

        # Notify the initiator that their request was accepted.
        try:
            NotificationService.create(
                db, user_id=initiator, type='friend_accepted',
                source_user_id=accepting_user_id,
                target_type='user', target_id=accepting_user_id,
                message="Your friend request was accepted")
        except Exception as e:
            logger.warning("FriendService.accept: notify failed: %s", e)

        return {'id': friendship_id, 'status': 'active'}

    @staticmethod
    def reject(db, friendship_id: str, rejecting_user_id: str) -> dict:
        from sqlalchemy import text
        row = db.execute(text(
            "SELECT user_a_id, user_b_id, status, initiator_id "
            "FROM friendships WHERE id = :id"),
            {'id': friendship_id}
        ).fetchone()
        if row is None:
            raise FriendError("not found")
        a, b, status, initiator = row[0], row[1], row[2], row[3]
        if rejecting_user_id not in (a, b):
            raise FriendError("not a participant")
        if rejecting_user_id == initiator:
            raise FriendError("initiator cannot reject — use cancel")
        if status not in ('pending',):
            raise FriendError(f"cannot reject from status={status}")
        db.execute(text(
            "UPDATE friendships SET status='rejected' WHERE id = :id"),
            {'id': friendship_id})
        db.commit()
        return {'id': friendship_id, 'status': 'rejected'}

    @staticmethod
    def cancel(db, friendship_id: str, canceller_id: str) -> dict:
        """Initiator withdraws their own pending request — row deleted."""
        from sqlalchemy import text
        row = db.execute(text(
            "SELECT initiator_id, status FROM friendships WHERE id = :id"),
            {'id': friendship_id}
        ).fetchone()
        if row is None:
            raise FriendError("not found")
        initiator, status = row[0], row[1]
        if canceller_id != initiator:
            raise FriendError("only the initiator can cancel")
        if status != 'pending':
            raise FriendError("only pending requests can be cancelled")
        db.execute(text("DELETE FROM friendships WHERE id = :id"),
                   {'id': friendship_id})
        db.commit()
        return {'id': friendship_id, 'status': 'cancelled'}

    @staticmethod
    def unfriend(db, requester_id: str, other_id: str) -> dict:
        """Remove an active friendship without blocking.

        Either participant can unfriend. The reciprocal Follow rows
        auto-created on accept() are NOT removed — unfriend is the
        symmetric inverse of accept(), follows are independent and
        the user can unfollow separately if they want. This matches
        Twitter/Instagram semantics: "we're no longer friends" does
        not silently remove follows the user may have set up
        themselves.
        """
        from sqlalchemy import text
        if requester_id == other_id:
            raise FriendError("cannot unfriend yourself")
        a, b = _sorted_pair(requester_id, other_id)
        row = db.execute(text(
            "SELECT id, status FROM friendships "
            "WHERE user_a_id = :a AND user_b_id = :b"),
            {'a': a, 'b': b}
        ).fetchone()
        if row is None:
            raise FriendError("not friends")
        fid, status = row[0], row[1]
        if status != 'active':
            raise FriendError(f"cannot unfriend from status={status}")
        db.execute(text("DELETE FROM friendships WHERE id = :id"),
                   {'id': fid})
        db.commit()
        return {'id': fid, 'status': 'unfriended'}

    # ── Block ────────────────────────────────────────────────────

    @staticmethod
    def block(db, blocker_id: str, blocked_id: str,
              reason: Optional[str] = None,
              tenant_id: Optional[str] = None) -> dict:
        """Block a user. Tears down any active friendship and
        triggers the PeerLink trust-ratchet teardown via the
        link_manager (plan Part R.5). Idempotent."""
        from sqlalchemy import text
        if blocker_id == blocked_id:
            raise FriendError("cannot block yourself")

        # Insert/upsert block row.
        bid = str(uuid.uuid4())
        existing = db.execute(text(
            "SELECT id FROM blocks "
            "WHERE blocker_id = :a AND blocked_id = :b"),
            {'a': blocker_id, 'b': blocked_id}
        ).fetchone()
        if existing is None:
            db.execute(text(
                "INSERT INTO blocks "
                "(id, tenant_id, blocker_id, blocked_id, reason, "
                " created_at) "
                "VALUES (:id, :tid, :a, :b, :r, CURRENT_TIMESTAMP)"),
                {'id': bid, 'tid': tenant_id, 'a': blocker_id,
                 'b': blocked_id, 'r': reason})

        # Mark any existing friendship as blocked.
        a, b = _sorted_pair(blocker_id, blocked_id)
        db.execute(text(
            "UPDATE friendships SET status='blocked', "
            "blocked_at=CURRENT_TIMESTAMP "
            "WHERE user_a_id = :a AND user_b_id = :b"),
            {'a': a, 'b': b})
        db.commit()

        # Tear down PeerLink trust (best-effort — module may be
        # unavailable in a test environment).
        try:
            from core.peer_link.link_manager import get_link_manager
            mgr = get_link_manager()
            if mgr.has_link(blocked_id):
                mgr.close_link(blocked_id)
        except Exception:
            pass

        return {'id': existing[0] if existing else bid, 'status': 'blocked'}

    @staticmethod
    def unblock(db, blocker_id: str, blocked_id: str) -> dict:
        """Remove a block row. Friendship row stays in 'blocked'
        state until the blocker explicitly re-friends — unblocking
        does NOT auto-restore an old friendship."""
        from sqlalchemy import text
        result = db.execute(text(
            "DELETE FROM blocks "
            "WHERE blocker_id = :a AND blocked_id = :b"),
            {'a': blocker_id, 'b': blocked_id})
        db.commit()
        return {'unblocked': True}

    # ── Read paths ───────────────────────────────────────────────

    @staticmethod
    def list_friends(db, user_id: str,
                     status: str = 'active') -> List[dict]:
        """Return friends in the given state.

        Default 'active' returns the canonical "my friends" list.
        Pass status='pending' to get incoming + outgoing pending
        requests. status='all' for everything.
        """
        from sqlalchemy import text
        from .models import User

        if status == 'all':
            where = ""
            params = {'uid': user_id}
        else:
            where = "AND status = :st"
            params = {'uid': user_id, 'st': status}

        rows = db.execute(text(
            "SELECT id, user_a_id, user_b_id, status, initiator_id, "
            "       created_at, accepted_at "
            "FROM friendships "
            "WHERE (user_a_id = :uid OR user_b_id = :uid) " + where),
            params
        ).fetchall()

        out = []
        for row in rows:
            other_id = row[2] if row[1] == user_id else row[1]
            other = db.query(User).filter(User.id == other_id).first()
            if not other:
                continue
            out.append({
                'friendship_id': row[0],
                'status': row[3],
                'initiator_id': row[4],
                'is_initiator': row[4] == user_id,
                'created_at': str(row[5]) if row[5] else None,
                'accepted_at': str(row[6]) if row[6] else None,
                'other_user': {
                    'id': other.id,
                    'username': other.username,
                    'display_name': getattr(other, 'display_name', None) or other.username,
                    'avatar_url': getattr(other, 'avatar_url', None),
                    'agent_kind': 'agent' if (getattr(other, 'user_type', '') == 'agent') else 'human',
                },
            })
        return out

    @staticmethod
    def list_blocks(db, user_id: str) -> List[dict]:
        from sqlalchemy import text
        from .models import User
        rows = db.execute(text(
            "SELECT b.id, b.blocked_id, b.reason, b.created_at "
            "FROM blocks b WHERE b.blocker_id = :uid"),
            {'uid': user_id}
        ).fetchall()
        out = []
        for row in rows:
            blocked = db.query(User).filter(User.id == row[1]).first()
            if not blocked:
                continue
            out.append({
                'block_id': row[0],
                'reason': row[2],
                'created_at': str(row[3]) if row[3] else None,
                'blocked_user': {
                    'id': blocked.id,
                    'username': blocked.username,
                },
            })
        return out

    @staticmethod
    def is_friend(db, a: str, b: str) -> bool:
        """Single-row lookup used by privacy.py + autocomplete ranking."""
        from sqlalchemy import text
        ua, ub = _sorted_pair(a, b)
        result = db.execute(text(
            "SELECT 1 FROM friendships "
            "WHERE user_a_id = :a AND user_b_id = :b "
            "AND status = 'active' LIMIT 1"),
            {'a': ua, 'b': ub}
        ).fetchone()
        return result is not None

    # ── Internal helpers ─────────────────────────────────────────

    @staticmethod
    def _notify_request(db, friendship_id: str, from_user_id: str,
                        to_user_id: str):
        try:
            from .services import NotificationService
            NotificationService.create(
                db, user_id=to_user_id, type='friend_request',
                source_user_id=from_user_id,
                target_type='friendship', target_id=friendship_id,
                message="You have a new friend request")
        except Exception as e:
            logger.warning("FriendService: notify_request failed: %s", e)
