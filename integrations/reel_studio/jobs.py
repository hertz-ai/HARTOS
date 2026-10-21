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
take it.  The job's process beats (``beat``) at every step of the reel,
which touches the claim.  A claim whose process is gone, or which has not
beaten for MAX_JOB_S (a pid can be reused by an unrelated process), is stale
and taken over; a slow reel that still beats never is.  A claim that cannot
be read yet is one its maker is still writing, held for CLAIM_WRITE_GRACE_S.
A stale claim is moved aside before it is removed and compared with the one
judged stale, so a live claim another process made in between is put back,
never removed; only the process that removed the stale claim ends its job.

Each job works in its own dir, ``work_dir(reel_id)`` under
``get_data_dir()/reel_studio/work/``.  It holds the pass-1 mezzanines and
frames of her clients' faces, so it never outlives its job: the pipeline
removes it when the reel ends, and this module removes it when a job ends
without finishing -- interrupted, or taken over as stale.

When the desktop restarts in the middle of a reel, its record still says
'running'.  ``sweep_interrupted`` (run once per process by ``sweep_once``,
from the first register_reel_studio_tools) marks every such record
'interrupted' and tells its user once, on her own chat topic and never
through central's crossbar (decision U4(d)): INTERRUPTED_NOTICE.  It also
removes every work dir no live job owns, and drops finished records older
than RECORD_KEEP_S.

A process's own jobs are told apart from a dead one that had the same pid
by ``_ACTIVE``, the reel ids this process started and has not finished.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Mapping, Optional

from core.file_cache import atomic_json_write
from integrations.reel_studio.contracts import REEL_STATUSES

logger = logging.getLogger(__name__)

JOB_SCHEMA = 'hart.reel.job.v1'
#: The file that holds the machine for the job it names.
CLAIM_NAME = 'active.claim'
#: The longest a job goes without a beat.  It beats at every step (each
#: clip's analysis, the VLM's 90 s budget, the render's 600 s bound, the QA's
#: 180 s), so a claim this silent is stale whatever its pid says.
MAX_JOB_S = 3600.0
#: How long a claim that cannot be read yet is held: its maker creates it,
#: then writes it, microseconds apart.
CLAIM_WRITE_GRACE_S = 30.0
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


def work_root() -> str:
    """``get_data_dir()/reel_studio/work``: each job's work dir lives in it
    (not created here)."""
    from core.platform_paths import get_data_dir
    return os.path.join(get_data_dir(), 'reel_studio', 'work')


def work_dir(reel_id: str) -> str:
    """Job ``reel_id``'s own work dir (not created here)."""
    if not _is_reel_id(reel_id):
        raise ValueError('not a reel id: %r' % (reel_id,))
    return os.path.join(work_root(), reel_id)


def remove_work_dir(path: str) -> None:
    """Remove a job's work dir and everything in it; never raises.

    It must not stay behind: it holds frames of her clients' faces.
    shutil.rmtree first; it works in the shipped interpreter (measured
    2026-10-14: Nunba's python-embed 3.12.6, built and installed).  A dev
    interpreter whose frozen ``os`` is older than its Lib cannot run it
    (measured on this repo's venv: 3.12.3's DLL with an August 2024
    shutil.py, AttributeError for os._walk_symlinks_as_files before anything
    is removed), and a file another process still holds open stops it on
    Windows, so the same bottom-up walk is then done here, file by file.  A
    link (symlink or junction) is removed itself, never what it points at.
    """
    if _is_link(path):
        _remove_link(path)
        return
    try:
        shutil.rmtree(path)
        return
    except FileNotFoundError:
        return
    except AttributeError as e:
        logger.debug('shutil.rmtree unusable on this interpreter (%s); removing by hand', e)
    except OSError as e:
        logger.warning('work dir %s not removed by rmtree (%s); trying file by file', path, e)
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.remove(os.path.join(root, name))
            except OSError as e:
                logger.warning('could not remove %s: %s', os.path.join(root, name), e)
        for name in dirs:
            sub = os.path.join(root, name)
            if _is_link(sub):
                _remove_link(sub)
                continue
            try:
                os.rmdir(sub)
            except OSError as e:
                logger.warning('could not remove %s: %s', sub, e)
    if os.path.isdir(path):
        try:
            os.rmdir(path)
        except OSError as e:
            logger.warning('work dir %s left behind: %s', path, e)


def _is_link(path: str) -> bool:
    return os.path.islink(path) or getattr(os.path, 'isjunction', lambda _p: False)(path)


def _remove_link(path: str) -> None:
    """Remove a symlink or junction itself, never what it points at."""
    try:
        if os.path.islink(path):
            os.unlink(path)
        else:
            os.rmdir(path)          # a junction: removes the link only
    except OSError as e:
        logger.warning('link %s left behind: %s', path, e)


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
        if not _is_reel_id(doc.get('reel_id')):       # it names the job's work dir
            raise ValueError('reel_id %r is not a reel id' % (doc.get('reel_id'),))
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


def _alive(reel_id: str, pid: int, since: float) -> bool:
    """Whether the job's process may still be working on it; ``since`` is
    when it last beat (its start when it never did)."""
    if pid == os.getpid():
        return reel_id in _ACTIVE
    if time.time() - since > MAX_JOB_S:
        return False
    return _pid_alive(pid)


def _read_claim(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The claim at ``path`` (default: the machine's): reel_id, pid,
    started_at and beat_at (when it was last touched); None when there is
    none.  One that cannot be read has reel_id ''."""
    path = path or _claim_path()
    try:
        with open(path, encoding='utf-8') as f:
            beat_at = os.fstat(f.fileno()).st_mtime
            text = f.read()
    except FileNotFoundError:
        return None
    try:
        claim = json.loads(text)
        return {'reel_id': str(claim['reel_id']), 'pid': int(claim['pid']),
                'started_at': float(claim['started_at']), 'beat_at': beat_at}
    except Exception as e:
        logger.info('reel jobs: claim %s unreadable (%s)', path, e)
        return {'reel_id': '', 'pid': 0, 'started_at': 0.0, 'beat_at': beat_at}


def _since(claim: Mapping[str, Any]) -> float:
    return max(claim['started_at'], claim['beat_at'])


def _take_claim(reel_id: str, now: float) -> bool:
    """Create the claim for ``reel_id``; False when another holds it.  A
    claim that cannot be written (the disk fills) is removed before the
    error is raised: left empty, it would read as one still being written
    and hold the machine for CLAIM_WRITE_GRACE_S."""
    os.makedirs(jobs_dir(), exist_ok=True)
    try:
        fd = os.open(_claim_path(), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOINHERIT', 0), 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'reel_id': reel_id, 'pid': os.getpid(), 'started_at': now}, f)
    except BaseException:
        _unlink(_claim_path())
        raise
    return True


def _remove_claim_if(seen: Mapping[str, Any]) -> bool:
    """Remove the claim if it is still the one ``seen``; True when it is gone.

    The claim is moved aside first (an atomic rename) and only then compared
    with ``seen``, so a claim another process made after ``seen`` was read
    -- it took the same stale claim over first -- is put back, not removed,
    and False is returned.  A claim held open by the process writing it
    cannot be moved on Windows: also False.
    """
    path = _claim_path()
    aside = '%s.%s.aside' % (path, uuid.uuid4().hex)
    try:
        os.rename(path, aside)
    except FileNotFoundError:
        return True
    except OSError as e:
        logger.info('reel jobs: claim not moved aside (%s); its holder keeps it', e)
        return False
    if _read_claim(aside) == dict(seen):
        _unlink(aside)
        return True
    logger.warning('reel jobs: the claim changed while it was being taken over; put back')
    try:
        if os.name == 'nt':
            os.rename(aside, path)          # refuses an existing target
        else:
            os.link(aside, path)            # refuses an existing target
            _unlink(aside)
    except FileExistsError:
        logger.warning('reel jobs: a newer claim stands; the one moved aside is dropped')
        _unlink(aside)
    except OSError as e:
        logger.error('reel jobs: claim %s could not be put back: %s', aside, e)
    return False


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        logger.debug('reel jobs: %s was already gone', path)
    except OSError as e:
        logger.warning('reel jobs: could not remove %s: %s', path, e)


def _release_claim(reel_id: str) -> None:
    claim = _read_claim()
    if claim is not None and claim['reel_id'] == reel_id:
        _remove_claim_if(claim)


def _drop_stale_claim() -> bool:
    """Take a stale claim away (see the module note) and end its job: its
    work dir removed, its record interrupted when it was still running.
    False when the claim is live, or is still being written."""
    claim = _read_claim()
    if claim is None:
        return True
    if not claim['reel_id']:
        if time.time() - claim['beat_at'] < CLAIM_WRITE_GRACE_S:
            return False
    elif _alive(claim['reel_id'], claim['pid'], _since(claim)):
        return False
    if not _remove_claim_if(claim):
        return False
    logger.warning('reel jobs: took over a stale claim (reel %s, pid %s)',
                   claim['reel_id'] or '?', claim['pid'])
    if claim['reel_id'] and _is_reel_id(claim['reel_id']):
        job = reel_job(claim['reel_id'])
        if job is not None and job.status == 'running':
            _interrupt(job)
        else:
            remove_work_dir(work_dir(claim['reel_id']))
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
        if not _alive(claim['reel_id'], claim['pid'], _since(claim)):
            return None
        return reel_job(claim['reel_id'])


def beat(reel_id: str) -> None:
    """Job ``reel_id`` is still being worked on: touch its claim, so
    MAX_JOB_S counts from now (the pipeline beats at every step of a reel).
    Only a job this process started and has not finished beats; anything
    else is a no-op.  Never raises."""
    with _LOCK:
        if reel_id not in _ACTIVE:
            return
        claim = _read_claim()
        if claim is None or claim['reel_id'] != reel_id:
            return
        try:
            os.utime(_claim_path(), None)
        except OSError as e:
            logger.warning('reel jobs: reel %s could not beat: %s', reel_id, e)


def _interrupt(job: JobRecord) -> None:
    """End a dead job: remove its work dir, mark it interrupted and tell its
    user once (caller holds _LOCK).  The work dir goes first and whatever
    else fails: it holds frames of her clients' faces."""
    remove_work_dir(work_dir(job.reel_id))
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


def _job_alive(job: Optional[JobRecord], claim: Optional[Mapping[str, Any]]) -> bool:
    """Whether ``job`` is running and its process may still be working on
    it; it last beat when the machine's ``claim`` (if it is the job's) was
    touched, else it never did."""
    if job is None or job.status != 'running':
        return False
    since = _since(claim) if claim and claim['reel_id'] == job.reel_id else job.started_at
    return _alive(job.reel_id, job.pid, since)


def _sweep_work_dirs() -> None:
    """Remove everything under work_root() that no live job owns (caller
    holds _LOCK).  The dirs are listed before each owner's record is read:
    a job's record is written before its dir is made, so a dir listed here
    belongs to a record that already exists."""
    root = work_root()
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return
    claim = _read_claim()
    for name in names:
        if _is_reel_id(name) and _job_alive(reel_job(name), claim):
            continue
        path = os.path.join(root, name)
        logger.info('reel jobs: removing %s, which no live job owns', path)
        if os.path.isdir(path) or _is_link(path):
            remove_work_dir(path)
        else:
            _unlink(path)


def sweep_interrupted() -> List[JobRecord]:
    """Mark every 'running' job whose process is gone as 'interrupted' (its
    work dir removed), tell each user once, remove every work dir no live
    job owns, and prune old finished records.  Returns the jobs marked.
    Creates nothing when there are no records."""
    swept: List[JobRecord] = []
    now = time.time()
    with _LOCK:
        claim = _read_claim()
        for job in _records():
            if job.status == 'running':
                if not _job_alive(job, claim):
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
        _sweep_work_dirs()
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


__all__ = ['JOB_SCHEMA', 'CLAIM_NAME', 'MAX_JOB_S', 'CLAIM_WRITE_GRACE_S', 'RECORD_KEEP_S',
           'END_STATUSES', 'INTERRUPTED_NOTICE', 'JobRecord', 'jobs_dir', 'work_root', 'work_dir',
           'remove_work_dir', 'reel_job', 'start_job', 'finish_job', 'active_job', 'beat',
           'sweep_interrupted', 'sweep_once']
