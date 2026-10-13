"""integrations.reel_studio.media.probe -- what a phone clip is, from ``ffmpeg -i``.

There is no ffprobe in this stack (core/media_bin), so the facts come from
the stderr of ``ffmpeg -hide_banner -i <clip>``, which ffmpeg prints before
it complains that no output was given.  ``parse_ffmpeg_info`` is the pure
parser; ``probe`` runs the binary through core.media_bin.run_ffmpeg.

What is read (ffmpeg 7.1 wording; older wording where noted):

* the first video stream that is not an attached picture (cover art):
  codec, coded size, pixel format (-> bit depth), colour transfer, average
  frame rate (``fps``, else ``tbr``; ``1k`` means 1000), sample aspect;
* ``displaymatrix: rotation of A degrees`` -- A is counter-clockwise, so
  ``ClipInfo.rotation`` (clockwise, the classic ``rotate`` tag, what Android
  calls the video rotation) is ``-A`` rounded to a right angle; ffmpeg before
  5.0 printed a ``rotate : 90`` stream tag instead, which is read too;
* ``DOVI configuration record: ... profile: P ...`` -- profile 5 has no
  SDR/HLG base layer, so the clip is refused (``dolby_vision_p5``); other
  profiles decode their base layer and keep its transfer;
* whether any audio stream exists;
* the container's ``creation_time`` (UTC), else Apple's
  ``com.apple.quicktime.creationdate`` (local time with an offset), else the
  video stream's ``creation_time``, as epoch seconds.  Plausibility is
  judged later by contracts.resolve_capture_order (A1 capture-order
  fallback), not here.

``hfr`` is ``fps_avg >= contracts.HFR_MIN_FPS``.  There is no ``is_vfr``:
the render always forces 30 fps CFR (plan finding 31).
"""
from __future__ import annotations

import calendar
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

from core import media_bin
from integrations.reel_studio.contracts import HFR_MIN_FPS, ClipInfo, ClipUnsupported

logger = logging.getLogger(__name__)

#: Bound on one ``ffmpeg -i``.  It reads only the container header, but the
#: first run of an 88 MB binary can wait on an antivirus scan.
PROBE_TIMEOUT_S = 30.0

#: ClipUnsupported reasons this module raises.
UNREADABLE = 'unreadable'          # ffmpeg cannot open it, or it has no duration
NO_VIDEO = 'no_video'              # no video stream (cover art does not count)
DOVI_P5 = 'dolby_vision_p5'        # Dolby Vision profile 5: no SDR/HLG base layer
UNSUPPORTED_REASONS = (UNREADABLE, NO_VIDEO, DOVI_P5)

_TRANSFER_BY_TRC = {'arib-std-b67': 'hlg', 'smpte2084': 'pq'}

_INPUT = re.compile(r'^Input #0\b')
_STREAM = re.compile(r'^\s*Stream #0:\d+\S*: (\w+): (.*)$')
_DURATION = re.compile(r'^\s*Duration: (N/A|(\d+):(\d{2}):(\d{2}(?:\.\d+)?))')
_META = re.compile(r'^\s+([\w.\-]+)\s*: (.*)$')
_MATRIX = re.compile(r'displaymatrix: rotation of (-?\d+(?:\.\d+)?) degrees')
_DOVI = re.compile(r'DOVI configuration record: .*?profile: (\d+)')
_SIZE = re.compile(r'^(\d+)x(\d+)(?:\s*\[SAR (\d+):(\d+) DAR \d+:\d+\])?')
_SAR = re.compile(r'^SAR (\d+):(\d+) DAR \d+:\d+$')
_RATE = re.compile(r'^(\d+(?:\.\d+)?)(k?) (fps|tbr)$')
_BIT_DEPTH = re.compile(r'p0?(\d{2})(?:le|be)?$')
_STAMP = re.compile(r'^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?'
                    r'\s*(Z|[+-]\d{2}:?\d{2})?$')
_RANGE_OR_FIELD = frozenset({'tv', 'pc', 'progressive', 'top first', 'bottom first',
                             'top coded first (swapped)', 'bottom coded first (swapped)'})


# ── small parsers ─────────────────────────────────────────────────────────

def _split_top(text: str) -> List[str]:
    """Split on ', ' outside () and [] (ffmpeg's stream summary is nested)."""
    parts, depth, cur = [], 0, []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in '([':
            depth += 1
        elif ch in ')]':
            depth = max(0, depth - 1)
        if depth == 0 and text.startswith(', ', i):
            parts.append(''.join(cur).strip())
            cur = []
            i += 2
            continue
        cur.append(ch)
        i += 1
    if cur:
        parts.append(''.join(cur).strip())
    return parts


def _epoch_seconds(stamp: str) -> Optional[float]:
    """'2026-10-12T05:31:22.000000Z' / '...+0530' -> epoch seconds (no zone = UTC)."""
    m = _STAMP.match(stamp.strip())
    if not m:
        return None
    y, mo, d, h, mi, s = (int(v) for v in m.groups()[:6])
    try:
        seconds = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))
    except (ValueError, OverflowError):
        return None
    zone = m.group(7)
    if zone and zone != 'Z':
        sign = -1 if zone[0] == '-' else 1
        digits = zone[1:].replace(':', '')
        seconds -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return float(seconds)


def _rate(token: str) -> Optional[Tuple[str, float]]:
    m = _RATE.match(token)
    if not m:
        return None
    value = float(m.group(1)) * (1000.0 if m.group(2) else 1.0)
    return m.group(3), value


def _bit_depth(pix_fmt: str) -> int:
    m = _BIT_DEPTH.search(pix_fmt)
    return int(m.group(1)) if m else 8


def _transfer(color: str) -> str:
    """The transfer named in a pixel format's '(range, space/primaries/trc, ...)' part."""
    for part in (p.strip() for p in color.split(',')):
        if not part or part in _RANGE_OR_FIELD:
            continue
        names = part.split('/')
        trc = names[2] if len(names) == 3 else names[0]
        return _TRANSFER_BY_TRC.get(trc, 'sdr')
    return 'sdr'


def _clockwise(ccw_degrees: float) -> int:
    """displaymatrix's counter-clockwise angle -> clockwise right angle 0/90/180/270."""
    return int(round(-ccw_degrees / 90.0)) % 4 * 90


# ── the stderr layout ─────────────────────────────────────────────────────

class _Stream:
    def __init__(self, kind: str, summary: str) -> None:
        self.kind = kind
        self.summary = summary
        self.meta: Dict[str, str] = {}
        self.side: List[str] = []


def _sections(stderr: str):
    """(global metadata, duration text, streams) of the first input."""
    lines = stderr.splitlines()
    start = next((i for i, line in enumerate(lines) if _INPUT.match(line)), None)
    if start is None:
        return None
    meta: Dict[str, str] = {}
    duration = None
    streams: List[_Stream] = []
    current = None
    block = 'global'
    for line in lines[start + 1:]:
        if line.startswith('Input #') or line.startswith('Output #') or not line.startswith(' '):
            break
        m = _STREAM.match(line)
        if m:
            current = _Stream(m.group(1), m.group(2))
            streams.append(current)
            block = 'stream'
            continue
        d = _DURATION.match(line)
        if d and current is None:
            duration = d
            continue
        stripped = line.strip()
        if stripped in ('Metadata:', 'Side data:'):
            block = stripped
            continue
        if current is None:
            mm = _META.match(line)
            if mm and block == 'Metadata:':
                meta.setdefault(mm.group(1), mm.group(2).strip())
            continue
        if block == 'Side data:':
            current.side.append(stripped)
        else:
            mm = _META.match(line)
            if mm:
                current.meta.setdefault(mm.group(1), mm.group(2).strip())
    return meta, duration, streams


def _last_line(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1][-300:] if lines else 'ffmpeg printed nothing'


def _duration_s(match) -> Optional[float]:
    if match is None or match.group(1) == 'N/A':
        return None
    h, m, s = int(match.group(2)), int(match.group(3)), float(match.group(4))
    return h * 3600 + m * 60 + s


def _creation_time(meta: Dict[str, str], video: _Stream) -> Optional[float]:
    for value in (meta.get('creation_time'), meta.get('com.apple.quicktime.creationdate'),
                  video.meta.get('creation_time')):
        if value:
            seconds = _epoch_seconds(value)
            if seconds is not None:
                return seconds
    return None


# ── public ────────────────────────────────────────────────────────────────

def parse_ffmpeg_info(stderr: str, *, path: str = '') -> ClipInfo:
    """Pure: ``ffmpeg -i`` stderr -> ClipInfo for ``path``.

    Raises ClipUnsupported('unreadable') when ffmpeg could not open the file
    or gave no duration, ('no_video') when no video stream exists, and
    ('dolby_vision_p5') for Dolby Vision profile 5.
    """
    sections = _sections(stderr or '')
    if sections is None:
        raise ClipUnsupported(UNREADABLE, _last_line(stderr or ''))
    meta, duration_match, streams = sections
    video = next((s for s in streams if s.kind == 'Video' and '(attached pic)' not in s.summary),
                 None)
    if video is None:
        raise ClipUnsupported(NO_VIDEO, 'no video stream in %d stream(s)' % len(streams))
    duration = _duration_s(duration_match)
    if not duration or duration <= 0:
        raise ClipUnsupported(UNREADABLE, 'ffmpeg reports no duration')

    for side in video.side:
        m = _DOVI.search(side)
        if m and int(m.group(1)) == 5:
            raise ClipUnsupported(DOVI_P5, 'Dolby Vision profile 5 has no SDR or HLG base layer')
    dovi = next((int(m.group(1)) for m in map(_DOVI.search, video.side) if m), None)

    tokens = _split_top(video.summary)
    codec = tokens[0].split()[0] if tokens and tokens[0] else ''
    pix_fmt, color = '', ''
    if len(tokens) > 1:
        head, _, rest = tokens[1].partition('(')
        pix_fmt, color = head.strip(), rest.rstrip(')')
    width = height = 0
    sar = (1, 1)
    rates: Dict[str, float] = {}
    for token in tokens[1:]:
        size = _SIZE.match(token)
        if size and not width:
            width, height = int(size.group(1)), int(size.group(2))
            if size.group(3):
                sar = (int(size.group(3)), int(size.group(4)))
            continue
        aspect = _SAR.match(token)
        if aspect:
            sar = (int(aspect.group(1)), int(aspect.group(2)))
            continue
        rate = _rate(token)
        if rate:
            rates.setdefault(rate[0], rate[1])
    fps = rates.get('fps') or rates.get('tbr')
    if not (width and height):
        raise ClipUnsupported(UNREADABLE, 'no frame size in %r' % video.summary[:200])
    if not fps:
        raise ClipUnsupported(UNREADABLE, 'no frame rate in %r' % video.summary[:200])

    rotation = 0
    matrix = next((m for m in map(_MATRIX.search, video.side) if m), None)
    if matrix:
        rotation = _clockwise(float(matrix.group(1)))
    elif video.meta.get('rotate'):
        try:
            rotation = int(round(float(video.meta['rotate']) / 90.0)) % 4 * 90
        except ValueError:
            logger.debug('ignoring rotate tag %r', video.meta['rotate'])
    display_w = width
    if sar[0] > 0 and sar[1] > 0 and sar[0] != sar[1]:
        display_w = int(round(width * sar[0] / float(sar[1])))
    display_h = height
    if rotation in (90, 270):
        display_w, display_h = display_h, display_w

    return ClipInfo(path=path, duration_s=duration, width=width, height=height,
                    display_w=display_w, display_h=display_h, rotation=rotation,
                    fps_avg=float(fps), codec=codec, pix_fmt=pix_fmt,
                    bit_depth=_bit_depth(pix_fmt), transfer=_transfer(color),
                    dovi_profile=dovi, hfr=float(fps) >= HFR_MIN_FPS,
                    has_audio=any(s.kind == 'Audio' for s in streams),
                    creation_time=_creation_time(meta, video))


def probe(path: str, *, timeout_s: float = PROBE_TIMEOUT_S) -> ClipInfo:
    """ClipInfo of the clip at ``path`` (made absolute).

    Raises ClipUnsupported (see parse_ffmpeg_info; a missing file is
    'unreadable' without running ffmpeg), media_bin.FFmpegFailed when ffmpeg
    hit its bound, and media_bin.MediaToolMissing when there is no ffmpeg.
    """
    path = os.path.abspath(os.fspath(path))
    if not os.path.isfile(path):
        raise ClipUnsupported(UNREADABLE, 'no such file')
    result = media_bin.run_ffmpeg(['-i', path], timeout_s=timeout_s)
    if result.timed_out:
        raise media_bin.FFmpegFailed('ffmpeg -i did not finish within %.1f s' % timeout_s,
                                     returncode=result.returncode,
                                     stderr_tail=result.stderr[-2000:], timed_out=True)
    return parse_ffmpeg_info(result.stderr, path=path)


__all__ = ['PROBE_TIMEOUT_S', 'UNREADABLE', 'NO_VIDEO', 'DOVI_P5', 'UNSUPPORTED_REASONS',
           'parse_ffmpeg_info', 'probe']
