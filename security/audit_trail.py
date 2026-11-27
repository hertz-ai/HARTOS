"""security.audit_trail -- THE record of what every agent received and produced.

Owner, 2026-11-20: "work on auditability of all modalities in HARTOS, every
message, transcription, audio, video, file uploaded, image caption, screen
captioned, video captioned per agent via nunba ui", "for using HARTOS in a
financial institution", "logging the role etc for each action without
compromise in speed or compute", "storage space shd be respected", "do the
best minimalistically as a framework".

ONE recorder, called from the few places every modality already passes
(the conversation table's writer, the one upload store, the image describer,
speech-to-text, speech out, the LLM call log, a step's state change).

One view, two stores (owner, 2026-11-21): the older ImmutableAuditLog
(security.immutable_audit_log) keeps the security and money events that
must commit with the database transaction they describe (log_event(db=)),
and this trail keeps every agent modality.  Nothing is written to both.
The agent page and its export (integrations.social.api_audit) read the two
as one trail, an event-log entry shown as an EVENT_LOG_KIND line.

Speed: ``record()`` builds a small dict and puts it on a bounded queue.  It
never touches the disk, the database or a lock held across I/O.  One
background thread does the rest: fills in who acted and in what role, puts
every stored credential value back as its {{secret:NAME}} alias (the vault's
own rule, hartos.ai_key_vault.AIKeyVault.mask_secrets: a tool runs with the
real value in hand, and only the vault knows an arbitrary password), hides
other secrets (security.secret_redactor.redact_secrets, the one pattern
rule), links each
line to the one before it (security.immutable_audit_log._compute_hash, the
one hash rule) and appends it to today's file.  A full queue never blocks a
turn: the record is counted and the next line written is a ``gap`` line
saying how many were lost, so a missing record is never silent.  A write
the disk refuses is counted the same way: the file is cut back to the last
point it is known whole and the next line starts a fresh segment.

Storage: text and checksums only.  Media (audio, video frames, files, images,
spoken replies) is recorded as a pointer (path or store id) with its sha256
and size, never copied.  Text over TEXT_CAP characters keeps its first
TEXT_CAP characters plus the sha256 and length of the whole.  Each day is one
file per process, gzipped once the day is over.  Files older than the
retention period are deleted.  Past the size cap, or below the free-disk
floor, lines keep their metadata and checksums but drop their text, and a
``storage_limit`` line says so; nothing inside the retention period is ever
deleted to make room.

Tamper evidence: every line carries ``prev`` and ``hash``; ``verify()``
recomputes them, so an edited, removed or reordered line breaks the chain.
Each closed day is listed in ``manifest.jsonl`` with its line count and last
hash, so a whole file deleted inside the retention period is caught too.  A
row the disk refuses is kept and written once there is room, on a line of its
own.

Roles: a person's role (account kind, tier, admin) and an agent's role (its
persona or step) are separate fields and never stand in for each other
(owner, 2026-11-07: a person's role is never an agent name, nor the reverse).
"""
from __future__ import annotations

import atexit
import gzip
import hashlib
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

logger = logging.getLogger('hevolve_security')

#: What a line can be.  Media kinds carry a pointer + sha256, text kinds text.
#: Raw video frames are not a kind: they live in memory only and nothing
#: records them; camera input reaches the trail as video_caption.
#: The lines the trail writes about itself, never about an agent: records
#: lost to a full queue, and the storage cap starting and ending.  The
#: writer names these and no other (_write_batch_locked, _check_storage).
GAP_KIND, STORAGE_LIMIT_KIND, STORAGE_OK_KIND = 'gap', 'storage_limit', 'storage_ok'
HOUSEKEEPING_KINDS = (GAP_KIND, STORAGE_LIMIT_KIND, STORAGE_OK_KIND)
KINDS = ('message', 'transcript', 'audio', 'file', 'image_caption',
         'screen_caption', 'video_caption', 'llm_call', 'speech_out',
         'video_out', 'action') + HOUSEKEEPING_KINDS
#: The kind the agent page gives an ImmutableAuditLog entry it shows beside
#: the trail's lines.  Never written into the trail: those entries stay in
#: their own store because they commit with a database transaction.
EVENT_LOG_KIND = 'security_event'
#: The kinds a person can filter the agent page by, served to it with the
#: lines (Nunba reads them, so it keeps no copy of KINDS): every kind except
#: the trail's lines about itself, which carry no agent, plus the event log's.
VIEW_KINDS = tuple(k for k in KINDS if k not in HOUSEKEEPING_KINDS) + (EVENT_LOG_KIND,)
TEXT_CAP = 20000
QUEUE_SIZE = 50000
BATCH = 500
#: Days a closed file is kept.  A financial institution sets its own period.
RETENTION_DAYS = int(os.environ.get('HEVOLVE_AUDIT_RETENTION_DAYS', '') or 365)
#: Size cap of the whole trail, and the free disk it must leave.
MAX_MB = int(os.environ.get('HEVOLVE_AUDIT_MAX_MB', '') or 2048)
MIN_FREE_MB = int(os.environ.get('HEVOLVE_AUDIT_MIN_FREE_MB', '') or 2048)
#: How often the writer re-measures size and free disk.
STORAGE_CHECK_S = 60.0
#: Request contexts remembered at once (request id -> who / which agent).
CONTEXT_CAP = 4096
#: A person's role is looked up at most this often per person.
ROLE_TTL_S = 600.0
_GENESIS = 'genesis'
_HASHED_OUT = ('prev', 'hash', 'kind', 'user_id', 'dir', 'ts')


def _canonical(body: dict) -> str:
    return json.dumps(body, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, default=str)


def line_hash(prev: str, line: dict) -> str:
    """The hash of one line, chained to ``prev`` (the one hash rule)."""
    from security.immutable_audit_log import _compute_hash
    body = {k: v for k, v in line.items() if k not in _HASHED_OUT}
    return _compute_hash(prev, str(line.get('kind') or ''),
                         str(line.get('user_id') or ''), str(line.get('dir') or ''),
                         str(line.get('ts') or ''), _canonical(body))


def sha256_file(path, limit: Optional[int] = None) -> Tuple[Optional[str], Optional[int]]:
    """(sha256, size) of a file, or (None, None) when it cannot be read."""
    try:
        h = hashlib.sha256()
        size = 0
        with open(path, 'rb') as f:
            for block in iter(lambda: f.read(1 << 20), b''):
                h.update(block)
                size += len(block)
                if limit is not None and size >= limit:
                    break
        return h.hexdigest(), size
    except OSError:
        return None, None


def _default_root() -> str:
    override = os.environ.get('HEVOLVE_AUDIT_DIR', '').strip()
    if override:
        return override
    from core.platform_paths import get_data_dir
    return os.path.join(get_data_dir(), 'audit_trail')


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')


def _iso(ts) -> str:
    """A line's ts text: UTC, to the millisecond.  ``ts`` is epoch seconds,
    or a datetime (naive is UTC)."""
    if isinstance(ts, datetime):
        moment = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat(timespec='milliseconds')
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec='milliseconds')


def _window_text(value, *, until: bool) -> Optional[str]:
    """A window end as a line's ts text, so read() comparing text compares
    times: parsed to UTC by the one window rule
    (immutable_audit_log.utc_naive: a zone is converted, a date-only until
    is that whole day, anything else raises ValueError), then cut to whole
    milliseconds, a line's precision: up for since, down for until."""
    if value is None or value == '':
        return None
    from security.immutable_audit_log import utc_naive
    moment = utc_naive(value, day_end=until)
    spare = moment.microsecond % 1000
    if spare:
        moment += timedelta(microseconds=-spare if until else 1000 - spare)
    return _iso(moment)


#: How deep a line's extra fields are kept; anything deeper is one marker.
EXTRA_DEPTH = 6


def _cut_deep(value, depth: int = 0):
    """``value`` with every non-string nested EXTRA_DEPTH levels or deeper
    replaced by one marker: the one depth rule for extra fields, applied
    before the vault and the pattern rule walk them, so neither recurses
    past it (rv_f2f8, finding 3: 600 nested lists exhausted the recursion
    limit inside the vault's walk and withheld the whole batch's text)."""
    if isinstance(value, str):
        return value
    if depth >= EXTRA_DEPTH:
        return '[withheld: nested too deep]'
    if isinstance(value, dict):
        return {k: _cut_deep(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_cut_deep(v, depth + 1) for v in value]
    return value


def _redact_extra(extra: dict) -> dict:
    """A line's extra fields (an action's step, a caption's question, file
    names, an error) as the trail may keep them: cut at the one depth rule
    (_cut_deep), secret-named keys withheld by the audit log's key rule
    (immutable_audit_log._redact_sensitive) and secrets inside string values
    hidden by the value rule (secret_redactor.redact_secrets), nested dicts
    and lists included.  The trail is hash-chained, so anything kept raw is
    kept for good: when redaction fails the fields are withheld, never
    written as they came."""
    try:
        from security.immutable_audit_log import _redact_sensitive
        from security.secret_redactor import redact_secrets

        def walk(value):
            if isinstance(value, str):
                return redact_secrets(value)[0]
            if isinstance(value, dict):
                safe = json.loads(_redact_sensitive({str(k): v for k, v in value.items()}))
                return {k: walk(v) for k, v in safe.items()}
            if isinstance(value, (list, tuple)):
                return [walk(v) for v in value]
            return value
        return walk(_cut_deep(extra))
    except Exception:
        logger.warning('audit_trail: extra fields could not be redacted; withheld',
                       exc_info=True)
        return {'withheld': 'the extra fields could not be checked for secrets'}


def _mask_stored_credentials(records: List[dict]) -> None:
    """Every stored credential value in the records' text and extra fields
    put back as its {{secret:NAME}} alias, in place, by the vault's own rule
    (hartos.ai_key_vault.AIKeyVault.mask_secrets, reached through
    core.tool_logging.credential_vault).

    A tool runs with the real value in hand (log_tool_execution resolves the
    model's aliases before the call), so a record of what it did, a command
    and its outcome, can carry the value, and no pattern rule can know an
    arbitrary password; only the vault can (review of abffee892: a
    `-p{{secret:DB_PASS}}` command reached the trail with the password).

    One call for the whole batch: mask_secrets reads the owner's grants, a
    database read, so it runs on the writer thread once per write, never on a
    caller's thread and never once per line.  It runs before the pattern
    redaction and the text cap, so a value the cap would cut leaves no
    prefix behind.  The trail's own lines (gap, storage) carry no agent
    content and are left alone.  With no vault (it cannot load) there is
    nothing to mask: no alias was resolved either.  When masking fails, the
    batch's text and extra fields are withheld (the text's sha256 and length
    kept), never written as they came: the trail is hash-chained, so a value
    kept once is kept for good.

    A text or extra that cannot be turned into text at all costs only its
    own line: that field is withheld and every other line is written."""
    fields = []
    for rec in records:
        if rec.get('kind') in HOUSEKEEPING_KINDS:
            continue
        if rec.get('text') is not None:
            try:
                if not isinstance(rec['text'], str):
                    rec['text'] = str(rec['text'])
                fields.append((rec, 'text'))
            except Exception as exc:
                # A __str__ that raises, an int past Python's digit limit
                # (rv_f2f8, finding 2): this line's text, never the batch.
                # The line names the error's kind, so a reader sees why the
                # text is missing (rv_3cd5, finding 3).
                logger.warning('audit_trail: a text could not be read; withheld',
                               exc_info=True)
                rec.pop('text')
                rec['text_unreadable'] = type(exc).__name__
        if rec.get('extra'):
            # Plain JSON values first, the form the line is written in: an
            # exception, bytes or a set in extra was written by json.dumps'
            # default=str, past both the vault and the pattern rule, which
            # only walk strings, dicts and lists (rv_cdf5, finding 2).  An
            # extra that cannot become JSON at all (a tuple key, a value
            # holding itself, a __str__ that raises) is withheld for this line
            # alone, never allowed to drop the batch (rv_2c5a finding 1,
            # rv_f2f8 finding 1).
            try:
                rec['extra'] = _cut_deep(json.loads(json.dumps(rec['extra'], default=str)))
            except Exception as exc:
                logger.warning('audit_trail: extra fields could not be written as '
                               'JSON; withheld', exc_info=True)
                rec['extra'] = {'withheld': 'the extra fields could not be turned '
                                            f'into JSON ({type(exc).__name__})'}
                continue
            fields.append((rec, 'extra'))
    if not fields:
        return
    try:
        from core.tool_logging import credential_vault
        vault = credential_vault()
        if vault is None:
            return
        masked = vault.mask_secrets([rec[key] for rec, key in fields])
        for (rec, key), value in zip(fields, masked):
            rec[key] = value
    except Exception:
        logger.warning('audit_trail: stored credentials could not be masked; '
                       'text and extra fields withheld', exc_info=True)
        for rec, key in fields:
            if key == 'text':
                text = rec.pop('text')
                rec['text_sha256'] = hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()
                rec['text_len'] = len(text)
            else:
                rec['extra'] = {'withheld': 'the extra fields could not be checked '
                                            'for stored credentials'}


def _plain(value) -> bool:
    """A value a fallback line may keep: text that can be written as UTF-8,
    or a whole number that can be written as text (never a bool).  A user id
    holding a lone surrogate (json.loads makes one from a request body) is
    text that cannot be written: kept, it made the fallback line fail too and
    the whole batch was lost (rv_e57b, finding 1)."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return False
    try:
        (value if isinstance(value, str) else str(value)).encode('utf-8')
    except (UnicodeEncodeError, ValueError):
        return False
    return True


def _gap_extra(queue_full: int, write_failed: int) -> dict:
    """A gap line's fields: how many records were lost, and why, each cause
    counted on its own (rv_36d0: one sentence for two causes told a reader
    neither).  A queue-only gap reads as it always has."""
    extra = {'lost': queue_full + write_failed}
    if write_failed:
        extra['write_failed'] = write_failed
    if queue_full and write_failed:
        extra['why'] = (f'{queue_full} were not kept because the queue was full, and '
                        f"{write_failed} because a write to the trail's file failed")
    elif write_failed:
        extra['why'] = "a write to the trail's file failed; these records were not kept"
    else:
        extra['why'] = 'the queue was full; these records were not kept'
    return extra


def _loss_parts(rec) -> Tuple[int, int]:
    """How many lost records a line stands for when it is lost, as (queue
    full, write failed): a gap line stands for the records it names, so they
    are carried into the next one; a storage line stands for none (the next
    storage check writes it again; rv_d9ed, finding 1: a gap said 2 lost for
    1); any other line stands for itself."""
    if isinstance(rec, dict) and rec.get('kind') in (STORAGE_LIMIT_KIND, STORAGE_OK_KIND):
        return 0, 0
    if isinstance(rec, dict) and rec.get('kind') == GAP_KIND:
        extra = rec.get('extra') or {}
        lost = extra.get('lost') if isinstance(extra.get('lost'), int) else 0
        failed = extra.get('write_failed') if isinstance(extra.get('write_failed'), int) else 0
        return max(lost - failed, 0), failed
    return 0, 1


def _unwritable_line(rec: dict, exc: Exception, now: float) -> dict:
    """What is kept of a record no line could be made from: its time, kind,
    direction and the ids that can be written (_plain), with the error's
    kind saying why the rest is missing and ``fields_withheld`` naming any
    of those fields that could not be written."""
    line = {'ts': rec['ts'] if isinstance(rec.get('ts'), str)
            else _iso(rec['t'] if isinstance(rec.get('t'), (int, float)) else now)}
    dropped = []
    for key in ('kind', 'dir', 'user_id', 'agent_id', 'request_id'):
        if key not in rec:
            continue
        if _plain(rec[key]):
            line[key] = rec[key]
        else:
            dropped.append(key)
    line.setdefault('kind', 'unknown')
    line['withheld'] = f'this line could not be written ({type(exc).__name__})'
    if dropped:
        line['fields_withheld'] = dropped
    return line


class AuditTrail:
    """The recorder.  One per process; ``get_trail()`` returns it."""

    def __init__(self, root: Optional[str] = None, *, retention_days: int = None,
                 max_bytes: int = None, min_free_bytes: int = None,
                 queue_size: int = QUEUE_SIZE, clock=time.time,
                 role_lookup=None, start: bool = True):
        self.root = root or _default_root()
        self.retention_days = RETENTION_DAYS if retention_days is None else retention_days
        self.max_bytes = MAX_MB * 2**20 if max_bytes is None else max_bytes
        self.min_free_bytes = MIN_FREE_MB * 2**20 if min_free_bytes is None else min_free_bytes
        self._clock = clock
        self._role_lookup = role_lookup or _person_role
        self._q: 'queue.Queue' = queue.Queue(maxsize=queue_size)
        # Records lost, by cause: a full queue (_lost) and a write the file
        # refused (_lost_write).  The next gap line names both.
        self._lost = 0
        self._lost_write = 0
        self._lost_lock = threading.Lock()
        # The last point the file is known to hold whole (a flush that
        # succeeded): its size, line count and last hash, and how many
        # records the lines handed to it since stand for, as (queue full,
        # write failed).  A failed write cuts the file back to that point
        # (_abandon_file).  The storage switch as it stood there goes back
        # with the lines cut away, a storage line among them.
        self._flushed_size = 0
        self._flushed_lines = 0
        self._flushed_prev = _GENESIS
        self._flushed_withheld = False
        self._pending = (0, 0)
        # Manifest rows the disk has not taken yet (_manifest).
        self._manifest_pending: List[dict] = []
        self._contexts: Dict[str, dict] = {}
        self._ctx_lock = threading.Lock()
        self._roles: Dict[str, Tuple[float, Optional[dict]]] = {}
        self._tag = f'{os.getpid()}-{int(clock() * 1000) % 10**8}'
        self._file = None
        self._file_day = None
        self._file_name = None
        self._prev = _GENESIS
        self._lines = 0
        self._withheld = False
        self._last_storage_check = 0.0
        self._last_fsync = 0.0
        self._thread = None
        self._start_lock = threading.Lock()
        # One writer at a time: the background thread, or drain()/close() on
        # another thread at exit.  Two appenders would fork the chain.
        self._write_lock = threading.Lock()
        self._autostart = start

    # ── the hot path ──────────────────────────────────────────────

    def record(self, kind: str, *, direction: str = 'in', text=None, ref=None,
               sha256=None, size=None, mime=None, user_id=None, agent_id=None,
               request_id=None, actor=None, agent_role=None, model=None,
               hash_ref: bool = False, digest_of=None, **extra) -> None:
        """Queue one line.  Never blocks, never raises, never does I/O.

        ``hash_ref``: the writer reads the file at ``ref`` for its sha256 and
        size, so the caller's thread never hashes media.  ``digest_of``: an
        object (a request body) the writer reduces to the sha256 and length
        of its JSON; the object itself is never kept."""
        try:
            rec = {'t': self._clock(), 'kind': kind, 'dir': direction}
            if hash_ref and ref and not sha256:
                rec['_hash_ref'] = True
            if digest_of is not None and not sha256:
                rec['_digest_of'] = digest_of
            for key, val in (('text', text), ('ref', ref), ('sha256', sha256),
                             ('bytes', size), ('mime', mime), ('user_id', user_id),
                             ('agent_id', agent_id), ('request_id', request_id),
                             ('actor', actor), ('agent_role', agent_role),
                             ('model', model)):
                if val is not None and val != '':
                    rec[key] = val
            if extra:
                rec['extra'] = {k: v for k, v in extra.items() if v is not None}
            self._ensure_started()
            self._q.put_nowait(rec)
        except queue.Full:
            with self._lost_lock:
                self._lost += 1
        except Exception:
            logger.warning('audit_trail: a record could not be queued', exc_info=True)

    def bind(self, request_id, **ids) -> None:
        """Remember who and which agent a request is for, so later records
        that carry only the request id are attributed in the writer."""
        if not request_id:
            return
        with self._ctx_lock:
            ctx = self._contexts.setdefault(str(request_id), {})
            ctx.update({k: v for k, v in ids.items() if v is not None and v != ''})
            while len(self._contexts) > CONTEXT_CAP:
                self._contexts.pop(next(iter(self._contexts)))

    def bound(self, request_id) -> dict:
        """A copy of the ids bind() remembered for a request, or {}."""
        if not request_id:
            return {}
        with self._ctx_lock:
            return dict(self._contexts.get(str(request_id), {}))

    # ── the writer ────────────────────────────────────────────────

    def _ensure_started(self) -> None:
        if self._thread is not None or not self._autostart:
            return
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='audit-trail',
                                                daemon=True)
                self._thread.start()

    def _run(self) -> None:
        try:
            self._maintain()
        except Exception:
            logger.warning('audit_trail: start-up maintenance failed', exc_info=True)
        while True:
            try:
                batch = [self._q.get()]
                while len(batch) < BATCH:
                    try:
                        batch.append(self._q.get_nowait())
                    except queue.Empty:
                        break
                self._write_batch(batch)
            except Exception:
                logger.warning('audit_trail: a batch could not be written', exc_info=True)

    def drain(self) -> int:
        """Write everything queued now, on the calling thread (tests, exit)."""
        batch = []
        while True:
            try:
                batch.append(self._q.get_nowait())
            except queue.Empty:
                break
        if batch or self._lost or self._lost_write:
            self._write_batch(batch)
        return len(batch)

    def _write_batch(self, batch: List) -> None:
        with self._write_lock:
            self._write_batch_locked(batch)

    def _write_batch_locked(self, batch: List) -> None:
        events = [r for r in batch if not isinstance(r, threading.Event)]
        done = [r for r in batch if isinstance(r, threading.Event)]
        with self._lost_lock:
            queue_lost, self._lost = self._lost, 0
            write_lost, self._lost_write = self._lost_write, 0
        if queue_lost or write_lost:
            events.insert(0, {'t': self._clock(), 'kind': GAP_KIND, 'dir': 'sys',
                              'extra': _gap_extra(queue_lost, write_lost)})
        written = 0
        try:
            if events:
                self._check_storage(events[-1]['t'])
                _mask_stored_credentials(events)
                for rec in events:
                    self._write_one(rec)
                    written += 1
                if self._file is not None:
                    self._file.flush()
                    self._mark_flushed()
                    if self._manifest_pending:
                        self._write_manifest()
                    now = self._clock()
                    if now - self._last_fsync >= 1.0:
                        try:
                            os.fsync(self._file.fileno())
                        except OSError:
                            pass
                        self._last_fsync = now
        except OSError:
            # The file itself failed (a full disk, a removed folder).  A disk
            # refuses below Python's buffers, so lines this batch handed to
            # the file may be lost in part while the chain already moved past
            # them (rv_36d0, finding 1): the file is cut back to the last point
            # known whole and those lines are counted with the records not yet
            # written, so the next line written is a gap line saying how many.
            self._count_lost(events[written:])
            self._abandon_file()
            raise
        finally:
            # A flush() waiting on this batch is answered whatever happened,
            # never left to time out.
            for ev in done:
                ev.set()

    def _write_one(self, rec: dict) -> None:
        """One record as one line.  A field the line cannot be made from (a
        digest_of with a tuple key, a ref whose __str__ raises) costs this
        line's content, never the lines after it (rv_3cd5, finding 2): the
        record is written as _unwritable_line.  Nothing was written for the
        failed try: json.dumps runs before the write, and the chain moves only
        after it.  A fallback line that still cannot be written is counted as
        lost, so the next gap line names it (rv_e57b, finding 1).  OSError (the
        file itself failed) goes to the batch."""
        try:
            self._append(self._finish(rec))
            return
        except OSError:
            raise
        except Exception as exc:
            logger.warning('audit_trail: a line could not be written; its kind '
                           'and ids are kept', exc_info=True)
            failed = exc
        try:
            self._append(_unwritable_line(rec, failed, self._clock()))
        except OSError:
            raise
        except Exception:
            logger.warning('audit_trail: a line could not be written even without '
                           'its content; counted as lost', exc_info=True)
            with self._lost_lock:
                self._lost_write += 1

    def _count_lost(self, unwritten: List[dict]) -> None:
        """Count records a failed batch did not write, so a gap line names them
        (a gap line among them stands for the records it carried)."""
        queue_part = write_part = 0
        for rec in unwritten:
            q, w = _loss_parts(rec)
            queue_part, write_part = queue_part + q, write_part + w
        if queue_part or write_part:
            with self._lost_lock:
                self._lost += queue_part
                self._lost_write += write_part

    def _mark_flushed(self) -> None:
        """The file holds every line handed to it so far: remember the point."""
        self._flushed_size = os.fstat(self._file.fileno()).st_size
        self._flushed_lines = self._lines
        self._flushed_prev = self._prev
        self._flushed_withheld = self._withheld
        self._pending = (0, 0)

    def _abandon_file(self) -> None:
        """Stop using a file a write failed on (a full disk).

        What was handed to it since the last flush that succeeded may be on
        disk in part, its last line cut short, while the chain already moved
        past it (rv_36d0, finding 1).  The file is cut back to the last point
        known whole, the lines handed to it since are counted as lost (the
        next gap line names them), and the handle is dropped without
        flushing: the next line opens a fresh segment, chained from genesis.
        Cutting a file back frees space; it never needs any.

        Those lines are dropped, never held in Python's buffers for a write
        once the disk has room: a record is on disk whole or named in a gap
        line, never both and never neither.

        A storage line among them takes its switch with it: text goes back to
        being kept or withheld as it was at that point, and storage is checked
        again on the next batch, so text is never withheld with nothing on
        disk saying why (rv_d9ed, finding 1).  A segment that never held a
        whole line is removed rather than listed in the manifest, so a disk
        that stays full is not asked for a row per batch."""
        f = self._file
        if f is None:
            return
        self._file = None
        q, w = self._pending
        self._pending = (0, 0)
        if q or w:
            with self._lost_lock:
                self._lost += q
                self._lost_write += w
        self._withheld = self._flushed_withheld
        self._last_storage_check = 0.0
        cut = False
        try:
            raw = f.buffer.raw
            try:
                os.ftruncate(raw.fileno(), self._flushed_size)
                cut = True
            except OSError:
                logger.warning('audit_trail: %s could not be cut back after a failed '
                               'write', self._file_name, exc_info=True)
            # Closing the raw file first closes the buffers above it, so
            # nothing they still hold is written later.
            raw.close()
            f.close()
        except Exception:
            logger.warning('audit_trail: %s could not be closed after a failed write',
                           self._file_name, exc_info=True)
        listed = True
        if cut and self._flushed_lines == 0:
            try:
                os.remove(os.path.join(self.root, self._file_name))
                listed = False
            except OSError:
                logger.warning('audit_trail: the empty segment %s could not be removed; '
                               'it is listed instead', self._file_name, exc_info=True)
        if listed:
            self._manifest(self._file_name, self._flushed_lines, self._flushed_prev)
        self._file_name = None
        self._file_day = None

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until everything queued before this call is on disk."""
        if self._thread is None:
            self.drain()
            return True
        ev = threading.Event()
        try:
            self._q.put(ev, timeout=timeout)
        except queue.Full:
            return False
        return ev.wait(timeout)

    def _finish(self, rec: dict) -> dict:
        """Attribution, roles, media checksums, secret redaction, the text cap.

        Attribution comes first: rec is filled in place, so when a later field
        fails (a digest_of that cannot be read) the line _unwritable_line keeps
        still carries the request's agent and person (rv_e57b, finding 2: the
        model-call line, which binds only its request id, lost both)."""
        rid = rec.get('request_id')
        if rid:
            with self._ctx_lock:
                ctx = dict(self._contexts.get(str(rid)) or {})
            for key, val in ctx.items():
                rec.setdefault(key, val)
        if rec.pop('_hash_ref', False):
            sha, size = sha256_file(rec['ref'])
            if sha is None:
                rec['ref_unreadable'] = True
            else:
                rec['sha256'] = sha
                rec.setdefault('bytes', size)
        if '_digest_of' in rec:
            raw = _canonical(rec.pop('_digest_of')).encode('utf-8', 'replace')
            rec['sha256'] = hashlib.sha256(raw).hexdigest()
            rec.setdefault('bytes', len(raw))
        if rec.get('extra'):
            rec['extra'] = _redact_extra(rec['extra'])
        uid = rec.get('user_id')
        if uid and 'person_role' not in rec:
            role = self._role_for(str(uid))
            if role:
                rec['person_role'] = role
        text = rec.get('text')
        if text is not None:
            text = text if isinstance(text, str) else str(text)
            try:
                from security.secret_redactor import redact_secrets
                text, hidden = redact_secrets(text)
                if hidden:
                    rec['secrets_hidden'] = hidden
            except Exception:
                logger.warning('audit_trail: secret redaction failed; text withheld',
                               exc_info=True)
                rec['text_sha256'] = hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()
                rec['text_len'] = len(text)
                text = None
            if text is not None and (len(text) > TEXT_CAP or self._withheld):
                rec['text_sha256'] = hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()
                rec['text_len'] = len(text)
                text = None if self._withheld else text[:TEXT_CAP]
            if text is None:
                rec.pop('text', None)
            else:
                rec['text'] = text
        rec['ts'] = _iso(rec.pop('t'))
        return rec

    def _role_for(self, user_id: str) -> Optional[dict]:
        now = self._clock()
        hit = self._roles.get(user_id)
        if hit and now - hit[0] < ROLE_TTL_S:
            return hit[1]
        try:
            role = self._role_lookup(user_id)
        except Exception:
            role = None
        self._roles[user_id] = (now, role)
        if len(self._roles) > CONTEXT_CAP:
            self._roles.pop(next(iter(self._roles)))
        return role

    def _append(self, rec: dict) -> None:
        day = rec['ts'][:10]
        if self._file is None or day != self._file_day:
            self._open(day)
        rec['prev'] = self._prev
        rec['hash'] = line_hash(self._prev, rec)
        self._file.write(json.dumps(rec, ensure_ascii=False, default=str) + '\n')
        self._prev = rec['hash']
        self._lines += 1
        q, w = _loss_parts(rec)
        self._pending = (self._pending[0] + q, self._pending[1] + w)

    def _open(self, day: str) -> None:
        self._close()
        os.makedirs(self.root, exist_ok=True)
        # A record that arrives after close() (daemon threads still running
        # at exit) reopens today's file.  Its chain starts at genesis, so it
        # goes to a fresh segment; appended to the closed one it would break
        # that file's chain mid-way and verify() would report tampering.
        # The counter is zero-padded so a process's segments sort in the
        # order they were written (read() walks names newest first).
        n = 0
        name = f'{day}_{self._tag}-{n:03d}.jsonl'
        while any(os.path.exists(os.path.join(self.root, name + gz)) for gz in ('', '.gz')):
            n += 1
            name = f'{day}_{self._tag}-{n:03d}.jsonl'
        self._file_name = name
        self._file_day = day
        self._file = open(os.path.join(self.root, self._file_name), 'a', encoding='utf-8')
        self._prev = _GENESIS
        self._lines = 0
        self._flushed_size, self._flushed_lines = 0, 0
        self._flushed_prev, self._pending = _GENESIS, (0, 0)
        self._flushed_withheld = self._withheld
        self._maintain()

    def _close(self) -> None:
        if self._file is None:
            return
        try:
            self._file.flush()
            self._mark_flushed()
        except OSError:
            # The day ends on a full disk: cut back and count, as a failed
            # batch does, instead of leaving lines it never wrote in the
            # manifest's count.
            self._abandon_file()
            return
        try:
            os.fsync(self._file.fileno())
        except OSError:
            pass
        self._file.close()
        self._file = None
        self._manifest(self._file_name, self._lines, self._prev)

    def close(self) -> None:
        """Write what is queued and close today's file (process exit)."""
        try:
            self.drain()
            with self._write_lock:
                self._close()
        except Exception:
            logger.warning('audit_trail: closing the trail failed', exc_info=True)

    # ── storage ───────────────────────────────────────────────────

    def _manifest(self, name: str, lines: int, last: str) -> None:
        """List a closed segment in manifest.jsonl.

        A segment is closed exactly when a full disk abandons it, so the disk
        may refuse its row or cut it short.  The row is kept and written with
        the next row, or after the next batch that reaches the disk, never
        dropped; and a row cut short is ended before the next one starts, so
        it cannot swallow the row after it (rv_d9ed, finding 2: the torn row
        and the next merged, and verify() missed a deleted segment)."""
        self._manifest_pending.append({'segment': name, 'lines': lines, 'last': last,
                                       'closed': _iso(self._clock())})
        self._write_manifest()

    def _write_manifest(self) -> None:
        """Write the manifest rows not yet on disk, each on a line of its own.
        A failure keeps them for the next try and says so in the log."""
        path = os.path.join(self.root, 'manifest.jsonl')
        text = ''.join(json.dumps(row) + '\n' for row in self._manifest_pending)
        try:
            try:
                with open(path, 'rb') as f:
                    f.seek(0, os.SEEK_END)
                    if f.tell():
                        f.seek(-1, os.SEEK_END)
                        if f.read(1) != b'\n':
                            text = '\n' + text
            except FileNotFoundError:
                pass
            with open(path, 'a', encoding='utf-8') as f:
                f.write(text)
        except OSError:
            logger.warning('audit_trail: the manifest could not list %d closed '
                           'segment(s); kept for the next write',
                           len(self._manifest_pending), exc_info=True)
            return
        self._manifest_pending.clear()

    def _segments(self) -> List[str]:
        try:
            names = os.listdir(self.root)
        except OSError:
            return []
        return sorted(n for n in names if n[:4].isdigit()
                      and (n.endswith('.jsonl') or n.endswith('.jsonl.gz')))

    def _maintain(self) -> None:
        """Gzip finished days, delete days past retention."""
        today = _day(self._clock())
        cutoff = (datetime.fromtimestamp(self._clock(), tz=timezone.utc)
                  - timedelta(days=self.retention_days)).strftime('%Y-%m-%d')
        for name in self._segments():
            path = os.path.join(self.root, name)
            if name[:10] < cutoff:
                try:
                    os.remove(path)
                except OSError:
                    logger.warning('audit_trail: %s is past retention but could not be '
                                   'removed', path, exc_info=True)
                continue
            if name.endswith('.jsonl') and name[:10] < today and name != self._file_name:
                try:
                    with open(path, 'rb') as src, gzip.open(path + '.gz', 'wb') as dst:
                        for block in iter(lambda: src.read(1 << 20), b''):
                            dst.write(block)
                    os.remove(path)
                except OSError:
                    logger.warning('audit_trail: %s could not be compressed', path,
                                   exc_info=True)

    def _used_bytes(self) -> int:
        total = 0
        for name in self._segments():
            try:
                total += os.path.getsize(os.path.join(self.root, name))
            except OSError:
                pass
        return total

    def _check_storage(self, now: float) -> None:
        if now - self._last_storage_check < STORAGE_CHECK_S and self._last_storage_check:
            return
        self._last_storage_check = now
        try:
            from core.platform_paths import free_bytes
            free = free_bytes(self.root)
        except Exception:
            free = None
        used = self._used_bytes()
        over = used >= self.max_bytes or (free is not None and free < self.min_free_bytes)
        if over != self._withheld:
            kind = STORAGE_LIMIT_KIND if over else STORAGE_OK_KIND
            why = ('text is withheld (metadata and checksums kept) until there is room'
                   if over else 'text is kept again')
            self._append(self._finish({'t': now, 'kind': kind, 'dir': 'sys',
                                       'extra': {'used_bytes': used, 'free_bytes': free,
                                                 'max_bytes': self.max_bytes,
                                                 'min_free_bytes': self.min_free_bytes,
                                                 'why': why}}))
            # Only once its line is handed to the file: a line that failed (a
            # full disk) must not leave every later line without text and
            # nothing saying why (rv_36d0, finding 3).  A line the disk
            # refuses later, at the flush, takes the switch back with it
            # (_abandon_file), and the next check writes it again.
            self._withheld = over
            logger.warning('audit_trail: %s (used %s bytes, free %s bytes)', why, used, free)

    # ── reading ───────────────────────────────────────────────────

    def _read_segment(self, name: str) -> Iterator[dict]:
        path = os.path.join(self.root, name)
        opener = gzip.open if name.endswith('.gz') else open
        try:
            with opener(path, 'rt', encoding='utf-8') as f:
                for raw in f:
                    raw = raw.strip()
                    if raw:
                        try:
                            yield json.loads(raw)
                        except ValueError:
                            yield {'kind': 'unreadable', 'raw': raw[:200]}
        except OSError:
            logger.warning('audit_trail: %s could not be read', path, exc_info=True)

    def read(self, *, agent_id=None, user_id=None, kinds: Iterable[str] = None,
             since: str = None, until: str = None, limit: int = 200) -> List[dict]:
        """Lines matching every filter given, newest first.  ``agent_id`` is
        one id or several (a goal's lines carry its prompt id, not its own).
        ``since`` / ``until``: an ISO date or time, or a datetime, compared
        as times by the one window rule (_window_text); a bad one raises
        ValueError.  They used to be compared as raw text, so a zoned since
        dropped lines at the very instant it named, and a date-only until
        dropped that whole day."""
        since = _window_text(since, until=False)
        until = _window_text(until, until=True)
        if self._thread is None:
            self.drain()
        elif self._file is not None:
            self.flush(2.0)
        kinds = set(kinds) if kinds else None
        if agent_id is not None:
            agent_id = ({str(a) for a in agent_id}
                        if isinstance(agent_id, (list, tuple, set, frozenset))
                        else {str(agent_id)})
        out: List[dict] = []
        for name in reversed(self._segments()):
            day = name[:10]
            if since and day < since[:10]:
                break
            if until and day > until[:10]:
                continue
            rows = [r for r in self._read_segment(name)
                    if (agent_id is None or str(r.get('agent_id')) in agent_id)
                    and (user_id is None or str(r.get('user_id')) == str(user_id))
                    and (kinds is None or r.get('kind') in kinds)
                    and (not since or str(r.get('ts', '')) >= since)
                    and (not until or str(r.get('ts', '')) <= until)]
            out.extend(reversed(rows))
        out.sort(key=lambda r: str(r.get('ts', '')), reverse=True)
        return out[:max(1, int(limit))]

    def verify(self) -> dict:
        """Recompute every line's hash and check the manifest.

        {'ok': bool, 'segments': [{segment, lines, ok, reason}], 'missing': [...]}"""
        if self._thread is None:
            self.drain()
        elif self._file is not None:
            self.flush(2.0)
        report = []
        present = set()
        for name in self._segments():
            present.add(name.replace('.gz', ''))
            prev, count, ok, reason = _GENESIS, 0, True, ''
            for row in self._read_segment(name):
                count += 1
                if row.get('prev') != prev or row.get('hash') != line_hash(prev, row):
                    ok, reason = False, f'line {count} does not match the line before it'
                    break
                prev = row['hash']
            report.append({'segment': name, 'lines': count, 'ok': ok, 'reason': reason})
        missing = []
        cutoff = (datetime.fromtimestamp(self._clock(), tz=timezone.utc)
                  - timedelta(days=self.retention_days)).strftime('%Y-%m-%d')
        try:
            with open(os.path.join(self.root, 'manifest.jsonl'), encoding='utf-8') as f:
                for raw in f:
                    try:
                        row = json.loads(raw)
                    except ValueError:
                        continue
                    seg = row.get('segment') or ''
                    if seg[:10] >= cutoff and seg not in present:
                        missing.append(seg)
        except OSError:
            pass
        return {'ok': all(r['ok'] for r in report) and not missing,
                'segments': report, 'missing': missing}


#: Conversation rows that are not something a person said or an agent wrote:
#: the vision loop's captions (record_interaction with prompt_id
#: 'vision_<channel>') and voice enrolment (base64 audio in the content).
_CAPTION_KINDS = {'vision_camera': 'video_caption', 'vision_screen': 'screen_caption'}
_ENROLMENT_PROMPT = 'biometric_enrollment'
# 'heard': words the open mic overheard that were not said to the agent
# (integrations.social.chat_messages.HEARD_ROLE): still a person's words
# coming in, on their own channel.
_DIRECTION = {'user': 'in', 'assistant': 'out', 'heard': 'in'}
_ACTOR = {'user': 'person', 'assistant': 'agent', 'heard': 'person'}


def record_conversation_row(row) -> None:
    """One audit line for one conversation-table row, whoever wrote it.

    Called by the table's insert listener (integrations.social.models), the
    one place every writer passes: the chat turn, channel adapters, and the
    vision loop's camera and screen captions."""
    role = getattr(row, 'role', '') or ''
    prompt_id = getattr(row, 'prompt_id', None)
    content = getattr(row, 'content', None)
    kind = _CAPTION_KINDS.get(str(prompt_id or ''))
    if kind and role == 'user':
        return  # the loop's own "describe what you see", not anyone's words
    fields = dict(direction=_DIRECTION.get(role, 'sys'),
                  user_id=getattr(row, 'user_id', None),
                  request_id=getattr(row, 'request_id', None),
                  actor=_ACTOR.get(role, 'system'),
                  channel=getattr(row, 'channel_type', None),
                  msg_id=getattr(row, 'msg_id', None),
                  device_id=getattr(row, 'device_id', None),
                  attachments=getattr(row, 'attachments', None) or None)
    if kind:
        record(kind, text=content, **fields)
    elif str(prompt_id or '') == _ENROLMENT_PROMPT:
        raw = (content or '').encode('utf-8', 'replace')
        record('audio', sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
               purpose='voice enrolment', **fields)
    else:
        record('message', text=content,
               agent_id=getattr(row, 'agent_id', None) or prompt_id, **fields)


def _person_role(user_id: str) -> Optional[dict]:
    """The account's kind, tier and admin flag, read from the users table."""
    from integrations.social.models import get_db, User
    db = get_db()
    try:
        user = db.query(User).filter(User.id == str(user_id)).first()
        if user is None:
            return None
        return {'kind': user.user_type, 'tier': user.role,
                'admin': bool(user.is_admin)}
    finally:
        db.close()


_trail: Optional[AuditTrail] = None
_trail_lock = threading.Lock()


def get_trail() -> AuditTrail:
    global _trail
    if _trail is None:
        with _trail_lock:
            if _trail is None:
                _trail = AuditTrail()
                atexit.register(_trail.close)
    return _trail


def current_request_id() -> Optional[str]:
    """The id of the turn this code runs in, for hooks that are not handed
    one: the turn's contextvar first (core.llm_outbound_logger.turn_request_id),
    then the request thread's own id (_get_request_id).  None outside a turn."""
    try:
        from core.llm_outbound_logger import _get_request_id, turn_request_id
        return turn_request_id() or _get_request_id() or None
    except Exception:
        return None


def record(kind: str, **fields) -> None:
    """Queue one audit line (see AuditTrail.record).  Safe on any hot path."""
    try:
        get_trail().record(kind, **fields)
    except Exception:
        logger.warning('audit_trail: record(%s) failed', kind, exc_info=True)


def bound(request_id) -> dict:
    """The ids a request was bound to (agent_id, user_id, ...), or {}.  Never raises."""
    try:
        return get_trail().bound(request_id)
    except Exception:
        return {}


def bind(request_id, **ids) -> None:
    """Attribute a request's later records (see AuditTrail.bind)."""
    try:
        get_trail().bind(request_id, **ids)
    except Exception:
        logger.warning('audit_trail: bind failed', exc_info=True)


__all__ = ['AuditTrail', 'EVENT_LOG_KIND', 'HOUSEKEEPING_KINDS', 'KINDS', 'VIEW_KINDS',
           'bind', 'get_trail', 'line_hash', 'record', 'record_conversation_row',
           'sha256_file']
