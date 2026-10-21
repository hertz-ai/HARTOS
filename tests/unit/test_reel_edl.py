"""integrations.reel_studio.media.edl: the deterministic planner.

plan_edit turns the analysis of her clips, the scored moments and a beat
grid into a ``hart.reel.edl.v1`` document that contracts.validate_edl
accepts:

* the clips play in the order they were FILMED (phone capture time, else
  the container's creation_time, else upload order) and the shots inside a
  clip by in-point; stage labels only drop or pick shots, never reorder;
* the reel ends on the best final-look shot, held for at least 1.5 s;
* an optional final-look teaser of at most 1 s opens the reel only when
  her style asks for it (``hook_first``);
* every cut is on the beat grid, the length is within 1 s of the target
  (clamped to 5-30 s), and the crop keeps her face in the upper part of
  the frame without bars.

Most tests build ClipAnalysis objects by hand (no decoding), so each rule
is checked on footage whose shots, stages and faces are known.  The last
test runs the real analysis on two synthetic clips.
"""
import json

import cv2
import numpy as np
import pytest

from integrations.reel_studio import contracts as C
from integrations.reel_studio.contracts import (
    BeatGrid, Box, Candidate, ClipAnalysis, ClipCapture, ClipInfo, PlanError, Quality, Segment)
from integrations.reel_studio.media import edl as E
from integrations.reel_studio.media.beats import synthetic_grid
from tests.unit.reel_media_fixtures import make_clip, requires_ffmpeg

XA, XB, XC = 'a' * 64, 'b' * 64, 'c' * 64
T0_MS = 1760247082000           # 2025-10-12, a plausible phone capture time
PORTRAIT = (1080, 1920)


# ── builders ──────────────────────────────────────────────────────────────

def _analysis(clip, bounds, *, size=PORTRAIT, dur=None, face=None, fps=30.0,
              capture=None, creation_time=None, scores=None):
    """A ClipAnalysis whose shots are ``bounds`` (seconds) with one candidate each."""
    dur = float(dur if dur is not None else bounds[-1])
    w, h = size
    info = ClipInfo(path='C:/clips/%s.mp4' % clip[:6], duration_s=dur, width=w, height=h,
                    display_w=w, display_h=h, rotation=0, fps_avg=fps, codec='h264',
                    hfr=fps >= C.HFR_MIN_FPS, creation_time=creation_time)
    times = np.round(np.arange(0.0, bounds[-1] - 1e-9, 0.2), 6)
    n = len(times)
    segments = [Segment(float(a), float(b)) for a, b in zip(bounds, bounds[1:])]
    cands = []
    for i, seg in enumerate(segments):
        t = round((seg.t0 + seg.t1) / 2.0, 6)
        score = scores[i] if scores else 0.6
        cands.append(Candidate(clip=clip, t_s=t, segment=i, jpeg=b'',
                               quality=Quality(score=score, sharp=150.0, luma=0.5, clipped=0.0),
                               face=face))
    return ClipAnalysis(clip=clip, info=info, times=times, corr=np.ones(n), corr_skip=np.ones(n),
                        sharp=np.full(n, 150.0), luma=np.full(n, 0.5), clipped=np.zeros(n),
                        shifts=np.zeros((n, 2)), faces=[face] * n, segments=segments,
                        candidates=cands, capture=capture)


def _phone(clip, order):
    return ClipCapture(clip=clip, order=order, captured_at_ms=T0_MS + order * 300000, src='phone')


def _moment(clip, t_s, stage, appeal):
    return {'clip': clip, 't_s': float(t_s), 'stage': stage, 'appeal': float(appeal),
            'face_visible': True, 'src': 'vlm'}


def _moments(items):
    items = list(items)
    return {'schema': C.MOMENTS_SCHEMA, 'src': 'vlm', 'model': None,
            'final_look_found': any(m['stage'] == C.FINAL_LOOK for m in items),
            'notes': [], 'moments': items}


def _story(final_appeals=(8.0, 9.5)):
    """Three clips filmed A, B, C; C holds two final-look shots."""
    a = _analysis(XA, [0, 3, 6, 9, 12], capture=_phone(XA, 0))
    b = _analysis(XB, [0, 4, 8, 12], capture=_phone(XB, 1))
    c = _analysis(XC, [0, 5, 10], capture=_phone(XC, 2))
    moments = _moments([
        _moment(XA, 1.4, 'bare_face', 5.0), _moment(XA, 4.4, 'base', 6.0),
        _moment(XA, 7.4, 'base', 5.5), _moment(XA, 10.4, 'eyes', 7.0),
        _moment(XB, 2.0, 'lips', 7.5), _moment(XB, 6.0, 'hair', 6.5),
        _moment(XB, 10.0, 'jewellery', 7.2),
        _moment(XC, 2.4, 'final_look', final_appeals[0]),
        _moment(XC, 7.4, 'final_look', final_appeals[1]),
    ])
    return [a, b, c], moments


def _grid(dur=31.0):
    return synthetic_grid(120.0, dur)


def _style(**kw):
    return C.make_style(dict({'target_s': 20}, **kw))


def _keys(edl):
    clips = edl['clips']
    return [(clips[s['clip']]['order'], s['in_s']) for s in edl['segments'] if s['role'] != 'teaser']


def _seg_len(s):
    return s['out_end_s'] - s['out_start_s']


def _plan(analyses, moments, **style):
    return E.plan_edit(moments, analyses, _grid(), _style(**style))


# ── the story: filming order, ending on the reveal ────────────────────────

def test_the_plan_is_a_valid_edl_in_capture_order_ending_on_the_reveal():
    analyses, moments = _story()
    # Given out of capture order: the plan must sort by when each clip was filmed.
    edl = _plan([analyses[2], analyses[0], analyses[1]], moments)
    assert C.validate_edl(edl) == []
    assert edl['schema'] == C.EDL_SCHEMA and edl['canvas'] == dict(C.CANVAS)
    keys = _keys(edl)
    assert keys == sorted(keys)
    assert {s['clip'] for s in edl['segments']} == {XA, XB, XC}
    last = edl['segments'][-1]
    assert last['role'] == 'reveal' and last['stage'] == C.FINAL_LOOK
    assert _seg_len(last) >= 1.5 - 1e-9
    assert [s['role'] for s in edl['segments'][:-1]] == ['process'] * (len(edl['segments']) - 1)
    assert edl['final_look_found'] is True and edl['hook_first'] is False
    assert C.capture_note('phone') in edl['notes'] and edl['capture_src'] == 'phone'
    assert edl['audio'] is None


def test_the_reveal_holds_near_its_ideal_length_when_there_is_footage_for_it():
    # A 20 s reel aims its reveal at 0.15 x 20 = 3 s; the shots before it
    # must not eat that time down to the 1.5 s minimum.
    analyses, moments = _story()
    last = _plan(analyses, moments)['segments'][-1]
    assert 2.5 - 1e-9 <= _seg_len(last) <= 4.0 + 1e-9
    assert last['speed'] == 1.0


def test_the_reveal_is_the_best_final_look_and_nothing_filmed_after_it_plays():
    analyses, moments = _story(final_appeals=(9.5, 8.0))    # the earlier shot is better
    edl = _plan(analyses, moments)
    assert C.validate_edl(edl) == []
    assert edl['reveal'] == {'clip': XC, 't_s': 2.4}
    last = edl['segments'][-1]
    assert last['clip'] == XC and last['in_s'] <= 2.4 <= last['out_s']
    later = [s for s in edl['segments'] if s['clip'] == XC and s['in_s'] > last['in_s']]
    assert later == []


def test_stage_labels_never_reorder_time():
    a = _analysis(XA, [0, 3, 6, 9, 12], capture=_phone(XA, 0))
    c = _analysis(XC, [0, 6], capture=_phone(XC, 1))
    # The labels run backwards in time inside clip A (a VLM mistake or a
    # touch-up): the story must still play in filming order.
    moments = _moments([_moment(XA, 1.4, 'lips', 8.0), _moment(XA, 4.4, 'eyes', 7.0),
                        _moment(XA, 7.4, 'base', 7.5), _moment(XA, 10.4, 'bare_face', 6.0),
                        _moment(XC, 3.0, 'final_look', 9.0)])
    edl = E.plan_edit(moments, [a, c], _grid(), _style(target_s=10))
    assert C.validate_edl(edl) == []
    keys = _keys(edl)
    assert keys == sorted(keys)


def test_every_cut_sits_on_her_songs_beats():
    analyses, moments = _story()
    beat = 60.0 / 96.0
    grid = BeatGrid(bpm=96.0, times_s=tuple(round(0.21 + k * beat, 6) for k in range(52)),
                    source='local_audio', confidence=0.8, song_key='cluster:7')
    edl = E.plan_edit(moments, analyses, grid, _style())
    assert C.validate_edl(edl) == []
    assert edl['beats']['source'] == 'local_audio' and edl['beats']['song_key'] == 'cluster:7'
    beats = np.asarray(grid.times_s)
    for seg in edl['segments'][:-1]:
        assert np.min(np.abs(beats - seg['out_end_s'])) <= 0.080 + 1e-9


def test_cuts_and_length_fall_on_whole_output_frames():
    analyses, moments = _story()
    edl = _plan(analyses, moments)
    for seg in edl['segments']:
        for t in (seg['out_start_s'], seg['out_end_s']):
            assert abs(t * 30 - round(t * 30)) < 1e-6
    assert abs(edl['duration_s'] - 20.0) <= 1.0


def test_target_is_clamped_to_thirty_seconds():
    analyses, moments = _story()
    edl = E.plan_edit(moments, analyses, _grid(), _style(target_s=45))
    assert edl['target_s'] == 30
    assert abs(edl['duration_s'] - 30) <= 1.0
    assert C.validate_edl(edl) == []


def test_weak_shots_are_dropped_but_each_stage_keeps_its_best_shot():
    bounds = [0, 3, 6, 9, 12, 15, 18, 21, 24, 27]
    a = _analysis(XA, bounds, capture=_phone(XA, 0))
    c = _analysis(XC, [0, 6], capture=_phone(XC, 1))
    items = [_moment(XA, 1.4, 'bare_face', 3.0)]              # weak, but the only "before"
    appeals = [9.0, 2.0, 9.0, 2.0, 9.0, 2.0, 9.0, 2.0]
    items += [_moment(XA, b + 1.4, 'base', ap) for b, ap in zip(bounds[1:-1], appeals)]
    items.append(_moment(XC, 3.0, 'final_look', 9.0))
    edl = E.plan_edit(_moments(items), [a, c], _grid(), _style(target_s=10))
    assert C.validate_edl(edl) == []
    process = [s for s in edl['segments'] if s['role'] == 'process']
    assert any(s['stage'] == 'bare_face' for s in process)
    assert all(s['appeal'] > 2.0 for s in process)


def test_a_short_final_look_is_slowed_to_hold_for_one_and_a_half_seconds():
    a = _analysis(XA, [0, 3, 6, 9, 12], capture=_phone(XA, 0))
    c = _analysis(XC, [0, 1.2], capture=_phone(XC, 1))
    moments = _moments([_moment(XA, 1.4, 'bare_face', 6.0), _moment(XA, 4.4, 'base', 6.0),
                        _moment(XA, 7.4, 'eyes', 6.0), _moment(XA, 10.4, 'lips', 6.0),
                        _moment(XC, 0.6, 'final_look', 9.0)])
    edl = E.plan_edit(moments, [a, c], _grid(), _style(target_s=10))
    assert C.validate_edl(edl) == []
    last = edl['segments'][-1]
    assert last['clip'] == XC and _seg_len(last) >= 1.5 - 1e-9
    assert 0.75 <= last['speed'] < 1.0


# ── the optional teaser ───────────────────────────────────────────────────

def test_hook_first_opens_with_a_final_look_teaser_of_at_most_one_second():
    analyses, moments = _story()
    edl = _plan(analyses, moments, hook_first=True)
    assert C.validate_edl(edl) == []
    first = edl['segments'][0]
    assert edl['hook_first'] is True
    assert first['role'] == 'teaser' and first['stage'] == C.FINAL_LOOK
    assert 0.4 - 1e-9 <= _seg_len(first) <= 1.0 + 1e-9
    assert edl['segments'][-1]['role'] == 'reveal'


def test_without_hook_first_there_is_no_teaser():
    analyses, moments = _story()
    edl = _plan(analyses, moments)
    assert all(s['role'] != 'teaser' for s in edl['segments'])


# ── no final look ─────────────────────────────────────────────────────────

def _no_final_story():
    analyses, moments = _story()
    items = [m for m in moments['moments'] if m['stage'] != C.FINAL_LOOK]
    items += [_moment(XC, 2.4, 'jewellery', 7.0), _moment(XC, 7.4, 'hair', 8.0)]
    return analyses, _moments(items)


def test_no_final_look_plays_the_story_in_order_and_says_so():
    analyses, moments = _no_final_story()
    edl = _plan(analyses, moments)
    assert C.validate_edl(edl) == []
    assert edl['final_look_found'] is False and edl['reveal'] is None
    assert C.NOTE_NO_FINAL_LOOK in edl['notes']
    assert all(s['role'] == 'process' for s in edl['segments'])
    keys = _keys(edl)
    assert keys == sorted(keys)


def test_hook_first_without_a_final_look_skips_the_teaser_with_a_note():
    analyses, moments = _no_final_story()
    edl = _plan(analyses, moments, hook_first=True)
    assert C.validate_edl(edl) == []
    assert edl['hook_first'] is False
    assert E.NOTE_TEASER_SKIPPED in edl['notes']


def test_a_later_final_look_label_does_not_spread_back_to_earlier_footage():
    a = _analysis(XA, [0, 3, 6, 9, 12], capture=_phone(XA, 0))
    c = _analysis(XC, [0, 3, 6], capture=_phone(XC, 1))
    moments = _moments([_moment(XC, 4.4, 'final_look', 9.0)])
    edl = E.plan_edit(moments, [a, c], _grid(), _style(target_s=10))
    assert C.validate_edl(edl) == []
    assert [s['stage'] for s in edl['segments'] if s['clip'] == XA] != []
    assert all(s['stage'] == 'other' for s in edl['segments'] if s['clip'] == XA)
    assert edl['segments'][-1]['stage'] == C.FINAL_LOOK


def test_no_moments_at_all_still_plans_a_reel():
    analyses, _moments_doc = _story()
    edl = E.plan_edit(None, analyses, _grid(), _style())
    assert C.validate_edl(edl) == []
    assert edl['final_look_found'] is False


# ── capture order precedence ──────────────────────────────────────────────

def test_container_creation_time_orders_clips_when_the_phone_sent_none():
    analyses, moments = _story()
    base = T0_MS / 1000.0
    a = _analysis(XA, [0, 3, 6, 9, 12], creation_time=base + 600)    # filmed last
    b = _analysis(XB, [0, 4, 8, 12], creation_time=base)             # filmed first
    c = _analysis(XC, [0, 5, 10], creation_time=base + 300)
    moments = _moments([_moment(XB, 2.0, 'bare_face', 6.0), _moment(XB, 6.0, 'base', 6.0),
                        _moment(XC, 2.4, 'eyes', 7.0), _moment(XC, 7.4, 'lips', 7.0),
                        _moment(XA, 4.4, 'hair', 7.0), _moment(XA, 10.4, 'final_look', 9.0)])
    edl = _plan([a, b, c], moments)
    assert C.validate_edl(edl) == []
    assert edl['capture_src'] == 'container' and C.capture_note('container') in edl['notes']
    order = {x: edl['clips'][x]['order'] for x in edl['clips']}
    assert order[XB] < order[XC] < order[XA]
    assert edl['segments'][-1]['clip'] == XA


def test_upload_order_is_used_when_any_clip_has_no_capture_time():
    base = T0_MS / 1000.0
    a = _analysis(XA, [0, 3, 6, 9, 12], creation_time=base + 600)
    b = _analysis(XB, [0, 4, 8, 12], creation_time=None)
    c = _analysis(XC, [0, 5, 10], creation_time=base)
    moments = _moments([_moment(XC, 7.4, 'final_look', 9.0)])
    edl = _plan([a, b, c], moments)
    assert C.validate_edl(edl) == []
    assert edl['capture_src'] == 'upload_order'
    assert C.capture_note('upload_order') in edl['notes']
    order = {x: edl['clips'][x]['order'] for x in edl['clips']}
    assert order[XA] < order[XB] < order[XC]


# ── footage limits ────────────────────────────────────────────────────────

def test_too_little_footage_is_a_plan_error_naming_what_is_missing():
    a = _analysis(XA, [0, 2, 4], capture=_phone(XA, 0))
    moments = _moments([_moment(XA, 3.0, 'final_look', 9.0)])
    with pytest.raises(PlanError) as err:
        E.plan_edit(moments, [a], _grid(), _style(target_s=20))
    assert err.value.code == 'not_enough_footage'
    assert err.value.data['need_s'] > 0


def test_no_analyses_is_not_enough_footage():
    with pytest.raises(PlanError) as err:
        E.plan_edit(None, [], _grid(), _style())
    assert err.value.code == 'not_enough_footage'


def test_a_clip_too_small_to_fill_the_canvas_is_left_out_with_a_note():
    analyses, moments = _story()
    small = _analysis('d' * 64, [0, 4, 8], size=(1280, 720), capture=_phone('d' * 64, 3))
    analyses[2] = _analysis(XC, [0, 5, 10], capture=_phone(XC, 4))
    edl = _plan(analyses + [small], moments)
    assert C.validate_edl(edl) == []
    assert 'd' * 64 not in edl['clips']
    assert E.NOTE_CLIP_TOO_SMALL in edl['notes']


def test_only_too_small_clips_is_clip_unsupported():
    small = _analysis(XA, [0, 10, 20], size=(1280, 720), capture=_phone(XA, 0))
    with pytest.raises(PlanError) as err:
        E.plan_edit(None, [small], _grid(), _style())
    assert err.value.code == 'clip_unsupported'
    assert err.value.data['xid'] == XA and err.value.data['reason'] == 'too_small'


def test_a_slow_motion_clip_is_conformed_to_thirty_fps_with_a_note():
    analyses, moments = _story()
    analyses[1] = _analysis(XB, [0, 4, 8, 12], fps=240.0, capture=_phone(XB, 1))
    edl = _plan(analyses, moments)
    assert C.validate_edl(edl) == []
    assert edl['clips'][XB]['hfr'] is True
    assert E.NOTE_HFR in edl['notes']


def test_a_beat_grid_without_beats_is_refused():
    analyses, moments = _story()
    empty = BeatGrid(bpm=0.0, times_s=(), source='local_audio', confidence=0.0)
    with pytest.raises(ValueError):
        E.plan_edit(moments, analyses, empty, _style())


# ── face and crop geometry ────────────────────────────────────────────────

def _clip_entry(w, h):
    return {'display_w': w, 'display_h': h}


def _face_in_window(clip, crop, face):
    x0, y0, x1, y1, scale = C.crop_window(clip, crop)
    dw, dh = clip['display_w'], clip['display_h']
    fx0, fy0 = face.x * dw, face.y * dh
    fx1, fy1 = (face.x + face.w) * dw, (face.y + face.h) * dh
    inside = (fx0 >= x0 - 0.5 and fy0 >= y0 - 0.5 and fx1 <= x1 + 0.5 and fy1 <= y1 + 0.5)
    cy = ((fy0 + fy1) / 2.0 - y0) / (y1 - y0)
    return inside, cy, (x0, y0, x1, y1, scale)


def test_a_low_face_in_a_4k_portrait_clip_is_lifted_into_the_upper_band():
    clip = _clip_entry(2160, 3840)
    face = Box(0.40, 0.62, 0.20, 0.16)               # centre at 0.70 of the height
    crop, ok = E.frame_face(clip, face)
    assert ok
    inside, cy, (x0, y0, x1, y1, scale) = _face_in_window(clip, crop, face)
    assert inside and 0.20 <= cy <= 0.55
    assert crop['zoom'] > 1.0 and scale <= 1.8 + 1e-9
    assert x0 >= -0.5 and y0 >= -0.5 and x1 <= 2160.5 and y1 <= 3840.5


def test_a_face_at_the_side_of_a_landscape_clip_stays_in_the_vertical_crop():
    clip = _clip_entry(1920, 1080)
    face = Box(0.74, 0.25, 0.10, 0.18)
    crop, ok = E.frame_face(clip, face)
    assert ok and crop['zoom'] == 1.0
    inside, cy, _ = _face_in_window(clip, crop, face)
    assert inside and 0.20 <= cy <= 0.55


def test_no_face_crops_the_centre():
    crop, ok = E.frame_face(_clip_entry(1080, 1920), None)
    assert ok and crop == {'cx': 0.5, 'cy': 0.5, 'zoom': 1.0}


def test_a_face_that_cannot_be_framed_is_reported():
    # 1080p landscape: the 9:16 window already spans the full height and
    # cannot zoom (1.78x is the upscale cap), so a low face stays low.
    crop, ok = E.frame_face(_clip_entry(1920, 1080), Box(0.45, 0.70, 0.10, 0.15))
    assert not ok


def test_faces_found_by_the_analysis_are_framed_in_the_plan():
    face = Box(0.40, 0.30, 0.20, 0.12)
    a = _analysis(XA, [0, 3, 6, 9, 12], size=(2160, 3840), face=face, capture=_phone(XA, 0))
    c = _analysis(XC, [0, 6], size=(2160, 3840), face=face, capture=_phone(XC, 1))
    moments = _moments([_moment(XA, 1.4, 'bare_face', 6.0), _moment(XA, 7.4, 'eyes', 7.0),
                        _moment(XC, 3.0, 'final_look', 9.0)])
    edl = E.plan_edit(moments, [a, c], _grid(), _style(target_s=10))
    assert C.validate_edl(edl) == []
    for seg in edl['segments']:
        assert seg['face'] == face.to_dict()


def test_an_unframable_face_is_dropped_from_the_edl_with_a_note():
    low = Box(0.45, 0.70, 0.10, 0.15)
    a = _analysis(XA, [0, 3, 6, 9, 12], size=(1920, 1080), face=low, capture=_phone(XA, 0))
    c = _analysis(XC, [0, 6], size=(1920, 1080), face=low, capture=_phone(XC, 1))
    moments = _moments([_moment(XC, 3.0, 'final_look', 9.0)])
    edl = E.plan_edit(moments, [a, c], _grid(), _style(target_s=10))
    assert C.validate_edl(edl) == []
    assert all(s['face'] is None for s in edl['segments'])
    assert E.NOTE_FACE_UNFRAMED in edl['notes']


# ── overlays ──────────────────────────────────────────────────────────────

def test_hook_text_and_logo_sit_in_the_safe_zone(tmp_path):
    logo = str(tmp_path / 'logo.png')
    assert cv2.imwrite(logo, np.full((200, 400, 3), 255, np.uint8))
    analyses, moments = _story()
    edl = E.plan_edit(moments, analyses, _grid(),
                      _style(hook_text='Bridal look in 20s', logo_corner='top_right'),
                      logo_path=logo)
    assert C.validate_edl(edl) == []
    text = [o for o in edl['overlays'] if o['type'] == 'text']
    logos = [o for o in edl['overlays'] if o['type'] == 'logo']
    assert len(text) == 1 and text[0]['text'] == 'Bridal look in 20s'
    assert text[0]['from_s'] == 0.0 and text[0]['to_s'] <= edl['duration_s']
    assert len(logos) == 1 and logos[0]['src'] == logo and logos[0]['corner'] == 'top_right'
    assert logos[0]['width_px'] / logos[0]['height_px'] == pytest.approx(2.0, rel=0.02)


def test_no_logo_when_she_turned_it_off_or_it_cannot_be_read(tmp_path):
    analyses, moments = _story()
    logo = str(tmp_path / 'logo.png')
    cv2.imwrite(logo, np.full((90, 180, 3), 255, np.uint8))
    off = E.plan_edit(moments, analyses, _grid(), _style(use_logo=False), logo_path=logo)
    assert [o for o in off['overlays'] if o['type'] == 'logo'] == []
    bad = tmp_path / 'broken.png'
    bad.write_bytes(b'not an image')
    edl = E.plan_edit(moments, analyses, _grid(), _style(), logo_path=str(bad))
    assert [o for o in edl['overlays'] if o['type'] == 'logo'] == []
    assert E.NOTE_LOGO_UNREADABLE in edl['notes']
    assert C.validate_edl(edl) == []


def test_blank_hook_text_adds_no_text_overlay():
    analyses, moments = _story()
    edl = _plan(analyses, moments, hook_text='   ')
    assert [o for o in edl['overlays'] if o['type'] == 'text'] == []


# ── determinism and input checks ──────────────────────────────────────────

def test_the_same_inputs_give_the_same_edl():
    analyses, moments = _story()
    one = json.dumps(_plan(analyses, moments, hook_first=True), sort_keys=True)
    two = json.dumps(_plan(analyses, moments, hook_first=True), sort_keys=True)
    assert one == two


def test_a_bad_moment_list_is_refused():
    analyses, moments = _story()
    moments['moments'][0]['stage'] = 'contour'
    with pytest.raises(ValueError):
        _plan(analyses, moments)


def test_moments_for_unknown_clips_are_ignored():
    analyses, moments = _story()
    moments['moments'].append(_moment('e' * 64, 1.0, 'final_look', 10.0))
    edl = _plan(analyses, moments)
    assert C.validate_edl(edl) == []
    assert 'e' * 64 not in edl['clips']


# ── with the real analysis ────────────────────────────────────────────────

class _NoFaces:
    def detect(self, bgr):
        return None


@requires_ffmpeg
def test_plan_from_real_analysis_of_synthetic_clips(tmp_path):
    from integrations.reel_studio.media.analyze import analyze_clip
    from integrations.reel_studio.media.keyframes import pick_keyframes
    from integrations.reel_studio.media.probe import probe

    first = make_clip(tmp_path, 'first.mp4', size=(608, 1080), duration_s=3.0,
                      creation_time='2026-10-12T05:31:22Z')
    last = make_clip(tmp_path, 'last.mp4', size=(608, 1080), duration_s=4.0,
                     vf='colorchannelmixer=rr=0.4:gg=0.4:bb=1.0',
                     creation_time='2026-10-12T05:36:22Z')
    analyses = [analyze_clip(probe(p), clip_id=x, detector=_NoFaces())
                for p, x in ((last, XB), (first, XA))]
    keys = pick_keyframes(sorted(analyses, key=lambda a: a.info.creation_time))
    items = [_moment(k.clip, k.t_s, 'base', 6.0) for k in keys]
    items[-1] = _moment(keys[-1].clip, keys[-1].t_s, 'final_look', 9.0)
    edl = E.plan_edit(_moments(items), analyses, synthetic_grid(120.0, 6.0), _style(target_s=5))
    assert C.validate_edl(edl) == []
    assert edl['capture_src'] == 'container'
    assert edl['segments'][-1]['clip'] == XB and edl['segments'][-1]['role'] == 'reveal'
    assert edl['clips'][XA]['path'] == first
