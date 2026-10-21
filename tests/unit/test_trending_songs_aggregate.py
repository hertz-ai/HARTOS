"""integrations.trends.trending_songs.rank_batch + merge_days: a day's worker
batch becomes central's published song list (hart.trending_songs.v1).

The maths is igkit.swipe's, the one implementation (Gate 4; plan WS-5/WS-6):
each reel's multiple over its own account's median of the newest 12 reels
(None with fewer than 6), each song scored by summing, over distinct
accounts, that account's best weight * log1p(multiple), the weight being a
recency decay.  These tests pin what this module adds around it: which reels
count, the dedupe, the window, the decay, the track lookups, the language
hint, the ranking ties and the day-to-day carry-over.
"""
import copy
import json
import math

import pytest

from tests.fixtures.trends import build as fx

NOW = 1792000000.0          # 2026-10-14T...Z
DAY = 86400.0
BASE = [1000] * 11


def _rank(batch, **kw):
    from integrations.trends.trending_songs import rank_batch
    return rank_batch(batch, now=NOW, **kw)


def _song(rank, key):
    found = [s for s in rank['songs'] if s['song_key'] == key]
    return found[0] if found else None


def _acct(handle, plays, songs, first_pk, **kw):
    return fx.account(handle, plays, now=NOW, songs=songs, first_pk=first_pk, **kw)


def test_a_reel_is_measured_against_its_own_accounts_median():
    reels = _acct('mua_a', [6000] + BASE, [101] + [None] * 11, 1000)
    rank = _rank(fx.batch({'mua_a': reels}))
    song = rank['songs'][0]
    assert song['song_key'] == 'cluster:101'
    assert song['max_outlier'] == 6.0
    assert (song['accounts'], song['reels']) == (1, 1)
    assert song['total_plays'] == 6000
    assert song['score'] == round(math.log1p(6.0), 3)
    assert song['examples'] == [{'code': 'C1000', 'plays': 6000, 'outlier': 6.0}]
    assert song['title'] == 'Saree Song' and song['artist'] == 'Fixture Artist'
    assert rank['dropped']['no_music'] == 11
    assert (rank['accounts_ok'], rank['accounts_total'], rank['accounts_ranked']) == (1, 1, 1)


def test_an_account_with_fewer_than_six_reels_is_not_ranked():
    thin = _acct('mua_thin', [50000, 900, 900, 900, 900], [202, None, None, None, None], 2000)
    full = _acct('mua_a', [6000] + BASE, [101] + [None] * 11, 1000)
    rank = _rank(fx.batch({'mua_thin': thin, 'mua_a': full}))
    assert _song(rank, 'cluster:202') is None
    assert _song(rank, 'cluster:101') is not None
    assert rank['accounts_ranked'] == 1


def test_a_muted_song_is_dropped_and_counted():
    songs = [{'kind': 'muted', 'cluster': 303}] + [None] * 11
    reels = _acct('mua_m', [9000] + BASE, songs, 3000)
    rank = _rank(fx.batch({'mua_m': reels}))
    assert rank['songs'] == []
    assert rank['dropped']['should_mute'] == 1


def test_play_count_none_uses_view_count_in_the_ranking():
    reels = _acct('mua_v', [None] + BASE, [104] + [None] * 11, 4000)
    reels[0]['view_count'] = 5000
    rank = _rank(fx.batch({'mua_v': reels}))
    assert _song(rank, 'cluster:104')['max_outlier'] == 5.0


def test_a_reel_seen_twice_is_counted_once():
    reels = _acct('mua_a', [6000] + BASE, [101] + [None] * 11, 1000)
    again = copy.deepcopy(reels[0])
    other = _acct('mua_b', [6000] + BASE, [None] * 12, 5000)
    rank = _rank(fx.batch({'mua_a': reels + [again], 'mua_b': other + [copy.deepcopy(again)]}))
    song = _song(rank, 'cluster:101')
    assert (song['reels'], song['accounts'], song['total_plays']) == (1, 1, 6000)


def test_ties_break_on_accounts_then_best_multiple_then_song_key():
    """log1p(3) == 2 * log1p(1): one account at 3x ties two accounts at 1x,
    and the song more accounts used ranks first.  Equal again on both: the
    song key decides, so the order never depends on dict order."""
    batch = fx.batch({
        'mua_x': _acct('mua_x', [3000] + BASE, [601] + [None] * 11, 6000),
        'mua_y1': _acct('mua_y1', [1000] + BASE, [602] + [None] * 11, 6100),
        'mua_y2': _acct('mua_y2', [1000] + BASE, [602] + [None] * 11, 6200),
        'mua_p': _acct('mua_p', [2000] + BASE, [702] + [None] * 11, 7000),
        'mua_q': _acct('mua_q', [2000] + BASE, [701] + [None] * 11, 7100),
    })
    keys = [s['song_key'] for s in _rank(batch)['songs']]
    assert keys.index('cluster:602') < keys.index('cluster:601')
    assert keys.index('cluster:701') < keys.index('cluster:702')
    scores = {s['song_key']: s['score'] for s in _rank(batch)['songs']}
    assert scores['cluster:601'] == scores['cluster:602']


def test_only_the_last_window_days_count_toward_songs_but_all_toward_the_median():
    plays = [2000, 1000, 1000, 1000, 1000, 1000, 1000, 1000, 90000, 1000, 1000, 1000]
    songs = [405, None, None, None, None, None, None, None, 404, None, None, None]
    reels = _acct('mua_w', plays, songs, 8000, step_s=DAY)
    rank = _rank(fx.batch({'mua_w': reels}))
    assert _song(rank, 'cluster:404') is None             # 8 days old
    assert _song(rank, 'cluster:405')['max_outlier'] == 2.0   # median still 1000


def test_older_reels_weigh_less():
    """Half-life of 3.5 days: the same multiple 3.5 days ago scores half."""
    fresh = _acct('mua_f', [4000] + BASE, [501] + [None] * 11, 9000)
    old = _acct('mua_o', [1000, 1000, 1000, 1000, 1000, 1000, 1000, 4000, 1000, 1000, 1000, 1000],
                [None] * 7 + [502] + [None] * 4, 9100, step_s=DAY / 2)
    rank = _rank(fx.batch({'mua_f': fresh, 'mua_o': old}))
    assert _song(rank, 'cluster:501')['score'] == round(math.log1p(4.0), 3)
    assert _song(rank, 'cluster:502')['score'] == round(0.5 * math.log1p(4.0), 3)


def test_a_looked_up_track_names_the_song_and_joins_its_cluster():
    """A reel named only by music_canonical_id is resolved with the track the
    worker looked up: its cluster id becomes the key (so it merges with the
    reels that carried the cluster), and its title and highlight fill in."""
    track = fx.load()['track']
    canon = _acct('mua_c', [5000] + BASE, [{'kind': 'canonical_only'}] + [None] * 11, 10000)
    licensed = _acct('mua_l', [3000] + BASE,
                     [{'cluster': 660000000000044, 'title': 'Mallige Song',
                       'highlights': [45000]}] + [None] * 11, 11000)
    rank = _rank(fx.batch({'mua_c': canon, 'mua_l': licensed},
                          tracks={'canonical:18200000000000044': track}))
    song = _song(rank, 'cluster:660000000000044')
    assert song is not None and _song(rank, 'canonical:18200000000000044') is None
    assert (song['accounts'], song['reels']) == (2, 2)
    assert song['title'] == 'Mallige Song' and song['artist'] == 'Fixture Singer'
    assert song['highlight_start_ms'] == [45000] and song['suggested_start_ms'] == 45000
    assert song['music_canonical_id'] == '18200000000000044'


def test_a_song_named_only_by_its_attribution_joins_its_cluster():
    """clips_music_attribution_info.audio_id is the song's audio asset id; a
    reel that carried the full music_asset_info for that asset names its
    cluster, so both reels count for one song, not two."""
    lic = _acct('mua_l', [3000] + BASE, [9911] + [None] * 11, 14000)
    lic[0]['clips_metadata']['music_info']['music_asset_info']['id'] = '990000000000000003'
    att = _acct('mua_t', [4000] + BASE, [{'kind': 'attribution_only'}] + [None] * 11, 14100)
    rank = _rank(fx.batch({'mua_l': lic, 'mua_t': att}))
    song = _song(rank, 'cluster:9911')
    assert (song['accounts'], song['reels']) == (2, 2)
    assert _song(rank, 'audio:990000000000000003') is None


def test_a_song_whose_name_never_resolved_is_not_listed():
    """A reel known only by a canonical id whose track lookup did not happen
    has no title: a suggestion she cannot search for is not one."""
    canon = _acct('mua_c', [5000] + BASE, [{'kind': 'canonical_only'}] + [None] * 11, 15000)
    rank = _rank(fx.batch({'mua_c': canon}))
    assert rank['songs'] == [] and rank['dropped']['untitled'] == 1


def test_yesterdays_list_names_a_song_without_a_new_lookup():
    """The worker skips lookups for canonical ids central already resolved
    (known_song_keys); the previous list resolves them here instead."""
    canon = _acct('mua_c', [5000] + BASE, [{'kind': 'canonical_only'}] + [None] * 11, 16000)
    prev = {'schema': 'hart.trending_songs.v1', 'date': '2026-10-13',
            'songs': [{'song_key': 'cluster:660000000000044',
                       'audio_cluster_id': '660000000000044',
                       'music_canonical_id': '18200000000000044', 'title': 'Mallige Song',
                       'artist': 'Fixture Singer', 'highlight_start_ms': [45000],
                       'duration_ms': 200000, 'is_explicit': False,
                       'first_seen': '2026-10-10', 'last_seen': '2026-10-13'}]}
    rank = _rank(fx.batch({'mua_c': canon}), previous=prev)
    song = _song(rank, 'cluster:660000000000044')
    assert song is not None and song['title'] == 'Mallige Song'
    assert song['suggested_start_ms'] == 45000 and song['artist'] == 'Fixture Singer'


def test_the_known_song_keys_name_every_alias_of_the_last_list():
    from integrations.trends.trending_songs import known_song_keys
    prev = {'schema': 'hart.trending_songs.v1',
            'songs': [{'song_key': 'cluster:1', 'music_canonical_id': '182', 'audio_asset_id': '77'},
                      {'song_key': 'original:5'}, 'junk', {'song_key': ''}]}
    assert set(known_song_keys(prev)) == {'cluster:1', 'canonical:182', 'audio:77', 'original:5'}
    assert known_song_keys(None) == [] and known_song_keys({'songs': 'x'}) == []


def test_the_suggested_start_is_the_first_highlight_or_zero():
    with_hl = _acct('mua_h', [3000] + BASE, [{'cluster': 801, 'highlights': [61000, 9000]}]
                    + [None] * 11, 12000)
    without = _acct('mua_n', [3000] + BASE, [{'cluster': 802, 'highlights': []}]
                    + [None] * 11, 12100)
    rank = _rank(fx.batch({'mua_h': with_hl, 'mua_n': without}))
    assert _song(rank, 'cluster:801')['suggested_start_ms'] == 61000
    assert _song(rank, 'cluster:802')['suggested_start_ms'] == 0


def test_a_native_script_title_carries_its_language_hint():
    reels = _acct('mua_t', [3000, 3000] + [1000] * 10,
                  [{'cluster': 901, 'title': 'மல்லிகைப் பூ வாசம்'},
                   {'cluster': 902, 'title': 'Saree Song'}] + [None] * 10, 13000)
    rank = _rank(fx.batch({'mua_t': reels}))
    assert _song(rank, 'cluster:901')['language_hint'] == 'ta'
    assert _song(rank, 'cluster:902')['language_hint'] is None


def test_failed_accounts_are_counted_but_bring_no_reels():
    reels = _acct('mua_a', [6000] + BASE, [101] + [None] * 11, 1000)
    rank = _rank(fx.batch({'mua_a': reels}, failed={'mua_gone': 'not_found'}))
    assert (rank['accounts_ok'], rank['accounts_total']) == (1, 2)


def test_the_list_is_capped():
    from integrations.trends import trending_songs as ts
    accounts = {}
    for i in range(ts.MAX_SONGS + 5):
        h = 'mua_cap_%d' % i
        accounts[h] = _acct(h, [2000 + i] + BASE, [20000 + i] + [None] * 11, 100000 + 100 * i)
    assert len(_rank(fx.batch(accounts))['songs']) == ts.MAX_SONGS


def test_merge_days_carries_first_seen_and_validates():
    from integrations.reel_studio.contracts import validate_trending_songs
    from integrations.trends.trending_songs import merge_days
    reels = _acct('mua_a', [6000, 4000] + [1000] * 10, [101, 102] + [None] * 10, 1000)
    rank = _rank(fx.batch({'mua_a': reels}))
    prev = {'schema': 'hart.trending_songs.v1', 'date': '2026-10-13',
            'songs': [{'song_key': 'cluster:101', 'first_seen': '2026-10-10',
                       'last_seen': '2026-10-13'},
                      {'song_key': 'cluster:999', 'first_seen': '2026-10-01',
                       'last_seen': '2026-10-13'}]}
    doc = merge_days(rank, prev, today='2026-10-14')
    assert validate_trending_songs(doc) == []
    assert doc['date'] == '2026-10-14' and doc['stale'] is False
    assert doc['window_days'] == 7 and doc['source'] == 'instagrapi'
    by_key = {s['song_key']: s for s in doc['songs']}
    assert by_key['cluster:101']['first_seen'] == '2026-10-10'
    assert by_key['cluster:102']['first_seen'] == '2026-10-14'
    assert all(s['last_seen'] == '2026-10-14' for s in doc['songs'])
    assert 'cluster:999' not in by_key
    text = json.dumps(doc)
    assert 'example.invalid' not in text and 'MPD' not in text


def test_merge_days_with_no_previous_list_starts_fresh():
    from integrations.trends.trending_songs import merge_days
    reels = _acct('mua_a', [6000] + BASE, [101] + [None] * 11, 1000)
    doc = merge_days(_rank(fx.batch({'mua_a': reels})), None, today='2026-10-14')
    assert doc['songs'][0]['first_seen'] == '2026-10-14'


@pytest.mark.parametrize('bad', [None, 'junk', [], {'accounts': 'x'}])
def test_a_batch_that_is_not_one_ranks_nothing(bad):
    rank = _rank(bad)
    assert rank['songs'] == [] and rank['accounts_ok'] == 0
