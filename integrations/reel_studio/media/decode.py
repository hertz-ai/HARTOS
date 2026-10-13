"""integrations.reel_studio.media.decode -- one ffmpeg pipe per clip.

``iter_frames`` decodes a probed clip ONCE, at a low analysis rate (5 fps),
into 8-bit BGR frames at a 768 px long side, in display orientation:

    -map 0:V:0                    first video stream that is not cover art
    fps=5:round=up                the frame ON SCREEN at k/5 s (see below)
    scale=W:H:flags=area          display aspect, 768 px long side, W and H even
    <tone-map chain>              HDR only, AFTER the downscale (measured on
                                  5 s of 4K HLG: 8 s instead of 20 s)
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

EVEN FRAME SIZES.  The scaled frame is still 4:2:0, and zscale refuses a
4:2:0 frame whose width or height is odd ("image dimensions must be
divisible by subsampling factor"), so ``frame_size`` rounds both sides to
the nearest even number.  Without that, a free display aspect (2336x1080,
1792x828, a gallery crop) gave a 355 px side and the HDR decode failed
outright; 16:9, 4:3 and 1:1 happen to land on even sizes.

``tonemap_filter`` is THE HDR->SDR chain; the renderer (WS-4) uses the same
string so analysis and render tone-map alike, under the same even-size
precondition.  Whether this ffmpeg has the filters for it comes from its
``-filters`` listing alone (``media_bin.ffmpeg_caps()`` would also run an
NVENC test encode on the GPU).
"""
from __future__ import annotations

import functools
import logging
from typing import Iterator, Optional, Tuple

import numpy as np

from core import media_bin
from integrations.reel_studio.contracts import TRANSFERS, ClipInfo

logger = logging.getLogger(__name__)

#: Analysis rate and frame size (plan WS-1; keyframes go to the VLM at 768).
DECODE_FPS = 5.0
LONG_SIDE = 768
#: The decode bound: a fixed allowance plus DECODE_TIMEOUT_PER_S per second
#: of clip at up to DECODE_TIMEOUT_REF_FPS.  4K 10-bit HEVC at 30 fps decodes
#: at about 2.3x real time on the development desktop, so 2 s per second
#: leaves room for a busy box.  The decoder decodes every source frame before
#: the fps filter drops most of them, so a faster source scales the
#: allowance (a 240 fps slow-motion clip gets 8x).
DECODE_TIMEOUT_BASE_S = 60.0
DECODE_TIMEOUT_PER_S = 2.0
DECODE_TIMEOUT_REF_FPS = 30.0
#: Bound on the one ``ffmpeg -filters`` listing.
FILTERS_TIMEOUT_S = 30.0

#: zscale input transfer per HDR transfer.
_TIN = {'hlg': 'arib-std-b67', 'pq': 'smpte2084'}


def _even(value: float) -> int:
    """The nearest even integer, at least 2."""
    return max(2, 2 * int(round(value / 2.0)))


def frame_size(info: ClipInfo, *, long_side: int = LONG_SIDE) -> Tuple[int, int]:
    """(width, height) of the decoded frames: display aspect, ``long_side`` long.

    Both are even (4:2:0 chroma, and zscale in the HDR chain, need it), so
    the short side is within one pixel of the exact aspect.  ``long_side``
    must be an even number >= 2; anything else raises ValueError.
    """
    side = int(long_side)
    if side < 2 or side % 2:
        raise ValueError('long_side must be an even number >= 2, got %r' % (long_side,))
    dw, dh = float(info.display_w), float(info.display_h)
    scale = side / max(dw, dh)
    return _even(dw * scale), _even(dh * scale)


def tonemap_filter(transfer: str) -> str:
    """The HDR->SDR (BT.709) filter chain for ``transfer``; '' for SDR.

    Linearise with zscale, tone-map with Hable in float RGB, then back to
    BT.709 limited range.  Phone HDR is BT.2020, so the input primaries and
    matrix are stated rather than trusted to per-frame tags.

    PRECONDITION: the frame entering the chain must have an EVEN width and
    an EVEN height.  Phone HDR decodes as 4:2:0, and zscale fails the whole
    run on an odd dimension ("image dimensions must be divisible by
    subsampling factor").  Scale to even sizes first, as iter_frames does.
    """
    if transfer not in TRANSFERS:
        raise ValueError('transfer must be one of %s, got %r' % (TRANSFERS, transfer))
    if transfer == 'sdr':
        return ''
    return ('zscale=tin=%s:pin=bt2020:min=bt2020nc:t=linear:npl=100,format=gbrpf32le,'
            'zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv'
            % _TIN[transfer])


@functools.lru_cache(maxsize=1)
def _ffmpeg_filters() -> frozenset:
    """The located ffmpeg's filter names, listed once (a failure is not kept).

    Only ``-filters`` runs, parsed by media_bin.parse_caps: ffmpeg_caps()
    would also open an h264_nvenc session on the GPU for its test encode.
    """
    result = media_bin.run_ffmpeg(['-filters'], timeout_s=FILTERS_TIMEOUT_S)
    if result.timed_out or result.returncode != 0:
        raise media_bin.FFmpegFailed('ffmpeg -filters failed', returncode=result.returncode,
                                     stderr_tail=result.stderr[-2000:],
                                     timed_out=result.timed_out)
    return media_bin.parse_caps('', result.stdout, '').filters


def _has_tonemap_filters() -> bool:
    """True when the located ffmpeg has zscale and tonemap (zimg builds)."""
    try:
        filters = _ffmpeg_filters()
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


def decode_bound_s(info: ClipInfo) -> float:
    """The default bound on decoding ``info``'s clip (see DECODE_TIMEOUT_*)."""
    load = max(1.0, float(info.fps_avg) / DECODE_TIMEOUT_REF_FPS)
    return DECODE_TIMEOUT_BASE_S + DECODE_TIMEOUT_PER_S * max(0.0, float(info.duration_s)) * load


def iter_frames(info: ClipInfo, *, fps: float = DECODE_FPS, long_side: int = LONG_SIDE,
                timeout_s: Optional[float] = None) -> Iterator[Tuple[float, np.ndarray]]:
    """Yield (t_s, bgr) for ``info``'s clip: uint8 HxWx3, read-only, upright.

    ``t_s`` is k/fps, the source time the frame is on screen; the size is
    frame_size(info, long_side=long_side), both sides even.  Arguments and
    the ffmpeg binary are checked now; decoding starts on the first next().
    Raises ValueError for fps <= 0 or a long_side that is not an even number
    >= 2, and media_bin.FFmpegFailed if ffmpeg fails or passes ``timeout_s``
    (default: decode_bound_s(info)).  Closing the iterator early stops ffmpeg.
    """
    if not (fps > 0):
        raise ValueError('fps must be > 0, got %r' % (fps,))
    size = frame_size(info, long_side=long_side)
    bound = timeout_s if timeout_s is not None else decode_bound_s(info)
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
           'DECODE_TIMEOUT_REF_FPS', 'FILTERS_TIMEOUT_S', 'frame_size', 'tonemap_filter',
           'decode_bound_s', 'iter_frames']
