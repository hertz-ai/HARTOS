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
"""
from __future__ import annotations

import os
import re
import sys
from typing import List, Optional, Tuple

from integrations.reel_studio.igkit.caption import EMOJI_RE

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


__all__ = ['INDIC_RANGES', 'WINDOWS_INDIC', 'WINDOWS_LATIN', 'LINUX_LATIN', 'MAC_LATIN',
           'needs_indic_font', 'drop_emoji', 'font_candidates', 'resolve_font']
