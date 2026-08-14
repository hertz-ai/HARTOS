"""
Immutable Audit Log — Tamper-Evident Event Chain

Every security-relevant event (state changes, goal dispatches, tool calls,
auth events) is recorded with a hash chain. Each entry's hash depends on
the previous entry's hash, forming a tamper-evident chain.

If any entry is modified or deleted, verify_chain() detects the break.

Usage:
    from security.immutable_audit_log import get_audit_log
    audit = get_audit_log()
    entry_id, entry_hash = audit.log_event('state_change', actor_id='user_1', action='completed task 5')
    ok, reason = audit.verify_chain()
"""

import hashlib
import json
import logging
import threading
import time
from datetime import datetime
from typing import Optional, List, Tuple, Dict, Any

logger = logging.getLogger('hevolve_security')

# Sensitive keys that should be redacted in detail_json
_SENSITIVE_KEYS = frozenset({
    'password', 'token', 'api_key', 'secret', 'credential',
    'private_key', 'ssn', 'credit_card', 'card_number',
})


def _redact_sensitive(detail: Optional[Dict]) -> Optional[str]:
    """Redact sensitive fields before storing in audit log."""
    if detail is None:
        return None
    safe = {}
    for k, v in detail.items():
        if any(s in k.lower() for s in _SENSITIVE_KEYS):
            safe[k] = '[REDACTED]'
        else:
            safe[k] = v
    return json.dumps(safe, sort_keys=True, default=str)


def _compute_hash(prev_hash: str, event_type: str, actor_id: str,
                  action: str, timestamp: str, detail_json: Optional[str]) -> str:
    """Compute SHA-256 hash of entry fields chained to previous hash."""
    payload = f"{prev_hash}|{event_type}|{actor_id}|{action}|{timestamp}|{detail_json or ''}"
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


class ImmutableAuditLog:
    """
    Append-only audit log with hash-chain integrity.

    Storage: SQLAlchemy AuditLogEntry table (see integrations/social/models.py).
    Falls back to in-memory list when DB is unavailable (test/standalone mode).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._memory_log: List[Dict] = []  # Fallback for no-DB mode
        self._use_db = self._check_db_available()

    def _check_db_available(self) -> bool:
        try:
            from integrations.social.models import AuditLogEntry  # noqa: F401
            return True
        except ImportError:
            return False

    def _get_last_hash(self, db=None) -> str:
        """Get the hash of the last entry in the chain.

        ``db``: reuse the CALLER'S session instead of opening one. This is not
        an optimization — see record(): a second session over a shared-pool
        connection (sqlite:// StaticPool) resets that connection on checkout,
        ROLLING BACK the caller's uncommitted transaction.
        """
        if self._use_db:
            try:
                from integrations.social.models import get_db, AuditLogEntry
                session = db if db is not None else get_db()
                try:
                    last = session.query(AuditLogEntry).order_by(
                        AuditLogEntry.id.desc()
                    ).first()
                    return last.entry_hash if last else 'genesis'
                finally:
                    if db is None:
                        session.close()
            except Exception:
                pass

        # Fallback: in-memory
        if self._memory_log:
            return self._memory_log[-1]['entry_hash']
        return 'genesis'

    def log_event(self, event_type: str, actor_id: str, action: str,
                  detail: Optional[Dict] = None,
                  target_id: Optional[str] = None,
                  db=None) -> Tuple[int, str]:
        """
        Append an immutable event to the audit log.

        Args:
            event_type: Category (state_change, goal_dispatched, tool_call, auth, security)
            actor_id: Who triggered the event (user_id, agent_id, system)
            action: What happened (free text, e.g. 'completed action 5')
            detail: Optional structured data (sensitive keys auto-redacted)
            target_id: Optional target entity ID
            db: A caller MID-TRANSACTION on its own session MUST pass it.
                The entry is added to that session WITHOUT commit, so it
                persists atomically with the work it describes. Two reasons,
                one per environment:
                  * shared-connection pools (sqlite:// StaticPool — every CI /
                    in-memory test env): opening a second session here resets
                    the shared connection on checkout, which ROLLS BACK the
                    caller's uncommitted writes. Measured: a settlement's
                    wallet + transaction inserts were destroyed mid-flight
                    while this log's own commit survived, and the settlement
                    still reported success.
                  * real databases: a separate session commits the entry even
                    if the caller's transaction later rolls back — the
                    immutable trail then attests to work that never happened.

        Returns:
            (entry_id, entry_hash)
        """
        with self._lock:
            # #48 fix: hash with the SAME timestamp we persist as created_at.
            # The model's created_at default (datetime.utcnow) fires AGAIN at
            # flush when we don't pass it, yielding a different value than the one
            # mixed into entry_hash → verify_chain recomputed with the DB's
            # created_at and saw a mismatch on EVERY DB-backed round-trip.
            # Capture `now` once, hash it, and store it explicitly.
            now = datetime.utcnow()
            timestamp = now.isoformat()
            detail_json = _redact_sensitive(detail)
            prev_hash = self._get_last_hash(db=db)
            entry_hash = _compute_hash(
                prev_hash, event_type, actor_id, action, timestamp, detail_json)

            if self._use_db:
                # RETRY A TRANSIENT WRITER LOCK BEFORE GIVING UP (real HW,
                # 2026-08-12). The shared engine sets busy_timeout=3000 on purpose
                # (models.py: fail fast rather than block daemon threads 15-30s and
                # trip watchdog restarts) — that is right for ordinary queries, but
                # it means a concurrent writer can bounce an AUDIT write, and the
                # fallback below then keeps the entry only IN MEMORY. Live evidence:
                #   WARNING:hevolve_security:DB audit log failed, using memory:
                #   (sqlite3.OperationalError) database is locked
                # Those entries are gone at process exit, and every one of them is a
                # gap in a HASH-CHAINED log — verify_chain cannot distinguish "never
                # happened" from "we dropped it", so the tamper-evidence is what
                # actually degrades. Losing security records to a lock that clears in
                # milliseconds is not an acceptable trade.
                #
                # So retry the whole unit of work with a short backoff (~0.25s,
                # 0.5s), which is far below the watchdog budget the 3s timeout was
                # protecting, and keeps the fast-fail behaviour of the shared engine
                # untouched — only this writer is more patient.
                #
                # ONLY when we own the session. A BORROWED session (db is not None)
                # belongs to the caller's transaction: after a failure it is in an
                # unusable state and rolling it back or re-flushing would corrupt
                # THEIR unit of work, so a caller-supplied session still gets exactly
                # one attempt and the existing behaviour.
                _own_session = db is None
                _attempts = 3 if _own_session else 1
                for _attempt in range(_attempts):
                    try:
                        from integrations.social.models import get_db, AuditLogEntry
                        caller_session = db is not None
                        session = db if caller_session else get_db()
                        try:
                            entry = AuditLogEntry(
                                event_type=event_type,
                                actor_id=actor_id,
                                target_id=target_id,
                                action=action,
                                detail_json=detail_json,
                                prev_hash=prev_hash,
                                entry_hash=entry_hash,
                                created_at=now,  # #48: persist the hashed timestamp
                            )
                            session.add(entry)
                            if caller_session:
                                # NEVER commit/rollback/close a borrowed session —
                                # the entry rides the caller's transaction. Flush
                                # only, to obtain the id.
                                session.flush()
                            else:
                                session.commit()
                            entry_id = entry.id
                            logger.debug(f"Audit log: {event_type} by {actor_id}: {action}")
                            return entry_id, entry_hash
                        except Exception:
                            if not caller_session:
                                session.rollback()
                            raise
                        finally:
                            if not caller_session:
                                session.close()
                    except Exception as e:
                        _last = (_attempt == _attempts - 1)
                        if not _last and 'database is locked' in str(e).lower():
                            _delay = 0.25 * (2 ** _attempt)
                            logger.debug(
                                "Audit log: SQLite busy, retry %d/%d in %.2fs",
                                _attempt + 1, _attempts - 1, _delay,
                            )
                            time.sleep(_delay)
                            continue
                        # Out of retries, or an error retrying cannot help. Still
                        # LOUD, and now it names how hard we tried so the warning
                        # cannot be mistaken for a first-and-only attempt.
                        logger.warning(
                            "DB audit log failed after %d attempt(s), using memory: %s",
                            _attempt + 1, e,
                        )
                        break

            # Fallback: in-memory
            entry_id = len(self._memory_log) + 1
            self._memory_log.append({
                'id': entry_id,
                'event_type': event_type,
                'actor_id': actor_id,
                'target_id': target_id,
                'action': action,
                'detail_json': detail_json,
                'prev_hash': prev_hash,
                'entry_hash': entry_hash,
                'created_at': timestamp,
            })
            return entry_id, entry_hash

    def verify_chain(self, limit: int = 1000) -> Tuple[bool, str]:
        """
        Verify the integrity of the audit log hash chain.

        Returns:
            (is_valid, reason)
        """
        entries = self._get_entries(limit=limit)
        if not entries:
            return True, 'Empty log'

        prev_hash = 'genesis'
        for entry in entries:
            expected = _compute_hash(
                prev_hash,
                entry['event_type'],
                entry['actor_id'],
                entry['action'],
                entry['created_at'],
                entry.get('detail_json'),
            )
            if entry['entry_hash'] != expected:
                return False, (
                    f"Chain broken at entry {entry['id']}: "
                    f"expected {expected[:16]}..., got {entry['entry_hash'][:16]}..."
                )
            prev_hash = entry['entry_hash']

        return True, f'Chain valid ({len(entries)} entries)'

    def get_trail(self, actor_id: Optional[str] = None,
                  event_type: Optional[str] = None,
                  limit: int = 100) -> List[Dict]:
        """Get audit trail, optionally filtered by actor or event type."""
        entries = self._get_entries(limit=limit * 5)  # Over-fetch for filtering
        if actor_id:
            entries = [e for e in entries if e['actor_id'] == actor_id]
        if event_type:
            entries = [e for e in entries if e['event_type'] == event_type]
        return entries[:limit]

    def _get_entries(self, limit: int = 1000) -> List[Dict]:
        """Get raw entries from DB or memory."""
        if self._use_db:
            try:
                from integrations.social.models import get_db, AuditLogEntry
                db = get_db()
                try:
                    rows = db.query(AuditLogEntry).order_by(
                        AuditLogEntry.id.asc()
                    ).limit(limit).all()
                    return [{
                        'id': r.id,
                        'event_type': r.event_type,
                        'actor_id': r.actor_id,
                        'target_id': r.target_id,
                        'action': r.action,
                        'detail_json': r.detail_json,
                        'prev_hash': r.prev_hash,
                        'entry_hash': r.entry_hash,
                        'created_at': r.created_at.isoformat() if hasattr(r.created_at, 'isoformat') else r.created_at,
                    } for r in rows]
                finally:
                    db.close()
            except Exception:
                pass

        return list(self._memory_log[:limit])


# Singleton
_audit_log = None


def get_audit_log() -> ImmutableAuditLog:
    global _audit_log
    if _audit_log is None:
        _audit_log = ImmutableAuditLog()
    return _audit_log
