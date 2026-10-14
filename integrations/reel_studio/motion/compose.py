"""integrations.reel_studio.motion.compose -- the reel pipeline's animated-overlay step.

``add_motion(edl, request, workdir=, progress=)`` runs after the planner
(media/edl.plan_edit) and before the render.  ``request`` is what her agent
asked for: ``{'overlays': [templates], 'hook_text': str, 'card_text':
[str]}`` (tools.reel_make reads her voice profile's motion_overlays and her
words).  It returns the EDL to render and the notes that say what happened:

1. layout.plan_motion places each template that fits (notes say which were
   left out and why);
2. runtime.ensure_ready: Remotion ready, else why not -- switched off (the
   owner has not confirmed the license), the owner asked / said no / nobody
   to ask on the consent card, still being set up (the wait is bounded), or
   failed;
3. ready: each item is rendered with alpha (her logo and the wipe's "before"
   frame go in as data: URIs, the frame cut from the reel's own footage with
   render.geometry_filter, the crop the reel shows) and laid in the EDL as a
   'motion' overlay; the animated title stands in for the still hook text,
   and the still logo appears once the sting ends;
4. not ready, or the render failed: the plain-ffmpeg versions -- the title is
   the hook text already there, the logo stays still, the end card becomes
   a boxed text overlay at the card's place and time, the wipe is the cut;
5. the result must pass contracts.validate_edl; if it does not (a layout
   bug), the plain versions are tried, then the planner's EDL is kept.

NOTE_MOTION records the outcome ('motion:rendered', 'motion:off', ...);
``message(notes)`` turns the notes into what her agent tells her.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import os
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from integrations.reel_studio.contracts import CANVAS, SAFE_ZONE, validate_edl
from integrations.reel_studio.motion import layout, runtime

logger = logging.getLogger(__name__)

#: The outcome of the step for one reel: 'motion:<state>', the state one of
#: runtime's (OFF, ASKED, DECLINED, UNAVAILABLE, SETTING_UP, FAILED: the
#: plain versions were laid) or RENDERED, or REFUSED (not even the plain
#: versions validated: the planner's EDL is kept).
NOTE_MOTION = 'motion:%s'
RENDERED = 'rendered'
REFUSED = 'refused'
#: How long a reel waits for a Remotion setup it starts (seconds); the setup
#: goes on after that and the next reel uses it.
SETUP_WAIT_S = 60.0
#: The plain end card: one boxed line of text (the renderer wraps it).
CARD_TEXT_PX = 56
CARD_TEXT_COLOR = '#FFFFFF'
CARD_JOIN = ' · '
#: Largest image put into a template as a data: URI.
IMAGE_MAX_BYTES = 8 << 20

#: What her agent says, per outcome, when it is not a rendered reel.
_SAY = {
    runtime.OFF: ('Animated overlays are switched off on this computer until its owner confirms '
                  'the Remotion license, so this reel has the plain versions (the end card as a '
                  'text box, the title as the hook text); say so.'),
    runtime.ASKED: ('Animated overlays need a one-time setup on this computer, and its owner has '
                    'been asked on the consent card; this reel has the plain versions. Say so.'),
    runtime.DECLINED: ('The owner of this computer said no to setting animated overlays up, so '
                       'this reel has the plain versions; say so.'),
    runtime.UNAVAILABLE: ('Animated overlays cannot be set up on this computer (there is nobody '
                          'to ask), so this reel has the plain versions; say so.'),
    runtime.SETTING_UP: ('Animated overlays are still being set up on this computer, so this '
                         'reel has the plain versions and the next one gets them; say so.'),
    runtime.FAILED: ('The animated overlays could not be made this time, so this reel has the '
                     'plain versions; say so.'),
    REFUSED: ('The overlays she chose did not fit this reel, so it has none; say so.'),
}
#: Why a template was left out (layout's reasons), in her agent's words.
_WHY = {
    layout.NO_LOGO: 'no logo is saved for her reels',
    layout.NO_TEXT: 'the reel has no hook text to animate',
    layout.NO_CARD_TEXT: 'her profile has no services, price or booking ask to put on it',
    layout.NO_FINAL_LOOK: 'no finished look was filmed to wipe to',
    layout.REVEAL_TOO_SHORT: 'the finished look is on screen too briefly',
    layout.NO_BEFORE_SHOT: 'there is no earlier shot to wipe from',
    layout.NO_ROOM: 'there was no room for it without covering her face',
}
_NAMES = {'title': 'the animated title', 'logo_sting': 'the logo sting',
          'end_card': 'the end card', 'wipe': 'the before-and-after wipe'}


def message(notes: Sequence[str]) -> str:
    """What her agent tells her about the overlays, from a reel's notes
    ('' when none were asked for or all were rendered)."""
    parts = []
    for note in notes:
        if not isinstance(note, str):
            continue
        if note.startswith('motion_skipped:'):
            _p, template, why = (note.split(':') + ['', ''])[:3]
            parts.append('%s was left out: %s; say so.' % (
                _NAMES.get(template, template).capitalize(), _WHY.get(why, why)))
        elif note.startswith('motion:'):
            said = _SAY.get(note.split(':', 1)[1])
            if said:
                parts.append(said)
    return ' '.join(parts)


def _overlays_without(edl: Mapping, kind: str) -> List[Dict[str, Any]]:
    return [dict(ov) for ov in edl['overlays'] if ov.get('type') != kind]


def plain_overlays(edl: Mapping, plan: layout.MotionPlan) -> List[Dict[str, Any]]:
    """The planner's overlays plus the plain-ffmpeg stand-ins for ``plan``:
    only the end card needs one (a boxed text overlay at the card's centre,
    for the card's time); the title is the hook text already there, the
    logo stays still, the wipe is the cut."""
    out = [dict(ov) for ov in edl['overlays']]
    for item in plan.items:
        if item.template != 'end_card':
            continue
        x0, y0, x1, y1 = item.box
        out.append({'type': 'text', 'text': CARD_JOIN.join(item.props['lines']), 'font': 'auto',
                    'size_px': CARD_TEXT_PX, 'color': CARD_TEXT_COLOR, 'box': True,
                    'x': round((SAFE_ZONE['x_min'] + SAFE_ZONE['x_max']) / 2.0 / CANVAS['w'], 6),
                    'y': round((y0 + y1) / 2.0 / CANVAS['h'], 6), 'anchor': 'center',
                    'from_s': item.from_s, 'to_s': item.to_s})
    return out


def _data_uri(path: str) -> str:
    """``path`` as a data: URI (the browser Remotion drives cannot open a
    local file path).  Raises OSError for a missing or too-large file."""
    size = os.path.getsize(path)
    if size > IMAGE_MAX_BYTES:
        raise OSError('%s is %d bytes, over %d' % (path, size, IMAGE_MAX_BYTES))
    mime = mimetypes.guess_type(path)[0] or 'image/png'
    with open(path, 'rb') as fh:
        return 'data:%s;base64,%s' % (mime, base64.b64encode(fh.read()).decode('ascii'))


def before_frame(edl: Mapping, seg_index: int, out_path: str) -> str:
    """The middle frame of segment ``seg_index`` as the reel shows it (its
    crop, at canvas size: render.geometry_filter) in a JPEG at
    ``out_path``.  Raises media_bin.FFmpegFailed / MediaToolMissing."""
    from core import media_bin
    from integrations.reel_studio.media import render
    seg = edl['segments'][seg_index]
    clip = edl['clips'][seg['clip']]
    mid = (seg['in_s'] + seg['out_s']) / 2.0
    tonemap = False
    if clip.get('transfer', 'sdr') != 'sdr':
        caps = media_bin.ffmpeg_caps()
        tonemap = bool(caps and 'zscale' in caps.filters and 'tonemap' in caps.filters)
    vf = render.geometry_filter(seg, clip, canvas={'w': int(CANVAS['w']), 'h': int(CANVAS['h'])},
                                tonemap=tonemap)
    result = media_bin.run_ffmpeg(
        ['-y', '-loglevel', 'error', '-ss', '%.6f' % mid, '-i', clip['path'], '-map', '0:V:0',
         '-frames:v', '1', '-vf', vf, '-q:v', '3', out_path], timeout_s=60.0)
    if result.timed_out or result.returncode != 0 or not os.path.isfile(out_path):
        raise media_bin.FFmpegFailed('the before frame could not be cut: %s'
                                     % (result.stderr or '').strip()[-300:],
                                     returncode=result.returncode,
                                     stderr_tail=(result.stderr or '')[-2000:],
                                     timed_out=result.timed_out)
    return out_path


def _render_items(edl: Mapping, plan: layout.MotionPlan, workdir: str) -> List[Dict[str, Any]]:
    """Render ``plan`` with Remotion; the overlays to lay.  Raises
    runtime.MotionFailed, OSError or media_bin errors."""
    fps = int(CANVAS['fps'])
    logo = next((ov for ov in edl['overlays'] if ov.get('type') == 'logo'), None)
    images: Dict[str, str] = {}
    jobs, overlays = [], []
    for k, item in enumerate(plan.items):
        props = dict(item.props)
        if item.image == 'logo':
            images.setdefault('logo', _data_uri(logo['src']))
            props['logo'] = images['logo']
        elif item.image == 'before':
            if 'before' not in images:
                frame = before_frame(edl, plan.before_segment,
                                     os.path.join(workdir, 'motion_before.jpg'))
                images['before'] = _data_uri(frame)
            props['before'] = images['before']
        x0, y0, x1, y1 = item.box
        props.update(width=int(x1 - x0), height=int(y1 - y0), fps=fps,
                     durationInFrames=max(1, int(round((item.to_s - item.from_s) * fps))))
        out = os.path.abspath(os.path.join(workdir, 'motion_%d_%s.mov' % (k, item.template)))
        jobs.append({'id': layout.TEMPLATE_IDS[item.template], 'props': props, 'out': out})
        overlays.append({'type': 'motion', 'template': item.template, 'src': out,
                         'box': [x0, y0, x1, y1], 'from_s': item.from_s, 'to_s': item.to_s})
    runtime.render(jobs, workdir)
    return overlays


def _with_motion(edl: Mapping, plan: layout.MotionPlan,
                 motion: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The planner's overlays with the rendered clips laid in: the animated
    title replaces the still hook text, and the still logo starts when the
    sting ends."""
    templates = {item.template: item for item in plan.items}
    out = _overlays_without(edl, 'text') if 'title' in templates else \
        [dict(ov) for ov in edl['overlays']]
    sting = templates.get('logo_sting')
    for ov in out:
        if sting is not None and ov.get('type') == 'logo':
            ov['from_s'] = sting.to_s
    return out + motion


def _valid(edl: Mapping, overlays: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    candidate = dict(edl, overlays=overlays)
    problems = validate_edl(candidate)
    if problems:
        logger.error('reel motion: the composed EDL does not validate: %s',
                     '; '.join(problems[:5]))
        return None
    return candidate


def add_motion(edl: Mapping, request: Optional[Mapping], *, workdir: str,
               progress: Optional[Callable[[str], None]] = None,
               wait_s: float = SETUP_WAIT_S) -> Tuple[Dict[str, Any], List[str]]:
    """(the EDL to render, notes) -- see the module note.  ``progress(text)``
    hears about a Remotion setup this reel waits for.  Never raises for a
    missing, refused or failing Remotion: the plain versions are laid."""
    request = request or {}
    wanted = [t for t in (request.get('overlays') or ()) if isinstance(t, str)]
    if not wanted:
        return dict(edl), []
    plan = layout.plan_motion(edl, wanted, hook_text=request.get('hook_text') or '',
                              card_text=request.get('card_text') or ())
    notes = list(plan.notes)
    if not plan.items:
        return dict(edl), notes
    readiness = runtime.ensure_ready(wait_s=wait_s, progress=progress)
    state = readiness.state
    composed = None
    if readiness.ready:
        try:
            composed = _valid(edl, _with_motion(edl, plan, _render_items(edl, plan, workdir)))
            # Rendered but not valid (a layout bug, logged): the plain versions.
            state = RENDERED if composed is not None else runtime.FAILED
        except Exception as e:      # Remotion, ffmpeg or a file: the plain versions
            logger.warning('reel motion: rendering the overlays failed: %s: %s',
                           type(e).__name__, e)
            state = runtime.FAILED
    elif readiness.detail:
        logger.warning('reel motion: Remotion is not ready (%s): %s', state, readiness.detail)
    if composed is None:
        composed = _valid(edl, plain_overlays(edl, plan))
        if composed is None:
            state = REFUSED
            composed = dict(edl)
    return composed, notes + [NOTE_MOTION % state]


__all__ = ['NOTE_MOTION', 'RENDERED', 'REFUSED', 'SETUP_WAIT_S', 'CARD_TEXT_PX', 'message',
           'plain_overlays', 'before_frame', 'add_motion']
