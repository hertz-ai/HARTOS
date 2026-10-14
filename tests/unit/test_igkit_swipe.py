"""integrations.reel_studio.igkit.swipe: the outlier maths, ported from
instagram-agent-skill's swipe.py (MIT).

swipe is the ONE implementation of "how far did this reel beat its own
account" (Gate 4): the central trends job (WS-6) scores songs through
rank_reels instead of keeping its own copy.  Raw views are not evidence; the
multiple over the account's own median is.
"""
import copy
import math

import pytest

from integrations.reel_studio.igkit import hooks, swipe

ROWS = [
    {'account': '@a', 'followers': 4000, 'median': 2000, 'views': 40000,
     'hook': 'Bare face to bride in 20 seconds', 'song_id': 's1', 'song_title': 'Song One',
     'artist': 'Artist One', 'lang': 'ta', 'highlight_start_ms': 38500},
    {'account': '@b', 'followers': 900000, 'median': 400000, 'views': 400000,
     'hook': 'Hey guys welcome back', 'song_id': 's2', 'song_title': 'Song Two',
     'artist': 'Artist Two', 'lang': 'hi'},
    {'account': '@c', 'followers': 5000, 'median': 1000, 'views': 6000,
     'hook': 'Wait for the temple jewellery', 'song_id': 's1', 'song_title': 'Song One',
     'artist': 'Artist One', 'lang': 'ta', 'highlight_start_ms': [12000, 40000]},
    {'account': '@d', 'followers': 3000, 'median': 1500, 'views': 90000, 'hook': 'muted',
     'song_id': 's3', 'should_mute': True},
    {'account': '@e', 'followers': 2000, 'views': 0, 'hook': 'no views'},
]


class TestAccountMedian:
    def test_fewer_than_six_reels_is_no_baseline(self):
        assert swipe.account_median([10, 20, 30, 40, 50]) is None

    def test_only_the_newest_twelve_count(self):
        plays = [100] * 6 + [1] * 6 + [10 ** 9] * 5
        assert swipe.account_median(plays) == 50.5

    def test_values_that_are_not_counts_are_ignored(self):
        assert swipe.account_median([None, True, -5, 'x', 10, 20, 30, 40, 50, 60]) == 35.0


class TestOutlier:
    def test_multiple_over_baseline(self):
        assert swipe.outlier(412000, 11000) == 37.45

    @pytest.mark.parametrize('base', [0, None, -1])
    def test_no_baseline(self, base):
        assert swipe.outlier(1000, base) is None


class TestRankReels:
    def test_ranked_by_multiple_not_views(self):
        r = swipe.rank_reels(ROWS)
        assert [x['account'] for x in r['reels']] == ['@a', '@c', '@b']
        assert [x['outlier'] for x in r['reels']] == [20.0, 6.0, 1.0]
        assert r['baseline'] == 'account median'
        assert r['n'] == 3 and r['accounts'] == 3

    def test_muted_and_viewless_rows_are_dropped_and_counted(self):
        r = swipe.rank_reels(ROWS)
        assert r['dropped'] == {'should_mute': 1, 'no_views': 1}

    @pytest.mark.parametrize('flag,muted', [
        (True, True), ('true', True), ('yes', True), (1, True), ('1', True), (2, True),
        (' ON ', True), (False, False), ('false', False), ('False', False), ('no', False),
        (0, False), ('0', False), (None, False), ('', False), ('y', False), ('maybe', False),
    ])
    def test_should_mute_written_as_text_or_a_number_is_read(self, flag, muted):
        # The string 'false' is truthy: such rows were dropped as muted.  The
        # reading is core.config_cache.flag_value's, the one in HARTOS: a
        # number is its truth and junk ('y', 'maybe') is not muted.
        r = swipe.rank_reels([dict(ROWS[0], should_mute=flag)])
        assert r['dropped']['should_mute'] == (1 if muted else 0)
        assert r['n'] == (0 if muted else 1)

    def test_source_guard_igkit_keeps_no_flag_table_of_its_own(self):
        # Amendment A9 d / Gate 4: core.config_cache.flag_value is the one
        # yes/no reader; igkit.parse_flag was a second table that disagreed.
        from integrations.reel_studio import igkit
        assert not hasattr(igkit, 'parse_flag')

    def test_followers_are_the_fallback_baseline(self):
        rows = [dict(row, median=None) for row in ROWS[:3]]
        r = swipe.rank_reels(rows)
        assert r['baseline'] == 'follower count'
        assert r['reels'][0]['outlier'] == 10.0          # 40000 / 4000

    def test_formula_names_come_from_the_one_classifier(self):
        r = swipe.rank_reels(ROWS)
        for reel in r['reels']:
            assert (reel['formula_id'], reel['formula']) == hooks.classify_hook(reel['hook'])
        assert r['reels'][0]['hook_score'] == hooks.rate_hook(ROWS[0]['hook'])['score']

    def test_rows_without_a_hook_still_rank(self):
        rows = [{'account': '@x', 'median': 100, 'views': 500, 'song_id': 's9'}]
        r = swipe.rank_reels(rows)
        assert r['reels'][0]['outlier'] == 5.0
        assert r['reels'][0]['formula'] == 'unclassified'
        assert r['reels'][0]['hook_score'] is None

    def test_counts_given_as_text_are_read(self):
        rows = [{'account': '@x', 'median': '11,000', 'views': '412,000', 'hook': 'x'}]
        assert swipe.rank_reels(rows)['reels'][0]['outlier'] == 37.45

    @pytest.mark.parametrize('text,count', [
        ('412,000', 412000), ('1,50,000', 150000), (' 7 ', 7), ('12.5', 12.5),
        ('4.5k', 4500), ('412K', 412000), ('1.2M', 1200000), ('2B', 2000000000),
        ('1.5 lakh', 150000), ('2 lakhs', 200000), ('1.2 crore', 12000000), ('3 cr', 30000000),
    ])
    def test_text_counts_keep_their_decimals_and_suffixes(self, text, count):
        # Upstream deleted every non-digit: "1.2M" was read as 12 and "4.5k"
        # as 45, with no error.
        assert swipe.outlier(text, 1) == count

    @pytest.mark.parametrize('text', ['1.2.3', 'about 400', '12 views', '1.2MM', 'k', '',
                                      '-5', '1,2', '1e6', 'nan', '12,34,5'])
    def test_text_that_is_not_a_count_is_none(self, text):
        assert swipe.outlier(text, 1) is None

    def test_a_baseline_from_text_counts(self):
        assert swipe.account_median(['1.2M', '800K', '1M', '900k', '1.1M', '950K']) == 975000.0

    def test_the_callers_rows_are_not_changed(self):
        rows = copy.deepcopy(ROWS)
        swipe.rank_reels(rows)
        assert rows == ROWS

    def test_empty(self):
        r = swipe.rank_reels([])
        assert r['reels'] == [] and r['top_songs'] == [] and r['n'] == 0


class TestTopSongs:
    def test_song_score_sums_log_multiples_over_distinct_accounts(self):
        songs = swipe.rank_reels(ROWS)['top_songs']
        assert [s['song_id'] for s in songs] == ['s1', 's2']
        s1 = songs[0]
        assert s1['score'] == pytest.approx(math.log1p(20.0) + math.log1p(6.0), abs=1e-3)
        assert (s1['n_reels'], s1['accounts']) == (2, 2)
        assert (s1['max_outlier'], s1['median_outlier']) == (20.0, 13.0)
        assert (s1['title'], s1['artist'], s1['lang']) == ('Song One', 'Artist One', 'ta')

    def test_the_start_comes_from_the_best_reel(self):
        s1 = swipe.rank_reels(ROWS)['top_songs'][0]
        assert s1['best_start_ms'] == 38500
        rows = [dict(ROWS[0], highlight_start_ms=None), ROWS[2]]
        assert swipe.rank_reels(rows)['top_songs'][0]['best_start_ms'] == 12000
        rows = [dict(ROWS[0], highlight_start_ms=None)]
        assert swipe.rank_reels(rows)['top_songs'][0]['best_start_ms'] == 0

    def test_one_account_counts_once_per_song(self):
        rows = [dict(ROWS[0]), dict(ROWS[0], views=8000)]   # same account, same song
        s1 = swipe.rank_reels(rows)['top_songs'][0]
        assert s1['accounts'] == 1 and s1['n_reels'] == 2
        assert s1['score'] == pytest.approx(math.log1p(20.0), abs=1e-3)

    def test_a_row_weight_scales_its_contribution(self):
        rows = [dict(ROWS[0], weight=0.5)]
        s1 = swipe.rank_reels(rows)['top_songs'][0]
        assert s1['score'] == pytest.approx(0.5 * math.log1p(20.0), abs=1e-3)

    def test_ties_break_on_accounts_then_best_multiple_then_id(self):
        rows = [
            {'account': '@p', 'median': 100, 'views': 300, 'song_id': 'zz'},
            {'account': '@q', 'median': 100, 'views': 300, 'song_id': 'aa'},
        ]
        assert [s['song_id'] for s in swipe.rank_reels(rows)['top_songs']] == ['aa', 'zz']
