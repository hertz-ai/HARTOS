"""integrations.reel_studio.motion.layout -- where and when each animated overlay sits.

Pure: no files, no processes.  ``plan_motion(edl, wanted, ...)`` reads a
planned EDL (media/edl.plan_edit's) and the templates she asked for
(contracts.MOTION_TEMPLATES) and returns a MotionPlan: one MotionItem per
template that fits, each a box in canvas pixels and a window in reel
seconds, and a note for each one left out and why.  compose.py renders the
items (or lays their plain-ffmpeg stand-ins) and contracts.validate_edl
judges the result; this module places them so that it passes:

* every box sits inside contracts.SAFE_ZONE, except the wipe, which fills
  the frame;
* the top of the frame holds her logo (the still one, or the sting that
  stands in for it while it plays), then the title below it;
* the end card plays over the reel's last END_CARD_S seconds and never
  covers her face in the final look's last reveal.min_hold_s (the
  contracts' rule: segment_face_box); when no band of the safe zone above
  or below her face is tall enough, the card ends before that hold;
* the wipe (her "before" frame wiped away to the reveal) opens the reveal
  and leaves the final look uncovered for reveal.min_hold_s;
* sizes are even numbers of pixels (the clip is rendered at the box size).

Every placement is a function of the EDL and the request only, so the same
reel asks for the same clips.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from integrations.reel_studio.contracts import (
    CANVAS, MOTION_TEMPLATES, QUALITY_BAR, SAFE_ZONE, logo_box, segment_face_box)

#: Composition ids in project/templates.js, per template.
TEMPLATE_IDS = {'title': 'Title', 'logo_sting': 'LogoSting', 'end_card': 'EndCard',
                'wipe': 'Wipe'}
#: How long each plays (seconds); the wipe shortens to fit the reveal down
#: to MIN_WIPE_S, the end card to MIN_CARD_S when it has to end early.
TITLE_S = 2.5
LOGO_STING_S = 1.5
END_CARD_S = 3.0
WIPE_S = 1.2
MIN_WIPE_S = 0.6
MIN_CARD_S = 1.0
#: The accent the templates draw rings, rules and the price in.
ACCENT = '#E8A0B4'
#: Inset of the title and the end card from the safe zone's sides, and the
#: gap kept between a box and what it must not touch (canvas pixels).
SIDE_INSET_PX = 40
GAP_PX = 24
TITLE_H = 300
TITLE_MIN_H = 180
CARD_H = 360
CARD_MIN_H = 220
STING_PX = 300
#: The end card's lines: at most this many, each at most this long.
CARD_LINES = 3
CARD_LINE_CHARS = 40

#: Why a template she asked for was left out (MotionPlan.notes:
#: 'motion_skipped:<template>:<why>').
SKIP_NOTE = 'motion_skipped:%s:%s'
NO_LOGO = 'no_logo'
NO_TEXT = 'no_text'
NO_CARD_TEXT = 'no_card_text'
NO_FINAL_LOOK = 'no_final_look'
REVEAL_TOO_SHORT = 'reveal_too_short'
NO_BEFORE_SHOT = 'no_before_shot'
NO_ROOM = 'no_room'


@dataclass(frozen=True)
class MotionItem:
    """One overlay to render: ``template`` (MOTION_TEMPLATES) at ``box``
    (x0, y0, x1, y1 canvas px, even sizes) over ``from_s``..``to_s``, with
    the template's ``props`` (text, lines, accent).  ``image`` names the
    picture compose adds as a prop: 'logo' (the still logo's file) or
    'before' (a frame of segment MotionPlan.before_segment)."""
    template: str
    box: Tuple[float, float, float, float]
    from_s: float
    to_s: float
    props: Mapping[str, Any]
    image: Optional[str] = None


@dataclass
class MotionPlan:
    items: List[MotionItem] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    #: The segment whose middle frame is the wipe's "before" picture.
    before_segment: Optional[int] = None


def _r3(v: float) -> float:
    return round(float(v), 3)


def _even(v: float) -> int:
    n = int(round(float(v)))
    return n - (n % 2)


def _box(x0: float, y0: float, w: float, h: float) -> Tuple[float, float, float, float]:
    """A box with even sides from its top-left corner."""
    w, h = _even(w), _even(h)
    return (float(int(x0)), float(int(y0)), float(int(x0) + w), float(int(y0) + h))


def _free_bands(lo: float, hi: float, taken: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """The vertical stretches of lo..hi no ``taken`` (y0, y1) range touches,
    each kept GAP_PX away from them, top to bottom."""
    bands, top = [], lo
    for y0, y1 in sorted(taken):
        if y0 - GAP_PX > top:
            bands.append((top, min(hi, y0 - GAP_PX)))
        top = max(top, y1 + GAP_PX)
    if hi > top:
        bands.append((top, hi))
    return [(a, b) for a, b in bands if b > a]


def card_lines(lines: Sequence[Any]) -> List[str]:
    """The end card's lines from her profile's words: at most CARD_LINES
    non-empty lines; one longer than CARD_LINE_CHARS keeps its first clause
    (to a comma or semicolon), then whole words up to the limit."""
    out = []
    for raw in lines or ():
        text = ' '.join(str(raw).split()) if isinstance(raw, str) else ''
        if not text:
            continue
        if len(text) > CARD_LINE_CHARS:
            text = re.split(r'[,;]', text)[0].strip() or text
        if len(text) > CARD_LINE_CHARS:
            cut = text[:CARD_LINE_CHARS + 1].rsplit(' ', 1)[0]
            text = cut if cut else text[:CARD_LINE_CHARS]
        out.append(text)
        if len(out) == CARD_LINES:
            break
    return out


def _logo(edl: Mapping) -> Optional[Mapping]:
    return next((ov for ov in edl['overlays'] if ov.get('type') == 'logo'), None)


def _sting(edl: Mapping, logo: Mapping) -> MotionItem:
    zone = SAFE_ZONE
    x0, y0, x1, y1 = logo_box(logo)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    left = min(max(cx - STING_PX / 2.0, zone['x_min']), zone['x_max'] - STING_PX)
    top = min(max(cy - STING_PX / 2.0, zone['y_min']), zone['y_max'] - STING_PX)
    end = min(LOGO_STING_S, float(edl['duration_s']))
    return MotionItem('logo_sting', _box(left, top, STING_PX, STING_PX), 0.0, _r3(end),
                      {'accent': ACCENT}, image='logo')


def _top_taken(edl: Mapping, sting: Optional[MotionItem]) -> List[Tuple[float, float]]:
    """Vertical ranges at the top the title must keep clear of: the sting
    while it plays, else the still logo when it sits at the top."""
    if sting is not None:
        return [(sting.box[1], sting.box[3])]
    logo = _logo(edl)
    if logo is not None and str(logo.get('corner', '')).startswith('top'):
        _x0, y0, _x1, y1 = logo_box(logo)
        return [(y0, y1)]
    return []


def _title(edl: Mapping, hook_text: str, sting: Optional[MotionItem]) -> Optional[MotionItem]:
    zone = SAFE_ZONE
    bands = [b for b in _free_bands(zone['y_min'] + GAP_PX, zone['y_max'] - GAP_PX,
                                    _top_taken(edl, sting)) if b[1] - b[0] >= TITLE_MIN_H]
    if not bands:
        return None
    top, bottom = bands[0]
    width = zone['x_max'] - zone['x_min'] - 2 * SIDE_INSET_PX
    return MotionItem('title', _box(zone['x_min'] + SIDE_INSET_PX, top, width,
                                    min(TITLE_H, bottom - top)),
                      0.0, _r3(min(TITLE_S, float(edl['duration_s']))),
                      {'text': hook_text, 'accent': ACCENT})


def _card_box(taken: Sequence[Tuple[float, float]]) -> Optional[Tuple[float, float, float, float]]:
    """The lowest band of the safe zone clear of ``taken`` that holds a card."""
    zone = SAFE_ZONE
    bands = [b for b in _free_bands(zone['y_min'] + GAP_PX, zone['y_max'] - GAP_PX, taken)
             if b[1] - b[0] >= CARD_MIN_H]
    if not bands:
        return None
    top, bottom = bands[-1]
    h = min(CARD_H, bottom - top)
    width = zone['x_max'] - zone['x_min'] - 2 * SIDE_INSET_PX
    return _box(zone['x_min'] + SIDE_INSET_PX, bottom - _even(h), width, h)


def _end_card(edl: Mapping, lines: List[str], bar: Mapping) -> Tuple[Optional[MotionItem], str]:
    dur = float(edl['duration_s'])
    hold = float(bar['reveal']['min_hold_s'])
    taken = []
    logo = _logo(edl)
    if logo is not None and logo.get('to_s') is None:
        _x0, y0, _x1, y1 = logo_box(logo)
        taken.append((y0, y1))
    face = None
    if edl['final_look_found']:
        face = segment_face_box(edl, edl['segments'][-1])
    props = {'lines': lines, 'accent': ACCENT}
    start = max(0.0, dur - END_CARD_S)
    box = _card_box(taken + ([(face[1], face[3])] if face is not None else []))
    if box is not None:
        return MotionItem('end_card', box, _r3(start), _r3(dur), props), ''
    # No band clear of her face: the card ends before the final look's hold.
    end = dur - hold
    start = max(0.0, end - END_CARD_S)
    box = _card_box(taken)
    if box is None or end - start < MIN_CARD_S:
        return None, NO_ROOM
    return MotionItem('end_card', box, _r3(start), _r3(end), props), ''


def _wipe(edl: Mapping, bar: Mapping) -> Tuple[Optional[MotionItem], Optional[int], str]:
    if not edl['final_look_found']:
        return None, None, NO_FINAL_LOOK
    segs = edl['segments']
    reveal = segs[-1]
    room = (reveal['out_end_s'] - reveal['out_start_s']) - float(bar['reveal']['min_hold_s'])
    length = min(WIPE_S, room)
    if length < MIN_WIPE_S:
        return None, None, REVEAL_TOO_SHORT
    process = [i for i, s in enumerate(segs[:-1]) if s['role'] == 'process']
    bare = [i for i in process if segs[i]['stage'] == 'bare_face']
    if not process:
        return None, None, NO_BEFORE_SHOT
    start = float(reveal['out_start_s'])
    item = MotionItem('wipe', (0.0, 0.0, float(CANVAS['w']), float(CANVAS['h'])),
                      _r3(start), _r3(start + length), {'accent': ACCENT}, image='before')
    return item, (bare or process)[0], ''


def plan_motion(edl: Mapping, wanted: Sequence[str], *, hook_text: str = '',
                card_text: Sequence[Any] = (), bar: Mapping = QUALITY_BAR) -> MotionPlan:
    """Where and when each template in ``wanted`` plays on ``edl`` (see the
    module note).  ``hook_text`` is the reel's on-screen hook (the title's
    words); ``card_text`` her end card's words (card_lines cleans them).
    Unknown templates are ignored; one asked for twice is planned once."""
    plan = MotionPlan()
    asked = [t for t in dict.fromkeys(wanted or ()) if t in MOTION_TEMPLATES]
    sting = None
    if 'logo_sting' in asked:
        logo = _logo(edl)
        if logo is None:
            plan.notes.append(SKIP_NOTE % ('logo_sting', NO_LOGO))
        else:
            sting = _sting(edl, logo)
            plan.items.append(sting)
    if 'title' in asked:
        text = ' '.join(str(hook_text or '').split())
        title = _title(edl, text, sting) if text else None
        if title is None:
            plan.notes.append(SKIP_NOTE % ('title', NO_TEXT if not text else NO_ROOM))
        else:
            plan.items.append(title)
    if 'end_card' in asked:
        lines = card_lines(card_text)
        card, why = _end_card(edl, lines, bar) if lines else (None, NO_CARD_TEXT)
        if card is None:
            plan.notes.append(SKIP_NOTE % ('end_card', why))
        else:
            plan.items.append(card)
    if 'wipe' in asked:
        wipe, before, why = _wipe(edl, bar)
        if wipe is None:
            plan.notes.append(SKIP_NOTE % ('wipe', why))
        else:
            plan.items.append(wipe)
            plan.before_segment = before
    return plan


__all__ = ['TEMPLATE_IDS', 'TITLE_S', 'LOGO_STING_S', 'END_CARD_S', 'WIPE_S', 'MIN_WIPE_S',
           'MIN_CARD_S', 'ACCENT', 'CARD_LINES', 'CARD_LINE_CHARS', 'SKIP_NOTE', 'NO_LOGO',
           'NO_TEXT', 'NO_CARD_TEXT', 'NO_FINAL_LOOK', 'REVEAL_TOO_SHORT', 'NO_BEFORE_SHOT',
           'NO_ROOM', 'MotionItem', 'MotionPlan', 'card_lines', 'plan_motion']
