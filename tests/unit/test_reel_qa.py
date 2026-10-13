"""integrations.reel_studio.media.qa: run_qa, the one quality bar.

A hard failure withholds the reel; advisory results are reported and never
fail it.  Each test takes one known-good reel and changes ONE thing, then
checks that exactly the matching hard check fails (or, for an advisory
defect, that the reel still passes and the advisory row says why).

The good reel is cut from one synthetic source (a green-tinted part, then a
red-tinted part, so there is one visible cut at 2.5 s) scaled to 1080x1920:
reel time t shows source time t, so the expected-frame cross-checks
(reveal_render, segment_fidelity) can match it.  The EDL is a 5 s,
two-segment story that contracts.validate_edl accepts: a process shot, then
a 2.5 s final-look reveal, the cut on the 120 BPM grid.
"""
import copy
import os

import numpy as np
import pytest

from core import media_bin
from integrations.reel_studio import contracts as C
from integrations.reel_studio.contracts import ClipAnalysis, ClipCapture, QUALITY_BAR
from integrations.reel_studio.media import qa as Q
from integrations.reel_studio.media.probe import probe
from tests.unit.reel_media_fixtures import concat_clips, make_clip, requires_ffmpeg

X = 'f' * 64
CUT = 2.5


def _ff(args):
    result = media_bin.run_ffmpeg(['-y', '-loglevel', 'error'] + list(args), timeout_s=120)
    assert result.returncode == 0 and not result.timed_out, result.stderr[-1500:]


_BT709 = 'setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv'


def _encode(src, out, *, dur=5.0, vf_extra='', size='1080:1920', faststart=True, extra=(),
            audio=False, rate=None, profile=None):
    vf = 'scale=%s:flags=bicubic,setsar=1' % size
    if vf_extra:
        vf += ',' + vf_extra
    vf += ',' + _BT709 + ',format=yuv420p'
    args = ['-i', src]
    if audio:
        args += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000']
    args += ['-t', '%g' % dur, '-map', '0:v:0'] + (['-map', '1:a:0', '-c:a', 'aac'] if audio else ['-an'])
    args += ['-vf', vf]
    if rate:
        args += ['-r', str(rate)]
    args += ['-c:v', 'libx264', '-preset', 'superfast', '-crf', '23']
    if profile:
        args += ['-profile:v', profile]
    args += list(extra)
    if faststart:
        args += ['-movflags', '+faststart']
    _ff(args + [out])
    return out


@pytest.fixture(scope='module')
def kit(tmp_path_factory):
    if not media_bin.get_ffmpeg():
        pytest.skip('no ffmpeg')
    d = tmp_path_factory.mktemp('qa_kit')
    # Green first (bright: the first-frame luma check reads it), then red,
    # the same tints test_reel_shots uses for a cut the histograms see.
    green = make_clip(d, 'green.mp4', size=(608, 1080), duration_s=CUT,
                      vf='lutrgb=r=val*0.15:b=val*0.15')
    red = make_clip(d, 'red.mp4', size=(608, 1080), duration_s=5.0,
                    vf='lutrgb=g=val*0.15:b=val*0.15')
    src = concat_clips(d, [green, red], 'src.mp4')
    good = _encode(src, str(d / 'good.mp4'))
    return {'dir': d, 'src': src, 'good': good}


def _edl(kit, *, cut=CUT, reveal_t=3.5, overlays=()):
    info = probe(kit['src'])
    clip = info.to_edl_clip(ClipCapture(clip=X, order=0, captured_at_ms=1760247082000,
                                        src='phone'))
    def seg(a, b, stage, role):
        return {'clip': X, 'in_s': a, 'out_s': b, 'speed': 1.0, 'out_start_s': a,
                'out_end_s': b, 'crop': {'cx': 0.5, 'cy': 0.5, 'zoom': 1.0}, 'face': None,
                'transition': {'type': 'cut', 'dur_s': 0.0}, 'stage': stage, 'appeal': 7.0,
                'role': role}
    edl = {'schema': C.EDL_SCHEMA, 'canvas': dict(C.CANVAS), 'target_s': 5.0,
           'duration_s': 5.0, 'audio': None, 'final_look_found': True, 'hook_first': False,
           'capture_src': 'phone', 'reveal': {'clip': X, 't_s': reveal_t},
           'notes': [C.capture_note('phone')],
           'beats': {'source': 'synthetic', 'song_key': None, 'bpm': 120.0,
                     'times_s': [0.5 * k for k in range(13)], 'confidence': 0.0},
           'clips': {X: clip},
           'segments': [seg(0.0, cut, 'base', 'process'), seg(cut, 5.0, 'final_look', 'reveal')],
           'overlays': list(overlays)}
    return edl


def _hard_failures(report):
    return sorted(r['id'] for r in report['checks'] if r['hard'] and not r['ok'])


def _advisory(report, check):
    return next(r for r in report['advisory'] if r['id'] == check)


def _check(report, check):
    return next(r for r in report['checks'] if r['id'] == check)


# ── the good reel ─────────────────────────────────────────────────────────

@requires_ffmpeg
def test_the_good_reel_passes_every_hard_check(kit):
    edl = _edl(kit)
    assert C.validate_edl(edl) == []
    report = Q.run_qa(kit['good'], edl)
    assert C.validate_qa_report(report) == []
    assert report['schema'] == C.QA_SCHEMA
    assert _hard_failures(report) == [] and report['passed'] is True
    ids = {r['id'] for r in report['checks']}
    assert {'resolution', 'codec', 'fps', 'duration', 'audio', 'size_cap', 'faststart',
            'first_frame_luma', 'order', 'reveal', 'beats', 'no_bars', 'face'} <= ids
    assert all(not r['hard'] for r in report['advisory'])
    # Reel time t shows source time t, so the cross-checks agree with the EDL.
    for check in ('reveal_render', 'segment_fidelity', 'beats_render', 'color_tags'):
        assert _advisory(report, check)['ok'], _advisory(report, check)
    assert _check(report, 'fps')['value']['frames'] == 150
    assert C.qa_summary(report) == {'passed': True, 'failed': []}


# ── each file defect fails exactly its own hard check ─────────────────────

_FILE_DEFECTS = {
    'resolution': dict(size='1080:1080'),
    'fps': dict(rate=25),
    'audio': dict(audio=True),
    'duration': dict(dur=6.5),
    'faststart': dict(faststart=False),
    'codec': dict(profile='baseline'),
    'first_frame_luma': dict(vf_extra="drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:"
                                      "enable='lt(n,3)'"),
}


@requires_ffmpeg
@pytest.mark.parametrize('check', sorted(_FILE_DEFECTS))
def test_each_file_defect_fails_exactly_its_own_hard_check(kit, check):
    out = _encode(kit['src'], str(kit['dir'] / ('bad_%s.mp4' % check)), **_FILE_DEFECTS[check])
    report = Q.run_qa(out, _edl(kit))
    assert C.validate_qa_report(report) == []
    assert _hard_failures(report) == [check]
    assert report['passed'] is False
    assert C.qa_summary(report)['failed'] == [check]


@requires_ffmpeg
def test_a_file_over_the_size_cap_fails_size_cap(kit):
    bar = {k: dict(v) for k, v in QUALITY_BAR.items()}
    bar['size_cap']['mbps'] = 0.05                      # a 5 s reel may then be ~32 KB
    bar['size_cap']['slack_bytes'] = 0
    report = Q.run_qa(kit['good'], _edl(kit), bar=bar)
    assert C.validate_qa_report(report, bar=bar) == []
    assert _hard_failures(report) == ['size_cap']


# ── each EDL defect fails exactly its own hard check ──────────────────────

def _cut_off_beat(e):
    for s, (a, b) in zip(e['segments'], ((0.0, 2.3), (2.3, 5.0))):
        s['in_s'], s['out_s'], s['out_start_s'], s['out_end_s'] = a, b, a, b


def _crop_outside(e):
    e['segments'][0]['crop']['cx'] = 0.9


def _wrong_reveal(e):
    e['reveal']['t_s'] = 1.0


def _out_of_order(e):
    first, last = e['segments']
    first['in_s'], first['out_s'] = 2.5, 5.0
    last['in_s'], last['out_s'] = 0.0, 2.5
    e['reveal']['t_s'] = 1.0


@requires_ffmpeg
@pytest.mark.parametrize('defect, check', [
    (_cut_off_beat, 'beats'), (_crop_outside, 'no_bars'), (_wrong_reveal, 'reveal'),
    (_out_of_order, 'order'),
])
def test_each_edl_defect_fails_exactly_its_own_hard_check(kit, defect, check):
    edl = _edl(kit)
    defect(edl)
    report = Q.run_qa(kit['good'], edl)
    assert C.validate_qa_report(report) == []
    assert _hard_failures(report) == [check]


# ── advisory defects are reported but never fail the reel ─────────────────

@requires_ffmpeg
def test_low_bitrate_on_static_footage_is_advisory(kit):
    flat = str(kit['dir'] / 'flat.mp4')
    _ff(['-f', 'lavfi', '-i', 'color=c=gray:size=1080x1920:rate=30', '-t', '5',
         '-vf', _BT709 + ',format=yuv420p', '-c:v', 'libx264', '-preset', 'superfast',
         '-crf', '23', '-movflags', '+faststart', flat])
    report = Q.run_qa(flat, _edl(kit))
    assert report['passed'] is True
    assert _advisory(report, 'bitrate_floor')['ok'] is False


@requires_ffmpeg
def test_dark_bands_like_black_hair_are_advisory(kit):
    banded = _encode(kit['src'], str(kit['dir'] / 'banded.mp4'),
                     vf_extra='drawbox=x=0:y=0:w=iw:h=300:color=black:t=fill,'
                              'drawbox=x=0:y=ih-300:w=iw:h=300:color=black:t=fill')
    report = Q.run_qa(banded, _edl(kit))
    assert report['passed'] is True
    assert _advisory(report, 'no_bars_render')['ok'] is False


@requires_ffmpeg
def test_a_cut_the_render_does_not_show_where_the_edl_says_is_advisory(kit):
    edl = _edl(kit, cut=2.0, reveal_t=3.5)              # the picture changes at 2.5 s
    assert C.validate_edl(edl) == []
    report = Q.run_qa(kit['good'], edl)
    assert report['passed'] is True
    assert _advisory(report, 'beats_render')['ok'] is False


@requires_ffmpeg
def test_the_reveal_cross_check_masks_overlay_regions(kit):
    # A white box where the hook text sits, over the last 2 s: the EDL says
    # a text overlay is there, so the comparison ignores that band.
    text = {'type': 'text', 'text': 'Hook', 'font': 'auto', 'size_px': 72, 'color': '#FFFFFF',
            'box': True, 'x': 0.393519, 'y': 0.18, 'anchor': 'center',
            'from_s': 3.0, 'to_s': 5.0}
    boxed = _encode(kit['src'], str(kit['dir'] / 'boxed.mp4'),
                    vf_extra="drawbox=x=0:y=166:w=iw:h=360:color=white:t=fill:enable='gte(t,3)'")
    edl = _edl(kit, overlays=[text])
    assert C.validate_edl(edl) == []
    report = Q.run_qa(boxed, edl)
    assert report['passed'] is True
    assert _advisory(report, 'reveal_render')['ok'] is True


@requires_ffmpeg
def test_a_reveal_that_shows_the_wrong_footage_is_flagged_but_advisory(kit):
    # The last seconds show the green part again instead of the red reveal.
    wrong = str(kit['dir'] / 'wrong_end.mp4')
    _ff(['-i', kit['src'], '-filter_complex',
         '[0:v]trim=0:2.5,setpts=PTS-STARTPTS[a];[0:v]trim=0:2.5,setpts=PTS-STARTPTS[b];'
         '[a][b]concat=n=2:v=1:a=0,scale=1080:1920,setsar=1,' + _BT709 + ',format=yuv420p[v]',
         '-map', '[v]', '-an', '-c:v', 'libx264', '-preset', 'superfast', '-crf', '23',
         '-movflags', '+faststart', wrong])
    report = Q.run_qa(wrong, _edl(kit))
    assert report['passed'] is True
    assert _advisory(report, 'reveal_render')['ok'] is False
    assert _advisory(report, 'segment_fidelity')['ok'] is False


@requires_ffmpeg
def test_shot_quality_reads_the_analysis_and_stays_advisory(kit):
    info = probe(kit['src'])
    n = 38
    dark = ClipAnalysis(clip=X, info=info, times=np.arange(n) * 0.2, corr=np.ones(n),
                        corr_skip=np.ones(n), sharp=np.full(n, 50.0), luma=np.full(n, 0.1),
                        clipped=np.zeros(n), shifts=np.zeros((n, 2)), faces=[None] * n)
    report = Q.run_qa(kit['good'], _edl(kit), analyses=[dark])
    assert report['passed'] is True
    row = _advisory(report, 'shot_quality')
    assert row['ok'] is False
    without = Q.run_qa(kit['good'], _edl(kit))
    assert all(r['id'] != 'shot_quality' for r in without['advisory'])


# ── broken inputs never raise ─────────────────────────────────────────────

def test_a_file_that_is_not_a_video_fails_without_raising(tmp_path):
    junk = tmp_path / 'junk.mp4'
    junk.write_bytes(b'this is not a video' * 100)
    edl = copy.deepcopy(_golden())
    report = Q.run_qa(str(junk), edl)
    assert C.validate_qa_report(report) == []
    assert report['passed'] is False
    assert _check(report, 'resolution')['ok'] is False


def test_a_missing_file_fails_without_raising(tmp_path):
    report = Q.run_qa(str(tmp_path / 'nope.mp4'), _golden())
    assert C.validate_qa_report(report) == []
    assert report['passed'] is False


def test_a_structurally_broken_edl_fails_schema(tmp_path):
    junk = tmp_path / 'junk.mp4'
    junk.write_bytes(b'x')
    report = Q.run_qa(str(junk), {'schema': 'nope'})
    assert C.validate_qa_report(report) == []
    assert 'schema' in _hard_failures(report)


def _golden():
    from tests.unit.reel_media_fixtures import golden_edl
    return golden_edl()


# ── faststart reading ─────────────────────────────────────────────────────

def _box(kind, payload=b''):
    return (8 + len(payload)).to_bytes(4, 'big') + kind + payload


def test_top_level_boxes_are_read_in_file_order(tmp_path):
    path = tmp_path / 'boxes.mp4'
    path.write_bytes(_box(b'ftyp', b'isom') + _box(b'moov', b'\0' * 20) + _box(b'mdat', b'\1' * 50))
    assert Q.mp4_top_level_boxes(str(path)) == ['ftyp', 'moov', 'mdat']
    path.write_bytes(_box(b'ftyp', b'isom') + _box(b'mdat', b'\1' * 50) + _box(b'moov', b'\0' * 20))
    assert Q.mp4_top_level_boxes(str(path)) == ['ftyp', 'mdat', 'moov']


def test_a_64_bit_box_size_is_followed(tmp_path):
    path = tmp_path / 'large.mp4'
    big = (1).to_bytes(4, 'big') + b'mdat' + (16 + 40).to_bytes(8, 'big') + b'\1' * 40
    path.write_bytes(_box(b'ftyp', b'isom') + big + _box(b'moov'))
    assert Q.mp4_top_level_boxes(str(path)) == ['ftyp', 'mdat', 'moov']
