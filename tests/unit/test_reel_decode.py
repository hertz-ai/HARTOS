"""integrations.reel_studio.media.decode: one ffmpeg pipe per clip.

``iter_frames`` decodes a clip once at 5 fps into 8-bit BGR at a 768 px long
side, in display orientation, tone-mapping HDR after the downscale.  The
real ffmpeg runs on lavfi clips made in tmp_path; the pure helpers
(frame_size, tonemap_filter's refusals, the decode bound) are checked
without it.

Both frame dimensions are even: the tone-map chain runs zscale on the
scaled 4:2:0 frame, and zscale refuses a size its chroma subsampling does
not divide ("image dimensions must be divisible by subsampling factor"),
which a free display aspect (2336x1080, a gallery crop) otherwise gives.
"""
import random

import numpy as np
import pytest

from core import media_bin
from core.subprocess_safe import BoundedResult
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
    # Display shapes whose exact short side is odd or fractional: rounded to
    # the nearest EVEN size, so 4:2:0 chroma (and zscale) can take the frame.
    (160, 346, (356, 768)),        # exact 355.1
    (346, 160, (768, 356)),
    (2336, 1080, (768, 356)),      # exact 355.1
    (2460, 1080, (768, 338)),      # exact 337.2
    (1792, 828, (768, 354)),       # exact 354.9
    (2532, 1170, (768, 354)),
    (1170, 2532, (354, 768)),
])
def test_frame_size_keeps_the_display_aspect_at_a_768_long_side(dw, dh, want):
    assert frame_size(_info(dw, dh)) == want


def test_frame_size_is_always_even_and_within_a_pixel_of_the_aspect():
    rng = random.Random(11)
    for _ in range(500):
        dw, dh = rng.randint(16, 4096), rng.randint(16, 4096)
        w, h = frame_size(_info(dw, dh))
        assert w % 2 == 0 and h % 2 == 0, (dw, dh, w, h)
        assert max(w, h) == 768
        scale = 768.0 / max(dw, dh)
        assert abs(w - dw * scale) <= 1.0 + 1e-9 and abs(h - dh * scale) <= 1.0 + 1e-9


def test_frame_size_honours_long_side():
    assert frame_size(_info(1080, 1920), long_side=160) == (90, 160)


def test_frame_size_refuses_an_odd_long_side():
    # An odd long side cannot be honoured with even dimensions.
    with pytest.raises(ValueError):
        frame_size(_info(1080, 1920), long_side=767)


def test_tonemap_filter_per_transfer():
    assert tonemap_filter('sdr') == ''
    assert tonemap_filter('hlg') and tonemap_filter('pq')
    assert tonemap_filter('hlg') != tonemap_filter('pq')
    with pytest.raises(ValueError):
        tonemap_filter('hdr10plus')


def test_iter_frames_refuses_bad_arguments_before_running_ffmpeg():
    with pytest.raises(ValueError):
        iter_frames(_info(1080, 1920), fps=0)
    with pytest.raises(ValueError):
        iter_frames(_info(1080, 1920), long_side=1)
    with pytest.raises(ValueError):
        iter_frames(_info(1080, 1920), long_side=767)


@pytest.fixture
def decode_bound_seen(monkeypatch):
    """The bound iter_frames hands to media_bin.ffmpeg_stream (nothing runs)."""
    seen = {}

    def fake_stream(args, *, frame_bytes, timeout_s):
        seen['timeout_s'] = timeout_s
        return iter(())

    monkeypatch.setattr(media_bin, 'ffmpeg_stream', fake_stream)
    return seen


@pytest.mark.parametrize('fps_avg, want_s', [
    (30.0, 60.0 + 2.0 * 10),
    (24.0, 60.0 + 2.0 * 10),        # never below the 30 fps allowance
    (60.0, 60.0 + 2.0 * 10 * 2),
    (240.0, 60.0 + 2.0 * 10 * 8),   # slow motion decodes 8x the frames of 30 fps
    # ...and 8x is the most any frame rate earns.  probe takes fps_avg from
    # the stream's 'fps' or, failing that, its 'tbr' timebase rate ('1k',
    # '90k' parse to 1000 and 90000), which would stretch the bound to days.
    (960.0, 60.0 + 2.0 * 10 * 8),
    (90000.0, 60.0 + 2.0 * 10 * 8),
])
def test_the_decode_bound_scales_with_the_source_frame_rate(decode_bound_seen, fps_avg, want_s):
    iter_frames(_info(1920, 1080, duration_s=10.0, fps_avg=fps_avg))
    assert decode_bound_seen['timeout_s'] == pytest.approx(want_s)
    # An explicit bound is used as given.
    iter_frames(_info(1920, 1080, duration_s=10.0, fps_avg=fps_avg), timeout_s=5.0)
    assert decode_bound_seen['timeout_s'] == 5.0


@pytest.mark.parametrize('duration_s, fps_avg', [
    (100 * 3600.0, 30.0),           # a container claiming 100 hours
    (2.0 ** 31, 90000.0),           # a lying mvhd duration at a '90k tbr' rate
])
def test_the_default_decode_bound_never_outlives_the_reel_job(decode_bound_seen,
                                                              duration_s, fps_avg):
    """A reel job whose claim goes MAX_JOB_S without a beat is stale, and the
    pipeline beats between clips, not inside one decode; so the default bound
    stops at MAX_JOB_S.  That also keeps it under threading.TIMEOUT_MAX, past
    which media_bin refuses a bound (ValueError) instead of decoding."""
    from integrations.reel_studio.jobs import MAX_JOB_S
    info = _info(1920, 1080, duration_s=duration_s, fps_avg=fps_avg)
    assert decode.decode_bound_s(info) == MAX_JOB_S
    iter_frames(info)
    assert decode_bound_seen['timeout_s'] == MAX_JOB_S


# ── the tone-map capability check ─────────────────────────────────────────

_FILTERS_WITH_ZIMG = """Filters:
  T.. = Timeline support
  .S. = Slice threading
  ..C = Command support
  A = Audio input/output
  V = Video input/output
  | = Source or sink filter
 TSC scale             V->V       Scale the input video size and/or convert the image format.
 ..C tonemap           V->V       Conversion to/from different dynamic ranges.
 .SC zscale            V->V       Apply resizing, colorspace and bit depth conversion.
"""


@pytest.fixture
def fresh_filter_listing():
    """media_bin lists ffmpeg's filters once per process; start and end each test clean."""
    media_bin.ffmpeg_filters.cache_clear()
    yield
    media_bin.ffmpeg_filters.cache_clear()


def test_the_tonemap_check_lists_filters_only(monkeypatch, fresh_filter_listing):
    # ffmpeg_caps() also runs an h264_nvenc test encode on the GPU; checking
    # for zscale/tonemap must not open an NVENC session as a side effect.
    calls = []

    def no_caps():
        raise AssertionError('the tone-map check must not run ffmpeg_caps (NVENC probe)')

    def fake_run(args, *, timeout_s):
        calls.append(list(args))
        return BoundedResult(0, _FILTERS_WITH_ZIMG, '')

    monkeypatch.setattr(media_bin, 'ffmpeg_caps', no_caps)
    monkeypatch.setattr(media_bin, 'run_ffmpeg', fake_run)
    assert decode._has_tonemap_filters() is True
    assert decode._has_tonemap_filters() is True
    assert calls == [['-filters']]                   # listed once, then remembered


def test_the_tonemap_check_without_zimg_or_after_a_failed_listing(monkeypatch,
                                                                  fresh_filter_listing):
    answers = [BoundedResult(1, '', 'boom'),
               BoundedResult(0, _FILTERS_WITH_ZIMG.replace(' .SC zscale', ' .SC zzz'), '')]
    calls = []

    def fake_run(args, *, timeout_s):
        calls.append(list(args))
        return answers.pop(0)

    def no_caps():
        raise AssertionError('the tone-map check must not run ffmpeg_caps (NVENC probe)')

    monkeypatch.setattr(media_bin, 'ffmpeg_caps', no_caps)
    monkeypatch.setattr(media_bin, 'run_ffmpeg', fake_run)
    assert decode._has_tonemap_filters() is False    # the listing failed: untoned
    assert decode._has_tonemap_filters() is False    # listed again (a failure is not kept)
    assert len(calls) == 2


def test_the_tonemap_check_shares_media_bins_one_filter_listing(monkeypatch,
                                                                fresh_filter_listing):
    # One `ffmpeg -filters` per process, whoever asks first (lane review P7):
    # once media_bin has listed them, decode lists nothing of its own.
    calls = []

    def fake_run(args, *, timeout_s):
        calls.append(list(args))
        return BoundedResult(0, _FILTERS_WITH_ZIMG, '')

    monkeypatch.setattr(media_bin, 'run_ffmpeg', fake_run)
    assert 'zscale' in media_bin.ffmpeg_filters()
    assert decode._has_tonemap_filters() is True
    assert calls == [['-filters']]


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
@pytest.mark.parametrize('size, shape', [
    ((320, 180), (432, 768, 3)),          # 16:9
    ((160, 346), (768, 356, 3)),          # a free portrait aspect: 355.1 px exact
    ((346, 160), (356, 768, 3)),          # and its landscape twin
])
def test_hlg_10_bit_decodes_to_8_bit_bgr(tmp_path, size, shape):
    info = probe(make_clip(tmp_path, 'hlg.mp4', size=size, duration_s=1.0, hdr='hlg'))
    assert info.transfer == 'hlg' and info.bit_depth == 10
    frames = list(iter_frames(info))
    assert len(frames) == 5
    for _, bgr in frames:
        assert bgr.dtype == np.uint8 and bgr.shape == shape
        assert 40 < bgr.mean() < 215          # tone-mapped picture, not black or blown


def _column_luma(path):
    """Mean of each column of the first decoded frame."""
    _, bgr = next(iter(iter_frames(probe(path))))
    return bgr.shape, bgr.astype(np.float64).mean(axis=(0, 2))


@requires_ffmpeg
def test_each_hdr_transfer_is_tone_mapped_by_its_own_curve(tmp_path):
    # One 10-bit HEVC ramp (black at the left, full code at the right),
    # stored twice: tagged HLG, and the SAME bitstream re-tagged PQ (only the
    # VUI transfer changes).  At a free aspect (160x346 -> 356x768).  Each
    # transfer's chain must run, keep the ramp's order, and apply its own
    # curve: PQ codes above ~100 nits compress into SDR white, while HLG's
    # relative curve spreads over the range.
    hlg = make_clip(tmp_path, 'ramp_hlg.mp4', size=(160, 346), duration_s=0.4,
                    source='color', color='black', vf="format=gray,geq=lum='X*255/W'",
                    hdr='hlg')
    pq = str(tmp_path / 'ramp_pq.mp4')
    result = media_bin.run_ffmpeg(['-y', '-loglevel', 'error', '-i', hlg, '-c', 'copy',
                                   '-bsf:v', 'hevc_metadata=transfer_characteristics=16', pq],
                                  timeout_s=120)
    assert result.returncode == 0, result.stderr[-500:]
    assert probe(pq).transfer == 'pq'

    hlg_shape, hlg_cols = _column_luma(hlg)
    pq_shape, pq_cols = _column_luma(pq)
    assert hlg_shape == pq_shape == (768, 356, 3)
    for cols in (hlg_cols, pq_cols):
        assert np.all(np.diff(cols) >= -1.0)            # the ramp keeps its order
        assert cols[:8].mean() < 12 and cols[-8:].mean() > 240
    # Different curves on identical codes.
    assert np.abs(hlg_cols - pq_cols).mean() > 10
    assert np.mean(pq_cols >= 250) > np.mean(hlg_cols >= 250) + 0.1


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
