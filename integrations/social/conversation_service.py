"""
HevolveSocial — Conversations (DM/group) + Messages.

Phase 7c.3.  Plan reference: sunny-gliding-eich.md, Part C.2 + Part E.3.

Naming distinctions (preserved exactly so we don't collide with existing
HARTOS surfaces):
  - `conversations` table (NEW, this module) — internal DM + group chat.
  - `conversation_entries` table (legacy, untouched) — HARTOS 31-channel
    external adapter (Telegram / WhatsApp / Discord / etc.).
  - `conversation` table (legacy, untouched) — single-user Q/A history.

Two kinds of conversation:
  - 'dm':    exactly 2 members, no title, idempotent dedup via member_hash.
  - 'group': 2+ members, has a title, supports add/remove members.

Conversation membership lives in the polymorphic `memberships` table
(v41) with parent_kind='conversation'. One source of truth for community
+ conversation membership rosters.

Messages flow:
  - send(): insert Message row, run MentionService.parse_and_record on
    the content (which fires agent dispatch + dual-notify), update
    conversations.last_message_at, fan out via NotificationService for
    offline members.
  - list(): paginated by `before` cursor (created_at-based).
  - edit(): caller's own message, within 24h, updates content +
    edited_at.
  - soft_delete(): caller's own; sets is_deleted=True, content='[deleted]'.

Block check: a sender's message to a conversation that contains a user
who blocked them is allowed (group-message); the BLOCK semantics are
about not seeing/being notified, not about being silently hidden from
groups they're already in. (See plan K.2.) For DMs: blocking severs
the DM creation path — `create_dm` refuses if either party blocks the
other.

Transport: NotificationService.create publishes via MessageBus
(LOCAL → SSE → PEERLINK → CROSSBAR) — same fan-out the rest of the
social stack uses. Typing + read-receipt are best-effort WAMP emits;
the DB never persists them (per plan E.3).

Agent reply:
  When a message contains an @-mention to an agent, MentionService's
  parse_and_record path fires `agentic_router.dispatch_to_agent` with
  source_kind='message'.  The dispatch worker's `_post_agent_reply`
  handles the 'message' kind (added in this session) and posts the
  agent's reply as a new Message row authored by the agent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

logger = logging.getLogger('hevolve_social')

_VALID_KINDS = ('dm', 'group')
_EDIT_WINDOW = timedelta(hours=24)
_MAX_MESSAGE_LEN = 8000


class ConversationError(ValueError):
    pass


def _member_hash(member_ids: List[str]) -> str:
    """SHA-256 of the sorted, comma-joined member IDs.

    Used as a stable dedup key for DMs so re-creating a DM between A
    and B returns the existing row.  Sorted so order doesn't matter.
    """
    sorted_ids = sorted(set(str(x) for x in member_ids if x))
    raw = ','.join(sorted_ids).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _is_blocked_either_way(db, x: str, y: str) -> bool:
    """Match the same helper used by InviteService / FriendService."""
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


def _ensure_member(db, conv_id: str, member_id: str,
                   tenant_id: Optional[str], role: str = 'member') -> None:
    """Idempotent insert into the polymorphic memberships table."""
    from sqlalchemy import text
    dialect = db.bind.dialect.name if db.bind is not None else 'sqlite'
    if dialect == 'sqlite':
        stmt = ("INSERT OR IGNORE INTO memberships "
                "(id, tenant_id, parent_kind, parent_id, member_id, "
                " agent_kind, role, joined_at) "
                "VALUES (:id, :tid, 'conversation', :pid, :mid, 'human', "
                " :role, CURRENT_TIMESTAMP)")
    else:
        stmt = ("INSERT INTO memberships "
                "(id, tenant_id, parent_kind, parent_id, member_id, "
                " agent_kind, role, joined_at) "
                "VALUES (:id, :tid, 'conversation', :pid, :mid, 'human', "
                " :role, CURRENT_TIMESTAMP) "
                "ON CONFLICT DO NOTHING")
    db.execute(text(stmt), {
        'id': str(uuid.uuid4()), 'tid': tenant_id,
        'pid': conv_id, 'mid': member_id, 'role': role})


def _list_member_ids(db, conv_id: str) -> List[str]:
    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT member_id FROM memberships "
        "WHERE parent_kind = 'conversation' AND parent_id = :pid "
        "ORDER BY joined_at"),
        {'pid': conv_id}
    ).fetchall()
    return [r[0] for r in rows]


def _is_member(db, conv_id: str, user_id: str) -> bool:
    from sqlalchemy import text
    result = db.execute(text(
        "SELECT 1 FROM memberships "
        "WHERE parent_kind='conversation' AND parent_id=:pid "
        "AND member_id=:mid LIMIT 1"),
        {'pid': conv_id, 'mid': user_id}
    ).fetchone()
    return result is not None


class ConversationService:
    """All public methods are static.  Each takes an explicit `db` so
    they can be called from any context without coupling to Flask `g`.
    """

    # ── Create ───────────────────────────────────────────────────

    @staticmethod
    def create(db, kind: str, member_ids: List[str], created_by: str,
               title: Optional[str] = None,
               tenant_id: Optional[str] = None) -> dict:
        """Create a DM (idempotent dedup) or group conversation.

        DM rules:
          - exactly 2 members (caller's id must be in member_ids)
          - same A↔B pair returns the existing row
          - refuses if either party blocks the other

        Group rules:
          - 2+ members (caller is auto-added if missing)
          - title can be empty; defaults to comma-joined display names
        """
        from sqlalchemy import text
        if kind not in _VALID_KINDS:
            raise ConversationError(f"invalid kind: {kind}")

        # Normalize member set, ensure caller is in it.
        member_set = set(str(m) for m in (member_ids or []) if m)
        member_set.add(str(created_by))
        members = sorted(member_set)
        if len(members) < 2:
            raise ConversationError("conversations need >= 2 members")
        if kind == 'dm' and len(members) != 2:
            raise ConversationError("dm requires exactly 2 members")
        if kind == 'dm':
            other = next(m for m in members if m != created_by)
            if _is_blocked_either_way(db, created_by, other):
                raise ConversationError("cannot start DM with blocked user")

        member_hash = _member_hash(members) if kind == 'dm' else None

        # DM dedup: SELECT existing row first.
        if kind == 'dm':
            existing = db.execute(text(
                "SELECT id FROM conversations "
                "WHERE kind = 'dm' AND member_hash = :h"),
                {'h': member_hash}
            ).fetchone()
            if existing:
                return ConversationService.get(db, existing[0])

        cid = str(uuid.uuid4())
        db.execute(text(
            "INSERT INTO conversations "
            "(id, tenant_id, kind, title, created_by, member_hash, "
            " is_locked, is_archived, created_at) "
            "VALUES (:id, :tid, :kind, :title, :cb, :mh, 0, 0, "
            " CURRENT_TIMESTAMP)"),
            {'id': cid, 'tid': tenant_id, 'kind': kind,
             'title': title, 'cb': created_by, 'mh': member_hash})

        # Insert memberships. Owner = created_by, role='admin' for groups.
        for mid in members:
            role = ('admin' if (kind == 'group' and mid == created_by)
                    else 'member')
            _ensure_member(db, cid, mid, tenant_id, role=role)

        db.commit()
        return ConversationService.get(db, cid)

    # ── Read ─────────────────────────────────────────────────────

    @staticmethod
    def get(db, conv_id: str) -> Optional[dict]:
        from sqlalchemy import text
        row = db.execute(text(
            "SELECT id, kind, title, created_by, last_message_at, "
            "       is_locked, is_archived, created_at "
            "FROM conversations WHERE id = :id"),
            {'id': conv_id}
        ).fetchone()
        if row is None:
            return None
        return {
            'id': row[0], 'kind': row[1], 'title': row[2],
            'created_by': row[3],
            'last_message_at': str(row[4]) if row[4] else None,
            'is_locked': bool(row[5]), 'is_archived': bool(row[6]),
            'created_at': str(row[7]) if row[7] else None,
            'members': _list_member_ids(db, row[0]),
        }

    @staticmethod
    def list_for_user(db, user_id: str,
                      include_archived: bool = False,
                      limit: int = 50) -> List[dict]:
        """Conversations the user is a member of, newest first."""
        from sqlalchemy import text
        # Join via memberships polymorphic table.
        where = ("WHERE m.parent_kind = 'conversation' AND m.member_id = :uid")
        if not include_archived:
            where += " AND c.is_archived = 0"
        rows = db.execute(text(
            f"SELECT c.id, c.kind, c.title, c.created_by, c.last_message_at, "
            f"       c.is_locked, c.is_archived, c.created_at "
            f"FROM conversations c "
            f"JOIN memberships m ON m.parent_id = c.id "
            f"{where} "
            f"ORDER BY COALESCE(c.last_message_at, c.created_at) DESC "
            f"LIMIT :lim"),
            {'uid': user_id, 'lim': limit}
        ).fetchall()
        out = []
        for row in rows:
            out.append({
                'id': row[0], 'kind': row[1], 'title': row[2],
                'created_by': row[3],
                'last_message_at': str(row[4]) if row[4] else None,
                'is_locked': bool(row[5]), 'is_archived': bool(row[6]),
                'created_at': str(row[7]) if row[7] else None,
                'members': _list_member_ids(db, row[0]),
            })
        return out

    # ── Member management (group only) ───────────────────────────

    @staticmethod
    def add_member(db, conv_id: str, requester_id: str, new_member_id: str,
                   tenant_id: Optional[str] = None) -> dict:
        """Add a member to a group conversation. Caller must be admin."""
        from sqlalchemy import text
        conv = ConversationService.get(db, conv_id)
        if conv is None:
            raise ConversationError("conversation not found")
        if conv['kind'] != 'group':
            raise ConversationError("cannot add members to a DM")
        # Caller must be admin (or owner).
        role_row = db.execute(text(
            "SELECT role FROM memberships "
            "WHERE parent_kind='conversation' AND parent_id=:pid "
            "AND member_id=:mid"),
            {'pid': conv_id, 'mid': requester_id}
        ).fetchone()
        if role_row is None or role_row[0] not in ('admin', 'owner'):
            raise ConversationError("only admins can add members")
        _ensure_member(db, conv_id, new_member_id, tenant_id, role='member')
        db.commit()
        return ConversationService.get(db, conv_id)

    @staticmethod
    def remove_member(db, conv_id: str, requester_id: str,
                      target_id: str) -> dict:
        """Remove a member from a group. Admin can remove anyone; member
        can remove themselves (leave)."""
        from sqlalchemy import text
        conv = ConversationService.get(db, conv_id)
        if conv is None:
            raise ConversationError("conversation not found")
        if conv['kind'] != 'group':
            raise ConversationError("cannot remove members from a DM")
        if requester_id != target_id:
            role_row = db.execute(text(
                "SELECT role FROM memberships "
                "WHERE parent_kind='conversation' AND parent_id=:pid "
                "AND member_id=:mid"),
                {'pid': conv_id, 'mid': requester_id}
            ).fetchone()
            if role_row is None or role_row[0] not in ('admin', 'owner'):
                raise ConversationError("only admins can remove others")
        db.execute(text(
            "DELETE FROM memberships "
            "WHERE parent_kind='conversation' AND parent_id=:pid "
            "AND member_id=:mid"),
            {'pid': conv_id, 'mid': target_id})
        db.commit()
        return ConversationService.get(db, conv_id)

    # ── Send / list / edit / delete messages ─────────────────────

    @staticmethod
    def send_message(db, conv_id: str, author_id: str, content: str,
                     thread_root_id: Optional[str] = None,
                     metadata: Optional[dict] = None,
                     tenant_id: Optional[str] = None) -> dict:
        """Insert a Message row + run MentionService + bump
        last_message_at + fire NotificationService for non-author members.

        Phase 9.B: when the `e2e_dms` server flag is on AND the
        conversation has `settings.e2e_enabled=True`, the plaintext
        is encrypted via Double Ratchet into one envelope per active
        recipient (e2e_dm_pipeline.encrypt_for_conversation), and
        `messages.content` is replaced with '[encrypted]' so legacy
        readers + admin tooling don't trip on null.  Mention parsing
        + notifications run on the PLAINTEXT (server-side, sender-
        side only) — the NotificationService snippet is bounded to
        140 chars, so no risk of leaking long content into the
        offline-recipient inbox.

        Returns the message dict the caller embeds in the API response.
        Encrypted DM responses include `is_encrypted=True` and
        `recipients=[ids]` so the sender knows who got an envelope.
        """
        from sqlalchemy import text
        if not content or not content.strip():
            raise ConversationError("empty message")
        if len(content) > _MAX_MESSAGE_LEN:
            raise ConversationError(
                f"message too long (>{_MAX_MESSAGE_LEN} chars)")
        if not _is_member(db, conv_id, author_id):
            raise ConversationError("not a conversation member")

        # Phase 9.B encryption decision.  Pre-computed so we know
        # whether to store '[encrypted]' or the plaintext below.
        is_encrypted = False
        try:
            from . import e2e_dm_pipeline
            is_encrypted = e2e_dm_pipeline.should_encrypt(db, conv_id)
        except Exception as e:
            logger.warning(
                "ConversationService.send_message: e2e flag check "
                "failed (falling back to plaintext): %s", e)

        stored_content = '[encrypted]' if is_encrypted else content

        mid = str(uuid.uuid4())
        meta_json = json.dumps(metadata) if metadata else None
        db.execute(text(
            "INSERT INTO messages "
            "(id, tenant_id, parent_kind, parent_id, thread_root_id, "
            " author_id, agent_kind, content, depth, is_deleted, "
            " metadata_json, created_at) "
            "VALUES (:id, :tid, 'conversation', :pid, :tr, :aid, "
            " 'human', :c, 0, 0, :meta, CURRENT_TIMESTAMP)"),
            {'id': mid, 'tid': tenant_id, 'pid': conv_id,
             'tr': thread_root_id, 'aid': author_id, 'c': stored_content,
             'meta': meta_json})
        db.execute(text(
            "UPDATE conversations SET last_message_at = CURRENT_TIMESTAMP "
            "WHERE id = :pid"),
            {'pid': conv_id})
        db.commit()

        # Phase 9.B: write per-recipient envelopes AFTER the messages
        # row commits (envelopes FK to messages.id).  Failures here
        # don't roll back the message — the caller's send_message
        # response surfaces `recipients` so the sender can retry the
        # encrypt path if needed.  This matches the existing pattern
        # where mention parsing failures don't abort the send.
        delivered = []
        if is_encrypted:
            try:
                from . import e2e_dm_pipeline
                delivered = e2e_dm_pipeline.encrypt_for_conversation(
                    db, conv_id, author_id, mid, content,
                    tenant_id=tenant_id)
                db.commit()
            except Exception as e:
                logger.warning(
                    "ConversationService.send_message: e2e encrypt "
                    "failed; envelopes incomplete (recipients may not "
                    "be able to decrypt): %s", e)

        # Mention parsing — same path post/comment use, source_kind='message'
        # so MentionService records rows AND dispatches @-mentioned agents.
        mentions = []
        try:
            from .mention_service import MentionService
            mentions = MentionService.parse_and_record(
                db, source_kind='message', source_id=mid,
                content=content, author_id=author_id,
                tenant_id=tenant_id)
        except Exception as e:
            logger.warning("ConversationService.send_message: mention "
                           "parse failed: %s", e)

        # Notify other members (in-app notification per plan B.5 + R.2).
        # The notification snippet is bounded to 140 chars by
        # NotificationService.create — for encrypted DMs we send a
        # neutral placeholder so plaintext doesn't leak via the
        # offline-recipient notification fan-out.
        notify_snippet = ('[encrypted message]' if is_encrypted
                          else content)
        ConversationService._notify_members(
            db, conv_id, author_id, mid, notify_snippet)

        return {
            'id': mid, 'parent_kind': 'conversation', 'parent_id': conv_id,
            'thread_root_id': thread_root_id,
            'author_id': author_id, 'agent_kind': 'human',
            'content': content, 'mentions': mentions,
            'metadata': metadata,
            'is_encrypted': is_encrypted,
            'recipients': delivered,
        }

    @staticmethod
    def list_messages(db, conv_id: str, requester_id: str,
                      before: Optional[str] = None,
                      limit: int = 50) -> List[dict]:
        """Paginated message history.  `before` is a created_at cursor
        (ISO datetime string) — return messages strictly older than
        that.  Default order: newest first.
        """
        from sqlalchemy import text
        if not _is_member(db, conv_id, requester_id):
            raise ConversationError("not a conversation member")
        params = {'pid': conv_id, 'lim': max(1, min(int(limit), 200))}
        where = ("WHERE parent_kind='conversation' AND parent_id=:pid "
                 "AND is_deleted = 0")
        if before:
            where += " AND created_at < :before"
            params['before'] = before
        rows = db.execute(text(
            f"SELECT id, thread_root_id, author_id, agent_kind, content, "
            f"       edited_at, metadata_json, created_at "
            f"FROM messages {where} "
            f"ORDER BY created_at DESC LIMIT :lim"),
            params
        ).fetchall()

        # Phase 9.B: when this conversation is e2e-encrypted, the
        # stored content is '[encrypted]' for every message.  We
        # decrypt the requester's per-recipient envelope into the
        # response so each user sees their own plaintext.  Other
        # callers (admin tooling, sync workers) that fetch via the
        # raw messages table still see the placeholder, preserving
        # the property that the server CAN'T read encrypted DMs
        # outside the requester's own message-list call.
        try:
            from . import e2e_dm_pipeline
            do_decrypt = e2e_dm_pipeline.should_encrypt(db, conv_id)
        except Exception:
            do_decrypt = False

        out = []
        for row in rows:
            mid, troot, aid, akind, content, edited, meta, created = row
            if do_decrypt and content == '[encrypted]':
                try:
                    from . import e2e_dm_pipeline
                    plaintext = e2e_dm_pipeline.decrypt_for_recipient(
                        db, conv_id, mid, requester_id)
                    if plaintext is not None:
                        content = plaintext
                except Exception as e:
                    logger.warning(
                        "ConversationService.list_messages: decrypt "
                        "failed for message %s: %s (returning placeholder)",
                        mid, e)
            out.append({
                'id': mid, 'parent_id': conv_id,
                'thread_root_id': troot,
                'author_id': aid, 'agent_kind': akind,
                'content': content,
                'edited_at': str(edited) if edited else None,
                'metadata': json.loads(meta) if meta else None,
                'created_at': str(created) if created else None,
                'is_encrypted': do_decrypt,
            })
        return out

    @staticmethod
    def edit_message(db, message_id: str, requester_id: str,
                     new_content: str) -> dict:
        from sqlalchemy import text
        if not new_content or not new_content.strip():
            raise ConversationError("empty message")
        row = db.execute(text(
            "SELECT author_id, created_at, parent_id "
            "FROM messages WHERE id = :id"),
            {'id': message_id}
        ).fetchone()
        if row is None:
            raise ConversationError("message not found")
        author_id, created_at, conv_id = row
        if author_id != requester_id:
            raise ConversationError("only author can edit")
        # 24h edit window. If the stored created_at is unparseable
        # (corrupted column or schema drift), refuse the edit rather
        # than silently allow forever — N4, reviewer-flagged.  The
        # safe default is "no edit" not "edit anytime".
        if isinstance(created_at, str):
            try:
                created_dt = datetime.strptime(
                    created_at.split('.')[0].replace('T', ' '),
                    '%Y-%m-%d %H:%M:%S')
            except Exception:
                raise ConversationError(
                    "cannot determine edit window from stored timestamp")
        else:
            created_dt = created_at
        if datetime.utcnow() - created_dt > _EDIT_WINDOW:
            raise ConversationError("edit window has passed")
        db.execute(text(
            "UPDATE messages SET content = :c, edited_at = CURRENT_TIMESTAMP "
            "WHERE id = :id"),
            {'c': new_content, 'id': message_id})
        db.commit()

        # Re-run mention diff so newly-added @-mentions fire and
        # removed ones get cleaned up.
        try:
            from .mention_service import MentionService
            MentionService.parse_and_record(
                db, source_kind='message', source_id=message_id,
                content=new_content, author_id=requester_id)
        except Exception as e:
            logger.warning("ConversationService.edit_message: mention "
                           "diff failed: %s", e)

        return {'id': message_id, 'content': new_content,
                'edited_at': datetime.utcnow().isoformat()}

    @staticmethod
    def soft_delete_message(db, message_id: str, requester_id: str) -> dict:
        """Soft-delete: keep row for audit, blank content. Author only.

        Bumps `edited_at = CURRENT_TIMESTAMP` so the change becomes
        visible to /sync via the COALESCE(edited_at, created_at)
        cursor expression — without this, offline clients would never
        see that the message was deleted (reviewer-flagged C3).
        """
        from sqlalchemy import text
        row = db.execute(text(
            "SELECT author_id FROM messages WHERE id = :id"),
            {'id': message_id}
        ).fetchone()
        if row is None:
            raise ConversationError("message not found")
        if row[0] != requester_id:
            raise ConversationError("only author can delete")
        db.execute(text(
            "UPDATE messages SET is_deleted = 1, content = '[deleted]', "
            "       edited_at = CURRENT_TIMESTAMP "
            "WHERE id = :id"),
            {'id': message_id})
        db.commit()
        # Sweep out stale Mention rows so the mentions index doesn't
        # carry pointers into deleted content (reviewer N-NEW-2).  We
        # invoke the same diff path edit_message uses, with empty
        # content — _wipe is the optimization for that case.
        try:
            from .mention_service import MentionService
            MentionService.parse_and_record(
                db, source_kind='message', source_id=message_id,
                content='', author_id=requester_id)
        except Exception as e:
            logger.warning(
                "soft_delete_message: mention sweep failed: %s", e)
        return {'id': message_id, 'is_deleted': True}

    # ── Typing + read-receipt (Phase 7c.7) ───────────────────────

    @staticmethod
    def emit_typing(db, conv_id: str, user_id: str,
                    tenant_id: Optional[str] = None) -> dict:
        """Fire a WAMP typing event for the conversation.

        Pure broadcast — never persisted (typing is ephemeral state with
        a 5s TTL on the receive side).  Refuses if the user isn't a
        conversation member.  Returns {'sent': True} on success so the
        endpoint can confirm; receivers see it through the WAMP
        subscription.
        """
        if not _is_member(db, conv_id, user_id):
            raise ConversationError("not a conversation member")
        # Delegate to the existing realtime publisher — same module
        # that NotificationService uses, same MessageBus fan-out.
        try:
            from .realtime import publish_event
            topic = f'tenant.{tenant_id or "_"}.conv.{conv_id}.typing'
            publish_event(topic, {
                'conv_id': conv_id, 'user_id': user_id,
                'kind': 'typing'},
                user_id=user_id)
        except Exception as e:
            logger.warning("ConversationService.emit_typing: publish "
                           "failed: %s", e)
        return {'sent': True, 'conv_id': conv_id, 'user_id': user_id}

    @staticmethod
    def mark_read(db, conv_id: str, user_id: str,
                  message_id: Optional[str] = None,
                  tenant_id: Optional[str] = None) -> dict:
        """Persist + broadcast a read-receipt for the user in the conv.

        Stores last_read_message_id + last_read_at on the user's
        memberships row.  If message_id is omitted we use the
        conversation's most recent message (mark-all-read).

        Other members of the conversation get the receipt via a WAMP
        broadcast on tenant.{tid}.conv.{id}.read.

        Pass-1 M3 contract:
          Returns a dict with stable keys:
            - sent (bool): True iff a read-receipt was persisted +
              broadcast.  False is a NO-OP success (no error), used
              when there's nothing to mark (empty conversation).
            - conv_id (str): always echoed back.
            - last_read_message_id (str | None): the message id we
              marked, or None on no-op.
            - reason (str, optional): present iff sent=False, names
              the no-op cause for caller logging.
          Errors (not-a-member, message-not-in-conv) RAISE
          ConversationError; no-ops return the dict.  Two distinct
          shapes for two distinct concerns.
        """
        from sqlalchemy import text
        if not _is_member(db, conv_id, user_id):
            raise ConversationError("not a conversation member")

        # Default: pick the latest message if message_id not given.
        if not message_id:
            row = db.execute(text(
                "SELECT id FROM messages "
                "WHERE parent_kind='conversation' AND parent_id=:cid "
                "AND is_deleted = 0 "
                "ORDER BY created_at DESC LIMIT 1"),
                {'cid': conv_id}
            ).fetchone()
            if row is None:
                # Empty conversation — nothing to mark.  Return the
                # canonical no-op shape (Pass-1 M3 fix: include
                # conv_id and last_read_message_id keys so callers
                # don't have to special-case the response).
                return {
                    'sent': False,
                    'conv_id': conv_id,
                    'last_read_message_id': None,
                    'reason': 'empty conversation',
                }
            message_id = row[0]

        # Validate the message belongs to this conversation (avoid
        # cross-conv reference attacks).
        check = db.execute(text(
            "SELECT 1 FROM messages "
            "WHERE id = :mid AND parent_kind='conversation' "
            "AND parent_id = :cid LIMIT 1"),
            {'mid': message_id, 'cid': conv_id}
        ).fetchone()
        if check is None:
            raise ConversationError("message not in conversation")

        db.execute(text(
            "UPDATE memberships SET last_read_message_id = :mid, "
            "       last_read_at = CURRENT_TIMESTAMP "
            "WHERE parent_kind='conversation' AND parent_id=:cid "
            "AND member_id=:uid"),
            {'mid': message_id, 'cid': conv_id, 'uid': user_id})
        db.commit()

        try:
            from .realtime import publish_event
            topic = f'tenant.{tenant_id or "_"}.conv.{conv_id}.read'
            publish_event(topic, {
                'conv_id': conv_id, 'user_id': user_id,
                'last_read_message_id': message_id,
                'kind': 'read'},
                user_id=user_id)
        except Exception as e:
            logger.warning("ConversationService.mark_read: publish "
                           "failed: %s", e)

        return {'sent': True, 'conv_id': conv_id,
                'last_read_message_id': message_id}

    # ── Internal helpers ─────────────────────────────────────────

    @staticmethod
    def _notify_members(db, conv_id: str, author_id: str,
                        message_id: str, content: str) -> None:
        """Fire one notification per non-author member.  Each notif
        publishes through MessageBus (LOCAL → SSE → PEERLINK → CROSSBAR).
        """
        try:
            from .services import NotificationService
        except Exception:
            return
        for member_id in _list_member_ids(db, conv_id):
            if member_id == author_id:
                continue
            try:
                NotificationService.create(
                    db, user_id=member_id, type='message',
                    source_user_id=author_id,
                    target_type='conversation', target_id=conv_id,
                    message=(content or '')[:140])
            except Exception as e:
                logger.warning("ConversationService: notify failed: %s", e)
