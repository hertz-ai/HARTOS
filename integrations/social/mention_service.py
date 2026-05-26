"""
HevolveSocial — Mention service.

Phase 7b. Plan reference: sunny-gliding-eich.md, Part E.5 + Part L.

Transport: P2P-first via core/peer_link/message_bus.MessageBus.publish(...).
Falls back to WAMP push for offline recipients and HTTP API for sync/restore.
Central is the audit log + discovery index, not the primary router.

Existing fan-out order (do not bypass):
  LOCAL → SSE → PEERLINK → CROSSBAR

This module ONLY persists Mention rows + Notification rows + dispatches
agents through the existing agentic_router.  It does NOT directly emit
WAMP or open PeerLink frames — transport selection is owned by
NotificationService (which already publishes via MessageBus).

Agent topology (preserved exactly):
  When the mentioned user is an agent (User.user_type == 'agent'):
    1. Insert Mention row with mentioned_kind='agent' + agent_owner_id.
    2. Fire TWO notifications: one for the agent, one for the owning
       human. Both go through NotificationService.create which already
       publishes on the social.{user_id} WAMP topic AND fans out via
       MessageBus (LOCAL → SSE → PEERLINK → CROSSBAR).
    3. Dispatch through agentic_router.find_matching_agent so the
       reply is gated by the existing GuardrailEnforcer.before_dispatch
       and after_response (Constitutional Filter + Constructive Filter
       — security/hive_guardrails.py).  No new privileged path.
    4. The agent's reply is posted as a regular Comment via the
       existing CommentService.create — the same path a human reply
       takes.  This guarantees the reply gets DLP-redacted, classified
       (when moderation_v2 flag is on), and fanned out exactly like
       any other comment.

Privacy:
  Friend graph affects mention delivery — a user blocked by the
  mentioned target is silently dropped (Mention row is still recorded
  for audit, but no Notification fires).  Phase 7c's Block table
  is checked when present; pre-7c the check is a no-op.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

logger = logging.getLogger('hevolve_social')


# Mirrors the client-side regex in components/shared/MentionInput.js.
# Allows alphanumerics, underscore, dot, hyphen — same charset as
# username validation in UserService.register.
USERNAME_PATTERN = re.compile(r'(?<!\w)@([a-zA-Z0-9_.-]{2,40})')


def _existing_mentions(db, source_kind: str, source_id: str) -> dict:
    """Return {username_lower: Mention row} for an existing source.

    Reads via raw SQL because the Mention model isn't an ORM class
    today (table created by migration v42 which lands with this
    phase). When v42 isn't yet applied we silently return empty.
    """
    try:
        from sqlalchemy import text
        rows = db.execute(text(
            "SELECT m.id, m.mentioned_user_id, u.username "
            "FROM mentions m "
            "LEFT JOIN users u ON u.id = m.mentioned_user_id "
            "WHERE m.source_kind = :sk AND m.source_id = :sid"),
            {'sk': source_kind, 'sid': source_id}
        ).fetchall()
    except Exception:
        return {}
    out = {}
    for row in rows:
        if row[2]:
            out[row[2].lower()] = (row[0], row[1])
    return out


def _is_blocked_either_way(db, x: str, y: str) -> bool:
    """Phase 7c block check — bidirectional. Best-effort: returns
    False if the Block table doesn't exist yet (pre-migration v43).

    Suppresses notification if EITHER party blocks the other:
      - target blocked author → target doesn't want messages from author
      - author blocked target → author shouldn't be able to @-summon target
    """
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


class MentionService:
    """Parse @-mentions in user content and fire notifications + agent dispatch.

    All public methods are static — they take an explicit `db` session
    and `tenant_id` so they can be called from any service without
    coupling to Flask `g`.
    """

    @staticmethod
    def parse(content: str) -> List[str]:
        """Extract every @-mentioned username from content.

        Returns list of lowercased, deduped usernames in order of
        first occurrence.  Used by the autocomplete UI to highlight
        accepted refs and by parse_and_record to look up users.
        """
        if not content:
            return []
        seen = set()
        out = []
        for m in USERNAME_PATTERN.finditer(content):
            uname = m.group(1).lower()
            if uname not in seen:
                seen.add(uname)
                out.append(uname)
        return out

    @staticmethod
    def parse_and_record(db, source_kind: str, source_id: str,
                         content: str, author_id: str,
                         tenant_id: Optional[str] = None,
                         dispatch_agents: bool = True) -> List[dict]:
        """Parse content, insert Mention rows, fire Notifications,
        dispatch any mentioned agents through the existing
        agentic_router.

        Returns a list of mention dicts the caller can include in the
        post / comment / message response payload:
          [{user_id, username, kind: 'human'|'agent'}]

        Idempotent: re-running on the same source replaces the
        existing mention set (insert new, delete removed) — used by
        update_post / update_comment paths.
        """
        from .models import User
        from .services import NotificationService

        usernames = MentionService.parse(content)
        if not usernames:
            # Source was edited to remove all mentions — wipe any
            # existing Mention rows so they don't linger in the index.
            MentionService._wipe(db, source_kind, source_id)
            return []

        # Fetch users matching the parsed usernames (tenant-scoped if
        # the column is populated — flat/regional pass-through with
        # NULL tenant_id matches NULL rows).
        qry = db.query(User).filter(User.username.in_(usernames),
                                    User.is_banned == False)  # noqa: E712
        if hasattr(User, 'tenant_id') and tenant_id:
            qry = qry.filter(User.tenant_id == tenant_id)
        matched = {u.username.lower(): u for u in qry.all()}

        # Diff against existing mentions (idempotent edit support).
        existing = _existing_mentions(db, source_kind, source_id)

        to_remove = set(existing.keys()) - set(matched.keys())
        to_add = set(matched.keys()) - set(existing.keys())

        # Remove stale rows (silently — no notification on un-mention).
        # SQLAlchemy expanding bindparam works on every dialect.
        if to_remove:
            try:
                from sqlalchemy import text, bindparam
                ids = [existing[u][0] for u in to_remove]
                db.execute(
                    text("DELETE FROM mentions WHERE id IN :ids").bindparams(
                        bindparam('ids', expanding=True)),
                    {'ids': ids})
                db.commit()
            except Exception as e:
                logger.warning("MentionService: stale removal failed: %s", e)

        out = []
        # Insert new rows + notify.
        for uname in usernames:
            u = matched.get(uname)
            if not u:
                continue
            # Skip blocked targets — record-once-no-notify.
            # Bidirectional: either party blocking the other suppresses delivery.
            blocked = _is_blocked_either_way(db, u.id, author_id)

            if uname in to_add:
                MentionService._insert_row(
                    db, source_kind, source_id, u, author_id,
                    tenant_id, suppress_notify=blocked)

            kind = 'agent' if (getattr(u, 'user_type', '') == 'agent') else 'human'
            out.append({
                'user_id': u.id,
                'username': u.username,
                'kind': kind,
                'agent_owner_id': getattr(u, 'owner_id', None),
            })

            # Agent dispatch (existing HARTOS topology — see plan B.4).
            if dispatch_agents and kind == 'agent' and uname in to_add and not blocked:
                MentionService._dispatch_agent(
                    db, agent=u, source_kind=source_kind,
                    source_id=source_id, content=content,
                    author_id=author_id, tenant_id=tenant_id)

        return out

    @staticmethod
    def diff_and_update(db, source_kind: str, source_id: str,
                        old_content: str, new_content: str,
                        author_id: str, tenant_id: Optional[str] = None):
        """Convenience wrapper for edit paths.

        We re-run parse_and_record on the new content; it handles
        insert/delete diffing internally.  Old content is currently
        unused (kept in signature for future once-per-edit dispatch
        guarantees — Phase 7c may use it for re-notification policy).
        """
        return MentionService.parse_and_record(
            db, source_kind, source_id, new_content, author_id,
            tenant_id=tenant_id)

    # ─── Private helpers ────────────────────────────────────────

    @staticmethod
    def _insert_row(db, source_kind, source_id, mentioned_user,
                    author_id, tenant_id, suppress_notify=False):
        """Insert one Mention row + one or two Notification rows
        (two if mentioned is an agent — agent + owner)."""
        import uuid
        from sqlalchemy import text
        from .services import NotificationService

        mid = str(uuid.uuid4())
        kind = 'agent' if (getattr(mentioned_user, 'user_type', '') == 'agent') else 'human'
        owner_id = getattr(mentioned_user, 'owner_id', None) if kind == 'agent' else None

        try:
            db.execute(text(
                "INSERT INTO mentions "
                "(id, tenant_id, source_kind, source_id, "
                " mentioned_user_id, mentioned_kind, agent_owner_id, "
                " created_at) "
                "VALUES "
                "(:id, :tid, :sk, :sid, :muid, :mk, :aoi, "
                " CURRENT_TIMESTAMP)"),
                {'id': mid, 'tid': tenant_id, 'sk': source_kind,
                 'sid': source_id, 'muid': mentioned_user.id,
                 'mk': kind, 'aoi': owner_id}
            )
            db.commit()
        except Exception as e:
            logger.warning("MentionService: insert failed: %s", e)
            return

        if suppress_notify:
            return

        # Notify the mentioned user via existing NotificationService —
        # which already publishes on social.{user_id} WAMP topic + the
        # MessageBus fan-out (LOCAL → SSE → PEERLINK → CROSSBAR).
        try:
            NotificationService.create(
                db, user_id=mentioned_user.id, type='mention',
                source_user_id=author_id,
                target_type=source_kind, target_id=source_id,
                message=f"You were mentioned in a {source_kind}")
        except Exception as e:
            logger.warning("MentionService: notify mentioned failed: %s", e)

        # Dual-notify the owner when the mention is on an agent.
        if kind == 'agent' and owner_id:
            try:
                NotificationService.create(
                    db, user_id=owner_id, type='agent_mention',
                    source_user_id=author_id,
                    target_type=source_kind, target_id=source_id,
                    message=f"Your agent {mentioned_user.username} was mentioned")
            except Exception as e:
                logger.warning("MentionService: notify owner failed: %s", e)

    @staticmethod
    def _wipe(db, source_kind, source_id):
        """Delete every Mention row for a source — used when an edit
        removes all @-mentions."""
        try:
            from sqlalchemy import text
            db.execute(text(
                "DELETE FROM mentions "
                "WHERE source_kind = :sk AND source_id = :sid"),
                {'sk': source_kind, 'sid': source_id})
            db.commit()
        except Exception:
            pass

    @staticmethod
    def _load_thread_context(db, source_kind: str, source_id: str,
                             requester_id: str,
                             max_messages: int = 20) -> str:
        """Return the prior thread as a plain-text transcript the LLM
        can read before responding.  Uses ONLY existing tables
        (messages, posts, comments) — no new CRUD, no new schema.

        Returns '' on any error or when there is no prior context.
        Caller falls back to the mention-only prompt in that case.

        Format (chronological, oldest-first so the LLM reads top-down):
            [2026-05-23 19:30] @alice: original post content
            [2026-05-23 19:32] @bob: first reply
            [2026-05-23 19:35] @alice: second reply
            ...

        Read access: we DELIBERATELY bypass the `_is_member` check on
        the conversation read path because being @mentioned is itself
        the authorisation signal — the same author who sent the
        message granted the agent context by tagging it.  For posts /
        comments, those tables are public-by-default.
        """
        from sqlalchemy import text
        try:
            if source_kind == 'message':
                row = db.execute(text(
                    "SELECT parent_id FROM messages WHERE id = :sid"),
                    {'sid': source_id}).fetchone()
                if not row or not row[0]:
                    return ''
                conv_id = row[0]
                msgs = db.execute(text(
                    "SELECT created_at, author_id, content "
                    "FROM messages "
                    "WHERE parent_kind='conversation' AND parent_id=:pid "
                    "  AND is_deleted = 0 AND content != '[encrypted]' "
                    "ORDER BY created_at DESC LIMIT :lim"),
                    {'pid': conv_id, 'lim': max_messages}).fetchall()
                # Reverse to chronological so the LLM reads top-down.
                lines = [
                    f"[{ts}] @{aid}: {content[:500]}"
                    for ts, aid, content in reversed(msgs)
                ]
                return '\n'.join(lines)

            if source_kind in ('post', 'comment'):
                # Resolve to root post id.
                if source_kind == 'comment':
                    row = db.execute(text(
                        "SELECT parent_id FROM comments WHERE id = :sid"),
                        {'sid': source_id}).fetchone()
                    if not row or not row[0]:
                        return ''
                    post_id = row[0]
                else:
                    post_id = source_id

                post = db.execute(text(
                    "SELECT created_at, author_id, title, content "
                    "FROM posts WHERE id = :pid AND is_deleted = 0"),
                    {'pid': post_id}).fetchone()
                if not post:
                    return ''
                lines = [
                    f"[{post[0]}] @{post[1]} (post): "
                    f"{(post[2] + ' — ') if post[2] else ''}"
                    f"{(post[3] or '')[:500]}"
                ]
                comments = db.execute(text(
                    "SELECT created_at, author_id, content "
                    "FROM comments "
                    "WHERE parent_id=:pid AND is_deleted = 0 "
                    "ORDER BY created_at ASC LIMIT :lim"),
                    {'pid': post_id, 'lim': max_messages}).fetchall()
                for ts, aid, c in comments:
                    lines.append(f"[{ts}] @{aid}: {(c or '')[:500]}")
                return '\n'.join(lines)
        except Exception as e:
            logger.debug(
                "MentionService._load_thread_context: skipping (%s)", e)
        return ''

    @staticmethod
    def _dispatch_agent(db, agent, source_kind, source_id, content,
                        author_id, tenant_id):
        """Dispatch the mentioned agent through the existing
        agentic_router. The router calls into autogen / LangChain
        with GuardrailEnforcer wrapping every step (security/
        hive_guardrails.py — see plan Part B.4).

        We do NOT post the agent's reply ourselves — agentic_router
        returns a plan / response which the existing agent runtime
        publishes back via the same channels a human reply would
        use. This keeps the agent topology unchanged; we just
        deliver the prompt.

        If agentic_router is unavailable (HARTOS still booting,
        offline, or the import fails), we silently degrade: the
        Mention + Notification rows are still recorded so the agent
        can pick up the work asynchronously the next time it
        reconciles its inbox.
        """
        try:
            from integrations import agentic_router
        except Exception:
            logger.info("MentionService: agentic_router unavailable; "
                        "skipping agent dispatch for %s", agent.username)
            return

        # Load the full thread the agent is being asked to join so it
        # has real context, not just the mention snippet.  Uses ONLY
        # existing CRUDs (no new schema): the same Message / Post /
        # Comment rows the UI reads.  When the lookup fails (offline /
        # source row missing / agent has no read access) we fall back
        # silently to the mention-only prompt — the prior behaviour.
        thread_context = MentionService._load_thread_context(
            db, source_kind=source_kind, source_id=source_id,
            requester_id=str(agent.id), max_messages=20,
        )

        if thread_context:
            prompt = (
                f"You were mentioned in a {source_kind} (id={source_id}).\n\n"
                f"## Prior thread (read this before responding):\n"
                f"{thread_context}\n\n"
                f"## New message (the one that mentioned you):\n"
                f"{content}\n\n"
                "Read the full prior thread above before drafting your "
                "reply so you don't repeat what's already been said or "
                "miss context.  Reply if appropriate; otherwise stay silent."
            )
        else:
            prompt = (
                f"You were mentioned in a {source_kind} (id={source_id}). "
                f"The author wrote:\n\n{content}\n\n"
                "Reply if appropriate; otherwise stay silent."
            )

        try:
            # agentic_router.dispatch_to_agent is the canonical hook
            # (Phase 7b — added in this session). It runs the prompt
            # through GuardrailEnforcer.before_dispatch + after_response
            # and posts the reply via CommentService.create — same path
            # any human reply takes (plan B.4: no privileged path).
            # The dispatch is async (daemon thread) so this call
            # returns immediately; the calling Flask request is not
            # blocked on the LLM round-trip.
            if hasattr(agentic_router, 'dispatch_to_agent'):
                agentic_router.dispatch_to_agent(
                    agent_id=agent.id, prompt=prompt,
                    context={'source_kind': source_kind,
                             'source_id': source_id,
                             'author_id': author_id,
                             'tenant_id': tenant_id})
                return
            # Older agentic_router build without the hook — Mention +
            # Notification rows are persisted upstream so the agent
            # runtime can pick up asynchronously next tick.
            logger.info("MentionService: queued mention for agent %s "
                        "(no direct dispatcher) — runtime will pick up",
                        agent.username)
        except Exception as e:
            # Never let agent dispatch failure break the post create.
            logger.warning("MentionService: agent dispatch failed for %s: %s",
                           agent.username, e)
