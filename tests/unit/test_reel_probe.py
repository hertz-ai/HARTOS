"""integrations.reel_studio.media.probe: what a phone clip is, from ``ffmpeg -i``.

There is no ffprobe in this stack, so the clip facts come from the stderr of
``ffmpeg -hide_banner -i <clip>``.  The canned texts in
tests/fixtures/reel/ffmpeg_info_*.txt are that stderr, captured from the
pinned ffmpeg 7.1 (imageio-ffmpeg 0.6.0) on lavfi-generated clips of phone
shapes; only the input path in the ``from '...'`` / ``Error opening`` lines
was replaced with a phone-style file name:

  hlg_rotated       1920x1080 HEVC Main 10 HLG, display matrix -90 (an
                    Android portrait recording), AAC, creation_time
  60fps             1920x1080 H.264 at 60 fps, AAC, creation_time
  240fps            1280x720 H.264 at 240 fps (slow motion), no creation time
  no_audio          1080x1920 H.264, no audio stream
  pq                1920x1080 HEVC Main 10 HDR10 (smpte2084)
  dovi_p84 / p5     the hlg_rotated shape with a real Dolby Vision
                    configuration box (dvvC profile 8 compat 4 / dvcC profile
                    5 compat 0) written into the hvc1 sample entry, and only
                    Apple's com.apple.quicktime.creationdate key (iPhone shape)
  unreadable        a text file named .mp4
  audio_cover_only  AAC plus a PNG cover (an attached picture, not video)

Variants of those texts (old-ffmpeg ``rotate`` tag, other matrix angles,
``N/A`` duration) are made by editing one line of the real text.  The tests
marked ``requires_ffmpeg`` run the real binary on clips made in tmp_path.
"""
import calendar
import os

import pytest

from core import media_bin
from core.subprocess_safe import BoundedResult
from integrations.reel_studio import contracts
from integrations.reel_studio.contracts import ClipInfo, ClipUnsupported
from integrations.reel_studio.media import probe as probe_mod
from integrations.reel_studio.media.probe import parse_ffmpeg_info, probe
from tests.unit.reel_media_fixtures import XID_A, golden_edl, make_clip, requires_ffmpeg

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'fixtures', 'reel')


def _text(name: str) -> str:
    with open(os.path.join(FIXTURES, 'ffmpeg_info_%s.txt' % name), encoding='utf-8') as fh:
        return fh.read()


def _epoch(y, mo, d, h, mi, s) -> float:
    return float(calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)))


# ── canned stderr from the real binary ────────────────────────────────────

def test_android_portrait_hlg_hevc():
    info = parse_ffmpeg_info(_text('hlg_rotated'), path='VID_20261012_110122.mp4')
    assert isinstance(info, ClipInfo)
    assert info.path == 'VID_20261012_110122.mp4'
    assert (info.codec, info.width, info.height) == ('hevc', 1920, 1080)
    # displaymatrix "rotation of -90.00 degrees": the frame turns 90 deg
    # clockwise for display, so the portrait recording shows 1080x1920.
    assert info.rotation == 90
    assert (info.display_w, info.display_h) == (1080, 1920)
    assert info.transfer == 'hlg'
    assert info.pix_fmt == 'yuv420p10le'
    assert info.bit_depth == 10
    assert info.fps_avg == 30.0
    assert info.hfr is False
    assert info.has_audio is True
    assert info.duration_s == pytest.approx(1.0)
    assert info.dovi_profile is None
    assert info.creation_time == _epoch(2026, 10, 12, 5, 31, 22)


def test_60fps_sdr_is_not_high_frame_rate():
    info = parse_ffmpeg_info(_text('60fps'))
    assert info.codec == 'h264'
    assert info.fps_avg == 60.0
    assert info.hfr is False
    assert info.transfer == 'sdr'
    assert info.bit_depth == 8
    assert info.rotation == 0
    assert (info.display_w, info.display_h) == (1920, 1080)
    assert info.has_audio is True
    assert info.creation_time == _epoch(2026, 10, 12, 5, 35, 0)


def test_240fps_slow_motion_is_high_frame_rate():
    info = parse_ffmpeg_info(_text('240fps'))
    assert info.fps_avg == 240.0
    assert info.hfr is True
    assert info.fps_avg >= contracts.HFR_MIN_FPS
    assert info.duration_s == pytest.approx(0.5)
    assert info.creation_time is None
    assert info.has_audio is False


def test_no_audio_stream():
    info = parse_ffmpeg_info(_text('no_audio'))
    assert info.has_audio is False
    assert info.rotation == 0
    assert (info.width, info.height) == (1080, 1920)
    assert (info.display_w, info.display_h) == (1080, 1920)


def test_hdr10_pq():
    info = parse_ffmpeg_info(_text('pq'))
    assert info.transfer == 'pq'
    assert info.bit_depth == 10


def test_dolby_vision_8_4_decodes_its_hlg_base_layer():
    info = parse_ffmpeg_info(_text('dovi_p84'))
    assert info.dovi_profile == 8
    assert info.transfer == 'hlg'
    assert info.rotation == 90
    assert (info.display_w, info.display_h) == (1080, 1920)
    # iPhone files carry Apple's local-time key; +0530 is 05:31:22 UTC.
    assert info.creation_time == _epoch(2026, 10, 12, 5, 31, 22)


def test_dolby_vision_profile_5_is_refused():
    with pytest.raises(ClipUnsupported) as err:
        parse_ffmpeg_info(_text('dovi_p5'))
    assert err.value.reason == 'dolby_vision_p5'
    assert err.value.reason in probe_mod.UNSUPPORTED_REASONS


def test_a_file_ffmpeg_cannot_open_is_unreadable():
    with pytest.raises(ClipUnsupported) as err:
        parse_ffmpeg_info(_text('unreadable'))
    assert err.value.reason == 'unreadable'
    assert 'Invalid data' in err.value.detail


def test_an_attached_picture_is_not_a_video_stream():
    with pytest.raises(ClipUnsupported) as err:
        parse_ffmpeg_info(_text('audio_cover_only'))
    assert err.value.reason == 'no_video'


def test_empty_stderr_is_unreadable_not_a_crash():
    for text in ('', '\n\n', 'At least one output file must be specified\n'):
        with pytest.raises(ClipUnsupported) as err:
            parse_ffmpeg_info(text)
        assert err.value.reason == 'unreadable'


# ── one-line variants of the real text ────────────────────────────────────

@pytest.mark.parametrize('angle, rotation, display', [
    ('90.00', 270, (1080, 1920)),     # ffmpeg -display_rotation 90 (counter-clockwise)
    ('-90.00', 90, (1080, 1920)),
    ('180.00', 180, (1920, 1080)),
    ('-180.00', 180, (1920, 1080)),
    ('-89.97', 90, (1080, 1920)),     # a matrix a hair off a right angle
])
def test_display_matrix_angles(angle, rotation, display):
    text = _text('hlg_rotated').replace('rotation of -90.00 degrees',
                                        'rotation of %s degrees' % angle)
    info = parse_ffmpeg_info(text)
    assert info.rotation == rotation
    assert (info.display_w, info.display_h) == display


def test_old_ffmpeg_rotate_tag_is_read_when_there_is_no_display_matrix():
    text = _text('60fps').replace(
        '        handler_name    : VideoHandler',
        '        rotate          : 90\n        handler_name    : VideoHandler', 1)
    info = parse_ffmpeg_info(text)
    assert info.rotation == 90
    assert (info.display_w, info.display_h) == (1080, 1920)


def test_unknown_duration_is_unreadable():
    text = _text('60fps').replace('Duration: 00:00:01.00', 'Duration: N/A')
    with pytest.raises(ClipUnsupported) as err:
        parse_ffmpeg_info(text)
    assert err.value.reason == 'unreadable'


def test_frame_rate_falls_back_to_tbr_and_reads_the_k_suffix():
    text = _text('240fps').replace('240 fps, 240 tbr', '1k tbr')
    info = parse_ffmpeg_info(text)
    assert info.fps_avg == 1000.0
    assert info.hfr is True


def test_non_square_pixels_widen_the_display_frame():
    text = _text('60fps').replace('1920x1080 [SAR 1:1 DAR 16:9]', '1440x1080 [SAR 4:3 DAR 16:9]')
    info = parse_ffmpeg_info(text)
    assert (info.width, info.height) == (1440, 1080)
    assert (info.display_w, info.display_h) == (1920, 1080)


def test_stream_creation_time_is_the_fallback():
    text = _text('60fps').replace(
        '    creation_time   : 2026-10-12T05:35:00.000000Z\n    encoder         : Lavf61.7.100',
        '    encoder         : Lavf61.7.100', 1)
    info = parse_ffmpeg_info(text)
    assert info.creation_time == _epoch(2026, 10, 12, 5, 35, 0)


# ── A1: the container time orders clips the phone gave no time for ──────

def test_probe_creation_time_orders_clips_without_a_phone_time():
    early = parse_ffmpeg_info(_text('hlg_rotated'))    # 05:31:22Z
    late = parse_ffmpeg_info(_text('60fps'))           # 05:35:00Z
    # Uploaded late-first, and the phone sent no captured_at.
    captures, src = contracts.resolve_capture_order([
        {'clip': 'late', 'captured_at_ms': None, 'creation_time': late.creation_time},
        {'clip': 'early', 'captured_at_ms': None, 'creation_time': early.creation_time},
    ])
    assert src == 'container'
    assert [c.clip for c in captures] == ['early', 'late']
    assert captures[0].captured_at_ms == int(early.creation_time * 1000)


def test_probed_clip_fills_a_valid_edl_clip_entry():
    info = parse_ffmpeg_info(_text('hlg_rotated'), path='C:/reel/work/a.mp4')
    capture = contracts.ClipCapture(clip=XID_A, order=0,
                                    captured_at_ms=int(info.creation_time * 1000),
                                    src='container')
    edl = golden_edl()
    edl['clips'][XID_A] = info.to_edl_clip(capture)
    problems = contracts.validate_edl(edl)
    assert not [p for p in problems if p.startswith('schema:')], problems


# ── probe(): the real binary ──────────────────────────────────────────────

def test_probe_timeout_raises_ffmpeg_failed(monkeypatch, tmp_path):
    clip = tmp_path / 'x.mp4'
    clip.write_bytes(b'\0' * 16)
    seen = {}

    def fake_run(args, *, timeout_s):
        seen['args'], seen['timeout_s'] = list(args), timeout_s
        return BoundedResult(-9, '', '', timed_out=True)

    monkeypatch.setattr(media_bin, 'run_ffmpeg', fake_run)
    with pytest.raises(media_bin.FFmpegFailed) as err:
        probe(str(clip), timeout_s=7.5)
    assert err.value.timed_out is True
    assert seen['timeout_s'] == 7.5
    assert seen['args'][-2:] == ['-i', os.path.abspath(str(clip))]


def test_probe_of_a_missing_file_is_unreadable_without_running_ffmpeg(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError('ffmpeg must not run for a missing file')

    monkeypatch.setattr(media_bin, 'run_ffmpeg', boom)
    with pytest.raises(ClipUnsupported) as err:
        probe(str(tmp_path / 'gone.mp4'))
    assert err.value.reason == 'unreadable'


@requires_ffmpeg
def test_real_display_rotation_90_reports_portrait_display(tmp_path):
    path = make_clip(tmp_path, 'rot.mp4', size=(320, 180), duration_s=1.0, display_rotation=90,
                     creation_time='2026-10-12T05:31:22Z')
    info = probe(path)
    assert info.path == os.path.abspath(path)
    assert (info.width, info.height) == (320, 180)
    assert info.rotation == 270           # 90 counter-clockwise = 270 clockwise
    assert (info.display_w, info.display_h) == (180, 320)
    assert info.creation_time == _epoch(2026, 10, 12, 5, 31, 22)
    assert info.duration_s == pytest.approx(1.0, abs=0.05)


@requires_ffmpeg
def test_real_hlg_clip(tmp_path):
    path = make_clip(tmp_path, 'hlg.mp4', size=(320, 180), duration_s=1.0, hdr='hlg', audio=True)
    info = probe(path)
    assert info.codec == 'hevc'
    assert info.transfer == 'hlg'
    assert info.bit_depth == 10
    assert info.has_audio is True


@requires_ffmpeg
def test_real_non_video_file_is_unreadable(tmp_path):
    bad = tmp_path / 'notes.mp4'
    bad.write_bytes(b'not a video\n' * 50)
    with pytest.raises(ClipUnsupported) as err:
        probe(str(bad))
    assert err.value.reason == 'unreadable'
