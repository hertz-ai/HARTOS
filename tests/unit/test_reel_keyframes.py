"""integrations.reel_studio.media.keyframes.pick_keyframes.

At most k (25) keyframes go to the VLM: the best candidate of each segment,
and, when there are more segments than k, the best per equal slice of the
whole story's timeline (clips in capture order), so the transformation is
covered from bare face to final look rather than its sharpest minute.
Pure: the analyses are built by hand.
"""
import random

import pytest

from integrations.reel_studio.contracts import Candidate, ClipAnalysis, ClipInfo, Quality, Segment
from integrations.reel_studio.media.keyframes import MAX_KEYFRAMES, pick_keyframes


def _info(duration_s, path='c.mp4'):
    return ClipInfo(path=path, duration_s=duration_s, width=1080, height=1920, display_w=1080,
                    display_h=1920, rotation=0, fps_avg=30.0, codec='h264')


def _cand(clip, t, seg, score):
    return Candidate(clip=clip, t_s=t, segment=seg, jpeg=b'\xff\xd8%s' % repr((clip, t)).encode(),
                     quality=Quality(score=score, sharp=100.0, luma=0.5, clipped=0.0))


def _analysis(clip, duration_s, seg_len, score_of):
    """Segments of seg_len seconds; three candidates each, scored by score_of(t)."""
    n = int(round(duration_s / seg_len))
    segs = [Segment(i * seg_len, (i + 1) * seg_len) for i in range(n)]
    cands = []
    for i, s in enumerate(segs):
        for frac in (0.2, 0.5, 0.8):
            t = round(s.t0 + frac * seg_len, 6)
            cands.append(_cand(clip, t, i, score_of(clip, t)))
    return ClipAnalysis(clip=clip, info=_info(duration_s, clip + '.mp4'), segments=segs,
                        candidates=cands)


def test_one_per_segment_when_there_are_few_segments():
    a = _analysis('a', 10.0, 2.0, lambda c, t: 0.5)
    b = _analysis('b', 6.0, 2.0, lambda c, t: 0.5)
    keys = pick_keyframes([a, b])
    assert len(keys) == 8
    assert [(k.clip, k.segment) for k in keys] == (
        [('a', i) for i in range(5)] + [('b', i) for i in range(3)])


def test_the_best_candidate_of_a_segment_wins_and_ties_go_to_the_earlier_frame():
    best = {0.4: 0.9, 1.0: 0.3, 1.6: 0.5, 2.4: 0.7, 3.0: 0.7, 3.6: 0.1}
    a = _analysis('a', 4.0, 2.0, lambda c, t: best[round(t, 6)])
    assert [k.t_s for k in pick_keyframes([a])] == [0.4, 2.4]


def test_never_more_than_k():
    a = _analysis('a', 100.0, 1.0, lambda c, t: 0.5)
    assert MAX_KEYFRAMES == 25
    assert len(pick_keyframes([a])) == 25
    assert len(pick_keyframes([a], k=7)) == 7
    assert pick_keyframes([a], k=0) == []
    assert pick_keyframes([]) == []


def test_keyframes_spread_over_the_whole_story():
    # Quality rises through the story (lighting improves as she works), so
    # the 25 best frames overall would all come from its end.
    a = _analysis('a', 60.0, 1.0, lambda c, t: t / 200.0)
    b = _analysis('b', 40.0, 1.0, lambda c, t: (60.0 + t) / 200.0)
    keys = pick_keyframes([a, b])
    assert len(keys) == 25
    story = [k.t_s + (60.0 if k.clip == 'b' else 0.0) for k in keys]
    for quarter in range(4):
        lo, hi = 25.0 * quarter, 25.0 * (quarter + 1)
        assert sum(lo <= s < hi for s in story) >= 5, (quarter, story)
    # Each pick is still the best of its own slice of the timeline.
    assert all(abs(k.t_s % 1.0 - 0.8) < 1e-6 for k in keys)


def test_story_order_and_one_per_segment_when_spreading():
    a = _analysis('a', 40.0, 1.0, lambda c, t: 0.5)
    b = _analysis('b', 40.0, 1.0, lambda c, t: 0.5)
    keys = pick_keyframes([a, b])
    order = [(0 if k.clip == 'a' else 1, k.t_s) for k in keys]
    assert order == sorted(order)
    assert len({(k.clip, k.segment) for k in keys}) == len(keys)


def test_deterministic_whatever_order_the_candidates_arrive_in():
    a = _analysis('a', 50.0, 1.0, lambda c, t: (t * 37 % 11) / 11.0)
    b = _analysis('b', 30.0, 1.0, lambda c, t: (t * 13 % 7) / 7.0)
    first = pick_keyframes([a, b])
    rng = random.Random(5)
    for _ in range(3):
        rng.shuffle(a.candidates)
        rng.shuffle(b.candidates)
        assert pick_keyframes([a, b]) == first


def test_segments_without_candidates_are_skipped():
    a = _analysis('a', 6.0, 2.0, lambda c, t: 0.5)
    a.candidates = [c for c in a.candidates if c.segment != 1]
    assert [k.segment for k in pick_keyframes([a])] == [0, 2]


def test_returns_the_candidates_themselves():
    a = _analysis('a', 4.0, 2.0, lambda c, t: 0.5)
    keys = pick_keyframes([a])
    assert all(any(k is c for c in a.candidates) for k in keys)
    assert all(isinstance(k.jpeg, bytes) and k.quality is not None for k in keys)


def test_a_candidate_without_quality_counts_as_zero():
    a = _analysis('a', 2.0, 2.0, lambda c, t: 0.5)
    a.candidates.append(Candidate(clip='a', t_s=0.1, segment=0, jpeg=b'x'))
    assert pick_keyframes([a])[0].t_s == pytest.approx(0.4)
