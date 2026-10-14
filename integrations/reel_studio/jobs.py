"""integrations.reel_studio.jobs -- one reel at a time on this desktop.

A reel is one job: ``start_job`` hands it out, ``finish_job`` records how it
ended.  Only one job runs on the machine at a time, whoever asks and from
whichever process (the desktop app, or the E2E CLI beside it): the render
runs one ffmpeg at a time and shares the GPU with the language models, and a
second reel would only make both slower.  ``start_job`` answers None
("busy") while another job holds the machine.

Each job is a record ``<reel_id>.json`` under
``get_data_dir()/reel_studio/jobs/``, written only by this module, atomically
(core.file_cache.atomic_json_write).  The machine is held by one claim file in
the same folder, made with an exclusive create, so two processes cannot both
take it.  A claim whose process is gone, or which is older than MAX_JOB_S
(a pid can be reused by an unrelated process), is stale and taken over.

When the desktop restarts in the middle of a reel, its record still says
'running'.  ``sweep_interrupted`` (run once per process by ``sweep_once``,
from the first register_reel_studio_tools) marks every such record
'interrupted' and tells its user once, on her own chat topic and never
through central's crossbar (decision U4(d)): INTERRUPTED_NOTICE.  It also
drops finished records older than RECORD_KEEP_S.

A process's own jobs are told apart from a dead one that had the same pid
by ``_ACTIVE``, the reel ids this process started and has not finished.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from core.file_cache import atomic_json_write
from integrations.reel_studio.contracts import REEL_STATUSES

logger = logging.getLogger(__name__)

JOB_SCHEMA = 'hart.reel.job.v1'
#: The file that holds the machine for the job it names.
CLAIM_NAME = 'active.claim'
#: No reel takes this long (analysis + a 90 s VLM budget + a 600 s render +
#: a 180 s QA); a claim older than this is stale whatever its pid says.
MAX_JOB_S = 3600.0
#: Finished records are kept this long (seconds since last written).
RECORD_KEEP_S = 30 * 24 * 3600.0
#: How a job may end: every reel_make status but 'busy', which is answered
#: before a job exists.
END_STATUSES = tuple(s for s in REEL_STATUSES if s != 'busy')
#: What she is told about a reel the desktop lost to a restart.
INTERRUPTED_NOTICE = "The desktop restarted while making your reel. Send 'try again'."

_LOCK = threading.Lock()
#: Reel ids this process started and has not finished.
_ACTIVE: set = set()
#: Whether this process has swept already (sweep_once).
_SWEPT = False


@dataclass
class JobRecord:
    reel_id: str
    user_id: str
    prompt_id: Optional[str]
    request_id: Optional[str]
    status: str
    pid: int
    started_at: float
    finished_at: Optional[float] = None
    detail: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── where ─────────────────────────────────────────────────────────────────

def jobs_dir() -> str:
    """``get_data_dir()/reel_studio/jobs`` (not created here)."""
    from core.platform_paths import get_data_dir
    return os.path.join(get_data_dir(), 'reel_studio', 'jobs')


def _record_path(reel_id: str) -> str:
    return os.path.join(jobs_dir(), reel_id + '.json')


def _claim_path() -> str:
    return os.path.join(jobs_dir(), CLAIM_NAME)


def _text_id(value) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _is_reel_id(value) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 64
            and all(c.isalnum() or c in '_-' for c in value))


# ── records ───────────────────────────────────────────────────────────────

def _write(job: JobRecord) -> None:
    atomic_json_write(_record_path(job.reel_id), dict(job.to_dict(), schema=JOB_SCHEMA))


def _read(path: str) -> Optional[JobRecord]:
    try:
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
        if doc.get('schema') != JOB_SCHEMA:
            raise ValueError('not a %s record' % JOB_SCHEMA)
        return JobRecord(reel_id=str(doc['reel_id']), user_id=str(doc['user_id']),
                         prompt_id=_text_id(doc.get('prompt_id')),
                         request_id=_text_id(doc.get('request_id')), status=str(doc['status']),
                         pid=int(doc['pid']), started_at=float(doc['started_at']),
                         finished_at=(float(doc['finished_at'])
                                      if doc.get('finished_at') is not None else None),
                         detail=str(doc.get('detail') or ''))
    except FileNotFoundError:
        return None
    except Exception as e:      # a damaged record is skipped, never fatal
        logger.warning('reel jobs: record %s unreadable (%s); skipped', path, e)
        return None


def reel_job(reel_id: str) -> Optional[JobRecord]:
    """The record of job ``reel_id``, or None."""
    if not _is_reel_id(reel_id):
        return None
    return _read(_record_path(reel_id))


def _records() -> List[JobRecord]:
    try:
        names = sorted(os.listdir(jobs_dir()))
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        if name.endswith('.json') and _is_reel_id(name[:-5]):
            job = _read(os.path.join(jobs_dir(), name))
            if job is not None:
                out.append(job)
    return out


# ── the claim ─────────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    # core.foreground's probe (psutil, else the OS); unknown reads as alive.
    from core.foreground import _pid_alive as alive
    return alive(int(pid))


def _alive(reel_id: str, pid: int, started_at: float) -> bool:
    """Whether the job's process may still be working on it."""
    if time.time() - started_at > MAX_JOB_S:
        return False
    if pid == os.getpid():
        return reel_id in _ACTIVE
    return _pid_alive(pid)


def _read_claim() -> Optional[Dict[str, Any]]:
    try:
        with open(_claim_path(), encoding='utf-8') as f:
            claim = json.load(f)
        return {'reel_id': str(claim['reel_id']), 'pid': int(claim['pid']),
                'started_at': float(claim['started_at'])}
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning('reel jobs: claim %s unreadable (%s); treated as stale', _claim_path(), e)
        return {'reel_id': '', 'pid': 0, 'started_at': 0.0}


def _take_claim(reel_id: str, now: float) -> bool:
    """Create the claim for ``reel_id``; False when another holds it."""
    os.makedirs(jobs_dir(), exist_ok=True)
    try:
        fd = os.open(_claim_path(), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOINHERIT', 0), 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump({'reel_id': reel_id, 'pid': os.getpid(), 'started_at': now}, f)
    return True


def _remove_claim() -> None:
    try:
        os.remove(_claim_path())
    except FileNotFoundError:
        logger.debug('reel jobs: the claim was already gone')


def _release_claim(reel_id: str) -> None:
    claim = _read_claim()
    if claim is not None and claim['reel_id'] == reel_id:
        _remove_claim()


def _drop_stale_claim() -> bool:
    """Remove a claim whose job is no longer alive (its record interrupted);
    False when a live job holds it."""
    claim = _read_claim()
    if claim is None:
        return True
    if claim['reel_id'] and _alive(claim['reel_id'], claim['pid'], claim['started_at']):
        return False
    job = reel_job(claim['reel_id']) if claim['reel_id'] else None
    if job is not None and job.status == 'running':
        _interrupt(job)
    _remove_claim()
    logger.warning('reel jobs: took over a stale claim (reel %s, pid %s)',
                   claim['reel_id'] or '?', claim['pid'])
    return True


# ── public ────────────────────────────────────────────────────────────────

def start_job(user_id, prompt_id, request_id) -> Optional[JobRecord]:
    """A new running job for this turn, or None when a reel is already being
    made on this machine.  A stale claim (see the module note) is taken
    over, and its job interrupted."""
    now = time.time()
    reel_id = uuid.uuid4().hex
    with _LOCK:
        if not _take_claim(reel_id, now):
            if not _drop_stale_claim() or not _take_claim(reel_id, now):
                return None
        job = JobRecord(reel_id=reel_id, user_id=str(user_id).strip(),
                        prompt_id=_text_id(prompt_id), request_id=_text_id(request_id),
                        status='running', pid=os.getpid(), started_at=now)
        try:
            _write(job)
        except BaseException:
            _release_claim(reel_id)
            raise
        _ACTIVE.add(reel_id)
        return job


def finish_job(reel_id: str, status: str, detail: str = '') -> None:
    """Record how job ``reel_id`` ended and free the machine.

    ``status`` is one of END_STATUSES (ValueError otherwise).  The machine is
    freed even when the record cannot be written (the write's OSError is
    raised after).  An unknown reel id frees nothing it does not hold.
    """
    if status not in END_STATUSES:
        raise ValueError('a job ends with one of %s, got %r' % (END_STATUSES, status))
    with _LOCK:
        try:
            job = reel_job(reel_id)
            if job is None:
                logger.warning('reel jobs: finish_job(%r) for no known job', reel_id)
                return
            job.status, job.detail, job.finished_at = status, str(detail or '')[:500], time.time()
            _write(job)
        finally:
            _ACTIVE.discard(reel_id)
            if _is_reel_id(reel_id):
                _release_claim(reel_id)


def active_job() -> Optional[JobRecord]:
    """The job holding the machine, if its process may still be working."""
    with _LOCK:
        claim = _read_claim()
        if claim is None or not claim['reel_id']:
            return None
        if not _alive(claim['reel_id'], claim['pid'], claim['started_at']):
            return None
        return reel_job(claim['reel_id'])


def _interrupt(job: JobRecord) -> None:
    """Mark a dead job interrupted and tell its user once (caller holds _LOCK)."""
    job.status, job.finished_at = 'interrupted', time.time()
    job.detail = job.detail or 'the process making it stopped'
    try:
        _write(job)
    except OSError as e:
        logger.error('reel jobs: could not mark %s interrupted: %s', job.reel_id, e)
        return
    try:
        from core.peer_link.crossbar_publish import publish_agent_message
        sent = publish_agent_message(text=INTERRUPTED_NOTICE, user_id=job.user_id,
                                     request_id=job.request_id, prompt_id=job.prompt_id,
                                     skip_crossbar=True)
    except Exception as e:
        sent = False
        logger.warning('reel jobs: interrupted notice for %s failed: %s', job.reel_id, e)
    logger.warning('reel jobs: reel %s for user %s was interrupted; notice %s', job.reel_id,
                   job.user_id, 'sent' if sent else 'NOT sent')


def sweep_interrupted() -> List[JobRecord]:
    """Mark every 'running' job whose process is gone as 'interrupted', tell
    each user once, and prune old finished records.  Returns the jobs
    marked.  Creates nothing when there are no records."""
    swept: List[JobRecord] = []
    now = time.time()
    with _LOCK:
        for job in _records():
            if job.status == 'running':
                if not _alive(job.reel_id, job.pid, job.started_at):
                    _interrupt(job)
                    swept.append(job)
                    _release_claim(job.reel_id)
                continue
            path = _record_path(job.reel_id)
            try:
                if now - os.path.getmtime(path) > RECORD_KEEP_S:
                    os.remove(path)
            except OSError as e:
                logger.debug('reel jobs: could not prune %s: %s', path, e)
    return swept


def sweep_once() -> List[JobRecord]:
    """sweep_interrupted, the first time it is asked in this process."""
    global _SWEPT
    with _LOCK:
        if _SWEPT:
            return []
        _SWEPT = True
    try:
        return sweep_interrupted()
    except Exception as e:       # never stops an agent being built
        logger.warning('reel jobs: sweep failed: %s', e)
        return []


__all__ = ['JOB_SCHEMA', 'CLAIM_NAME', 'MAX_JOB_S', 'RECORD_KEEP_S', 'END_STATUSES',
           'INTERRUPTED_NOTICE', 'JobRecord', 'jobs_dir', 'reel_job', 'start_job', 'finish_job',
           'active_job', 'sweep_interrupted', 'sweep_once']
