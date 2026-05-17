"""
HevolveSocial — Sync deltas + cursor (multi-device backfill / restore).

Phase 7c.6.  Plan reference: sunny-gliding-eich.md, Part R.3.

GET /api/social/sync?since=<cursor>&kinds=<csv> returns the user's
deltas across every social kind they own or can see, since the
cursor.  Used for:

  - New-device login (cold pull): since=0 → full backfill.
  - Reconnect after offline period: since=<last_seen_cursor>.
  - WAMP push tells device "something changed", device pulls the delta.
  - Background catch-up timer when peer-to-peer is silent.

Cursor design (single-server flat/regional/central):
  Cursor = max(timestamp) seen so far.  Server stamps every social
  write with `created_at` / `edited_at` (existing columns), so
  ordering within a single server is monotonic.

  For multi-server (Phase 8 cloud + 9 hardening) the cursor will
  upgrade to a hybrid logical clock (HLC) tuple
    (physical_ms, logical_counter, node_id)
  to handle clock skew across nodes.  This module is the
  layering point — callers always pass an opaque string cursor;
  the service decides how to interpret it.

Per-kind queries respect existing privacy gates:
  - conversations: only ones the user is a member of (memberships table)
  - messages: only in conversations the user is a member of
  - posts / comments: only in communities the user can see (delegate
    to existing visibility checks)
  - friendships / blocks / invites: only ones involving the user
  - mentions: only ones targeting the user
  - memberships: only the user's own rows

Every delta carries the kind + payload + a `cursor` field so the
client can advance its high-water mark deterministically.

Transport: this is request-response only. The client polls (or is
nudged by a WAMP push and then polls). Fan-out from each write to
subscribed clients still happens through the existing MessageBus
(LOCAL → SSE → PEERLINK → CROSSBAR) — sync is the catch-up path,
not the live path.

Zero-loss invariant:
  Every social write that affects user U is tagged with a server
  timestamp >= cursor.  The sync query returns every row strictly
  greater than the cursor.  Therefore the union of (live fan-out
  while online) + (sync deltas while offline) covers every state
  change exactly once at the storage layer.  Client-side dedup by
  primary key removes any double-delivery from overlap.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve_social')


# Default cursor when the client is making its first sync call.
# 1970 epoch ensures every row qualifies for the initial backfill.
_EPOCH_CURSOR = '1970-01-01 00:00:00'


# Whitelisted kinds the sync endpoint can return. Each kind maps to a
# private fetcher below.  Caller-passed `kinds` is intersected with
# this set so unknown kinds are silently dropped.
_VALID_KINDS = (
    'conversations', 'messages',
    'friendships', 'blocks',
    'invites',
    'mentions',
    'memberships',
    'notifications',
)


def _parse_cursor(cursor: Optional[str]) -> Tuple[str, str]:
    """Decode `<timestamp>|<id>` into (timestamp, id).

    Both halves protect against the SQLite second-resolution problem:
    multiple rows can share a `created_at` timestamp, and a cursor
    that only stored the timestamp would either lose those siblings
    (with `>`) or re-emit them forever (with `>=`).  Encoding the
    last-seen primary key as a tiebreaker fixes both — see fetcher
    queries below for the compound `WHERE` shape.

    Bad input → epoch (`'1970-01-01 00:00:00', ''`) so the next
    sync is a full backfill.  Never raises.
    """
    if not cursor or cursor in ('0', '', 'null'):
        return _EPOCH_CURSOR, ''
    raw = str(cursor)
    # Split on '|'; missing delimiter means a legacy timestamp-only
    # cursor — treat the id half as empty (still correct, just slightly
    # less precise on tie-row continuation).
    if '|' in raw:
        ts_part, id_part = raw.split('|', 1)
    else:
        ts_part, id_part = raw, ''
    ts = ts_part.replace('T', ' ').split('.')[0]
    if not ts:
        return _EPOCH_CURSOR, ''
    # Pass-1 N1 fix: removed the dead `len(ts) == 10` date-only
    # branch.  Encoded cursors always include a time component
    # because they come from `str(datetime)` which produces
    # `YYYY-MM-DD HH:MM:SS`.  Any caller passing a bare date string
    # is upgrading to epoch via the ValueError path below, which is
    # the same fallback the date-only branch produced.  Simpler.
    try:
        datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
        return ts, id_part
    except (ValueError, TypeError):
        return _EPOCH_CURSOR, ''


def _normalize_cursor(cursor: Optional[str]) -> str:
    """Backward-compat shim returning just the timestamp portion.

    Existing callers + tests that compare cursor as a timestamp string
    keep working; new fetchers pull the (ts, id) pair via _parse_cursor.
    """
    ts, _ = _parse_cursor(cursor)
    return ts


def _encode_cursor(ts: str, row_id: str) -> str:
    """Encode the compound cursor for client return + next-call use."""
    return f'{ts}|{row_id}' if row_id else ts


def _cursor_predicate(activity_expr: str, cursor_id: str,
                      ts_param: str = 'ts',
                      id_param: str = 'id',
                      table_id_col: str = 'id') -> str:
    """Build the strictly-greater-than cursor WHERE fragment.

    Two shapes:
      - cursor_id == ''  →  `activity > :ts`   (strict, no id branch)
      - cursor_id != ''  →  `(activity > :ts OR (activity = :ts AND id > :id))`

    Reviewer H-NEW-1: when the prior cursor was a bare timestamp
    (encoded as `<ts>` because no id-bearing rows shipped on the
    previous call), the second branch with `:id = ''` matched every
    row at the boundary timestamp because every non-empty id is
    string-greater than ''.  That re-emitted boundary rows on every
    sync forever.  Splitting the branch closes the hole.
    """
    if not cursor_id:
        return f"({activity_expr} > :{ts_param})"
    return (
        f"({activity_expr} > :{ts_param} "
        f"OR ({activity_expr} = :{ts_param} "
        f"AND {table_id_col} > :{id_param}))"
    )


def _tenant_predicate(tenant_id: Optional[str],
                      alias: str = '') -> Tuple[str, Dict[str, Any]]:
    """Return (`AND ...` SQL fragment, params dict) for tenant filtering.

    When `tenant_id` is None (flat/regional pass-through) this is the
    empty string — the WHERE clause is unchanged.  When set, the
    fragment enforces (col == tid OR col IS NULL) so legacy untenanted
    rows remain visible to the tenant for backward compatibility, same
    semantics the ORM-level `tenant_filter.py` listener uses.

    `alias` lets callers prefix the column for joined queries
    (e.g. `'msg'` → `msg.tenant_id`).  Empty string → bare `tenant_id`.

    The `:tid` placeholder is reserved; callers must avoid colliding
    with their own `tid` parameter.  This is checked in tests.

    Pass-1 M6 note — f-string SQL safety:
      The f-string interpolation in the returned fragment is intentional
      and safe.  `alias` is a static string fragment chosen by the
      caller from a closed set ('msg', 'c', 'm', etc.) — never user
      input.  All user-controlled values (`tenant_id`) flow through the
      `:tid` bind parameter which SQLAlchemy escapes.  Future callers
      adding new aliases must keep this invariant: no user data in
      the `alias` argument.

    Pass-5 F10 note — raw-SQL DRY:
      This helper is the canonical tenant-scope predicate for raw
      SQL queries that bypass the ORM tenant_filter listener.  Every
      raw-`text()` query in sync_service / e2e_key_service / call_service
      uses it.  When adding a new raw-SQL service, import this helper
      rather than rolling your own — keeps the loose-mode/strict-mode
      contract in one place (Phase 8 strict mode is enforced by the
      ORM listener, not by this helper, so raw-SQL paths run in
      effectively-loose mode regardless of strict-mode setting; that
      tradeoff is documented at tenant_filter.py module docstring).
    """
    if not tenant_id:
        return '', {}
    prefix = f'{alias}.' if alias else ''
    return (
        f' AND ({prefix}tenant_id = :tid OR {prefix}tenant_id IS NULL)',
        {'tid': tenant_id})


def _max_cursor(*candidates: Optional[str]) -> str:
    """Pick the largest non-null cursor by tuple-comparing (ts, id).

    String compare is INCORRECT here: '|' (0x7C) sorts after every
    digit (0x30-0x39), so a cursor `T0|<uuid>` would compare GREATER
    than a strictly later cursor `T1` (no id) — causing the returned
    cursor to advance to the wrong value and the next sync to either
    re-emit shipped rows or skip future ones.

    Reviewer-flagged bug, regression test in test_phase7c6_sync.py:
    test_max_cursor_tuple_compares_correctly.
    """
    parsed = [_parse_cursor(c) for c in candidates if c]
    if not parsed:
        return _EPOCH_CURSOR
    best_ts, best_id = max(parsed)  # tuple compare: ts first, id tiebreaker
    return _encode_cursor(best_ts, best_id)


class SyncService:
    """All public methods are static. Each takes an explicit `db` so
    they can be called from any context without coupling to Flask `g`.
    """

    @staticmethod
    def deltas(db, user_id: str,
               since: Optional[str] = None,
               kinds: Optional[List[str]] = None,
               limit_per_kind: int = 200,
               tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Return a delta dict + a new cursor.

        Cursor format:  '<timestamp>|<row_id>'  (opaque to client).
        Both halves are required for correctness on dialects that
        store sub-second-precision timestamps as second-resolution
        (SQLite default) — without an id tiebreaker, sibling rows
        sharing a timestamp would either be re-delivered forever
        (`>=`) or silently lost (`>`).
        """
        cursor_ts, cursor_id = _parse_cursor(since)
        if kinds:
            wanted = set(k for k in kinds if k in _VALID_KINDS)
        else:
            wanted = set(_VALID_KINDS)

        deltas: Dict[str, List[dict]] = {}
        max_seen = _encode_cursor(cursor_ts, cursor_id)
        any_capped = False

        fetchers = {
            'conversations': SyncService._conversations_since,
            'messages':      SyncService._messages_since,
            'friendships':   SyncService._friendships_since,
            'blocks':        SyncService._blocks_since,
            'invites':       SyncService._invites_since,
            'mentions':      SyncService._mentions_since,
            'memberships':   SyncService._memberships_since,
            'notifications': SyncService._notifications_since,
        }
        for kind in wanted:
            try:
                # tenant_id is passed to every fetcher so the raw SQL
                # WHERE clause can enforce isolation. The ORM-level
                # tenant_filter listener does NOT fire on raw text()
                # queries, so we have to do it explicitly here —
                # critical privacy gate, reviewer-flagged.
                rows, kind_max, capped = fetchers[kind](
                    db, user_id, cursor_ts, cursor_id, limit_per_kind,
                    tenant_id)
            except Exception as e:
                logger.warning(
                    "SyncService: %s fetch failed: %s", kind, e)
                rows, kind_max, capped = [], max_seen, False
            deltas[kind] = rows
            max_seen = _max_cursor(max_seen, kind_max)
            if capped:
                any_capped = True

        return {
            'cursor': max_seen,
            'has_more': any_capped,
            'deltas': deltas,
        }

    # ── Per-kind fetchers ────────────────────────────────────────
    #
    # Contract: each fetcher receives (db, user_id, cursor, limit) and
    # returns (rows, max_cursor_seen_in_this_kind, hit_limit_bool).
    # rows is a list of plain dicts (JSON-serializable). The `cursor`
    # passed in is the lower bound (strictly greater); rows are ordered
    # by their relevant timestamp ascending so that paginating with the
    # returned max_cursor yields a strict progression on the next call.

    # Compound-cursor SQL pattern used by every fetcher below:
    #
    #   WHERE  activity_ts > :ts
    #     OR  (activity_ts = :ts  AND row_id > :id)
    #
    # The activity_ts is a per-table COALESCE expression that captures
    # the row's "last meaningful change" (create + edit + accept +
    # block + delete folded into one column).  The id tiebreaker
    # handles SQLite's second-resolution timestamps without losing
    # rows.  Each fetcher returns the encoded compound cursor of the
    # last row it shipped so the next call resumes correctly.

    @staticmethod
    def _conversations_since(db, user_id, cursor_ts, cursor_id, limit,
                             tenant_id):
        from sqlalchemy import text
        tenant_sql, tenant_params = _tenant_predicate(tenant_id, alias='c')
        cursor_sql = _cursor_predicate(
            'COALESCE(c.last_message_at, c.created_at)',
            cursor_id, table_id_col='c.id')
        params = {'uid': user_id, 'ts': cursor_ts,
                  'lim': limit + 1}
        if cursor_id:
            params['id'] = cursor_id
        params.update(tenant_params)
        rows = db.execute(text(
            "SELECT c.id, c.kind, c.title, c.created_by, "
            "       c.last_message_at, c.is_locked, c.is_archived, "
            "       c.created_at, "
            "       COALESCE(c.last_message_at, c.created_at) AS act_ts "
            "FROM conversations c "
            "JOIN memberships m ON m.parent_id = c.id "
            "WHERE m.parent_kind = 'conversation' AND m.member_id = :uid "
            f"AND {cursor_sql} "
            f"{tenant_sql} "
            "ORDER BY COALESCE(c.last_message_at, c.created_at) ASC, "
            "         c.id ASC "
            "LIMIT :lim"),
            params
        ).fetchall()
        capped = len(rows) > limit
        rows = rows[:limit]
        out = []
        max_seen = _encode_cursor(cursor_ts, cursor_id)
        for row in rows:
            ts = str(row[8] or '')
            max_seen = _max_cursor(max_seen, _encode_cursor(ts, row[0]))
            out.append({
                'id': row[0], 'kind': row[1], 'title': row[2],
                'created_by': row[3],
                'last_message_at': str(row[4]) if row[4] else None,
                'is_locked': bool(row[5]), 'is_archived': bool(row[6]),
                'created_at': str(row[7]) if row[7] else None,
            })
        return out, max_seen, capped

    @staticmethod
    def _messages_since(db, user_id, cursor_ts, cursor_id, limit,
                        tenant_id):
        from sqlalchemy import text
        tenant_sql, tenant_params = _tenant_predicate(tenant_id, alias='msg')
        cursor_sql = _cursor_predicate(
            'COALESCE(msg.edited_at, msg.created_at)',
            cursor_id, table_id_col='msg.id')
        params = {'uid': user_id, 'ts': cursor_ts,
                  'lim': limit + 1}
        if cursor_id:
            params['id'] = cursor_id
        params.update(tenant_params)
        rows = db.execute(text(
            "SELECT msg.id, msg.parent_kind, msg.parent_id, "
            "       msg.thread_root_id, msg.author_id, msg.agent_kind, "
            "       msg.content, msg.depth, msg.edited_at, "
            "       msg.is_deleted, msg.metadata_json, msg.created_at, "
            "       COALESCE(msg.edited_at, msg.created_at) AS act_ts "
            "FROM messages msg "
            "JOIN memberships m ON m.parent_id = msg.parent_id "
            "WHERE m.parent_kind = 'conversation' AND m.member_id = :uid "
            "AND msg.parent_kind = 'conversation' "
            f"AND {cursor_sql} "
            f"{tenant_sql} "
            "ORDER BY COALESCE(msg.edited_at, msg.created_at) ASC, "
            "         msg.id ASC "
            "LIMIT :lim"),
            params
        ).fetchall()
        capped = len(rows) > limit
        rows = rows[:limit]
        out = []
        max_seen = _encode_cursor(cursor_ts, cursor_id)
        for row in rows:
            ts = str(row[12] or '')
            max_seen = _max_cursor(max_seen, _encode_cursor(ts, row[0]))
            out.append({
                'id': row[0], 'parent_kind': row[1], 'parent_id': row[2],
                'thread_root_id': row[3], 'author_id': row[4],
                'agent_kind': row[5], 'content': row[6],
                'depth': row[7],
                'edited_at': str(row[8]) if row[8] else None,
                'is_deleted': bool(row[9]),
                'metadata_json': row[10],
                'created_at': str(row[11]) if row[11] else None,
            })
        return out, max_seen, capped

    @staticmethod
    def _friendships_since(db, user_id, cursor_ts, cursor_id, limit,
                           tenant_id):
        from sqlalchemy import text
        tenant_sql, tenant_params = _tenant_predicate(tenant_id)
        cursor_sql = _cursor_predicate(
            'COALESCE(blocked_at, accepted_at, created_at)', cursor_id)
        params = {'uid': user_id, 'ts': cursor_ts, 'lim': limit + 1}
        if cursor_id:
            params['id'] = cursor_id
        params.update(tenant_params)
        rows = db.execute(text(
            "SELECT id, user_a_id, user_b_id, status, initiator_id, "
            "       created_at, accepted_at, blocked_at, "
            "       COALESCE(blocked_at, accepted_at, created_at) AS act_ts "
            "FROM friendships "
            "WHERE (user_a_id = :uid OR user_b_id = :uid) "
            f"AND {cursor_sql} "
            f"{tenant_sql} "
            "ORDER BY COALESCE(blocked_at, accepted_at, created_at) ASC, "
            "         id ASC LIMIT :lim"),
            params
        ).fetchall()
        capped = len(rows) > limit
        rows = rows[:limit]
        out = []
        max_seen = _encode_cursor(cursor_ts, cursor_id)
        for row in rows:
            ts = str(row[8] or '')
            max_seen = _max_cursor(max_seen, _encode_cursor(ts, row[0]))
            out.append({
                'id': row[0], 'user_a_id': row[1], 'user_b_id': row[2],
                'status': row[3], 'initiator_id': row[4],
                'created_at': str(row[5]) if row[5] else None,
                'accepted_at': str(row[6]) if row[6] else None,
                'blocked_at': str(row[7]) if row[7] else None,
            })
        return out, max_seen, capped

    @staticmethod
    def _blocks_since(db, user_id, cursor_ts, cursor_id, limit, tenant_id):
        from sqlalchemy import text
        tenant_sql, tenant_params = _tenant_predicate(tenant_id)
        cursor_sql = _cursor_predicate('created_at', cursor_id)
        params = {'uid': user_id, 'ts': cursor_ts, 'lim': limit + 1}
        if cursor_id:
            params['id'] = cursor_id
        params.update(tenant_params)
        rows = db.execute(text(
            "SELECT id, blocker_id, blocked_id, reason, created_at "
            "FROM blocks "
            "WHERE blocker_id = :uid "
            f"AND {cursor_sql} "
            f"{tenant_sql} "
            "ORDER BY created_at ASC, id ASC LIMIT :lim"),
            params
        ).fetchall()
        capped = len(rows) > limit
        rows = rows[:limit]
        out = []
        max_seen = _encode_cursor(cursor_ts, cursor_id)
        for row in rows:
            ts = str(row[4] or '')
            max_seen = _max_cursor(max_seen, _encode_cursor(ts, row[0]))
            out.append({
                'id': row[0], 'blocker_id': row[1], 'blocked_id': row[2],
                'reason': row[3],
                'created_at': str(row[4]) if row[4] else None,
            })
        return out, max_seen, capped

    @staticmethod
    def _invites_since(db, user_id, cursor_ts, cursor_id, limit, tenant_id):
        from sqlalchemy import text
        from .models import User
        u = db.query(User).filter(User.id == user_id).first()
        email_clause = ""
        params = {'uid': user_id, 'ts': cursor_ts, 'lim': limit + 1}
        if cursor_id:
            params['id'] = cursor_id
        if u and u.email:
            email_clause = " OR LOWER(invitee_email) = :em"
            params['em'] = (u.email or '').lower()
        tenant_sql, tenant_params = _tenant_predicate(tenant_id)
        cursor_sql = _cursor_predicate(
            'COALESCE(responded_at, created_at)', cursor_id)
        params.update(tenant_params)
        rows = db.execute(text(
            f"SELECT id, parent_kind, parent_id, invitee_id, "
            f"       invitee_email, invite_code, invited_by, "
            f"       role_offered, status, created_at, "
            f"       expires_at, responded_at, "
            f"       COALESCE(responded_at, created_at) AS act_ts "
            f"FROM invites "
            f"WHERE ( invitee_id = :uid OR invited_by = :uid {email_clause} ) "
            f"AND {cursor_sql} "
            f"{tenant_sql} "
            f"ORDER BY COALESCE(responded_at, created_at) ASC, "
            f"         id ASC LIMIT :lim"),
            params
        ).fetchall()
        capped = len(rows) > limit
        rows = rows[:limit]
        out = []
        max_seen = _encode_cursor(cursor_ts, cursor_id)
        for row in rows:
            ts = str(row[12] or '')
            max_seen = _max_cursor(max_seen, _encode_cursor(ts, row[0]))
            out.append({
                'id': row[0], 'parent_kind': row[1], 'parent_id': row[2],
                'invitee_id': row[3], 'invitee_email': row[4],
                'invite_code': row[5], 'invited_by': row[6],
                'role_offered': row[7], 'status': row[8],
                'created_at': str(row[9]) if row[9] else None,
                'expires_at': str(row[10]) if row[10] else None,
                'responded_at': str(row[11]) if row[11] else None,
            })
        return out, max_seen, capped

    @staticmethod
    def _mentions_since(db, user_id, cursor_ts, cursor_id, limit, tenant_id):
        from sqlalchemy import text
        tenant_sql, tenant_params = _tenant_predicate(tenant_id)
        cursor_sql = _cursor_predicate('created_at', cursor_id)
        params = {'uid': user_id, 'ts': cursor_ts, 'lim': limit + 1}
        if cursor_id:
            params['id'] = cursor_id
        params.update(tenant_params)
        rows = db.execute(text(
            "SELECT id, source_kind, source_id, mentioned_user_id, "
            "       mentioned_kind, agent_owner_id, created_at "
            "FROM mentions "
            "WHERE (mentioned_user_id = :uid OR agent_owner_id = :uid) "
            f"AND {cursor_sql} "
            f"{tenant_sql} "
            "ORDER BY created_at ASC, id ASC LIMIT :lim"),
            params
        ).fetchall()
        capped = len(rows) > limit
        rows = rows[:limit]
        out = []
        max_seen = _encode_cursor(cursor_ts, cursor_id)
        for row in rows:
            ts = str(row[6] or '')
            max_seen = _max_cursor(max_seen, _encode_cursor(ts, row[0]))
            out.append({
                'id': row[0], 'source_kind': row[1], 'source_id': row[2],
                'mentioned_user_id': row[3], 'mentioned_kind': row[4],
                'agent_owner_id': row[5],
                'created_at': str(row[6]) if row[6] else None,
            })
        return out, max_seen, capped

    @staticmethod
    def _memberships_since(db, user_id, cursor_ts, cursor_id, limit,
                           tenant_id):
        from sqlalchemy import text
        tenant_sql, tenant_params = _tenant_predicate(tenant_id)
        cursor_sql = _cursor_predicate('joined_at', cursor_id)
        params = {'uid': user_id, 'ts': cursor_ts, 'lim': limit + 1}
        if cursor_id:
            params['id'] = cursor_id
        params.update(tenant_params)
        rows = db.execute(text(
            "SELECT id, parent_kind, parent_id, member_id, agent_kind, "
            "       role, joined_at, muted_until, notification_pref "
            "FROM memberships "
            "WHERE member_id = :uid "
            f"AND {cursor_sql} "
            f"{tenant_sql} "
            "ORDER BY joined_at ASC, id ASC LIMIT :lim"),
            params
        ).fetchall()
        capped = len(rows) > limit
        rows = rows[:limit]
        out = []
        max_seen = _encode_cursor(cursor_ts, cursor_id)
        for row in rows:
            ts = str(row[6] or '')
            max_seen = _max_cursor(max_seen, _encode_cursor(ts, row[0]))
            out.append({
                'id': row[0], 'parent_kind': row[1], 'parent_id': row[2],
                'member_id': row[3], 'agent_kind': row[4],
                'role': row[5],
                'joined_at': str(row[6]) if row[6] else None,
                'muted_until': str(row[7]) if row[7] else None,
                'notification_pref': row[8],
            })
        return out, max_seen, capped

    @staticmethod
    def _notifications_since(db, user_id, cursor_ts, cursor_id, limit,
                             tenant_id):
        from sqlalchemy import text
        tenant_sql, tenant_params = _tenant_predicate(tenant_id)
        cursor_sql = _cursor_predicate('created_at', cursor_id)
        params = {'uid': user_id, 'ts': cursor_ts, 'lim': limit + 1}
        if cursor_id:
            params['id'] = cursor_id
        params.update(tenant_params)
        rows = db.execute(text(
            "SELECT id, type, source_user_id, target_type, target_id, "
            "       message, is_read, created_at "
            "FROM notifications "
            "WHERE user_id = :uid "
            f"AND {cursor_sql} "
            f"{tenant_sql} "
            "ORDER BY created_at ASC, id ASC LIMIT :lim"),
            params
        ).fetchall()
        capped = len(rows) > limit
        rows = rows[:limit]
        out = []
        max_seen = _encode_cursor(cursor_ts, cursor_id)
        for row in rows:
            ts = str(row[7] or '')
            max_seen = _max_cursor(max_seen, _encode_cursor(ts, row[0]))
            out.append({
                'id': row[0], 'type': row[1], 'source_user_id': row[2],
                'target_type': row[3], 'target_id': row[4],
                'message': row[5], 'is_read': bool(row[6]),
                'created_at': str(row[7]) if row[7] else None,
            })
        return out, max_seen, capped


__all__ = ['SyncService']
