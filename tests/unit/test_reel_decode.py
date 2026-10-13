"""integrations.reel_studio.media.decode: one ffmpeg pipe per clip.

``iter_frames`` decodes a clip once at 5 fps into 8-bit BGR at a 768 px long
side, in display orientation, tone-mapping HDR after the downscale.  The
real ffmpeg runs on lavfi clips made in tmp_path; the pure helpers
(frame_size, tonemap_filter) are checked without it.
"""
import numpy as np
import pytest

from integrations.reel_studio.contracts import ClipInfo
from integrations.reel_studio.media import decode
from integrations.reel_studio.media.decode import frame_size, iter_frames, tonemap_filter
from integrations.reel_studio.media.probe import probe
from tests.unit.reel_media_fixtures import make_clip, requires_ffmpeg


def _info(dw, dh, **kw):
    base = dict(path='x.mp4', duration_s=2.0, width=dw, height=dh, display_w=dw, display_h=dh,
                rotation=0, fps_avg=30.0, codec='h264')
    base.update(kw)
    return ClipInfo(**base)


# ── pure helpers ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('dw, dh, want', [
    (1080, 1920, (432, 768)),
    (1920, 1080, (768, 432)),
    (1080, 2400, (346, 768)),      # a 20:9 phone
    (320, 180, (768, 432)),        # small sources are brought to the same scale
    (2160, 3840, (432, 768)),
])
def test_frame_size_keeps_the_display_aspect_at_a_768_long_side(dw, dh, want):
    assert frame_size(_info(dw, dh)) == want


def test_frame_size_honours_long_side():
    assert frame_size(_info(1080, 1920), long_side=160) == (90, 160)


def test_tonemap_filter_per_transfer():
    assert tonemap_filter('sdr') == ''
    hlg = tonemap_filter('hlg')
    pq = tonemap_filter('pq')
    assert 'tin=arib-std-b67' in hlg and 'tonemap=' in hlg
    assert 'tin=smpte2084' in pq and 'tonemap=' in pq
    with pytest.raises(ValueError):
        tonemap_filter('hdr10plus')


def test_iter_frames_refuses_bad_arguments_before_running_ffmpeg():
    with pytest.raises(ValueError):
        iter_frames(_info(1080, 1920), fps=0)
    with pytest.raises(ValueError):
        iter_frames(_info(1080, 1920), long_side=1)


# ── the real binary ───────────────────────────────────────────────────────

@requires_ffmpeg
@pytest.mark.parametrize('duration_s, expect', [(2.0, 10), (3.3, 17), (1.0, 5)])
def test_frame_count_is_fps_times_duration(tmp_path, duration_s, expect):
    info = probe(make_clip(tmp_path, 'c.mp4', size=(160, 90), duration_s=duration_s))
    frames = list(iter_frames(info))
    assert abs(len(frames) - duration_s * 5) <= 1
    assert len(frames) == expect
    times = [t for t, _ in frames]
    assert times == pytest.approx([k / 5.0 for k in range(len(frames))])
    for _, bgr in frames:
        assert bgr.dtype == np.uint8 and bgr.shape == (432, 768, 3)


@requires_ffmpeg
def test_long_side_is_768_for_portrait_coded_clips(tmp_path):
    info = probe(make_clip(tmp_path, 'p.mp4', size=(180, 320), duration_s=1.0))
    shapes = {bgr.shape for _, bgr in iter_frames(info)}
    assert shapes == {(768, 432, 3)}


@requires_ffmpeg
def test_frames_are_in_display_orientation(tmp_path):
    # A white block in the CODED top-left corner of a black frame.  The
    # display matrix turns the frame 90 deg counter-clockwise, which carries
    # that corner to the bottom-left; a squash without rotation would leave
    # it top-left.
    path = make_clip(tmp_path, 'rot.mp4', size=(320, 180), duration_s=1.0, source='color',
                     color='black', vf='drawbox=x=0:y=0:w=80:h=45:color=white:t=fill',
                     display_rotation=90)
    info = probe(path)
    _, bgr = next(iter(iter_frames(info)))
    assert bgr.shape == (768, 432, 3)
    gray = bgr.mean(axis=2)
    h, w = gray.shape
    quads = {'top_left': gray[:h // 4, :w // 4], 'top_right': gray[:h // 4, -w // 4:],
             'bottom_left': gray[-h // 4:, :w // 4], 'bottom_right': gray[-h // 4:, -w // 4:]}
    lit = {k for k, v in quads.items() if v.mean() > 200}
    assert lit == {'bottom_left'}


@requires_ffmpeg
def test_each_frame_is_the_one_on_screen_at_its_time(tmp_path):
    # Frame n of a 30 fps clip carries luma 4n, so the decoded value names
    # the source frame.  At 5 fps, time k/5 s shows source frame 6k.
    path = make_clip(tmp_path, 'n.mp4', size=(160, 90), duration_s=2.0, source='color',
                     color='black', vf="format=gray,geq=lum='N*4'")
    info = probe(path)
    got = [int(round(bgr.mean() / 4.0)) for _, bgr in iter_frames(info)]
    assert got == [6 * k for k in range(10)]


@requires_ffmpeg
def test_hlg_10_bit_decodes_to_8_bit_bgr(tmp_path):
    info = probe(make_clip(tmp_path, 'hlg.mp4', size=(320, 180), duration_s=1.0, hdr='hlg'))
    assert info.transfer == 'hlg' and info.bit_depth == 10
    frames = list(iter_frames(info))
    assert len(frames) == 5
    for _, bgr in frames:
        assert bgr.dtype == np.uint8 and bgr.shape == (432, 768, 3)
        assert 40 < bgr.mean() < 215          # tone-mapped picture, not black or blown


@requires_ffmpeg
def test_hdr_without_tonemap_filters_still_decodes(tmp_path, monkeypatch):
    # A system ffmpeg without zimg has no zscale: analysis goes on untoned.
    info = probe(make_clip(tmp_path, 'hlg.mp4', size=(160, 90), duration_s=0.6, hdr='hlg'))
    monkeypatch.setattr(decode, '_has_tonemap_filters', lambda: False)
    frames = list(iter_frames(info))
    assert len(frames) == 3 and frames[0][1].dtype == np.uint8


@requires_ffmpeg
def test_closing_early_stops_the_decode(tmp_path):
    info = probe(make_clip(tmp_path, 'c.mp4', size=(160, 90), duration_s=3.0))
    it = iter_frames(info)
    first = next(it)
    it.close()
    assert first[0] == 0.0
    with pytest.raises(StopIteration):
        next(it)
