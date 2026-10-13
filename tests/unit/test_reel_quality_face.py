"""integrations.reel_studio.media.quality and .face.

frame_quality measures sharpness as the Laplacian variance INSIDE the face
box (portrait-mode phones blur the background on purpose), exposure from
luma, and blown highlights.  HaarFaceDetector wraps OpenCV's shipped Haar
cascades; its mapping is checked through stand-in cascades, because no face
picture ships with HARTOS (real faces are E2E-1's job, on her clips).
shake_from_shifts is the residual of the camera path against its moving
average.
"""
import cv2
import numpy as np
import pytest

from integrations.reel_studio.contracts import QUALITY_BAR, Box, ClipInfo, Quality
from integrations.reel_studio.media.analyze import analyze_clip
from integrations.reel_studio.media.face import HaarFaceDetector, segment_face
from integrations.reel_studio.media.probe import probe
from integrations.reel_studio.media.quality import frame_quality, shake_from_shifts
from tests.unit.reel_media_fixtures import make_clip, requires_ffmpeg

W, H = 432, 768
FACE = Box(0.3, 0.2, 0.4, 0.3)


def _texture(seed=1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    gray = rng.normal(128, 40, (H, W)).clip(0, 255).astype(np.uint8)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def _blur(img, sigma=5.0):
    return cv2.GaussianBlur(img, (0, 0), sigma)


def _face_px(box=FACE):
    return (int(box.x * W), int(box.y * H), int((box.x + box.w) * W), int((box.y + box.h) * H))


def _sharp_face_blurred_background():
    img = _blur(_texture())
    x0, y0, x1, y1 = _face_px()
    img[y0:y1, x0:x1] = _texture()[y0:y1, x0:x1]
    return img


# ── frame_quality ─────────────────────────────────────────────────────────

def test_blur_scores_below_sharp():
    sharp = frame_quality(_texture())
    blurred = frame_quality(_blur(_texture()))
    assert isinstance(sharp, Quality)
    assert blurred.sharp * 5 < sharp.sharp
    assert blurred.score < sharp.score
    assert 0.0 <= blurred.score <= 1.0 and 0.0 <= sharp.score <= 1.0


def test_dark_frames_are_flagged_and_score_lower():
    normal = frame_quality(_texture())
    dark = frame_quality((_texture().astype(np.float32) * 0.15).astype(np.uint8))
    assert dark.luma < QUALITY_BAR['shot_quality']['luma_min'] <= normal.luma
    assert dark.score < normal.score


def test_blown_highlights_are_measured():
    img = _texture()
    img[: int(H * 0.3)] = 255
    q = frame_quality(img)
    assert q.clipped == pytest.approx(0.3, abs=0.01)
    assert q.clipped > QUALITY_BAR['shot_quality']['max_clipped']
    assert q.score < frame_quality(_texture()).score


def test_a_blurred_background_keeps_the_face_region_sharp():
    portrait = _sharp_face_blurred_background()
    in_face = frame_quality(portrait, FACE)
    whole = frame_quality(portrait, None)
    all_sharp = frame_quality(_texture(), FACE)
    all_soft = frame_quality(_blur(_texture()), FACE)
    assert in_face.sharp > 0.8 * all_sharp.sharp
    assert in_face.sharp > 3 * whole.sharp
    assert in_face.sharp > 10 * all_soft.sharp
    assert in_face.face_area == pytest.approx(FACE.w * FACE.h)
    assert whole.face_area == 0.0


def test_a_visible_face_raises_the_score():
    img = _texture()
    assert frame_quality(img, FACE).score > frame_quality(img, None).score


def test_a_face_box_too_small_to_measure_falls_back_to_the_whole_frame():
    img = _sharp_face_blurred_background()
    tiny = Box(0.5, 0.5, 4.0 / W, 4.0 / H)
    assert frame_quality(img, tiny).sharp == pytest.approx(frame_quality(img, None).sharp)


# ── segment_face ──────────────────────────────────────────────────────────

def test_segment_face_is_the_median_of_the_boxes_found():
    boxes = [Box(0.1, 0.2, 0.3, 0.3), None, Box(0.2, 0.2, 0.3, 0.4), Box(0.3, 0.1, 0.2, 0.3)]
    assert segment_face(boxes) == Box(0.2, 0.2, 0.3, 0.3)


def test_segment_face_needs_enough_frames_with_a_face():
    assert segment_face([]) is None
    assert segment_face([None] * 6) is None
    assert segment_face([FACE] + [None] * 9) is None          # 10 % with a face
    assert segment_face([FACE] * 3 + [None] * 9) == FACE       # 25 %
    assert segment_face([FACE] + [None] * 9, min_frac=0.1) == FACE


# ── HaarFaceDetector ──────────────────────────────────────────────────────

class FakeCascade:
    """Stands in for cv2.CascadeClassifier: canned rectangles per call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.seen = []

    def empty(self):
        return False

    def detectMultiScale(self, gray, **kwargs):
        self.seen.append((gray.shape, gray.dtype, kwargs))
        rects = self.answers.pop(0) if self.answers else []
        return np.array(rects, dtype=np.int32).reshape(-1, 4)


def test_haar_maps_the_largest_face_back_to_the_frame():
    frontal = FakeCascade([(10, 10, 20, 20), (45, 80, 90, 80)])
    det = HaarFaceDetector(cascades=[(frontal, False)])
    box = det.detect(_texture())
    # The cascade ran on a 180x320 gray copy (320 px long side).
    assert frontal.seen[0][0] == (320, 180) and frontal.seen[0][1] == np.uint8
    assert box == Box(45 / 180, 80 / 320, 90 / 180, 80 / 320)


def test_haar_tries_the_mirrored_profile_when_no_frontal_face():
    frontal = FakeCascade([])
    profile = FakeCascade([], [(20, 100, 60, 60)])       # found only on the mirror
    det = HaarFaceDetector(cascades=[(frontal, False), (profile, True)])
    box = det.detect(_texture())
    assert len(profile.seen) == 2
    # x on the mirror is 20, so on the frame it is 180 - 20 - 60 = 100.
    assert box == Box(100 / 180, 100 / 320, 60 / 180, 60 / 320)


def test_haar_returns_none_without_a_face():
    det = HaarFaceDetector(cascades=[(FakeCascade([]), False), (FakeCascade([], []), True)])
    assert det.detect(_texture()) is None


def test_the_real_cascades_load_and_find_no_face_in_noise():
    assert HaarFaceDetector().detect(_texture()) is None


# ── a FaceDetector drives analyze_clip ────────────────────────────────────

def test_analyze_clip_uses_the_detector_every_fifth_frame_and_carries_the_box():
    class Alternating:
        def __init__(self):
            self.calls = 0

        def detect(self, bgr):
            self.calls += 1
            return FACE if self.calls % 2 == 1 else None

    portrait = _sharp_face_blurred_background()
    info = ClipInfo(path='p.mp4', duration_s=4.0, width=W, height=H, display_w=W,
                    display_h=H, rotation=0, fps_avg=30.0, codec='h264')
    det = Alternating()
    analysis = analyze_clip(info, detector=det, frames=((i / 5.0, portrait) for i in range(20)))
    assert det.calls == 4
    assert analysis.faces == [FACE] * 5 + [None] * 5 + [FACE] * 5 + [None] * 5
    # Sharpness is measured inside the carried box, and outside it without one.
    assert analysis.sharp[0] == pytest.approx(frame_quality(portrait, FACE).sharp, rel=1e-4)
    assert analysis.sharp[7] == pytest.approx(frame_quality(portrait, None).sharp, rel=1e-4)
    with_face = [c for c in analysis.candidates if c.face == FACE]
    assert with_face, 'frames with a face outscore the rest'
    assert max(c.quality.score for c in analysis.candidates) == with_face[0].quality.score


# ── shake_from_shifts ─────────────────────────────────────────────────────

def test_a_steady_pan_has_no_shake():
    shifts = np.tile([1.5, -0.5], (30, 1))
    shifts[0] = 0.0
    shake = shake_from_shifts(shifts)
    assert shake.shape == (30,)
    assert np.allclose(shake[1:], 0.0, atol=1e-9)


def test_jitter_has_more_shake_than_a_pan():
    rng = np.random.default_rng(3)
    pan = np.tile([1.0, 0.0], (60, 1))
    jitter = pan + rng.normal(0, 3.0, (60, 2))
    assert shake_from_shifts(jitter).mean() > 1.0 > shake_from_shifts(pan).mean()


def test_shake_of_tiny_inputs():
    assert shake_from_shifts(np.zeros((0, 2))).shape == (0,)
    assert shake_from_shifts(np.array([[0.0, 0.0]])).tolist() == [0.0]


@requires_ffmpeg
def test_a_jittered_clip_shakes_more_than_a_steady_one(tmp_path):
    grid = 'drawgrid=w=37:h=23:t=3:c=white,'
    steady = make_clip(tmp_path, 'steady.mp4', size=(320, 180), duration_s=4.0, source='color',
                       color='gray', vf=grid + 'crop=w=280:h=158:x=20:y=11,scale=320:180')
    jitter = make_clip(tmp_path, 'jitter.mp4', size=(320, 180), duration_s=4.0, source='color',
                       color='gray', vf=grid + "crop=w=280:h=158:x='20+18*sin(n*2.1)':"
                                             "y='11+10*cos(n*1.7)',scale=320:180")
    calm = shake_from_shifts(analyze_clip(probe(steady)).shifts).mean()
    shaky = shake_from_shifts(analyze_clip(probe(jitter)).shifts).mean()
    assert shaky > 3 * calm
