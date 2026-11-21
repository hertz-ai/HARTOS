"""
Immutable Audit Log — Tamper-Evident Event Chain

Every security-relevant event (goal dispatches, steering, tool calls, auth
events, money settlements) is recorded with a hash chain. Each entry's hash
depends on the previous entry's hash, forming a tamper-evident chain.

If any entry is modified or deleted, verify_chain() detects the break.

One view, two stores (owner, 2026-11-21): this log keeps the events that
must commit with a database transaction (log_event(db=)); every agent
modality, a step's state change included, is on the audit trail
(security.audit_trail).  The agent page reads both as one trail.

Usage:
    from security.immutable_audit_log import get_audit_log
    audit = get_audit_log()
    entry_id, entry_hash = audit.log_event('goal_dispatched', actor_id='user_1', action='dispatch goal 5')
    ok, reason = audit.verify_chain()
"""

import hashlib
import json
import logging
import re
import threading
import time
from datetime import date, datetime, time as dt_time, timezone
from typing import Optional, List, Tuple, Dict, Any

logger = logging.getLogger('hevolve_security')

# Besides a secret's name (security.edge_privacy.is_secret_key, the one
# rule), the audit log withholds a key whose letters, run together, hold one
# of these anywhere ('auth_tokens', 'token_value', 'client_secrets',
# 'apiKeys', 'apikeys', 'private-key-pem', 'secret_answer', 'credential_id',
# 'creditcards', 'card_numbers'): the one rule reads a secret word only as
# the whole name or its last word, and this log is hash-chained, so a value
# kept raw is kept for good.  Withholding a harmless 'max_tokens' or
# 'apiKeyName' here is the price (reviews of 2f3d2255e, fbae5ea36 and
# d2a2f2ad4).
_SENSITIVE_KEY_PARTS = ('password', 'token', 'apikey', 'secret', 'credential',
                        'privatekey', 'creditcard', 'cardnumber')
# A government number is a word of the key that starts or ends with one of
# these ('ssn', 'ssn_last4', 'ssns', 'SSNumber', 'userssn', 'mySsnValue'),
# never the letters anywhere in it: matched anywhere, 'classname',
# 'processName', 'businessName' and 'witnessNode' were withheld (review of
# fbae5ea36); matched only as a whole word, 'ssns' and 'userssn' were kept
# raw (review of d2a2f2ad4).
_SENSITIVE_NUMBER_WORDS = ('ssn',)


def _redact_sensitive(detail: Optional[Dict]) -> Optional[str]:
    """Redact sensitive fields before storing in audit log.

    A field is sensitive by the one secret-name rule (which also reads
    'passwd', 'pwd', 'cookie' and 'API-Key'), when its key's letters hold
    one of _SENSITIVE_KEY_PARTS, or when a word of its key starts or ends
    with one of _SENSITIVE_NUMBER_WORDS.  The words are read both as
    written ('ssns', 'userssn') and in secret_key_form ('my_ssn_value')."""
    if detail is None:
        return None
    from security.edge_privacy import is_secret_key, secret_key_form
    safe = {}
    for k, v in detail.items():
        raw, form = str(k).lower(), secret_key_form(str(k))
        letters = re.sub('[^a-z]+', '', raw)
        words = set(form.split('_')) | set(re.split('[^a-z]+', raw))
        if (is_secret_key(form)
                or any(p in letters for p in _SENSITIVE_KEY_PARTS)
                or any(w.startswith(n) or w.endswith(n)
                       for w in words for n in _SENSITIVE_NUMBER_WORDS)):
            safe[k] = '[REDACTED]'
        else:
            safe[k] = v
    return json.dumps(safe, sort_keys=True, default=str)


def _compute_hash(prev_hash: str, event_type: str, actor_id: str,
                  action: str, timestamp: str, detail_json: Optional[str]) -> str:
    """Compute SHA-256 hash of entry fields chained to previous hash."""
    payload = f"{prev_hash}|{event_type}|{actor_id}|{action}|{timestamp}|{detail_json or ''}"
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def utc_naive(value, *, day_end: bool = False) -> Optional[datetime]:
    """``value`` (an ISO date or time, or a datetime) as the naive UTC
    datetime created_at holds; None or '' is None.  The ONE window rule of
    the agent page, for both stores (this log and security.audit_trail, read
    by integrations.social.api_audit too): a time with a zone is read as the
    moment it names, one without as UTC; a date alone, in any form
    date.fromisoformat reads (2026-11-21, and 20261121 on Python 3.11+), is
    that UTC day's start, or with ``day_end`` (an until) its last moment, so
    until=2026-11-21 keeps the whole of that day.  Anything else raises
    ValueError saying what to send, so a bad window is refused, never read
    as no window."""
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        try:
            day = date.fromisoformat(text)
        except ValueError:
            day = None
        if day is not None:
            moment = datetime.combine(day, dt_time.max if day_end else dt_time.min)
        else:
            try:
                moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
            except ValueError:
                raise ValueError(
                    'since and until take an ISO date or time, such as 2026-11-21 or '
                    f'2026-11-21T08:00:00+00:00; {text[:40]!r} is neither') from None
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


#: Which field of an entry holds the agent it concerns, by event type: the
#: rule the agent page reads an agent's entries by (get_trail(related_to=)).
#: Read from the writers: target_id is the goal or agent for
#: goal_dispatched (agent_engine.dispatch), agent_steered
#: (social.dashboard_service), a2ui_push and a2ui_register_component
#: (agent_engine.liquid_ui_service); actor_id is the agent for wm_window_op
#: (agent_engine.hart_wm_client), whose target_id is a window's con_id, a
#: small number.  No other writer names an agent in either field: their
#: actor is a component, a person, a node, a module or a step number
#: (core.platform.bootstrap's action_retry_exhausted), their target a post,
#: a comment or a remote-desktop session.  Matched as text, an id from any
#: of those would land on the page of an agent that shares its digits.  A
#: new writer whose entry concerns an agent adds its event type here.
AGENT_FIELD = {'goal_dispatched': 'target_id', 'agent_steered': 'target_id',
               'a2ui_push': 'target_id', 'a2ui_register_component': 'target_id',
               'wm_window_op': 'actor_id'}
#: Entries read per round trip while verify walks the chain.
VERIFY_BATCH = 5000
#: Broken ranges a verify report lists, in walk order; past this it counts.
VERIFY_MAX_RANGES = 20


def _entry_hash_ok(entry) -> bool:
    """Whether an entry's own hash matches its fields and the prev_hash it
    stores (its link to the entry before it is checked separately)."""
    return entry['entry_hash'] == _compute_hash(
        entry['prev_hash'], entry['event_type'], entry['actor_id'], entry['action'],
        entry['created_at'], entry.get('detail_json'))


def _ranges_text(ranges: List[Dict], capped: bool) -> str:
    """'entry 1005' / 'entries 1-30058, 31002 and 3 more ranges'."""
    shown = [str(r['from_id']) if r['from_id'] == r['to_id']
             else f"{r['from_id']}-{r['to_id']}" for r in ranges[:5]]
    more = len(ranges) - len(shown)
    tail = f' and {more}{"+" if capped else ""} more ranges' if more else ''
    single = len(ranges) == 1 and ranges[0]['count'] == 1 and not capped
    return f"entr{'y' if single else 'ies'} {', '.join(shown)}{tail}"


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
                # NEVER silent: this is the hash-chain link. Falling through to the
                # in-memory tail (or 'genesis') means the NEXT entry chains onto a
                # different predecessor than the DB holds, so the chain silently
                # stops being tamper-evident — the one property this class exists
                # for. The read must still not raise (an audit write must not be
                # lost because the chain head could not be read), so it degrades,
                # but it says so.
                logger.exception(
                    "immutable_audit_log: could not read the chain head from the DB; "
                    "falling back to the in-memory tail. The chain may FORK here and "
                    "tamper-evidence is degraded until the DB read succeeds again.")

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

    def verify_chain(self) -> Tuple[bool, str]:
        """(is_valid, reason) over the WHOLE chain, oldest first, past any
        break (check_chain, no time budget); a log that could not be read is
        (False, 'Not checked: ...').  It read only the oldest 1000 entries
        before, so a change to any later entry was reported 'Chain valid'."""
        report = self.check_chain()
        return bool(report['ok']), report['reason']

    def check_chain(self, budget_s: Optional[float] = None,
                    batch: int = VERIFY_BATCH,
                    newest_first: bool = False) -> Dict[str, Any]:
        """Check every entry the walk reaches: its own hash (over its fields
        and the prev_hash it stores) and its link (that prev_hash is the
        entry before it's hash; 'genesis' for the first).  ``batch`` entries
        per read, never all of them, and a break does not stop the walk: the
        broken entries are reported as ranges of adjacent entries.

        ``newest_first`` walks from the newest entry back (the agent page: the
        entries it shows are checked first); otherwise from the oldest.  With
        ``budget_s`` the walk stops once that many seconds have passed; then
        the oldest entry reached has its own hash checked, not its link to
        the older entry nobody read.

        Returns {ok, reason, checked, total, newest_checked_id,
        oldest_checked_id, broken, broken_ranges, ranges_capped}.  ok is True
        when every checked entry is valid, False when one is broken, and None
        when the log could not be read: NOT CHECKED, never 'Empty log' (a
        read failure used to fall back to the empty in-memory log and say
        'Empty log', ok).  The whole log was checked only when checked ==
        total."""
        started = time.monotonic()
        checked = broken = 0
        ranges: List[Dict] = []
        run: Optional[Dict] = None
        capped = False
        newest_id = oldest_id = None

        def note(entry, valid):
            nonlocal checked, broken, run, capped, newest_id, oldest_id
            checked += 1
            entry_id = entry['id']
            newest_id = entry_id if newest_id is None else max(newest_id, entry_id)
            oldest_id = entry_id if oldest_id is None else min(oldest_id, entry_id)
            if valid:
                run = None
                return
            broken += 1
            if run is not None:
                run['from_id'] = min(run['from_id'], entry_id)
                run['to_id'] = max(run['to_id'], entry_id)
                run['count'] += 1
            elif len(ranges) < VERIFY_MAX_RANGES:
                run = {'from_id': entry_id, 'to_id': entry_id, 'count': 1}
                ranges.append(run)
            else:
                capped = True

        total, read_error = None, None
        try:
            total = self._count_entries(strict=True)
        except Exception as e:
            read_error = e
        cursor, pending, last_hash, stopped = None, None, 'genesis', False
        while read_error is None:
            try:
                if newest_first:
                    entries = self._get_entries(batch, newest=True, before_id=cursor,
                                                strict=True)
                else:
                    entries = self._get_entries(batch, after_id=cursor, strict=True)
            except Exception as e:
                read_error = e
                break
            if not entries:
                break
            if newest_first:  # this batch is older than everything checked so far
                if pending is not None:
                    note(pending, _entry_hash_ok(pending)
                         and pending['prev_hash'] == entries[-1]['entry_hash'])
                for i in range(len(entries) - 1, 0, -1):
                    note(entries[i], _entry_hash_ok(entries[i])
                         and entries[i]['prev_hash'] == entries[i - 1]['entry_hash'])
                pending, cursor = entries[0], entries[0]['id']
            else:
                for entry in entries:
                    note(entry, _entry_hash_ok(entry) and entry['prev_hash'] == last_hash)
                    last_hash = entry['entry_hash']
                cursor = entries[-1]['id']
            if budget_s is not None and time.monotonic() - started >= budget_s:
                stopped = True
                break
        if pending is not None:
            # The oldest entry read.  It links to genesis when nothing older
            # exists; when the walk stopped early or a read failed, its older
            # neighbour was never read, so only its own hash is checked.
            first = read_error is None and (not stopped or checked + 1 >= (total or 0))
            note(pending, _entry_hash_ok(pending)
                 and (not first or pending['prev_hash'] == 'genesis'))
        if read_error is not None:
            logger.warning('immutable_audit_log: verify could not read the log after '
                           '%d entries; the rest is NOT CHECKED', checked,
                           exc_info=read_error)
        if total is not None:
            total = max(total, checked)
        which = 'newest' if newest_first else 'oldest'
        if broken:
            head = (f'Chain broken at {_ranges_text(ranges, capped)}: {broken:,} of '
                    f'{checked:,} checked entries fail their own hash or their link')
        elif read_error is not None:
            head = (f'{which} {checked:,} entries checked, all valid' if checked
                    else 'Not checked')
        elif not checked:
            head = 'Empty log'
        elif checked == total:
            head = f'Chain valid ({checked:,} entries)'
        else:
            head = f'{which} {checked:,} of {total:,} entries checked, all valid'
        if read_error is not None:
            rest = 'the rest was not checked' if checked else 'nothing was checked'
            tail = (f'; the audit log could not be read, so {rest} '
                    f'({type(read_error).__name__}: {read_error})')
        elif total and checked < total:
            tail = (f'; {total - checked:,} {"older" if newest_first else "newer"} '
                    f'entries not checked in this request')
        else:
            tail = ''
        return {'ok': (False if broken else None) if read_error is not None else not broken,
                'reason': head + tail, 'checked': checked, 'total': total,
                'newest_checked_id': newest_id, 'oldest_checked_id': oldest_id,
                'broken': broken, 'broken_ranges': ranges, 'ranges_capped': capped}

    def _count_entries(self, strict: bool = False) -> int:
        """How many entries the log holds (check_chain's total).  ``strict``:
        a database read failure raises instead of counting the in-memory
        tail, so verify can say NOT CHECKED."""
        if self._use_db:
            try:
                from integrations.social.models import get_db, AuditLogEntry
                db = get_db()
                try:
                    return int(db.query(AuditLogEntry.id).count())
                finally:
                    db.close()
            except Exception:
                if strict:
                    raise
                logger.exception(
                    "immutable_audit_log: could not count entries in the DB; "
                    "counting the in-memory tail (%d entries) instead.",
                    len(self._memory_log))
        return len(self._memory_log)

    def get_trail(self, actor_id: Optional[str] = None,
                  event_type: Optional[str] = None,
                  limit: int = 100, *, related_to=None,
                  since=None, until=None) -> List[Dict]:
        """The newest ``limit`` entries that match every filter given, oldest
        first.

        ``related_to``: agent ids; an entry matches when the field its event
        type names in AGENT_FIELD holds one of them (what the agent did, and
        what was done to it).  ``since`` / ``until``: an ISO date or time
        bounding created_at, both ends included (utc_naive, the one window
        rule: a date-only until is that whole day; anything else raises
        ValueError).  The filters run in the query, so a match is found
        however many entries surround it.  It used to read the OLDEST
        limit*5 rows and filter after, so on a long-running node it never
        saw a recent entry."""
        return self._get_entries(limit, newest=True, actor_id=actor_id,
                                 event_type=event_type, related_to=related_to,
                                 since=utc_naive(since),
                                 until=utc_naive(until, day_end=True))

    def _get_entries(self, limit: int = 1000, *, newest: bool = False,
                     actor_id: Optional[str] = None, event_type: Optional[str] = None,
                     related_to=None, since: Optional[datetime] = None,
                     until: Optional[datetime] = None,
                     after_id: Optional[int] = None, before_id: Optional[int] = None,
                     strict: bool = False) -> List[Dict]:
        """Raw entries from DB or memory, oldest first: the first ``limit``,
        or with ``newest`` the last ``limit`` that match the filters
        (get_trail).  ``after_id`` / ``before_id`` bound the id (check_chain's
        next batch, oldest-first / newest-first).  ``strict``: a database
        read failure raises instead of answering from the in-memory tail, so
        verify can say NOT CHECKED rather than read an empty log as valid."""
        related = None if related_to is None else {str(i) for i in related_to}
        if self._use_db:
            try:
                from integrations.social.models import get_db, AuditLogEntry
                db = get_db()
                try:
                    # Columns, not entities: the verify walk reads every row.
                    query = db.query(
                        AuditLogEntry.id, AuditLogEntry.event_type,
                        AuditLogEntry.actor_id, AuditLogEntry.target_id,
                        AuditLogEntry.action, AuditLogEntry.detail_json,
                        AuditLogEntry.prev_hash, AuditLogEntry.entry_hash,
                        AuditLogEntry.created_at)
                    if after_id is not None:
                        query = query.filter(AuditLogEntry.id > after_id)
                    if before_id is not None:
                        query = query.filter(AuditLogEntry.id < before_id)
                    if actor_id:
                        query = query.filter(AuditLogEntry.actor_id == actor_id)
                    if event_type:
                        query = query.filter(AuditLogEntry.event_type == event_type)
                    if related is not None:
                        from sqlalchemy import and_, or_
                        query = query.filter(or_(*[
                            and_(AuditLogEntry.event_type.in_(
                                     [t for t, f in AGENT_FIELD.items() if f == field]),
                                 getattr(AuditLogEntry, field).in_(related))
                            for field in sorted(set(AGENT_FIELD.values()))]))
                    if since is not None:
                        query = query.filter(AuditLogEntry.created_at >= since)
                    if until is not None:
                        query = query.filter(AuditLogEntry.created_at <= until)
                    rows = query.order_by(
                        AuditLogEntry.id.desc() if newest else AuditLogEntry.id.asc()
                    ).limit(limit).all()
                    if newest:
                        rows = rows[::-1]
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
                if strict:
                    raise
                # NEVER silent: on failure this returns the in-memory tail, which
                # for a fresh process is EMPTY. An auditor reading "no entries"
                # cannot distinguish "nothing happened" from "the audit store is
                # unreachable" — the difference between a clean record and a
                # missing one. Degrade, but say so.
                logger.exception(
                    "immutable_audit_log: could not read entries from the DB; "
                    "returning the in-memory tail (%d entries). A caller seeing an "
                    "empty audit log here is seeing a READ FAILURE, not an empty "
                    "history.", len(self._memory_log))

        def concerns(entry) -> bool:  # AGENT_FIELD, as the query reads it
            value = entry.get(AGENT_FIELD.get(entry['event_type'], ''))
            return value is not None and str(value) in related

        entries = [e for e in self._memory_log
                   if (after_id is None or e['id'] > after_id)
                   and (before_id is None or e['id'] < before_id)
                   and (not actor_id or e['actor_id'] == actor_id)
                   and (not event_type or e['event_type'] == event_type)
                   and (related is None or concerns(e))
                   and (since is None or utc_naive(e['created_at']) >= since)
                   and (until is None or utc_naive(e['created_at']) <= until)]
        if newest:
            return entries[max(0, len(entries) - limit):]
        return entries[:limit]


# Singleton
_audit_log = None


def get_audit_log() -> ImmutableAuditLog:
    global _audit_log
    if _audit_log is None:
        _audit_log = ImmutableAuditLog()
    return _audit_log
