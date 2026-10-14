"""integrations.trends.watchlist: the Instagram accounts her desktop's
trending-song run watches (owner ruling A17b).

The one writer and the one reader of get_data_dir()/trends/watchlist.json.
Until it existed nothing wrote that file (critic item 2.3), so on every
install the daily job skipped with 'no_watchlist' forever.  What these
tests hold it to:

* only Instagram handles are kept (sources.HANDLE_RE, the rule the worker
  and the job already read with; a leading '@' is dropped), each once
  whatever its case, at most sources.MAX_ACCOUNTS, and what was refused is
  said, not dropped silently;
* add, remove and list; the niche is a short tag (trending_songs.clean_tag,
  the rule merge_days writes it with), and the region, the niche and each
  account's cached id survive a change;
* a file that is there but cannot be read is never written over; a damaged
  one reads as empty and is replaced by the next change;
* the daily job reads the accounts through it: what she tracks is what the
  worker is started on, and with none left the job stops.

    python -m pytest tests/unit/test_trends_watchlist.py -q -p no:cacheprovider
"""
import json
import os

import pytest

from tests.unit.test_trends_daily_job import FakePopen, NOW, job  # noqa: F401  (fixture)


@pytest.fixture
def data(tmp_path, monkeypatch):
    from tests.conftest import refuse_all_network
    refuse_all_network(monkeypatch)
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    from core import platform_paths
    platform_paths.reset_cache()
    from integrations.trends import watchlist
    yield watchlist
    platform_paths.reset_cache()


def _file(tmp):
    return os.path.join(str(tmp), 'trends', 'watchlist.json')


def _on_disk(tmp):
    with open(_file(tmp), encoding='utf-8') as fh:
        return json.load(fh)


def _handles(wl):
    return [a['handle'] for a in wl.read_watchlist()['accounts']]


# ── the writer ───────────────────────────────────────────────────────────

def test_a_fresh_install_tracks_nothing_until_accounts_are_named(data, tmp_path):
    wl = data
    assert wl.read_watchlist() == {'region': None, 'niche': None, 'accounts': []}
    assert not os.path.exists(_file(tmp_path))
    out = wl.update_watchlist(track='@glam_by_meena, bridal.chennai')
    assert out['added'] == ['glam_by_meena', 'bridal.chennai'] and out['changed'] is True
    assert out['tracked'] == ['glam_by_meena', 'bridal.chennai']
    assert _handles(wl) == ['glam_by_meena', 'bridal.chennai']
    assert wl.watchlist_path() == _file(tmp_path)
    assert [a if isinstance(a, str) else a['handle']
            for a in _on_disk(tmp_path)['accounts']] == ['glam_by_meena', 'bridal.chennai']


def test_what_is_not_an_instagram_handle_is_refused_and_said(data, tmp_path):
    wl = data
    out = wl.update_watchlist(track=['not a handle!', 'https://instagram.com/x', 'a' * 31,
                                     'sp@ce!'])
    assert out['added'] == [] and out['changed'] is False and out['tracked'] == []
    refused = [r['value'] for r in out['refused']]
    assert 'a' * 31 in refused and 'sp@ce!' in refused and 'https://instagram.com/x' in refused
    assert all(r['why'] for r in out['refused'])
    assert not os.path.exists(_file(tmp_path))          # nothing to write, nothing written


def test_a_name_with_spaces_is_one_piece_refused_never_several_accounts(data, tmp_path):
    """A handle has no spaces: "glam by meena" is a name she said, not the
    three accounts 'glam', 'by' and 'meena'."""
    wl = data
    out = wl.update_watchlist(track='glam by meena')
    assert out['added'] == [] and [r['value'] for r in out['refused']] == ['glam by meena']
    assert not os.path.exists(_file(tmp_path))
    out = wl.update_watchlist(track='@mua_one;\nmua_two')
    assert out['added'] == ['mua_one', 'mua_two']


def test_a_handle_is_tracked_once_whatever_its_case(data):
    wl = data
    wl.update_watchlist(track='Glam_By_Meena')
    out = wl.update_watchlist(track='glam_by_meena, @GLAM_BY_MEENA, mua_two')
    assert out['added'] == ['mua_two']
    assert out['already_tracked'] == ['Glam_By_Meena']
    assert _handles(wl) == ['Glam_By_Meena', 'mua_two']


def test_stop_tracking_is_case_blind_and_an_unknown_handle_is_said(data):
    wl = data
    wl.update_watchlist(track='mua_one, mua_two, mua_three')
    out = wl.update_watchlist(untrack='MUA_TWO, nobody_here')
    assert out['removed'] == ['mua_two'] and out['not_tracked'] == ['nobody_here']
    assert _handles(wl) == ['mua_one', 'mua_three'] and out['tracked'] == _handles(wl)


def test_the_list_holds_at_most_what_one_run_watches(data):
    from integrations.trends import sources
    wl = data
    names = ['mua_%02d' % i for i in range(sources.MAX_ACCOUNTS + 3)]
    out = wl.update_watchlist(track=names)
    assert out['added'] == names[:sources.MAX_ACCOUNTS]
    assert out['over_limit'] == names[sources.MAX_ACCOUNTS:]
    assert out['max_accounts'] == sources.MAX_ACCOUNTS
    assert len(_handles(wl)) == sources.MAX_ACCOUNTS
    # Stopping one makes room for one, in the same change.
    out = wl.update_watchlist(untrack='mua_00', track='mua_new')
    assert out['removed'] == ['mua_00'] and out['added'] == ['mua_new']


def test_the_niche_is_a_short_tag_and_a_bad_one_is_said(data, tmp_path):
    from integrations.trends.trending_songs import clean_tag
    wl = data
    out = wl.update_watchlist(track='mua_one', niche='bridal makeup')
    assert out['niche'] == 'bridal_makeup' and wl.read_watchlist()['niche'] == 'bridal_makeup'
    assert clean_tag(out['niche']) == 'bridal_makeup'     # the rule merge_days writes with
    out = wl.update_watchlist(niche='bridal & party!')
    assert out['niche'] == 'bridal_makeup' and out['changed'] is False
    assert [r['value'] for r in out['refused']] == ['bridal & party!']
    assert _on_disk(tmp_path)['niche'] == 'bridal_makeup'


def test_a_change_keeps_the_region_the_niche_and_each_cached_id(data, tmp_path):
    wl = data
    os.makedirs(os.path.dirname(_file(tmp_path)))
    with open(_file(tmp_path), 'w', encoding='utf-8') as fh:
        json.dump({'region': 'IN', 'niche': 'makeup',
                   'accounts': ['mua_a', {'handle': 'mua_b', 'pk': '555'}, 'not a handle!']}, fh)
    wl.update_watchlist(track='mua_c')
    got = wl.read_watchlist()
    assert (got['region'], got['niche']) == ('IN', 'makeup')
    assert got['accounts'] == [{'handle': 'mua_a', 'pk': None}, {'handle': 'mua_b', 'pk': '555'},
                               {'handle': 'mua_c', 'pk': None}]


def test_a_watchlist_that_cannot_be_read_is_never_written_over(data, tmp_path):
    """Something is at the file's place that cannot be read (here a folder):
    the reader answers empty, and a change is refused rather than guessed."""
    wl = data
    os.makedirs(_file(tmp_path))
    assert wl.read_watchlist()['accounts'] == []
    with pytest.raises(wl.WatchlistUnavailable):
        wl.update_watchlist(track='mua_one')
    assert os.path.isdir(_file(tmp_path))


def test_a_damaged_watchlist_reads_as_empty_and_the_next_change_replaces_it(data, tmp_path):
    wl = data
    os.makedirs(os.path.dirname(_file(tmp_path)))
    with open(_file(tmp_path), 'w', encoding='utf-8') as fh:
        fh.write('{damaged')
    assert wl.read_watchlist()['accounts'] == []
    out = wl.update_watchlist(track='mua_one')
    assert out['tracked'] == ['mua_one'] and _handles(wl) == ['mua_one']


# ── the daily job reads it ───────────────────────────────────────────────

def test_the_daily_job_watches_the_accounts_she_tracked(job):
    """What her agent tracks is what the worker is started on (and the niche
    rides the run), through the one reader."""
    from integrations.trends import watchlist as wl
    dj = job.dj
    os.remove(job.tmp / 'trends' / 'watchlist.json')
    assert dj.why_no_list() == 'no_watchlist'
    wl.update_watchlist(track='@Mua_C, mua_d', niche='bridal makeup')
    assert dj.maybe_tick(now=NOW) == 'started'
    with open(FakePopen.made[0].argv[3], encoding='utf-8') as fh:
        spec = json.load(fh)
    assert spec['accounts'] == [{'handle': 'Mua_C', 'pk': None}, {'handle': 'mua_d', 'pk': None}]
    with open(job.tmp / 'trends' / 'state.json', encoding='utf-8') as fh:
        assert json.load(fh)['running']['niche'] == 'bridal_makeup'


def test_when_she_stops_tracking_every_account_the_job_stops(job):
    from integrations.trends import watchlist as wl
    dj = job.dj
    wl.update_watchlist(untrack='mua_a, MUA_B')
    assert wl.read_watchlist()['accounts'] == []
    assert dj.maybe_tick(now=NOW) == 'skip'
    assert FakePopen.made == [] and dj.why_no_list() == 'no_watchlist'


def test_with_no_accounts_the_reason_tells_her_naming_them_is_the_first_step(data):
    """The 'no_watchlist' words ride her agent's reply and the reel's push:
    now that accounts can be named, they say so, still with no file, no
    mechanism and no password."""
    from integrations.trends.trending_songs_client import note_for
    note = note_for('no_watchlist')
    assert 'accounts to track' in note and 'choose a song in Instagram' in note
    for word in ('watchlist', '.json', 'password', 'consent card', 'reel_'):
        assert word not in note
