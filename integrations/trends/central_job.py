"""integrations.trends.central_job: central's daily trending-songs run,
driven from the agent daemon's tick (owner ruling U6).

``maybe_tick()`` is called on every daemon tick, before the daemon's "no
active goals" stop: the list serves every desktop whether or not central
has goals of its own.  It runs no thread and no LLM:

* off a central node it returns at once ('skip'), and between checks it
  only compares two numbers;
* on central, when the run is due, it starts the worker as a child process
  (``HEVOLVE_TRENDS_PYTHON -I ig_worker.py <spec> <out>``) and returns
  'started'; the research account's credentials go in the child's
  environment only, never in argv, the spec or a log line;
* later ticks only ``poll()`` the child ('running'); when it exits, its
  batch is ranked (trending_songs.rank_batch, through igkit.swipe), merged
  with yesterday's list and published ('published'), or the run is
  recorded as failed ('failed') and yesterday's list is kept, marked stale;
* after TIMEOUT_S the child is killed ('timeout').  The worker stops itself
  earlier (its spec's deadline_s), so this is the backstop.

Due means: the node is central (security.key_delegation.get_node_tier),
the research credentials are set (security.secrets_manager.get_secret),
the trends interpreter exists, ``trends/watchlist.json`` names at least one
account, nothing is frozen, and ``state.json``'s next_run_at has passed
(set a day after the last good run, plus up to JITTER_S).

Failures (goals-trends report 2.5):
* challenge / login: frozen for FREEZE_S (doubling on each freeze in a row,
  at most MAX_FREEZE_S), and the node's owner is told (NotificationService)
  that the research account needs a person;
* throttled / feedback: cooled down COOLDOWN_S (doubling, at most
  MAX_COOLDOWN_S);
* anything else (a partial run, a dead or killed worker, bad output, no
  instagrapi): retried after RETRY_S, doubling, at most MAX_RETRY_S.
A restart while the child runs leaves ``state.json`` saying so; the next
process adopts the child's batch when it is written, or records the run as
interrupted once TIMEOUT_S has passed.

Files (get_data_dir()/trends/): watchlist.json (kept by hand: {"region",
"niche", "accounts": [handle | {"handle", "pk"}]}), state.json,
.ig_session.json (the worker's, 0600), worker.log, run/ (the current
spec and batch, removed after each run), songs/ (trending_songs).
"""
import json
import logging
import os
import random
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.file_cache import atomic_json_write
from integrations.trends import sources as _sources
from integrations.trends import trending_songs as ts

logger = logging.getLogger(__name__)

#: The child is killed after this long; the worker's own deadline is
#: WORKER_DEADLINE_S, so it normally writes its batch first.
TIMEOUT_S = 900.0
WORKER_DEADLINE_S = 780.0
#: A good run is next due a day later, plus up to JITTER_S (a jittered hour).
DAY_S = 86400.0
JITTER_S = 3600.0
#: How often a node that is not ready (or a run that is not due) is looked
#: at again, and how often a child an earlier process started is.
CHECK_S = 600.0
ORPHAN_CHECK_S = 60.0
FREEZE_S = 48 * 3600.0
MAX_FREEZE_S = 7 * DAY_S
COOLDOWN_S = 6 * 3600.0
MAX_COOLDOWN_S = 24 * 3600.0
RETRY_S = 3600.0
MAX_RETRY_S = 12 * 3600.0
#: Seconds each private request waits (instagrapi's delay_range).
DELAY_RANGE = (3, 8)
FREEZE_CODES = ('challenge', 'login')
COOLDOWN_CODES = ('throttled', 'feedback')
#: The notification type the owner gets when the run is frozen.
PAUSED_NOTIFICATION = 'trends_paused'

USERNAME_SECRET = 'HEVOLVE_IG_RESEARCH_USERNAME'
PASSWORD_SECRET = 'HEVOLVE_IG_RESEARCH_PASSWORD'
PYTHON_ENV = 'HEVOLVE_TRENDS_PYTHON'
WORKER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ig_worker.py')
#: All the child's environment holds besides the credentials: what an
#: interpreter needs to start and reach the network.  Nothing else of the
#: daemon's environment (keys, tokens, HARTOS settings) reaches it.
_CHILD_ENV_KEYS = ('PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'HOME', 'USERPROFILE', 'LANG',
                   'LC_ALL', 'TMPDIR', 'TEMP', 'TMP', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
                   'REQUESTS_CA_BUNDLE', 'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY')

_lock = threading.Lock()
#: The child this process started: {'proc', 'pid', 'started_at', 'spec',
#: 'out', 'region', 'niche'}, or None.
_run: Optional[Dict[str, Any]] = None
#: The tick before which a skip is not re-examined (a cheap no-op).
_next_check_at = 0.0
_last_skip: Optional[str] = None


def maybe_tick(*, now: Optional[float] = None) -> str:
    """One daemon tick's worth of the run: 'skip', 'started', 'running',
    'published', 'failed' or 'timeout'.  Never raises and never blocks: a
    second caller while one is inside gets 'running'."""
    now = time.time() if now is None else float(now)
    if not _lock.acquire(blocking=False):
        return 'running'
    try:
        return _tick(now)
    except Exception as e:
        logger.warning("trending songs: tick failed: %s", e, exc_info=True)
        return 'failed'
    finally:
        _lock.release()


def _tick(now: float) -> str:
    global _next_check_at
    if _run is not None:
        return _poll(now)
    if now < _next_check_at:
        return 'skip'
    _next_check_at = now + CHECK_S
    why = _not_ready()
    if why:
        _note_skip(why)
        return 'skip'
    state = _load_state()
    running = state.get('running')
    if isinstance(running, dict):
        return _adopt(state, running, now)
    if not _due(state, now):
        return 'skip'
    return _start(state, now)


# ── readiness ─────────────────────────────────────────────────────────────

def _not_ready() -> Optional[str]:
    try:
        from security.key_delegation import get_node_tier
        if get_node_tier() != 'central':
            return 'not_central'
    except Exception as e:
        logger.debug("trending songs: node tier unreadable (%s)", e)
        return 'not_central'
    username, password = _credentials()
    if not username or not password:
        return 'no_credentials'
    if not _python():
        return 'no_interpreter'
    if not _watchlist()['accounts']:
        return 'no_watchlist'
    return None


def _note_skip(why: str) -> None:
    """Say once why a central node is not running the job (a desktop, which
    never runs it, says nothing)."""
    global _last_skip
    if why != _last_skip and why != 'not_central':
        logger.info("trending songs: not running on this central node (%s)", why)
    _last_skip = why


def _credentials():
    from security.secrets_manager import get_secret
    username = (get_secret(USERNAME_SECRET, '') or '').strip()
    password = get_secret(PASSWORD_SECRET, '') or ''
    return username, password


def _python() -> Optional[str]:
    path = (os.environ.get(PYTHON_ENV) or '').strip()
    return path if path and os.path.isfile(path) else None


def _watchlist() -> Dict[str, Any]:
    """trends/watchlist.json: ``{'region', 'niche', 'accounts'}``, the
    accounts cleaned by sources.clean_accounts; no accounts when it is
    missing or unreadable."""
    try:
        with open(os.path.join(ts.trends_dir(), 'watchlist.json'), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return {'region': None, 'niche': None, 'accounts': []}
    return {'region': data.get('region'), 'niche': data.get('niche'),
            'accounts': _sources.clean_accounts(data.get('accounts'))}


# ── state ─────────────────────────────────────────────────────────────────

def _state_path() -> str:
    return os.path.join(ts.trends_dir(), 'state.json')


def _load_state() -> Dict[str, Any]:
    try:
        with open(_state_path(), encoding='utf-8') as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_state(state: Dict[str, Any]) -> None:
    atomic_json_write(_state_path(), state)


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _due(state: Dict[str, Any], now: float) -> bool:
    return _number(state.get('frozen_until')) <= now and _number(state.get('next_run_at')) <= now


# ── one run ───────────────────────────────────────────────────────────────

def _start(state: Dict[str, Any], now: float) -> str:
    global _run
    python = _python()
    if not python:
        return 'skip'
    watch = _watchlist()
    pks = state.get('pks') if isinstance(state.get('pks'), dict) else {}
    accounts = [{'handle': a['handle'],
                 'pk': a['pk'] or _sources.clean_id(pks.get(a['handle']))}
                for a in watch['accounts']]
    folder = ts.trends_dir()
    run_dir = os.path.join(folder, 'run')
    _clear(run_dir)
    stamp = '%d' % int(now)
    spec_path = os.path.join(run_dir, 'spec-%s.json' % stamp)
    out_path = os.path.join(run_dir, 'batch-%s.json' % stamp)
    spec = {'accounts': accounts, 'budget': _sources.DEFAULT_BUDGET,
            'known_song_keys': ts.known_song_keys(ts.read_latest()),
            'session_path': os.path.join(folder, '.ig_session.json'),
            'delay_range': list(DELAY_RANGE), 'deadline_s': WORKER_DEADLINE_S}
    atomic_json_write(spec_path, spec)
    try:
        proc = _spawn([python, '-I', WORKER_PATH, spec_path, out_path], folder)
    except OSError as e:
        _remove(spec_path)
        return _failed(state, now, 'spawn_failed', str(e)[:200])
    _run = {'proc': proc, 'pid': proc.pid, 'started_at': now, 'spec': spec_path,
            'out': out_path, 'region': watch['region'], 'niche': watch['niche']}
    state['running'] = {k: _run[k] for k in ('pid', 'started_at', 'spec', 'out', 'region',
                                              'niche')}
    _save_state(state)
    logger.info("trending songs: worker started (pid %s, %d account(s))", proc.pid,
                len(accounts))
    return 'started'


def _spawn(argv, cwd: str):
    """Start the worker with only _CHILD_ENV_KEYS and the credentials in its
    environment, stdin closed, output to trends/worker.log."""
    username, password = _credentials()
    env = {k: os.environ[k] for k in _CHILD_ENV_KEYS if k in os.environ}
    env[USERNAME_SECRET] = username
    env[PASSWORD_SECRET] = password
    from core.subprocess_safe import no_window_kwargs
    log = open(os.path.join(cwd, 'worker.log'), 'wb')
    try:
        kwargs = dict(env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
                      stderr=subprocess.STDOUT, close_fds=True, **no_window_kwargs())
        if os.name == 'posix':
            # Its own session: a signal meant for the daemon's terminal or
            # process group never reaches the worker mid-run.
            kwargs['start_new_session'] = True
        return subprocess.Popen(argv, **kwargs)
    finally:
        log.close()


def _poll(now: float) -> str:
    run = _run
    rc = run['proc'].poll()
    if rc is not None:
        return _finish(_load_state(), run, rc, now)
    if now - run['started_at'] < TIMEOUT_S:
        return 'running'
    try:
        run['proc'].kill()
        run['proc'].wait(timeout=10)
    except Exception as e:
        logger.warning("trending songs: could not kill worker %s: %s", run.get('pid'), e)
    state = _load_state()
    _end_run(state, run)
    return _failed(state, now, 'timeout', 'killed after %d s' % TIMEOUT_S, outcome='timeout')


def _adopt(state: Dict[str, Any], running: Dict[str, Any], now: float) -> str:
    """A child an earlier process started: use its batch once written, else
    wait until TIMEOUT_S after its start, then record it interrupted."""
    global _next_check_at
    run = {key: running.get(key) for key in ('pid', 'spec', 'out', 'region', 'niche')}
    if not isinstance(run['out'], str) or not run['out']:
        return _finish(state, run, None, now, missing='interrupted')
    if _read_batch(run['out']) is not None:
        return _finish(state, run, None, now, missing='interrupted')
    if now < _number(running.get('started_at')) + TIMEOUT_S:
        _next_check_at = now + ORPHAN_CHECK_S
        return 'running'
    return _finish(state, run, None, now, missing='interrupted')


def _read_batch(path: Any) -> Optional[dict]:
    if not isinstance(path, str):
        return None
    try:
        with open(path, encoding='utf-8') as fh:
            batch = json.load(fh)
    except (OSError, ValueError):
        return None
    if (not isinstance(batch, dict) or batch.get('schema') != _sources.BATCH_SCHEMA
            or not isinstance(batch.get('accounts'), list)):
        return None
    return batch


def _finish(state: Dict[str, Any], run: Dict[str, Any], rc: Optional[int], now: float, *,
            missing: Optional[str] = None) -> str:
    """The child is done: rank and publish its batch, or record why not."""
    batch = _read_batch(run.get('out'))
    _end_run(state, run)
    if batch is None:
        error = missing or ('bad_output' if rc == 0 else 'worker_failed')
        return _failed(state, now, error, 'exit %s' % rc)
    _remember_pks(state, batch)
    error = batch.get('error')
    detail = batch.get('error_detail') if isinstance(batch.get('error_detail'), str) else None
    if error in FREEZE_CODES:
        return _freeze(state, now, error, detail)
    if error in COOLDOWN_CODES:
        return _cool_down(state, now, error, detail)
    if error:
        return _failed(state, now, str(error)[:40], detail)
    previous = ts.read_latest()
    rank = ts.rank_batch(batch, now=now, previous=previous)
    doc = ts.merge_days(rank, previous, today=_day(now), generated_at=_iso(now),
                        region=run.get('region'), niche=run.get('niche'))
    reason = ts.why_not_published(doc)
    if reason or not ts.publish(doc):
        return _failed(state, now, reason or 'not_written',
                       '%s of %s accounts answered' % (doc['accounts_ok'],
                                                       doc['accounts_total']))
    state.update(last_ok_at=now, next_run_at=now + DAY_S + random.uniform(0.0, JITTER_S),
                 consecutive_failures=0, freezes=0, cooldowns=0)
    state.pop('frozen_until', None)
    state['last_run'] = {'outcome': 'published', 'error': None, 'at': now,
                         'songs': len(doc['songs']), 'accounts_ok': doc['accounts_ok'],
                         'accounts_total': doc['accounts_total'],
                         'requests': batch.get('requests')}
    _save_state(state)
    return 'published'


def _end_run(state: Dict[str, Any], run: Dict[str, Any]) -> None:
    global _run
    _run = None
    state['running'] = None
    for key in ('spec', 'out'):
        if isinstance(run.get(key), str):
            _remove(run[key])


def _remember_pks(state: Dict[str, Any], batch: dict) -> None:
    """Cache each answered account's id, so the next run skips its lookup."""
    pks = state.get('pks') if isinstance(state.get('pks'), dict) else {}
    for row in batch['accounts']:
        if not isinstance(row, dict):
            continue
        handle, pk = row.get('handle'), _sources.clean_id(row.get('pk'))
        if isinstance(handle, str) and _sources.HANDLE_RE.fullmatch(handle) and pk:
            pks[handle] = pk
    state['pks'] = dict(list(pks.items())[-200:])


def _record(state: Dict[str, Any], now: float, outcome: str, error: str,
            detail: Optional[str]) -> None:
    state['consecutive_failures'] = int(_number(state.get('consecutive_failures'))) + 1
    state['last_run'] = {'outcome': outcome, 'error': error, 'error_detail': detail, 'at': now}
    state['running'] = None


def _failed(state: Dict[str, Any], now: float, error: str, detail: Optional[str] = None, *,
            outcome: str = 'failed') -> str:
    _record(state, now, outcome, error, detail)
    n = state['consecutive_failures']
    state['next_run_at'] = now + min(MAX_RETRY_S, RETRY_S * 2 ** (n - 1))
    _save_state(state)
    ts.mark_stale(error)
    logger.warning("trending songs: run %s (%s%s); next try in %.1f h", outcome, error,
                   ': %s' % detail if detail else '', (state['next_run_at'] - now) / 3600)
    return outcome


def _cool_down(state: Dict[str, Any], now: float, error: str, detail: Optional[str]) -> str:
    _record(state, now, 'failed', error, detail)
    n = int(_number(state.get('cooldowns'))) + 1
    state['cooldowns'] = n
    state['next_run_at'] = now + min(MAX_COOLDOWN_S, COOLDOWN_S * 2 ** (n - 1))
    _save_state(state)
    ts.mark_stale(error)
    logger.warning("trending songs: Instagram asked to slow down (%s); cooling down %.0f h",
                   error, (state['next_run_at'] - now) / 3600)
    return 'failed'


def _freeze(state: Dict[str, Any], now: float, error: str, detail: Optional[str]) -> str:
    _record(state, now, 'failed', error, detail)
    n = int(_number(state.get('freezes'))) + 1
    span = min(MAX_FREEZE_S, FREEZE_S * 2 ** (n - 1))
    state.update(freezes=n, frozen_until=now + span, next_run_at=now + span)
    _save_state(state)
    ts.mark_stale(error)
    logger.warning("trending songs: frozen %.0f h, the research account needs a person (%s)",
                   span / 3600, error)
    _tell_owner(error, span)
    return 'failed'


def _tell_owner(error: str, span: float) -> None:
    """Tell this node's owner that the research account needs a person."""
    what = ('asked the research account to prove it is a person' if error == 'challenge'
            else 'refused the research account\'s login')
    message = ('Trending songs are paused for %d hours: Instagram %s. Sign in to that '
               'account by hand; the daily list resumes on its own.' % (span // 3600, what))
    try:
        from integrations.agent_engine.capability_setup import setup_owner
        owner = setup_owner()
        if not owner:
            logger.warning("trending songs: paused (%s), and this node has no owner to tell",
                           error)
            return
        from integrations.social.models import db_session
        from integrations.social.services import NotificationService
        with db_session() as db:
            NotificationService.create(db, owner, PAUSED_NOTIFICATION, message=message)
    except Exception as e:
        logger.warning("trending songs: paused (%s); the owner could not be told: %s", error, e)


# ── small helpers ─────────────────────────────────────────────────────────

def _day(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _clear(run_dir: str) -> None:
    """Remove what an earlier run left in run/ (its spec and batch)."""
    try:
        names = os.listdir(run_dir)
    except OSError:
        return
    for name in names:
        if name.endswith(('.json', '.tmp')):
            _remove(os.path.join(run_dir, name))
