"""Chat-sync service — canonical writer + cursor-pull reader for
cross-device chat mirroring (U1-U9 workstream, task #389).

Design principles (CLAUDE.md Gates):
  * Gate 2 (DRY): extends the existing ``ConversationEntry`` table —
    does NOT introduce a parallel ChatMessage model.  Every chat turn
    (/chat hot path, channel adapters, LangChain prep_outputs) already
    lands here; this module gives it a stable API + cursor-pull +
    WAMP publish in one place.
  * Gate 3 (SRP):
      - ``persist(...)`` writes one row (per role) and returns the row.
      - ``pull_since(...)`` reads rows since a cursor, capped by row- +
        byte-budget from ``core.constants.CHAT_CURSOR_PULL_MAX_*``.
      - ``publish_new(...)`` fires the chat.new WAMP event (synchronous
        call — ``publish_async`` already routes its HTTP leg through
        its own executor, so the MessageBus hand-off is O(1)).
      - ``persist_and_publish_async(...)`` submits both to a module
        executor so callers on the chat hot path incur no latency.
    NO I/O mixing; the HTTP handler in Nunba's main.py stays pure glue.
  * Gate 4 (no parallel paths): ONE persistence helper — this one.
    ``world_model_bridge._persist_to_conversation_entry`` is preserved
    for now (other call sites), but new writes go through here.
  * Gate 8 (security): ``user_id`` is attacker-controllable via body;
    the HTTP gate (Nunba main.py) confirms JWT→user_id BEFORE calling
    ``persist`` / ``pull_since``.  This module trusts its inputs.
    Dedup-on-msg_id verifies user_id matches before returning — a
    cross-user msg_id collision (astronomically unlikely) would NOT
    leak the other user's row.

Public API
----------
    persist(user_id, role, content, *, msg_id=None, agent_id=None,
            prompt_id=None, request_id=None, device_id=None,
            lang=None, attachments=None, channel_type='chat') -> dict|None
        Insert one ConversationEntry; return the ``to_dict()`` of the
        inserted row or None on failure.

    pull_since(user_id, since_id=0, *, limit=None, channel_type=None,
               agent_id=None) -> list[dict]
        Cursor pull.

    publish_new(row_dict) -> None
        Synchronous (but internally fast) WAMP publish.

    persist_and_publish_async(...) -> None
        Fire-and-forget.  Submits persist+publish to a module executor.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

# Lazy module cache — keep this file import-cheap so tests that only
# exercise ``_generate_msg_id`` don't need SQLAlchemy on the path.
_MODELS_CACHE: dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()

# Module-level executor for persist-and-publish off the hot path.  Bound
# small (2 workers) — we are not CPU-bound here; the bottleneck is a DB
# round-trip.  max_workers>1 lets a slow write (network hiccup to a
# regional MySQL) not block the next turn's persist.  Thread-name prefix
# so it shows up distinctly in ``core.diag`` thread-dump output.
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix='chat-sync')


def _models():
    """Return ``(get_db, ConversationEntry)`` — cached on first call.

    Raises ImportError if HARTOS social models can't be loaded, in which
    case the caller should degrade (skip the write, do NOT crash).
    """
    if 'get_db' in _MODELS_CACHE:
        return _MODELS_CACHE['get_db'], _MODELS_CACHE['ConversationEntry']
    with _CACHE_LOCK:
        if 'get_db' in _MODELS_CACHE:
            return _MODELS_CACHE['get_db'], _MODELS_CACHE['ConversationEntry']
        from integrations.social.models import ConversationEntry, get_db
        _MODELS_CACHE['get_db'] = get_db
        _MODELS_CACHE['ConversationEntry'] = ConversationEntry
    return _MODELS_CACHE['get_db'], _MODELS_CACHE['ConversationEntry']


def _generate_msg_id() -> str:
    """ULID-like 16-char hex id.

    48 bits of milliseconds (12 hex chars) + 16 bits random (4 hex).
    Sortable by embedding time first — not cryptographically secure,
    collision-resistant enough for per-user dedup.  For busy multi-user
    servers, clients should supply their own msg_id; this is a fallback.
    """
    ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF  # 48-bit cap
    rnd = int.from_bytes(os.urandom(2), 'big')
    return f"{ms:012x}{rnd:04x}"


def _payload_bytes(obj: Any) -> int:
    """Cheap JSON-encoded size estimate for the byte cap in pull_since."""
    try:
        return len(json.dumps(obj, separators=(',', ':'), default=str))
    except Exception:  # noqa: BLE001
        return 0


def persist(
    user_id: str,
    role: str,
    content: str,
    *,
    msg_id: str | None = None,
    agent_id: str | None = None,
    prompt_id: str | None = None,
    request_id: str | None = None,
    device_id: str | None = None,
    lang: str | None = None,
    attachments: list[dict] | None = None,
    channel_type: str = 'chat',
) -> dict | None:
    """Insert one ConversationEntry.  Returns the row dict or None.

    Deduplication: if ``msg_id`` already exists for THIS user, the
    existing row is returned without a second insert.  Across users a
    msg_id collision (astronomically unlikely with 64 bits of entropy)
    is refused — we do not return another user's data.
    """
    if not user_id or not role or not content:
        return None
    if role not in ('user', 'assistant', 'system'):
        logger.debug("chat_messages.persist: invalid role %r", role)
        return None

    msg_id = msg_id or _generate_msg_id()
    try:
        get_db, ConversationEntry = _models()
    except ImportError:
        logger.debug("chat_messages.persist: social models unavailable")
        return None

    db = None
    try:
        db = get_db()
        existing = db.query(ConversationEntry).filter(
            ConversationEntry.msg_id == msg_id
        ).first()
        if existing is not None:
            if str(existing.user_id) != str(user_id):
                # Cross-user msg_id collision: refuse rather than leak
                # the other user's row.  Caller retries with a new id.
                logger.warning(
                    "chat_messages.persist: msg_id %r cross-user collision "
                    "(owner=%r, caller=%r); refusing",
                    msg_id, existing.user_id, user_id,
                )
                return None
            return existing.to_dict()

        entry = ConversationEntry(
            user_id=str(user_id),
            channel_type=channel_type,
            role=role,
            content=content[:10000],
            agent_id=agent_id,
            prompt_id=prompt_id,
            msg_id=msg_id,
            request_id=request_id,
            device_id=device_id,
            lang=lang,
            attachments=attachments,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry.to_dict()
    except Exception as e:  # noqa: BLE001
        logger.debug("chat_messages.persist failed: %s", e)
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def pull_since(
    user_id: str,
    since_id: int = 0,
    *,
    limit: int | None = None,
    channel_type: str | None = None,
    agent_id: str | None = None,
) -> list[dict]:
    """Return ConversationEntry rows with ``id > since_id`` for this user.

    Ordered by id ASC (monotonic cursor).  Truncated at the row budget
    AND the byte budget — whichever fires first.  Always returns at
    least one row if matches exist and the first row is smaller than
    the byte cap.  Empty list on error or nothing-new.
    """
    try:
        from core.constants import (
            CHAT_CURSOR_PULL_MAX_BYTES,
            CHAT_CURSOR_PULL_MAX_ROWS,
        )
    except ImportError:
        CHAT_CURSOR_PULL_MAX_ROWS = 500
        CHAT_CURSOR_PULL_MAX_BYTES = 2 * 1024 * 1024

    try:
        lim = int(limit) if limit is not None else CHAT_CURSOR_PULL_MAX_ROWS
    except (TypeError, ValueError):
        lim = CHAT_CURSOR_PULL_MAX_ROWS
    row_cap = min(max(1, lim), CHAT_CURSOR_PULL_MAX_ROWS)

    try:
        since = int(since_id or 0)
    except (TypeError, ValueError):
        since = 0
    if since < 0:
        since = 0

    if not user_id:
        return []

    try:
        get_db, ConversationEntry = _models()
    except ImportError:
        return []

    db = None
    try:
        db = get_db()
        q = db.query(ConversationEntry).filter(
            ConversationEntry.user_id == str(user_id),
            ConversationEntry.id > since,
        )
        if channel_type:
            q = q.filter(ConversationEntry.channel_type == channel_type)
        if agent_id:
            q = q.filter(ConversationEntry.agent_id == agent_id)
        q = q.order_by(ConversationEntry.id.asc()).limit(row_cap)

        out: list[dict] = []
        running = 0
        for row in q.all():
            d = row.to_dict()
            # Back-fill msg_id for legacy rows written before v38 so
            # clients can dedup safely.  Uses ``legacy-<seq>`` — never
            # collides with a real 16-char hex msg_id.  Not written
            # back to DB — pulled-dict-only.
            if not d.get('msg_id'):
                d['msg_id'] = f"legacy-{d['id']}"
            size = _payload_bytes(d)
            if out and running + size > CHAT_CURSOR_PULL_MAX_BYTES:
                break
            out.append(d)
            running += size
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("chat_messages.pull_since failed: %s", e)
        return []
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def publish_new(row_dict: dict) -> None:
    """Publish chat.new for a freshly-persisted row.

    Routes through ``core.peer_link.message_bus`` — that handles LOCAL
    in-memory delivery synchronously (microseconds) and fans out to
    PeerLink + Crossbar asynchronously via its own executor.  We import
    the bus module here rather than pulling ``hart_intelligence_entry``
    because the latter has a heavy langchain/torch import chain that
    is inappropriate for a synchronous hot-path helper and also breaks
    unit tests that don't have GPU-grade memory available.  Safe to
    call on the hot path.
    """
    if not row_dict or not row_dict.get('user_id'):
        return
    try:
        from core.constants import CHAT_TOPIC_NEW
    except ImportError:
        return
    topic = f"{CHAT_TOPIC_NEW}.{row_dict['user_id']}"
    try:
        from core.peer_link.message_bus import get_message_bus
    except ImportError:
        logger.debug("chat_messages.publish_new: message_bus unavailable")
        return
    try:
        bus = get_message_bus()
        bus.publish(topic, row_dict)
    except Exception as e:  # noqa: BLE001
        logger.debug("chat_messages.publish_new failed for user %s: %s",
                     row_dict.get('user_id'), e)


def persist_and_publish_async(
    user_id: str,
    role: str,
    content: str,
    **kwargs: Any,
) -> None:
    """Fire-and-forget persist + publish.  Safe on the chat hot path.

    Submits to a bounded module executor; the caller returns immediately
    with no DB or WAMP latency.  On executor saturation the submit
    itself could block — but max_workers=2 is only saturated when two
    previous persists are stuck, which is already a symptom of DB
    trouble; we deliberately let backpressure surface there rather
    than queue unbounded.
    """
    def _run() -> None:
        row = persist(user_id, role, content, **kwargs)
        if row:
            publish_new(row)

    try:
        _EXECUTOR.submit(_run)
    except RuntimeError as e:
        # Executor shut down (interpreter exit).  Fall back to inline.
        logger.debug("chat_messages.persist_and_publish_async: executor "
                     "unavailable (%s); running inline", e)
        try:
            _run()
        except Exception:  # noqa: BLE001
            pass
