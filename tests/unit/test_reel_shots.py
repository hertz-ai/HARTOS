"""integrations.reel_studio.media.shots: cuts from histogram correlation.

``segment_from_corr`` is pure: a cut is where 1-corr exceeds
max(0.35, median + 6*MAD); a one-frame flash (its neighbours alike) is not a
cut; shots under 0.4 s are merged into the neighbour they resemble more.
``split_segments`` cuts long one-take shots on a fixed 8 s grid so each piece
keeps its own candidates.  The ``requires_ffmpeg`` tests run real clips
through probe + analyze_clip.
"""
import numpy as np
import pytest

from integrations.reel_studio.contracts import Segment
from integrations.reel_studio.media.analyze import analyze_clip
from integrations.reel_studio.media.probe import probe
from integrations.reel_studio.media.shots import (
    CUT_FLOOR, cut_threshold, hist_corr, hs_histogram, segment_from_corr, split_segments)
from tests.unit.reel_media_fixtures import concat_clips, make_clip, requires_ffmpeg


def _times(n, fps=5.0):
    return np.arange(n) / fps


def _segs(segments):
    return [(round(s.t0, 6), round(s.t1, 6)) for s in segments]


# ── segment_from_corr (pure) ──────────────────────────────────────────────

def test_no_cut_is_one_segment_to_the_end():
    t = _times(10)
    ones = np.ones(10)
    assert _segs(segment_from_corr(t, ones, ones, end_s=2.0)) == [(0.0, 2.0)]
    # Without end_s the last frame is held one frame step.
    assert _segs(segment_from_corr(t, ones, ones)) == [(0.0, 2.0)]


def test_a_cut_lands_between_the_two_frames():
    t = _times(20)
    corr = np.ones(20)
    corr[10] = 0.2                       # frame 10 (2.0 s) starts a new shot
    skip = np.ones(20)
    skip[10] = 0.2                       # so frames 9 and 11 differ too
    assert _segs(segment_from_corr(t, corr, skip, end_s=4.0)) == [(0.0, 1.9), (1.9, 4.0)]


def test_a_one_frame_flash_is_not_a_cut():
    t = _times(20)
    corr = np.ones(20)
    corr[5] = corr[6] = 0.1              # into the flash and back out
    skip = np.ones(20)
    skip[5] = 0.98                       # frames 4 and 6 are alike
    assert _segs(segment_from_corr(t, corr, skip, end_s=4.0)) == [(0.0, 4.0)]


def test_a_one_frame_shot_is_merged_into_its_closer_neighbour():
    t = _times(20)
    corr = np.ones(20)
    corr[5], corr[6] = 0.1, 0.5          # strong cut in, weaker cut out
    skip = np.ones(20)
    skip[5] = 0.1                        # frames 4 and 6 differ: not a flash
    skip[6] = 0.5                        # frames 5 and 7 differ as much as 5 and 6
    # The 0.2 s middle shot joins the side across the weaker boundary.
    assert _segs(segment_from_corr(t, corr, skip, end_s=4.0)) == [(0.0, 0.9), (0.9, 4.0)]


def test_shots_of_exactly_the_minimum_survive():
    t = _times(20)
    corr = np.ones(20)
    corr[5] = corr[7] = 0.1              # frames 5 and 6: a 0.4 s shot
    skip = np.full(20, 0.1)
    assert _segs(segment_from_corr(t, corr, skip, end_s=4.0)) == [(0.0, 0.9), (0.9, 1.3),
                                                                  (1.3, 4.0)]


def test_the_threshold_rises_with_a_restless_clip():
    n = 41
    t = _times(n)
    corr = np.where(np.arange(n) % 2 == 0, 0.70, 0.60)   # 1-corr: 0.30 / 0.40
    corr[0] = 1.0
    corr[25] = 0.05                                      # one real cut
    # 1-corr over frames 1..40: twenty 0.30, nineteen 0.40 and one 0.95, so
    # the median is 0.35 and the MAD 0.05: every 0.40 stays under 0.65.
    assert cut_threshold(corr) == pytest.approx(0.35 + 6 * 0.05)
    got = segment_from_corr(t, corr, np.full(n, 0.05), end_s=8.2)
    assert _segs(got) == [(0.0, 4.9), (4.9, 8.2)]


def test_the_threshold_never_drops_below_the_floor():
    assert cut_threshold(np.ones(30)) == CUT_FLOOR
    assert cut_threshold(np.array([1.0])) == CUT_FLOOR
    assert cut_threshold(np.array([])) == CUT_FLOOR


def test_degenerate_inputs():
    assert segment_from_corr([], [], []) == []
    assert _segs(segment_from_corr([0.0], [1.0], [1.0], end_s=0.2)) == [(0.0, 0.2)]


# ── split_segments (pure) ─────────────────────────────────────────────────

@pytest.mark.parametrize('segments, want', [
    ([Segment(0.0, 20.0)], [(0, 8), (8, 16), (16, 20)]),
    ([Segment(0.0, 16.2)], [(0, 8), (8, 16.2)]),                 # 0.2 s tail merged back
    ([Segment(0.0, 5.0), Segment(5.0, 17.0)], [(0, 5), (5, 8), (8, 16), (16, 17)]),
    ([Segment(7.8, 12.0)], [(7.8, 12.0)]),                       # 0.2 s head merged forward
    ([Segment(0.0, 0.3)], [(0, 0.3)]),
])
def test_split_segments_on_the_absolute_8s_grid(segments, want):
    assert _segs(split_segments(segments, 8.0, min_len_s=0.4)) == [
        (float(a), float(b)) for a, b in want]


# ── histograms ────────────────────────────────────────────────────────────

def test_histogram_correlation_separates_colours():
    red = np.zeros((90, 160, 3), np.uint8)
    red[..., 2] = 200
    red[:, :80, 1] = 60
    blue = red[..., ::-1].copy()
    assert hist_corr(hs_histogram(red), hs_histogram(red)) == pytest.approx(1.0)
    assert hist_corr(hs_histogram(red), hs_histogram(blue)) < 1.0 - CUT_FLOOR


# ── real clips ────────────────────────────────────────────────────────────

_TINTS = ('lutrgb=g=val*0.15:b=val*0.15', 'lutrgb=r=val*0.15:b=val*0.15',
          'lutrgb=r=val*0.15:g=val*0.15')


@requires_ffmpeg
def test_three_tinted_clips_give_three_shots(tmp_path):
    parts = [make_clip(tmp_path, 't%d.mp4' % i, size=(320, 180), duration_s=2.0, vf=vf)
             for i, vf in enumerate(_TINTS)]
    analysis = analyze_clip(probe(concat_clips(tmp_path, parts, 'cat.mp4')))
    segs = analysis.segments
    assert len(segs) == 3, _segs(segs)
    assert abs(segs[1].t0 - 2.0) <= 0.2 and abs(segs[2].t0 - 4.0) <= 0.2
    assert segs[0].t0 == 0.0 and segs[-1].t1 == pytest.approx(6.0, abs=0.05)


@requires_ffmpeg
def test_a_static_clip_is_one_shot(tmp_path):
    analysis = analyze_clip(probe(make_clip(tmp_path, 's.mp4', size=(320, 180), duration_s=2.0,
                                            source='color', color='teal')))
    assert len(analysis.segments) == 1


@requires_ffmpeg
def test_a_one_frame_white_flash_is_not_a_cut(tmp_path):
    analysis = analyze_clip(probe(make_clip(tmp_path, 'f.mp4', size=(320, 180), duration_s=3.0,
                                            flash_at_s=1.0)))
    times = np.asarray(analysis.times)
    k = int(np.argmin(np.abs(times - 1.0)))
    # The pass saw the flash (a big jump into it) ...
    assert 1.0 - analysis.corr[k] > CUT_FLOOR
    assert analysis.clipped[k] > 0.9
    # ... and still found one shot, with the blown frame never a candidate.
    assert len(analysis.segments) == 1
    assert all(abs(c.t_s - 1.0) > 1e-6 for c in analysis.candidates)
