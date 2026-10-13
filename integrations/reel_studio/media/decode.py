"""integrations.reel_studio.media.decode -- one ffmpeg pipe per clip.

``iter_frames`` decodes a probed clip ONCE, at a low analysis rate (5 fps),
into 8-bit BGR frames at a 768 px long side, in display orientation:

    -map 0:V:0                    first video stream that is not cover art
    fps=5:round=up                the frame ON SCREEN at k/5 s (see below)
    scale=W:H:flags=area          display aspect, 768 px long side
    <tone-map chain>              HDR only, AFTER the downscale (8 s vs 20 s
                                  for 5 s of 4K HLG, report_media-models 4)
    -pix_fmt bgr24 -f rawvideo pipe:1

ffmpeg's autorotate turns the frame by its display matrix before the
filters run, so the frames are upright and the scale target uses the
display size.  cv2's own FFmpeg (4.4) is not used: it does not tone-map,
and one decoder keeps analysis and render seeing the same pixels.

``round=up`` was measured on the pinned ffmpeg 7.1: with the default
``round=near`` the fps filter emits source frame 6k+2 for slot k of a 30 fps
clip (the image lags its timestamp by 67 ms, and a one-frame flash at 1.0 s
is never sampled); with ``round=up`` it emits frame 6k, the one on screen at
exactly k/5 s (floor(k*0.2*29.97) at 29.97 fps).  So a frame's time here is
a source time the renderer can seek to.

``tonemap_filter`` is THE HDR->SDR chain; the renderer (WS-4) uses the same
string so analysis and render tone-map alike.
"""
from __future__ import annotations

import logging
from typing import Iterator, Optional, Tuple

import numpy as np

from core import media_bin
from integrations.reel_studio.contracts import TRANSFERS, ClipInfo

logger = logging.getLogger(__name__)

#: Analysis rate and frame size (plan WS-1; keyframes go to the VLM at 768).
DECODE_FPS = 5.0
LONG_SIDE = 768
#: The decode bound: a fixed allowance plus this many seconds per second of
#: clip.  4K 10-bit HEVC decodes at about 2.3x real time on this desktop
#: (report_media-models 4), so 2 s per second leaves room for a busy box.
DECODE_TIMEOUT_BASE_S = 60.0
DECODE_TIMEOUT_PER_S = 2.0

#: zscale input transfer per HDR transfer.
_TIN = {'hlg': 'arib-std-b67', 'pq': 'smpte2084'}


def frame_size(info: ClipInfo, *, long_side: int = LONG_SIDE) -> Tuple[int, int]:
    """(width, height) of the decoded frames: display aspect, ``long_side`` long."""
    dw, dh = float(info.display_w), float(info.display_h)
    scale = long_side / max(dw, dh)
    return max(2, int(round(dw * scale))), max(2, int(round(dh * scale)))


def tonemap_filter(transfer: str) -> str:
    """The HDR->SDR (BT.709) filter chain for ``transfer``; '' for SDR.

    Linearise with zscale, tone-map with Hable in float RGB, then back to
    BT.709 limited range.  Phone HDR is BT.2020, so the input primaries and
    matrix are stated rather than trusted to per-frame tags.
    """
    if transfer not in TRANSFERS:
        raise ValueError('transfer must be one of %s, got %r' % (TRANSFERS, transfer))
    if transfer == 'sdr':
        return ''
    return ('zscale=tin=%s:pin=bt2020:min=bt2020nc:t=linear:npl=100,format=gbrpf32le,'
            'zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv'
            % _TIN[transfer])


def _has_tonemap_filters() -> bool:
    """True when the located ffmpeg has zscale and tonemap (zimg builds)."""
    try:
        filters = media_bin.ffmpeg_caps().filters
    except media_bin.FFmpegFailed as e:
        logger.warning('could not list ffmpeg filters (%s); decoding HDR without tone-mapping', e)
        return False
    return 'zscale' in filters and 'tonemap' in filters


def _filter_chain(info: ClipInfo, fps: float, size: Tuple[int, int]) -> str:
    chain = ['fps=%s:round=up' % _num(fps), 'scale=%d:%d:flags=area' % size]
    if info.transfer != 'sdr':
        if _has_tonemap_filters():
            chain.append(tonemap_filter(info.transfer))
        else:
            logger.warning('%s is %s but this ffmpeg has no zscale/tonemap; analysing it '
                           'untoned (flat colours)', info.path, info.transfer)
    return ','.join(chain)


def _num(value: float) -> str:
    return ('%.6f' % value).rstrip('0').rstrip('.')


def iter_frames(info: ClipInfo, *, fps: float = DECODE_FPS, long_side: int = LONG_SIDE,
                timeout_s: Optional[float] = None) -> Iterator[Tuple[float, np.ndarray]]:
    """Yield (t_s, bgr) for ``info``'s clip: uint8 HxWx3, read-only, upright.

    ``t_s`` is k/fps, the source time the frame is on screen.  Arguments and
    the ffmpeg binary are checked now; decoding starts on the first next().
    Raises media_bin.FFmpegFailed if ffmpeg fails or passes ``timeout_s``
    (default: DECODE_TIMEOUT_BASE_S + DECODE_TIMEOUT_PER_S * duration).
    Closing the iterator early stops ffmpeg.
    """
    if not (fps > 0):
        raise ValueError('fps must be > 0, got %r' % (fps,))
    if int(long_side) < 2:
        raise ValueError('long_side must be >= 2, got %r' % (long_side,))
    size = frame_size(info, long_side=int(long_side))
    bound = timeout_s if timeout_s is not None else (
        DECODE_TIMEOUT_BASE_S + DECODE_TIMEOUT_PER_S * max(0.0, float(info.duration_s)))
    args = ['-loglevel', 'error', '-i', info.path, '-map', '0:V:0', '-an', '-sn', '-dn',
            '-vf', _filter_chain(info, fps, size),
            '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1']
    w, h = size
    stream = media_bin.ffmpeg_stream(args, frame_bytes=w * h * 3, timeout_s=bound)
    return _frames(stream, w, h, float(fps))


def _frames(stream, w: int, h: int, fps: float) -> Iterator[Tuple[float, np.ndarray]]:
    try:
        for k, chunk in enumerate(stream):
            yield k / fps, np.frombuffer(chunk, np.uint8).reshape(h, w, 3)
    finally:
        stream.close()


__all__ = ['DECODE_FPS', 'LONG_SIDE', 'DECODE_TIMEOUT_BASE_S', 'DECODE_TIMEOUT_PER_S',
           'frame_size', 'tonemap_filter', 'iter_frames']
