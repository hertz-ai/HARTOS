"""integrations.reel_studio.media.fonts -- which font file draws her on-screen text.

ffmpeg's drawtext needs a font FILE (``fontfile=``); it is not told to look
one up by name, so the result does not depend on how fontconfig is set up.

* Text in an Indic script (Devanagari, Bengali, Gurmukhi, Gujarati, Oriya,
  Tamil, Telugu, Kannada, Malayalam, Sinhala) needs a font with those
  glyphs.  V1 uses Windows' own Nirmala UI (``%WINDIR%\\Fonts\\Nirmala.ttc``
  on Windows 11, ``Nirmala.ttf``/``NirmalaB.ttf`` on Windows 10), which also
  covers Latin, so mixed Tamil and English text draws with one font.  No
  font is bundled yet (the Noto fonts' licence is still to be approved), so
  on Linux and macOS Indic text has no font and the renderer leaves that
  overlay out with a note instead of drawing empty boxes.
* Latin-only text uses a bold system sans: Segoe UI or Arial on Windows,
  DejaVu or Liberation Sans on Linux, Arial or Helvetica on macOS.

drawtext cannot draw colour emoji either (they come out as boxes), so
``drop_emoji`` removes them first, using igkit.caption.EMOJI_RE, the one
definition of "an emoji as she sees it" (code point, variation selector,
skin tone and zero-width-joined parts together).

Which code points need an Indic font is a font-coverage question, not
language detection, so this module checks Unicode blocks itself rather than
calling the TTS router's language guesser.

How wide a line is drawn (``advance_em``) is read from the font file itself:
its ``cmap`` and ``hmtx`` tables, face 0 of a collection (the face drawtext
opens).  drawtext lays each character as that character's own glyph at the
glyph's advance width: measured 2026-10-15 with this repo's ffmpeg 7.1, the
box behind a line is the sum of its characters' advances to within 1 px a
character at 100 px, for every printable ASCII character in Segoe UI Bold,
Arial Bold, DejaVu Sans Bold and Nirmala UI and for every Tamil code point in
Nirmala UI (its vowel signs at their own glyphs' advances, the ones drawn
without an advance at none).  The tables are read with ``struct``: Pillow's
FreeType text path (the pinned 9.5.0 under Python 3.12) dies with an access
violation in ``getlength`` on every font tried, so no font is opened through
it in-process.
"""
from __future__ import annotations

import bisect
import functools
import logging
import os
import re
import struct
import sys
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from integrations.reel_studio.igkit.caption import EMOJI_RE

logger = logging.getLogger(__name__)

#: Unicode blocks Nirmala UI covers that Latin fonts do not: Devanagari
#: (U+0900) through Sinhala (U+0DFF).
INDIC_RANGES: Tuple[Tuple[int, int], ...] = ((0x0900, 0x0DFF),)

#: Font files tried, in order, per platform (file names under the Windows
#: fonts directory; absolute paths elsewhere).
WINDOWS_INDIC = ('Nirmala.ttc', 'NirmalaB.ttf', 'Nirmala.ttf')
WINDOWS_LATIN = ('segoeuib.ttf', 'arialbd.ttf', 'segoeui.ttf', 'arial.ttf') + WINDOWS_INDIC
LINUX_LATIN = (
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/TTF/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
    '/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf',
    '/run/current-system/sw/share/X11/fonts/DejaVuSans-Bold.ttf',
)
MAC_LATIN = (
    '/System/Library/Fonts/Supplemental/Arial Bold.ttf',
    '/Library/Fonts/Arial Bold.ttf',
    '/System/Library/Fonts/Helvetica.ttc',
)

_SPACES = re.compile(r'\s+')
#: Joiners and selectors left behind once the emoji around them are gone.
_ORPHANS = re.compile('[\u200d\ufe0f\u2640\u2642]')


def needs_indic_font(text: str) -> bool:
    """True when ``text`` has a character from an Indic script block."""
    return any(lo <= ord(ch) <= hi for ch in text for lo, hi in INDIC_RANGES)


def drop_emoji(text: str) -> str:
    """``text`` without emoji (and the joiners they leave), spaces collapsed."""
    cleaned = _ORPHANS.sub('', EMOJI_RE.sub(' ', text))
    return _SPACES.sub(' ', cleaned).strip()


def _windows_fonts_dir() -> str:
    return os.path.join(os.environ.get('WINDIR') or os.environ.get('SystemRoot')
                        or 'C:\\Windows', 'Fonts')


def font_candidates(text: str, *, platform: Optional[str] = None) -> List[str]:
    """The font files that could draw ``text`` on ``platform``, best first."""
    platform = platform or sys.platform
    indic = needs_indic_font(text)
    if platform == 'win32':
        names = WINDOWS_INDIC if indic else WINDOWS_LATIN
        return [os.path.join(_windows_fonts_dir(), name) for name in names]
    if indic:
        return []
    return list(MAC_LATIN if platform == 'darwin' else LINUX_LATIN)


def resolve_font(text: str, *, platform: Optional[str] = None) -> Optional[str]:
    """The first existing font file that can draw ``text``, or None."""
    for path in font_candidates(text, platform=platform):
        if os.path.isfile(path):
            return path
    return None


# ── advance widths, read from the font file ───────────────────────────────

#: The most bytes read for one font table: a bound against a broken file, far
#: above any font here (Nirmala.ttc's cmap and hmtx are tens of KB).
_TABLE_MAX_BYTES = 16 << 20
#: The (platform, encoding) pairs of a 32-bit Unicode cmap, which FreeType
#: takes over a 16-bit one (Windows UCS-4; Unicode 2.0+ full repertoire).
_CMAP_32 = ((3, 10), (0, 4), (0, 6))


class _Widths(NamedTuple):
    units_per_em: int
    advances: Tuple[int, ...]           # per glyph id, in font units
    glyph: Callable[[int], int]         # code point -> glyph id (0: .notdef)


def _read_tables(fontfile: str, tags: Sequence[bytes]) -> Dict[bytes, bytes]:
    """The ``tags`` tables of ``fontfile``'s first face (a .ttf/.otf, or
    face 0 of a .ttc collection).  Raises OSError, KeyError (no such
    table), ValueError or struct.error for a file that is not such a font."""
    with open(fontfile, 'rb') as fh:
        base = 0
        if fh.read(4) == b'ttcf':
            fh.seek(12)
            base = struct.unpack('>I', fh.read(4))[0]
        fh.seek(base + 4)
        count = struct.unpack('>H', fh.read(2))[0]
        fh.seek(base + 12)
        directory = fh.read(16 * count)
        records = {}
        for k in range(count):
            tag, _checksum, offset, length = struct.unpack_from('>4sIII', directory, 16 * k)
            records[tag] = (offset, length)
        out = {}
        for tag in tags:
            offset, length = records[tag]
            if length > _TABLE_MAX_BYTES:
                raise ValueError('its %r table is %d bytes' % (tag, length))
            fh.seek(offset)
            out[tag] = fh.read(length)
            if len(out[tag]) != length:
                raise ValueError('its %r table is cut short' % (tag,))
    return out


def _cmap_format4(data: bytes, at: int) -> Callable[[int], int]:
    """A 16-bit segment map: each segment maps by idDelta, or through
    glyphIdArray (idRangeOffset), checked to lie inside the table."""
    count = struct.unpack_from('>H', data, at + 6)[0] // 2
    ends = struct.unpack_from('>%dH' % count, data, at + 14)
    starts = struct.unpack_from('>%dH' % count, data, at + 16 + 2 * count)
    deltas = struct.unpack_from('>%dH' % count, data, at + 16 + 4 * count)
    ranges_at = at + 16 + 6 * count
    ranges = struct.unpack_from('>%dH' % count, data, ranges_at)
    for i in range(count):
        if ranges[i] and ranges_at + 2 * i + ranges[i] + 2 * (ends[i] - starts[i]) + 2 > len(data):
            raise ValueError('its cmap segment %d points past the table' % i)

    def glyph(cp: int) -> int:
        i = bisect.bisect_left(ends, cp)
        if cp > 0xFFFF or i == count or starts[i] > cp:
            return 0
        if not ranges[i]:
            return (cp + deltas[i]) & 0xFFFF
        g = struct.unpack_from('>H', data, ranges_at + 2 * i + ranges[i] + 2 * (cp - starts[i]))[0]
        return (g + deltas[i]) & 0xFFFF if g else 0
    return glyph


def _cmap_format12(data: bytes, at: int) -> Callable[[int], int]:
    """A 32-bit segmented coverage: groups of consecutive code points."""
    count = struct.unpack_from('>I', data, at + 12)[0]
    if at + 16 + 12 * count > len(data):
        raise ValueError('its cmap groups run past the table')
    groups = [struct.unpack_from('>III', data, at + 16 + 12 * k) for k in range(count)]
    ends = [g[1] for g in groups]

    def glyph(cp: int) -> int:
        i = bisect.bisect_left(ends, cp)
        if i == count or groups[i][0] > cp:
            return 0
        return groups[i][2] + cp - groups[i][0]
    return glyph


def _unicode_cmap(data: bytes) -> Callable[[int], int]:
    """The Unicode cmap FreeType selects: the last 32-bit one (format 12),
    else the last 16-bit one (format 4)."""
    wide = narrow = None
    for k in range(struct.unpack_from('>H', data, 2)[0]):
        platform, encoding, offset = struct.unpack_from('>HHI', data, 4 + 8 * k)
        fmt = struct.unpack_from('>H', data, offset)[0]
        if fmt == 12 and (platform, encoding) in _CMAP_32:
            wide = offset
        elif fmt == 4 and (platform == 0 or (platform, encoding) == (3, 1)):
            narrow = offset
    if wide is not None:
        return _cmap_format12(data, wide)
    if narrow is not None:
        return _cmap_format4(data, narrow)
    raise ValueError('it has no Unicode cmap')


@functools.lru_cache(maxsize=16)
def _widths(fontfile: str) -> Optional[_Widths]:
    """``fontfile``'s advance widths, or None (logged once per file) when it
    cannot be read as a TrueType/OpenType font."""
    try:
        tables = _read_tables(fontfile, (b'head', b'hhea', b'hmtx', b'cmap'))
        units_per_em = struct.unpack_from('>H', tables[b'head'], 18)[0]
        count = struct.unpack_from('>H', tables[b'hhea'], 34)[0]
        # hmtx: (advance, left side bearing) per glyph up to count; a glyph
        # past it takes the last advance.
        advances = struct.unpack_from('>%dH' % (2 * count), tables[b'hmtx'])[0::2]
        glyph = _unicode_cmap(tables[b'cmap'])
        if not units_per_em or not advances:
            raise ValueError('it has no advance widths')
    except (OSError, KeyError, ValueError, struct.error) as e:
        logger.warning('cannot read the advance widths of %s (%s: %s); text drawn with it is '
                       'measured by a guess', fontfile, type(e).__name__, e)
        return None
    return _Widths(units_per_em, advances, glyph)


def advance_em(text: str, fontfile: Optional[str]) -> Optional[float]:
    """How wide drawtext draws ``text`` with ``fontfile``, in ems: the sum of
    each character's own glyph advance (.notdef's for a character the font
    has no glyph for; see the module note).  None when ``fontfile`` is None
    or cannot be read as a font."""
    widths = _widths(fontfile) if fontfile else None
    if widths is None:
        return None
    last = len(widths.advances) - 1
    units = sum(widths.advances[min(widths.glyph(ord(ch)), last)] for ch in text)
    return units / float(widths.units_per_em)


__all__ = ['INDIC_RANGES', 'WINDOWS_INDIC', 'WINDOWS_LATIN', 'LINUX_LATIN', 'MAC_LATIN',
           'needs_indic_font', 'drop_emoji', 'font_candidates', 'resolve_font', 'advance_em']
