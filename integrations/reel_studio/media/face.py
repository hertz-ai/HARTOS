"""integrations.reel_studio.media.face -- where the face is.

Scope (owner ruling 2026-10-14, widened the same day; recorded in CLAUDE.md
as the named exception to "HARTOS has no ML code"): OpenCV Haar face
DETECTION is allowed in HARTOS for crop framing (edl.frame_face) AND for
face-region shot quality: sharpness measured inside the face box
(quality.quality_from_gray, run by analyze.py), the face-area ranking term
in that score, and the post-render face-in-frame check (qa.py face_render).
Detection only.  Never recognition, identity, embeddings or stored
biometrics: nothing here may tell one face from another or say whose face it
is.  The only output is a Box, where a face sits as fractions of the frame.

V1 uses the Haar cascades that ship inside opencv-python (``cv2.data``):
frontal first, then profile on the frame and on its mirror image (the
profile cascade only knows one side).  No model file is added (plan U12;
YuNet waits for a licence check).  Haar misses faces half hidden by a brush
or a dupatta, so a box found every few frames is carried across the frames
in between (analyze.py), and a segment's face is the median of the boxes
found in it (``segment_face``), which also discards one-off false hits.

Detection runs on a gray copy with a DETECT_SIDE px long side; boxes come
back as fractions of the frame (contracts.Box).  Anything with a ``detect``
method fits the FaceDetector protocol; tests pass stand-ins.
"""
from __future__ import annotations

import logging
import math
import os
from typing import List, Optional, Protocol, Sequence, Tuple

import cv2
import numpy as np

from integrations.reel_studio.contracts import Box

logger = logging.getLogger(__name__)

#: Long side of the gray image the cascades run on.
DETECT_SIDE = 320
#: Smallest face the cascades look for, in DETECT_SIDE pixels.
MIN_FACE_PX = 24
#: The cascades, in the order they are tried: (file in cv2.data, also try the mirror).
HAAR_CASCADES: Tuple[Tuple[str, bool], ...] = (
    ('haarcascade_frontalface_default.xml', False),
    ('haarcascade_profileface.xml', True),
)
#: segment_face needs a face in at least this fraction of the frames.
MIN_FACE_FRACTION = 0.25


class FaceDetector(Protocol):
    """Finds the main face in a BGR frame, or None."""

    def detect(self, bgr: np.ndarray) -> Optional[Box]:
        ...


def _load_cascades() -> List[Tuple[object, bool]]:
    loaded = []
    for name, mirror in HAAR_CASCADES:
        path = os.path.join(cv2.data.haarcascades, name)
        cascade = cv2.CascadeClassifier(path)
        if cascade.empty():
            logger.warning('Haar cascade %s did not load from %s; faces will not be found '
                           'with it', name, path)
            continue
        loaded.append((cascade, mirror))
    return loaded


class HaarFaceDetector:
    """OpenCV Haar cascades: the largest face, as a Box of the frame.

    ``cascades`` is a sequence of (classifier, try_mirror) pairs; the default
    loads HAAR_CASCADES from cv2.data on first use.  The first classifier
    that finds anything wins.
    """

    def __init__(self, *, cascades: Optional[Sequence[Tuple[object, bool]]] = None,
                 side: int = DETECT_SIDE, min_face_px: int = MIN_FACE_PX) -> None:
        self._cascades = list(cascades) if cascades is not None else None
        self._side = int(side)
        self._min = int(min_face_px)

    def _classifiers(self) -> List[Tuple[object, bool]]:
        if self._cascades is None:
            self._cascades = _load_cascades()
        return self._cascades

    def detect(self, bgr: np.ndarray) -> Optional[Box]:
        gray = bgr if bgr.ndim == 2 else cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]
        scale = min(1.0, self._side / float(max(h, w)))
        if scale < 1.0:
            gray = cv2.resize(gray, (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                              interpolation=cv2.INTER_AREA)
        gray = cv2.equalizeHist(gray)
        gh, gw = gray.shape[:2]
        for cascade, mirror in self._classifiers():
            rects = self._find(cascade, gray)
            if not rects and mirror:
                rects = [(gw - x - rw, y, rw, rh)
                         for x, y, rw, rh in self._find(cascade, cv2.flip(gray, 1))]
            if rects:
                x, y, rw, rh = max(rects, key=lambda r: (r[2] * r[3], -r[1], -r[0]))
                return Box(x / float(gw), y / float(gh), rw / float(gw), rh / float(gh))
        return None

    def _find(self, cascade, gray) -> List[Tuple[int, int, int, int]]:
        found = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                         minSize=(self._min, self._min))
        return [tuple(int(v) for v in r) for r in np.asarray(found).reshape(-1, 4)]


def segment_face(boxes: Sequence[Optional[Box]], *,
                 min_frac: float = MIN_FACE_FRACTION) -> Optional[Box]:
    """The component-wise median of the boxes found, or None.

    None when ``boxes`` is empty or fewer than ``min_frac`` of its entries
    hold a box (a face seen in one frame of forty is most likely a false hit).
    """
    present = [b for b in boxes if b is not None]
    if not boxes or not present or len(present) < math.ceil(min_frac * len(boxes) - 1e-9):
        return None
    med = np.median(np.array([[b.x, b.y, b.w, b.h] for b in present], dtype=np.float64), axis=0)
    return Box(*(float(v) for v in med))


__all__ = ['DETECT_SIDE', 'MIN_FACE_PX', 'HAAR_CASCADES', 'MIN_FACE_FRACTION', 'FaceDetector',
           'HaarFaceDetector', 'segment_face']
