"""An animated ('motion') overlay in the EDL: contract, pass-2 composite, QA mask.

Amendment A14: Reel Studio's animated overlays (a kinetic title, a logo
sting, a booking/price end card, a before->after wipe) are rendered with
alpha by the motion runtime (Remotion, integrations/reel_studio/motion) and
composited over the ffmpeg cut through core.media_bin, inside
contracts.SAFE_ZONE, under the same QA as every reel.  In the EDL each is an
overlay ``{'type': 'motion', 'template', 'src', 'box': [x0, y0, x1, y1],
'from_s', 'to_s'}``:

* contracts.validate_edl ('overlays'): inside the reel's time and inside
  SAFE_ZONE -- except the before->after wipe, the one full-frame template,
  which covers the whole canvas only over the reveal's opening and leaves the
  final look uncovered for QUALITY_BAR reveal.min_hold_s at the end (A1:
  the reel ends on the reveal);
* render pass 2: the clip is one more input, shifted to from_s, laid at its
  box with its alpha, the main picture passing through outside its window;
* qa: its box is masked from the frame comparisons while it is on screen.

The clips here are made by ffmpeg itself (ProRes 4444 with alpha), so no
Node, npm or browser is involved.
"""
import os

import numpy as np
import pytest

from core import media_bin
from integrations.reel_studio import contracts as C
from integrations.reel_studio.contracts import ClipCapture
from integrations.reel_studio.media import render as R
from integrations.reel_studio.media.probe import probe
from tests.unit.reel_media_fixtures import golden_edl, make_clip, requires_ffmpeg

TITLE_BOX = [60.0, 260.0, 790.0, 560.0]
FULL = [0.0, 0.0, float(C.CANVAS['w']), float(C.CANVAS['h'])]


def _motion(template='title', box=None, start=0.0, end=2.5, src='C:/reel/work/m0.mov'):
    return {'type': 'motion', 'template': template, 'src': src,
            'box': list(box or TITLE_BOX), 'from_s': start, 'to_s': end}


def _problems(edl, check='overlays'):
    return [p for p in C.validate_edl(edl) if p.startswith(check + ':')]


# ── the contract ──────────────────────────────────────────────────────────

def test_a_title_inside_the_safe_zone_is_a_valid_edl():
    edl = golden_edl()
    edl['overlays'].append(_motion())
    assert C.validate_edl(edl) == []


@pytest.mark.parametrize('template, box', [
    ('title', [60.0, 260.0, 1000.0, 560.0]),       # under the right-hand buttons
    ('end_card', [40.0, 1200.0, 810.0, 1600.0]),   # under the caption area
    ('logo_sting', [300.0, 100.0, 600.0, 400.0]),  # under the top bar
    ('title', FULL),                               # only the wipe may fill the frame
])
def test_a_box_leaving_the_safe_zone_fails_overlays(template, box):
    edl = golden_edl()
    edl['overlays'].append(_motion(template, box))
    assert _problems(edl), C.validate_edl(edl)


@pytest.mark.parametrize('start, end', [(19.0, 21.0), (-0.5, 1.0), (3.0, 3.0), (25.0, 26.0)])
def test_a_clip_outside_the_reel_fails_overlays(start, end):
    edl = golden_edl()
    edl['overlays'].append(_motion(start=start, end=end))
    assert _problems(edl)


@pytest.mark.parametrize('bad', [
    {'template': 'confetti'}, {'src': ''}, {'to_s': None}, {'box': [0, 0, 10]},
    {'box': [500.0, 600.0, 100.0, 900.0]}, {'box': 'all'},
])
def test_a_malformed_motion_overlay_is_a_schema_problem(bad):
    edl = golden_edl()
    edl['overlays'].append(dict(_motion(), **bad))
    problems = C.validate_edl(edl)
    assert problems and all(p.startswith('schema:') for p in problems), problems


def test_the_wipe_over_the_reveals_opening_is_valid():
    """The golden reveal runs 15-20 s: a 1.2 s wipe from 15 s leaves 3.8 s."""
    edl = golden_edl()
    edl['overlays'].append(_motion('wipe', FULL, 15.0, 16.2))
    assert C.validate_edl(edl) == []


@pytest.mark.parametrize('start, end, why', [
    (15.0, 19.0, 'leaves 1.0 s of the final look, under reveal.min_hold_s'),
    (14.0, 15.5, 'starts before the reveal, over a process shot'),
    (15.0, 20.0, 'covers the reveal to the end'),
])
def test_a_wipe_that_hides_the_reveal_fails_overlays(start, end, why):
    edl = golden_edl()
    edl['overlays'].append(_motion('wipe', FULL, start, end))
    assert _problems(edl), why


def test_the_reveals_face_box_in_canvas_pixels():
    """The golden reveal: clip B (1920x1080) at zoom 1, face x .45 y .3
    w .1 h .2 -> canvas px 369.3-710.7 x 576-960 (the review's probe)."""
    edl = golden_edl()
    box = C.segment_face_box(edl, edl['segments'][-1])
    assert box == pytest.approx((369.333, 576.0, 710.667, 960.0), abs=0.01)
    no_face = dict(edl['segments'][-1], face=None)
    assert C.segment_face_box(edl, no_face) is None


@pytest.mark.parametrize('template, box, start, end', [
    # The review's probe of 4fab306ff: an end card over her face for the
    # whole 15-20 s reveal validated clean.
    ('end_card', [0.0, 230.0, 850.0, 1440.0], 15.0, 20.0),
    ('end_card', [40.0, 900.0, 810.0, 1260.0], 17.0, 20.0),       # its top edge on her chin
    ('title', [60.0, 600.0, 790.0, 900.0], 18.6, 19.9),            # any non-wipe template
    ('logo_sting', [400.0, 700.0, 700.0, 1000.0], 19.0, 20.0),
])
def test_a_motion_box_over_her_face_in_the_final_hold_fails_overlays(template, box, start, end):
    """A1: the reel ends on the reveal.  The wipe must leave the final look
    uncovered for reveal.min_hold_s; every other motion box must leave her
    FACE uncovered for that long (QA masks the whole box, so reveal_render
    cannot see this)."""
    edl = golden_edl()
    edl['overlays'].append(_motion(template, box, start, end))
    problems = _problems(edl)
    assert problems and any('face' in p for p in problems), C.validate_edl(edl)


@pytest.mark.parametrize('box, start, end, why', [
    ([40.0, 1000.0, 810.0, 1400.0], 17.0, 20.0, 'below her face, through the hold'),
    ([40.0, 260.0, 810.0, 540.0], 17.0, 20.0, 'above her face, through the hold'),
    ([0.0, 230.0, 850.0, 1440.0], 15.0, 18.4, 'over her face, but gone before the last 1.5 s'),
])
def test_a_motion_box_that_leaves_her_face_in_the_final_hold_is_valid(box, start, end, why):
    edl = golden_edl()
    edl['overlays'].append(_motion('end_card', box, start, end))
    assert C.validate_edl(edl) == [], why


def test_without_a_face_box_the_face_rule_has_nothing_to_keep_clear():
    edl = golden_edl()
    edl['segments'][-1]['face'] = None
    edl['overlays'].append(_motion('end_card', [0.0, 230.0, 850.0, 1440.0], 15.0, 20.0))
    assert C.validate_edl(edl) == []


def test_a_wipe_must_fill_the_frame():
    edl = golden_edl()
    edl['overlays'].append(_motion('wipe', TITLE_BOX, 15.0, 16.0))
    assert _problems(edl)


def test_no_final_look_means_no_wipe():
    """With no reveal there is nothing to wipe to (the EDL's own reveal
    rows aside, the overlay itself is refused)."""
    edl = golden_edl()
    edl['final_look_found'] = False
    edl['reveal'] = None
    edl['segments'][-1]['role'] = 'process'
    edl['segments'][-1]['stage'] = 'lips'
    edl['notes'].append(C.NOTE_NO_FINAL_LOOK)
    edl['overlays'].append(_motion('wipe', FULL, 15.0, 16.0))
    assert _problems(edl)


# ── pass 2 ────────────────────────────────────────────────────────────────

def test_the_final_args_lay_the_clip_at_its_box_from_its_start():
    edl = golden_edl()
    edl['overlays'] = [_motion(start=2.5, end=5.0, src='C:/work/m 0.mov')]
    small = {'w': 216, 'h': 384}
    args = R.plan_final_args(edl, 'C:/work/list.txt', 'C:/work/out.mp4', encoder='libx264',
                             textfiles={}, canvas=small)
    inputs = [args[i + 1] for i, a in enumerate(args) if a == '-i']
    assert inputs == ['C:/work/list.txt', 'C:/work/m 0.mov']
    graph = args[args.index('-filter_complex') + 1]
    f = small['w'] / 1080.0
    assert 'setpts=PTS-STARTPTS+2.5/TB' in graph
    assert 'scale=%d:%d' % (round(730 * f), round(300 * f)) in graph
    assert 'overlay=x=%d:y=%d' % (round(60 * f), round(260 * f)) in graph
    assert 'eof_action=pass' in graph
    assert 'gte(t\\,2.5)*lt(t\\,5)' in graph
    assert graph.endswith('[out]')


def test_the_wipe_is_laid_on_the_cut_before_text_logos_and_other_overlays():
    """The real Remotion render (48948fd70) showed the wipe's "before"
    picture, laid last, hiding the still logo and the end card while it
    played.  The wipe is part of the picture: pass 2 lays it on the cut
    first, then the text, the logos and the other animated overlays."""
    edl = golden_edl()
    edl['overlays'] += [_motion('end_card', [40.0, 1000.0, 810.0, 1400.0], 15.0, 20.0,
                                src='C:/work/card.mov'),
                        _motion('wipe', FULL, 15.0, 16.2, src='C:/work/wipe.mov')]
    args = R.plan_final_args(edl, 'C:/work/list.txt', 'C:/work/out.mp4', encoder='libx264',
                             textfiles={0: ('C:/work/t.txt', 'C:/fonts/f.ttf')},
                             canvas={'w': 216, 'h': 384})
    inputs = [args[i + 1] for i, a in enumerate(args) if a == '-i']
    wipe, logo, card = (inputs.index(p) for p in ('C:/work/wipe.mov', edl['overlays'][1]['src'],
                                                    'C:/work/card.mov'))
    graph = args[args.index('-filter_complex') + 1]
    order = [graph.index('[%d:v]setpts' % wipe), graph.index('drawtext'),
             graph.index('[%d:v]format=rgba' % logo), graph.index('[%d:v]setpts' % card)]
    assert order == sorted(order), graph
    assert graph.startswith('[%d:v]setpts' % wipe), graph
    assert graph.endswith('[out]')


def _alpha_clip(d, name, size, seconds, *, color='red', alpha="if(lt(X,W/2),255,0)"):
    """A ProRes 4444 clip of ``color`` with ``alpha`` (default: opaque on its
    left half, clear on its right)."""
    out = os.path.join(str(d), name)
    w, h = size
    result = media_bin.run_ffmpeg(
        ['-y', '-loglevel', 'error', '-f', 'lavfi',
         '-i', 'color=c=%s:size=%dx%d:rate=30:duration=%g' % (color, w, h, seconds),
         '-vf', "format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='%s'" % alpha,
         '-c:v', 'prores_ks', '-profile:v', '4444', '-pix_fmt', 'yuva444p10le', out],
        timeout_s=120)
    assert result.returncode == 0, result.stderr[-800:]
    return out


def _frame(path, t_s, w, h):
    stream = media_bin.ffmpeg_stream(
        ['-loglevel', 'error', '-ss', '%.3f' % t_s, '-i', path, '-frames:v', '1',
         '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1'], frame_bytes=w * h * 3, timeout_s=60)
    try:
        chunk = next(stream)
    finally:
        stream.close()
    return np.frombuffer(chunk, np.uint8).reshape(h, w, 3)


def _red(px):
    b, g, r = (int(v) for v in px)
    return r > 170 and g < 90 and b < 90


def _black(px):
    return all(int(v) < 40 for v in px)


@requires_ffmpeg
def test_the_clip_is_composited_with_its_alpha_only_inside_its_window(tmp_path):
    small = {'w': 216, 'h': 384}
    src = make_clip(tmp_path, 'black.mp4', size=(216, 384), source='color', color='black',
                    duration_s=3.0)
    info = probe(src)
    x = 'e' * 64
    clip = info.to_edl_clip(ClipCapture(clip=x, order=0, captured_at_ms=1760247082000,
                                        src='phone'))
    seg = {'clip': x, 'in_s': 0.0, 'out_s': 3.0, 'speed': 1.0, 'out_start_s': 0.0,
           'out_end_s': 3.0, 'crop': {'cx': 0.5, 'cy': 0.5, 'zoom': 1.0}, 'face': None,
           'transition': {'type': 'cut', 'dur_s': 0.0}, 'stage': 'final_look', 'appeal': 7.0,
           'role': 'reveal'}
    f = small['w'] / 1080.0
    box = [100.0, 400.0, 700.0, 1000.0]
    moving = _alpha_clip(tmp_path, 'm0.mov', (round(600 * f), round(600 * f)), 1.0)
    edl = {'schema': C.EDL_SCHEMA, 'canvas': dict(C.CANVAS), 'target_s': 5.0,
           'duration_s': 3.0, 'audio': None, 'final_look_found': True, 'hook_first': False,
           'capture_src': 'phone', 'reveal': {'clip': x, 't_s': 1.5},
           'notes': [C.capture_note('phone')],
           'beats': {'source': 'synthetic', 'song_key': None, 'bpm': 120.0,
                     'times_s': [0.5 * k for k in range(0, 8)], 'confidence': 0.0},
           'clips': {x: clip}, 'segments': [seg],
           'overlays': [_motion('title', box, 1.0, 2.0, src=moving)]}
    out = str(tmp_path / 'reel.mp4')
    result = R.render_reel(edl, out, workdir=str(tmp_path / 'work'), encoder='libx264',
                           canvas=small, timeout_s=120)
    assert result.duration_s == pytest.approx(3.0)
    left = (int((400 + 300) * f), int((100 + 150) * f))     # (row, col): left half of the box
    right = (int((400 + 300) * f), int((100 + 450) * f))    # right half: clear
    outside = (int(200 * f), int(900 * f))
    during = _frame(out, 1.5, small['w'], small['h'])
    assert _red(during[left]), during[left]
    assert _black(during[right]), 'the clip\'s clear half let the picture through'
    assert _black(during[outside])
    for t in (0.5, 2.5):
        frame = _frame(out, t, small['w'], small['h'])
        assert _black(frame[left]), 'outside its window the picture passes through (t=%s)' % t
    assert probe(out).duration_s == pytest.approx(3.0, abs=0.05)


@requires_ffmpeg
def test_her_logo_stays_on_screen_while_the_wipe_plays(tmp_path):
    """Rendered: an opaque full-frame wipe clip (blue) over the reveal's
    opening and her still logo (red) top-left: during the wipe the logo is
    drawn on the wipe, and the wipe covers the cut everywhere else."""
    from tests.unit.reel_media_fixtures import make_png
    small = {'w': 216, 'h': 384}
    f = small['w'] / 1080.0
    src = make_clip(tmp_path, 'black.mp4', size=(216, 384), source='color', color='black',
                    duration_s=3.0)
    x = 'd' * 64
    clip = probe(src).to_edl_clip(ClipCapture(clip=x, order=0, captured_at_ms=1760247082000,
                                              src='phone'))
    seg = {'clip': x, 'in_s': 0.0, 'out_s': 3.0, 'speed': 1.0, 'out_start_s': 0.0,
           'out_end_s': 3.0, 'crop': {'cx': 0.5, 'cy': 0.5, 'zoom': 1.0}, 'face': None,
           'transition': {'type': 'cut', 'dur_s': 0.0}, 'stage': 'final_look', 'appeal': 7.0,
           'role': 'reveal'}
    wipe = _alpha_clip(tmp_path, 'wipe.mov', (216, 384), 1.0, color='blue', alpha='255')
    logo = {'type': 'logo', 'src': make_png(tmp_path, 'logo.png', color='red'),
            'corner': 'top_left', 'width_px': 180, 'height_px': 90, 'margin_px': 48,
            'opacity': 1.0, 'from_s': 0.0, 'to_s': None}
    edl = {'schema': C.EDL_SCHEMA, 'canvas': dict(C.CANVAS), 'target_s': 5.0,
           'duration_s': 3.0, 'audio': None, 'final_look_found': True, 'hook_first': False,
           'capture_src': 'phone', 'reveal': {'clip': x, 't_s': 1.5},
           'notes': [C.capture_note('phone')],
           'beats': {'source': 'synthetic', 'song_key': None, 'bpm': 120.0,
                     'times_s': [0.5 * k for k in range(0, 8)], 'confidence': 0.0},
           'clips': {x: clip}, 'segments': [seg],
           'overlays': [logo, _motion('wipe', FULL, 0.0, 1.0, src=wipe)]}
    assert [p for p in C.validate_edl(edl) if p.startswith('overlays:')] == []
    out = str(tmp_path / 'reel.mp4')
    R.render_reel(edl, out, workdir=str(tmp_path / 'work'), encoder='libx264', canvas=small,
                  timeout_s=120)
    x0, y0, x1, y1 = C.logo_box(logo)
    inside = (int((y0 + y1) / 2 * f), int((x0 + x1) / 2 * f))      # (row, col)
    elsewhere = (int(1200 * f), int(700 * f))
    during = _frame(out, 0.5, small['w'], small['h'])
    assert _red(during[inside]), 'her logo is drawn on the wipe: %s' % (during[inside],)
    b, g, r = (int(v) for v in during[elsewhere])
    assert b > 170 and r < 90, 'the wipe covers the cut: %s' % (during[elsewhere],)


@requires_ffmpeg
def test_a_full_size_reel_with_a_motion_overlay_passes_every_hard_check(tmp_path):
    """QA masks the overlay: an opaque box over most of the safe zone for the
    whole reel would otherwise fail the reveal frame comparison."""
    from integrations.reel_studio.media.qa import run_qa
    src = make_clip(tmp_path, 'p.mp4', size=(608, 1080), fps=30, duration_s=2.5)
    x = 'f' * 64
    clip = probe(src).to_edl_clip(ClipCapture(clip=x, order=0, captured_at_ms=1760247082000,
                                              src='phone'))
    seg = {'clip': x, 'in_s': 0.0, 'out_s': 2.0, 'speed': 1.0, 'out_start_s': 0.0,
           'out_end_s': 2.0, 'crop': {'cx': 0.5, 'cy': 0.5, 'zoom': 1.0}, 'face': None,
           'transition': {'type': 'cut', 'dur_s': 0.0}, 'stage': 'final_look', 'appeal': 7.0,
           'role': 'reveal'}
    box = [0.0, 230.0, 850.0, 1440.0]
    moving = _alpha_clip(tmp_path, 'big.mov', (850, 1210), 2.0)
    edl = {'schema': C.EDL_SCHEMA, 'canvas': dict(C.CANVAS), 'target_s': 5.0,
           'duration_s': 2.0, 'audio': None, 'final_look_found': True, 'hook_first': False,
           'capture_src': 'phone', 'reveal': {'clip': x, 't_s': 1.0},
           'notes': [C.capture_note('phone')],
           'beats': {'source': 'synthetic', 'song_key': None, 'bpm': 120.0,
                     'times_s': [0.5 * k for k in range(0, 8)], 'confidence': 0.0},
           'clips': {x: clip}, 'segments': [seg],
           'overlays': [_motion('end_card', box, 0.0, 2.0, src=moving)]}
    problems = [p for p in C.validate_edl(edl) if not p.startswith('duration:')]
    assert problems == []
    out = str(tmp_path / 'reel.mp4')
    R.render_reel(edl, out, workdir=str(tmp_path / 'work'), encoder='libx264', timeout_s=300)
    report = run_qa(out, edl)
    assert C.validate_qa_report(report) == []
    hard = {r['id']: r for r in report['checks']}
    for check in ('resolution', 'codec', 'fps', 'audio', 'size_cap', 'faststart',
                  'first_frame_luma', 'overlays', 'reveal'):
        assert hard[check]['ok'], hard[check]
    assert hard['fps']['value']['frames'] == 60
    reveal = next(r for r in report['advisory'] if r['id'] == 'reveal_render')
    assert reveal['ok'], reveal
