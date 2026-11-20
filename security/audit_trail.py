"""security.audit_trail -- THE record of what every agent received and produced.

Owner, 2026-11-20: "work on auditability of all modalities in HARTOS, every
message, transcription, audio, video, file uploaded, image caption, screen
captioned, video captioned per agent via nunba ui", "for using HARTOS in a
financial institution", "logging the role etc for each action without
compromise in speed or compute", "storage space shd be respected", "do the
best minimalistically as a framework".

ONE recorder, called from the few places every modality already passes
(the conversation table's writer, the one upload store, the image describer,
speech-to-text, speech out, the LLM call log).  Nothing else keeps a trail.

Speed: ``record()`` builds a small dict and puts it on a bounded queue.  It
never touches the disk, the database or a lock held across I/O.  One
background thread does the rest: fills in who acted and in what role, hides
secrets (security.secret_redactor.redact_secrets, the one rule), links each
line to the one before it (security.immutable_audit_log._compute_hash, the
one hash rule) and appends it to today's file.  A full queue never blocks a
turn: the record is counted and the next line written is a ``gap`` line
saying how many were lost, so a missing record is never silent.

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
hash, so a whole file deleted inside the retention period is caught too.

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
KINDS = ('message', 'transcript', 'audio', 'video', 'file', 'image_caption',
         'screen_caption', 'video_caption', 'llm_call', 'speech_out',
         'action', 'gap', 'storage_limit', 'storage_ok')
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


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec='milliseconds')


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
        self._lost = 0
        self._lost_lock = threading.Lock()
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
        if batch or self._lost:
            self._write_batch(batch)
        return len(batch)

    def _write_batch(self, batch: List) -> None:
        with self._write_lock:
            self._write_batch_locked(batch)

    def _write_batch_locked(self, batch: List) -> None:
        events = [r for r in batch if not isinstance(r, threading.Event)]
        done = [r for r in batch if isinstance(r, threading.Event)]
        with self._lost_lock:
            lost, self._lost = self._lost, 0
        if lost:
            events.insert(0, {'t': self._clock(), 'kind': 'gap', 'dir': 'sys',
                              'extra': {'lost': lost,
                                        'why': 'the queue was full; these records were not kept'}})
        if events:
            self._check_storage(events[-1]['t'])
            for rec in events:
                self._append(self._finish(rec))
            if self._file is not None:
                self._file.flush()
                now = self._clock()
                if now - self._last_fsync >= 1.0:
                    try:
                        os.fsync(self._file.fileno())
                    except OSError:
                        pass
                    self._last_fsync = now
        for ev in done:
            ev.set()

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
        """Attribution, roles, media checksums, secret redaction, the text cap."""
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
        rid = rec.get('request_id')
        if rid:
            with self._ctx_lock:
                ctx = dict(self._contexts.get(str(rid)) or {})
            for key, val in ctx.items():
                rec.setdefault(key, val)
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

    def _open(self, day: str) -> None:
        self._close()
        os.makedirs(self.root, exist_ok=True)
        self._file_name = f'{day}_{self._tag}.jsonl'
        self._file_day = day
        self._file = open(os.path.join(self.root, self._file_name), 'a', encoding='utf-8')
        self._prev = _GENESIS
        self._lines = 0
        self._maintain()

    def _close(self) -> None:
        if self._file is None:
            return
        try:
            self._file.flush()
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
        path = os.path.join(self.root, 'manifest.jsonl')
        row = {'segment': name, 'lines': lines, 'last': last,
               'closed': _iso(self._clock())}
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row) + '\n')

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
            self._withheld = over
            kind = 'storage_limit' if over else 'storage_ok'
            why = ('text is withheld (metadata and checksums kept) until there is room'
                   if over else 'text is kept again')
            self._append(self._finish({'t': now, 'kind': kind, 'dir': 'sys',
                                       'extra': {'used_bytes': used, 'free_bytes': free,
                                                 'max_bytes': self.max_bytes,
                                                 'min_free_bytes': self.min_free_bytes,
                                                 'why': why}}))
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
        one id or several (a goal's lines carry its prompt id, not its own)."""
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
_DIRECTION = {'user': 'in', 'assistant': 'out'}
_ACTOR = {'user': 'person', 'assistant': 'agent'}


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


def bind(request_id, **ids) -> None:
    """Attribute a request's later records (see AuditTrail.bind)."""
    try:
        get_trail().bind(request_id, **ids)
    except Exception:
        logger.warning('audit_trail: bind failed', exc_info=True)


__all__ = ['AuditTrail', 'KINDS', 'bind', 'get_trail', 'line_hash', 'record',
           'record_conversation_row', 'sha256_file']
