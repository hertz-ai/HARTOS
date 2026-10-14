"""What a bad day does to central's trending-songs run.

Two halves:
* sources.collect, the loop the worker runs over the watchlist, driven here
  by a fake TrendSource returning each error code (goals-trends 2.5): one
  account that fails is skipped; a challenge or a failed login stops the run
  (and freezes it); throttled or feedback stops it (and cools it down); the
  request budget caps the run; tracks are looked up only for songs not yet
  known.
* central_job finishing a run whose batch carries those outcomes: freeze or
  cool-down in state.json, the owner told (NotificationService, mocked) of a
  freeze, yesterday's list left in place marked stale, and no second worker
  started while the freeze holds.
"""
import json
import os

import pytest

from tests.fixtures.trends import build as fx

NOW = 1792000000.0


class FakeSource:
    """A TrendSource that answers from tables and counts its requests."""

    def __init__(self, reels=None, errors=None, tracks=None, track_errors=None):
        self.reels = reels or {}
        self.errors = errors or {}
        self.tracks = tracks or {}
        self.track_errors = track_errors or {}
        self.used = 0
        self.fetched = []
        self.looked = []

    def fetch_reels(self, handle, pk_hint):
        from integrations.trends.sources import TrendSourceError
        self.used += 1
        self.fetched.append((handle, pk_hint))
        if handle in self.errors:
            raise TrendSourceError(self.errors[handle])
        return {'pk': pk_hint or 'pk-' + handle, 'reels': list(self.reels.get(handle, []))}

    def lookup_track(self, song_key):
        from integrations.trends.sources import TrendSourceError
        self.used += 1
        self.looked.append(song_key)
        if song_key in self.track_errors:
            raise TrendSourceError(self.track_errors[song_key])
        return self.tracks.get(song_key)

    def requests_used(self):
        return self.used


def _accounts(*handles):
    return [{'handle': h, 'pk': None} for h in handles]


def _collect(source, accounts, **kw):
    from integrations.trends.sources import collect
    return collect(source, accounts, **kw)


@pytest.mark.parametrize('code', ['not_found', 'transport', 'unknown'])
def test_one_failing_account_is_skipped_and_the_rest_run(code):
    src = FakeSource(reels={'mua_a': [{'pk': 1}], 'mua_c': [{'pk': 3}]}, errors={'mua_b': code})
    batch = _collect(src, _accounts('mua_a', 'mua_b', 'mua_c'))
    assert batch['error'] is None
    rows = {r['handle']: r for r in batch['accounts']}
    assert rows['mua_b']['ok'] is False and rows['mua_b']['error'] == code
    assert rows['mua_a']['ok'] and rows['mua_c']['ok']
    assert (batch['accounts_ok'], batch['accounts_total']) == (2, 3)


@pytest.mark.parametrize('code', ['challenge', 'login', 'throttled', 'feedback'])
def test_a_stopping_error_ends_the_run_where_it_happened(code):
    src = FakeSource(reels={'mua_a': [{'pk': 1}]}, errors={'mua_b': code})
    batch = _collect(src, _accounts('mua_a', 'mua_b', 'mua_c'))
    assert batch['error'] == code
    assert [h for h, _ in src.fetched] == ['mua_a', 'mua_b']
    rows = {r['handle']: r for r in batch['accounts']}
    assert rows['mua_c']['error'] == 'not_run' and rows['mua_c']['ok'] is False
    assert src.looked == []


def test_the_request_budget_caps_the_run():
    src = FakeSource(reels={h: [] for h in ('a1', 'a2', 'a3', 'a4', 'a5')})
    batch = _collect(src, _accounts('a1', 'a2', 'a3', 'a4', 'a5'), budget=3)
    assert len(src.fetched) == 3 and src.used == 3
    assert batch['budget_exhausted'] is True
    assert [r['error'] for r in batch['accounts']][3:] == ['budget', 'budget']
    assert batch['requests'] == 3


def test_at_most_max_accounts_are_watched():
    from integrations.trends.sources import MAX_ACCOUNTS
    handles = ['mua_%d' % i for i in range(MAX_ACCOUNTS + 5)]
    src = FakeSource(reels={h: [] for h in handles})
    batch = _collect(src, _accounts(*handles), budget=1000)
    assert len(src.fetched) == MAX_ACCOUNTS and batch['accounts_total'] == MAX_ACCOUNTS


def test_a_cached_pk_is_handed_to_the_source():
    src = FakeSource(reels={'mua_a': []})
    batch = _collect(src, [{'handle': 'mua_a', 'pk': '777'}])
    assert src.fetched == [('mua_a', '777')]
    assert batch['accounts'][0]['pk'] == '777'


def test_tracks_are_looked_up_once_and_only_for_unknown_songs():
    canon = fx.reel('canonical_only', pk=1, handle='mua_a', plays=10, taken_at=int(NOW))
    canon2 = fx.reel('canonical_only', pk=2, handle='mua_b', plays=10, taken_at=int(NOW))
    other = fx.reel('canonical_only', pk=3, handle='mua_b', plays=10, taken_at=int(NOW))
    other['clips_metadata']['music_canonical_id'] = '18200000000000099'
    licensed = fx.reel('licensed', pk=4, handle='mua_b', plays=10, taken_at=int(NOW))
    track = fx.load()['track']
    src = FakeSource(reels={'mua_a': [canon], 'mua_b': [canon2, other, licensed]},
                     tracks={'canonical:18200000000000044': track})
    batch = _collect(src, _accounts('mua_a', 'mua_b'),
                     known_song_keys=['canonical:18200000000000099'])
    assert src.looked == ['canonical:18200000000000044']
    got = batch['tracks']['canonical:18200000000000044']
    assert got['audio_cluster_id'] in (660000000000044, '660000000000044')
    assert 'example.invalid' not in json.dumps(got) and 'MPD' not in json.dumps(got)


def test_a_track_lookup_that_is_not_found_is_skipped_but_a_challenge_stops():
    canon = fx.reel('canonical_only', pk=1, handle='mua_a', plays=10, taken_at=int(NOW))
    src = FakeSource(reels={'mua_a': [canon]},
                     track_errors={'canonical:18200000000000044': 'not_found'})
    batch = _collect(src, _accounts('mua_a'))
    assert batch['error'] is None and batch['tracks'] == {}
    src = FakeSource(reels={'mua_a': [canon]},
                     track_errors={'canonical:18200000000000044': 'challenge'})
    assert _collect(src, _accounts('mua_a'))['error'] == 'challenge'


def test_the_budget_also_caps_track_lookups():
    canon = fx.reel('canonical_only', pk=1, handle='mua_a', plays=10, taken_at=int(NOW))
    src = FakeSource(reels={'mua_a': [canon]})
    batch = _collect(src, _accounts('mua_a'), budget=1)
    assert src.looked == [] and batch['budget_exhausted'] is True


def test_three_transport_failures_in_a_row_stop_the_run():
    """The network is down, not one account: stop instead of spending each
    remaining account's retries on it."""
    src = FakeSource(reels={'mua_e': [{'pk': 5}]},
                     errors={'mua_a': 'transport', 'mua_b': 'transport', 'mua_c': 'transport'})
    batch = _collect(src, _accounts('mua_a', 'mua_b', 'mua_c', 'mua_d', 'mua_e'))
    assert batch['error'] == 'transport'
    assert [h for h, _ in src.fetched] == ['mua_a', 'mua_b', 'mua_c']
    rows = {r['handle']: r for r in batch['accounts']}
    assert rows['mua_d']['error'] == 'not_run' and rows['mua_e']['error'] == 'not_run'


def test_transport_failures_between_successes_do_not_stop_the_run():
    src = FakeSource(reels={'mua_b': [], 'mua_d': []},
                     errors={'mua_a': 'transport', 'mua_c': 'transport', 'mua_e': 'transport'})
    batch = _collect(src, _accounts('mua_a', 'mua_b', 'mua_c', 'mua_d', 'mua_e'))
    assert batch['error'] is None and len(src.fetched) == 5
    assert (batch['accounts_ok'], batch['accounts_total']) == (2, 5)


def test_the_deadline_ends_the_run_before_the_job_kills_it():
    """The worker stops asking at its own deadline and still writes its
    batch, so central never has to kill it to get an answer."""
    ticks = iter([0.0, 50.0, 150.0, 150.0, 150.0, 150.0])
    src = FakeSource(reels={h: [] for h in ('a1', 'a2', 'a3', 'a4')})
    batch = _collect(src, _accounts('a1', 'a2', 'a3', 'a4'), deadline=100.0,
                     clock=lambda: next(ticks))
    assert [h for h, _ in src.fetched] == ['a1', 'a2']
    assert batch['error'] is None and batch['deadline_reached'] is True
    assert [r['error'] for r in batch['accounts']] == [None, None, 'deadline', 'deadline']


def test_a_source_that_raises_something_else_skips_that_account_as_unknown():
    class Broken(FakeSource):
        def fetch_reels(self, handle, pk_hint):
            if handle == 'mua_a':
                self.used += 1
                raise ValueError('a bug in the source')
            return super().fetch_reels(handle, pk_hint)
    src = Broken(reels={'mua_b': []})
    batch = _collect(src, _accounts('mua_a', 'mua_b'))
    rows = {r['handle']: r for r in batch['accounts']}
    assert rows['mua_a']['error'] == 'unknown' and rows['mua_b']['ok'] is True
    assert batch['error'] is None


def test_the_slimmed_track_carries_no_url_or_manifest():
    from integrations.trends.sources import slim_track
    track = fx.load()['track']
    slim = slim_track(track)
    assert slim['title'] == 'Mallige Song' and slim['highlight_start_times_in_ms'] == [45000]
    text = json.dumps(slim)
    assert 'example.invalid' not in text and 'MPD' not in text
    assert slim_track(None) is None and slim_track('x') is None


# ── central_job finishing a bad run ──────────────────────────────────────

class FakePopen:
    made = []

    def __init__(self, argv, **kw):
        self.argv, self.kw = list(argv), kw
        self.pid = 4242
        self.returncode = None
        self.killed = False
        FakePopen.made.append(self)

    def poll(self):
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
    creds = {'HEVOLVE_IG_RESEARCH_USERNAME': 'research_fixture',
             'HEVOLVE_IG_RESEARCH_PASSWORD': 'pw-fixture'}
    monkeypatch.setattr('security.secrets_manager.get_secret',
                        lambda name, default='': creds.get(name, default))
    py = tmp_path / 'trends-venv' / 'python'
    py.parent.mkdir()
    py.write_text('', encoding='utf-8')
    monkeypatch.setenv('HEVOLVE_TRENDS_PYTHON', str(py))
    (tmp_path / 'trends').mkdir(exist_ok=True)
    (tmp_path / 'trends' / 'watchlist.json').write_text(json.dumps(
        {'region': 'IN', 'niche': 'makeup', 'accounts': ['mua_a', 'mua_b', 'mua_c']}),
        encoding='utf-8')
    FakePopen.made = []
    monkeypatch.setattr(cj.subprocess, 'Popen', FakePopen)
    told = []
    monkeypatch.setattr('integrations.agent_engine.capability_setup.setup_owner',
                        lambda: 'owner_fixture')
    import contextlib

    @contextlib.contextmanager
    def _session(commit=True):
        yield 'db'
    monkeypatch.setattr('integrations.social.models.db_session', _session)
    monkeypatch.setattr('integrations.social.services.NotificationService.create',
                        staticmethod(lambda db, user_id, type, **kw: told.append(
                            (user_id, type, kw.get('message', '')))))
    yield cj, told
    platform_paths.reset_cache()


def _yesterday():
    from integrations.trends import trending_songs as ts
    doc = {'schema': 'hart.trending_songs.v1', 'date': '2026-10-13',
           'generated_at': '2026-10-13T03:06:00Z', 'source': 'instagrapi', 'window_days': 7,
           'stale': False, 'accounts_ok': 3, 'accounts_total': 3,
           'songs': [{'song_key': 'cluster:1', 'title': 'Saree Song', 'artist': 'A',
                      'suggested_start_ms': 0, 'score': 1.0, 'accounts': 1, 'reels': 1,
                      'highlight_start_ms': [], 'language_hint': None}]}
    assert ts.publish(doc) is True
    return doc


def _state(tmp_path):
    with open(tmp_path / 'trends' / 'state.json', encoding='utf-8') as fh:
        return json.load(fh)


def test_a_challenge_freezes_the_job_tells_the_owner_and_marks_the_list_stale(job, tmp_path):
    cj, told = job
    _yesterday()
    assert cj.maybe_tick(now=NOW) == 'started'
    FakePopen.made[0].exit_with(fx.batch({}, failed={'mua_a': 'challenge'}, error='challenge'))
    assert cj.maybe_tick(now=NOW + 60) == 'failed'
    state = _state(tmp_path)
    assert state['frozen_until'] >= NOW + 60 + cj.FREEZE_S
    assert state['last_run']['error'] == 'challenge'
    assert [(u, t) for u, t, _ in told] == [('owner_fixture', 'trends_paused')]
    assert 'research_fixture' not in told[0][2] and 'pw-fixture' not in told[0][2]
    from integrations.trends import trending_songs as ts
    latest = ts.read_latest()
    assert latest['stale'] is True and latest['stale_reason'] == 'challenge'
    assert latest['date'] == '2026-10-13'
    assert cj.maybe_tick(now=NOW + 3600) == 'skip'
    assert cj.maybe_tick(now=NOW + 20 * 3600) == 'skip'
    assert len(FakePopen.made) == 1


@pytest.mark.parametrize('code', ['throttled', 'feedback'])
def test_throttling_cools_down_for_six_to_twenty_four_hours(job, tmp_path, code):
    cj, told = job
    _yesterday()
    cj.maybe_tick(now=NOW)
    FakePopen.made[0].exit_with(fx.batch({}, error=code))
    assert cj.maybe_tick(now=NOW + 60) == 'failed'
    state = _state(tmp_path)
    assert NOW + 60 + 6 * 3600 <= state['next_run_at'] <= NOW + 60 + 24 * 3600
    assert not state.get('frozen_until')
    assert told == []
    from integrations.trends import trending_songs as ts
    assert ts.read_latest()['stale_reason'] == code


def test_a_partial_run_is_not_published_and_retried_sooner(job, tmp_path):
    cj, _ = job
    good = _yesterday()
    cj.maybe_tick(now=NOW)
    reels = fx.account('mua_a', [6000] + [1000] * 11, now=NOW, songs=[101] + [None] * 11,
                       first_pk=1000)
    FakePopen.made[0].exit_with(fx.batch({'mua_a': reels},
                                         failed={'mua_b': 'transport', 'mua_c': 'transport'}))
    assert cj.maybe_tick(now=NOW + 60) == 'failed'
    from integrations.trends import trending_songs as ts
    latest = ts.read_latest()
    assert latest['songs'] == good['songs'] and latest['stale'] is True
    state = _state(tmp_path)
    assert NOW + 60 < state['next_run_at'] <= NOW + 60 + 2 * 3600
    assert state['consecutive_failures'] == 1


def test_a_worker_that_died_without_output_fails_the_run(job, tmp_path):
    cj, _ = job
    cj.maybe_tick(now=NOW)
    FakePopen.made[0].returncode = 1
    assert cj.maybe_tick(now=NOW + 60) == 'failed'
    assert _state(tmp_path)['last_run']['error'] == 'worker_failed'


def test_garbage_output_fails_the_run(job, tmp_path):
    cj, _ = job
    cj.maybe_tick(now=NOW)
    proc = FakePopen.made[0]
    with open(proc.argv[-1], 'w', encoding='utf-8') as fh:
        fh.write('{"schema": "something else"')
    proc.returncode = 0
    assert cj.maybe_tick(now=NOW + 60) == 'failed'
    assert _state(tmp_path)['last_run']['error'] == 'bad_output'
