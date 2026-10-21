"""integrations.reel_studio.media.quality -- how usable one frame is, and camera shake.

``frame_quality`` scores a frame for the planner and the VLM fallback
(``Quality.score`` in 0..1; moments use appeal = 10 * score when the VLM is
down):

* sharpness: variance of the Laplacian INSIDE the face box when one is
  known, because portrait-mode phones blur the background on purpose; the
  whole frame otherwise, or when the box is under MIN_SHARP_BOX_PX;
* exposure: mean luma (0..1) and the fraction of blown highlights (gray >=
  CLIP_LEVEL); crushed blacks are not counted, since black hair and dupattas
  are legitimately dark;
* a visible face, weighted by its area up to FACE_AREA_FULL.

score = 0.5 * sharp / (sharp + SHARP_REF)
      + 0.3 * max(0, 1 - 2|luma - 0.5|) * max(0, 1 - clipped / CLIPPED_ZERO)
      + 0.2 * min(1, face_area / FACE_AREA_FULL)

The weights rank frames within a clip; the advisory QUALITY_BAR
``shot_quality`` row (clip-relative sharpness percentile, luma 0.3-0.7,
< 2 % clipped) is judged by QA from the stored arrays, not here.

``shake_from_shifts`` is pure: the per-frame distance between the camera
path (cumulative phase-correlation shifts) and its centred moving average.
A steady pan scores zero; hand jitter does not.
"""
from __future__ import annotations

from typing import Optional

import cv2
import numpy as np

from integrations.reel_studio.contracts import Box, Quality

#: Laplacian variance (8-bit gray at a 768 px long side) that scores 0.5.
SHARP_REF = 100.0
#: Gray level from which a pixel counts as a blown highlight.
CLIP_LEVEL = 250
#: Clipped fraction at which the exposure term reaches zero.
CLIPPED_ZERO = 0.10
#: Face area (fraction of the frame) that earns the full face term.
FACE_AREA_FULL = 0.02
#: A face box narrower or shorter than this many pixels of the analysed
#: frame is too small to measure sharpness in; the whole frame is measured.
#: (Not face.MIN_FACE_PX, the smallest face the cascades look for.)
MIN_SHARP_BOX_PX = 24
#: Frames either side in the shake smoothing window (1 s at 5 fps).
SHAKE_RADIUS = 2


def frame_quality(bgr: np.ndarray, face: Optional[Box] = None) -> Quality:
    """Quality of a BGR frame, sharpness measured inside ``face`` when given."""
    gray = bgr if bgr.ndim == 2 else cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return quality_from_gray(gray, face)


def quality_from_gray(gray: np.ndarray, face: Optional[Box] = None) -> Quality:
    """frame_quality for a frame already converted to 8-bit gray."""
    h, w = gray.shape[:2]
    region = gray
    face_area = 0.0
    if face is not None:
        face_area = max(0.0, float(face.w) * float(face.h))
        x0, y0 = max(0, int(face.x * w)), max(0, int(face.y * h))
        x1, y1 = min(w, int((face.x + face.w) * w)), min(h, int((face.y + face.h) * h))
        if x1 - x0 >= MIN_SHARP_BOX_PX and y1 - y0 >= MIN_SHARP_BOX_PX:
            region = gray[y0:y1, x0:x1]
    sharp = float(cv2.Laplacian(region, cv2.CV_64F).var())
    luma = float(gray.mean()) / 255.0
    clipped = float(np.count_nonzero(gray >= CLIP_LEVEL)) / float(gray.size)
    exposure = max(0.0, 1.0 - 2.0 * abs(luma - 0.5)) * max(0.0, 1.0 - clipped / CLIPPED_ZERO)
    score = (0.5 * sharp / (sharp + SHARP_REF) + 0.3 * exposure
             + 0.2 * min(1.0, face_area / FACE_AREA_FULL))
    return Quality(score=min(1.0, max(0.0, score)), sharp=sharp, luma=luma, clipped=clipped,
                   face_area=face_area)


def shake_from_shifts(shifts) -> np.ndarray:
    """Pure: per-frame shake from frame-to-frame (dx, dy) shifts, shape (n,).

    The camera path is the running sum of the shifts; each frame's shake is
    its distance from the path's centred moving average over SHAKE_RADIUS
    frames either side (the window shrinks symmetrically at the ends, so a
    straight path scores exactly zero everywhere).
    """
    s = np.asarray(shifts, dtype=np.float64).reshape(-1, 2)
    n = len(s)
    if n == 0:
        return np.zeros(0)
    path = np.cumsum(s, axis=0)
    idx = np.arange(n)
    radius = np.minimum(SHAKE_RADIUS, np.minimum(idx, n - 1 - idx))
    sums = np.vstack([np.zeros((1, 2)), np.cumsum(path, axis=0)])
    smooth = (sums[idx + radius + 1] - sums[idx - radius]) / (2 * radius + 1)[:, None]
    return np.linalg.norm(path - smooth, axis=1)


__all__ = ['SHARP_REF', 'CLIP_LEVEL', 'CLIPPED_ZERO', 'FACE_AREA_FULL', 'MIN_SHARP_BOX_PX',
           'SHAKE_RADIUS', 'frame_quality', 'quality_from_gray', 'shake_from_shifts']
