"""integrations.reel_studio.media.render (+ fonts): the two-pass ffmpeg render.

Pass 1 renders each EDL segment on its own, one ffmpeg process at a time,
each with exactly one input: scale to cover -> (HDR) tone-map -> crop ->
setpts (speed) -> fps=30 LAST, so a speed-ramped segment is still 30 fps
CFR with an exact frame count.  Pass 2 joins the segments with the concat
demuxer, draws her hook text from a UTF-8 ``textfile=`` with an escaped
``fontfile``, lays the logo in its corner and encodes with CRF/CQ under a
maxrate (never a fixed bitrate), silent, BT.709, faststart.

The full-size test renders three synthetic clips (landscape, rotated
portrait with an audio track, 60 fps) at 1080x1920 and runs every hard QA
check on the result.  The other ffmpeg tests render at a 216x384 canvas
(the ``canvas`` override) from tiny sources to keep the shared pytest slot
short; the production path always renders contracts.CANVAS.
"""
import os
import re
import sys

import numpy as np
import pytest

from core import media_bin
from core.subprocess_safe import BoundedResult
from integrations.reel_studio import contracts as C
from integrations.reel_studio.contracts import ClipCapture
from integrations.reel_studio.media import fonts, render as R
from integrations.reel_studio.media.probe import probe
from tests.unit.reel_media_fixtures import make_clip, make_png, requires_ffmpeg

SMALL = {'w': 216, 'h': 384}
TAMIL_HOOK = "மணமகள்: 100% 'வாவ்'"
XL, XR, XH = '1' * 64, '2' * 64, '3' * 64


# ── builders ──────────────────────────────────────────────────────────────

def _entry(path, order):
    info = probe(path)
    cap = ClipCapture(clip=path, order=order, captured_at_ms=1760247082000 + order * 60000,
                      src='phone')
    return info.to_edl_clip(cap)


def _seg(clip, in_s, out_s, start, end, *, speed=1.0, stage='base', role='process',
         crop=None):
    return {'clip': clip, 'in_s': in_s, 'out_s': out_s, 'speed': speed,
            'out_start_s': start, 'out_end_s': end,
            'crop': crop or {'cx': 0.5, 'cy': 0.5, 'zoom': 1.0}, 'face': None,
            'transition': {'type': 'cut', 'dur_s': 0.0}, 'stage': stage, 'appeal': 7.0,
            'role': role}


def _edl(clips, segments, overlays=(), target=5.0):
    last = segments[-1]
    return {'schema': C.EDL_SCHEMA, 'canvas': dict(C.CANVAS), 'target_s': target,
            'duration_s': last['out_end_s'], 'audio': None, 'final_look_found': True,
            'hook_first': False, 'capture_src': 'phone',
            'reveal': {'clip': last['clip'], 't_s': round((last['in_s'] + last['out_s']) / 2, 3)},
            'notes': [C.capture_note('phone')],
            'beats': {'source': 'synthetic', 'song_key': None, 'bpm': 120.0,
                      'times_s': [0.5 * k for k in range(0, 2 * int(target) + 3)],
                      'confidence': 0.0},
            'clips': clips, 'segments': list(segments), 'overlays': list(overlays)}


def _text(text, start=0.0, end=2.0):
    return {'type': 'text', 'text': text, 'font': 'auto', 'size_px': 72, 'color': '#FFFFFF',
            'box': True, 'x': 0.393519, 'y': 0.18, 'anchor': 'center',
            'from_s': start, 'to_s': end}


def _logo(src, corner='top_left'):
    return {'type': 'logo', 'src': src, 'corner': corner, 'width_px': 180, 'height_px': 90,
            'margin_px': 48, 'opacity': 1.0, 'from_s': 0.0, 'to_s': None}


def _frame_at(path, t_s, w, h):
    stream = media_bin.ffmpeg_stream(
        ['-loglevel', 'error', '-ss', '%.3f' % t_s, '-i', path, '-frames:v', '1',
         '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1'], frame_bytes=w * h * 3, timeout_s=60)
    try:
        chunk = next(stream)
    finally:
        stream.close()
    return np.frombuffer(chunk, np.uint8).reshape(h, w, 3)


class _Spy:
    """Records every run_ffmpeg call; ``fail`` decides calls that fail without running."""

    def __init__(self, real, fail=None):
        self.real, self.fail, self.calls = real, fail, []

    def __call__(self, args, *, timeout_s):
        self.calls.append((list(args), timeout_s))
        if self.fail is not None and self.fail(args):
            return BoundedResult(1, '', 'No NVENC capable devices found', False)
        return self.real(args, timeout_s=timeout_s)


def _outputs(spy, workdir):
    return [a for a, _t in spy.calls if a and str(a[-1]).startswith(str(workdir))]


def _nirmala():
    path = fonts.resolve_font(TAMIL_HOOK)
    return path if path and os.path.isfile(path) else None


# ── pure: argument builders ───────────────────────────────────────────────

def _hlg_clip():
    return {'path': 'C:/clips/hlg.mp4', 'duration_s': 4.0, 'rotation': 90, 'transfer': 'hlg',
            'fps_avg': 30.0, 'display_w': 1080, 'display_h': 1920, 'hfr': False, 'order': 0,
            'captured_at_ms': None, 'capture_src': 'upload_order'}


def test_segment_args_put_the_filters_in_render_order_and_fps_last():
    seg = _seg(XL, 1.0, 2.5, 0.0, 1.0, speed=1.5)
    args = R.plan_segment_args(seg, _hlg_clip(), 'C:/work/seg.mp4')
    assert args.count('-i') == 1
    i = args.index('-i')
    assert args[args.index('-ss') + 1] == '1.000000' and args.index('-ss') < i
    assert args[args.index('-to') + 1] == '2.500000' and args.index('-to') < i
    vf = args[args.index('-vf') + 1]
    order = [vf.index(s) for s in ('scale=', 'zscale', 'tonemap', 'crop=', 'setpts=', 'fps=30')]
    assert order == sorted(order)
    assert 'setpts=(PTS-STARTPTS)/1.5' in vf
    assert args[args.index('-frames:v') + 1] == '30'
    assert '-an' in args and args[-1] == 'C:/work/seg.mp4'
    assert args[args.index('-c:v') + 1] == 'libx264' and args[args.index('-crf') + 1] == '14'


def test_sdr_segments_are_not_tone_mapped():
    clip = dict(_hlg_clip(), transfer='sdr')
    vf = R.plan_segment_args(_seg(XL, 0.0, 1.0, 0.0, 1.0), clip, 'o.mp4')
    assert 'zscale' not in ' '.join(vf) and 'tonemap' not in ' '.join(vf)


def _final(encoder, edl=None):
    edl = edl or _edl({XL: dict(_hlg_clip(), transfer='sdr')},
                      [_seg(XL, 0.0, 5.0, 0.0, 5.0, stage='final_look', role='reveal')],
                      overlays=[_text('Hook'), _logo('C:/logo.png')])
    return R.plan_final_args(edl, 'C:/work/list.txt', 'C:/out/reel.mp4', encoder=encoder,
                             textfiles={0: ('C:/work/text_0.txt', 'C:/Windows/Fonts/arial.ttf')})


def test_final_args_use_crf_under_a_maxrate_never_a_fixed_bitrate():
    args = _final('libx264')
    assert args[args.index('-c:v') + 1] == 'libx264'
    assert '-crf' in args and args[args.index('-maxrate') + 1] == '12M'
    assert '-b:v' not in args
    assert args[args.index('-movflags') + 1] == '+faststart'
    assert '-an' in args and args[args.index('-profile:v') + 1] == 'high'
    assert args[args.index('-pix_fmt') + 1] == 'yuv420p'
    assert args[args.index('-f') + 1] == 'concat' and args.count('-i') == 2   # list + logo
    fc = args[args.index('-filter_complex') + 1]
    assert 'textfile=C\\\\:/work/text_0.txt' in fc and 'expansion=none' in fc
    assert 'overlay=' in fc


def test_final_args_with_nvenc_use_constant_quality():
    args = _final('h264_nvenc')
    assert args[args.index('-c:v') + 1] == 'h264_nvenc'
    assert args[args.index('-cq') + 1] == '21' and args[args.index('-b:v') + 1] == '0'
    assert args[args.index('-maxrate') + 1] == '12M'


def test_a_text_overlay_without_a_font_is_left_out_of_the_filter():
    edl = _edl({XL: dict(_hlg_clip(), transfer='sdr')},
               [_seg(XL, 0.0, 5.0, 0.0, 5.0, stage='final_look', role='reveal')],
               overlays=[_text('Hook')])
    args = R.plan_final_args(edl, 'C:/work/list.txt', 'C:/out/reel.mp4', encoder='libx264',
                             textfiles={})
    assert 'drawtext' not in ' '.join(args)


# ── pure: filter-path escaping ────────────────────────────────────────────

def _ffmpeg_unescape(value):
    """ffmpeg's two unescaping levels: the filtergraph, then the option value."""
    def level(s, special):
        out, i = [], 0
        while i < len(s):
            if s[i] == '\\' and i + 1 < len(s):
                out.append(s[i + 1])
                i += 2
                continue
            assert s[i] not in special, (s, s[i])
            out.append(s[i])
            i += 1
        return ''.join(out)
    return level(level(value, "'[],;"), "':")


@pytest.mark.parametrize('path', [
    'C:\\Windows\\Fonts\\Nirmala.ttc',
    "D:\\work\\it's [a], dir; x\\text_0.txt",
    '/home/her/reel:studio/text.txt',
    'C:/already/forward/slash.ttf',
])
def test_escape_filter_path_round_trips_through_both_ffmpeg_levels(path):
    escaped = R.escape_filter_path(path)
    assert _ffmpeg_unescape(escaped) == path.replace('\\', '/')


# ── fonts ─────────────────────────────────────────────────────────────────

def test_latin_text_gets_an_existing_font_file():
    path = fonts.resolve_font('Bridal look in 20s')
    if path is None:
        pytest.skip('no Latin font found on this machine')
    assert os.path.isfile(path)


@pytest.mark.skipif(sys.platform != 'win32', reason='Nirmala ships with Windows only (U12)')
@pytest.mark.parametrize('text', [TAMIL_HOOK, 'दुल्हन का मेकअप', 'ਦੁਲਹਨ ਮੇਕਅਪ', 'Bridal மணமகள்'])
def test_indic_text_on_windows_uses_nirmala(text):
    path = fonts.resolve_font(text)
    if path is None:
        pytest.skip('Nirmala is not installed on this Windows machine')
    assert os.path.basename(path).lower().startswith('nirmala')


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
def test_indic_text_has_no_font_off_windows(platform):
    assert fonts.resolve_font(TAMIL_HOOK, platform=platform) is None


def test_emoji_are_dropped_from_overlay_text():
    assert fonts.drop_emoji('Bridal ✨ look 💄💍') == 'Bridal look'
    assert fonts.drop_emoji('மணமகள் 👰🏽\u200d♀️') == 'மணமகள்'


# ── real renders ──────────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def three_clips(tmp_path_factory):
    if not media_bin.get_ffmpeg():
        pytest.skip('no ffmpeg')
    d = tmp_path_factory.mktemp('render_src')
    land = make_clip(d, 'land.mp4', size=(1920, 1080), fps=30, duration_s=3.0)
    rot = make_clip(d, 'rot.mp4', size=(1080, 608), fps=30, duration_s=3.0,
                    display_rotation=90, audio=True)
    p60 = make_clip(d, 'p60.mp4', size=(608, 1080), fps=60, duration_s=3.0,
                    vf='colorchannelmixer=rr=0.3:gg=0.5:bb=1.0')
    logo = make_png(d, 'logo.png', size=(180, 90), color='red')
    return {'land': land, 'rot': rot, 'p60': p60, 'logo': logo, 'dir': d}


def _three_clip_edl(c, overlays):
    clips = {XL: _entry(c['land'], 0), XR: _entry(c['rot'], 1), XH: _entry(c['p60'], 2)}
    assert (clips[XR]['display_w'], clips[XR]['display_h']) == (608, 1080)
    segs = [_seg(XL, 0.5, 1.5, 0.0, 1.0, stage='bare_face'),
            _seg(XR, 0.3, 1.8, 1.0, 2.0, speed=1.5, stage='eyes'),
            _seg(XH, 0.0, 1.0, 2.0, 3.0, stage='lips'),
            _seg(XH, 1.0, 3.0, 3.0, 5.0, stage='final_look', role='reveal')]
    return _edl(clips, segs, overlays=overlays, target=5.0)


@requires_ffmpeg
def test_three_clips_and_a_speed_ramp_pass_every_hard_qa_check(three_clips, tmp_path,
                                                               monkeypatch):
    from integrations.reel_studio.media.qa import run_qa
    hook = TAMIL_HOOK if _nirmala() else "Bridal: 100% 'wow'"
    edl = _three_clip_edl(three_clips, [_text(hook), _logo(three_clips['logo'])])
    assert C.validate_edl(edl) == []
    spy = _Spy(media_bin.run_ffmpeg)
    monkeypatch.setattr(R.media_bin, 'run_ffmpeg', spy)
    work = tmp_path / 'work'
    out = str(tmp_path / 'reel.mp4')
    result = R.render(edl, out, workdir=str(work), encoder='libx264', timeout_s=300)

    pass1 = [a for a in _outputs(spy, work) if '-frames:v' in a]
    assert len(pass1) == len(edl['segments'])
    assert all(a.count('-i') == 1 for a in pass1)
    final = [a for a, _t in spy.calls if a and a[-1].endswith('.mp4') and '-filter_complex' in a]
    assert len(final) == 1
    fc = final[0][final[0].index('-filter_complex') + 1]
    assert 'textfile=' in fc and 'fontfile=' in fc
    assert all(0 < t <= 300 for _a, t in spy.calls)

    assert result.path == os.path.abspath(out) and result.encoder == 'libx264'
    assert result.size_bytes == os.path.getsize(out)
    assert result.duration_s == pytest.approx(5.0)
    monkeypatch.undo()
    report = run_qa(out, edl)
    assert C.validate_qa_report(report) == []
    failed = [r for r in report['checks'] if r['hard'] and not r['ok']]
    assert failed == [] and report['passed'] is True
    fps = next(r for r in report['checks'] if r['id'] == 'fps')
    assert fps['value']['frames'] == 150
    assert probe(out).has_audio is False

    # The logo is in its corner (top-left of the safe zone), red on the frame.
    frame = _frame_at(out, 3.5, 1080, 1920)
    x0, y0, x1, y1 = C.logo_box(edl['overlays'][1])
    b, g, r = frame[int((y0 + y1) / 2), int((x0 + x1) / 2)].astype(int)
    assert r > 180 and g < 90 and b < 90
    b, g, r = frame[int((y0 + y1) / 2), 1080 - 60].astype(int)
    assert not (r > 180 and g < 90 and b < 90)
    # Intermediates are gone; the work dir is the caller's to remove.
    assert [p for p in os.listdir(work) if p.startswith('reel_')] == []


def _black_source(d):
    return make_clip(d, 'black.mp4', size=(216, 384), source='color', color='black',
                     duration_s=2.0)


def _one_seg_edl(path, overlays=()):
    return _edl({XL: _entry(path, 0)}, [_seg(XL, 0.0, 2.0, 0.0, 2.0, stage='final_look',
                                             role='reveal')],
                overlays=overlays, target=5.0)


@requires_ffmpeg
def test_hook_text_is_drawn_from_a_textfile_in_an_awkward_work_dir(tmp_path):
    src = _black_source(tmp_path)
    hook = TAMIL_HOOK if _nirmala() else "Bridal: 100% 'wow'"
    edl = _one_seg_edl(src, [_text(hook, 0.0, 2.0)])
    work = tmp_path / "it's [a], work; dir"
    out = str(tmp_path / 'text.mp4')
    result = R.render(edl, out, workdir=str(work), encoder='libx264', canvas=SMALL)
    assert result.notes == ()
    frame = _frame_at(out, 0.5, SMALL['w'], SMALL['h'])
    band = frame[int(0.18 * 384) - 25:int(0.18 * 384) + 25]
    assert (band.max(axis=2) > 200).sum() > 30          # white glyphs on black


@requires_ffmpeg
def test_text_with_no_font_is_skipped_with_a_note(tmp_path, monkeypatch):
    src = _black_source(tmp_path)
    monkeypatch.setattr(R.fonts, 'resolve_font', lambda text, **kw: None)
    edl = _one_seg_edl(src, [_text('Hook')])
    out = str(tmp_path / 'nofont.mp4')
    result = R.render(edl, out, workdir=str(tmp_path / 'w'), encoder='libx264', canvas=SMALL)
    assert R.NOTE_TEXT_NO_FONT in result.notes
    frame = _frame_at(out, 0.5, SMALL['w'], SMALL['h'])
    assert frame.max() < 40


@requires_ffmpeg
def test_hlg_source_comes_out_bt709_with_exact_frames(tmp_path):
    src = make_clip(tmp_path, 'hlg.mp4', size=(384, 216), duration_s=2.0, hdr='hlg')
    # A slow (0.75x) segment then a 1x segment: 30 + 36 frames.
    edl = _edl({XL: _entry(src, 0)},
               [_seg(XL, 0.0, 0.75, 0.0, 1.0, speed=0.75),
                _seg(XL, 0.8, 2.0, 1.0, 2.2, stage='final_look', role='reveal')],
               target=5.0)
    out = str(tmp_path / 'hlg_out.mp4')
    result = R.render(edl, out, workdir=str(tmp_path / 'w'), encoder='libx264', canvas=SMALL)
    assert result.duration_s == pytest.approx(2.2)
    info = probe(out)
    assert info.transfer == 'sdr' and info.bit_depth == 8
    assert (info.width, info.height) == (216, 384)
    counted = media_bin.run_ffmpeg(['-i', out, '-map', '0:v:0', '-f', 'null', '-'], timeout_s=60)
    frames = [int(x) for x in re.findall(r'frame=\s*(\d+)', counted.stderr)]
    assert frames[-1] == 66
    frame = _frame_at(out, 0.5, SMALL['w'], SMALL['h'])
    assert 20 < frame.mean() < 235                      # tone-mapped, not blown or black


@requires_ffmpeg
def test_nvenc_failure_falls_back_to_libx264_with_a_note(tmp_path, monkeypatch):
    src = _black_source(tmp_path)
    spy = _Spy(media_bin.run_ffmpeg, fail=lambda args: 'h264_nvenc' in args)
    monkeypatch.setattr(R.media_bin, 'run_ffmpeg', spy)
    out = str(tmp_path / 'fallback.mp4')
    result = R.render(_one_seg_edl(src), out, workdir=str(tmp_path / 'w'),
                      encoder='h264_nvenc', canvas=SMALL)
    assert result.encoder == 'libx264' and R.NOTE_NVENC_FAILED in result.notes
    assert os.path.isfile(out)


@requires_ffmpeg
def test_a_failed_render_leaves_no_output_and_no_intermediates(tmp_path, monkeypatch):
    src = _black_source(tmp_path)
    spy = _Spy(media_bin.run_ffmpeg, fail=lambda args: '-filter_complex' in args)
    monkeypatch.setattr(R.media_bin, 'run_ffmpeg', spy)
    out = str(tmp_path / 'broken.mp4')
    work = tmp_path / 'w'
    with pytest.raises(media_bin.FFmpegFailed):
        R.render(_one_seg_edl(src), out, workdir=str(work), encoder='libx264', canvas=SMALL)
    assert not os.path.exists(out)
    assert [p for p in os.listdir(work) if p.startswith('reel_')] == []


def test_a_structurally_broken_edl_is_refused_before_ffmpeg_runs(tmp_path, monkeypatch):
    spy = _Spy(lambda args, timeout_s: pytest.fail('ffmpeg must not run'))
    monkeypatch.setattr(R.media_bin, 'run_ffmpeg', spy)
    with pytest.raises(ValueError):
        R.render({'schema': C.EDL_SCHEMA}, str(tmp_path / 'x.mp4'), workdir=str(tmp_path))
    assert spy.calls == []


def test_an_unknown_encoder_is_refused(tmp_path):
    with pytest.raises(ValueError):
        R.render(_edl({XL: dict(_hlg_clip(), transfer='sdr')},
                      [_seg(XL, 0.0, 5.0, 0.0, 5.0, stage='final_look', role='reveal')]),
                 str(tmp_path / 'x.mp4'), workdir=str(tmp_path), encoder='h265_magic')
