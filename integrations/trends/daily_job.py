"""integrations.trends.daily_job: the daily trending-songs run on HER
desktop, driven from the agent daemon's tick.

Owner rulings: A10d ("all in local nunba build not in central": the list is
made on her desktop, which adds value while she is away; never on a central
node, and nothing is served to or fetched from one) and U6 (a child process
the daemon's tick starts and then only polls).

``maybe_tick()`` is called on every daemon tick that gets past the daemon's
yield gate (dispatch.should_yield_to_user: nobody is using the computer),
before its "no active goals" stop: her list does not depend on goals.  It
runs no thread and no LLM:

* on a central node (or one whose tier cannot be read), without her
  research account, or without a watchlist it returns 'skip', and between
  checks it only compares two numbers;
* when the run is due it starts the worker as a child process
  (``<worker venv python> -I ig_worker.py <spec> <out>``) and returns
  'started'; the research account's credentials are written to the child's
  stdin as one JSON line and nowhere else (not argv, not the spec, not its
  environment, not a log line);
* later ticks only ``poll()`` the child ('running'); when it exits, its
  batch is ranked (trending_songs.rank_batch, through igkit.swipe), merged
  with yesterday's list and published ('published'), or the run is
  recorded as failed ('failed') and yesterday's list is kept, marked stale;
* after TIMEOUT_S the child is killed ('timeout').  The worker stops itself
  earlier (its spec's deadline_s), so this is the backstop.

Her research account: a DEDICATED Instagram account made for this research,
never her business account.  Its username and password are owner-entered
credentials in this desktop's own vault (hartos.ai_key_vault;
USERNAME_CREDENTIAL and PASSWORD_CREDENTIAL), never tracked.  The way in is
the one credential consent card: when her agent asks for trending songs and
the account is missing, ``ask_for_account()`` (through
trending_songs_client) files it with hartos.ai_key_vault.request_credential,
one card per part, for this computer's owner.  The card's Accept keeps the
value in Nunba's desktop vault, which hands it to this vault
(hold_credential) now and at every boot, and grants the consent row that
names it; that row is what survives a restart.  A value sent to the
localhost submit (/api/credentials/submit, store_credential) is not owner-
entered past a restart: it names the credential only in the process that
stored it.  The values are read only through AIKeyVault.owner_credential,
so a name the owner never entered reads as nothing, whatever an
environment variable or the node's own secrets hold.  Once her grant names
it, the vault's own lookup order applies: an environment variable of the
same name is read before the value she entered
(SecretsManager.get_secret reads os.environ first).

The worker's interpreter: instagrapi pins pydantic and requests above what
HARTOS and Nunba's bundled python-embed carry, so it lives in its own venv
under the data dir (core.venv_paths, the per-backend venv store; name
WORKER_VENV, packages WORKER_PACKAGES).  When that venv is missing, its last
install did not finish (failed, timed out, or cut short by a restart), or a
run found no instagrapi in it, the owner is asked first
(capability_setup.ask_owner_for_setup, capability SETUP_CAPABILITY); on yes
Nunba's installer builds it on its own thread
(tts.backend_venv.install_in_background) and later ticks only ask how it
went ('installing', then 'installed').  HARTOS run without Nunba has no
installer, so the job waits and says so.

Due means: the node is a desktop (security.key_delegation.get_node_tier
answers and is not central), her research account is entered,
``trends/watchlist.json`` names at least one account, nothing is frozen,
and ``state.json``'s next_run_at has passed (set a day after the last good
run, plus up to JITTER_S).

Failures (goals-trends report 2.5):
* challenge / login: frozen for FREEZE_S (doubling on each freeze in a row,
  at most MAX_FREEZE_S), and the desktop's owner is told
  (NotificationService) that the research account needs a person; after a
  refused login the credential card asks the owner to enter the account
  again (request_credential, "rejected").  Entering it does not end the
  pause: the run after it uses what is entered by then;
* throttled / feedback: cooled down COOLDOWN_S (doubling, at most
  MAX_COOLDOWN_S);
* anything else (a partial run, a dead or killed worker, bad output, no
  instagrapi, a failed install): retried after RETRY_S, doubling, at most
  MAX_RETRY_S.
A restart while the child runs leaves ``state.json`` saying so; the next
process adopts the child's batch when it is written, or records the run as
interrupted once TIMEOUT_S has passed.

``why_no_list()`` says why there is no list (for her agent's reply), and
where the ask for her research account stands when that is why.

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
from typing import Any, Dict, List, Optional

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

#: Her research account, as owner-entered credentials in this desktop's
#: vault (hartos.ai_key_vault.AIKeyVault.credential_name form).  Never her
#: business account.
USERNAME_CREDENTIAL = 'IG_RESEARCH_USERNAME'
PASSWORD_CREDENTIAL = 'IG_RESEARCH_PASSWORD'
#: What the credential card says for each part (request_credential builds
#: the card from label, used_by and description).  The job reads the values
#: itself, so no agent is handed their {{secret:...}} alias.
ACCOUNT_LABELS = {USERNAME_CREDENTIAL: 'The research Instagram account username',
                  PASSWORD_CREDENTIAL: 'The research Instagram account password'}
ACCOUNT_USED_BY = 'the daily trending-song list'
ACCOUNT_DESCRIPTION = ("Use a separate Instagram account made only for this research, never "
                       "your business account: this computer signs in to it once a day, "
                       "while it is not in use, to see which songs makeup reels use.")

#: The worker's own venv under the data dir (core.venv_paths.venv_path), and
#: what Nunba's installer puts in it, in install order: instagrapi's direct
#: dependencies first, then instagrapi, all exact pins.
#: tts.backend_venv.install_into_venv imports each spec it installed to
#: verify it, through tts.package_installer._PIP_TO_IMPORT for the three
#: whose import name differs (pysocks -> socks, pillow -> PIL,
#: pycryptodomex -> Cryptodome; that table's keys are lowercase, as pip's
#: names are case-insensitive).  No hashed lock: pip resolves what these
#: pins leave open.  instagrapi ships almost daily: bump it on purpose and
#: re-read ig_worker.py against the new source.
WORKER_VENV = 'ig_trends'
WORKER_PACKAGES = ('pydantic==2.12.5', 'requests==2.34.2', 'curl_cffi==0.15.0',
                   'pysocks==1.7.1', 'pillow==12.2.0', 'pycryptodomex==3.23.0',
                   'instagrapi==3.0.24')
#: A last run that ended this way leaves no usable venv behind, so the next
#: due run installs again (asking first) instead of starting the worker:
#: no instagrapi in it, or an install that failed or timed out after
#: ensure_venv had already made its interpreter.
REINSTALL_ERRORS = ('dependency', 'install_failed', 'install_timeout')
#: The owner's yes to building it (capability_setup scope), and what the
#: card says.
SETUP_CAPABILITY = 'trends:' + WORKER_VENV
SETUP_REASON = ("Find the songs trending in makeup reels on Instagram once a day, while "
                "this computer is not in use? It installs a small Instagram reader "
                "(instagrapi) on this computer and signs in with the research account "
                "entered here, never your business account.")
#: An install still running after this long is given up (Nunba's installer
#: allows each package 30 minutes).
INSTALL_TIMEOUT_S = 3 * 3600.0

WORKER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ig_worker.py')
#: All the child's environment holds: what an interpreter needs to start and
#: reach the network.  Nothing else of the daemon's environment (keys,
#: tokens, HARTOS settings) reaches it, and no credential: those go on its
#: stdin.
_CHILD_ENV_KEYS = ('PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'HOME', 'USERPROFILE', 'LANG',
                   'LC_ALL', 'TMPDIR', 'TEMP', 'TMP', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
                   'REQUESTS_CA_BUNDLE', 'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY')

_lock = threading.Lock()
#: What this process is waiting on: the worker it started ({'kind': 'worker',
#: 'proc', 'pid', 'started_at', 'spec', 'out', 'region', 'niche'}) or the
#: install it asked Nunba for ({'kind': 'install', 'started_at'}), or None.
_run: Optional[Dict[str, Any]] = None
#: The tick before which a skip is not re-examined (a cheap no-op).
_next_check_at = 0.0
_last_skip: Optional[str] = None


def maybe_tick(*, now: Optional[float] = None) -> str:
    """One daemon tick's worth of the run: 'skip', 'installing', 'installed',
    'started', 'running', 'published', 'failed' or 'timeout'.  Never raises
    and never blocks: a second caller while one is inside gets 'running'."""
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
    python = _worker_python(state)
    if python is None:
        return _install(state, now)
    return _start(state, now, python)


def why_no_list() -> str:
    """Why this computer has no trending-song list (for her agent's reply):
    'central' (never made on a central node), 'tier_unknown' (the node's
    tier could not be read, so nothing runs), no research account entered
    here -- 'account_asked' (its consent card is waiting), 'account_declined'
    (the owner said no to it) or 'no_account' (nobody asked, or nobody here
    to ask) --, 'no_watchlist', 'running', 'installing' (this process is
    installing the worker's venv), 'setup_asked' / 'setup_declined' /
    'setup_unavailable' (the owner's answer to installing the worker),
    'no_installer' (HARTOS without Nunba), the last run's error code, or
    'not_run_yet'."""
    why = _not_ready()
    if why == 'no_account':
        return _account_ask_state()
    if why:
        return why
    state = _load_state()
    if isinstance(state.get('running'), dict):
        return 'running'
    run = _run
    if isinstance(run, dict) and run.get('kind') == 'install':
        return 'installing'
    if isinstance(state.get('waiting'), str) and state['waiting']:
        return state['waiting']
    last = state.get('last_run') if isinstance(state.get('last_run'), dict) else {}
    if last.get('error'):
        return str(last['error'])[:40]
    return 'not_run_yet'


# ── readiness ─────────────────────────────────────────────────────────────

def _tier_refusal() -> Optional[str]:
    """'central' on a central node, which never runs the job; 'tier_unknown'
    when security.key_delegation.get_node_tier cannot answer (failing closed:
    nothing runs, and the reason says what happened rather than calling her
    desktop a server); None on a desktop."""
    try:
        from security.key_delegation import get_node_tier
        tier = get_node_tier()
    except Exception as e:      # said once per change by _note_skip
        logger.debug("trending songs: this node's tier could not be read (%s)", e)
        return 'tier_unknown'
    return 'central' if tier == 'central' else None


def _not_ready() -> Optional[str]:
    why = _tier_refusal()
    if why:
        return why
    username, password = _credentials()
    if not username or not password:
        return 'no_account'
    if not _watchlist()['accounts']:
        return 'no_watchlist'
    return None


def _note_skip(why: str) -> None:
    """Say once why this node is not running the job (a central node, which
    never runs it, says nothing)."""
    global _last_skip
    if why != _last_skip and why != 'central':
        logger.info("trending songs: not running on this computer yet (%s)", why)
    _last_skip = why


def _entered() -> Dict[str, str]:
    """Her research account's parts as the owner entered them here:
    ``{USERNAME_CREDENTIAL: ..., PASSWORD_CREDENTIAL: ...}``, '' for a part
    not entered.  Owner-entered credentials only
    (AIKeyVault.owner_credential), so a name she never entered reads as ''
    whatever the environment or the node's own secrets hold.  Both '' when
    the vault cannot be read."""
    try:
        from hartos.ai_key_vault import get_ai_key_vault
        vault = get_ai_key_vault()
        names = vault.owner_credential_names()
        return {USERNAME_CREDENTIAL: (vault.owner_credential(USERNAME_CREDENTIAL, names)
                                      or '').strip(),
                PASSWORD_CREDENTIAL: vault.owner_credential(PASSWORD_CREDENTIAL, names) or ''}
    except Exception as e:
        logger.warning("trending songs: the research account could not be read: %s", e)
        return {USERNAME_CREDENTIAL: '', PASSWORD_CREDENTIAL: ''}


def _credentials():
    """``(username, password)`` of her research account (_entered); ('', '')
    when it is not entered or the vault cannot be read."""
    entered = _entered()
    return entered[USERNAME_CREDENTIAL], entered[PASSWORD_CREDENTIAL]


# ── asking the owner for her research account ─────────────────────────────

def ask_for_account() -> List[str]:
    """File the credential consent card for each part of her research
    account not entered on this computer yet, for its owner: called when her
    agent asks for trending songs and there is no list
    (trending_songs_client).  hartos.ai_key_vault.request_credential is the
    one credential ask: one card per part however often it is asked (each
    ask sends it again to whatever screen is up, as one card), never again
    after the owner's no, nothing filed when nobody here can be asked.
    Nothing is asked on a central node or one whose tier cannot be read.
    Returns the names asked for ([] when nothing is missing or nothing was
    asked).  Never raises."""
    if _tier_refusal():
        return []
    missing = [name for name, value in _entered().items() if not value]
    if missing:
        _ask_owner_for(missing)
    return missing


def _ask_owner_for(names: List[str], *, rejected: bool = False) -> None:
    """request_credential for each of ``names``, as this job (no agent);
    ``rejected`` says Instagram refused the value entered, so the card
    comes back for one that was already entered.  Its answer is for an
    agent that uses the {{secret:...}} alias; the job needs none of it
    (where the ask stands is read from the consent table,
    _account_ask_state)."""
    try:
        from hartos.ai_key_vault import request_credential
    except ImportError as e:
        logger.warning("trending songs: the owner cannot be asked for the research "
                       "account (%s)", e)
        return
    for name in names:
        ask = {'key_name': name, 'label': ACCOUNT_LABELS[name], 'used_by': ACCOUNT_USED_BY,
               'description': ACCOUNT_DESCRIPTION}
        if rejected:
            ask['rejected'] = True
        try:
            request_credential(json.dumps(ask), agent_id=None)
        except Exception as e:
            logger.warning("trending songs: the card for %s could not be filed: %s", name, e)


def _account_ask_state() -> str:
    """Where the ask for her research account stands, as the consent table
    says (the cards ask_for_account files, for no agent): 'account_declined'
    when the owner said no to a missing part, 'account_asked' when a card
    for a missing part is on file and undecided, else 'no_account' (never
    asked, nobody here to ask, or the table unreadable)."""
    from integrations.agent_engine.capability_setup import setup_owner
    owner = setup_owner()
    missing = [name for name, value in _entered().items() if not value]
    if not owner or not missing:
        return 'no_account'
    try:
        from integrations.social.consent_service import CREDENTIAL_SCOPE_PREFIX, ConsentService
        from integrations.social.models import db_session
        scopes = [CREDENTIAL_SCOPE_PREFIX + name for name in missing]
        with db_session(commit=False) as db:
            if any(ConsentService.declined(db, owner, 'credential', scope=s) for s in scopes):
                return 'account_declined'
            if any(ConsentService.ask_on_file(db, owner, 'credential', scope=s) is not None
                   for s in scopes):
                return 'account_asked'
    except Exception as e:
        logger.warning("trending songs: where the research account ask stands is "
                       "unknown: %s", e)
    return 'no_account'


# ── the worker's interpreter ──────────────────────────────────────────────

def _worker_python(state: Dict[str, Any]) -> Optional[str]:
    """The worker venv's python when the worker can run in it, else None:
    the venv is missing (or another interpreter built it,
    core.venv_paths.venv_python_if_exists); an install was started and this
    process is not running it (a restart cut it short: ``installing_since``
    is still in the state); or the last run ended in REINSTALL_ERRORS and
    nothing was installed since.  Only called with no install running here."""
    from core.venv_paths import venv_python_if_exists
    python = venv_python_if_exists(WORKER_VENV)
    if not python:
        return None
    if _number(state.get('installing_since')):
        return None
    last = state.get('last_run') if isinstance(state.get('last_run'), dict) else {}
    if (last.get('error') in REINSTALL_ERRORS
            and _number(last.get('at')) >= _number(state.get('installed_at'))):
        return None
    return python


def _installer():
    """Nunba's venv installer (tts.backend_venv): ``(install_in_background,
    background_install)``, or None where HARTOS runs without Nunba."""
    try:
        from tts.backend_venv import background_install, install_in_background
    except ImportError:
        return None
    return install_in_background, background_install


def _wait(state: Dict[str, Any], why: str) -> str:
    """The run is due but waits on the owner or on Nunba: say why once."""
    if state.get('waiting') != why:
        state['waiting'] = why
        _save_state(state)
    _note_skip(why)
    return 'skip'


def _install(state: Dict[str, Any], now: float) -> str:
    """Build the worker's venv: ask the owner first, then hand the install
    to Nunba's installer, which runs it on its own thread; later ticks only
    ask how it went (_poll_install).  ``installing_since`` stays in the
    state until it ends, so a process that finds it there without running
    the install knows a restart cut it short (_worker_python)."""
    global _run
    installer = _installer()
    if installer is None:
        return _wait(state, 'no_installer')
    from integrations.agent_engine.capability_setup import ask_owner_for_setup
    answer = ask_owner_for_setup(SETUP_CAPABILITY, reason=SETUP_REASON)
    if answer != 'granted':
        return _wait(state, 'setup_' + answer)
    state.pop('waiting', None)
    try:
        installer[0](WORKER_VENV, list(WORKER_PACKAGES))
    except Exception as e:
        return _failed(state, now, 'install_failed', str(e)[:200])
    _run = {'kind': 'install', 'started_at': now}
    state['installing_since'] = now
    _save_state(state)
    logger.info("trending songs: the owner allowed it; installing the worker's venv %r",
                WORKER_VENV)
    return 'installing'


def _poll_install(run: Dict[str, Any], now: float) -> str:
    global _run, _next_check_at
    installer = _installer()
    status = installer[1](WORKER_VENV) if installer else None
    if isinstance(status, dict) and status.get('running'):
        if now - run['started_at'] < INSTALL_TIMEOUT_S:
            return 'installing'
        _run = None
        state = _load_state()
        state.pop('installing_since', None)
        return _failed(state, now, 'install_timeout',
                       'still running after %d s' % INSTALL_TIMEOUT_S)
    _run = None
    state = _load_state()
    state.pop('installing_since', None)
    if not isinstance(status, dict) or status.get('ok') is not True:
        message = status.get('message') if isinstance(status, dict) else None
        return _failed(state, now, 'install_failed',
                       message[-200:] if isinstance(message, str) else 'no result')
    state['installed_at'] = now
    _save_state(state)
    _next_check_at = now                 # the run it was built for starts next tick
    logger.info("trending songs: the worker's venv %r is installed", WORKER_VENV)
    return 'installed'


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

def _start(state: Dict[str, Any], now: float, python: str) -> str:
    global _run
    username, password = _credentials()
    if not username or not password:
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
    _hand_credentials(proc, username, password)
    _run = {'kind': 'worker', 'proc': proc, 'pid': proc.pid, 'started_at': now,
            'spec': spec_path, 'out': out_path, 'region': watch['region'],
            'niche': watch['niche']}
    state['running'] = {k: _run[k] for k in ('pid', 'started_at', 'spec', 'out', 'region',
                                              'niche')}
    state.pop('waiting', None)
    _save_state(state)
    logger.info("trending songs: worker started (pid %s, %d account(s))", proc.pid,
                len(accounts))
    return 'started'


def _spawn(argv, cwd: str):
    """Start the worker with only _CHILD_ENV_KEYS in its environment, a pipe
    for its stdin (the credentials, _hand_credentials), output to
    trends/worker.log."""
    env = {k: os.environ[k] for k in _CHILD_ENV_KEYS if k in os.environ}
    from core.subprocess_safe import no_window_kwargs
    log = open(os.path.join(cwd, 'worker.log'), 'wb')
    try:
        kwargs = dict(env=env, cwd=cwd, stdin=subprocess.PIPE, stdout=log,
                      stderr=subprocess.STDOUT, close_fds=True, **no_window_kwargs())
        if os.name == 'posix':
            # Its own session: a signal meant for the desktop's terminal or
            # process group never reaches the worker mid-run.
            kwargs['start_new_session'] = True
        return subprocess.Popen(argv, **kwargs)
    finally:
        log.close()


def _hand_credentials(proc, username: str, password: str) -> None:
    """Write her research account to the worker's stdin as one JSON line and
    close it: the only way the credentials leave this process.  The line is
    ASCII (json escapes anything else), so the child's stdin encoding cannot
    garble a password.  A worker that died before reading it exits on its
    own and its batch says why."""
    line = (json.dumps({'username': username, 'password': password}) + '\n').encode('utf-8')
    try:
        proc.stdin.write(line)
    except (OSError, ValueError) as e:
        logger.warning("trending songs: the worker did not take its sign-in (%s)",
                       type(e).__name__)
    finally:
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass


def _poll(now: float) -> str:
    run = _run
    if run.get('kind') == 'install':
        return _poll_install(run, now)
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
    if error == 'login':
        # The owner's rule for a refused sign-in: ask again, on the same
        # card (request_credential "rejected"; it stops asking after the
        # owner's no or MAX_CREDENTIAL_ENTRIES tries in a day).
        _ask_owner_for([USERNAME_CREDENTIAL, PASSWORD_CREDENTIAL], rejected=True)
    _tell_owner(error, span)
    return 'failed'


def _tell_owner(error: str, span: float) -> None:
    """Tell this node's owner that the research account needs a person."""
    if error == 'challenge':
        message = ('Trending songs are paused for %d hours: Instagram asked the research '
                   'account to prove it is a person. Sign in to that account by hand; the '
                   'daily list tries again on its own after the pause.' % (span // 3600))
    else:
        message = ('Trending songs are paused for %d hours: Instagram refused the research '
                   'account\'s sign-in. Enter its username and password again on the card '
                   'this computer shows; the daily list tries again on its own after the '
                   'pause, with what is entered by then.' % (span // 3600))
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
