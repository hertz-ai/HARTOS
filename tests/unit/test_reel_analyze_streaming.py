"""integrations.reel_studio.media.analyze: ONE streaming pass per clip.

analyze_clip keeps about ten numbers per analysed frame plus a few JPEG
candidates per segment, never the frames themselves.  These tests feed it a
synthetic frame stream (``frames=``), so a 10-minute clip at 5 fps (3,000
frames at 432x768) runs without ffmpeg, and measure what it holds with
tracemalloc.
"""
import tracemalloc

import cv2
import numpy as np
import pytest

from integrations.reel_studio.contracts import Box, ClipAnalysis, ClipInfo, ClipUnsupported
from integrations.reel_studio.media import analyze as analyze_mod
from integrations.reel_studio.media.analyze import analyze_clip

W, H = 432, 768          # portrait analysis frames (768 px long side)
FPS = 5.0


def _info(duration_s, **kw):
    base = dict(path='C:/clips/one_take.mp4', duration_s=duration_s, width=1080, height=1920,
                display_w=1080, display_h=1920, rotation=0, fps_avg=30.0, codec='h264')
    base.update(kw)
    return ClipInfo(**base)


def _shot_base(hue: int) -> np.ndarray:
    """A smooth, strongly coloured frame for one shot (camera-like, compressible)."""
    yy, xx = np.mgrid[0:H, 0:W]
    hsv = np.empty((H, W, 3), np.uint8)
    hsv[..., 0] = hue
    hsv[..., 1] = (150 + 80 * xx / W).astype(np.uint8)
    hsv[..., 2] = (90 + 120 * yy / H).astype(np.uint8)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


class CountingDetector:
    """A FaceDetector stand-in: one fixed face, and a call log."""

    def __init__(self, box=Box(0.3, 0.2, 0.4, 0.3)):
        self.box = box
        self.calls = 0

    def detect(self, bgr):
        self.calls += 1
        return self.box


def _jpeg_long_side(jpeg: bytes) -> int:
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    return max(img.shape[:2])


def test_ten_minutes_at_5fps_stays_bounded():
    shots, shot_s = 10, 60.0
    bases = [_shot_base(hue) for hue in range(5, 180, 18)][:shots]
    n = int(shots * shot_s * FPS)               # 3,000 frames
    info = _info(shots * shot_s)
    detector = CountingDetector()

    def frames():
        for i in range(n):
            t = i / FPS
            frame = bases[int(t // shot_s)].copy()
            x = int(40 + 300 * ((i % 50) / 50.0))
            cv2.rectangle(frame, (x, 300), (x + 60, 420), (255, 255, 255), 2)
            yield t, frame

    tracemalloc.start()
    try:
        analysis = analyze_clip(info, clip_id='x' * 64, detector=detector, frames=frames())
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert isinstance(analysis, ClipAnalysis)
    assert peak < 64 * 2 ** 20, 'peak traced memory %.1f MB' % (peak / 2 ** 20)
    for name in ('times', 'corr', 'corr_skip', 'sharp', 'luma', 'clipped'):
        assert len(getattr(analysis, name)) == n, name
    assert np.asarray(analysis.shifts).shape == (n, 2)
    assert len(analysis.faces) == n
    assert detector.calls == n // 5             # Haar every 5th analysed frame

    # Segments tile the clip; every shot change is a boundary.
    segs = analysis.segments
    assert segs[0].t0 == 0.0 and segs[-1].t1 == pytest.approx(info.duration_s)
    for a, b in zip(segs, segs[1:]):
        assert a.t1 == pytest.approx(b.t0)
    bounds = [s.t0 for s in segs[1:]]
    for k in range(1, shots):
        assert min(abs(b - k * shot_s) for b in bounds) <= 0.2, k * shot_s

    # At most 3 JPEG candidates per segment, each at a 768 px long side.
    per_seg = {}
    for c in analysis.candidates:
        per_seg[c.segment] = per_seg.get(c.segment, 0) + 1
        assert c.clip == 'x' * 64
        assert segs[c.segment].t0 - 1e-9 <= c.t_s < segs[c.segment].t1
        assert c.face == detector.box
        assert 0.0 <= c.quality.score <= 1.0
    assert max(per_seg.values()) <= 3
    assert set(per_seg) == set(range(len(segs)))      # no segment left without one
    for c in analysis.candidates[:5] + analysis.candidates[-5:]:
        assert _jpeg_long_side(c.jpeg) == 768


def test_the_kept_candidates_are_each_segments_best_frames():
    # Three 2 s shots; in each, ONE frame is crisp and the rest are blurred.
    rng = np.random.default_rng(7)
    texture = rng.integers(0, 256, (H, W), dtype=np.uint8)
    crisp_at = {0: 0.8, 1: 3.0, 2: 5.4}
    frames = []
    for i in range(30):
        t = i / FPS
        shot = int(t // 2.0)
        base = _shot_base(20 + 60 * shot).astype(np.int16)
        tex = texture if abs(t - crisp_at[shot]) < 1e-9 else cv2.GaussianBlur(texture, (0, 0), 6)
        frame = np.clip(base + ((tex.astype(np.int16) - 128) // 3)[..., None], 0, 255)
        frame = frame.astype(np.uint8)
        frames.append((t, frame))
    analysis = analyze_clip(_info(6.0), detector=CountingDetector(None), frames=iter(frames))
    assert len(analysis.segments) == 3
    best = {}
    for c in analysis.candidates:
        if c.segment not in best or c.quality.score > best[c.segment].quality.score:
            best[c.segment] = c
    assert {s: best[s].t_s for s in best} == pytest.approx(crisp_at)
    assert all(sum(1 for c in analysis.candidates if c.segment == s) == 3 for s in range(3))
    # Candidates come back in timeline order.
    assert [c.t_s for c in analysis.candidates] == sorted(c.t_s for c in analysis.candidates)


def test_clip_id_defaults_to_the_path_and_info_is_kept():
    info = _info(1.0)
    frames = ((i / FPS, _shot_base(40)) for i in range(5))
    analysis = analyze_clip(info, detector=CountingDetector(None), frames=frames)
    assert analysis.clip == info.path
    assert analysis.info is info
    assert len(analysis.segments) == 1
    assert analysis.faces == [None] * 5


def test_candidate_bytes_are_capped(monkeypatch):
    # 20 one-second shots of alternating hue: 20 segments that would keep 60
    # JPEGs.  With the byte budget set to about 25 JPEGs, the pass evicts the
    # weakest extras first and every segment still keeps one.
    frames = []
    for i in range(100):
        t = i / FPS
        shot = int(t)
        frame = _shot_base(10 + 80 * (shot % 2))
        cv2.putText(frame, str(i), (50, 400), cv2.FONT_HERSHEY_SIMPLEX, 3, (255, 255, 255), 5)
        frames.append((t, frame))
    one = len(cv2.imencode('.jpg', frames[0][1], [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes())
    budget = 25 * one
    monkeypatch.setattr(analyze_mod, 'MAX_CANDIDATE_BYTES', budget)
    analysis = analyze_clip(_info(20.0), detector=CountingDetector(None), frames=iter(frames))
    assert len(analysis.segments) == 20
    total = sum(len(c.jpeg) for c in analysis.candidates)
    assert total <= budget
    assert {c.segment for c in analysis.candidates} == set(range(20))
    assert len(analysis.candidates) < 60


def test_frames_of_another_size_are_brought_to_the_long_side():
    frames = ((i / FPS, cv2.resize(_shot_base(40), (1080, 1920))) for i in range(5))
    analysis = analyze_clip(_info(1.0), detector=CountingDetector(None), frames=frames)
    assert all(_jpeg_long_side(c.jpeg) == 768 for c in analysis.candidates)


def test_no_frames_at_all_is_unreadable():
    with pytest.raises(ClipUnsupported) as err:
        analyze_clip(_info(1.0), detector=CountingDetector(None), frames=iter(()))
    assert err.value.reason == 'unreadable'


def test_a_failing_detector_does_not_fail_the_analysis():
    class Broken:
        def detect(self, bgr):
            raise RuntimeError('cascade exploded')

    frames = ((i / FPS, _shot_base(40)) for i in range(10))
    analysis = analyze_clip(_info(2.0), detector=Broken(), frames=frames)
    assert analysis.faces == [None] * 10
    assert analysis.candidates
