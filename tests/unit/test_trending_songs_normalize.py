"""integrations.trends.trending_songs.normalize_reel: one reel, as the private
API sends it or as instagrapi's Media model dumps it, becomes one record.

The worker (ig_worker.py, central's own venv) hands back reels without
interpreting them; this is where they are read, in HARTOS, with the one
reader of a reel's music (sources.music_of, which the worker shares so it
knows which songs need a track lookup).  Shapes come from instagrapi 3.0.24's
types.py / extractors.py (goals-trends report 1.8): clips_metadata arrives as
a dict or as a pydantic model, play_count may be None (extract_media_v1 falls
back to video_play_count only when the key is absent), and the song is named
by music_asset_info, by music_canonical_id, by original_sound_info, or only by
clips_music_attribution_info.
"""
import copy
import json

import pytest

from tests.fixtures.trends import build as fx


class _Model:
    """A pydantic model as far as the reader cares: model_dump(mode=...)."""

    def __init__(self, data):
        self._data = copy.deepcopy(data)

    def model_dump(self, mode='python', **_kw):
        return copy.deepcopy(self._data)


@pytest.fixture
def reels():
    return fx.load()


def _norm(raw, **kw):
    from integrations.trends.trending_songs import normalize_reel
    return normalize_reel(raw, **kw)


def test_a_licensed_song_is_keyed_by_its_audio_cluster(reels):
    rec = _norm(reels['licensed'])
    assert rec['song_key'] == 'cluster:660000000000001'
    assert rec['audio_cluster_id'] == '660000000000001'
    assert rec['audio_asset_id'] == '770000000000000001'
    assert rec['music_canonical_id'] == '18200000000000001'
    assert (rec['title'], rec['artist']) == ('Saree Song', 'Fixture Artist')
    assert rec['highlight_start_ms'] == [38500, 61000]
    assert rec['duration_ms'] == 180000
    assert rec['is_original_audio'] is False and rec['is_explicit'] is False
    assert rec['should_mute'] is False
    assert rec['plays'] == 41200
    assert rec['media_pk'] == '3412000000000000101' and rec['code'] == 'Cfix0001'
    assert rec['account'] == 'mua_fixture_one'
    assert rec['taken_at'] == 1760300000.0


def test_clips_metadata_as_a_model_reads_the_same_as_a_dict(reels):
    raw = copy.deepcopy(reels['licensed'])
    want = _norm(raw)
    raw['clips_metadata'] = _Model(raw['clips_metadata'])
    assert _norm(raw) == want


def test_a_whole_media_model_with_an_iso_taken_at_reads_the_same(reels):
    """Media.model_dump(mode='json') writes taken_at as an ISO string and the
    user's pk as a string (coerce_numbers_to_str)."""
    dumped = copy.deepcopy(reels['licensed'])
    dumped['taken_at'] = '2025-10-12T20:13:20Z'
    dumped['pk'] = str(dumped['pk'])
    dumped['user']['pk'] = str(dumped['user']['pk'])
    assert _norm(_Model(dumped)) == _norm(reels['licensed'])


def test_play_count_none_falls_back_to_view_count(reels):
    rec = _norm(reels['attribution_only'])
    assert rec['plays'] == 15500


def test_a_zero_or_absent_count_is_no_count(reels):
    """extract_media_v1 writes play_count 0 when the API sent none; a reel's
    true plays are never 0, and a 0 would drag the account's median down."""
    raw = copy.deepcopy(reels['licensed'])
    raw['play_count'] = 0
    raw['view_count'] = 0
    assert _norm(raw)['plays'] is None
    raw.pop('play_count')
    raw['video_play_count'] = 1234
    assert _norm(raw)['plays'] == 1234
    raw['video_play_count'] = True          # a flag, not a count
    assert _norm(raw)['plays'] is None


def test_an_original_sound_is_keyed_by_its_audio_asset(reels):
    rec = _norm(reels['original_audio'])
    assert rec['song_key'] == 'original:880000000000000002'
    assert rec['is_original_audio'] is True
    assert rec['title'] == 'Original audio'
    assert rec['artist'] == 'mua_fixture_two'
    assert rec['duration_ms'] == 21000
    assert rec['highlight_start_ms'] == []


def test_attribution_alone_still_names_the_song(reels):
    """No music_info at all: the song is known only by
    clips_music_attribution_info (song_name, artist_name, audio_id)."""
    rec = _norm(reels['attribution_only'])
    assert rec['song_key'] == 'audio:990000000000000003'
    assert (rec['title'], rec['artist']) == ('Mehendi Beat', 'Fixture Band')
    assert rec['is_original_audio'] is False


def test_a_canonical_id_alone_keys_the_song_and_asks_for_its_track(reels):
    rec = _norm(reels['canonical_only'])
    assert rec['song_key'] == 'canonical:18200000000000044'
    assert rec['music_canonical_id'] == '18200000000000044'
    assert rec['needs_track'] is True
    assert _norm(reels['licensed'])['needs_track'] is False


def test_a_reel_with_no_music_is_kept_for_the_accounts_baseline(reels):
    rec = _norm(reels['no_music'])
    assert rec is not None
    assert rec['song_key'] is None and rec['plays'] == 3900


@pytest.mark.parametrize('flag, muted', [(True, True), ('true', True), ('yes', True),
                                         (1, True), (False, False), ('false', False),
                                         (None, False), ('junk', False)])
def test_should_mute_reads_the_attribution_flag_one_way(reels, flag, muted):
    raw = copy.deepcopy(reels['licensed'])
    raw['clips_music_attribution_info']['should_mute_audio'] = flag
    assert _norm(raw)['should_mute'] is muted


def test_should_mute_on_the_original_sound_mutes_too(reels):
    raw = copy.deepcopy(reels['original_audio'])
    raw['clips_metadata']['original_sound_info']['should_mute_audio'] = True
    assert _norm(raw)['should_mute'] is True
    assert _norm(reels['muted'])['should_mute'] is True


def test_the_handle_fills_in_when_the_reel_names_no_user(reels):
    raw = copy.deepcopy(reels['licensed'])
    raw.pop('user')
    assert _norm(raw, account='mua_watchlist')['account'] == 'mua_watchlist'
    raw['user'] = {'pk': 42}
    assert _norm(raw, account='mua_watchlist')['account'] == 'mua_watchlist'


@pytest.mark.parametrize('raw', [None, 7, 'reel', [], {}, {'code': 'C1'}, {'pk': True}])
def test_what_is_not_a_reel_is_none(raw):
    assert _norm(raw) is None


def test_no_url_or_manifest_survives_into_a_record(reels):
    for kind in ('licensed', 'original_audio', 'attribution_only', 'muted'):
        text = json.dumps(_norm(reels[kind]))
        assert 'example.invalid' not in text and 'MPD' not in text


def test_the_workers_slimmed_reel_reads_exactly_like_the_raw_one(reels):
    """The worker keeps only what this reader reads (sources.slim_reel), and
    nothing it drops changes the record."""
    from integrations.trends.sources import slim_reel
    for kind in ('licensed', 'original_audio', 'attribution_only', 'canonical_only',
                 'muted', 'no_music'):
        slim = slim_reel(reels[kind])
        assert _norm(slim) == _norm(reels[kind]), kind
        text = json.dumps(slim)
        assert 'example.invalid' not in text and 'MPD' not in text, kind
        assert 'caption' not in slim and 'video_versions' not in slim
        assert set(slim['user']) <= {'pk', 'username'}
