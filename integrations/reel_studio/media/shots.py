"""integrations.reel_studio.media.shots -- where one shot ends and the next begins.

A cut is a jump in colour content between two analysed frames, measured as
the correlation of their hue x saturation histograms (H 50 bins x S 60
bins, OpenCV's HISTCMP_CORREL).  ``segment_from_corr`` is pure:

* a cut is where ``1 - corr`` exceeds ``max(CUT_FLOOR, median + 6 * MAD)``
  over the clip, so a restless hand-held clip needs a bigger jump than a
  tripod one;
* a one-frame flash is not a cut: when the frames either side of a jump are
  alike (``corr_skip``, the correlation of frames t-1 and t+1), the jump
  into the odd frame and the jump back out are both dropped;
* shots shorter than ``MIN_SHOT_S`` join the neighbour across the weaker
  boundary (the one with the smaller jump);
* a boundary sits half way between the last frame of one shot and the first
  of the next.

``split_segments`` then cuts long one-take shots on a fixed grid of
``SPLIT_EVERY_S`` seconds from the clip start, so a ten-minute single take
still offers candidates from every part of the transformation (plan U8
reels are built from many moments).  Grid points are absolute so the
streaming pass (analyze.py) knows every possible boundary before the clip
ends.  Every final boundary is either a jump above CUT_FLOOR or a grid
point; analyze relies on that.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence

import cv2
import numpy as np

from integrations.reel_studio.contracts import Segment

#: 1 - corr must exceed this for a cut, however calm the clip is.
CUT_FLOOR = 0.35
#: Robust spread multiplier for the adaptive threshold.
MAD_K = 6.0
#: Shortest shot kept (QUALITY_BAR shot_rules.min_shot_s is the EDL's own rule).
MIN_SHOT_S = 0.4
#: Long shots are cut on this absolute grid (seconds from the clip start).
SPLIT_EVERY_S = 8.0
#: Hue x saturation histogram bins (OpenCV hue runs 0..179).
HIST_BINS = (50, 60)

_EPS = 1e-9


def hs_histogram(bgr: np.ndarray) -> np.ndarray:
    """Hue x saturation histogram (HIST_BINS) of a BGR frame, float32."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return cv2.calcHist([hsv], [0, 1], None, list(HIST_BINS), [0, 180, 0, 256])


def hist_corr(a: np.ndarray, b: np.ndarray) -> float:
    """Correlation of two histograms: 1.0 alike, near 0 or below unrelated."""
    return float(cv2.compareHist(a, b, cv2.HISTCMP_CORREL))


def cut_threshold(corr: Sequence[float]) -> float:
    """``max(CUT_FLOOR, median + MAD_K * MAD)`` of ``1 - corr`` over frames 1..n-1.

    ``corr[0]`` (the first frame, which has no predecessor) is ignored.
    """
    d = 1.0 - np.asarray(corr, dtype=np.float64)[1:]
    if d.size == 0:
        return CUT_FLOOR
    med = float(np.median(d))
    mad = float(np.median(np.abs(d - med)))
    return max(CUT_FLOOR, med + MAD_K * mad)


def _cuts(corr: np.ndarray, corr_skip: np.ndarray, threshold: float) -> List[int]:
    """Frame indexes that start a new shot, flashes removed."""
    jumps = [i for i in range(1, len(corr)) if 1.0 - corr[i] > threshold]
    dropped = set()
    for i in jumps:
        if i in dropped:
            continue
        if 1.0 - corr_skip[i] <= threshold:
            # Frames i-1 and i+1 are alike: frame i is a one-frame blip.
            dropped.add(i)
            dropped.add(i + 1)
    return [i for i in jumps if i not in dropped]


def segment_from_corr(times: Sequence[float], corr: Sequence[float],
                      corr_skip: Sequence[float], *, min_len_s: float = MIN_SHOT_S,
                      end_s: Optional[float] = None) -> List[Segment]:
    """Pure: shots from per-frame histogram correlations.

    ``times[i]`` is frame i's time; ``corr[i]`` correlates frame i-1 with
    frame i (``corr[0]`` is ignored); ``corr_skip[i]`` correlates frames i-1
    and i+1.  The shots tile ``[times[0], end_s)``; ``end_s`` defaults to
    one frame step after the last frame.
    """
    t = np.asarray(times, dtype=np.float64)
    n = len(t)
    if n == 0:
        return []
    c = np.asarray(corr, dtype=np.float64)
    skip = np.asarray(corr_skip, dtype=np.float64)
    if len(c) != n or len(skip) != n:
        raise ValueError('times, corr and corr_skip need one value per frame '
                         '(%d, %d, %d)' % (n, len(c), len(skip)))
    if end_s is None:
        step = float(np.median(np.diff(t))) if n > 1 else 0.0
        end_s = float(t[-1]) + step
    threshold = cut_threshold(c)
    cuts = _cuts(c, skip, threshold)
    bounds = [float(t[0])] + [(float(t[i - 1]) + float(t[i])) / 2.0 for i in cuts] + [float(end_s)]
    strength = [1.0 - float(c[i]) for i in cuts]      # strength[j]: boundary bounds[j + 1]
    _merge_short(bounds, strength, min_len_s)
    return [Segment(a, b) for a, b in zip(bounds, bounds[1:])]


def _merge_short(bounds: List[float], strength: List[float], min_len_s: float) -> None:
    """In place: drop the weaker boundary of the shortest too-short shot, repeatedly."""
    while len(bounds) > 2:
        lengths = [b - a for a, b in zip(bounds, bounds[1:])]
        short = [j for j, length in enumerate(lengths) if length < min_len_s - _EPS]
        if not short:
            return
        j = min(short, key=lambda k: (lengths[k], k))
        left = j - 1 if j > 0 else None                  # boundary before shot j
        right = j if j < len(lengths) - 1 else None      # boundary after shot j
        if left is None:
            drop = right
        elif right is None:
            drop = left
        else:
            drop = left if strength[left] <= strength[right] else right
        del bounds[drop + 1]
        del strength[drop]


def split_segments(segments: Sequence[Segment], every_s: float = SPLIT_EVERY_S, *,
                   min_len_s: float = MIN_SHOT_S) -> List[Segment]:
    """Pure: cut each segment at the absolute grid points k * every_s inside it.

    A piece shorter than ``min_len_s`` joins its neighbour inside the same
    segment, so the grid never makes a shot too short to use.  A segment
    that is itself shorter than that is returned unchanged.
    """
    if every_s <= 0:
        raise ValueError('every_s must be > 0, got %r' % (every_s,))
    out: List[Segment] = []
    for seg in segments:
        first = int(math.floor(seg.t0 / every_s)) + 1
        points = [k * every_s for k in range(first, int(math.ceil(seg.t1 / every_s)) + 1)
                  if seg.t0 + _EPS < k * every_s < seg.t1 - _EPS]
        bounds = [seg.t0] + points + [seg.t1]
        j = 0
        while len(bounds) > 2 and j < len(bounds) - 1:
            if bounds[j + 1] - bounds[j] < min_len_s - _EPS:
                # Join the short piece to its neighbour: drop its inner boundary.
                del bounds[j + 1 if j + 1 < len(bounds) - 1 else j]
                j = max(0, j - 1)
                continue
            j += 1
        out.extend(Segment(a, b) for a, b in zip(bounds, bounds[1:]))
    return out


__all__ = ['CUT_FLOOR', 'MAD_K', 'MIN_SHOT_S', 'SPLIT_EVERY_S', 'HIST_BINS', 'hs_histogram',
           'hist_corr', 'cut_threshold', 'segment_from_corr', 'split_segments']
