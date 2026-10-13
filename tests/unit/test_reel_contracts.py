"""integrations.reel_studio.contracts: the shared Reel Studio contracts (WS-0).

Every workstream builds against these schemas and constants, so each defect
the plan names must be refused by its OWN check id (that id is what
qa.run_qa reports), and the good shapes must be accepted.

Story order follows the user's amendment A1: clips play in capture-time
order, the reel ends on the final-look reveal, and a <= 1 s final-look
teaser at the start is optional (hook_first).  The old "the first second is
the final look" hook rule is gone.
"""
import copy
import os
import sys
import types

import pytest

from integrations.reel_studio import contracts as C
from tests.unit.reel_media_fixtures import XID_A, XID_B, golden_edl

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHA = 'c' * 64


def _checks(edl, **kw):
    return set(C.failed_checks(C.validate_edl(edl, **kw)))


def _with_teaser(edl, length=0.5):
    """Prepend a final-look teaser cut from the reveal clip; shift the rest."""
    for seg in edl['segments']:
        seg['out_start_s'] = round(seg['out_start_s'] + length, 6)
        seg['out_end_s'] = round(seg['out_end_s'] + length, 6)
    teaser = copy.deepcopy(edl['segments'][-1])
    teaser.update(in_s=8.0, out_s=8.0 + length, out_start_s=0.0, out_end_s=length,
                  role='teaser')
    edl['segments'].insert(0, teaser)
    edl['duration_s'] = round(edl['duration_s'] + length, 6)
    edl['hook_first'] = True
    return edl


def _without_final_look(edl):
    edl['final_look_found'] = False
    edl['reveal'] = None
    edl['segments'][-1].update(stage='jewellery', role='process')
    edl['notes'].append(C.NOTE_NO_FINAL_LOOK)
    return edl


# ── the EDL: accepted shapes ──────────────────────────────────────────────

def test_the_golden_edl_is_valid():
    assert C.validate_edl(golden_edl()) == []


def test_no_final_look_is_valid_when_the_edl_says_so():
    assert C.validate_edl(_without_final_look(golden_edl())) == []


def test_the_optional_teaser_is_valid():
    assert C.validate_edl(_with_teaser(golden_edl())) == []


def test_a_speed_ramped_segment_is_valid():
    edl = golden_edl()
    edl['segments'][0].update(in_s=1.0, out_s=5.5, speed=1.5)
    assert C.validate_edl(edl) == []


# ── the EDL: each defect fails its own check ──────────────────────────────

def _gap(e):
    e['segments'][2].update(in_s=12.2, out_start_s=7.2)


def _cut_120ms_off_beat(e):
    e['segments'][0].update(out_s=4.12, out_end_s=3.12)
    e['segments'][1].update(in_s=6.12, out_start_s=3.12)


def _duration_2s_off(e):
    e['target_s'] = 22.0


def _target_45(e):
    e['target_s'] = 45.0


def _duration_not_the_last_cut(e):
    e['duration_s'] = 19.5


def _reveal_not_final_look(e):
    e['segments'][-1]['stage'] = 'jewellery'


def _reveal_too_short(e):
    e['segments'][-1].update(out_s=7.0, out_end_s=16.0)
    e['duration_s'] = 16.0
    e['target_s'] = 16.0


def _reveal_points_elsewhere(e):
    e['reveal'] = {'clip': XID_B, 't_s': 2.0}


def _reveal_names_the_other_clip(e):
    e['reveal'] = {'clip': XID_A, 't_s': 8.0}


def _clips_out_of_capture_order(e):
    e['clips'][XID_A]['order'] = 1
    e['clips'][XID_B]['order'] = 0


def _segments_out_of_time_within_a_clip(e):
    e['segments'][1].update(in_s=12.0, out_s=16.0)
    e['segments'][2].update(in_s=6.0, out_s=10.0)


def _capture_source_not_noted(e):
    e['notes'] = []


def _overlay_at_y_100px(e):
    e['overlays'][0]['y'] = 100 / 1920


def _overlay_outlives_the_reel(e):
    e['overlays'][0]['to_s'] = 25.0


def _logo_too_wide_for_the_safe_zone(e):
    e['overlays'][1]['width_px'] = 900


def _zoom_0_9(e):
    e['segments'][0]['crop']['zoom'] = 0.9


def _crop_window_outside_the_source(e):
    e['segments'][3]['crop']['cx'] = 0.9
    e['segments'][3]['face'] = None


def _speed_3(e):
    e['segments'][0].update(in_s=1.0, out_s=10.0, speed=3.0)


def _upscale_over_1_8(e):
    e['segments'][3]['crop']['zoom'] = 1.1


def _shot_under_0_4s(e):
    e['beats']['times_s'] = sorted(e['beats']['times_s'] + [0.3])
    e['segments'][0].update(out_s=1.3, out_end_s=0.3)
    e['segments'][1].update(in_s=6.0, out_s=12.7, out_start_s=0.3)


def _shot_past_the_end_of_its_clip(e):
    e['segments'][-1].update(in_s=16.0, out_s=21.0)
    e['reveal'] = {'clip': XID_B, 't_s': 18.0}


def _face_below_the_band(e):
    e['segments'][0]['face'] = {'x': 0.4, 'y': 0.6, 'w': 0.2, 'h': 0.15}


def _crossfade(e):
    e['segments'][1]['transition'] = {'type': 'xfade', 'dur_s': 0.5}


def _audio_track(e):
    e['audio'] = {'file': 'song.m4a', 'start_s': 38.5}


DEFECTS = [
    (_gap, 'contiguous'),
    (_cut_120ms_off_beat, 'beats'),
    (_duration_2s_off, 'duration'),
    (_target_45, 'duration'),
    (_duration_not_the_last_cut, 'contiguous'),
    (_reveal_not_final_look, 'reveal'),
    (_reveal_too_short, 'reveal'),
    (_reveal_points_elsewhere, 'reveal'),
    (_reveal_names_the_other_clip, 'reveal'),
    (_clips_out_of_capture_order, 'order'),
    (_segments_out_of_time_within_a_clip, 'order'),
    (_capture_source_not_noted, 'order'),
    (_overlay_at_y_100px, 'overlays'),
    (_overlay_outlives_the_reel, 'overlays'),
    (_logo_too_wide_for_the_safe_zone, 'overlays'),
    (_zoom_0_9, 'no_bars'),
    (_crop_window_outside_the_source, 'no_bars'),
    (_speed_3, 'shot_rules'),
    (_upscale_over_1_8, 'shot_rules'),
    (_shot_under_0_4s, 'shot_rules'),
    (_shot_past_the_end_of_its_clip, 'shot_rules'),
    (_face_below_the_band, 'face'),
    (_crossfade, 'transition'),
    (_audio_track, 'audio'),
]


@pytest.mark.parametrize('defect, check', DEFECTS, ids=[d.__name__.strip('_') for d, _ in DEFECTS])
def test_each_defect_fails_exactly_its_own_check(defect, check):
    edl = golden_edl()
    defect(edl)
    assert _checks(edl) == {check}


def test_no_final_look_must_be_noted():
    edl = _without_final_look(golden_edl())
    edl['notes'].remove(C.NOTE_NO_FINAL_LOOK)
    assert _checks(edl) == {'reveal'}


def test_a_reveal_role_needs_a_final_look():
    edl = _without_final_look(golden_edl())
    edl['segments'][-1]['role'] = 'reveal'
    assert _checks(edl) == {'reveal'}


def test_a_teaser_over_one_second_is_refused():
    edl = _with_teaser(golden_edl(), length=1.5)
    edl['target_s'] = 21.0
    assert _checks(edl) == {'teaser'}


def test_a_teaser_needs_hook_first():
    edl = _with_teaser(golden_edl())
    edl['hook_first'] = False
    assert _checks(edl) == {'teaser'}


def test_hook_first_needs_its_teaser():
    edl = golden_edl()
    edl['hook_first'] = True
    assert _checks(edl) == {'teaser'}


def test_a_teaser_needs_a_final_look():
    edl = _without_final_look(_with_teaser(golden_edl()))
    assert _checks(edl) == {'teaser'}


def test_the_teaser_is_outside_the_time_order():
    """The teaser shows the end at the start; that alone is not an order fault."""
    edl = _with_teaser(golden_edl())
    assert 'order' not in _checks(edl)


@pytest.mark.parametrize('bad', [None, [], 'edl', 7, {'schema': 'hart.reel.edl.v1'}])
def test_garbage_is_a_schema_problem_never_an_exception(bad):
    problems = C.validate_edl(bad)
    assert problems and set(C.failed_checks(problems)) == {'schema'}


def test_structural_faults_report_only_schema():
    for mutate in (lambda e: e.pop('segments'),
                   lambda e: e['segments'][2].update(clip='d' * 64),
                   lambda e: e.update(canvas={'w': 1080, 'h': 1080, 'fps': 30}),
                   lambda e: e.update(schema='hart.reel.edl.v0'),
                   lambda e: e['segments'][0].update(stage='contour'),
                   lambda e: e['clips'][XID_A].update(transfer='dolby'),
                   lambda e: e.pop('hook_first')):
        edl = golden_edl()
        mutate(edl)
        assert _checks(edl) == {'schema'}


def test_thresholds_come_from_the_bar_passed_in():
    bar = dict(C.QUALITY_BAR)
    bar['shot_rules'] = dict(C.QUALITY_BAR['shot_rules'], min_shot_s=3.5)
    assert _checks(golden_edl(), bar=bar) == {'shot_rules'}
    assert _checks(golden_edl()) == set()


def test_problems_name_their_check_and_say_what_is_wrong():
    edl = golden_edl()
    _cut_120ms_off_beat(edl)
    [problem] = C.validate_edl(edl)
    check, _, detail = problem.partition(': ')
    assert check == 'beats' and '3.12' in detail


# ── the quality bar (A1 replaces the hook check) ──────────────────────────

def test_the_quality_bar_carries_the_a1_story_checks():
    bar = C.QUALITY_BAR
    assert 'hook' not in bar
    assert bar['order']['hard'] is True
    assert bar['reveal']['hard'] is True and bar['reveal']['min_hold_s'] == 1.5
    assert bar['reveal_render']['hard'] is False
    assert bar['reveal_render']['from_end_s'] == 0.75
    assert bar['reveal_render']['min_corr'] == 0.85
    assert bar['first_frame_luma'] == {'hard': True, 'at_s': 0.04, 'min_luma': 0.12}
    assert bar['teaser']['max_s'] == 1.0


def test_the_quality_bar_keeps_the_plan_thresholds():
    bar = C.QUALITY_BAR
    assert bar['beats']['tol_s'] == 0.080 and bar['beats']['hard'] is True
    assert bar['beats_render']['hard'] is False
    assert (bar['face']['cy_min'], bar['face']['cy_max']) == (0.20, 0.55)
    rules = bar['shot_rules']
    assert (rules['min_shot_s'], rules['max_upscale']) == (0.4, 1.8)
    assert (rules['speed_min'], rules['speed_max']) == (0.75, 2.0)
    assert bar['duration']['tol_s'] == 1.0
    assert bar['size_cap']['hard'] is True and bar['size_cap']['mbps'] == 14
    assert bar['bitrate_floor']['hard'] is False
    assert bar['no_bars']['hard'] is True and bar['no_bars_render']['hard'] is False
    assert bar['audio']['hard'] is True and bar['audio']['allow_audio'] is False
    assert bar['resolution']['w'] == 1080 and bar['resolution']['h'] == 1920
    assert bar['fps']['fps'] == 30


def test_every_edl_check_has_a_row_in_the_bar():
    assert set(C.EDL_CHECKS) <= set(C.QUALITY_BAR)
    assert all(C.QUALITY_BAR[c]['hard'] is True for c in C.EDL_CHECKS)


def test_the_bar_cannot_be_edited_in_place():
    with pytest.raises(TypeError):
        C.QUALITY_BAR['beats'] = {'hard': False}
    with pytest.raises(TypeError):
        C.QUALITY_BAR['beats']['tol_s'] = 1.0


# ── capture order (A1) ────────────────────────────────────────────────────

T0 = 1760247082000          # 2025-10-12, epoch ms


def _order(clips):
    caps, src = C.resolve_capture_order(clips)
    return [c.clip for c in caps], src, caps


def test_the_phones_capture_time_wins():
    ids, src, caps = _order([
        {'clip': 'x', 'captured_at_ms': T0 + 60000, 'creation_time': (T0 - 999000) / 1000},
        {'clip': 'y', 'captured_at_ms': T0, 'creation_time': (T0 + 999000) / 1000},
    ])
    assert ids == ['y', 'x'] and src == 'phone'
    assert [c.order for c in caps] == [0, 1]
    assert [c.captured_at_ms for c in caps] == [T0, T0 + 60000]
    assert {c.src for c in caps} == {'phone'}


def test_container_time_is_the_fallback():
    ids, src, caps = _order([
        {'clip': 'x', 'captured_at_ms': None, 'creation_time': (T0 + 5000) / 1000},
        {'clip': 'y', 'captured_at_ms': None, 'creation_time': T0 / 1000},
    ])
    assert ids == ['y', 'x'] and src == 'container'
    assert caps[0].captured_at_ms == T0


def test_phone_and_container_times_mix():
    ids, src, _ = _order([
        {'clip': 'x', 'captured_at_ms': T0 + 5000, 'creation_time': None},
        {'clip': 'y', 'captured_at_ms': None, 'creation_time': T0 / 1000},
    ])
    assert ids == ['y', 'x'] and src == 'phone+container'


def test_upload_order_when_any_clip_has_no_time():
    ids, src, caps = _order([
        {'clip': 'x', 'captured_at_ms': T0 + 5000, 'creation_time': None},
        {'clip': 'y', 'captured_at_ms': None, 'creation_time': None},
        {'clip': 'z', 'captured_at_ms': T0, 'creation_time': None},
    ])
    assert ids == ['x', 'y', 'z'] and src == 'upload_order'
    assert {c.src for c in caps} == {'upload_order'}


def test_implausible_times_count_as_missing():
    """0 and the QuickTime 1904 epoch are 'no clock', not 'filmed first'."""
    ids, src, _ = _order([
        {'clip': 'x', 'captured_at_ms': 0, 'creation_time': -2082844800.0},
        {'clip': 'y', 'captured_at_ms': True, 'creation_time': 0.0},
    ])
    assert ids == ['x', 'y'] and src == 'upload_order'


def test_equal_times_keep_upload_order():
    ids, _, _ = _order([{'clip': c, 'captured_at_ms': T0, 'creation_time': None}
                        for c in ('p', 'q', 'r')])
    assert ids == ['p', 'q', 'r']


def test_a_clip_listed_twice_is_refused():
    with pytest.raises(ValueError):
        C.resolve_capture_order([{'clip': 'x'}, {'clip': 'x'}])


def test_the_capture_note_names_the_source():
    assert C.capture_note('phone') == 'capture_order:phone'


# ── dataclasses feed the EDL with the names the validator reads ───────────

def _info(**kw):
    base = dict(path='C:/reel/work/a.mp4', duration_s=30.0, width=1920, height=1080,
                display_w=1080, display_h=1920, rotation=90, fps_avg=29.97, codec='hevc')
    base.update(kw)
    return C.ClipInfo(**base)


def test_clip_info_and_capture_make_a_valid_edl_clip_entry():
    edl = golden_edl()
    cap = C.ClipCapture(clip=XID_A, order=0, captured_at_ms=1760247082000, src='phone')
    edl['clips'][XID_A] = _info().to_edl_clip(cap)
    assert C.validate_edl(edl) == []


def test_beat_grid_makes_a_valid_edl_beats_entry():
    edl = golden_edl()
    grid = C.BeatGrid(bpm=120.0, times_s=tuple(0.5 * k for k in range(43)),
                      source='synthetic', confidence=0.0)
    edl['beats'] = grid.to_edl()
    assert C.validate_edl(edl) == []


def test_clip_source_reads_the_phones_capture_time_from_the_store_record():
    record = types.SimpleNamespace(
        xid=SHA, path='C:/data/user_files/u/in/' + SHA, name='VID_20261012.mp4',
        mime='video/mp4', size=1234, sha256=SHA, created_at=1760247999.5,
        meta={'captured_at': 1760247082000, 'duration_ms': 15000})
    src = C.ClipSource.from_stored(record)
    assert (src.xid, src.captured_at_ms, src.duration_ms) == (SHA, 1760247082000, 15000)
    assert src.committed_at == 1760247999.5
    as_dict = C.ClipSource.from_stored(dict(vars(record)))
    assert as_dict == src
    bare = C.ClipSource.from_stored(dict(vars(record), meta={}))
    assert bare.captured_at_ms is None and bare.duration_ms is None


def test_value_types_refuse_impossible_values():
    with pytest.raises(ValueError):
        C.Moment(clip='x', t_s=1.0, stage='contour', appeal=5, face_visible=True, src='vlm')
    with pytest.raises(ValueError):
        C.Moment(clip='x', t_s=1.0, stage='lips', appeal=11, face_visible=True, src='vlm')
    with pytest.raises(ValueError):
        C.Moment(clip='x', t_s=1.0, stage='lips', appeal=5, face_visible=True, src='guess')
    with pytest.raises(ValueError):
        C.Box(x=0.1, y=0.1, w=-0.2, h=0.1)
    with pytest.raises(ValueError):
        _info(transfer='dolby')
    box = C.Box(x=0.4, y=0.2, w=0.2, h=0.1)
    assert (box.cx, box.cy) == (pytest.approx(0.5), pytest.approx(0.25))


def test_errors_carry_their_code():
    err = C.ClipUnsupported('dolby_vision_p5', 'profile 5 has no SDR/HLG base layer')
    assert err.reason == 'dolby_vision_p5' and 'dolby_vision_p5' in str(err)
    plan = C.PlanError('not_enough_footage', need_s=12.0)
    assert plan.code == 'not_enough_footage' and plan.data == {'need_s': 12.0}


# ── moments ───────────────────────────────────────────────────────────────

def _moments():
    return {'schema': 'hart.reel.moments.v1', 'src': 'mixed', 'model': 'qwen3.5-0.8b',
            'final_look_found': True, 'notes': [],
            'moments': [
                C.Moment(clip=XID_A, t_s=2.0, stage='bare_face', appeal=4.0,
                         face_visible=True, src='vlm').to_dict(),
                C.Moment(clip=XID_B, t_s=8.0, stage='final_look', appeal=9.0,
                         face_visible=True, src='fallback').to_dict()]}


def test_a_moment_list_is_valid():
    assert C.validate_moments(_moments()) == []


def test_moment_list_flags_must_match_its_moments():
    doc = _moments()
    doc['final_look_found'] = False
    assert C.validate_moments(doc)
    doc = _moments()
    doc['src'] = 'vlm'
    assert C.validate_moments(doc)


# ── reply media (what the phone renders) ──────────────────────────────────

def _media(**kw):
    item = {'kind': 'video', 'xid': SHA, 'sha256': SHA, 'mime': 'video/mp4',
            'size': 24567890, 'width': 1080, 'height': 1920, 'duration_s': 20.0,
            'has_audio': False, 'reel_id': '7f1c2a9e-0000-4000-8000-000000000000',
            'prompt_id': '1234', 'request_id': 'rq-12345678',
            'caption': 'Bridal glow', 'hashtags': ['#bridalmakeup', '#chennaimua'],
            'song': {'song_key': 'cluster:123', 'title': 'Song', 'artist': 'Artist',
                     'lang': 'ta', 'start_ms': 38500, 'start_label': '0:38',
                     'beats': 'synthetic'},
            'notes': ['capture_order:phone'], 'qa': {'passed': True, 'failed': []}}
    item.update(kw)
    return item


def test_a_reply_media_item_is_valid():
    assert C.validate_media_item(_media()) == []
    assert C.validate_media_item(_media(song=None, request_id=None)) == []


@pytest.mark.parametrize('change', [
    {'qa': {'passed': False, 'failed': ['fps']}},
    {'xid': 'd' * 64},
    {'sha256': 'not-hex', 'xid': 'not-hex'},
    {'hashtags': ['#a', '#b', '#c', '#d', '#e', '#f']},
    {'hashtags': ['bridal']},
    {'has_audio': True},
    {'prompt_id': 1234},
    {'mime': 'video/quicktime'},
    {'width': 1080, 'height': 1080},
    {'kind': 'image'},
    {'size': 0},
    {'song': {'song_key': None, 'title': 'S', 'artist': 'A', 'lang': None,
              'start_ms': 65000, 'start_label': '0:65', 'beats': 'synthetic'}},
])
def test_a_reply_media_item_with_a_defect_is_refused(change):
    assert C.validate_media_item(_media(**change))


def test_an_outbox_listing_item_carries_its_media():
    item = {'xid': SHA, 'name': 'reel_x.mp4', 'mime': 'video/mp4', 'size': 24567890,
            'sha256': SHA, 'created_at': 1760248000.0, 'fetched': False,
            'meta': {'prompt_id': '1234', 'request_id': 'rq-12345678', 'media': _media()}}
    assert C.validate_outbox_item(item) == []
    bad = copy.deepcopy(item)
    bad['meta']['media']['xid'] = bad['meta']['media']['sha256'] = 'e' * 64
    assert C.validate_outbox_item(bad)
    bad = dict(item, fetched='no')
    assert C.validate_outbox_item(bad)


# ── inbound files: offer frames and chat attachments (A1, A5) ─────────────

def _offer(**kw):
    frame = {'type': 'file_offer', 'name': 'VID_20261012_101500.mp4', 'size': 123456789,
             'sha256': SHA, 'mime': 'video/mp4', 'purpose': 'reel_source',
             'captured_at': 1760247082000, 'duration_ms': 15000}
    frame.update(kw)
    return frame


def test_a_file_offer_carries_the_phones_capture_time():
    assert C.validate_file_offer(_offer()) == []
    assert 'captured_at' in C.INBOUND_META_KEYS
    older_phone = _offer()
    del older_phone['captured_at']
    assert C.validate_file_offer(older_phone) == []


@pytest.mark.parametrize('change', [
    {'name': '../escape.mp4'}, {'name': 'a/b.mp4'}, {'name': 'a\\b.mp4'}, {'name': ''},
    {'purpose': 'logo'},                       # video/mp4 is not a logo type
    {'purpose': 'reel'},                       # outbound only
    {'size': 0}, {'sha256': SHA[:63]}, {'sha256': SHA.upper()},
    {'captured_at': '2026-10-12'}, {'captured_at': 0}, {'captured_at': True},
    {'captured_at': 1760247082000.5}, {'duration_ms': -1}, {'type': 'file_put'},
])
def test_a_bad_file_offer_is_refused(change):
    assert C.validate_file_offer(_offer(**change))


def test_each_purpose_has_its_own_mime_list():
    assert C.validate_file_offer(_offer(purpose='logo', mime='image/png', name='logo.png')) == []
    assert C.validate_file_offer(_offer(purpose='song_audio', mime='audio/mpeg',
                                        name='song.mp3')) == []
    assert C.validate_file_offer(_offer(purpose='reel_source', mime='image/png'))


def _attachment(**kw):
    item = {'kind': 'peerlink_file', 'ref': SHA, 'purpose': 'reel_source',
            'name': 'VID_20261012.mp4', 'mime': 'video/mp4', 'size': 123456789,
            'duration_ms': 15000, 'captured_at': 1760247082000}
    item.update(kw)
    return item


def test_a_chat_attachment_is_valid_with_or_without_capture_time():
    assert C.validate_attachment(_attachment()) == []
    item = _attachment()
    del item['captured_at'], item['duration_ms']
    assert C.validate_attachment(item) == []


@pytest.mark.parametrize('change', [
    {'ref': SHA.upper()}, {'ref': 'abc'}, {'kind': 'file_url'}, {'purpose': 'reel'},
    {'mime': 'application/pdf'}, {'size': -1}, {'captured_at': 'yesterday'},
])
def test_a_bad_chat_attachment_is_refused(change):
    assert C.validate_attachment(_attachment(**change))


# ── trending songs ────────────────────────────────────────────────────────

def _published():
    return {'schema': 'hart.trending_songs.v1', 'date': '2026-10-13',
            'generated_at': '2026-10-13T03:00:00Z', 'region': 'IN', 'niche': 'makeup',
            'source': 'instagrapi', 'window_days': 7, 'stale': False,
            'accounts_ok': 19, 'accounts_total': 20,
            'dropped': {'should_mute': 3, 'no_music': 7},
            'songs': [{'song_key': 'cluster:123', 'audio_cluster_id': '123',
                       'title': 'Song', 'artist': 'Artist', 'is_original_audio': False,
                       'is_explicit': False, 'duration_ms': 180000,
                       'highlight_start_ms': [38500], 'suggested_start_ms': 38500,
                       'language_hint': 'ta', 'score': 4.2, 'accounts': 3, 'reels': 4,
                       'max_outlier': 6.1, 'total_plays': 1234567,
                       'first_seen': '2026-10-10', 'last_seen': '2026-10-13',
                       'examples': [{'code': 'Cxyz', 'plays': 120000, 'outlier': 6.1}]}]}


def _client():
    return {'schema': 'hart.trending_songs.client.v1', 'fetched_at': '2026-10-13T04:00:00Z',
            'generated_at': '2026-10-13T03:00:00Z', 'stale': False, 'reason': None,
            'songs': [{'song_key': 'cluster:123', 'title': 'Song', 'artist': 'Artist',
                       'lang': 'ta', 'suggested_start_ms': 38500, 'start_label': '0:38',
                       'score': 4.2}]}


def test_trending_lists_are_valid():
    assert C.validate_trending_songs(_published()) == []
    assert C.validate_trending_songs(_client()) == []


def test_trending_lists_carry_no_urls_and_no_session_data():
    doc = _published()
    doc['songs'][0]['preview_url'] = 'x'
    assert C.validate_trending_songs(doc)
    doc = _published()
    doc['songs'][0]['examples'][0]['code'] = 'https://www.instagram.com/reel/Cxyz'
    assert C.validate_trending_songs(doc)
    doc = _client()
    doc['session'] = {'sessionid': 'secret'}
    assert C.validate_trending_songs(doc)


def test_malformed_trending_lists_are_refused():
    doc = _published()
    doc['date'] = '13/10/2026'
    assert C.validate_trending_songs(doc)
    doc = _client()
    doc['songs'][0]['start_label'] = '0:39'
    assert C.validate_trending_songs(doc)
    assert C.validate_trending_songs({'schema': 'hart.trending_songs.v9'})


# ── style, QA report, helpers ─────────────────────────────────────────────

def test_style_defaults_are_valid_and_chronological():
    assert C.validate_style(dict(C.STYLE_DEFAULTS)) == []
    assert C.STYLE_DEFAULTS['hook_first'] is False


def test_make_style_clamps_the_target_and_refuses_unknown_keys():
    assert C.make_style({'target_s': 45})['target_s'] == 30
    assert C.make_style({'hook_first': True})['hook_first'] is True
    with pytest.raises(ValueError):
        C.make_style({'open_with': 'final_look'})


@pytest.mark.parametrize('change', [
    {'hook_first': 'yes'}, {'language': 'fr'}, {'logo_corner': 'bottom_right'},
    {'pace': 'frantic'}, {'target_s': 45}, {'open_with': 'final_look'},
])
def test_a_bad_style_is_refused(change):
    assert C.validate_style(dict(C.STYLE_DEFAULTS, **change))


def _qa(**kw):
    report = {'schema': 'hart.reel.qa.v1', 'passed': True,
              'checks': [{'id': 'fps', 'ok': True, 'value': 30, 'want': 30, 'hard': True},
                         {'id': 'order', 'ok': True, 'value': None, 'want': None, 'hard': True}],
              'advisory': [{'id': 'bitrate_floor', 'ok': False, 'value': 3.1, 'want': 4.0,
                            'hard': False}]}
    report.update(kw)
    return report


def test_a_qa_report_is_valid_and_summarises():
    assert C.validate_qa_report(_qa()) == []
    assert C.qa_summary(_qa()) == {'passed': True, 'failed': []}


def test_a_qa_report_cannot_pass_a_failed_hard_check():
    failed = _qa()
    failed['checks'][0]['ok'] = False
    assert C.validate_qa_report(failed)
    failed['passed'] = False
    assert C.validate_qa_report(failed) == []
    assert C.qa_summary(failed) == {'passed': False, 'failed': ['fps']}


def test_a_qa_report_cannot_soften_a_hard_check_or_invent_one():
    soft = _qa()
    soft['checks'][0]['hard'] = False
    assert C.validate_qa_report(soft)
    unknown = _qa()
    unknown['checks'].append({'id': 'hook', 'ok': True, 'value': 1, 'want': 1, 'hard': True})
    assert C.validate_qa_report(unknown)


@pytest.mark.parametrize('value, want', [
    (45, 30), (2, 5), (20.4, 20.4), (None, 20), ('abc', 20), (float('nan'), 20), (True, 20),
])
def test_clamp_target_s(value, want):
    assert C.clamp_target_s(value) == want


@pytest.mark.parametrize('ms, label', [(0, '0:00'), (38500, '0:38'), (65000, '1:05'),
                                       (600000, '10:00')])
def test_start_label(ms, label):
    assert C.start_label(ms) == label


@pytest.mark.parametrize('corner', C.LOGO_CORNERS)
def test_logos_sit_inside_the_safe_zone(corner):
    overlay = {'type': 'logo', 'corner': corner, 'width_px': 180, 'height_px': 90,
               'margin_px': 48}
    x0, y0, x1, y1 = C.logo_box(overlay)
    zone = C.SAFE_ZONE
    assert zone['x_min'] <= x0 < x1 <= zone['x_max']
    assert zone['y_min'] <= y0 < y1 <= zone['y_max']
    assert (x1 - x0, y1 - y0) == (180, 90)


def test_crop_window_covers_the_canvas_from_the_display_frame():
    clip = golden_edl()['clips'][XID_B]
    x0, y0, x1, y1, scale = C.crop_window(clip, {'cx': 0.5, 'cy': 0.5, 'zoom': 1.0})
    assert scale == pytest.approx(1920 / 1080)
    assert (x1 - x0) * scale == pytest.approx(1080)
    assert (y1 - y0) * scale == pytest.approx(1920)
    assert (y0, y1) == (pytest.approx(0.0), pytest.approx(1080.0))


def test_importing_the_contracts_stays_cheap():
    """tools.py imports these at agent construction; no cv2 may come with them."""
    from core.subprocess_safe import run_bounded
    code = ('import sys; import integrations.reel_studio.contracts; '
            'print("cv2" in sys.modules, "imageio_ffmpeg" in sys.modules)')
    result = run_bounded([sys.executable, '-c', code], timeout=300, cwd=ROOT)
    assert not result.timed_out, 'import took longer than 300 s'
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.split()[-2:] == ['False', 'False']
