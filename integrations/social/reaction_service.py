"""
HevolveSocial — Emoji reactions on posts / comments / messages.

Phase 7c.4. Plan reference: sunny-gliding-eich.md, Part E.6.

Coexists with the existing binary VoteService — votes are aggregate
karma, reactions are emoji. Both can be applied to the same source
without conflict; the data flows separately.

Polymorphic by (source_kind, source_id):
  source_kind ∈ {'post', 'comment', 'message'}.
  source_id is the primary key of the source row.

Toggle semantics:
  - First call by a user with a given emoji on a given source → INSERT.
  - Second call by the same user with the same emoji → DELETE.
  - Other users' reactions are unaffected.
  - The UNIQUE INDEX (source_kind, source_id, user_id, emoji) guards
    against accidental duplicates from concurrent toggles.

Allowed emoji set is a small whitelist today; tenants can override
via per-tenant settings (Phase 8) — until then everyone shares the
same list to keep the UX coherent.

Block check: a user reacting to content authored by someone they've
blocked (or who blocked them) is silently no-op'd.  This matches the
mention/notification pattern from earlier phases.

Transport:
  - INSERT/DELETE go through the regular DB session; no separate
    fan-out leg today (reactions are best surfaced via /sync deltas
    and a per-source aggregate fetch).
  - When `reactions` flag is on AND the source has subscribers, the
    aggregate count is published over the existing post/comment/message
    realtime topic via NotificationService — no privileged path.
"""

from __future__ import annotations

import logging
import uuid
from typing import List, Optional, Tuple

logger = logging.getLogger('hevolve_social')


_VALID_SOURCE_KINDS = ('post', 'comment', 'message')

# Whitelist matches plan Part E.6.  Per-tenant override comes in Phase 8.
ALLOWED_EMOJI = frozenset([
    '👍', '❤️', '🔥', '😂', '😢', '😮', '🎉', '👎', '🚀',
])

_MAX_USERS_IN_PREVIEW = 5


class ReactionError(ValueError):
    pass


def _is_blocked_either_way(db, x: str, y: str) -> bool:
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


def _author_of(db, source_kind: str, source_id: str,
               tenant_id: Optional[str] = None) -> Optional[str]:
    """Look up the author_id of a post/comment/message.

    Used for the block check — we don't want a user reacting to
    content from someone they've blocked (or who blocked them) and
    surfacing the reaction back.

    Tenant gate (reviewer C-NEW-2): when `tenant_id` is set, the
    lookup additionally filters to rows where the source's tenant
    matches OR is NULL (legacy pass-through, same semantics as
    sync_service._tenant_predicate).  Without this, an attacker who
    leaks a `source_id` from a different tenant could trick the
    reaction service into letting them react across tenants —
    counts would then leak across the boundary in `list_for`.
    """
    from sqlalchemy import text
    table = {'post': 'posts', 'comment': 'comments',
             'message': 'messages'}.get(source_kind)
    if not table:
        return None
    sql = f"SELECT author_id FROM {table} WHERE id = :sid"
    params = {'sid': source_id}
    if tenant_id:
        sql += " AND (tenant_id = :tid OR tenant_id IS NULL)"
        params['tid'] = tenant_id
    try:
        row = db.execute(text(sql), params).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _public_count_for(db, source_kind: str, source_id: str, emoji: str,
                      tenant_id: Optional[str] = None) -> int:
    """Aggregate count for a single (source, emoji) — used so a
    silent-noop response carries the SAME shape a successful response
    would, so an attacker can't infer block state by comparing
    `count: 0` vs `count: N` (reviewer C-NEW-1).
    """
    from sqlalchemy import text
    sql = ("SELECT COUNT(*) FROM reactions "
           "WHERE source_kind = :sk AND source_id = :sid AND emoji = :em")
    params = {'sk': source_kind, 'sid': source_id, 'em': emoji}
    if tenant_id:
        sql += " AND (tenant_id = :tid OR tenant_id IS NULL)"
        params['tid'] = tenant_id
    try:
        row = db.execute(text(sql), params).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


class ReactionService:
    """All public methods are static.  Each takes an explicit `db` so
    they can be called from any context without coupling to Flask `g`.
    """

    @staticmethod
    def toggle(db, source_kind: str, source_id: str, user_id: str,
               emoji: str,
               tenant_id: Optional[str] = None) -> dict:
        """Toggle the (user_id, emoji) reaction on the source.

        Returns:
          { 'action': 'added'|'removed', 'emoji': str,
            'count': int, 'me_reacted': bool }

        Refuses if:
          - source_kind not in valid set
          - emoji not in ALLOWED_EMOJI
          - source row doesn't exist
          - either party has blocked the other
        """
        from sqlalchemy import text

        if source_kind not in _VALID_SOURCE_KINDS:
            raise ReactionError(f"invalid source_kind: {source_kind}")
        if emoji not in ALLOWED_EMOJI:
            raise ReactionError(f"emoji not allowed: {emoji}")

        # Confirm the source exists + capture author for block check.
        # Tenant-scoped lookup (C-NEW-2) so a cross-tenant source_id
        # never proceeds.  When tenant_id is None (flat/regional)
        # the lookup is unscoped — same pass-through semantics as
        # sync_service.
        author_id = _author_of(db, source_kind, source_id, tenant_id)
        if author_id is None:
            raise ReactionError(f"{source_kind} not found")
        # Reactor is allowed to react to their OWN content.
        if author_id != user_id and _is_blocked_either_way(
                db, user_id, author_id):
            # Silent no-op — but return the SAME shape (real public
            # count for that emoji) so an attacker can't compare to
            # other endpoints and infer block state (C-NEW-1).
            return {
                'action': 'noop', 'emoji': emoji,
                'count': _public_count_for(
                    db, source_kind, source_id, emoji, tenant_id),
                'me_reacted': False,
            }

        # Check if reaction already exists.
        existing = db.execute(text(
            "SELECT id FROM reactions "
            "WHERE source_kind = :sk AND source_id = :sid "
            "AND user_id = :uid AND emoji = :em LIMIT 1"),
            {'sk': source_kind, 'sid': source_id,
             'uid': user_id, 'em': emoji}
        ).fetchone()

        if existing:
            # Toggle off — remove it.
            db.execute(text(
                "DELETE FROM reactions WHERE id = :rid"),
                {'rid': existing[0]})
            db.commit()
            action = 'removed'
            me_reacted = False
        else:
            # Toggle on — insert (UNIQUE INDEX guards against race).
            try:
                db.execute(text(
                    "INSERT INTO reactions "
                    "(id, tenant_id, source_kind, source_id, user_id, "
                    " emoji, created_at) "
                    "VALUES (:id, :tid, :sk, :sid, :uid, :em, "
                    " CURRENT_TIMESTAMP)"),
                    {'id': str(uuid.uuid4()), 'tid': tenant_id,
                     'sk': source_kind, 'sid': source_id,
                     'uid': user_id, 'em': emoji})
                db.commit()
            except Exception as e:
                # UNIQUE violation = concurrent toggle won — treat as
                # successful add (the row is there).
                db.rollback()
                logger.info(
                    "ReactionService: concurrent toggle resolved as "
                    "no-op (%s)", e)
            action = 'added'
            me_reacted = True

        # Updated count for this emoji on this source — tenant-scoped
        # so M-NEW-4: cross-tenant rows can't pollute the count.
        count = _public_count_for(
            db, source_kind, source_id, emoji, tenant_id)

        return {
            'action': action, 'emoji': emoji,
            'count': count, 'me_reacted': me_reacted,
        }

    # Hard cap on rows fetched for aggregation — defense against a
    # source with millions of reactions creating a Python-memory hot
    # spot.  Reviewer M-NEW-2.  Counts are still aggregated in SQL
    # via GROUP BY for the dominant emojis.
    _LIST_FOR_HARD_CAP = 10000

    @staticmethod
    def list_for(db, source_kind: str, source_id: str,
                 viewer_id: Optional[str] = None,
                 tenant_id: Optional[str] = None) -> List[dict]:
        """Return aggregated reactions on a source.

        Shape per emoji:
          { 'emoji': str, 'count': int,
            'users': [<first 5 user_ids>],
            'me_reacted': bool }

        Sorted by count DESC then emoji ASC for stable client display.

        Two-phase aggregation (M-NEW-2 fix):
          1. SQL GROUP BY emoji → counts (cheap, indexed).
          2. Bounded SELECT for the user-preview slice (LIMIT 10k) —
             enough to fill the 5-user preview per emoji on any
             realistic post; oldest reactors win the preview slot to
             match the deterministic created_at ASC ordering callers
             have come to rely on.
        """
        from sqlalchemy import text
        if source_kind not in _VALID_SOURCE_KINDS:
            raise ReactionError(f"invalid source_kind: {source_kind}")

        tenant_clause = ""
        params = {'sk': source_kind, 'sid': source_id}
        if tenant_id:
            tenant_clause = " AND (tenant_id = :tid OR tenant_id IS NULL)"
            params['tid'] = tenant_id

        # Phase 1 — counts in SQL.
        count_rows = db.execute(text(
            f"SELECT emoji, COUNT(*) AS c FROM reactions "
            f"WHERE source_kind = :sk AND source_id = :sid"
            f"{tenant_clause} "
            f"GROUP BY emoji"),
            params
        ).fetchall()
        if not count_rows:
            return []
        counts = {row[0]: int(row[1]) for row in count_rows}

        # Phase 2 — bounded user preview.  We pull at most
        # _LIST_FOR_HARD_CAP rows so a viral post with 1M reactions
        # doesn't blow up the Python heap; the GROUP BY counts above
        # are exact regardless of this cap.
        preview_params = dict(params)
        preview_params['lim'] = ReactionService._LIST_FOR_HARD_CAP
        preview_rows = db.execute(text(
            f"SELECT emoji, user_id FROM reactions "
            f"WHERE source_kind = :sk AND source_id = :sid"
            f"{tenant_clause} "
            f"ORDER BY emoji ASC, created_at ASC LIMIT :lim"),
            preview_params
        ).fetchall()

        users_by_emoji: dict = {}
        me_by_emoji: dict = {}
        for emoji, user_id in preview_rows:
            slot = users_by_emoji.setdefault(emoji, [])
            if len(slot) < _MAX_USERS_IN_PREVIEW:
                slot.append(user_id)
            if viewer_id and user_id == viewer_id:
                me_by_emoji[emoji] = True

        out = []
        for emoji, count in counts.items():
            out.append({
                'emoji': emoji, 'count': count,
                'users': users_by_emoji.get(emoji, []),
                'me_reacted': me_by_emoji.get(emoji, False),
            })
        # If the viewer's reaction was beyond the preview cap, fall back
        # to a direct lookup for me_reacted so the flag is never wrong.
        if viewer_id:
            for slot in out:
                if not slot['me_reacted']:
                    found = db.execute(text(
                        f"SELECT 1 FROM reactions "
                        f"WHERE source_kind = :sk AND source_id = :sid "
                        f"AND emoji = :em AND user_id = :uid"
                        f"{tenant_clause} LIMIT 1"),
                        {**params, 'em': slot['emoji'], 'uid': viewer_id}
                    ).fetchone()
                    if found:
                        slot['me_reacted'] = True

        out.sort(key=lambda r: (-r['count'], r['emoji']))
        return out

    @staticmethod
    def remove(db, source_kind: str, source_id: str, user_id: str,
               emoji: str,
               tenant_id: Optional[str] = None) -> dict:
        """Explicit remove (idempotent — no-op if not present).

        Used by the DELETE endpoint when we want delete-only semantics
        rather than toggle (e.g., when the client is reconciling local
        state and wants to remove without risking re-add).
        """
        from sqlalchemy import text
        if source_kind not in _VALID_SOURCE_KINDS:
            raise ReactionError(f"invalid source_kind: {source_kind}")
        if emoji not in ALLOWED_EMOJI:
            raise ReactionError(f"emoji not allowed: {emoji}")
        delete_params = {'sk': source_kind, 'sid': source_id,
                         'uid': user_id, 'em': emoji}
        # Tenant scope on the DELETE so a leaked source_id from
        # another tenant can't be used to mass-clear someone else's
        # reactions (C-NEW-2 mirror for the delete path).
        tenant_clause = ""
        if tenant_id:
            tenant_clause = " AND (tenant_id = :tid OR tenant_id IS NULL)"
            delete_params['tid'] = tenant_id
        db.execute(text(
            f"DELETE FROM reactions "
            f"WHERE source_kind = :sk AND source_id = :sid "
            f"AND user_id = :uid AND emoji = :em{tenant_clause}"),
            delete_params)
        db.commit()
        count = _public_count_for(
            db, source_kind, source_id, emoji, tenant_id)
        return {'action': 'removed', 'emoji': emoji,
                'count': count, 'me_reacted': False}
