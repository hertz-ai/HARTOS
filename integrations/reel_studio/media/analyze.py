"""integrations.reel_studio.media.analyze -- ONE streaming pass per clip.

``analyze_clip`` decodes a clip once (decode.iter_frames: 5 fps, 768 px
long side, upright, tone-mapped) and, frame by frame, keeps only:

* hue x saturation histogram correlation with the previous frame, and
  between the frames either side of each frame (flash rejection), from a
  160 px copy, holding two histograms at a time;
* the cv2.phaseCorrelate shift against the previous 160 px gray frame;
* Laplacian sharpness inside the face box, luma and blown highlights
  (quality.quality_from_gray);
* the face box: Haar on every DETECT_EVERY-th frame, carried in between;
* a few JPEG candidates (q85, 768 px) per run of frames that no final
  boundary can split.

So memory is about ten numbers per frame plus a bounded set of JPEGs, never
the frames (plan finding 20); a ten-minute clip holds a few MB.  Keyframes
for the VLM come from these candidates, already at 768 px, so nothing is
decoded twice (finding 21).

HOW CANDIDATES STAY CORRECT WITHOUT KNOWING THE SHOTS YET.  The shot
boundaries are only known at the end (the cut threshold needs the whole
clip's statistics).  But every final boundary is either a jump above
shots.CUT_FLOOR or a point of shots' fixed SPLIT_EVERY_S grid, and merging
only ever removes boundaries.  So the pass starts a new candidate pool at
each such jump and grid point; every final segment is a union of whole
pools, and its best ``keep_per_segment`` frames are among its pools' best.
A frame is JPEG-encoded only when it enters its pool's top list.  A byte
budget (MAX_CANDIDATE_BYTES) bounds the pathological case of a clip that
jumps at nearly every frame: the weakest extras go first, a pool's last
candidate only when nothing else is left.
"""
from __future__ import annotations

import bisect
import logging
import math
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import cv2
import numpy as np

from integrations.reel_studio.contracts import (
    Box, Candidate, ClipAnalysis, ClipInfo, ClipUnsupported, Quality)
from integrations.reel_studio.media import decode
from integrations.reel_studio.media.face import FaceDetector, HaarFaceDetector
from integrations.reel_studio.media.probe import UNREADABLE
from integrations.reel_studio.media.quality import quality_from_gray
from integrations.reel_studio.media.shots import (
    CUT_FLOOR, MIN_SHOT_S, SPLIT_EVERY_S, hist_corr, hs_histogram, segment_from_corr,
    split_segments)

logger = logging.getLogger(__name__)

#: Face detection runs on every Nth analysed frame (once a second at 5 fps).
DETECT_EVERY = 5
#: JPEG quality of kept candidates (plan WS-1).
JPEG_QUALITY = 85
#: Candidates kept per segment.
KEEP_PER_SEGMENT = 3
#: Long side of the copy used for histograms and phase correlation.
SMALL_SIDE = 160
#: Cap on the JPEG bytes one clip's pass may hold (see the module note).
MAX_CANDIDATE_BYTES = 32 * 2 ** 20


class _Kept:
    __slots__ = ('key', 't', 'jpeg', 'quality', 'face')

    def __init__(self, key, t, jpeg, quality, face):
        self.key, self.t, self.jpeg, self.quality, self.face = key, t, jpeg, quality, face


class _Pools:
    """Runs of frames no final boundary can split, each with its best few JPEGs."""

    def __init__(self, keep: int, budget: int) -> None:
        self.keep = max(1, int(keep))
        self.budget = int(budget)
        self.pools: List[List[_Kept]] = []
        self.bytes = 0

    def open(self) -> None:
        self.pools.append([])

    def offer(self, key: Tuple[float, int], make: Callable[[], _Kept]) -> None:
        pool = self.pools[-1]
        if len(pool) >= self.keep and key <= min(k.key for k in pool):
            return
        kept = make()
        pool.append(kept)
        self.bytes += len(kept.jpeg)
        if len(pool) > self.keep:
            self._remove(pool, min(pool, key=lambda k: k.key))
        while self.bytes > self.budget and self._evict():
            pass

    def _remove(self, pool: List[_Kept], kept: _Kept) -> None:
        pool.remove(kept)
        self.bytes -= len(kept.jpeg)

    def _evict(self) -> bool:
        """Drop the weakest candidate, preferring pools that keep another."""
        for shared in (True, False):
            choices = [(k.key, n, k) for n, pool in enumerate(self.pools)
                       if (len(pool) > 1 or not shared) for k in pool]
            if choices:
                _key, n, kept = min(choices, key=lambda c: (c[0], -c[1]))
                self._remove(self.pools[n], kept)
                return True
        return False

    def kept(self) -> Iterable[_Kept]:
        for pool in self.pools:
            yield from pool


def _resized(bgr: np.ndarray, long_side: int) -> np.ndarray:
    h, w = bgr.shape[:2]
    if max(h, w) == long_side:
        return bgr
    scale = long_side / float(max(h, w))
    size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    return cv2.resize(bgr, size, interpolation=interp)


def _jpeg(bgr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise ValueError('JPEG encode failed for a %s frame' % (bgr.shape,))
    return buf.tobytes()


class _Detect:
    """Runs the face detector; a detector error costs that frame its face, never the pass."""

    def __init__(self, detector: FaceDetector) -> None:
        self.detector = detector
        self.failed = 0

    def __call__(self, bgr: np.ndarray) -> Optional[Box]:
        try:
            return self.detector.detect(bgr)
        except Exception as e:      # a third-party detector: any failure means "no face"
            self.failed += 1
            log = logger.warning if self.failed == 1 else logger.debug
            log('face detector %s failed (%d so far): %s', type(self.detector).__name__,
                self.failed, e)
            return None


def analyze_clip(info: ClipInfo, *, clip_id: Optional[str] = None,
                 detector: Optional[FaceDetector] = None, fps: float = decode.DECODE_FPS,
                 long_side: int = decode.LONG_SIDE, keep_per_segment: int = KEEP_PER_SEGMENT,
                 frames: Optional[Iterable[Tuple[float, np.ndarray]]] = None) -> ClipAnalysis:
    """Analyse one clip in a single streaming pass (see the module note).

    ``clip_id`` names the clip in the result and its candidates (the store
    xid; default ``info.path``).  ``detector`` defaults to HaarFaceDetector.
    ``frames`` replaces the decode with any iterable of (t_s, bgr) at
    ``fps`` (frames of another size are brought to ``long_side``).

    Raises ClipUnsupported('unreadable') when no frame comes out, and
    whatever decode.iter_frames raises (media_bin.FFmpegFailed,
    MediaToolMissing).
    """
    clip = clip_id or info.path
    detect = _Detect(detector if detector is not None else HaarFaceDetector())
    source = frames if frames is not None else decode.iter_frames(info, fps=fps,
                                                                   long_side=long_side)
    times: List[float] = []
    corr: List[float] = []
    corr_skip: List[float] = []
    sharp: List[float] = []
    luma: List[float] = []
    clipped: List[float] = []
    shifts: List[Tuple[float, float]] = []
    faces: List[Optional[Box]] = []
    pools = _Pools(keep_per_segment, MAX_CANDIDATE_BYTES)
    hists: List[np.ndarray] = []        # the last two histograms
    prev_small: Optional[np.ndarray] = None
    window: Optional[np.ndarray] = None
    box: Optional[Box] = None

    for i, (t, bgr) in enumerate(source):
        t = float(t)
        bgr = _resized(bgr, int(long_side))
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        small = _resized(bgr, SMALL_SIDE)

        hist = hs_histogram(small)
        c = hist_corr(hists[-1], hist) if hists else 1.0
        if len(hists) == 2:
            corr_skip[i - 1] = hist_corr(hists[0], hist)
        hists = (hists + [hist])[-2:]
        corr.append(c)
        corr_skip.append(c)              # replaced when the next frame arrives

        small_gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev_small is None or prev_small.shape != small_gray.shape:
            window = cv2.createHanningWindow((small_gray.shape[1], small_gray.shape[0]),
                                             cv2.CV_32F)
            shifts.append((0.0, 0.0))
        else:
            (dx, dy), _response = cv2.phaseCorrelate(prev_small, small_gray, window)
            shifts.append((float(dx), float(dy)))
        prev_small = small_gray

        if i % DETECT_EVERY == 0:
            box = detect(bgr)
        faces.append(box)
        quality: Quality = quality_from_gray(gray, box)
        sharp.append(quality.sharp)
        luma.append(quality.luma)
        clipped.append(quality.clipped)

        if (i == 0 or 1.0 - c > CUT_FLOOR
                or math.floor(t / SPLIT_EVERY_S) != math.floor(times[-1] / SPLIT_EVERY_S)):
            pools.open()
        times.append(t)
        pools.offer((quality.score, -i),
                    lambda: _Kept((quality.score, -i), t, _jpeg(bgr), quality, box))

    if not times:
        raise ClipUnsupported(UNREADABLE, 'no frame could be decoded from %s' % (info.path,))
    end_s = float(info.duration_s) if info.duration_s > times[-1] else times[-1] + 1.0 / fps
    shots = segment_from_corr(times, corr, corr_skip, min_len_s=MIN_SHOT_S, end_s=end_s)
    segments = split_segments(shots, SPLIT_EVERY_S, min_len_s=MIN_SHOT_S)

    starts = [s.t0 for s in segments]
    by_segment: Dict[int, List[_Kept]] = {}
    for kept in pools.kept():
        index = max(0, bisect.bisect_right(starts, kept.t) - 1)
        by_segment.setdefault(index, []).append(kept)
    candidates = []
    for index in sorted(by_segment):
        best = sorted(by_segment[index], key=lambda k: k.key, reverse=True)[:keep_per_segment]
        candidates += [Candidate(clip=clip, t_s=k.t, segment=index, jpeg=k.jpeg,
                                 quality=k.quality, face=k.face)
                       for k in sorted(best, key=lambda k: k.t)]

    return ClipAnalysis(clip=clip, info=info, times=np.asarray(times, dtype=np.float64),
                        corr=np.asarray(corr, dtype=np.float64),
                        corr_skip=np.asarray(corr_skip, dtype=np.float64),
                        sharp=np.asarray(sharp, dtype=np.float64),
                        luma=np.asarray(luma, dtype=np.float64),
                        clipped=np.asarray(clipped, dtype=np.float64),
                        shifts=np.asarray(shifts, dtype=np.float64).reshape(-1, 2),
                        faces=faces, segments=segments, candidates=candidates)


__all__ = ['DETECT_EVERY', 'JPEG_QUALITY', 'KEEP_PER_SEGMENT', 'SMALL_SIDE',
           'MAX_CANDIDATE_BYTES', 'analyze_clip']
