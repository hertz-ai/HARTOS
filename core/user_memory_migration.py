"""Re-home a guest session's memory onto the account it logs into (#117).

Why this exists
---------------
Recall reads memory keyed by user_id: the date-range path queries
ConversationEntry.user_id, the semantic path reads simplemem_db/user_<id>/.
A guest browses under a random UUID (guest_user_id); when they later log in with
a cloud account, their notification user_id flips to the account id and recall
suddenly reads an empty buffer — the on-disk evidence was ~6 fragmented buffers
for one human.  This module moves the guest memory onto the account so recall is
continuous across the login boundary.

Safety
------
Re-keying ``from -> to`` is a memory-THEFT vector if ``from`` is arbitrary: a
caller could absorb another user's chat history by naming their id.  So the
*mechanism* here is deliberately dumb (re-key + merge), and **eligibility is the
caller's job**: callers MUST pass only a ``from_user_id`` the requester is
entitled to claim.  ``is_claimable_guest(from_user_id)`` is the provided gate —
an id is claimable only when NO cloud account row owns it (a never-registered
anonymous guest).  Guest UUIDs are unguessable (crypto.randomUUID), so in
practice a requester only knows their own.  The /link-device wire applies this
gate before calling migrate_user_memory.

Non-destructive + idempotent: ConversationEntry rows are RE-KEYED (not deleted),
the SimpleMem rolling buffer is appended via the canonical API, and the source
buffer is cleared so a re-run is a no-op.  Best-effort: a failure in one half
never aborts the other or raises to the caller.
"""
from __future__ import annotations

import logging
from typing import Dict

logger = logging.getLogger('user_memory_migration')


def is_claimable_guest(from_user_id) -> bool:
    """True iff ``from_user_id`` is an anonymous guest no cloud account owns —
    i.e. safe for a logging-in user to absorb.  A registered account (a User row
    carrying an email) is NOT claimable.  Fails CLOSED (returns False) on any
    error, so an unknown state never authorises a migration."""
    if not from_user_id:
        return False
    try:
        from integrations.social.models import db_session, User
        with db_session(commit=False) as db:
            u = db.query(User).filter(User.id == str(from_user_id)).first()
            if u is None:
                return True  # no account row at all -> pure anonymous guest
            # A row with a real credential (email) is an account, not a guest.
            return not (getattr(u, 'email', None) or '').strip()
    except Exception as e:
        logger.warning("is_claimable_guest(%s) failed (deny): %s", from_user_id, e)
        return False


def migrate_user_memory(from_user_id, to_user_id) -> Dict[str, int]:
    """Move ``from_user_id``'s memory onto ``to_user_id`` (#117).

    CALLER MUST have authorised the claim (see is_claimable_guest) — this does
    NOT re-check eligibility, it only performs the move.

    Returns a summary {'conversation_entries': N, 'buffer_messages': M}.
    """
    summary = {'conversation_entries': 0, 'buffer_messages': 0}
    if not from_user_id or not to_user_id:
        return summary
    if str(from_user_id) == str(to_user_id):
        return summary

    # 1. Re-key the authoritative chat log (the full history the date-recall
    #    path reads via ConversationEntry).  A plain UPDATE — non-destructive,
    #    and idempotent (a re-run finds no rows still keyed to `from`).
    try:
        from integrations.social._models_local import ConversationEntry
        from integrations.social.models import get_db
        db = get_db()
        try:
            n = db.query(ConversationEntry).filter(
                ConversationEntry.user_id == str(from_user_id)
            ).update({ConversationEntry.user_id: str(to_user_id)},
                     synchronize_session=False)
            db.commit()
            summary['conversation_entries'] = int(n or 0)
        finally:
            db.close()
    except Exception as e:
        logger.warning("migrate_user_memory: ConversationEntry re-key %s->%s failed: %s",
                       from_user_id, to_user_id, e)

    # 2. Merge the SimpleMem rolling buffer (the recent semantic window).  Use
    #    the canonical API (no buffer.json format assumptions); clear the source
    #    so a re-run can't double-add.
    try:
        from integrations.channels.memory.simplemem_langchain import SimpleMemChatMemory
        from_mem = SimpleMemChatMemory.load_or_create(from_user_id)
        msgs = list(getattr(from_mem.chat_memory, 'messages', []) or [])
        if msgs:
            to_mem = SimpleMemChatMemory.load_or_create(to_user_id)
            to_mem.chat_memory.add_messages(msgs)
            summary['buffer_messages'] = len(msgs)
            try:
                from_mem.chat_memory.clear()
            except Exception:
                pass  # merged into `to`; failing to clear `from` is non-fatal
    except Exception as e:
        logger.warning("migrate_user_memory: SimpleMem merge %s->%s failed: %s",
                       from_user_id, to_user_id, e)

    if summary['conversation_entries'] or summary['buffer_messages']:
        logger.info("Migrated guest memory %s -> %s: %s",
                    from_user_id, to_user_id, summary)
    return summary
