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

    def _get_last_hash(self) -> str:
        """Get the hash of the last entry in the chain."""
        if self._use_db:
            try:
                from integrations.social.models import get_db, AuditLogEntry
                db = get_db()
                try:
                    last = db.query(AuditLogEntry).order_by(
                        AuditLogEntry.id.desc()
                    ).first()
                    return last.entry_hash if last else 'genesis'
                finally:
                    db.close()
            except Exception:
                pass

        # Fallback: in-memory
        if self._memory_log:
            return self._memory_log[-1]['entry_hash']
        return 'genesis'

    def log_event(self, event_type: str, actor_id: str, action: str,
                  detail: Optional[Dict] = None,
                  target_id: Optional[str] = None) -> Tuple[int, str]:
        """
        Append an immutable event to the audit log.

        Args:
            event_type: Category (state_change, goal_dispatched, tool_call, auth, security)
            actor_id: Who triggered the event (user_id, agent_id, system)
            action: What happened (free text, e.g. 'completed action 5')
            detail: Optional structured data (sensitive keys auto-redacted)
            target_id: Optional target entity ID

        Returns:
            (entry_id, entry_hash)
        """
        with self._lock:
            timestamp = datetime.utcnow().isoformat()
            detail_json = _redact_sensitive(detail)
            prev_hash = self._get_last_hash()
            entry_hash = _compute_hash(
                prev_hash, event_type, actor_id, action, timestamp, detail_json)

            if self._use_db:
                try:
                    from integrations.social.models import get_db, AuditLogEntry
                    db = get_db()
                    try:
                        entry = AuditLogEntry(
                            event_type=event_type,
                            actor_id=actor_id,
                            target_id=target_id,
                            action=action,
                            detail_json=detail_json,
                            prev_hash=prev_hash,
                            entry_hash=entry_hash,
                        )
                        db.add(entry)
                        db.commit()
                        entry_id = entry.id
                        logger.debug(f"Audit log: {event_type} by {actor_id}: {action}")
                        return entry_id, entry_hash
                    except Exception:
                        db.rollback()
                        raise
                    finally:
                        db.close()
                except Exception as e:
                    logger.warning(f"DB audit log failed, using memory: {e}")

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
