"""Shared synthetic media for the Reel Studio tests.

Every clip, click track and logo here is generated INSIDE the calling test's
own tmp dir with ffmpeg's lavfi sources, through core.media_bin.run_ffmpeg
(the one runner).  Nothing is downloaded and nothing is written anywhere
else.  A test that needs ffmpeg marks itself ``@requires_ffmpeg``; on a box
without a binary it skips and says why.

Synthetic clips exercise the deterministic chain only (decode, rotation,
HDR flags, cuts, beats, render, QA).  They contain no faces, so face crops
and VLM judgement need real clips (plan U9, E2E-1).

``golden_edl()`` is the reference ``hart.reel.edl.v1`` document that
integrations.reel_studio.contracts.validate_edl accepts.  It is built here,
next to the media fixtures, so the planner, render and pipeline tests start
from the same known-good EDL and mutate one field at a time.
"""
from __future__ import annotations

import copy
import os
from typing import Optional, Sequence, Tuple

import pytest

from core import media_bin

#: One fixture command may take this long.  Every clip here is tiny; the
#: bound exists so a wedged encoder fails the test instead of hanging it.
FIXTURE_TIMEOUT_S = 120.0


def ffmpeg_available() -> bool:
    """True when core.media_bin can locate an ffmpeg binary."""
    return media_bin.get_ffmpeg() is not None


requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(),
    reason=('no ffmpeg binary: core.media_bin found none via IMAGEIO_FFMPEG_EXE, '
            'the imageio-ffmpeg package (pinned in requirements.txt) or PATH'))


def _run(args: Sequence[str]) -> None:
    result = media_bin.run_ffmpeg(list(args), timeout_s=FIXTURE_TIMEOUT_S)
    if result.timed_out or result.returncode != 0:
        raise RuntimeError('fixture ffmpeg failed (rc=%s, timed_out=%s): %s\n%s' % (
            result.returncode, result.timed_out, ' '.join(args), result.stderr[-2000:]))


def make_clip(out_dir, name: str = 'clip.mp4', *,
              size: Tuple[int, int] = (320, 180),
              fps: float = 30,
              duration_s: float = 2.0,
              source: str = 'testsrc2',
              color: str = 'red',
              vf: Optional[str] = None,
              audio: bool = False,
              hdr: Optional[str] = None,
              display_rotation: Optional[int] = None,
              creation_time: Optional[str] = None,
              flash_at_s: Optional[float] = None) -> str:
    """Encode a small synthetic clip and return its absolute path.

    source            'testsrc2' (moving pattern) or 'color' (solid ``color``).
    vf                extra filter chain applied after the source (tints etc.).
    audio             add a 440 Hz AAC track (otherwise the file has no audio).
    hdr               None for 8-bit H.264 SDR, or 'hlg' for 10-bit HEVC
                      tagged bt2020 / arib-std-b67 (Android HLG shape).
    display_rotation  degrees written as a container display matrix
                      (stream copy, so the pixels stay unrotated, exactly
                      like a phone's portrait recording).
    creation_time     container creation_time, e.g. '2026-10-12T05:31:22Z'.
    flash_at_s        one frame filled white at this time (a camera flash).
    """
    w, h = size
    out_dir = os.fspath(out_dir)
    out = os.path.abspath(os.path.join(out_dir, name))
    if source == 'testsrc2':
        src = 'testsrc2=size=%dx%d:rate=%s' % (w, h, fps)
    elif source == 'color':
        src = 'color=c=%s:size=%dx%d:rate=%s' % (color, w, h, fps)
    else:
        raise ValueError('unknown source %r' % (source,))
    chain = []
    if vf:
        chain.append(vf)
    if flash_at_s is not None:
        frame = int(round(flash_at_s * fps))
        chain.append("drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='eq(n\\,%d)'" % frame)

    args = ['-y', '-loglevel', 'error', '-f', 'lavfi', '-i', src]
    if audio:
        args += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000']
    args += ['-t', '%g' % duration_s]
    if chain:
        args += ['-vf', ','.join(chain)]
    if hdr is None:
        args += ['-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p']
    elif hdr == 'hlg':
        args += ['-c:v', 'libx265', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p10le',
                 '-x265-params',
                 'log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc',
                 '-color_primaries', 'bt2020', '-color_trc', 'arib-std-b67',
                 '-colorspace', 'bt2020nc', '-tag:v', 'hvc1']
    else:
        raise ValueError('unknown hdr %r' % (hdr,))
    if audio:
        args += ['-c:a', 'aac', '-shortest']
    else:
        args += ['-an']
    metadata = ['-metadata', 'creation_time=%s' % creation_time] if creation_time else []

    if display_rotation is None:
        _run(args + metadata + [out])
        return out
    # The display matrix is attached on a stream copy, which drops the global
    # metadata, so creation_time is applied on that second step.
    raw = out + '.raw.mp4'
    _run(args + [raw])
    try:
        _run(['-y', '-loglevel', 'error', '-display_rotation', '%d' % display_rotation,
              '-i', raw, '-c', 'copy'] + metadata + [out])
    finally:
        os.remove(raw)
    return out


def concat_clips(out_dir, parts: Sequence[str], name: str = 'concat.mp4') -> str:
    """Join clips made by make_clip with identical settings (stream copy)."""
    out_dir = os.fspath(out_dir)
    listing = os.path.join(out_dir, name + '.txt')
    with open(listing, 'w', encoding='utf-8') as fh:
        for part in parts:
            fh.write("file '%s'\n" % os.path.abspath(part).replace('\\', '/').replace("'", "'\\''"))
    out = os.path.abspath(os.path.join(out_dir, name))
    try:
        _run(['-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', listing,
              '-c', 'copy', out])
    finally:
        os.remove(listing)
    return out


def make_click_track(out_dir, bpm: float, duration_s: float, name: str = 'clicks.wav', *,
                     sample_rate: int = 22050, click_s: float = 0.01,
                     freq_hz: float = 1000.0, offset_s: float = 0.0) -> str:
    """Mono WAV with a short tone burst on every beat of ``bpm``.

    Beats fall at offset_s + k * 60 / bpm.  aevalsrc evaluates the expression
    per sample, so the click positions are exact to the sample.
    """
    period = 60.0 / bpm
    expr = ("if(lt(mod(t-%g\\,%g)\\,%g)*gte(t\\,%g)\\,0.9*sin(2*PI*%g*t)\\,0)"
            % (offset_s, period, click_s, offset_s, freq_hz))
    out = os.path.abspath(os.path.join(os.fspath(out_dir), name))
    _run(['-y', '-loglevel', 'error', '-f', 'lavfi',
          '-i', "aevalsrc='%s':s=%d:d=%g" % (expr, sample_rate, duration_s),
          '-ac', '1', out])
    return out


def make_png(out_dir, name: str = 'logo.png', *, size: Tuple[int, int] = (180, 90),
             color: str = 'white') -> str:
    """A solid-colour PNG (a stand-in logo)."""
    out = os.path.abspath(os.path.join(os.fspath(out_dir), name))
    _run(['-y', '-loglevel', 'error', '-f', 'lavfi',
          '-i', 'color=c=%s:size=%dx%d' % (color, size[0], size[1]), '-frames:v', '1', out])
    return out


# ── The reference EDL ──────────────────────────────────────────────────────

#: Clip ids in the golden EDL (store xids are sha256 hex).
XID_A = 'a' * 64        # portrait 1080x1920, filmed first
XID_B = 'b' * 64        # landscape 1920x1080, filmed 5 minutes later

_GOLDEN_EDL = {
    'schema': 'hart.reel.edl.v1',
    'canvas': {'w': 1080, 'h': 1920, 'fps': 30},
    'target_s': 20.0,
    'duration_s': 20.0,
    'audio': None,
    'final_look_found': True,
    'hook_first': False,
    'capture_src': 'phone',
    'reveal': {'clip': XID_B, 't_s': 8.0},
    'notes': ['capture_order:phone'],
    'beats': {'source': 'synthetic', 'song_key': None, 'bpm': 120.0,
              'times_s': [round(0.5 * k, 3) for k in range(0, 43)],
              'confidence': 0.0},
    'clips': {
        XID_A: {'path': 'C:/reel/work/a.mp4', 'duration_s': 30.0, 'rotation': 90,
                'transfer': 'sdr', 'fps_avg': 29.97, 'display_w': 1080, 'display_h': 1920,
                'hfr': False, 'order': 0, 'captured_at_ms': 1760247082000,
                'capture_src': 'phone'},
        XID_B: {'path': 'C:/reel/work/b.mp4', 'duration_s': 20.0, 'rotation': 0,
                'transfer': 'hlg', 'fps_avg': 30.0, 'display_w': 1920, 'display_h': 1080,
                'hfr': False, 'order': 1, 'captured_at_ms': 1760247382000,
                'capture_src': 'phone'},
    },
    'segments': [
        {'clip': XID_A, 'in_s': 1.0, 'out_s': 4.0, 'speed': 1.0,
         'out_start_s': 0.0, 'out_end_s': 3.0,
         'crop': {'cx': 0.5, 'cy': 0.5, 'zoom': 1.0},
         'face': {'x': 0.4, 'y': 0.2, 'w': 0.2, 'h': 0.15},
         'transition': {'type': 'cut', 'dur_s': 0.0},
         'stage': 'bare_face', 'appeal': 5.0, 'role': 'process'},
        {'clip': XID_A, 'in_s': 6.0, 'out_s': 10.0, 'speed': 1.0,
         'out_start_s': 3.0, 'out_end_s': 7.0,
         'crop': {'cx': 0.5, 'cy': 0.5, 'zoom': 1.0},
         'face': {'x': 0.4, 'y': 0.2, 'w': 0.2, 'h': 0.15},
         'transition': {'type': 'cut', 'dur_s': 0.0},
         'stage': 'base', 'appeal': 6.0, 'role': 'process'},
        {'clip': XID_A, 'in_s': 12.0, 'out_s': 16.0, 'speed': 1.0,
         'out_start_s': 7.0, 'out_end_s': 11.0,
         'crop': {'cx': 0.5, 'cy': 0.5, 'zoom': 1.0},
         'face': None,
         'transition': {'type': 'cut', 'dur_s': 0.0},
         'stage': 'eyes', 'appeal': 6.5, 'role': 'process'},
        {'clip': XID_B, 'in_s': 0.5, 'out_s': 4.5, 'speed': 1.0,
         'out_start_s': 11.0, 'out_end_s': 15.0,
         'crop': {'cx': 0.5, 'cy': 0.5, 'zoom': 1.0},
         'face': {'x': 0.45, 'y': 0.3, 'w': 0.1, 'h': 0.2},
         'transition': {'type': 'cut', 'dur_s': 0.0},
         'stage': 'lips', 'appeal': 7.0, 'role': 'process'},
        {'clip': XID_B, 'in_s': 6.0, 'out_s': 11.0, 'speed': 1.0,
         'out_start_s': 15.0, 'out_end_s': 20.0,
         'crop': {'cx': 0.5, 'cy': 0.5, 'zoom': 1.0},
         'face': {'x': 0.45, 'y': 0.3, 'w': 0.1, 'h': 0.2},
         'transition': {'type': 'cut', 'dur_s': 0.0},
         'stage': 'final_look', 'appeal': 9.0, 'role': 'reveal'},
    ],
    'overlays': [
        {'type': 'text', 'text': 'Bridal look in 20s', 'font': 'auto', 'size_px': 72,
         'color': '#FFFFFF', 'box': True, 'x': 0.5, 'y': 0.18, 'anchor': 'center',
         'from_s': 0.0, 'to_s': 2.0},
        {'type': 'logo', 'src': 'C:/reel/work/logo.png', 'corner': 'top_left',
         'width_px': 180, 'height_px': 90, 'margin_px': 48, 'opacity': 0.9,
         'from_s': 0.0, 'to_s': None},
    ],
}


def golden_edl() -> dict:
    """A fresh deep copy of the reference EDL.

    Two clips in capture order (A filmed before B), five segments in time
    order ending on a 5 s final-look reveal, every cut on the 120 BPM grid,
    20.0 s against a 20 s target, landscape B covered at 1.78x (under the
    1.8 upscale cap), overlays inside the safe zone.
    """
    return copy.deepcopy(_GOLDEN_EDL)
