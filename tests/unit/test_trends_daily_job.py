"""integrations.trends.daily_job.maybe_tick: the daily trending-songs run on
HER desktop, driven from the agent daemon's tick.

Owner rulings: A10d ("all in local nunba build not in central": the list is
made on her desktop, never on central) and U6 (a child process the daemon's
tick starts and then only polls: no thread, no LLM).

What these tests hold the job to:
* never on a central node, nor on one whose tier cannot be read; on her
  desktop only when she has entered the DEDICATED research account on this
  computer (an owner-entered credential in the desktop's vault,
  hartos.ai_key_vault -- never an environment variable or the node's own
  secrets) and a watchlist exists;
* the account's way in: her agent's ask (ask_for_account) files the one
  credential consent card (hartos.ai_key_vault.request_credential) for each
  missing part, one card each however often she asks, none after the
  owner's no; what the card stores (the desktop vault's value, the grant
  that names it) runs the job and survives a restart;
* a refused login pauses the job for FREEZE_S and asks the owner, on the
  same card, to enter the account again;
* the worker runs in its own interpreter under the data dir (the per-backend
  venv core.venv_paths resolves); when that venv is missing, or its last
  install failed, timed out or was cut short by a restart, the owner is
  asked first (capability_setup) and, on yes, Nunba's installer
  (tts.backend_venv.install_in_background) builds it off the tick;
* the credentials reach the child on its stdin only: never argv, the spec,
  its environment or a log line;
* once a day plus jitter; 900 s kill; restarts adopt a finished batch;
* the daemon calls it only past its yield gate (so while she is away) and
  before its "no goals" stop.
"""
import contextlib
import json
import os
import subprocess
import threading
import types

import pytest

from tests.fixtures.trends import build as fx

NOW = 1792000000.0
HOUR = 3600.0
USER = 'research_fixture'
PASSWORD = 'pw-fixture'
OWNER = 'owner-trends'


class FakeStdin:
    def __init__(self):
        self.data = b''
        self.closed = False

    def write(self, data):
        self.data += data
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakePopen:
    made = []

    def __init__(self, argv, **kw):
        self.argv, self.kw = list(argv), kw
        self.pid = 4242
        self.returncode = None
        self.killed = False
        self.polls = 0
        self.stdin = FakeStdin() if kw.get('stdin') == subprocess.PIPE else None
        FakePopen.made.append(self)

    def poll(self):
        self.polls += 1
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode

    def exit_with(self, batch, rc=0):
        with open(self.argv[-1], 'w', encoding='utf-8') as fh:
            json.dump(batch, fh)
        self.returncode = rc


class FakeVault:
    """hartos.ai_key_vault's AIKeyVault as the job reads it: only names the
    owner entered resolve (the real vault on the card's path: ``cards``)."""

    def __init__(self, creds):
        self.creds = creds

    def owner_credential_names(self):
        return set(self.creds)

    def owner_credential(self, name, names=None):
        names = self.owner_credential_names() if names is None else names
        return self.creds.get(name, '') if name in names else ''


def _installer(inst):
    """A stand-in for Nunba's tts.backend_venv (install_in_background,
    background_install), recording every install asked for."""
    mod = types.ModuleType('tts.backend_venv')

    def install_in_background(backend, packages):
        inst['calls'].append((backend, list(packages)))
        if inst['status'] and inst['status'].get('running'):
            return 'running'
        inst['status'] = {'running': True, 'ok': None, 'message': '',
                          'started_at': 1.0, 'finished_at': None}
        return 'started'

    def background_install(backend):
        return dict(inst['status']) if inst['status'] else None
    mod.install_in_background = install_in_background
    mod.background_install = background_install
    pkg = types.ModuleType('tts')
    pkg.__path__ = []
    pkg.backend_venv = mod
    return {'tts': pkg, 'tts.backend_venv': mod}


@pytest.fixture
def job(tmp_path, monkeypatch):
    from tests.conftest import refuse_all_network
    refuse_all_network(monkeypatch)
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    from core import platform_paths
    platform_paths.reset_cache()
    from integrations.trends import daily_job as dj
    monkeypatch.setattr(dj, '_run', None)
    monkeypatch.setattr(dj, '_next_check_at', 0.0)
    monkeypatch.setattr('security.key_delegation.get_node_tier', lambda: 'flat')
    # No owner unless a test names one (``cards``): this box's own
    # environment must not decide who is asked.
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
    env = types.SimpleNamespace(dj=dj, asked=[], consent='granted', tmp=tmp_path,
                                creds={dj.USERNAME_CREDENTIAL: USER,
                                       dj.PASSWORD_CREDENTIAL: PASSWORD},
                                inst={'calls': [], 'status': None})
    vault = FakeVault(env.creds)
    monkeypatch.setattr('hartos.ai_key_vault.get_ai_key_vault', lambda: vault)
    py = tmp_path / 'venvs' / dj.WORKER_VENV / 'python'
    py.parent.mkdir(parents=True)
    py.write_text('', encoding='utf-8')
    env.python = str(py)
    monkeypatch.setattr('core.venv_paths.venv_python_if_exists',
                        lambda backend: env.python if backend == dj.WORKER_VENV else None)

    def _ask(capability, *, reason):
        env.asked.append((capability, reason))
        return env.consent
    monkeypatch.setattr('integrations.agent_engine.capability_setup.ask_owner_for_setup', _ask)
    (tmp_path / 'trends').mkdir(exist_ok=True)
    (tmp_path / 'trends' / 'watchlist.json').write_text(json.dumps(
        {'region': 'IN', 'niche': 'makeup',
         'accounts': ['mua_a', {'handle': 'mua_b', 'pk': '555'}, 'not a handle!']}),
        encoding='utf-8')
    FakePopen.made = []
    monkeypatch.setattr(dj.subprocess, 'Popen', FakePopen)
    env.built = []

    class _NoThread:
        def __init__(self, *a, **kw):
            env.built.append(kw.get('name'))
            raise AssertionError('the trends job must not start a thread')
    monkeypatch.setattr(threading, 'Thread', _NoThread)
    from tests.unit.module_swap import swap_modules
    with swap_modules(_installer(env.inst)):
        yield env
    platform_paths.reset_cache()


def _good_batch():
    return fx.batch({
        'mua_a': fx.account('mua_a', [6000] + [1000] * 11, now=NOW, songs=[101] + [None] * 11,
                            first_pk=1000),
        'mua_b': fx.account('mua_b', [3000] + [1000] * 11, now=NOW, songs=[101] + [None] * 11,
                            first_pk=2000),
    })


def _state(tmp_path):
    with open(tmp_path / 'trends' / 'state.json', encoding='utf-8') as fh:
        return json.load(fh)


def _fresh_tick(dj):
    dj._next_check_at = 0.0


# ── where it runs ────────────────────────────────────────────────────────

def test_on_central_it_never_runs(job, monkeypatch):
    monkeypatch.setattr('security.key_delegation.get_node_tier', lambda: 'central')
    assert job.dj.maybe_tick(now=NOW) == 'skip'
    assert FakePopen.made == [] and job.asked == [] and job.inst['calls'] == []
    assert job.dj.why_no_list() == 'central'


@pytest.mark.parametrize('tier', ['flat', 'regional', 'local'])
def test_on_her_desktop_it_runs(job, monkeypatch, tier):
    monkeypatch.setattr('security.key_delegation.get_node_tier', lambda: tier)
    assert job.dj.maybe_tick(now=NOW) == 'started'
    assert len(FakePopen.made) == 1


@pytest.mark.parametrize('missing', ['USERNAME_CREDENTIAL', 'PASSWORD_CREDENTIAL'])
def test_without_the_research_account_it_does_nothing(job, missing):
    job.creds.pop(getattr(job.dj, missing))
    assert job.dj.maybe_tick(now=NOW) == 'skip'
    assert FakePopen.made == [] and job.asked == [] and job.inst['calls'] == []
    assert job.dj.why_no_list() == 'no_account'


def test_the_account_comes_only_from_what_the_owner_entered_here(job, monkeypatch):
    """Environment variables and the node's own secrets are not her
    research account: only an owner-entered credential in this desktop's
    vault is."""
    job.creds.clear()
    for name in ('HEVOLVE_IG_RESEARCH_USERNAME', 'HEVOLVE_IG_RESEARCH_PASSWORD',
                 'IG_RESEARCH_USERNAME', 'IG_RESEARCH_PASSWORD'):
        monkeypatch.setenv(name, 'from-the-environment')
    monkeypatch.setattr('security.secrets_manager.get_secret',
                        lambda name, default='': 'from-the-node-vault')
    assert job.dj.maybe_tick(now=NOW) == 'skip'
    assert FakePopen.made == []


def test_without_a_watchlist_it_does_nothing(job):
    os.remove(job.tmp / 'trends' / 'watchlist.json')
    assert job.dj.maybe_tick(now=NOW) == 'skip'
    assert FakePopen.made == [] and job.inst['calls'] == []
    assert job.dj.why_no_list() == 'no_watchlist'


def test_a_node_whose_tier_cannot_be_read_runs_nothing_and_says_so(job, monkeypatch):
    """Failing closed, but not as 'central': on her desktop that reason
    would tell her this node is a server."""
    def _unreadable():
        raise RuntimeError('certificate chain unreadable')
    monkeypatch.setattr('security.key_delegation.get_node_tier', _unreadable)
    assert job.dj.maybe_tick(now=NOW) == 'skip'
    assert FakePopen.made == [] and job.asked == [] and job.inst['calls'] == []
    assert job.dj.why_no_list() == 'tier_unknown'
    assert job.dj.ask_for_account() == []


# ── her research account: the one way in ─────────────────────────────────

@pytest.fixture
def cards(job, monkeypatch):
    """The consent card's real path: hartos.ai_key_vault.request_credential
    (the one credential ask) and a real, fresh AIKeyVault, on a private
    consent table, with this computer's owner named and every card the
    owner would see recorded.  The same table shape as
    tests/unit/test_capability_setup_on_demand.py."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from hartos import ai_key_vault
    from integrations.social import models
    from integrations.social.models import UserConsent
    from security.secrets_manager import SecretsManager
    engine = create_engine('sqlite://', poolclass=StaticPool,
                           connect_args={'check_same_thread': False})
    UserConsent.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextlib.contextmanager
    def _session(commit=True):
        db = factory()
        try:
            yield db
            if commit:
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    monkeypatch.setattr(models, 'db_session', _session)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    monkeypatch.delenv('HEVOLVE_MASTER_KEY', raising=False)
    for name in (job.dj.USERNAME_CREDENTIAL, job.dj.PASSWORD_CREDENTIAL):
        monkeypatch.delenv(name, raising=False)
    SecretsManager.reset()
    vault = ai_key_vault.AIKeyVault()
    monkeypatch.setattr(ai_key_vault, 'get_ai_key_vault', lambda: vault)
    sent = []
    monkeypatch.setattr('integrations.social.consent_service._emit',
                        lambda topic, data, msg_id=None: sent.append((topic, dict(data), msg_id)))
    yield types.SimpleNamespace(vault=vault, sent=sent, session=_session, mp=monkeypatch)
    SecretsManager.reset()
    engine.dispose()


def _asks(cards):
    return [(data, msg_id) for topic, data, msg_id in cards.sent if topic == 'consent.request']


def _rows(cards):
    from integrations.social.models import UserConsent
    with cards.session() as db:
        return [(r.consent_type, r.scope, r.agent_id, bool(r.granted))
                for r in db.query(UserConsent).all()]


def test_with_no_account_her_agents_ask_files_one_card_per_part(job, cards):
    dj = job.dj
    assert dj.why_no_list() == 'no_account'               # nobody asked yet
    assert dj.ask_for_account() == [dj.USERNAME_CREDENTIAL, dj.PASSWORD_CREDENTIAL]
    asks = _asks(cards)
    assert sorted(a['scope'] for a, _ in asks) == sorted(
        ['secret:' + dj.USERNAME_CREDENTIAL, 'secret:' + dj.PASSWORD_CREDENTIAL])
    for ask, _ in asks:
        assert ask['consent_type'] == 'credential' and ask['user_id'] == OWNER
        assert ask['agent_id'] is None                    # the job asks, not an agent
        assert 'never your business account' in ask['reason']
    assert dj.why_no_list() == 'account_asked'
    for _ in range(2):                                    # she asks again, and again
        dj.ask_for_account()
    assert sorted(_rows(cards)) == sorted(
        [('credential', 'secret:' + dj.USERNAME_CREDENTIAL, None, False),
         ('credential', 'secret:' + dj.PASSWORD_CREDENTIAL, None, False)])
    assert len({msg_id for _, msg_id in _asks(cards)}) == 2     # still two cards
    assert FakePopen.made == [] and job.built == []


def test_what_the_card_stores_runs_the_job_and_survives_a_restart(job, cards):
    """The card's Accept: Nunba's desktop vault keeps the value and hands it
    to this vault (export_to_env -> hold_credential), and the grant names
    it.  A restart is a fresh vault, the same grant rows, and the same
    hand-over at boot."""
    from hartos.ai_key_vault import AIKeyVault
    from integrations.social.consent_service import ConsentService
    dj = job.dj
    dj.ask_for_account()
    with cards.session() as db:
        for name in (dj.USERNAME_CREDENTIAL, dj.PASSWORD_CREDENTIAL):
            ConsentService.grant_consent(db, OWNER, 'credential', 'secret:' + name)
    cards.vault.hold_credential(dj.USERNAME_CREDENTIAL, USER)
    cards.vault.hold_credential(dj.PASSWORD_CREDENTIAL, PASSWORD)
    assert dj.maybe_tick(now=NOW) == 'started'
    assert json.loads(FakePopen.made[0].stdin.data.decode('utf-8')) == {
        'username': USER, 'password': PASSWORD}
    restarted = AIKeyVault()
    restarted.hold_credential(dj.USERNAME_CREDENTIAL, USER)
    restarted.hold_credential(dj.PASSWORD_CREDENTIAL, PASSWORD)
    cards.mp.setattr('hartos.ai_key_vault.get_ai_key_vault', lambda: restarted)
    assert dj.why_no_list() == 'running'                  # the account still reads


def test_a_value_held_without_the_owners_grant_is_not_her_account(job, cards):
    dj = job.dj
    cards.vault.hold_credential(dj.USERNAME_CREDENTIAL, USER)
    cards.vault.hold_credential(dj.PASSWORD_CREDENTIAL, PASSWORD)
    assert dj.maybe_tick(now=NOW) == 'skip'
    assert FakePopen.made == []
    assert dj.why_no_list() == 'no_account'


def test_after_the_owners_no_she_is_told_and_that_card_is_not_shown_again(job, cards):
    from integrations.social.consent_service import ConsentService
    dj = job.dj
    dj.ask_for_account()
    with cards.session() as db:
        ConsentService.decline(db, OWNER, 'credential', 'secret:' + dj.USERNAME_CREDENTIAL)
    cards.sent.clear()
    dj.ask_for_account()
    scopes = {a['scope'] for a, _ in _asks(cards)}
    assert 'secret:' + dj.USERNAME_CREDENTIAL not in scopes        # her no stands
    assert 'secret:' + dj.PASSWORD_CREDENTIAL in scopes            # the other card waits
    assert dj.why_no_list() == 'account_declined'


def test_with_nobody_to_ask_no_card_is_filed(job, cards, monkeypatch):
    dj = job.dj
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID')
    dj.ask_for_account()
    assert _asks(cards) == [] and _rows(cards) == []
    assert dj.why_no_list() == 'no_account'


def test_a_central_node_asks_for_no_account(job, cards, monkeypatch):
    dj = job.dj
    monkeypatch.setattr('security.key_delegation.get_node_tier', lambda: 'central')
    assert dj.ask_for_account() == []
    assert _asks(cards) == [] and _rows(cards) == []


def test_an_entered_account_is_not_asked_for(job):
    assert job.dj.ask_for_account() == []


# ── one run ──────────────────────────────────────────────────────────────

def test_a_due_run_starts_one_isolated_child_with_credentials_only_on_its_stdin(
        job, monkeypatch):
    dj = job.dj
    monkeypatch.setenv('HEVOLVE_MASTER_KEY', 'parent-only-secret')
    monkeypatch.setenv('OPENAI_API_KEY', 'parent-only-key')
    assert dj.maybe_tick(now=NOW) == 'started'
    assert len(FakePopen.made) == 1
    proc = FakePopen.made[0]
    assert proc.argv[:2] == [job.python, '-I']
    assert os.path.basename(proc.argv[2]) == 'ig_worker.py'
    assert os.path.isfile(proc.argv[2])
    assert len(proc.argv) == 5
    assert all(PASSWORD not in a and USER not in a for a in proc.argv)
    env = proc.kw['env']
    assert PASSWORD not in json.dumps(env) and USER not in json.dumps(env)
    assert 'HEVOLVE_MASTER_KEY' not in env and 'OPENAI_API_KEY' not in env
    assert proc.kw['stdin'] == subprocess.PIPE
    assert json.loads(proc.stdin.data.decode('utf-8')) == {'username': USER,
                                                           'password': PASSWORD}
    assert proc.stdin.closed is True
    with open(proc.argv[3], encoding='utf-8') as fh:
        spec = json.load(fh)
    assert spec['accounts'] == [{'handle': 'mua_a', 'pk': None},
                                {'handle': 'mua_b', 'pk': '555'}]
    assert spec['budget'] == 60 and spec['delay_range'] == [3, 8]
    assert 0 < spec['deadline_s'] < dj.TIMEOUT_S       # it answers before it is killed
    assert PASSWORD not in json.dumps(spec) and USER not in json.dumps(spec)
    assert spec['session_path'] == os.path.join(str(job.tmp), 'trends', '.ig_session.json')
    assert proc.argv[4].startswith(os.path.join(str(job.tmp), 'trends'))
    assert _state(job.tmp)['running']['pid'] == 4242
    assert PASSWORD not in json.dumps(_state(job.tmp))
    assert job.asked == [] and job.inst['calls'] == []    # the venv was there
    assert job.built == []


def test_while_the_child_runs_a_tick_only_polls(job):
    dj = job.dj
    assert dj.maybe_tick(now=NOW) == 'started'
    proc = FakePopen.made[0]
    for i in range(1, 4):
        assert dj.maybe_tick(now=NOW + 30 * i) == 'running'
    assert len(FakePopen.made) == 1 and proc.polls == 3
    assert dj.why_no_list() == 'running'
    assert job.built == []


def test_a_clean_exit_publishes_and_schedules_tomorrow_with_jitter(job):
    dj = job.dj
    dj.maybe_tick(now=NOW)
    FakePopen.made[0].exit_with(_good_batch())
    assert dj.maybe_tick(now=NOW + 400) == 'published'
    from integrations.trends import trending_songs as ts
    latest = ts.read_latest()
    assert latest['songs'][0]['song_key'] == 'cluster:101'
    assert (latest['region'], latest['niche']) == ('IN', 'makeup')
    state = _state(job.tmp)
    assert state['last_ok_at'] == NOW + 400
    assert NOW + 400 + 24 * HOUR <= state['next_run_at'] <= NOW + 400 + 24 * HOUR + dj.JITTER_S
    assert state['running'] is None and state['consecutive_failures'] == 0
    assert state['pks']['mua_a'] == str(fx.user_pk('mua_a'))
    assert dj.maybe_tick(now=NOW + 2 * HOUR) == 'skip'
    assert dj.maybe_tick(now=NOW + 23 * HOUR) == 'skip'
    assert dj.maybe_tick(now=state['next_run_at'] + 1) == 'started'
    assert len(FakePopen.made) == 2
    with open(FakePopen.made[1].argv[3], encoding='utf-8') as fh:
        spec = json.load(fh)
    assert {'handle': 'mua_a', 'pk': str(fx.user_pk('mua_a'))} in spec['accounts']
    assert 'cluster:101' in spec['known_song_keys']
    assert 'canonical:18200000000000001' in spec['known_song_keys']
    assert job.built == []


def test_after_900_seconds_the_child_is_killed_and_recorded(job):
    dj = job.dj
    dj.maybe_tick(now=NOW)
    proc = FakePopen.made[0]
    assert dj.maybe_tick(now=NOW + dj.TIMEOUT_S - 1) == 'running'
    assert dj.maybe_tick(now=NOW + dj.TIMEOUT_S) == 'timeout'
    assert proc.killed is True
    state = _state(job.tmp)
    assert state['last_run']['outcome'] == 'timeout' and state['running'] is None
    assert NOW < state['next_run_at'] <= NOW + dj.TIMEOUT_S + 2 * HOUR
    assert dj.maybe_tick(now=NOW + dj.TIMEOUT_S + 60) == 'skip'


def test_a_run_left_by_an_earlier_process_is_waited_out_then_cleared(job):
    dj = job.dj
    dj.maybe_tick(now=NOW)
    # The process restarts: the child handle is gone, state.json says running.
    dj._run = None
    _fresh_tick(dj)
    assert dj.maybe_tick(now=NOW + 60) == 'running'
    assert len(FakePopen.made) == 1
    _fresh_tick(dj)
    assert dj.maybe_tick(now=NOW + dj.TIMEOUT_S + 1) == 'failed'
    state = _state(job.tmp)
    assert state['running'] is None and state['last_run']['error'] == 'interrupted'


def test_a_run_an_earlier_process_saw_finish_is_still_published(job):
    """The desktop restarted while the child ran; the child finished on its
    own and wrote its batch.  That day's work is not thrown away."""
    dj = job.dj
    dj.maybe_tick(now=NOW)
    FakePopen.made[0].exit_with(_good_batch())
    dj._run = None
    _fresh_tick(dj)
    assert dj.maybe_tick(now=NOW + 120) == 'published'
    from integrations.trends import trending_songs as ts
    assert ts.read_latest()['songs'][0]['song_key'] == 'cluster:101'
    assert _state(job.tmp)['running'] is None
    assert len(FakePopen.made) == 1


def test_the_run_files_are_removed_after_the_run(job):
    dj = job.dj
    dj.maybe_tick(now=NOW)
    proc = FakePopen.made[0]
    spec_path, out_path = proc.argv[3], proc.argv[4]
    proc.exit_with(_good_batch())
    assert dj.maybe_tick(now=NOW + 60) == 'published'
    assert not os.path.exists(spec_path) and not os.path.exists(out_path)


def test_a_child_that_cannot_start_never_raises_into_the_tick(job, monkeypatch):
    dj = job.dj

    def _refuse(*a, **kw):
        raise OSError('exec format error')
    monkeypatch.setattr(dj.subprocess, 'Popen', _refuse)
    assert dj.maybe_tick(now=NOW) == 'failed'
    state = _state(job.tmp)
    assert state['last_run']['error'] == 'spawn_failed'
    assert state['running'] is None and state['next_run_at'] > NOW


def test_two_callers_at_once_start_one_child(job):
    dj = job.dj
    assert dj._lock.acquire(blocking=False)
    try:
        assert dj.maybe_tick(now=NOW) == 'running'
    finally:
        dj._lock.release()
    assert FakePopen.made == []
    assert dj.maybe_tick(now=NOW) == 'started'


def test_before_the_first_run_there_is_simply_no_list_yet(job):
    assert job.dj.why_no_list() == 'not_run_yet'


def test_a_failed_run_is_why_there_is_no_list(job):
    dj = job.dj
    dj.maybe_tick(now=NOW)
    FakePopen.made[0].returncode = 1
    assert dj.maybe_tick(now=NOW + 60) == 'failed'
    assert dj.why_no_list() == 'worker_failed'


def _record_credential_asks(monkeypatch):
    asked = []

    def request_credential(text, agent_id=None):
        asked.append((json.loads(text), agent_id))
        return "Asked the owner of this computer on the consent card."
    monkeypatch.setattr('hartos.ai_key_vault.request_credential', request_credential)
    return asked


def test_a_refused_login_pauses_and_asks_the_owner_to_enter_the_account_again(
        job, monkeypatch):
    """Owner 2026-09-25: "agent shd ask user when login attempts fails".
    The pause is FREEZE_S whatever is entered meanwhile; the next run uses
    what the card stored by then."""
    dj = job.dj
    asked = _record_credential_asks(monkeypatch)
    assert dj.maybe_tick(now=NOW) == 'started'
    FakePopen.made[0].exit_with(fx.batch({}, error='login'))
    assert dj.maybe_tick(now=NOW + 60) == 'failed'
    state = _state(job.tmp)
    assert state['frozen_until'] == NOW + 60 + dj.FREEZE_S
    assert sorted(a['key_name'] for a, _ in asked) == sorted(
        [dj.USERNAME_CREDENTIAL, dj.PASSWORD_CREDENTIAL])
    assert all(a['rejected'] is True and agent is None for a, agent in asked)
    assert dj.why_no_list() == 'login'
    _fresh_tick(dj)
    assert dj.maybe_tick(now=NOW + dj.FREEZE_S) == 'skip'          # the pause holds
    _fresh_tick(dj)
    assert dj.maybe_tick(now=NOW + 61 + dj.FREEZE_S) == 'started'
    assert len(FakePopen.made) == 2


def test_a_challenge_pauses_without_asking_for_the_account_again(job, monkeypatch):
    """A challenge wants a person to sign in by hand, not a new password."""
    dj = job.dj
    asked = _record_credential_asks(monkeypatch)
    dj.maybe_tick(now=NOW)
    FakePopen.made[0].exit_with(fx.batch({}, error='challenge'))
    assert dj.maybe_tick(now=NOW + 60) == 'failed'
    assert _state(job.tmp)['frozen_until'] == NOW + 60 + dj.FREEZE_S
    assert asked == []


# ── the worker's own interpreter ─────────────────────────────────────────

def test_the_worker_interpreter_is_exact_pins_with_instagrapi():
    """instagrapi and every direct dependency it names, pinned (the pins
    requirements-trends.txt carried; Nunba's install_into_venv verifies
    each by its import name, tts.package_installer._PIP_TO_IMPORT), and
    instagrapi last, so its own requirements find them in place."""
    from integrations.trends import daily_job as dj
    assert dj.WORKER_PACKAGES and all('==' in spec for spec in dj.WORKER_PACKAGES)
    names = [spec.split('==')[0] for spec in dj.WORKER_PACKAGES]
    assert set(names) >= {'pydantic', 'requests', 'curl_cffi', 'pysocks', 'pillow',
                          'pycryptodomex', 'instagrapi'}
    assert names[-1] == 'instagrapi'
    from core.venv_paths import validate_backend_name
    validate_backend_name(dj.WORKER_VENV)                 # a name the venv store takes


def test_without_the_worker_interpreter_the_owner_is_asked_first(job):
    dj = job.dj
    job.python = None
    job.consent = 'asked'
    assert dj.maybe_tick(now=NOW) == 'skip'
    assert [c for c, _ in job.asked] == ['trends:' + dj.WORKER_VENV]
    assert 'Instagram' in job.asked[0][1]
    assert job.inst['calls'] == [] and FakePopen.made == []
    assert dj.why_no_list() == 'setup_asked'


def test_a_declined_setup_installs_nothing(job):
    dj = job.dj
    job.python = None
    job.consent = 'declined'
    assert dj.maybe_tick(now=NOW) == 'skip'
    assert job.inst['calls'] == [] and FakePopen.made == []
    assert dj.why_no_list() == 'setup_declined'


def test_on_yes_nunbas_installer_builds_it_off_the_tick(job):
    dj = job.dj
    job.python = None
    assert dj.maybe_tick(now=NOW) == 'installing'
    assert job.inst['calls'] == [(dj.WORKER_VENV, list(dj.WORKER_PACKAGES))]
    assert dj.maybe_tick(now=NOW + 30) == 'installing'       # it only asks how it goes
    assert len(job.inst['calls']) == 1 and FakePopen.made == []
    assert dj.why_no_list() == 'installing'
    job.inst['status'].update(running=False, ok=True, finished_at=2.0)
    job.python = str(job.tmp / 'venvs' / dj.WORKER_VENV / 'python')
    assert dj.maybe_tick(now=NOW + 60) == 'installed'
    assert dj.maybe_tick(now=NOW + 90) == 'started'
    assert FakePopen.made[0].argv[0] == job.python
    assert job.built == []


def test_a_failed_install_backs_off_and_is_tried_again_later(job):
    dj = job.dj
    job.python = None
    assert dj.maybe_tick(now=NOW) == 'installing'
    job.inst['status'].update(running=False, ok=False, message='pip install failed',
                              finished_at=2.0)
    assert dj.maybe_tick(now=NOW + 60) == 'failed'
    state = _state(job.tmp)
    assert state['last_run']['error'] == 'install_failed'
    assert state['next_run_at'] > NOW + 60
    _fresh_tick(dj)
    assert dj.maybe_tick(now=NOW + 120) == 'skip'
    assert len(job.inst['calls']) == 1
    _fresh_tick(dj)
    assert dj.maybe_tick(now=state['next_run_at'] + 1) == 'installing'
    assert len(job.inst['calls']) == 2


def test_an_installer_that_refuses_is_a_failed_install_not_a_wait(job, monkeypatch):
    import sys
    dj = job.dj
    job.python = None
    job.consent = 'asked'
    assert dj.maybe_tick(now=NOW) == 'skip'
    assert dj.why_no_list() == 'setup_asked'
    job.consent = 'granted'

    def _refuse(backend, packages):
        raise ValueError('no packages to install')
    monkeypatch.setattr(sys.modules['tts.backend_venv'], 'install_in_background', _refuse)
    _fresh_tick(dj)
    assert dj.maybe_tick(now=NOW + 700) == 'failed'
    assert dj.why_no_list() == 'install_failed'
    assert FakePopen.made == []


def test_an_install_that_never_ends_is_given_up(job):
    dj = job.dj
    job.python = None
    assert dj.maybe_tick(now=NOW) == 'installing'
    assert dj.maybe_tick(now=NOW + dj.INSTALL_TIMEOUT_S - 1) == 'installing'
    assert dj.maybe_tick(now=NOW + dj.INSTALL_TIMEOUT_S) == 'failed'
    assert _state(job.tmp)['last_run']['error'] == 'install_timeout'


@pytest.mark.parametrize('how', ['install_failed', 'install_timeout'])
def test_after_an_install_that_did_not_finish_the_next_run_installs_again(job, how):
    """ensure_venv makes the venv's interpreter before pip runs, so a failed
    or timed-out install leaves one behind.  The next due run installs
    again; it never starts a worker there that can only exit 'dependency'
    (a whole backoff cycle lost), and never while that install may still be
    writing into the venv."""
    dj = job.dj
    job.python = None
    assert dj.maybe_tick(now=NOW) == 'installing'
    job.python = str(job.tmp / 'venvs' / dj.WORKER_VENV / 'python')
    if how == 'install_failed':
        job.inst['status'].update(running=False, ok=False, message='pip install failed',
                                  finished_at=2.0)
        assert dj.maybe_tick(now=NOW + 60) == 'failed'
    else:
        assert dj.maybe_tick(now=NOW + dj.INSTALL_TIMEOUT_S) == 'failed'
    state = _state(job.tmp)
    assert state['last_run']['error'] == how
    _fresh_tick(dj)
    assert dj.maybe_tick(now=state['next_run_at'] + 1) == 'installing'
    assert len(job.inst['calls']) == 2 and FakePopen.made == []


def test_an_install_cut_short_by_a_restart_is_installed_again_not_run(job):
    dj = job.dj
    job.python = None
    assert dj.maybe_tick(now=NOW) == 'installing'
    job.python = str(job.tmp / 'venvs' / dj.WORKER_VENV / 'python')
    # The process restarts: its install thread and Nunba's record are gone.
    dj._run = None
    job.inst['status'] = None
    assert dj.why_no_list() != 'installing'                # nothing is installing
    _fresh_tick(dj)
    assert dj.maybe_tick(now=NOW + 60) == 'installing'
    assert len(job.inst['calls']) == 2 and FakePopen.made == []


def test_a_worker_without_instagrapi_gets_its_interpreter_reinstalled(job):
    """A venv whose install was cut short (the desktop restarted mid-pip)
    starts a worker that finds no instagrapi: the next due run reinstalls."""
    dj = job.dj
    assert dj.maybe_tick(now=NOW) == 'started'
    FakePopen.made[0].exit_with(fx.batch({}, error='dependency'), rc=3)
    assert dj.maybe_tick(now=NOW + 60) == 'failed'
    state = _state(job.tmp)
    assert state['last_run']['error'] == 'dependency'
    _fresh_tick(dj)
    assert dj.maybe_tick(now=state['next_run_at'] + 1) == 'installing'
    assert job.inst['calls'] == [(dj.WORKER_VENV, list(dj.WORKER_PACKAGES))]
    assert len(FakePopen.made) == 1


def test_without_nunbas_installer_nothing_is_installed(job):
    """HARTOS run from source, with no Nunba beside it: there is no
    installer, so the job waits and says why."""
    from tests.unit.module_swap import swap_modules
    dj = job.dj
    job.python = None
    with swap_modules({'tts': None, 'tts.backend_venv': None}):
        assert dj.maybe_tick(now=NOW) == 'skip'
        assert dj.why_no_list() == 'no_installer'
    assert FakePopen.made == []


# ── the daemon's hook ────────────────────────────────────────────────────

def _daemon_db(monkeypatch):
    from unittest.mock import MagicMock
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    monkeypatch.setattr('integrations.social.models.get_db', lambda: db)
    monkeypatch.setattr('security.hive_guardrails.HiveCircuitBreaker.is_halted',
                        staticmethod(lambda: False))
    return db


def test_the_daemon_tick_calls_it_before_the_no_goals_stop(monkeypatch):
    """agent_daemon._tick runs the trends hook next to settlement, so a
    desktop with no active goals still makes her list."""
    from integrations.agent_engine.agent_daemon import AgentDaemon
    calls = []
    monkeypatch.setattr('integrations.trends.daily_job.maybe_tick',
                        lambda **kw: calls.append(kw) or 'skip')
    monkeypatch.setattr('integrations.agent_engine.dispatch.should_yield_to_user',
                        lambda: False)
    db = _daemon_db(monkeypatch)
    AgentDaemon()._tick()
    assert calls == [{}]
    db.close.assert_called()


def test_while_she_uses_the_computer_the_daemon_does_not_run_it(monkeypatch):
    """The hook sits past the daemon's yield gate: a person at the machine
    (dispatch.should_yield_to_user) keeps it from running, so the list is
    made while she is away."""
    from integrations.agent_engine.agent_daemon import AgentDaemon
    calls = []
    monkeypatch.setattr('integrations.trends.daily_job.maybe_tick',
                        lambda **kw: calls.append(kw) or 'skip')
    monkeypatch.setattr('integrations.agent_engine.dispatch.should_yield_to_user',
                        lambda: True)
    _daemon_db(monkeypatch)
    AgentDaemon()._tick()                   # a fresh daemon is not starved yet
    assert calls == []


def test_the_daemon_tick_survives_a_failing_hook(monkeypatch):
    from integrations.agent_engine.agent_daemon import AgentDaemon

    def _boom(**kw):
        raise RuntimeError('trends broke')
    monkeypatch.setattr('integrations.trends.daily_job.maybe_tick', _boom)
    monkeypatch.setattr('integrations.agent_engine.dispatch.should_yield_to_user',
                        lambda: False)
    db = _daemon_db(monkeypatch)
    AgentDaemon()._tick()          # must not raise
    db.query.assert_called()       # and the goal query still ran


# ── no node serves a trends route ────────────────────────────────────────

@pytest.mark.parametrize('tier', ['central', 'flat'])
def test_no_node_serves_a_trends_route(monkeypatch, tier):
    """The list never leaves her desktop: the agent engine registers no
    trends route on any node (the central-only API is gone)."""
    from flask import Flask
    import integrations.agent_engine as ae
    from tests.unit.module_swap import swap_modules
    monkeypatch.setattr('security.key_delegation.get_node_tier', lambda: tier)

    class _NoThread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass
    monkeypatch.setattr(threading, 'Thread', _NoThread)
    monkeypatch.setattr(ae, '_agent_engine_initialized', False)
    monkeypatch.setenv('HEVOLVE_AGENT_ENGINE_ENABLED', 'true')

    def _no_bp():
        raise RuntimeError('not under test')
    monkeypatch.setattr(ae, 'get_engine_blueprint', _no_bp)
    app = Flask('engine_' + tier)
    with swap_modules({'integrations.agent_engine.commercial_api': None,
                       'integrations.agent_engine.build_distribution': None,
                       'integrations.social.api_regional_host': None}):
        ae.init_agent_engine(app)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert not [r for r in rules if 'trends' in r]
    assert app.test_client().get('/api/trends/songs/latest').status_code == 404
