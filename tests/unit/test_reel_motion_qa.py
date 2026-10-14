"""QA's advisory frame checks with animated overlays on screen (review of 4fab306ff).

An animated ('motion') overlay's box is masked from the frame comparisons
while it plays (its alpha changes frame to frame).  Two advisory rows then
misread a reel that is fine:

* segment_fidelity: a full-frame wipe over a segment's middle frame leaves
  no pixel to compare, _masked_corr answers None, and the row failed, so
  the reply carried 'advisory:segment_fidelity'.  A fully masked segment is
  now skipped and said so ('masked'), not failed.
* face_render: Haar looks at the composited frame, so a motion box over her
  face hid it and the row failed.  A segment whose face is under a motion
  box at the frame checked is now left out of the count ('covered').

Real ffmpeg renders (small canvas: the advisory rows read any size).
"""
import os

import pytest

from core import media_bin
from integrations.reel_studio import contracts as C
from integrations.reel_studio.contracts import ClipCapture
from integrations.reel_studio.media import render as R
from integrations.reel_studio.media.probe import probe
from integrations.reel_studio.media.qa import run_qa
from tests.unit.reel_media_fixtures import make_clip, requires_ffmpeg

SMALL = {'w': 216, 'h': 384}
X = 'c' * 64


def _clip_with_alpha(d, name, size, seconds):
    out = os.path.join(str(d), name)
    w, h = size
    result = media_bin.run_ffmpeg(
        ['-y', '-loglevel', 'error', '-f', 'lavfi',
         '-i', 'color=c=red:size=%dx%d:rate=30:duration=%g' % (w, h, seconds),
         '-vf', 'format=rgba', '-c:v', 'prores_ks', '-profile:v', '4444',
         '-pix_fmt', 'yuva444p10le', out], timeout_s=120)
    assert result.returncode == 0, result.stderr[-800:]
    return out


def _edl(tmp_path, overlays, face=None):
    src = make_clip(tmp_path, 'src.mp4', size=(216, 384), fps=30, duration_s=4.5)
    clip = probe(src).to_edl_clip(ClipCapture(clip=X, order=0, captured_at_ms=1760247082000,
                                              src='phone'))
    seg = {'clip': X, 'in_s': 0.0, 'out_s': 4.0, 'speed': 1.0, 'out_start_s': 0.0,
           'out_end_s': 4.0, 'crop': {'cx': 0.5, 'cy': 0.5, 'zoom': 1.0}, 'face': face,
           'transition': {'type': 'cut', 'dur_s': 0.0}, 'stage': 'final_look', 'appeal': 8.0,
           'role': 'reveal'}
    return {'schema': C.EDL_SCHEMA, 'canvas': dict(C.CANVAS), 'target_s': 5.0,
            'duration_s': 4.0, 'audio': None, 'final_look_found': True, 'hook_first': False,
            'capture_src': 'phone', 'reveal': {'clip': X, 't_s': 3.0},
            'notes': [C.capture_note('phone')],
            'beats': {'source': 'synthetic', 'song_key': None, 'bpm': 120.0,
                      'times_s': [0.5 * k for k in range(0, 10)], 'confidence': 0.0},
            'clips': {X: clip}, 'segments': [seg], 'overlays': overlays}


def _advisory(report, check):
    return next(r for r in report['advisory'] if r['id'] == check)


@requires_ffmpeg
def test_a_segment_fully_under_a_wipe_is_skipped_not_failed(tmp_path):
    wipe = _clip_with_alpha(tmp_path, 'wipe.mov', (SMALL['w'], SMALL['h']), 2.5)
    edl = _edl(tmp_path, [{'type': 'motion', 'template': 'wipe', 'src': wipe,
                           'box': [0.0, 0.0, 1080.0, 1920.0], 'from_s': 0.0, 'to_s': 2.5}])
    assert [p for p in C.validate_edl(edl) if p.startswith('overlays')] == []
    out = str(tmp_path / 'reel.mp4')
    R.render_reel(edl, out, workdir=str(tmp_path / 'work'), encoder='libx264', canvas=SMALL,
                  timeout_s=120)
    row = _advisory(run_qa(out, edl), 'segment_fidelity')
    assert row['ok'], row
    assert row['value'] == [{'segment': 0, 'corr': None, 'masked': True}]


@requires_ffmpeg
def test_a_face_under_a_motion_box_is_not_counted_missing(tmp_path):
    f = SMALL['w'] / 1080.0
    box = [60.0, 520.0, 790.0, 1020.0]
    title = _clip_with_alpha(tmp_path, 'title.mov',
                             (round((box[2] - box[0]) * f), round((box[3] - box[1]) * f)), 1.4)
    face = {'x': 0.3, 'y': 0.3, 'w': 0.4, 'h': 0.2}          # canvas 324-756 x 576-960
    edl = _edl(tmp_path, [{'type': 'motion', 'template': 'title', 'src': title, 'box': box,
                           'from_s': 1.0, 'to_s': 2.4}], face=face)
    assert [p for p in C.validate_edl(edl) if p.startswith('overlays')] == [], \
        'gone before the final hold, so the face rule allows it'
    out = str(tmp_path / 'reel.mp4')
    R.render_reel(edl, out, workdir=str(tmp_path / 'work'), encoder='libx264', canvas=SMALL,
                  timeout_s=120)
    row = _advisory(run_qa(out, edl), 'face_render')
    assert row['ok'], row
    assert row['value'] == {'segments_with_face': 1, 'found': 0, 'covered': 1}
