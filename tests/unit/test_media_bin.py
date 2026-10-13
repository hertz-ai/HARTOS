"""core.media_bin: THE ffmpeg locator and runner (Reel Studio WS-0).

Behavioural: the real module is imported and called; only the boundaries are
replaced (the imageio_ffmpeg module, PATH, the env var, and -- for the caps
logic -- core.subprocess_safe.run_bounded).  The tests marked
``requires_ffmpeg`` drive the real binary on tiny lavfi inputs, including the
timeout paths, so "timeout kills" is measured rather than assumed.
"""
import os
import sys
import threading
import time
import types

import pytest

from core import media_bin
from core.subprocess_safe import BoundedResult
from tests.unit.module_swap import swap_modules
from tests.unit.reel_media_fixtures import (
    concat_clips, make_click_track, make_clip, make_png, requires_ffmpeg)

#: Allowed overrun past a bound on a box other sessions load heavily
#: (same allowance as tests/unit/test_one_bounded_wait.py).
SLACK_S = 4.0

# Excerpts of the real ffmpeg 7.1 (gyan.dev essentials) listings, headers
# included, because the headers are what a naive parser picks up as names.
ENCODERS_TEXT = """Encoders:
 V..... = Video
 A..... = Audio
 S..... = Subtitle
 .F.... = Frame-level multithreading
 ..S... = Slice-level multithreading
 ...X.. = Codec is experimental
 ....B. = Supports draw_horiz_band
 .....D = Supports direct rendering method 1
 ------
 V....D a64multi             Multicolor charset for Commodore 64 (codec a64_multi)
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
 V....D libx265              libx265 H.265 / HEVC (codec hevc)
 A....D aac                  AAC (Advanced Audio Coding)
 S..... ass                  ASS (Advanced SubStation Alpha) subtitle (codec ass)
"""

FILTERS_TEXT = """Filters:
  T.. = Timeline support
  .S. = Slice threading
  ..C = Command support
  A = Audio input/output
  V = Video input/output
  N = Dynamic number and/or type of input/output
  | = Source or sink filter
 TSC aap               AA->A      Apply Affine Projection algorithm to first audio stream.
 T.C cropdetect        V->V       Auto-detect crop size.
 T.C drawtext          V->V       Draw text on top of video frames using libfreetype library.
 ..C scale             V->V       Scale the input video size and/or convert the image format.
 .S. tonemap           V->V       Conversion to/from different dynamic ranges.
 .SC zscale            V->V       Apply resizing, colorspace and bit depth conversion.
 ..C concat            N->N       Concatenate audio and video streams.
 ... testsrc2          |->V       Generate another test pattern.
"""

VERSION_TEXT = ("ffmpeg version 7.1-essentials_build-www.gyan.dev Copyright (c) 2000-2024 "
                "the FFmpeg developers\nbuilt with gcc 14.2.0 (Rev1, Built by MSYS2 project)\n")


@pytest.fixture(autouse=True)
def _fresh_caches():
    """get_ffmpeg / ffmpeg_caps are process-cached; every test starts cold."""
    media_bin.get_ffmpeg.cache_clear()
    media_bin.ffmpeg_caps.cache_clear()
    yield
    media_bin.get_ffmpeg.cache_clear()
    media_bin.ffmpeg_caps.cache_clear()


def _fake_binary(directory, name='ffmpeg'):
    """An executable file shutil.which will resolve (it is never run)."""
    if sys.platform == 'win32':
        name += '.exe'
    path = os.path.join(str(directory), name)
    with open(path, 'wb') as fh:
        fh.write(b'not really ffmpeg')
    os.chmod(path, 0o755)
    return path


def _fake_imageio(get_exe):
    mod = types.ModuleType('imageio_ffmpeg')
    mod.get_ffmpeg_exe = get_exe
    return mod


def _imageio_must_not_be_asked():
    raise AssertionError('imageio_ffmpeg was consulted although the env var named a binary')


# ── locating the binary ───────────────────────────────────────────────────

def test_the_env_var_wins_without_asking_imageio(tmp_path, monkeypatch):
    exe = _fake_binary(tmp_path, 'my-ffmpeg')
    monkeypatch.setenv('IMAGEIO_FFMPEG_EXE', exe)
    with swap_modules({'imageio_ffmpeg': _fake_imageio(_imageio_must_not_be_asked)}):
        assert media_bin.get_ffmpeg() == exe


def test_an_env_var_naming_a_missing_file_is_not_trusted(tmp_path, monkeypatch, caplog):
    """imageio itself returns the env value unchecked ("the user is explicit");
    a typo must not become the binary every later call fails on."""
    missing = str(tmp_path / 'nope' / 'ffmpeg.exe')
    monkeypatch.setenv('IMAGEIO_FFMPEG_EXE', missing)
    monkeypatch.setenv('PATH', str(tmp_path))
    fake = _fake_imageio(lambda: os.environ['IMAGEIO_FFMPEG_EXE'])
    with swap_modules({'imageio_ffmpeg': fake}), caplog.at_level('WARNING'):
        assert media_bin.get_ffmpeg() is None
    assert 'IMAGEIO_FFMPEG_EXE' in caplog.text


def test_the_imageio_bundled_binary_is_used_when_no_env(tmp_path, monkeypatch):
    bundled = _fake_binary(tmp_path, 'ffmpeg-win-x86_64-v7.1')
    monkeypatch.delenv('IMAGEIO_FFMPEG_EXE', raising=False)
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    with swap_modules({'imageio_ffmpeg': _fake_imageio(lambda: bundled)}):
        assert media_bin.get_ffmpeg() == bundled


def test_path_is_the_last_resort(tmp_path, monkeypatch):
    on_path = _fake_binary(tmp_path)
    monkeypatch.delenv('IMAGEIO_FFMPEG_EXE', raising=False)
    monkeypatch.setenv('PATH', str(tmp_path))
    with swap_modules({'imageio_ffmpeg': None}):
        found = media_bin.get_ffmpeg()
    assert found is not None
    assert os.path.normcase(os.path.abspath(found)) == os.path.normcase(on_path)


def test_imageio_finding_nothing_falls_through_to_path(tmp_path, monkeypatch):
    on_path = _fake_binary(tmp_path)
    monkeypatch.delenv('IMAGEIO_FFMPEG_EXE', raising=False)
    monkeypatch.setenv('PATH', str(tmp_path))

    def _none_found():
        raise RuntimeError('No ffmpeg exe could be found.')

    with swap_modules({'imageio_ffmpeg': _fake_imageio(_none_found)}):
        found = media_bin.get_ffmpeg()
    assert os.path.normcase(os.path.abspath(found)) == os.path.normcase(on_path)


def test_none_when_nothing_is_found(tmp_path, monkeypatch):
    monkeypatch.delenv('IMAGEIO_FFMPEG_EXE', raising=False)
    monkeypatch.setenv('PATH', str(tmp_path))
    with swap_modules({'imageio_ffmpeg': None}):
        assert media_bin.get_ffmpeg() is None
        with pytest.raises(media_bin.MediaToolMissing) as err:
            media_bin.require_ffmpeg()
    assert 'IMAGEIO_FFMPEG_EXE' in str(err.value)


def test_a_miss_is_not_remembered_but_a_find_is(tmp_path, monkeypatch):
    """A lookup that found nothing (say imageio's first `ffmpeg -version` ran
    past its bound while antivirus scanned the binary) must not disable
    reels until the next restart; a binary once found is kept."""
    monkeypatch.delenv('IMAGEIO_FFMPEG_EXE', raising=False)
    monkeypatch.setenv('PATH', str(tmp_path))
    with swap_modules({'imageio_ffmpeg': None}):
        assert media_bin.get_ffmpeg() is None
        found = _fake_binary(tmp_path)
        assert os.path.normcase(media_bin.get_ffmpeg()) == os.path.normcase(found)
        os.remove(found)
        assert os.path.normcase(media_bin.get_ffmpeg()) == os.path.normcase(found)


def test_a_wedged_imageio_lookup_is_bounded(tmp_path, monkeypatch):
    """imageio validates its candidates with `ffmpeg -version` and no timeout
    (Gate 7); the lookup is bounded here so a hung binary cannot hang us."""
    release = threading.Event()
    monkeypatch.delenv('IMAGEIO_FFMPEG_EXE', raising=False)
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setattr(media_bin, '_LOCATE_WAIT_S', 0.3)
    fake = _fake_imageio(lambda: release.wait(10) and None)
    try:
        with swap_modules({'imageio_ffmpeg': fake}):
            t0 = time.monotonic()
            found = media_bin.get_ffmpeg()
            elapsed = time.monotonic() - t0
    finally:
        release.set()
    assert found is None
    assert elapsed < 0.3 + SLACK_S


# ── capabilities ──────────────────────────────────────────────────────────

def test_parse_caps_reads_the_real_listing_format():
    caps = media_bin.parse_caps(ENCODERS_TEXT, FILTERS_TEXT, VERSION_TEXT)
    assert {'libx264', 'h264_nvenc', 'libx265', 'aac', 'ass', 'a64multi'} == set(caps.encoders)
    assert {'aap', 'cropdetect', 'drawtext', 'scale', 'tonemap', 'zscale', 'concat',
            'testsrc2'} == set(caps.filters)
    assert caps.version == '7.1-essentials_build-www.gyan.dev'
    assert caps.nvenc_ok is False
    assert isinstance(caps.encoders, frozenset) and isinstance(caps.filters, frozenset)


def test_parse_caps_on_empty_text_finds_nothing():
    caps = media_bin.parse_caps('', '', '')
    assert caps.encoders == frozenset() and caps.filters == frozenset()
    assert caps.version == ''


class _ScriptedFFmpeg:
    """Stands in for run_bounded: answers the listings, records every call."""

    def __init__(self, *, encoders=ENCODERS_TEXT, nvenc_rc=0, listing_rc=0):
        self.calls = []
        self.encoders = encoders
        self.nvenc_rc = nvenc_rc
        self.listing_rc = listing_rc

    def __call__(self, cmd, timeout=5.0, **kwargs):
        self.calls.append((list(cmd), timeout, kwargs))
        if '-encoders' in cmd:
            return BoundedResult(self.listing_rc, self.encoders, '')
        if '-filters' in cmd:
            return BoundedResult(0, FILTERS_TEXT, '')
        if '-version' in cmd:
            return BoundedResult(0, VERSION_TEXT, '')
        if 'h264_nvenc' in cmd:
            err = '' if self.nvenc_rc == 0 else 'Cannot load nvcuda.dll'
            return BoundedResult(self.nvenc_rc, '', err)
        raise AssertionError('unexpected ffmpeg call: %r' % (cmd,))

    def probe_calls(self):
        return [c for c in self.calls if 'h264_nvenc' in c[0]]


def _scripted(monkeypatch, **kw):
    fake = _ScriptedFFmpeg(**kw)
    monkeypatch.setattr(media_bin, 'get_ffmpeg', lambda: 'ffmpeg-under-test')
    monkeypatch.setattr(media_bin.subprocess_safe, 'run_bounded', fake)
    return fake


def test_nvenc_ok_is_false_when_the_probe_encode_fails(monkeypatch):
    fake = _scripted(monkeypatch, nvenc_rc=1)
    caps = media_bin.ffmpeg_caps()
    assert 'h264_nvenc' in caps.encoders
    assert caps.nvenc_ok is False
    [(cmd, timeout, _kw)] = fake.probe_calls()
    assert cmd[0] == 'ffmpeg-under-test'
    assert 0 < timeout <= 60


def test_nvenc_ok_is_true_when_the_probe_encode_succeeds(monkeypatch):
    _scripted(monkeypatch, nvenc_rc=0)
    assert media_bin.ffmpeg_caps().nvenc_ok is True


def test_no_probe_encode_when_nvenc_is_not_listed(monkeypatch):
    without = '\n'.join(l for l in ENCODERS_TEXT.splitlines() if 'nvenc' not in l)
    fake = _scripted(monkeypatch, encoders=without)
    caps = media_bin.ffmpeg_caps()
    assert caps.nvenc_ok is False
    assert fake.probe_calls() == []


def test_caps_are_measured_once_per_process(monkeypatch):
    fake = _scripted(monkeypatch)
    first = media_bin.ffmpeg_caps()
    n = len(fake.calls)
    assert media_bin.ffmpeg_caps() is first
    assert len(fake.calls) == n


def test_a_failed_listing_raises_and_is_not_remembered(monkeypatch):
    fake = _scripted(monkeypatch, listing_rc=1)
    with pytest.raises(media_bin.FFmpegFailed):
        media_bin.ffmpeg_caps()
    fake.listing_rc = 0
    assert 'libx264' in media_bin.ffmpeg_caps().encoders


# ── running ───────────────────────────────────────────────────────────────

def test_run_ffmpeg_runs_the_located_binary_bounded(monkeypatch):
    seen = {}

    def _record(cmd, timeout=5.0, **kwargs):
        seen.update(cmd=list(cmd), timeout=timeout, kwargs=kwargs)
        return BoundedResult(0, '', 'ok')

    monkeypatch.setattr(media_bin, 'get_ffmpeg', lambda: 'ffmpeg-under-test')
    monkeypatch.setattr(media_bin.subprocess_safe, 'run_bounded', _record)
    result = media_bin.run_ffmpeg(['-hide_banner', '-i', 'in.mp4', '-f', 'null', '-'],
                                  timeout_s=12.5)
    assert result.stderr == 'ok'
    cmd = seen['cmd']
    assert cmd[0] == 'ffmpeg-under-test'
    assert cmd.count('-hide_banner') == 1 and cmd.count('-nostdin') == 1
    assert cmd[-5:] == ['-i', 'in.mp4', '-f', 'null', '-']
    assert seen['timeout'] == 12.5
    # ffmpeg's stderr carries UTF-8 metadata (Tamil titles); decoding must not raise.
    assert seen['kwargs'].get('encoding') == 'utf-8'
    assert seen['kwargs'].get('errors') == 'replace'


@pytest.mark.parametrize('bad', [0, -1, float('nan'), float('inf')])
def test_run_ffmpeg_refuses_a_missing_bound(monkeypatch, bad):
    monkeypatch.setattr(media_bin, 'get_ffmpeg', lambda: 'ffmpeg-under-test')
    with pytest.raises(ValueError):
        media_bin.run_ffmpeg(['-version'], timeout_s=bad)


def test_a_vanished_binary_is_reported_as_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(media_bin, 'get_ffmpeg', lambda: str(tmp_path / 'gone.exe'))
    with pytest.raises(media_bin.MediaToolMissing):
        media_bin.run_ffmpeg(['-version'], timeout_s=5)


def test_streaming_without_ffmpeg_fails_at_the_call(monkeypatch):
    monkeypatch.setattr(media_bin, 'get_ffmpeg', lambda: None)
    with pytest.raises(media_bin.MediaToolMissing):
        media_bin.ffmpeg_stream(['-version'], frame_bytes=10, timeout_s=5)


@pytest.mark.parametrize('frame_bytes', [0, -3])
def test_streaming_refuses_a_nonsense_frame_size(monkeypatch, frame_bytes):
    monkeypatch.setattr(media_bin, 'get_ffmpeg', lambda: 'ffmpeg-under-test')
    with pytest.raises(ValueError):
        media_bin.ffmpeg_stream(['-version'], frame_bytes=frame_bytes, timeout_s=5)


# ── the real binary ───────────────────────────────────────────────────────

#: A source that never ends; -re paces it at its own 30 fps so the runaway
#: tests do not spin a core while they wait for the bound.
_ENDLESS = ['-re', '-f', 'lavfi', '-i', 'testsrc2=size=64x36:rate=30']


@requires_ffmpeg
def test_real_listings_parse():
    enc = media_bin.run_ffmpeg(['-encoders'], timeout_s=60)
    flt = media_bin.run_ffmpeg(['-filters'], timeout_s=60)
    ver = media_bin.run_ffmpeg(['-version'], timeout_s=60)
    caps = media_bin.parse_caps(enc.stdout, flt.stdout, ver.stdout)
    assert 'libx264' in caps.encoders
    assert {'scale', 'drawtext', 'concat'} <= caps.filters
    assert caps.version


@requires_ffmpeg
def test_the_timeout_kills_a_runaway_ffmpeg():
    t0 = time.monotonic()
    result = media_bin.run_ffmpeg(_ENDLESS + ['-f', 'null', '-'], timeout_s=1.0)
    elapsed = time.monotonic() - t0
    assert result.timed_out is True
    assert elapsed < 1.0 + SLACK_S


@requires_ffmpeg
def test_stream_yields_whole_frames():
    frame = 64 * 36 * 3
    frames = list(media_bin.ffmpeg_stream(
        ['-f', 'lavfi', '-i', 'testsrc2=size=64x36:rate=5', '-t', '2',
         '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1'],
        frame_bytes=frame, timeout_s=60))
    assert len(frames) == 10
    assert all(len(f) == frame for f in frames)


@requires_ffmpeg
def test_stream_timeout_kills_and_says_so():
    got = 0
    t0 = time.monotonic()
    with pytest.raises(media_bin.FFmpegFailed) as err:
        for _ in media_bin.ffmpeg_stream(
                _ENDLESS + ['-f', 'rawvideo', '-pix_fmt', 'gray', 'pipe:1'],
                frame_bytes=64 * 36, timeout_s=1.5):
            got += 1
    elapsed = time.monotonic() - t0
    assert err.value.timed_out is True
    assert got > 0
    assert elapsed < 1.5 + SLACK_S


@requires_ffmpeg
def test_stream_failure_carries_ffmpegs_own_words(tmp_path):
    missing = str(tmp_path / 'no_such_clip.mp4')
    with pytest.raises(media_bin.FFmpegFailed) as err:
        list(media_bin.ffmpeg_stream(['-i', missing, '-f', 'rawvideo', 'pipe:1'],
                                     frame_bytes=16, timeout_s=30))
    assert err.value.timed_out is False
    assert err.value.returncode not in (0, None)
    assert 'no_such_clip' in err.value.stderr_tail


@requires_ffmpeg
def test_closing_the_stream_early_stops_ffmpeg(monkeypatch):
    media_bin.require_ffmpeg()      # locate first: imageio's lookup also Popens
    started = []
    real_popen = media_bin.subprocess.Popen

    def _recording_popen(*a, **kw):
        proc = real_popen(*a, **kw)
        started.append(proc)
        return proc

    monkeypatch.setattr(media_bin.subprocess, 'Popen', _recording_popen)
    stream = media_bin.ffmpeg_stream(
        _ENDLESS + ['-f', 'rawvideo', '-pix_fmt', 'gray', 'pipe:1'],
        frame_bytes=64 * 36, timeout_s=60)
    assert len(next(stream)) == 64 * 36
    t0 = time.monotonic()
    stream.close()
    assert time.monotonic() - t0 < SLACK_S
    [proc] = started
    assert proc.poll() is not None


# ── the shared fixtures make what they say ────────────────────────────────

def _info(path):
    """ffmpeg's own description of a file (it exits 1: no output named)."""
    return media_bin.run_ffmpeg(['-i', path], timeout_s=60).stderr


@requires_ffmpeg
def test_fixture_clip_carries_rotation_creation_time_audio_and_rate(tmp_path):
    path = make_clip(tmp_path, 'phone.mp4', fps=60, duration_s=1.0, audio=True,
                     display_rotation=90, creation_time='2026-10-12T05:31:22Z')
    info = _info(path)
    assert 'displaymatrix: rotation of' in info and '90.00 degrees' in info
    assert '2026-10-12T05:31:22' in info
    assert 'Audio: aac' in info
    assert '60 fps' in info
    assert not os.path.exists(path + '.raw.mp4')


@requires_ffmpeg
def test_fixture_hlg_clip_is_ten_bit_hlg(tmp_path):
    info = _info(make_clip(tmp_path, 'hlg.mp4', duration_s=0.5, hdr='hlg'))
    assert 'hevc' in info and 'yuv420p10le' in info and 'arib-std-b67' in info
    assert 'Audio' not in info


@requires_ffmpeg
def test_fixture_flash_is_exactly_one_white_frame(tmp_path):
    path = make_clip(tmp_path, 'flash.mp4', size=(32, 18), fps=10, duration_s=2.0,
                     source='color', color='black', flash_at_s=1.0)
    means = [sum(f) / len(f) for f in media_bin.ffmpeg_stream(
        ['-i', path, '-f', 'rawvideo', '-pix_fmt', 'gray', 'pipe:1'],
        frame_bytes=32 * 18, timeout_s=60)]
    assert len(means) == 20
    bright = [i for i, m in enumerate(means) if m > 200]
    assert bright == [10]


@requires_ffmpeg
def test_fixture_concat_click_track_and_png(tmp_path):
    parts = [make_clip(tmp_path, '%s.mp4' % c, source='color', color=c, duration_s=1.0)
             for c in ('red', 'green', 'blue')]
    assert 'Duration: 00:00:03.00' in _info(concat_clips(tmp_path, parts))
    clicks = _info(make_click_track(tmp_path, bpm=120, duration_s=2.0))
    assert 'Duration: 00:00:02.00' in clicks and '22050 Hz' in clicks and 'mono' in clicks
    logo = _info(make_png(tmp_path, size=(180, 90)))
    assert 'png' in logo and '180x90' in logo
