"""integrations.reel_studio.media.edl -- the deterministic reel planner.

``plan_edit`` turns the analysed clips, the scored moments and a beat grid
into an edit decision list (``hart.reel.edl.v1``) that
contracts.validate_edl accepts, or raises contracts.PlanError.

THE STORY.  She asked for clips "synced in order of time, progressively
showcasing the outcome", so:

* clips play in the order they were filmed: the phone's capture time
  (MediaStore DATE_TAKEN), else the container's creation_time, else the
  upload order (contracts.resolve_capture_order; the EDL notes say which);
  shots inside a clip play by in-point.  Stage labels from the VLM only
  decide which shots to DROP (weak ones, near-duplicates of the reveal) and
  which shot best shows each stage; they never move a shot in time;
* the reel ENDS on the reveal: the best final-look shot, held at least
  QUALITY_BAR reveal.min_hold_s.  Footage filmed after that shot is not
  used, because nothing may play after the reveal and nothing may play out
  of order;
* when her style asks for it (``hook_first``), a final-look teaser of at
  most QUALITY_BAR teaser.max_s opens the reel; it is the only shot exempt
  from time order;
* when no final look was filmed (or none can be placed), the reel is the
  story in order with a ``no_final_look`` note.

THE CUTS.  Every cut sits on a beat of the grid, rounded to a whole output
frame (1/30 s, well inside the 80 ms beat tolerance), so the renderer can
cut frame-exactly.  The reel lasts the target (clamped to 5-30 s), or up to
a second less when the footage runs short.  Which shots play and how long
is one dynamic programme over (shot in story order) x (beat): each shot
earns its appeal per second on screen, loses for straying from the pace's
shot length, and the best shot of each makeup stage earns a bonus so the
transformation is shown step by step; a shot may be skipped, never
reordered.  Process shots play at 1x; only a reveal shot too short to hold
the minimum is slowed (not below shot_rules.speed_min).

THE FRAME.  The source is scaled to cover the 1080x1920 canvas (never
beyond shot_rules.max_upscale; a clip that would need more is left out), and
the 9:16 window is placed so her face sits inside it with its centre in the
QUALITY_BAR face band (aiming at FACE_TARGET_CY), zooming in only as far as
needed.  A face that cannot be placed so (a low face in a 1080p landscape
clip, which cannot zoom) makes its shot a last resort; if it is used anyway
the segment carries no face box and the EDL notes ``face_unframed``.
"""
from __future__ import annotations

import bisect
import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from integrations.reel_studio.contracts import (
    CANVAS, EDL_SCHEMA, FINAL_LOOK, NOTE_NO_FINAL_LOOK, PROGRESSION, QUALITY_BAR, SAFE_ZONE,
    STAGES, Box, ClipAnalysis, PlanError, capture_note, crop_window, logo_box, make_style,
    resolve_capture_order, validate_edl, validate_moments)
from integrations.reel_studio.media.face import segment_face

logger = logging.getLogger(__name__)

FPS = CANVAS['fps']
#: Shot length each pace aims at, in seconds (the beats decide the exact cut).
SHOT_S = {'beat': 1.5, 'calm': 2.5}
#: The reveal aims at this share of the reel, within these bounds (seconds).
REVEAL_SHARE = 0.15
REVEAL_IDEAL_S = (2.0, 4.0)
#: How long the hook text stays up, and how it looks (canvas pixels).
HOOK_TEXT_S = 2.0
TEXT_SIZE_PX = 72
TEXT_Y = 0.18
TEXT_COLOR = '#FFFFFF'
#: Gap kept between a top logo and the hook text below it (canvas pixels).
TEXT_GAP_PX = 40
#: The logo's longer side, its distance from the safe-zone edges, opacity.
LOGO_MAX_PX = 180
LOGO_MARGIN_PX = 48
LOGO_OPACITY = 0.9
#: A logo file larger than this is not read.
LOGO_MAX_BYTES = 20 * 2 ** 20
#: Where the face centre is aimed, as a fraction of the window height.
FACE_TARGET_CY = 0.38
#: Kept inside the QUALITY_BAR face band, so rounding never lands on its edge.
FACE_BAND_MARGIN = 0.01
#: Zoom is searched from 1.0 upwards in these steps.
ZOOM_STEP = 0.05
#: Final-look shots tried as the reveal before planning without one.
MAX_REVEAL_TRIES = 5
#: Shots the dynamic programme considers at most (the best ones are kept).
MAX_PLAN_SHOTS = 120

# Weights of the shot choice (appeal is 0..10, earned per second on screen).
LENGTH_WEIGHT = 6.0           # cost of a shot twice (or half) the pace's length
REVEAL_LENGTH_WEIGHT = 40.0   # the reveal stays near its ideal hold
STAGE_BONUS = 12.0            # the best shot of a makeup stage
BEFORE_BONUS = 3.0            # ... more for the bare face, the "before"
UNJUDGED_PENALTY = 1.0        # a shot the VLM never saw (appeal from quality)
OTHER_PENALTY = 2.0           # stage 'other': not part of the transformation
REPEAT_REVEAL_PENALTY = 3.0   # a final-look shot before the reveal repeats it
UNFRAMED_PENALTY = 4.0        # her face cannot be placed in the frame
SLOWMO_PENALTY_PER_S = 4.0    # per second of reveal that needs slowing down

#: Notes this planner adds to the EDL (besides capture_order:* and no_final_look).
NOTE_TEASER_SKIPPED = 'teaser_skipped'
NOTE_FACE_UNFRAMED = 'face_unframed'
NOTE_HFR = 'hfr_conformed'
NOTE_CLIP_TOO_SMALL = 'clip_too_small'
NOTE_REVEAL_UNPLACEABLE = 'reveal_unplaceable'
NOTE_LOGO_UNREADABLE = 'logo_unreadable'

_NEG = float('-inf')
_EPS = 1e-6


# ── small helpers ─────────────────────────────────────────────────────────

def _r6(value: float) -> float:
    return round(float(value), 6)


def _frames(seconds: float) -> int:
    return int(round(float(seconds) * FPS))


def _floor_frames(seconds: float) -> int:
    return int(math.floor(float(seconds) * FPS + _EPS))


def _min_shot_frames(bar: Mapping) -> int:
    return int(math.ceil(bar['shot_rules']['min_shot_s'] * FPS - _EPS))


def _as_box(face) -> Optional[Box]:
    if face is None or isinstance(face, Box):
        return face
    return Box(float(face['x']), float(face['y']), float(face['w']), float(face['h']))


def _base_scale(display_w: float, display_h: float, canvas: Mapping = CANVAS) -> float:
    """Source -> canvas magnification at zoom 1 (scale to cover)."""
    return max(canvas['w'] / float(display_w), canvas['h'] / float(display_h))


# ── framing ───────────────────────────────────────────────────────────────

def _place(lo: float, hi: float, prefer: float) -> Optional[float]:
    if lo > hi + _EPS:
        return None
    return min(max(prefer, lo), max(lo, hi))


def frame_face(clip: Mapping, face, *, bar: Mapping = QUALITY_BAR,
               canvas: Mapping = CANVAS) -> Tuple[Dict[str, float], bool]:
    """The crop ``{cx, cy, zoom}`` that frames ``face`` in ``clip``, and whether it could.

    ``clip`` needs ``display_w``/``display_h``; ``face`` is a contracts.Box
    (or its dict) as fractions of the display frame, or None.  The window is
    the canvas aspect scaled to cover the frame, times ``zoom``; the smallest
    zoom (from 1.0, in ZOOM_STEP steps, up to the upscale cap) that keeps the
    whole face inside the window with its centre in the face band wins, the
    centre aimed at FACE_TARGET_CY.  No face: the centre crop at zoom 1.
    When no zoom works, the zoom-1 window nearest to that aim is returned
    with False.
    """
    centre = {'cx': 0.5, 'cy': 0.5, 'zoom': 1.0}
    face = _as_box(face)
    if face is None:
        return centre, True
    dw, dh = float(clip['display_w']), float(clip['display_h'])
    base = _base_scale(dw, dh, canvas)
    zmax = bar['shot_rules']['max_upscale'] / base
    band_lo = bar['face']['cy_min'] + FACE_BAND_MARGIN
    band_hi = bar['face']['cy_max'] - FACE_BAND_MARGIN
    fx0, fy0 = face.x * dw, face.y * dh
    fx1, fy1 = (face.x + face.w) * dw, (face.y + face.h) * dh
    fcx, fcy = (fx0 + fx1) / 2.0, (fy0 + fy1) / 2.0

    zoom = 1.0
    while zoom <= zmax + _EPS:
        ww, wh = canvas['w'] / (base * zoom), canvas['h'] / (base * zoom)
        y0 = _place(max(0.0, fy1 - wh, fcy - band_hi * wh), min(dh - wh, fy0, fcy - band_lo * wh),
                    fcy - FACE_TARGET_CY * wh)
        x0 = _place(max(0.0, fx1 - ww), min(dw - ww, fx0), fcx - ww / 2.0)
        if x0 is not None and y0 is not None:
            return {'cx': _r6((x0 + ww / 2.0) / dw), 'cy': _r6((y0 + wh / 2.0) / dh),
                    'zoom': round(zoom, 4)}, True
        zoom = round(zoom + ZOOM_STEP, 4)

    ww, wh = canvas['w'] / base, canvas['h'] / base
    x0 = min(max(fcx - ww / 2.0, 0.0), max(0.0, dw - ww))
    y0 = min(max(fcy - FACE_TARGET_CY * wh, 0.0), max(0.0, dh - wh))
    return {'cx': _r6((x0 + ww / 2.0) / dw), 'cy': _r6((y0 + wh / 2.0) / dh), 'zoom': 1.0}, False


# ── inputs ────────────────────────────────────────────────────────────────

def _beat_frames(beats) -> List[int]:
    times = list(getattr(beats, 'times_s', None) or ())
    if not times or not float(getattr(beats, 'bpm', 0) or 0) > 0:
        raise ValueError('the beat grid has no beats; use beats.reel_beat_grid, which falls '
                         'back to the synthetic grid')
    return sorted({_frames(t) for t in times if t >= 0})


def _moment_items(moments) -> List[Dict[str, Any]]:
    if moments is None:
        return []
    problems = validate_moments(moments)
    if problems:
        raise ValueError('bad moment list: %s' % '; '.join(problems[:5]))
    return [dict(m) for m in moments['moments']]


@dataclass
class _Clip:
    analysis: ClipAnalysis
    entry: Dict[str, Any]
    base: float


def _order_clips(analyses: Sequence[ClipAnalysis]) -> Tuple[List[_Clip], str]:
    """The clips in capture order, with their EDL entries and the batch source."""
    ids = [a.clip for a in analyses]
    if len(set(ids)) != len(ids):
        raise ValueError('two analyses name the same clip')
    captures = [a.capture for a in analyses]
    if all(c is not None for c in captures):
        srcs = {c.src for c in captures}
        if 'upload_order' in srcs:
            batch = 'upload_order'
        else:
            batch = srcs.pop() if len(srcs) == 1 else 'phone+container'
        by_clip = dict(zip(ids, captures))
    else:
        items = [{'clip': a.clip,
                  'captured_at_ms': (a.capture.captured_at_ms
                                     if a.capture is not None and a.capture.src == 'phone'
                                     else None),
                  'creation_time': a.info.creation_time} for a in analyses]
        resolved, batch = resolve_capture_order(items)
        by_clip = {c.clip: c for c in resolved}
    clips = []
    for a in analyses:
        cap = by_clip[a.clip]
        clips.append(_Clip(analysis=a, entry=a.info.to_edl_clip(cap),
                           base=_base_scale(a.info.display_w, a.info.display_h)))
    clips.sort(key=lambda c: c.entry['order'])
    return clips, batch


@dataclass
class _Shot:
    clip: _Clip
    seg: int
    t0: float
    t1: float
    stage: Optional[str]
    appeal: float
    judged: bool
    moment_t: Optional[float]
    anchor: float
    framed: bool
    index: int = 0

    @property
    def xid(self) -> str:
        return self.clip.analysis.clip


def _window_faces(analysis: ClipAnalysis, t0: float, t1: float) -> Optional[Box]:
    times = list(analysis.times) if analysis.times is not None else []
    faces = list(analysis.faces or [])
    if not times or len(faces) != len(times):
        return None
    lo = bisect.bisect_left(times, t0 - _EPS)
    hi = bisect.bisect_left(times, t1 - _EPS)
    if hi <= lo:
        k = min(max(0, lo - 1), len(faces) - 1)
        return faces[k]
    return segment_face(faces[lo:hi])


def _shots(clips: Sequence[_Clip], items: Sequence[Mapping]) -> List[_Shot]:
    """Every analysed segment as a candidate shot, in story order."""
    by_clip: Dict[str, List[Mapping]] = {}
    for item in items:
        by_clip.setdefault(item['clip'], []).append(item)
    known = {c.analysis.clip for c in clips}
    unknown = sorted({m['clip'] for m in items} - known)
    if unknown:
        logger.info('ignoring moments for %d clip(s) not in this reel', len(unknown))

    shots: List[_Shot] = []
    for c in clips:
        a = c.analysis
        segments = list(a.segments)
        starts = [s.t0 for s in segments]
        judged: Dict[int, Mapping] = {}
        for item in by_clip.get(a.clip, ()):
            k = bisect.bisect_right(starts, item['t_s'] + _EPS) - 1
            if not (0 <= k < len(segments)) or item['t_s'] > segments[k].t1 + _EPS:
                continue
            held = judged.get(k)
            if held is None or (-item['appeal'], item['t_s']) < (-held['appeal'], held['t_s']):
                judged[k] = item
        best_cand: Dict[int, Any] = {}
        for cand in a.candidates:
            score = cand.quality.score if cand.quality is not None else 0.0
            held = best_cand.get(cand.segment)
            held_score = held.quality.score if held is not None and held.quality else -1.0
            if held is None or (-score, cand.t_s) < (-held_score, held.t_s):
                best_cand[cand.segment] = cand
        for k, seg in enumerate(segments):
            m = judged.get(k)
            cand = best_cand.get(k)
            if m is not None:
                stage, appeal, anchor, moment_t = m['stage'], float(m['appeal']), m['t_s'], m['t_s']
            else:
                score = cand.quality.score if cand is not None and cand.quality else 0.0
                stage, appeal, moment_t = None, 10.0 * float(score), None
                anchor = cand.t_s if cand is not None else (seg.t0 + seg.t1) / 2.0
            _crop, framed = frame_face(c.entry, _window_faces(a, seg.t0, seg.t1))
            shots.append(_Shot(clip=c, seg=k, t0=float(seg.t0), t1=float(seg.t1), stage=stage,
                               appeal=min(10.0, max(0.0, appeal)), judged=m is not None,
                               moment_t=moment_t, anchor=float(anchor), framed=framed))
    # An unjudged shot shows what the last judged shot before it showed; one
    # before any judged shot is 'other' (a later label, such as the final
    # look, says nothing about footage filmed earlier).
    previous = 'other'
    for i, shot in enumerate(shots):
        shot.index = i
        if shot.judged:
            previous = shot.stage
        else:
            shot.stage = previous
    return shots


# ── the dynamic programme ─────────────────────────────────────────────────

@dataclass
class _Option:
    shot: _Shot
    cap: int          # most frames it can fill at 1x
    rate: float       # appeal earned per second on screen
    bonus: float


@dataclass
class _RevealSpec:
    min_f: int
    cap_f: int
    range_f: int
    ideal_f: int
    rate: float


def _length_cost(length: int, ideal: int, weight: float = LENGTH_WEIGHT) -> float:
    return weight * ((length - ideal) / float(ideal)) ** 2


def _fill(options: Sequence[_Option], points: Sequence[int], start: int, end: int,
          ideal: int, end_ideal: int, min_f: int,
          reveal: Optional[_RevealSpec]) -> Optional[Tuple[List[Tuple[_Shot, int, int]], int]]:
    """Choose shots (in order, each at most once) and cuts (beat frames) filling start..end.

    With ``reveal`` the process shots end at a beat and the reveal runs from
    there to ``end``; without, the last process shot ends at ``end``.
    Returns ([(shot, from_frame, to_frame)], reveal_start_frame) or None.
    """
    pts = [start] + [p for p in points if start < p < end] + [end]
    last = len(pts) - 1
    best = [_NEG] * len(pts)
    best[0] = 0.0
    backs: List[Dict[int, int]] = []
    for opt in options:
        row = list(best)
        choice: Dict[int, int] = {}
        for j in range(1, len(pts)):
            if j == last and reveal is not None:
                continue
            target = end_ideal if j == last else ideal
            for jp in range(j - 1, -1, -1):
                length = pts[j] - pts[jp]
                if length > opt.cap:
                    break
                if length < min_f or best[jp] == _NEG:
                    continue
                value = (best[jp] + length / float(FPS) * opt.rate
                         - _length_cost(length, target) + opt.bonus)
                if value > row[j] + 1e-9:
                    row[j] = value
                    choice[j] = jp
        backs.append(choice)
        best = row

    if reveal is not None:
        final, j = _NEG, None
        for k in range(0, last):
            length = end - pts[k]
            if best[k] == _NEG or not reveal.min_f <= length <= reveal.cap_f:
                continue
            value = (best[k] + length / float(FPS) * reveal.rate
                     - _length_cost(length, reveal.ideal_f, REVEAL_LENGTH_WEIGHT)
                     - SLOWMO_PENALTY_PER_S * max(0, length - reveal.range_f) / float(FPS))
            if value > final + 1e-9:
                final, j = value, k
        if j is None:
            return None
    else:
        if best[last] == _NEG:
            return None
        j = last
    reveal_start = pts[j]
    cuts: List[Tuple[_Shot, int, int]] = []
    for i in range(len(options) - 1, -1, -1):
        jp = backs[i].get(j)
        if jp is not None:
            cuts.append((options[i].shot, pts[jp], pts[j]))
            j = jp
    cuts.reverse()
    return cuts, reveal_start


def _options(candidates: Sequence[_Shot], has_reveal: bool) -> List[_Option]:
    def rate(s: _Shot) -> float:
        value = s.appeal
        if not s.judged:
            value -= UNJUDGED_PENALTY
        if s.stage == 'other':
            value -= OTHER_PENALTY
        if has_reveal and s.stage == FINAL_LOOK:
            value -= REPEAT_REVEAL_PENALTY
        if not s.framed:
            value -= UNFRAMED_PENALTY
        return value

    rates = {s.index: rate(s) for s in candidates}
    bonus: Dict[int, float] = {}
    for stage in PROGRESSION:
        if has_reveal and stage == FINAL_LOOK:
            continue
        judged = [s for s in candidates if s.judged and s.stage == stage]
        if judged:
            top = min(judged, key=lambda s: (-rates[s.index], s.index))
            bonus[top.index] = STAGE_BONUS + (BEFORE_BONUS if stage == STAGES[0] else 0.0)
    keep = list(candidates)
    if len(keep) > MAX_PLAN_SHOTS:
        ranked = sorted(keep, key=lambda s: (-(s.index in bonus), -rates[s.index], s.index))
        keep = sorted(ranked[:MAX_PLAN_SHOTS], key=lambda s: s.index)
    return [_Option(shot=s, cap=_floor_frames(s.t1 - s.t0), rate=rates[s.index],
                    bonus=bonus.get(s.index, 0.0)) for s in keep]


# ── one attempt ───────────────────────────────────────────────────────────

@dataclass
class _Plan:
    cuts: List[Tuple[_Shot, int, int]]
    end_f: int
    reveal: Optional[_Shot] = None
    reveal_range: Tuple[float, float] = (0.0, 0.0)
    reveal_start_f: int = 0
    teaser_f: int = 0


def _reveal_range(reveal: _Shot, shots: Sequence[_Shot], want_s: float) -> Tuple[float, float, int]:
    """Source seconds the reveal may use, and the first story index it covers.

    Its own shot, joined with neighbouring final-look shots of the same clip
    (later ones first) while it is shorter than ``want_s``.
    """
    lo = hi = reveal.index
    r0, r1 = reveal.t0, reveal.t1

    def joins(k: int) -> bool:
        s = shots[k]
        return s.clip is reveal.clip and s.stage == FINAL_LOOK

    while r1 - r0 < want_s - _EPS:
        if hi + 1 < len(shots) and joins(hi + 1) and abs(shots[hi + 1].t0 - r1) < 1e-3:
            hi += 1
            r1 = shots[hi].t1
        elif lo - 1 >= 0 and joins(lo - 1) and abs(shots[lo - 1].t1 - r0) < 1e-3:
            lo -= 1
            r0 = shots[lo].t0
        else:
            break
    return r0, r1, lo


def _teaser_frames(beat_frames: Sequence[int], bar: Mapping) -> Optional[int]:
    lo, hi = _min_shot_frames(bar), _floor_frames(bar['teaser']['max_s'])
    fits = [f for f in beat_frames if lo <= f <= hi]
    return max(fits) if fits else None


def _attempt(shots: Sequence[_Shot], reveal: Optional[_Shot], beat_frames: Sequence[int],
             style: Mapping, bar: Mapping, notes: List[str]) -> Optional[_Plan]:
    target = float(style['target_s'])
    min_f = _min_shot_frames(bar)
    ideal = _frames(SHOT_S.get(style['pace'], SHOT_S['beat']))
    reveal_ideal_s = min(REVEAL_IDEAL_S[1], max(REVEAL_IDEAL_S[0], REVEAL_SHARE * target))
    reveal_ideal = _frames(reveal_ideal_s)
    speed_min = bar['shot_rules']['speed_min']

    spec = None
    teaser = 0
    r0 = r1 = 0.0
    if reveal is not None:
        r0, r1, first = _reveal_range(reveal, shots, reveal_ideal_s)
        candidates = [s for s in shots if s.index < first]
        range_f = _floor_frames(r1 - r0)
        spec = _RevealSpec(min_f=int(math.ceil(bar['reveal']['min_hold_s'] * FPS - _EPS)),
                           cap_f=_floor_frames((r1 - r0) / speed_min), range_f=range_f,
                           ideal_f=reveal_ideal, rate=reveal.appeal)
        if style['hook_first']:
            teaser = _teaser_frames(beat_frames, bar) or 0
            if teaser and teaser > _floor_frames((r1 - r0) / speed_min):
                teaser = 0
            if not teaser:
                notes.append(NOTE_TEASER_SKIPPED)
    else:
        candidates = list(shots)
        if style['hook_first']:
            notes.append(NOTE_TEASER_SKIPPED)

    options = _options(candidates, reveal is not None)
    target_f = _frames(target)
    tol_f = _floor_frames(bar['duration']['tol_s'])
    for end in sorted({target_f, target_f - FPS // 2, target_f - tol_f + 1}, reverse=True):
        if end <= teaser:
            continue
        filled = _fill(options, beat_frames, teaser, end, ideal, reveal_ideal, min_f, spec)
        if filled is not None:
            cuts, reveal_start = filled
            return _Plan(cuts=cuts, end_f=end, reveal=reveal, reveal_range=(r0, r1),
                         reveal_start_f=reveal_start, teaser_f=teaser)
    return None


# ── the EDL ───────────────────────────────────────────────────────────────

def _window(anchor: float, length: float, lo: float, hi: float, lead: float) -> float:
    """Start of a ``length`` window inside [lo, hi] with ``anchor`` ``lead`` of the way in."""
    return min(max(anchor - lead * length, lo), hi - length)


def _segment(shot: _Shot, f0: int, f1: int, role: str, *, lo: float, hi: float,
             anchor: float, lead: float, notes: List[str], speed_min: float,
             stage: Optional[str] = None) -> Dict:
    out_s = (f1 - f0) / float(FPS)
    room = hi - lo
    speed = 1.0
    if out_s > room + _EPS:
        # Slowed just enough to fill the slot; the plan never asks for less
        # than speed_min (a float hair under it is rounded back up).
        speed = max(speed_min, math.floor(room / out_s * 1e4) / 1e4)
    src = out_s * speed
    a = _window(anchor, src, lo, hi, lead)
    in_s, out_src = _r6(a), _r6(a + src)
    face = _window_faces(shot.clip.analysis, in_s, out_src)
    crop, framed = frame_face(shot.clip.entry, face)
    if face is not None and not framed:
        notes.append(NOTE_FACE_UNFRAMED)
        face = None
    return {'clip': shot.xid, 'in_s': in_s, 'out_s': out_src, 'speed': speed,
            'out_start_s': _r6(f0 / float(FPS)), 'out_end_s': _r6(f1 / float(FPS)),
            'crop': crop, 'face': None if face is None else {k: _r6(v) for k, v in
                                                              face.to_dict().items()},
            'transition': {'type': 'cut', 'dur_s': 0.0},
            'stage': stage or shot.stage, 'appeal': round(shot.appeal, 3), 'role': role}


def _image_size(path: str) -> Optional[Tuple[int, int]]:
    """(width, height) of an image file, reading only its header; None if unreadable."""
    try:
        if os.path.getsize(path) > LOGO_MAX_BYTES:
            return None
        from PIL import Image
        with Image.open(path) as img:
            w, h = img.size
    except (OSError, ValueError) as e:
        logger.info('logo %s is not readable: %s', path, e)
        return None
    return (int(w), int(h)) if w > 0 and h > 0 else None


def _overlays(style: Mapping, duration_s: float, logo_path: Optional[str],
              notes: List[str]) -> List[Dict]:
    logo = None
    if logo_path and style['use_logo']:
        size = _image_size(logo_path)
        if size is None:
            notes.append(NOTE_LOGO_UNREADABLE)
        else:
            w, h = size
            if w >= h:
                lw, lh = LOGO_MAX_PX, max(1, int(round(LOGO_MAX_PX * h / float(w))))
            else:
                lw, lh = max(1, int(round(LOGO_MAX_PX * w / float(h)))), LOGO_MAX_PX
            logo = {'type': 'logo', 'src': os.path.abspath(logo_path),
                    'corner': style['logo_corner'], 'width_px': lw, 'height_px': lh,
                    'margin_px': LOGO_MARGIN_PX, 'opacity': LOGO_OPACITY,
                    'from_s': 0.0, 'to_s': None}
    out = []
    hook = (style.get('hook_text') or '').strip()
    if hook:
        y_px = TEXT_Y * CANVAS['h']
        if logo is not None and logo['corner'].startswith('top'):
            y_px = max(y_px, logo_box(logo)[3] + TEXT_GAP_PX + TEXT_SIZE_PX / 2.0)
        out.append({'type': 'text', 'text': hook, 'font': 'auto', 'size_px': TEXT_SIZE_PX,
                    'color': TEXT_COLOR, 'box': True,
                    'x': _r6((SAFE_ZONE['x_min'] + SAFE_ZONE['x_max']) / 2.0 / CANVAS['w']),
                    'y': _r6(y_px / CANVAS['h']), 'anchor': 'center',
                    'from_s': 0.0, 'to_s': _r6(min(HOOK_TEXT_S, duration_s))})
    if logo is not None:
        out.append(logo)
    return out


def _dedupe(notes: Sequence[str]) -> List[str]:
    seen, out = set(), []
    for note in notes:
        if note not in seen:
            seen.add(note)
            out.append(note)
    return out


def _need_s(shots: Sequence[_Shot], target: float, bar: Mapping) -> float:
    have = sum(s.t1 - s.t0 for s in shots)
    return max(0.1, math.ceil(((target - bar['duration']['tol_s']) - have) * 10.0) / 10.0)


def plan_edit(moments: Optional[Mapping], analyses: Sequence[ClipAnalysis], beats,
              style: Optional[Mapping], *, logo_path: Optional[str] = None,
              bar: Mapping = QUALITY_BAR) -> Dict[str, Any]:
    """The EDL for one reel (see the module note).

    ``moments`` is a ``hart.reel.moments.v1`` document (or None: no stages
    known, appeal from frame quality); ``analyses`` are the clips' analysis
    results in upload order, each with ``capture`` filled in or left None
    (then the container creation_time, else upload order, decides);
    ``beats`` is a contracts.BeatGrid covering the reel; ``style`` is passed
    through contracts.make_style (defaults filled, target clamped to 5-30 s);
    ``logo_path`` is her logo image, used when ``style['use_logo']``.

    Raises ValueError for a bad style, moment list or an empty beat grid, and
    PlanError: 'not_enough_footage' (data need_s), 'clip_unsupported' (data
    xid, reason 'too_small') when no clip can fill the canvas, 'no_plan' when
    the cuts cannot be placed on the grid, 'plan_invalid' if the result would
    fail contracts.validate_edl (a planner bug, never shipped).
    """
    style = make_style(dict(style or {}))
    beat_frames = _beat_frames(beats)
    items = _moment_items(moments)
    target = float(style['target_s'])
    if not analyses:
        raise PlanError('not_enough_footage', 'no clips', need_s=target - bar['duration']['tol_s'])

    clips, batch = _order_clips(analyses)
    notes = [capture_note(batch)]
    cap = bar['shot_rules']['max_upscale']
    usable = [c for c in clips if c.base <= cap + 1e-9]
    small = [c for c in clips if c.base > cap + 1e-9]
    if small:
        notes.append(NOTE_CLIP_TOO_SMALL)
        logger.info('leaving out %d clip(s) that would need more than %.2fx upscale',
                    len(small), cap)
    if not usable:
        raise PlanError('clip_unsupported', 'every clip is too small to fill the canvas',
                        xid=small[0].analysis.clip, reason='too_small')
    if any(c.analysis.info.hfr for c in usable):
        notes.append(NOTE_HFR)

    shots = _shots(usable, items)
    finals = sorted((s for s in shots if s.judged and s.stage == FINAL_LOOK),
                    key=lambda s: (not s.framed, -s.appeal, -s.index))
    plan = None
    for reveal in finals[:MAX_REVEAL_TRIES]:
        attempt_notes: List[str] = []
        plan = _attempt(shots, reveal, beat_frames, style, bar, attempt_notes)
        if plan is not None:
            notes += attempt_notes
            break
    if plan is None:
        if finals:
            notes.append(NOTE_REVEAL_UNPLACEABLE)
        plan = _attempt(shots, None, beat_frames, style, bar, notes)
    if plan is None:
        need = _need_s(shots, target, bar)
        have = sum(s.t1 - s.t0 for s in shots)
        if have < target - bar['duration']['tol_s']:
            raise PlanError('not_enough_footage', 'about %.1f s more footage is needed' % need,
                            need_s=need)
        raise PlanError('no_plan', 'the cuts could not be placed on the beat grid')
    return _edl(plan, usable, batch, beats, style, logo_path, notes, bar)


def _edl(plan: _Plan, clips: Sequence[_Clip], batch: str, beats, style: Mapping,
         logo_path: Optional[str], notes: List[str], bar: Mapping) -> Dict[str, Any]:
    segments = []
    reveal = plan.reveal
    r0, r1 = plan.reveal_range
    speed_min = bar['shot_rules']['speed_min']
    if reveal is not None and plan.teaser_f:
        segments.append(_segment(reveal, 0, plan.teaser_f, 'teaser', lo=r0, hi=r1,
                                 anchor=reveal.moment_t, lead=0.5, notes=notes,
                                 speed_min=speed_min, stage=FINAL_LOOK))
    for shot, f0, f1 in plan.cuts:
        segments.append(_segment(shot, f0, f1, 'process', lo=shot.t0, hi=shot.t1,
                                 anchor=shot.anchor, lead=0.5, notes=notes,
                                 speed_min=speed_min))
    if reveal is not None:
        # The reveal opens a little before her best final-look frame and holds on it.
        segments.append(_segment(reveal, plan.reveal_start_f, plan.end_f, 'reveal', lo=r0,
                                 hi=r1, anchor=reveal.moment_t, lead=0.25, notes=notes,
                                 speed_min=speed_min, stage=FINAL_LOOK))
    else:
        notes.append(NOTE_NO_FINAL_LOOK)

    used = {s['clip'] for s in segments}
    duration = _r6(plan.end_f / float(FPS))
    edl = {
        'schema': EDL_SCHEMA,
        'canvas': dict(CANVAS),
        'target_s': style['target_s'],
        'duration_s': duration,
        'audio': None,
        'final_look_found': reveal is not None,
        'hook_first': bool(reveal is not None and plan.teaser_f),
        'capture_src': batch,
        'reveal': ({'clip': reveal.xid, 't_s': _r6(reveal.moment_t)} if reveal is not None
                   else None),
        'notes': [],
        'beats': beats.to_edl(),
        'clips': {c.analysis.clip: c.entry for c in clips if c.analysis.clip in used},
        'segments': segments,
        'overlays': _overlays(style, duration, logo_path, notes),
    }
    edl['notes'] = _dedupe(notes)
    problems = validate_edl(edl, bar=bar)
    if problems:
        raise PlanError('plan_invalid', '; '.join(problems[:5]))
    return edl


__all__ = ['FPS', 'SHOT_S', 'REVEAL_SHARE', 'REVEAL_IDEAL_S', 'HOOK_TEXT_S', 'TEXT_SIZE_PX',
           'TEXT_Y', 'TEXT_COLOR', 'TEXT_GAP_PX', 'LOGO_MAX_PX', 'LOGO_MARGIN_PX',
           'LOGO_OPACITY', 'LOGO_MAX_BYTES', 'FACE_TARGET_CY', 'FACE_BAND_MARGIN', 'ZOOM_STEP',
           'MAX_REVEAL_TRIES', 'MAX_PLAN_SHOTS', 'NOTE_TEASER_SKIPPED', 'NOTE_FACE_UNFRAMED',
           'NOTE_HFR', 'NOTE_CLIP_TOO_SMALL', 'NOTE_REVEAL_UNPLACEABLE', 'NOTE_LOGO_UNREADABLE',
           'frame_face', 'plan_edit']
