"""integrations.trends.trending_songs.publish / mark_stale / read_latest: the
one writer of central's song list.

Files under get_data_dir()/trends/songs/: latest.json plus one
YYYY-MM-DD.json per published day, each written with
core.file_cache.atomic_json_write; 30 days are kept (goals-trends 2.3/2.5).
Refused: a list contracts.validate_trending_songs rejects (it carries a URL
or session data, or is malformed), a run where fewer than 60% of the
watched accounts answered, and an empty list in place of a non-empty one.
A failed day leaves yesterday's list in place, marked stale.
"""
import json
import os

import pytest


@pytest.fixture
def ts(tmp_path, monkeypatch):
    from tests.conftest import refuse_all_network
    refuse_all_network(monkeypatch)
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    from core import platform_paths
    platform_paths.reset_cache()
    from integrations.trends import trending_songs
    yield trending_songs
    platform_paths.reset_cache()


def _songs_dir(tmp_path):
    return tmp_path / 'trends' / 'songs'


def _doc(date='2026-10-14', songs=None, ok=19, total=20):
    if songs is None:
        songs = [{'song_key': 'cluster:123', 'audio_cluster_id': '123', 'audio_asset_id': '456',
                  'music_canonical_id': None, 'title': 'Saree Song', 'artist': 'Fixture Artist',
                  'is_original_audio': False, 'is_explicit': False, 'duration_ms': 180000,
                  'highlight_start_ms': [38500], 'suggested_start_ms': 38500,
                  'language_hint': None, 'score': 4.2, 'accounts': 3, 'reels': 4,
                  'max_outlier': 6.1, 'median_outlier': 3.0, 'total_plays': 1234567,
                  'first_seen': '2026-10-10', 'last_seen': date,
                  'examples': [{'code': 'Cfix0001', 'plays': 120000, 'outlier': 6.1}]}]
    return {'schema': 'hart.trending_songs.v1', 'date': date,
            'generated_at': date + 'T03:06:00Z', 'region': 'IN', 'niche': 'makeup',
            'source': 'instagrapi', 'window_days': 7, 'stale': False,
            'accounts_ok': ok, 'accounts_total': total, 'accounts_ranked': ok,
            'dropped': {'should_mute': 3, 'no_music': 7, 'no_views': 0}, 'songs': songs}


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def test_publish_writes_latest_and_the_day(ts, tmp_path):
    doc = _doc()
    assert ts.publish(doc) is True
    d = _songs_dir(tmp_path)
    assert _read(d / 'latest.json') == doc
    assert _read(d / '2026-10-14.json') == doc
    assert ts.read_latest() == doc
    assert not [n for n in os.listdir(d) if n.endswith('.tmp')]


def test_an_empty_list_never_replaces_a_good_one(ts, tmp_path):
    good = _doc(date='2026-10-13')
    assert ts.publish(good) is True
    assert ts.publish(_doc(songs=[])) is False
    assert ts.read_latest() == good
    assert not (_songs_dir(tmp_path) / '2026-10-14.json').exists()


def test_an_empty_first_list_is_still_published(ts):
    assert ts.publish(_doc(songs=[])) is True
    assert ts.read_latest()['songs'] == []


@pytest.mark.parametrize('ok, total, published', [(11, 20, False), (12, 20, True),
                                                  (0, 20, False), (3, 5, True)])
def test_a_partial_run_below_sixty_percent_is_not_published(ts, ok, total, published):
    assert ts.publish(_doc(ok=ok, total=total)) is published
    assert (ts.read_latest() is not None) is published


def test_a_list_carrying_a_url_or_session_data_is_refused(ts):
    doc = _doc()
    doc['songs'][0]['preview_url'] = 'https://example.invalid/a.m4a'
    assert ts.publish(doc) is False
    doc = _doc()
    doc['session'] = {'sessionid': 'x'}
    assert ts.publish(doc) is False
    doc = _doc()
    doc['songs'][0]['examples'][0]['code'] = 'https://www.instagram.com/reel/C1'
    assert ts.publish(doc) is False
    assert ts.read_latest() is None


@pytest.mark.parametrize('bad', [None, 'x', {'schema': 'hart.trending_songs.v9'},
                                 dict(_doc(), date='14/10/2026')])
def test_a_malformed_list_is_refused(ts, bad):
    assert ts.publish(bad) is False
    assert ts.read_latest() is None


def test_days_older_than_keep_days_are_pruned(ts, tmp_path):
    d = _songs_dir(tmp_path)
    d.mkdir(parents=True)
    for name in ('2026-09-10.json', '2026-09-13.json', '2026-09-14.json', '2026-10-01.json'):
        (d / name).write_text('{}', encoding='utf-8')
    (d / 'notes.txt').write_text('keep me', encoding='utf-8')
    assert ts.publish(_doc(date='2026-10-14'), keep_days=30) is True
    left = sorted(os.listdir(d))
    assert left == ['2026-09-14.json', '2026-10-01.json', '2026-10-14.json',
                    'latest.json', 'notes.txt']


def test_mark_stale_keeps_the_list_and_says_why(ts):
    assert ts.mark_stale('challenge') is False          # nothing published yet
    doc = _doc()
    ts.publish(doc)
    assert ts.mark_stale('challenge') is True
    latest = ts.read_latest()
    assert latest['stale'] is True and latest['stale_reason'] == 'challenge'
    assert latest['songs'] == doc['songs'] and latest['date'] == doc['date']
    from integrations.reel_studio.contracts import validate_trending_songs
    assert validate_trending_songs(latest) == []


def test_a_fresh_publish_clears_stale(ts):
    ts.publish(_doc(date='2026-10-13'))
    ts.mark_stale('throttled')
    ts.publish(_doc(date='2026-10-14'))
    latest = ts.read_latest()
    assert latest['stale'] is False and 'stale_reason' not in latest


def test_a_damaged_latest_reads_as_none(ts, tmp_path):
    d = _songs_dir(tmp_path)
    d.mkdir(parents=True)
    (d / 'latest.json').write_text('{not json', encoding='utf-8')
    assert ts.read_latest() is None
    (d / 'latest.json').write_text('[1, 2]', encoding='utf-8')
    assert ts.read_latest() is None
