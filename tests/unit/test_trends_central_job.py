"""integrations.trends.central_job.maybe_tick: central's daily trending-songs
run, driven from the agent daemon's tick (owner ruling U6).

No thread and no LLM: a tick that finds the run due starts the worker as a
child process (``HEVOLVE_TRENDS_PYTHON -I ig_worker.py <spec> <out>``,
credentials in the child's environment, never in argv) and returns; later
ticks only poll() it.  On exit the batch is ranked through igkit.swipe and
published; after 900 s the child is killed and the run recorded as a
timeout.  It does nothing unless the node is central, the research
credentials are set, the trends interpreter exists, a watchlist exists and
the run is due (24 h after the last good one, plus jitter).  The daemon calls
it before its "no goals" stop: the list serves every desktop whether or not
central has goals of its own.
"""
import contextlib
import json
import os
import threading

import pytest

from tests.fixtures.trends import build as fx

NOW = 1792000000.0
HOUR = 3600.0


class FakePopen:
    made = []

    def __init__(self, argv, **kw):
        self.argv, self.kw = list(argv), kw
        self.pid = 4242
        self.returncode = None
        self.killed = False
        self.polls = 0
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


CREDS = {'HEVOLVE_IG_RESEARCH_USERNAME': 'research_fixture',
         'HEVOLVE_IG_RESEARCH_PASSWORD': 'pw-fixture'}


@pytest.fixture
def job(tmp_path, monkeypatch):
    from tests.conftest import refuse_all_network
    refuse_all_network(monkeypatch)
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    from core import platform_paths
    platform_paths.reset_cache()
    from integrations.trends import central_job as cj
    monkeypatch.setattr(cj, '_run', None)
    monkeypatch.setattr(cj, '_next_check_at', 0.0)
    monkeypatch.setattr('security.key_delegation.get_node_tier', lambda: 'central')
    creds = dict(CREDS)
    monkeypatch.setattr('security.secrets_manager.get_secret',
                        lambda name, default='': creds.get(name, default))
    py = tmp_path / 'trends-venv' / 'python'
    py.parent.mkdir()
    py.write_text('', encoding='utf-8')
    monkeypatch.setenv('HEVOLVE_TRENDS_PYTHON', str(py))
    (tmp_path / 'trends').mkdir(exist_ok=True)
    (tmp_path / 'trends' / 'watchlist.json').write_text(json.dumps(
        {'region': 'IN', 'niche': 'makeup',
         'accounts': ['mua_a', {'handle': 'mua_b', 'pk': '555'}, 'not a handle!']}),
        encoding='utf-8')
    FakePopen.made = []
    monkeypatch.setattr(cj.subprocess, 'Popen', FakePopen)
    built = []

    class _NoThread:
        def __init__(self, *a, **kw):
            built.append(kw.get('name'))
            raise AssertionError('central_job must not start a thread')
    monkeypatch.setattr(threading, 'Thread', _NoThread)
    yield cj, creds, built
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


def test_off_central_it_does_nothing(job, monkeypatch):
    cj, _, _ = job
    for tier in ('flat', 'regional', 'local'):
        monkeypatch.setattr('security.key_delegation.get_node_tier', lambda t=tier: t)
        assert cj.maybe_tick(now=NOW) == 'skip'
    assert FakePopen.made == []


@pytest.mark.parametrize('missing', ['HEVOLVE_IG_RESEARCH_USERNAME',
                                     'HEVOLVE_IG_RESEARCH_PASSWORD'])
def test_without_credentials_it_does_nothing(job, missing):
    cj, creds, _ = job
    creds.pop(missing)
    assert cj.maybe_tick(now=NOW) == 'skip'
    assert FakePopen.made == []


def test_without_the_trends_interpreter_it_does_nothing(job, monkeypatch, tmp_path):
    cj, _, _ = job
    monkeypatch.delenv('HEVOLVE_TRENDS_PYTHON')
    assert cj.maybe_tick(now=NOW) == 'skip'
    monkeypatch.setenv('HEVOLVE_TRENDS_PYTHON', str(tmp_path / 'no-such-python'))
    monkeypatch.setattr(cj, '_next_check_at', 0.0)
    assert cj.maybe_tick(now=NOW) == 'skip'
    assert FakePopen.made == []


def test_without_a_watchlist_it_does_nothing(job, tmp_path):
    cj, _, _ = job
    os.remove(tmp_path / 'trends' / 'watchlist.json')
    assert cj.maybe_tick(now=NOW) == 'skip'
    assert FakePopen.made == []


def test_a_due_run_starts_one_isolated_child_with_credentials_only_in_its_env(
        job, monkeypatch, tmp_path):
    cj, _, built = job
    monkeypatch.setenv('HEVOLVE_MASTER_KEY', 'parent-only-secret')
    monkeypatch.setenv('OPENAI_API_KEY', 'parent-only-key')
    assert cj.maybe_tick(now=NOW) == 'started'
    assert len(FakePopen.made) == 1
    proc = FakePopen.made[0]
    py = os.environ['HEVOLVE_TRENDS_PYTHON']
    assert proc.argv[:2] == [py, '-I']
    assert os.path.basename(proc.argv[2]) == 'ig_worker.py'
    assert os.path.isfile(proc.argv[2])
    assert len(proc.argv) == 5
    assert all('pw-fixture' not in a and 'research_fixture' not in a for a in proc.argv)
    env = proc.kw['env']
    assert env['HEVOLVE_IG_RESEARCH_USERNAME'] == 'research_fixture'
    assert env['HEVOLVE_IG_RESEARCH_PASSWORD'] == 'pw-fixture'
    assert 'HEVOLVE_MASTER_KEY' not in env and 'OPENAI_API_KEY' not in env
    assert proc.kw.get('stdin') is not None          # never the daemon's stdin
    with open(proc.argv[3], encoding='utf-8') as fh:
        spec = json.load(fh)
    assert spec['accounts'] == [{'handle': 'mua_a', 'pk': None},
                                {'handle': 'mua_b', 'pk': '555'}]
    assert spec['budget'] == 60 and spec['delay_range'] == [3, 8]
    assert 0 < spec['deadline_s'] < cj.TIMEOUT_S       # it answers before it is killed
    assert 'pw-fixture' not in json.dumps(spec)
    assert spec['session_path'] == os.path.join(str(tmp_path), 'trends', '.ig_session.json')
    assert proc.argv[4].startswith(os.path.join(str(tmp_path), 'trends'))
    assert _state(tmp_path)['running']['pid'] == 4242
    assert built == []


def test_while_the_child_runs_a_tick_only_polls(job):
    cj, _, built = job
    assert cj.maybe_tick(now=NOW) == 'started'
    proc = FakePopen.made[0]
    for i in range(1, 4):
        assert cj.maybe_tick(now=NOW + 30 * i) == 'running'
    assert len(FakePopen.made) == 1 and proc.polls == 3
    assert built == []


def test_a_clean_exit_publishes_and_schedules_tomorrow(job, tmp_path):
    cj, _, built = job
    cj.maybe_tick(now=NOW)
    FakePopen.made[0].exit_with(_good_batch())
    assert cj.maybe_tick(now=NOW + 400) == 'published'
    from integrations.trends import trending_songs as ts
    latest = ts.read_latest()
    assert latest['songs'][0]['song_key'] == 'cluster:101'
    assert (latest['region'], latest['niche']) == ('IN', 'makeup')
    state = _state(tmp_path)
    assert state['last_ok_at'] == NOW + 400
    assert NOW + 400 + 24 * HOUR <= state['next_run_at'] <= NOW + 400 + 24 * HOUR + cj.JITTER_S
    assert state['running'] is None and state['consecutive_failures'] == 0
    assert state['pks']['mua_a'] == str(fx.user_pk('mua_a'))
    assert cj.maybe_tick(now=NOW + 2 * HOUR) == 'skip'
    assert cj.maybe_tick(now=NOW + 23 * HOUR) == 'skip'
    assert cj.maybe_tick(now=state['next_run_at'] + 1) == 'started'
    assert len(FakePopen.made) == 2
    with open(FakePopen.made[1].argv[3], encoding='utf-8') as fh:
        spec = json.load(fh)
    assert {'handle': 'mua_a', 'pk': str(fx.user_pk('mua_a'))} in spec['accounts']
    assert 'cluster:101' in spec['known_song_keys']
    assert 'canonical:18200000000000001' in spec['known_song_keys']
    assert built == []


def test_after_900_seconds_the_child_is_killed_and_recorded(job, tmp_path):
    cj, _, _ = job
    cj.maybe_tick(now=NOW)
    proc = FakePopen.made[0]
    assert cj.maybe_tick(now=NOW + cj.TIMEOUT_S - 1) == 'running'
    assert cj.maybe_tick(now=NOW + cj.TIMEOUT_S) == 'timeout'
    assert proc.killed is True
    state = _state(tmp_path)
    assert state['last_run']['outcome'] == 'timeout' and state['running'] is None
    assert NOW < state['next_run_at'] <= NOW + cj.TIMEOUT_S + 2 * HOUR
    assert cj.maybe_tick(now=NOW + cj.TIMEOUT_S + 60) == 'skip'


def test_a_run_left_by_an_earlier_process_is_waited_out_then_cleared(job, tmp_path):
    cj, _, _ = job
    cj.maybe_tick(now=NOW)
    # The process restarts: the child handle is gone, state.json says running.
    cj._run = None
    cj._next_check_at = 0.0
    assert cj.maybe_tick(now=NOW + 60) == 'running'
    assert len(FakePopen.made) == 1
    cj._next_check_at = 0.0
    assert cj.maybe_tick(now=NOW + cj.TIMEOUT_S + 1) == 'failed'
    state = _state(tmp_path)
    assert state['running'] is None and state['last_run']['error'] == 'interrupted'


def test_a_run_an_earlier_process_saw_finish_is_still_published(job, tmp_path):
    """The daemon restarted while the child ran; the child finished on its
    own and wrote its batch.  That day's work is not thrown away."""
    cj, _, _ = job
    cj.maybe_tick(now=NOW)
    FakePopen.made[0].exit_with(_good_batch())
    cj._run = None
    cj._next_check_at = 0.0
    assert cj.maybe_tick(now=NOW + 120) == 'published'
    from integrations.trends import trending_songs as ts
    assert ts.read_latest()['songs'][0]['song_key'] == 'cluster:101'
    assert _state(tmp_path)['running'] is None
    assert len(FakePopen.made) == 1


def test_the_run_files_are_removed_after_the_run(job, tmp_path):
    cj, _, _ = job
    cj.maybe_tick(now=NOW)
    proc = FakePopen.made[0]
    spec_path, out_path = proc.argv[3], proc.argv[4]
    proc.exit_with(_good_batch())
    assert cj.maybe_tick(now=NOW + 60) == 'published'
    assert not os.path.exists(spec_path) and not os.path.exists(out_path)


def test_a_child_that_cannot_start_never_raises_into_the_tick(job, monkeypatch, tmp_path):
    cj, _, _ = job

    def _refuse(*a, **kw):
        raise OSError('exec format error')
    monkeypatch.setattr(cj.subprocess, 'Popen', _refuse)
    assert cj.maybe_tick(now=NOW) == 'failed'
    state = _state(tmp_path)
    assert state['last_run']['error'] == 'spawn_failed'
    assert state['running'] is None and state['next_run_at'] > NOW


def test_two_callers_at_once_start_one_child(job):
    cj, _, _ = job
    assert cj._lock.acquire(blocking=False)
    try:
        assert cj.maybe_tick(now=NOW) == 'running'
    finally:
        cj._lock.release()
    assert FakePopen.made == []
    assert cj.maybe_tick(now=NOW) == 'started'


def test_the_daemon_tick_calls_it_before_the_no_goals_stop(monkeypatch):
    """agent_daemon._tick runs the trends hook next to settlement, so a
    central with no active goals still refreshes the list."""
    from unittest.mock import MagicMock
    from integrations.agent_engine.agent_daemon import AgentDaemon
    calls = []
    monkeypatch.setattr('integrations.trends.central_job.maybe_tick',
                        lambda **kw: calls.append(kw) or 'skip')
    monkeypatch.setattr('integrations.agent_engine.dispatch.should_yield_to_user',
                        lambda: False)
    monkeypatch.setattr('security.hive_guardrails.HiveCircuitBreaker.is_halted',
                        staticmethod(lambda: False))
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    monkeypatch.setattr('integrations.social.models.get_db', lambda: db)
    AgentDaemon()._tick()
    assert calls == [{}]
    db.close.assert_called()


def test_the_daemon_tick_survives_a_failing_hook(monkeypatch):
    from unittest.mock import MagicMock
    from integrations.agent_engine.agent_daemon import AgentDaemon

    def _boom(**kw):
        raise RuntimeError('trends broke')
    monkeypatch.setattr('integrations.trends.central_job.maybe_tick', _boom)
    monkeypatch.setattr('integrations.agent_engine.dispatch.should_yield_to_user',
                        lambda: False)
    monkeypatch.setattr('security.hive_guardrails.HiveCircuitBreaker.is_halted',
                        staticmethod(lambda: False))
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    monkeypatch.setattr('integrations.social.models.get_db', lambda: db)
    AgentDaemon()._tick()          # must not raise
    db.query.assert_called()       # and the goal query still ran
