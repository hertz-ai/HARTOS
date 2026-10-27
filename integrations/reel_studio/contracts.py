"""integrations.reel_studio.contracts -- the shared Reel Studio contracts.

Every Reel Studio part builds against this file: analysis (WS-1), beats
(WS-2), VLM moments (WS-3), planner/render/QA (WS-4), trends (WS-6), the
pipeline and tools (WS-9), and the phone (WS-12) through the JSON shapes
below.  It holds:

* schema ids, constants and ``QUALITY_BAR`` (every threshold, in one place);
* the value types passed between the parts (ClipInfo, Candidate, Moment,
  BeatGrid, ClipCapture, ClipSource, ClipAnalysis, RenderResult, ...);
* pure validators for the EDL, moment list, QA report, reply media item,
  outbox listing item, inbound file offer, trending-song lists and the style
  dict, and (re-exported from core.chat_attachments) the chat attachment;
* the capture-order rule (user amendment A1): clips play in the order they
  were filmed, the reel ends on the final-look reveal, and a <= 1 s
  final-look teaser at the start is optional (``hook_first``).

It imports only the standard library, core.user_file_store (itself standard
library plus two small core helpers), core.chat_attachments (standard
library plus the store) and integrations.agent_engine.human_voice (standard
library only), so the agent tools can import it at construction time
without pulling cv2, numpy or ffmpeg.

Validators never raise on bad input; they return a list of problems, empty
when the document is good.  Each problem reads ``"<id>: <what is wrong>"``.
For the EDL the id is the QUALITY_BAR check that failed, which is what
qa.run_qa reports.

LAYERING.  core/ may not import integrations/, so the facts about inbound
files that the store (core/user_file_store.py, the one upload service), the
device door and core/chat_attachments.py need as much as these validators do
live in the store, and this module imports them from there (amendment A9 d):
the purposes and their MIME types (INBOUND_PURPOSES, INBOUND_MIMES), the
offer's meta keys (INBOUND_META_KEYS), OUTBOUND_PURPOSE, and the xid,
file-name and capture-time rules (is_xid, is_file_name, is_capture_ms).  A
purpose added to the store's registry is one the contracts accept, and a
captured_at the store keeps is one they accept.  For the same reason the ONE
/chat attachments rule (amendment A9 a) is core.chat_attachments.
validate_attachment, re-exported here as validate_attachment: what a turn
sends, what /chat reads (parse_inbound) and what a reply's mirror row is are
judged by that one function.

HASHTAGS.  The human-voice guide (integrations.agent_engine.human_voice,
which goal_manager's HUMAN_VOICE_GUIDE is) is the one source of
published-text style (owner ruling A10c): a reel caption may carry up to its
REEL_HASHTAG_LIMIT hashtags, any other published text up to its
POST_HASHTAG_LIMIT.  HASHTAG_LIMIT here is REEL_HASHTAG_LIMIT, imported,
never a second literal.
"""
from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

# The /chat attachments rule, re-exported (see LAYERING above).
from core.chat_attachments import validate_attachment  # noqa: F401
# The store's own facts about inbound files (see LAYERING above).  All are
# re-exported; OUTBOUND_PURPOSE is read here by the pipeline only.
from core.user_file_store import (INBOUND_META_KEYS, INBOUND_MIMES, INBOUND_PURPOSES,
                                  OUTBOUND_PURPOSE, is_capture_ms, is_file_name,  # noqa: F401
                                  is_xid)
# The reel hashtag cap is the human-voice guide's (see HASHTAGS above).
from integrations.agent_engine.human_voice import REEL_HASHTAG_LIMIT as HASHTAG_LIMIT


def _frozen(value):
    """Deep read-only copy: dicts become mappingproxies, lists tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: _frozen(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(v) for v in value)
    return value


# ── schema ids ────────────────────────────────────────────────────────────

EDL_SCHEMA = 'hart.reel.edl.v1'
MOMENTS_SCHEMA = 'hart.reel.moments.v1'
QA_SCHEMA = 'hart.reel.qa.v1'
TRENDING_SCHEMA = 'hart.trending_songs.v1'
TRENDING_CLIENT_SCHEMA = 'hart.trending_songs.client.v1'

# ── vocabulary ────────────────────────────────────────────────────────────

#: Makeup stages a frame can show.  The first seven are the transformation
#: in the order it happens; 'other' is anything else.
STAGES = ('bare_face', 'base', 'eyes', 'lips', 'hair', 'jewellery', 'final_look', 'other')
PROGRESSION = STAGES[:-1]
FINAL_LOOK = 'final_look'

#: The posted reel: portrait 1080x1920 at 30 fps CFR.
CANVAS = _frozen({'w': 1080, 'h': 1920, 'fps': 30})
#: Where overlays may sit, in canvas pixels: Instagram's reel UI covers
#: y < 230, y > 1440 and the right-hand 230 px (ig-reel SKILL.md:106-109).
SAFE_ZONE = _frozen({'x_min': 0, 'x_max': 850, 'y_min': 230, 'y_max': 1440})
#: Reel length range in seconds (plan U8), and the default target.
TARGET_S_RANGE = (5, 30)
DEFAULT_TARGET_S = 20
#: A clip at or above this average frame rate is high-frame-rate (slow-mo);
#: it is conformed to 30 fps at 1x with a note.
HFR_MIN_FPS = 100.0

TRANSFERS = ('sdr', 'pq', 'hlg')
ROTATIONS = (0, 90, 180, 270)
BEAT_SOURCES = ('song', 'local_audio', 'synthetic')
#: Segment roles: an optional final-look teaser first, the process shots in
#: capture order, the reveal last.
ROLES = ('teaser', 'process', 'reveal')
LOGO_CORNERS = ('top_left', 'top_right', 'bottom_left')
#: Animated overlays (amendment A14), each a clip with alpha the motion
#: runtime renders (integrations.reel_studio.motion) and pass 2 lays over
#: the cut: a kinetic title, a logo sting, a booking/price end card, and a
#: before->after wipe over the reveal's opening.
MOTION_TEMPLATES = ('title', 'logo_sting', 'end_card', 'wipe')
#: The motion templates that are a picture covering the whole frame (her
#: "before" face), not text or a logo, so SAFE_ZONE does not bound them.
MOTION_FULL_FRAME = ('wipe',)
MOMENT_SOURCES = ('vlm', 'fallback')

#: Where each clip's capture time came from (A1 precedence): the phone's
#: MediaStore DATE_TAKEN, else the container's creation_time, else the
#: upload/commit order.  'phone+container' is a batch that needed both.
CLIP_CAPTURE_SOURCES = ('phone', 'container', 'upload_order')
CAPTURE_SOURCES = ('phone', 'container', 'phone+container', 'upload_order')
#: Earliest believable capture time (2001-09-09, epoch ms).  Phones and
#: muxers write 0 or the QuickTime 1904 epoch when they have no clock.
MIN_CAPTURE_MS = 10 ** 12

NOTE_NO_FINAL_LOOK = 'no_final_look'

# ── inbound files (phone -> desktop) ──────────────────────────────────────
#
# INBOUND_PURPOSES, INBOUND_MIMES (accepted MIME types per purpose),
# INBOUND_META_KEYS (the optional ``captured_at``, MediaStore DATE_TAKEN in
# epoch ms with DATE_MODIFIED*1000 as the phone's fallback, and
# ``duration_ms`` of a file_offer, kept in StoredFile.meta) and
# OUTBOUND_PURPOSE are core.user_file_store's, imported above.

# ── delivery and style ────────────────────────────────────────────────────

# HASHTAG_LIMIT, the most hashtags a reel caption may carry, is the
# human-voice guide's REEL_HASHTAG_LIMIT, imported above (owner ruling A10c).
STYLE_LANGUAGES = ('ta', 'hi', 'pa', 'en')
STYLE_PACES = ('beat', 'calm')
#: The style dict reel_make hands the planner.  The story is chronological;
#: ``hook_first`` adds the optional final-look teaser (A1), and is set only
#: when her instructions ask for it.
STYLE_DEFAULTS = _frozen({
    'target_s': DEFAULT_TARGET_S, 'hook_first': False, 'pace': 'beat', 'hook_text': '',
    'use_logo': True, 'logo_corner': 'top_left', 'language': None, 'song_key': None,
})
#: Every ``status`` a reel_make result can carry.
REEL_STATUSES = ('ok', 'needs_clips', 'busy', 'no_space', 'quota', 'not_enough_footage',
                 'clip_unsupported', 'render_failed', 'qa_failed', 'interrupted')

# ── the quality bar ───────────────────────────────────────────────────────

#: Every QA check and its threshold, in ONE place (plan section 8, amended
#: by A1: the old hard "hook" check is replaced by order + reveal, and the
#: first-frame luma check stays hard).  A failed hard check withholds the
#: reel; advisory results go into the QA report's ``advisory`` list and the
#: reply notes.  Read-only: copy a row to vary it (``dict(QUALITY_BAR[k])``).
QUALITY_BAR = _frozen({
    # Rendered file, measured with ffmpeg.
    'resolution': {'hard': True, 'w': 1080, 'h': 1920, 'sar': '1:1'},
    'codec': {'hard': True, 'codec': 'h264', 'profile': 'High', 'pix_fmt': 'yuv420p'},
    'color_tags': {'hard': False, 'primaries': 'bt709', 'trc': 'bt709', 'space': 'bt709'},
    'fps': {'hard': True, 'fps': 30, 'frame_count_tol': 1},
    'duration': {'hard': True, 'tol_s': 1.0},
    'audio': {'hard': True, 'allow_audio': False},
    'size_cap': {'hard': True, 'mbps': 14.0, 'slack_bytes': 1 << 20},
    'bitrate_floor': {'hard': False, 'min_mbps': 4.0},
    'faststart': {'hard': True},
    'first_frame_luma': {'hard': True, 'at_s': 0.04, 'min_luma': 0.12},
    # The story (A1).
    'order': {'hard': True},
    'reveal': {'hard': True, 'min_hold_s': 1.5},
    'reveal_render': {'hard': False, 'from_end_s': 0.75, 'min_corr': 0.85,
                      'mask_overlays': True},
    'teaser': {'hard': True, 'max_s': 1.0},
    # EDL structure, rhythm and geometry.
    'schema': {'hard': True},
    'contiguous': {'hard': True, 'eps_s': 0.001},
    'shot_rules': {'hard': True, 'min_shot_s': 0.4, 'max_upscale': 1.8,
                   'speed_min': 0.75, 'speed_max': 2.0},
    'beats': {'hard': True, 'tol_s': 0.080},
    'beats_render': {'hard': False, 'scene_threshold': 0.3},
    'no_bars': {'hard': True, 'min_zoom': 1.0},
    'no_bars_render': {'hard': False},
    'face': {'hard': True, 'cy_min': 0.20, 'cy_max': 0.55},
    'face_render': {'hard': False},
    'overlays': {'hard': True},
    'transition': {'hard': True, 'allowed': ['cut']},
    'segment_fidelity': {'hard': False, 'min_corr': 0.80},
    'shot_quality': {'hard': False, 'sharp_pctl': 40, 'luma_min': 0.3, 'luma_max': 0.7,
                     'max_clipped': 0.02},
})

#: The checks validate_edl can report, in the order it runs them.
EDL_CHECKS = ('schema', 'contiguous', 'duration', 'shot_rules', 'beats', 'no_bars', 'face',
              'order', 'reveal', 'teaser', 'overlays', 'transition', 'audio')

#: Pixel slack for geometry checks (a crop window exactly on the frame edge
#: is inside it).
_PX_EPS = 0.5


# ── errors ────────────────────────────────────────────────────────────────

class ClipUnsupported(Exception):
    """A clip the editor cannot use (e.g. ``dolby_vision_p5``)."""

    def __init__(self, reason: str, detail: str = '') -> None:
        super().__init__('%s: %s' % (reason, detail) if detail else reason)
        self.reason = reason
        self.detail = detail


class PlanError(Exception):
    """The planner cannot build an EDL (e.g. ``not_enough_footage``, need_s=)."""

    def __init__(self, code: str, detail: str = '', **data: Any) -> None:
        super().__init__('%s: %s' % (code, detail) if detail else code)
        self.code = code
        self.detail = detail
        self.data = data


# ── small predicates ──────────────────────────────────────────────────────

_DATE = re.compile(r'\d{4}-\d{2}-\d{2}')
_URL = re.compile(r'(?i)^\s*https?://')
#: Beyond a secret's name (security.edge_privacy.is_secret_key), what a
#: published or cached song list never carries under a key: an address, or the
#: login its source (instagrapi) keeps, whose names are glued ('sessionid',
#: 'csrftoken', 'cookies', 'authorization_data'), so these match anywhere in
#: the key.
_SONG_LIST_KEY_PARTS = ('url', 'session', 'cookie', 'csrf', 'authorization')


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_str(v) -> bool:
    return isinstance(v, str)


def _is_text(v) -> bool:
    return isinstance(v, str) and v.strip() != ''


def _is_opt_str(v) -> bool:
    return v is None or isinstance(v, str)


def _is_str_list(v) -> bool:
    return isinstance(v, (list, tuple)) and all(isinstance(x, str) for x in v)


_Spec = Mapping[str, Tuple[Callable[[Any], bool], str]]


def _spec_problems(obj, spec: _Spec, where: str, *, optional: Sequence[str] = ()) -> List[str]:
    """Problems with ``obj`` against ``{key: (predicate, what it must be)}``."""
    if not isinstance(obj, Mapping):
        return ['%s is not an object' % where]
    out = []
    for key, (ok, want) in spec.items():
        if key not in obj:
            if key not in optional:
                out.append('%s.%s is missing' % (where, key))
        elif not ok(obj[key]):
            out.append('%s.%s=%r is not %s' % (where, key, obj[key], want))
    return out


# ── small helpers ─────────────────────────────────────────────────────────

def clamp_target_s(value, default: float = DEFAULT_TARGET_S) -> float:
    """``value`` limited to TARGET_S_RANGE; ``default`` when it is not a number."""
    if not _is_num(value):
        return default
    lo, hi = TARGET_S_RANGE
    return min(max(value, lo), hi)


def start_label(ms: int) -> str:
    """'m:ss' for a song start offset in milliseconds (38500 -> '0:38')."""
    total = int(ms) // 1000
    return '%d:%02d' % (total // 60, total % 60)


def capture_note(src: str) -> str:
    """The EDL note that records where the clip order came from."""
    return 'capture_order:%s' % src


def logo_box(overlay: Mapping, *, zone: Mapping = SAFE_ZONE) -> Tuple[float, float, float, float]:
    """(x0, y0, x1, y1) canvas pixels of a logo overlay.

    ``corner`` and ``margin_px`` are measured from the SAFE_ZONE edges, not
    the canvas edges, so a logo of sensible size lands where Instagram's UI
    does not cover it.  The renderer places the logo with this same box.
    """
    w, h = float(overlay['width_px']), float(overlay['height_px'])
    m = float(overlay.get('margin_px', 0))
    corner = overlay['corner']
    if corner == 'top_left':
        x0, y0 = zone['x_min'] + m, zone['y_min'] + m
    elif corner == 'top_right':
        x0, y0 = zone['x_max'] - m - w, zone['y_min'] + m
    elif corner == 'bottom_left':
        x0, y0 = zone['x_min'] + m, zone['y_max'] - m - h
    else:
        raise ValueError('logo corner must be one of %s, got %r' % (LOGO_CORNERS, corner))
    return x0, y0, x0 + w, y0 + h


def crop_window(clip: Mapping, crop: Mapping, *,
                canvas: Mapping = CANVAS) -> Tuple[float, float, float, float, float]:
    """Source window a segment shows: (x0, y0, x1, y1, scale) in display pixels.

    The display frame (after rotation) is scaled to cover the canvas, times
    ``zoom``, and the canvas-sized window is centred on (cx, cy), given as
    fractions of the display frame.  ``scale`` is source->canvas
    magnification (the upscale).  The renderer crops this same window.
    """
    dw, dh = float(clip['display_w']), float(clip['display_h'])
    scale = max(canvas['w'] / dw, canvas['h'] / dh) * float(crop['zoom'])
    half_w, half_h = canvas['w'] / scale / 2.0, canvas['h'] / scale / 2.0
    cx, cy = float(crop['cx']) * dw, float(crop['cy']) * dh
    return cx - half_w, cy - half_h, cx + half_w, cy + half_h, scale


def segment_face_box(edl: Mapping, seg: Mapping) -> Optional[Tuple[float, float, float, float]]:
    """(x0, y0, x1, y1) canvas pixels of a segment's face box, or None when
    the segment has none.  The face is fractions of its clip's display
    frame; the segment's crop window (crop_window, the one the renderer
    crops) maps it onto the canvas.  The overlay rules and the overlay
    layout (motion/layout.py) both read this."""
    face = seg.get('face')
    if not isinstance(face, Mapping):
        return None
    clip = edl['clips'][seg['clip']]
    dw, dh = float(clip['display_w']), float(clip['display_h'])
    x0, y0, _x1, _y1, scale = crop_window(clip, seg['crop'])
    fx0, fy0 = float(face['x']) * dw, float(face['y']) * dh
    fx1, fy1 = fx0 + float(face['w']) * dw, fy0 + float(face['h']) * dh
    return ((fx0 - x0) * scale, (fy0 - y0) * scale, (fx1 - x0) * scale, (fy1 - y0) * scale)


def failed_checks(problems: Sequence[str]) -> List[str]:
    """The distinct check ids in a validator's problem list, sorted."""
    return sorted({p.split(':', 1)[0] for p in problems})


# ── value types ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Box:
    """A box as fractions of the display frame (0..1), e.g. a face."""
    x: float
    y: float
    w: float
    h: float

    def __post_init__(self):
        if not all(_is_num(v) for v in (self.x, self.y, self.w, self.h)) \
                or self.w < 0 or self.h < 0:
            raise ValueError('Box needs finite numbers and w, h >= 0: %r' % (self,))

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    def to_dict(self) -> Dict[str, float]:
        return {'x': self.x, 'y': self.y, 'w': self.w, 'h': self.h}


@dataclass(frozen=True)
class Segment:
    """A shot found in one clip: source seconds [t0, t1)."""
    t0: float
    t1: float

    @property
    def dur(self) -> float:
        return self.t1 - self.t0


@dataclass(frozen=True)
class Quality:
    """Per-frame quality.  ``score`` (0..1) is what appeal uses (10 * score)."""
    score: float
    sharp: float
    luma: float
    clipped: float
    face_area: float = 0.0


@dataclass(frozen=True)
class Candidate:
    """A frame kept from the analysis pass: JPEG at a 768 px long side."""
    clip: str
    t_s: float
    segment: int
    jpeg: bytes = field(repr=False)
    quality: Optional[Quality] = None
    face: Optional[Box] = None


#: A keyframe is a Candidate chosen for VLM scoring.
Keyframe = Candidate


@dataclass(frozen=True)
class Moment:
    """A scored keyframe (moments list ``hart.reel.moments.v1``)."""
    clip: str
    t_s: float
    stage: str
    appeal: float
    face_visible: bool
    src: str

    def __post_init__(self):
        if self.stage not in STAGES:
            raise ValueError('stage must be one of %s, got %r' % (STAGES, self.stage))
        if not (_is_num(self.appeal) and 0 <= self.appeal <= 10):
            raise ValueError('appeal must be within 0..10, got %r' % (self.appeal,))
        if self.src not in MOMENT_SOURCES:
            raise ValueError('src must be one of %s, got %r' % (MOMENT_SOURCES, self.src))

    def to_dict(self) -> Dict[str, Any]:
        return {'clip': self.clip, 't_s': self.t_s, 'stage': self.stage,
                'appeal': self.appeal, 'face_visible': self.face_visible, 'src': self.src}


@dataclass(frozen=True)
class BeatGrid:
    """Beat times the cuts snap to."""
    bpm: float
    times_s: Tuple[float, ...]
    source: str
    confidence: float
    song_key: Optional[str] = None

    def __post_init__(self):
        if self.source not in BEAT_SOURCES:
            raise ValueError('source must be one of %s, got %r' % (BEAT_SOURCES, self.source))

    def to_edl(self) -> Dict[str, Any]:
        return {'source': self.source, 'song_key': self.song_key, 'bpm': self.bpm,
                'times_s': list(self.times_s), 'confidence': self.confidence}


@dataclass(frozen=True)
class ClipCapture:
    """Where a clip sits in the story: its capture-order rank and the time used."""
    clip: str
    order: int
    captured_at_ms: Optional[int]
    src: str


@dataclass(frozen=True)
class ClipInfo:
    """What probing a clip found.

    width/height are the coded size; display_w/display_h are after the
    display-matrix ``rotation`` (degrees, 0/90/180/270).  ``creation_time``
    is the container's, as epoch seconds.  ``hfr`` is fps_avg >= HFR_MIN_FPS.
    """
    path: str
    duration_s: float
    width: int
    height: int
    display_w: int
    display_h: int
    rotation: int
    fps_avg: float
    codec: str
    pix_fmt: str = ''
    bit_depth: int = 8
    transfer: str = 'sdr'
    dovi_profile: Optional[int] = None
    hfr: bool = False
    has_audio: bool = False
    creation_time: Optional[float] = None

    def __post_init__(self):
        if self.transfer not in TRANSFERS:
            raise ValueError('transfer must be one of %s, got %r' % (TRANSFERS, self.transfer))
        if not (_is_int(self.rotation) and self.rotation in ROTATIONS):
            raise ValueError('rotation must be one of %s, got %r' % (ROTATIONS, self.rotation))

    def to_edl_clip(self, capture: ClipCapture) -> Dict[str, Any]:
        """This clip's entry in an EDL's ``clips`` map."""
        return {'path': self.path, 'duration_s': self.duration_s, 'rotation': self.rotation,
                'transfer': self.transfer, 'fps_avg': self.fps_avg,
                'display_w': self.display_w, 'display_h': self.display_h, 'hfr': self.hfr,
                'order': capture.order, 'captured_at_ms': capture.captured_at_ms,
                'capture_src': capture.src}


@dataclass(frozen=True)
class ClipSource:
    """An inbound clip as the pipeline sees it, read from the store's record.

    ``committed_at`` (the store's created_at) gives the upload order;
    ``captured_at_ms`` is the phone's capture time from the offer's meta.
    """
    xid: str
    path: str
    name: str
    mime: str
    size: int
    sha256: str
    committed_at: float
    captured_at_ms: Optional[int] = None
    duration_ms: Optional[int] = None

    @classmethod
    def from_stored(cls, stored) -> 'ClipSource':
        """From a user_file_store StoredFile, or a dict with the same fields."""
        if isinstance(stored, Mapping):
            get = stored.get
        else:
            def get(key, default=None):
                return getattr(stored, key, default)
        meta = get('meta') or {}
        captured = meta.get('captured_at')
        duration = meta.get('duration_ms')
        return cls(xid=str(get('xid')), path=str(get('path')), name=str(get('name') or ''),
                   mime=str(get('mime') or ''), size=int(get('size') or 0),
                   sha256=str(get('sha256') or ''),
                   committed_at=float(get('created_at') or 0.0),
                   captured_at_ms=captured if is_capture_ms(captured) else None,
                   duration_ms=duration if _is_int(duration) and duration >= 0 else None)


@dataclass
class ClipAnalysis:
    """One clip's single streaming analysis pass (bounded memory).

    The arrays are numpy arrays, one value per analysed frame (5 fps);
    ``candidates`` keeps at most a few JPEGs per segment.  ``capture`` is
    filled in by the pipeline from resolve_capture_order.
    """
    clip: str
    info: ClipInfo
    times: Any = None
    corr: Any = None
    corr_skip: Any = None
    sharp: Any = None
    luma: Any = None
    clipped: Any = None
    shifts: Any = None
    faces: List[Optional[Box]] = field(default_factory=list)
    segments: List[Segment] = field(default_factory=list)
    candidates: List[Candidate] = field(default_factory=list)
    capture: Optional[ClipCapture] = None


@dataclass(frozen=True)
class RenderResult:
    path: str
    duration_s: float
    size_bytes: int
    encoder: str
    notes: Tuple[str, ...] = ()


# ── capture order (A1) ────────────────────────────────────────────────────

def _phone_ms(value) -> Optional[int]:
    return value if _is_int(value) and value >= MIN_CAPTURE_MS else None


def _container_ms(value) -> Optional[int]:
    if not _is_num(value):
        return None
    ms = int(round(value * 1000))
    return ms if ms >= MIN_CAPTURE_MS else None


def resolve_capture_order(clips: Sequence[Mapping]) -> Tuple[List[ClipCapture], str]:
    """Put clips in the order they were filmed.

    ``clips`` are given in upload/commit order, each
    ``{'clip': id, 'captured_at_ms': phone ms or None, 'creation_time':
    container epoch seconds or None}``.  Per clip the phone's time wins over
    the container's; implausible times count as missing.  If every clip has
    a time the clips are sorted by it (ties keep upload order); if any clip
    has none, the whole batch keeps upload order, since a guess would put
    that clip anywhere.

    Returns (captures sorted by ``order``, batch source), the source being
    one of CAPTURE_SOURCES, for the EDL's ``capture_src`` and its note.
    """
    facts = []
    seen = set()
    for index, item in enumerate(clips):
        clip = item.get('clip')
        if not isinstance(clip, str) or clip in seen:
            raise ValueError('each clip needs a distinct string id, got %r' % (clip,))
        seen.add(clip)
        phone = _phone_ms(item.get('captured_at_ms'))
        container = _container_ms(item.get('creation_time'))
        if phone is not None:
            facts.append((index, clip, phone, 'phone'))
        elif container is not None:
            facts.append((index, clip, container, 'container'))
        else:
            facts.append((index, clip, None, None))
    if facts and all(f[2] is not None for f in facts):
        ordered = sorted(facts, key=lambda f: (f[2], f[0]))
        kinds = {f[3] for f in facts}
        batch = kinds.pop() if len(kinds) == 1 else 'phone+container'
        return ([ClipCapture(clip=c, order=k, captured_at_ms=ts, src=src)
                 for k, (_i, c, ts, src) in enumerate(ordered)], batch)
    return ([ClipCapture(clip=c, order=i, captured_at_ms=ts, src='upload_order')
             for i, c, ts, _src in facts], 'upload_order')


# ── the EDL ───────────────────────────────────────────────────────────────

def _is_pos(v) -> bool:
    return _is_num(v) and v > 0


def _is_nonneg(v) -> bool:
    return _is_num(v) and v >= 0


def _is_pos_int(v) -> bool:
    return _is_int(v) and v > 0


def _is_nonneg_int(v) -> bool:
    return _is_int(v) and v >= 0


_CLIP_SPEC = {
    'path': (_is_text, 'a non-empty string'),
    'duration_s': (_is_pos, 'a number > 0'),
    'rotation': (lambda v: _is_int(v) and v in ROTATIONS, 'one of %s' % (ROTATIONS,)),
    'transfer': (lambda v: v in TRANSFERS, 'one of %s' % (TRANSFERS,)),
    'fps_avg': (_is_pos, 'a number > 0'),
    'display_w': (_is_pos_int, 'an int > 0'),
    'display_h': (_is_pos_int, 'an int > 0'),
    'hfr': (lambda v: isinstance(v, bool), 'true or false'),
    'order': (_is_nonneg_int, 'an int >= 0'),
    'captured_at_ms': (lambda v: v is None or _is_nonneg_int(v), 'null or epoch ms'),
    'capture_src': (lambda v: v in CLIP_CAPTURE_SOURCES, 'one of %s' % (CLIP_CAPTURE_SOURCES,)),
}
_CROP_SPEC = {'cx': (_is_num, 'a number'), 'cy': (_is_num, 'a number'),
              'zoom': (_is_pos, 'a number > 0')}
_FACE_SPEC = {'x': (_is_num, 'a number'), 'y': (_is_num, 'a number'),
              'w': (_is_nonneg, 'a number >= 0'), 'h': (_is_nonneg, 'a number >= 0')}
_TRANSITION_SPEC = {'type': (_is_text, 'a non-empty string'),
                    'dur_s': (_is_nonneg, 'a number >= 0')}
_SEGMENT_SPEC = {
    'clip': (_is_str, 'a clip id'),
    'in_s': (_is_nonneg, 'a number >= 0'),
    'out_s': (_is_num, 'a number'),
    'speed': (_is_pos, 'a number > 0'),
    'out_start_s': (_is_num, 'a number'),
    'out_end_s': (_is_num, 'a number'),
    'crop': (lambda v: isinstance(v, Mapping), 'an object'),
    'face': (lambda v: v is None or isinstance(v, Mapping), 'null or an object'),
    'transition': (lambda v: isinstance(v, Mapping), 'an object'),
    'stage': (lambda v: v in STAGES, 'one of %s' % (STAGES,)),
    'appeal': (_is_num, 'a number'),
    'role': (lambda v: v in ROLES, 'one of %s' % (ROLES,)),
}
_TEXT_OVERLAY_SPEC = {
    'text': (_is_text, 'a non-empty string'), 'font': (_is_str, 'a string'),
    'size_px': (_is_pos, 'a number > 0'), 'color': (_is_str, 'a string'),
    'box': (lambda v: isinstance(v, bool), 'true or false'),
    'x': (_is_num, 'a number'), 'y': (_is_num, 'a number'),
    'anchor': (lambda v: v == 'center', "'center'"),
    'from_s': (_is_num, 'a number'), 'to_s': (lambda v: v is None or _is_num(v), 'null or a number'),
}
_LOGO_OVERLAY_SPEC = {
    'src': (_is_text, 'a non-empty string'),
    'corner': (lambda v: v in LOGO_CORNERS, 'one of %s' % (LOGO_CORNERS,)),
    'width_px': (_is_pos, 'a number > 0'), 'height_px': (_is_pos, 'a number > 0'),
    'margin_px': (_is_nonneg, 'a number >= 0'),
    'opacity': (lambda v: _is_num(v) and 0 < v <= 1, 'a number in (0, 1]'),
    'from_s': (_is_num, 'a number'), 'to_s': (lambda v: v is None or _is_num(v), 'null or a number'),
}


def _is_box(v) -> bool:
    """[x0, y0, x1, y1] canvas pixels with x0 < x1 and y0 < y1."""
    return (isinstance(v, (list, tuple)) and len(v) == 4 and all(_is_num(n) for n in v)
            and v[0] < v[2] and v[1] < v[3])


_MOTION_OVERLAY_SPEC = {
    'template': (lambda v: v in MOTION_TEMPLATES, 'one of %s' % (MOTION_TEMPLATES,)),
    'src': (_is_text, 'a non-empty string'),
    'box': (_is_box, '[x0, y0, x1, y1] canvas pixels, x0 < x1 and y0 < y1'),
    # A motion clip has a length: it ends.
    'from_s': (_is_num, 'a number'), 'to_s': (_is_num, 'a number'),
}
_BEATS_SPEC = {
    'source': (lambda v: v in BEAT_SOURCES, 'one of %s' % (BEAT_SOURCES,)),
    'song_key': (_is_opt_str, 'null or a string'),
    'bpm': (_is_pos, 'a number > 0'),
    'times_s': (lambda v: isinstance(v, (list, tuple)) and all(_is_num(t) for t in v)
                and all(a <= b for a, b in zip(v, v[1:])), 'an ascending list of numbers'),
    'confidence': (lambda v: _is_num(v) and 0 <= v <= 1, 'a number in [0, 1]'),
}
_EDL_SPEC = {
    'schema': (lambda v: v == EDL_SCHEMA, repr(EDL_SCHEMA)),
    'canvas': (lambda v: isinstance(v, Mapping) and dict(v) == dict(CANVAS), repr(dict(CANVAS))),
    'target_s': (_is_num, 'a number'),
    'duration_s': (_is_num, 'a number'),
    'audio': (lambda v: v is None or isinstance(v, Mapping), 'null or an object'),
    'final_look_found': (lambda v: isinstance(v, bool), 'true or false'),
    'hook_first': (lambda v: isinstance(v, bool), 'true or false'),
    'capture_src': (lambda v: v in CAPTURE_SOURCES, 'one of %s' % (CAPTURE_SOURCES,)),
    'reveal': (lambda v: v is None or isinstance(v, Mapping), 'null or an object'),
    'notes': (_is_str_list, 'a list of strings'),
    'beats': (lambda v: isinstance(v, Mapping), 'an object'),
    'clips': (lambda v: isinstance(v, Mapping) and len(v) > 0, 'a non-empty object'),
    'segments': (lambda v: isinstance(v, (list, tuple)) and len(v) > 0, 'a non-empty list'),
    'overlays': (lambda v: isinstance(v, (list, tuple)), 'a list'),
}


def _edl_schema(edl) -> List[str]:
    problems = _spec_problems(edl, _EDL_SPEC, 'edl')
    if problems:
        return problems
    problems += _spec_problems(edl['beats'], _BEATS_SPEC, 'beats')
    if edl['reveal'] is not None:
        problems += _spec_problems(edl['reveal'], {'clip': (_is_str, 'a clip id'),
                                                   't_s': (_is_num, 'a number')}, 'reveal')
    for xid, clip in edl['clips'].items():
        problems += _spec_problems(clip, _CLIP_SPEC, 'clips[%s]' % (xid,))
    for i, seg in enumerate(edl['segments']):
        where = 'segments[%d]' % i
        found = _spec_problems(seg, _SEGMENT_SPEC, where)
        if not found:
            found += _spec_problems(seg['crop'], _CROP_SPEC, where + '.crop')
            found += _spec_problems(seg['transition'], _TRANSITION_SPEC, where + '.transition')
            if seg['face'] is not None:
                found += _spec_problems(seg['face'], _FACE_SPEC, where + '.face')
            if seg['clip'] not in edl['clips']:
                found.append('%s.clip names no clip in clips' % where)
            if not seg['out_s'] > seg['in_s']:
                found.append('%s.out_s must be after in_s' % where)
        problems += found
    for i, ov in enumerate(edl['overlays']):
        where = 'overlays[%d]' % i
        kind = ov.get('type') if isinstance(ov, Mapping) else None
        if kind == 'text':
            problems += _spec_problems(ov, _TEXT_OVERLAY_SPEC, where)
        elif kind == 'logo':
            problems += _spec_problems(ov, _LOGO_OVERLAY_SPEC, where)
        elif kind == 'motion':
            problems += _spec_problems(ov, _MOTION_OVERLAY_SPEC, where)
        else:
            problems.append("%s.type must be 'text', 'logo' or 'motion'" % where)
    if (isinstance(edl['reveal'], Mapping) and edl['reveal'].get('clip') not in edl['clips']
            and not problems):
        problems.append('reveal.clip names no clip in clips')
    return problems


def _seg_len(seg) -> float:
    return seg['out_end_s'] - seg['out_start_s']


def _check_contiguous(edl, bar) -> List[str]:
    eps = bar['contiguous']['eps_s']
    out, prev_end = [], 0.0
    for i, seg in enumerate(edl['segments']):
        if abs(seg['out_start_s'] - prev_end) > eps:
            out.append('segment %d starts at %.3f s but the timeline is at %.3f s'
                       % (i, seg['out_start_s'], prev_end))
        want = (seg['out_s'] - seg['in_s']) / seg['speed']
        if abs(_seg_len(seg) - want) > eps:
            out.append('segment %d lasts %.3f s on the timeline but (out_s-in_s)/speed is %.3f s'
                       % (i, _seg_len(seg), want))
        prev_end = seg['out_end_s']
    if abs(edl['duration_s'] - prev_end) > eps:
        out.append('duration_s is %.3f s but the last segment ends at %.3f s'
                   % (edl['duration_s'], prev_end))
    return out


def _check_duration(edl, bar) -> List[str]:
    out = []
    lo, hi = TARGET_S_RANGE
    target = edl['target_s']
    if not lo <= target <= hi:
        out.append('target_s %.3f is outside %s..%s s' % (target, lo, hi))
    tol = bar['duration']['tol_s']
    if abs(edl['duration_s'] - target) > tol:
        out.append('duration %.3f s is more than %.3f s from the %.3f s target'
                   % (edl['duration_s'], tol, target))
    return out


def _check_shot_rules(edl, bar) -> List[str]:
    rules, eps = bar['shot_rules'], bar['contiguous']['eps_s']
    out = []
    for i, seg in enumerate(edl['segments']):
        clip = edl['clips'][seg['clip']]
        if _seg_len(seg) < rules['min_shot_s'] - eps:
            out.append('segment %d is %.3f s, under the %.3f s minimum shot'
                       % (i, _seg_len(seg), rules['min_shot_s']))
        if not rules['speed_min'] <= seg['speed'] <= rules['speed_max']:
            out.append('segment %d speed %.3f is outside %.2f..%.2f'
                       % (i, seg['speed'], rules['speed_min'], rules['speed_max']))
        scale = crop_window(clip, seg['crop'])[4]
        if scale > rules['max_upscale'] + 1e-9:
            out.append('segment %d magnifies its source %.3fx, over the %.2fx cap'
                       % (i, scale, rules['max_upscale']))
        if seg['out_s'] > clip['duration_s'] + eps:
            out.append('segment %d ends at %.3f s but its clip is %.3f s long'
                       % (i, seg['out_s'], clip['duration_s']))
    return out


def _check_beats(edl, bar) -> List[str]:
    tol = bar['beats']['tol_s']
    times = list(edl['beats']['times_s'])
    out = []
    for i, seg in enumerate(edl['segments'][:-1]):
        cut = seg['out_end_s']
        if not times:
            out.append('cut at %.3f s with an empty beat grid' % cut)
            continue
        k = bisect.bisect_left(times, cut)
        nearest = min(abs(times[j] - cut) for j in (k - 1, k) if 0 <= j < len(times))
        if nearest > tol + 1e-9:
            out.append('cut at %.3f s (after segment %d) is %.3f s from the nearest beat'
                       % (cut, i, nearest))
    return out


def _check_no_bars(edl, bar) -> List[str]:
    out = []
    for i, seg in enumerate(edl['segments']):
        clip = edl['clips'][seg['clip']]
        if seg['crop']['zoom'] < bar['no_bars']['min_zoom']:
            out.append('segment %d zoom %.3f is under %.2f, which leaves bars'
                       % (i, seg['crop']['zoom'], bar['no_bars']['min_zoom']))
        x0, y0, x1, y1, _ = crop_window(clip, seg['crop'])
        if (x0 < -_PX_EPS or y0 < -_PX_EPS or x1 > clip['display_w'] + _PX_EPS
                or y1 > clip['display_h'] + _PX_EPS):
            out.append('segment %d crop window (%.1f, %.1f)-(%.1f, %.1f) leaves its %dx%d source'
                       % (i, x0, y0, x1, y1, clip['display_w'], clip['display_h']))
    return out


def _check_face(edl, bar) -> List[str]:
    lo, hi = bar['face']['cy_min'], bar['face']['cy_max']
    out = []
    for i, seg in enumerate(edl['segments']):
        face = seg['face']
        if face is None:
            continue
        clip = edl['clips'][seg['clip']]
        dw, dh = clip['display_w'], clip['display_h']
        x0, y0, x1, y1, _ = crop_window(clip, seg['crop'])
        fx0, fy0 = face['x'] * dw, face['y'] * dh
        fx1, fy1 = (face['x'] + face['w']) * dw, (face['y'] + face['h']) * dh
        if (fx0 < x0 - _PX_EPS or fy0 < y0 - _PX_EPS or fx1 > x1 + _PX_EPS
                or fy1 > y1 + _PX_EPS):
            out.append('segment %d crops the face out of the frame' % i)
            continue
        cy = ((fy0 + fy1) / 2.0 - y0) / (y1 - y0)
        if not lo <= cy <= hi:
            out.append('segment %d puts the face centre at %.2f of the height, outside %.2f..%.2f'
                       % (i, cy, lo, hi))
    return out


def _check_order(edl, bar) -> List[str]:
    clips, segs = edl['clips'], edl['segments']
    out = []
    orders = [c['order'] for c in clips.values()]
    if len(set(orders)) != len(orders):
        out.append('two clips share a capture-order rank: %s' % sorted(orders))
    previous = None
    for i, seg in enumerate(segs):
        if seg['role'] == 'teaser':
            continue
        key = (clips[seg['clip']]['order'], seg['in_s'])
        if previous is not None and key < previous[1]:
            out.append('segment %d (clip rank %d, %.3f s) plays after segment %d '
                       '(clip rank %d, %.3f s): the story must run in capture order'
                       % (i, key[0], key[1], previous[0], previous[1][0], previous[1][1]))
        previous = (i, key)
    src = edl['capture_src']
    if src != 'upload_order':
        ranked = sorted(clips.items(), key=lambda kv: kv[1]['order'])
        stamps = [c['captured_at_ms'] for _x, c in ranked]
        if any(s is None for s in stamps):
            out.append('capture_src is %r but a clip has no captured_at_ms' % src)
        elif any(a > b for a, b in zip(stamps, stamps[1:])):
            out.append('clip ranks do not follow captured_at_ms')
    if capture_note(src) not in edl['notes']:
        out.append('notes do not record %r' % capture_note(src))
    return out


def _check_reveal(edl, bar) -> List[str]:
    segs, eps = edl['segments'], bar['contiguous']['eps_s']
    last = segs[-1]
    reveals = [i for i, s in enumerate(segs) if s['role'] == 'reveal']
    out = []
    if not edl['final_look_found']:
        if reveals:
            out.append('segment %d is a reveal but no final look was found' % reveals[0])
        if edl['reveal'] is not None:
            out.append('reveal is set but no final look was found')
        if NOTE_NO_FINAL_LOOK not in edl['notes']:
            out.append('notes do not say %r' % NOTE_NO_FINAL_LOOK)
        return out
    if reveals != [len(segs) - 1]:
        out.append('the reveal must be the last segment and only the last (reveal roles at %s)'
                   % reveals)
    if last['stage'] != FINAL_LOOK:
        out.append('the last segment shows %r, not the final look' % last['stage'])
    hold = bar['reveal']['min_hold_s']
    if _seg_len(last) < hold - eps:
        out.append('the reveal holds %.3f s, under %.3f s' % (_seg_len(last), hold))
    pick = edl['reveal']
    if pick is None:
        out.append('final_look_found but reveal names no shot')
    elif pick['clip'] != last['clip'] or not (last['in_s'] - eps <= pick['t_s']
                                               <= last['out_s'] + eps):
        out.append('the last segment (%s %.3f-%.3f s) is not the chosen final look (%s at %.3f s)'
                   % (last['clip'][:12], last['in_s'], last['out_s'], pick['clip'][:12],
                      pick['t_s']))
    return out


def _check_teaser(edl, bar) -> List[str]:
    segs, eps = edl['segments'], bar['contiguous']['eps_s']
    teasers = [i for i, s in enumerate(segs) if s['role'] == 'teaser']
    out = []
    if not edl['hook_first']:
        if teasers:
            out.append('segment %d is a teaser but hook_first is false' % teasers[0])
        return out
    if teasers != [0]:
        out.append('hook_first needs exactly one teaser, as the first segment (teasers at %s)'
                   % teasers)
    if not edl['final_look_found']:
        out.append('hook_first needs a final look to tease')
    first = segs[0]
    if first['role'] == 'teaser':
        if first['stage'] != FINAL_LOOK:
            out.append('the teaser shows %r, not the final look' % first['stage'])
        cap = bar['teaser']['max_s']
        if _seg_len(first) > cap + eps:
            out.append('the teaser lasts %.3f s, over %.3f s' % (_seg_len(first), cap))
    return out


def _check_overlays(edl, bar) -> List[str]:
    zone, eps = SAFE_ZONE, bar['contiguous']['eps_s']
    dur = edl['duration_s']
    out = []
    for i, ov in enumerate(edl['overlays']):
        start, end = ov['from_s'], ov['to_s']
        if start < -eps or start >= dur or (end is not None and (end <= start or end > dur + eps)):
            out.append('overlay %d runs %s..%s s, outside the %.3f s reel' % (i, start, end, dur))
        if ov['type'] == 'text':
            # The anchor and one line's height are checkable here; the width
            # depends on the font, so the renderer wraps text to the zone.
            x = ov['x'] * CANVAS['w']
            y = ov['y'] * CANVAS['h']
            half = ov['size_px'] / 2.0
            if not (zone['x_min'] <= x <= zone['x_max']
                    and zone['y_min'] <= y - half and y + half <= zone['y_max']):
                out.append('text overlay %d at (%.0f, %.0f) px leaves the safe zone' % (i, x, y))
        elif ov['type'] == 'logo':
            x0, y0, x1, y1 = logo_box(ov)
            if not (zone['x_min'] <= x0 and x1 <= zone['x_max']
                    and zone['y_min'] <= y0 and y1 <= zone['y_max']):
                out.append('logo %d box (%.0f, %.0f)-(%.0f, %.0f) px leaves the safe zone'
                           % (i, x0, y0, x1, y1))
        elif ov['template'] in MOTION_FULL_FRAME:
            out += _full_frame_problems(i, ov, edl, bar)
        else:
            x0, y0, x1, y1 = ov['box']
            if not (zone['x_min'] <= x0 and x1 <= zone['x_max']
                    and zone['y_min'] <= y0 and y1 <= zone['y_max']):
                out.append('%s %d box (%.0f, %.0f)-(%.0f, %.0f) px leaves the safe zone'
                           % (ov['template'], i, x0, y0, x1, y1))
            out += _face_hold_problems(i, ov, edl, bar)
    return out


def _face_hold_problems(i: int, ov: Mapping, edl: Mapping, bar: Mapping) -> List[str]:
    """A motion box (other than the full-frame wipe, which leaves the whole
    final look uncovered for reveal.min_hold_s) never covers her face during
    the final look's last reveal.min_hold_s: A1, the reel ends on the
    reveal, and QA masks the whole box, so reveal_render cannot see it."""
    if not edl['final_look_found']:
        return []
    face = segment_face_box(edl, edl['segments'][-1])
    if face is None:
        return []
    eps, hold = bar['contiguous']['eps_s'], bar['reveal']['min_hold_s']
    if ov['to_s'] <= edl['duration_s'] - hold + eps:
        return []
    x0, y0, x1, y1 = ov['box']
    if x1 <= face[0] + _PX_EPS or face[2] <= x0 + _PX_EPS or y1 <= face[1] + _PX_EPS \
            or face[3] <= y0 + _PX_EPS:
        return []
    return ['%s %d box (%.0f, %.0f)-(%.0f, %.0f) px covers her face (%.0f, %.0f)-(%.0f, %.0f) '
            'in the final look\'s last %.1f s' % (ov['template'], i, x0, y0, x1, y1, face[0],
                                                   face[1], face[2], face[3], hold)]


def _full_frame_problems(i: int, ov: Mapping, edl: Mapping, bar: Mapping) -> List[str]:
    """A full-frame motion overlay (the before->after wipe) fills the frame,
    only over the reveal's opening, and leaves the final look uncovered for
    reveal.min_hold_s at the end: A1, the reel ends on the reveal."""
    eps, name = bar['contiguous']['eps_s'], ov['template']
    out = []
    if any(abs(a - b) > _PX_EPS for a, b in zip(ov['box'], (0, 0, CANVAS['w'], CANVAS['h']))):
        out.append('%s %d box %s does not fill the %dx%d frame'
                   % (name, i, list(ov['box']), CANVAS['w'], CANVAS['h']))
    if not edl['final_look_found']:
        out.append('%s %d has no final look to reveal' % (name, i))
        return out
    opening = edl['segments'][-1]['out_start_s']
    if ov['from_s'] < opening - eps:
        out.append('%s %d starts at %.3f s, before the reveal at %.3f s'
                   % (name, i, ov['from_s'], opening))
    left, hold = edl['duration_s'] - ov['to_s'], bar['reveal']['min_hold_s']
    if left < hold - eps:
        out.append('%s %d leaves the final look uncovered for %.3f s, under %.3f s'
                   % (name, i, left, hold))
    return out


def _check_transition(edl, bar) -> List[str]:
    allowed = bar['transition']['allowed']
    return ['segment %d uses %r; only %s in V1' % (i, s['transition']['type'], list(allowed))
            for i, s in enumerate(edl['segments'])
            if s['transition']['type'] not in allowed or s['transition']['dur_s'] > 0]


def _check_audio(edl, bar) -> List[str]:
    if edl['audio'] is not None and not bar['audio']['allow_audio']:
        return ['the posted reel is silent in V1; audio must be null']
    return []


_EDL_CHECK_FUNCS = (
    ('contiguous', _check_contiguous), ('duration', _check_duration),
    ('shot_rules', _check_shot_rules), ('beats', _check_beats), ('no_bars', _check_no_bars),
    ('face', _check_face), ('order', _check_order), ('reveal', _check_reveal),
    ('teaser', _check_teaser), ('overlays', _check_overlays),
    ('transition', _check_transition), ('audio', _check_audio),
)


def validate_edl(edl, *, bar: Mapping = QUALITY_BAR) -> List[str]:
    """Problems with an EDL (``hart.reel.edl.v1``); empty when it is valid.

    Structure is checked first; a structurally broken EDL reports only
    ``schema`` problems.  Then every rule runs and reports under its
    QUALITY_BAR check id, with thresholds read from ``bar``:

    contiguous  segments tile 0..duration_s; each lasts (out_s-in_s)/speed
    duration    target_s within TARGET_S_RANGE; |duration_s - target_s| <= tol
    shot_rules  shot >= min_shot_s; speed in range; upscale <= cap; inside its clip
    beats       every cut within tol_s of a beat
    no_bars     zoom >= 1 and the crop window inside the display frame
    face        face box inside the crop, its centre in the cy band
    order       non-teaser segments ascend by (clip capture rank, in_s); ranks
                follow captured_at_ms; the notes record capture_src (A1)
    reveal      with a final look: the last segment, and only it, is the
                reveal, shows final_look, holds >= min_hold_s and covers the
                chosen shot; without one: no reveal and a 'no_final_look' note
    teaser      hook_first <=> one final-look teaser first, <= max_s (A1)
    overlays    inside SAFE_ZONE and inside the reel's time; a full-frame
                motion overlay (MOTION_FULL_FRAME: the before->after wipe)
                fills the frame over the reveal's opening only, leaving the
                final look uncovered for reveal.min_hold_s at the end; any
                other motion box keeps off her face (segment_face_box of the
                reveal) during that last reveal.min_hold_s
    transition  cuts only
    audio       the posted reel is silent (U2)
    """
    problems = _edl_schema(edl)
    if problems:
        return ['schema: ' + p for p in problems]
    out = []
    for check, func in _EDL_CHECK_FUNCS:
        out += ['%s: %s' % (check, p) for p in func(edl, bar)]
    return out


# ── moments ───────────────────────────────────────────────────────────────

_MOMENT_SPEC = {
    'clip': (_is_str, 'a clip id'), 't_s': (_is_nonneg, 'a number >= 0'),
    'stage': (lambda v: v in STAGES, 'one of %s' % (STAGES,)),
    'appeal': (lambda v: _is_num(v) and 0 <= v <= 10, 'a number in 0..10'),
    'face_visible': (lambda v: isinstance(v, bool), 'true or false'),
    'src': (lambda v: v in MOMENT_SOURCES, 'one of %s' % (MOMENT_SOURCES,)),
}


def validate_moments(doc) -> List[str]:
    """Problems with a moment list (``hart.reel.moments.v1``)."""
    problems = _spec_problems(doc, {
        'schema': (lambda v: v == MOMENTS_SCHEMA, repr(MOMENTS_SCHEMA)),
        'src': (lambda v: v in MOMENT_SOURCES + ('mixed',), "'vlm', 'fallback' or 'mixed'"),
        'model': (_is_opt_str, 'null or a string'),
        'final_look_found': (lambda v: isinstance(v, bool), 'true or false'),
        'notes': (_is_str_list, 'a list of strings'),
        'moments': (lambda v: isinstance(v, (list, tuple)), 'a list'),
    }, 'moments')
    if problems:
        return problems
    for i, m in enumerate(doc['moments']):
        problems += _spec_problems(m, _MOMENT_SPEC, 'moments[%d]' % i)
    if problems or not doc['moments']:
        return problems
    found = any(m['stage'] == FINAL_LOOK for m in doc['moments'])
    if doc['final_look_found'] != found:
        problems.append('final_look_found is %s but the moments say %s'
                        % (doc['final_look_found'], found))
    kinds = {m['src'] for m in doc['moments']}
    want = kinds.pop() if len(kinds) == 1 else 'mixed'
    if doc['src'] != want:
        problems.append('src is %r but the moments are %r' % (doc['src'], want))
    return problems


# ── reply media (desktop -> phone) ────────────────────────────────────────

_SONG_SPEC = {
    'song_key': (_is_opt_str, 'null or a string'), 'title': (_is_str, 'a string'),
    'artist': (_is_str, 'a string'), 'lang': (_is_opt_str, 'null or a string'),
    'start_ms': (_is_nonneg_int, 'an int >= 0'), 'start_label': (_is_str, "an 'm:ss' string"),
    'beats': (lambda v: v in BEAT_SOURCES, 'one of %s' % (BEAT_SOURCES,)),
}


def _is_hashtags(v) -> bool:
    return (isinstance(v, (list, tuple)) and len(v) <= HASHTAG_LIMIT
            and all(isinstance(t, str) and len(t) > 1 and t.startswith('#')
                    and not any(c.isspace() for c in t) for t in v))


_MEDIA_SPEC = {
    'kind': (lambda v: v == 'video', "'video'"),
    'xid': (is_xid, '64 lowercase hex'),
    'sha256': (is_xid, '64 lowercase hex'),
    'mime': (lambda v: v == 'video/mp4', "'video/mp4'"),
    'size': (_is_pos_int, 'an int > 0'),
    'width': (lambda v: v == CANVAS['w'] and _is_int(v), str(CANVAS['w'])),
    'height': (lambda v: v == CANVAS['h'] and _is_int(v), str(CANVAS['h'])),
    'duration_s': (_is_pos, 'a number > 0'),
    'has_audio': (lambda v: isinstance(v, bool), 'true or false'),
    'reel_id': (_is_text, 'a non-empty string'),
    'prompt_id': (_is_text, 'a non-empty string (prompt ids travel as strings)'),
    'request_id': (_is_opt_str, 'null or a string'),
    'caption': (_is_str, 'a string'),
    'hashtags': (_is_hashtags, 'at most %d #tags' % HASHTAG_LIMIT),
    'song': (lambda v: v is None or isinstance(v, Mapping), 'null or an object'),
    'notes': (_is_str_list, 'a list of strings'),
    'qa': (lambda v: isinstance(v, Mapping), 'an object'),
}


def validate_media_item(item, *, bar: Mapping = QUALITY_BAR) -> List[str]:
    """Problems with a reply ``media`` item (the reel the phone receives).

    The same shape rides the /chat reply, the push and the outbox listing.
    A reel that failed a hard QA check is never delivered, so ``qa.passed``
    must be true.
    """
    problems = _spec_problems(item, _MEDIA_SPEC, 'media')
    if problems:
        return problems
    if item['sha256'] != item['xid']:
        problems.append('media.xid must be the sha256 of the file')
    if item['has_audio'] and not bar['audio']['allow_audio']:
        problems.append('media.has_audio: the posted reel is silent in V1')
    song = item['song']
    if song is not None:
        found = _spec_problems(song, _SONG_SPEC, 'media.song')
        if not found and song['start_label'] != start_label(song['start_ms']):
            found.append('media.song.start_label %r does not match start_ms %d (%r)'
                         % (song['start_label'], song['start_ms'], start_label(song['start_ms'])))
        problems += found
    qa = item['qa']
    if qa.get('passed') is not True:
        problems.append('media.qa.passed must be true: a reel that failed QA is not delivered')
    if not (_is_str_list(qa.get('failed')) and len(qa.get('failed')) == 0):
        problems.append('media.qa.failed must be an empty list')
    return problems


def validate_outbox_item(item) -> List[str]:
    """Problems with one ``file_items`` entry of a ``file_list`` reply."""
    problems = _spec_problems(item, {
        'xid': (is_xid, '64 lowercase hex'), 'name': (is_file_name, 'a bare file name'),
        'mime': (_is_text, 'a MIME type'), 'size': (_is_nonneg_int, 'an int >= 0'),
        'sha256': (is_xid, '64 lowercase hex'), 'created_at': (_is_num, 'epoch seconds'),
        'fetched': (lambda v: isinstance(v, bool), 'true or false'),
        'meta': (lambda v: isinstance(v, Mapping), 'an object'),
    }, 'item')
    if problems:
        return problems
    if item['sha256'] != item['xid']:
        problems.append('item.xid must be the sha256 of the file')
    meta = item['meta']
    problems += _spec_problems(meta, {
        'prompt_id': (_is_opt_str, 'null or a string'),
        'request_id': (_is_opt_str, 'null or a string'),
        'media': (lambda v: v is None or isinstance(v, Mapping), 'null or an object'),
    }, 'item.meta', optional=('media',))
    media = meta.get('media')
    if isinstance(media, Mapping):
        problems += ['item.meta.' + p for p in validate_media_item(media)]
        if media.get('xid') != item['xid']:
            problems.append('item.meta.media.xid is not the item xid')
    return problems


# ── inbound files (phone -> desktop) ──────────────────────────────────────

def _mime_problems(obj, where: str) -> List[str]:
    allowed = INBOUND_MIMES.get(obj['purpose'], frozenset())
    if obj['mime'] not in allowed:
        return ['%s.mime %r is not accepted for purpose %r (%s)'
                % (where, obj['mime'], obj['purpose'], sorted(allowed))]
    return []


def validate_file_offer(frame) -> List[str]:
    """Problems with a ``file_offer`` frame (phone -> desktop).

    ``captured_at`` (epoch ms, from MediaStore DATE_TAKEN) and
    ``duration_ms`` are optional so older phones still upload; the store
    keeps them in StoredFile.meta (INBOUND_META_KEYS) and the pipeline reads
    them back through ClipSource.from_stored.
    """
    problems = _spec_problems(frame, {
        'type': (lambda v: v == 'file_offer', "'file_offer'"),
        'name': (is_file_name, 'a bare file name'),
        'size': (_is_pos_int, 'an int > 0'),
        'sha256': (is_xid, '64 lowercase hex'),
        'mime': (_is_str, 'a MIME type'),
        'purpose': (lambda v: v in INBOUND_PURPOSES, 'one of %s' % (INBOUND_PURPOSES,)),
        'captured_at': (is_capture_ms, 'epoch milliseconds (an int > 0)'),
        'duration_ms': (_is_nonneg_int, 'an int >= 0'),
    }, 'file_offer', optional=INBOUND_META_KEYS)
    return problems or _mime_problems(frame, 'file_offer')


# A /chat turn's ``attachments`` row (and a chat-mirror row) is judged by
# validate_attachment, core.chat_attachments' one rule, imported above.


# ── trending songs ────────────────────────────────────────────────────────

def _leaky_key(key) -> bool:
    from security.edge_privacy import is_secret_key, secret_key_form
    return isinstance(key, str) and (
        is_secret_key(secret_key_form(key))
        or any(p in key.lower() for p in _SONG_LIST_KEY_PARTS))


def _leaks(value, where: str) -> List[str]:
    """URLs, secrets or session data anywhere inside a song list."""
    out = []
    if isinstance(value, Mapping):
        for key, sub in value.items():
            if _leaky_key(key):
                out.append('%s.%s: song lists carry no URLs or session data' % (where, key))
            out += _leaks(sub, '%s.%s' % (where, key))
    elif isinstance(value, (list, tuple)):
        for i, sub in enumerate(value):
            out += _leaks(sub, '%s[%d]' % (where, i))
    elif isinstance(value, str) and _URL.match(value):
        out.append('%s: song lists carry no URLs' % where)
    return out


_PUBLISHED_SONG_SPEC = {
    'song_key': (_is_text, 'a non-empty string'), 'title': (_is_str, 'a string'),
    'artist': (_is_str, 'a string'), 'suggested_start_ms': (_is_nonneg_int, 'an int >= 0'),
    'score': (_is_num, 'a number'), 'accounts': (_is_nonneg_int, 'an int >= 0'),
    'reels': (_is_nonneg_int, 'an int >= 0'),
    'language_hint': (_is_opt_str, 'null or a string'),
    'highlight_start_ms': (lambda v: isinstance(v, (list, tuple))
                           and all(_is_nonneg_int(x) for x in v), 'a list of ints'),
}
_CLIENT_SONG_SPEC = {
    'song_key': (_is_text, 'a non-empty string'), 'title': (_is_str, 'a string'),
    'artist': (_is_str, 'a string'), 'lang': (_is_opt_str, 'null or a string'),
    'suggested_start_ms': (_is_nonneg_int, 'an int >= 0'),
    'start_label': (_is_str, "an 'm:ss' string"), 'score': (_is_num, 'a number'),
}


def validate_trending_songs(doc) -> List[str]:
    """Problems with a trending-song list: central's published
    ``hart.trending_songs.v1`` or the desktop client's
    ``hart.trending_songs.client.v1``.  Neither may carry URLs or session
    data anywhere."""
    if not isinstance(doc, Mapping):
        return ['trending: not an object']
    schema = doc.get('schema')
    if schema == TRENDING_SCHEMA:
        problems = _spec_problems(doc, {
            'date': (lambda v: isinstance(v, str) and _DATE.fullmatch(v) is not None,
                     'YYYY-MM-DD'),
            'generated_at': (_is_text, 'a timestamp string'),
            'source': (_is_text, 'a non-empty string'),
            'window_days': (_is_pos_int, 'an int > 0'),
            'stale': (lambda v: isinstance(v, bool), 'true or false'),
            'songs': (lambda v: isinstance(v, (list, tuple)), 'a list'),
        }, 'trending')
        if not problems:
            for i, song in enumerate(doc['songs']):
                problems += _spec_problems(song, _PUBLISHED_SONG_SPEC, 'songs[%d]' % i,
                                           optional=('language_hint', 'highlight_start_ms'))
    elif schema == TRENDING_CLIENT_SCHEMA:
        problems = _spec_problems(doc, {
            'fetched_at': (_is_text, 'a timestamp string'),
            'stale': (lambda v: isinstance(v, bool), 'true or false'),
            'reason': (_is_opt_str, 'null or a string'),
            'songs': (lambda v: isinstance(v, (list, tuple)), 'a list'),
        }, 'trending')
        if not problems:
            for i, song in enumerate(doc['songs']):
                found = _spec_problems(song, _CLIENT_SONG_SPEC, 'songs[%d]' % i)
                if not found and song['start_label'] != start_label(song['suggested_start_ms']):
                    found.append('songs[%d].start_label %r does not match suggested_start_ms'
                                 % (i, song['start_label']))
                problems += found
    else:
        problems = ['trending.schema %r is not %r or %r'
                    % (schema, TRENDING_SCHEMA, TRENDING_CLIENT_SCHEMA)]
    return problems + _leaks(doc, 'trending')


# ── style ─────────────────────────────────────────────────────────────────

_STYLE_SPEC = {
    'target_s': (lambda v: _is_num(v) and TARGET_S_RANGE[0] <= v <= TARGET_S_RANGE[1],
                 'a number in %s..%s' % TARGET_S_RANGE),
    'hook_first': (lambda v: isinstance(v, bool), 'true or false'),
    'pace': (lambda v: v in STYLE_PACES, 'one of %s' % (STYLE_PACES,)),
    'hook_text': (_is_str, 'a string'),
    'use_logo': (lambda v: isinstance(v, bool), 'true or false'),
    'logo_corner': (lambda v: v in LOGO_CORNERS, 'one of %s' % (LOGO_CORNERS,)),
    'language': (lambda v: v is None or v in STYLE_LANGUAGES,
                 'null or one of %s' % (STYLE_LANGUAGES,)),
    'song_key': (_is_opt_str, 'null or a string'),
}


def validate_style(style) -> List[str]:
    """Problems with a style dict (the keys of STYLE_DEFAULTS, nothing else)."""
    problems = _spec_problems(style, _STYLE_SPEC, 'style')
    if isinstance(style, Mapping):
        problems += ['style.%s is not a style key' % k for k in style if k not in _STYLE_SPEC]
    return problems


def make_style(overrides: Optional[Mapping] = None) -> Dict[str, Any]:
    """STYLE_DEFAULTS with ``overrides`` applied and target_s clamped.

    Raises ValueError for an unknown key or a value validate_style refuses.
    """
    style = dict(STYLE_DEFAULTS)
    for key, value in (overrides or {}).items():
        if key not in _STYLE_SPEC:
            raise ValueError('%r is not a style key (%s)' % (key, sorted(_STYLE_SPEC)))
        style[key] = value
    style['target_s'] = clamp_target_s(style['target_s'])
    problems = validate_style(style)
    if problems:
        raise ValueError('; '.join(problems))
    return style


# ── QA report ─────────────────────────────────────────────────────────────

_QA_ROW_SPEC = {
    'id': (_is_str, 'a check id'), 'ok': (lambda v: isinstance(v, bool), 'true or false'),
    'hard': (lambda v: isinstance(v, bool), 'true or false'),
    'value': (lambda v: True, 'present'), 'want': (lambda v: True, 'present'),
}


def validate_qa_report(report, *, bar: Mapping = QUALITY_BAR) -> List[str]:
    """Problems with a QA report (``hart.reel.qa.v1``).

    Every row names a QUALITY_BAR check and carries that check's own
    ``hard`` flag (a report cannot soften a hard check); ``passed`` is true
    exactly when every hard row is ok; ``advisory`` rows are never hard.
    """
    problems = _spec_problems(report, {
        'schema': (lambda v: v == QA_SCHEMA, repr(QA_SCHEMA)),
        'passed': (lambda v: isinstance(v, bool), 'true or false'),
        'checks': (lambda v: isinstance(v, (list, tuple)), 'a list'),
        'advisory': (lambda v: isinstance(v, (list, tuple)), 'a list'),
    }, 'qa')
    if problems:
        return problems
    rows = [('checks', i, r) for i, r in enumerate(report['checks'])]
    rows += [('advisory', i, r) for i, r in enumerate(report['advisory'])]
    for where, i, row in rows:
        found = _spec_problems(row, _QA_ROW_SPEC, 'qa.%s[%d]' % (where, i))
        if not found:
            if row['id'] not in bar:
                found.append('qa.%s[%d].id %r is not a QUALITY_BAR check' % (where, i, row['id']))
            elif row['hard'] != bar[row['id']]['hard']:
                found.append('qa.%s[%d] marks %r hard=%s but the bar says %s'
                             % (where, i, row['id'], row['hard'], bar[row['id']]['hard']))
            if where == 'advisory' and row['hard']:
                found.append('qa.advisory[%d] is a hard check' % i)
        problems += found
    if problems:
        return problems
    should_pass = all(r['ok'] for r in report['checks'] if r['hard'])
    if report['passed'] != should_pass:
        problems.append('qa.passed is %s but the hard checks say %s'
                        % (report['passed'], should_pass))
    return problems


def qa_summary(report: Mapping) -> Dict[str, Any]:
    """``{'passed', 'failed': [hard check ids that failed]}`` for replies."""
    failed = [r['id'] for r in report.get('checks', ()) if r.get('hard') and not r.get('ok')]
    return {'passed': bool(report.get('passed')), 'failed': failed}
