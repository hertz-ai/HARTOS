"""integrations.reel_studio.style -- what her own words say about the reel.

She tells her Reel Studio agent how she wants her reels when she makes it
("open with the final look, 20s, Tamil songs, add my logo").  Her agent
record keeps those words (goal, flows, extra information), and
``parse_style_hints`` reads them deterministically, so a reel made on a
later turn still follows them even when the agent's model forgets to pass
them as tool arguments.

``parse_style_hints(text)`` is pure and returns only what it found, among
contracts.STYLE_DEFAULTS' keys:

* ``target_s``: "20s", "20 sec", "15 seconds", "30-second", "half a
  minute", "1 minute" (kept as said; make_style clamps it to 5-30).  An
  age or a decade ("brides in their 20s", "1990s") is not a length;
* ``hook_first`` (amendment A1): the reel tells the story in the order she
  filmed it and ends on the reveal; only when she asks to open with the
  result ("open with the final look", "start with the result", "end result
  first", "add a teaser of the final look") does a <= 1 s final-look teaser
  open it.  "don't open with the final look" and "no teaser" say False;
  "end with the final look" or "start with the bare face" say nothing,
  because that is the story anyway;
* ``language``: Tamil, Hindi, Punjabi or English, by its English name or in
  its own script (தமிழ், हिंदी / हिन्दी, ਪੰਜਾਬੀ / पंजाबी); the first one named
  wins ("Tamil songs with English captions" is 'ta');
* ``use_logo``: "add my logo", "with my watermark" say True; "no logo",
  "don't add my logo", "without the logo" say False, and a refusal wins;
* ``logo_corner``: "logo in the top right", "upper-left logo" (bottom
  right is not offered: Instagram's buttons cover it);
* ``pace``: "calm pace", "slow-paced" -> 'calm'; "fast cuts", "on the
  beat" -> 'beat'.

``agent_record_text(prompt_id)`` reads her record through the one agent
config reader (core.cache_loaders.load_agent_config), read-only.

``build_style(args, hints, profile)`` decides: the tool's arguments over her
record's words over her voice profile (logo corner, caption language) over
STYLE_DEFAULTS, through contracts.make_style.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Mapping, Optional

from integrations.reel_studio.contracts import (
    LOGO_CORNERS, STYLE_DEFAULTS, STYLE_LANGUAGES, make_style)

logger = logging.getLogger(__name__)

_I = re.IGNORECASE

# ── length ────────────────────────────────────────────────────────────────

_SECONDS = re.compile(r'(?<![\w.])(\d{1,3}(?:\.\d+)?)\s*-?\s*'
                      r'(s|secs?|seconds?)(?![\w])', _I)
_MINUTES = re.compile(r'(?<![\w.])(\d{1,2})\s*-?\s*(?:mins?|minutes?)(?![\w])', _I)
_HALF_MINUTE = re.compile(r'\bhalf\s+(?:a\s+)?minute\b', _I)
_ONE_MINUTE = re.compile(r'\b(?:a|one)\s+minute\b', _I)
#: Words before "20s" that make it an age or a decade, not a length.
_AGE_BEFORE = re.compile(r"\b(?:her|his|their|my|your|our|the|early|late|mid)\s*-?\s*$", _I)

# ── the opening (A1) ──────────────────────────────────────────────────────

#: What "the result" is called.
_RESULT_NOUN = (r'(?:final\s+look|finished\s+look|final\s+result|end\s+result|result|reveal|'
                r'after\s+look|after)\b')
_RESULT = r'(?:the\s+|her\s+|my\s+|a\s+)?(?:quick\s+|short\s+)?' + _RESULT_NOUN
_FIRST = r'\s+(?:first|up\s*front|at\s+the\s+(?:start|beginning))\b'
_HOOK_PHRASES = (
    # open / start / begin / lead / kick off (the reel) with|on <the result>
    re.compile(r'\b(?:open|start|begin|lead|kick\s*off)(?:s|ing)?\s+'
               r'(?:the\s+(?:reel|video)\s+|it\s+)?(?:with|on|by\s+showing)\s+' + _RESULT, _I),
    # <the result> first / up front / at the start
    re.compile(r'\b' + _RESULT_NOUN + _FIRST, _I),
    # show / tease / flash <the result> first
    re.compile(r'\b(?:show|tease|flash)(?:s|ing)?\s+' + _RESULT + _FIRST, _I),
    # add / with / include a teaser
    re.compile(r'\b(?:add|with|include|use|want)\s+(?:a\s+|the\s+)?(?:quick\s+|short\s+)?teaser\b', _I),
    # hook with the final look
    re.compile(r'\bhook\s+(?:with|on)\s+' + _RESULT, _I),
)
_NO_TEASER = re.compile(r'\b(?:no|without(?:\s+a)?|skip(?:\s+the)?)\s+teasers?\b', _I)
#: A negation shortly before a hook phrase turns it into a refusal.
_NEGATION_BEFORE = re.compile(r"\b(?:don'?t|do\s+not|never|no\s+need\s+to|not)\s+(?:\w+\s+){0,2}$",
                              _I)

# ── language ──────────────────────────────────────────────────────────────

_LANGUAGES = (
    ('ta', re.compile(r'\btamil\b|தமிழ்', _I)),
    ('hi', re.compile(r'\bhindi\b|हिंदी|हिन्दी', _I)),
    ('pa', re.compile(r'\bpunjabi\b|\bpanjabi\b|ਪੰਜਾਬੀ|पंजाबी', _I)),
    ('en', re.compile(r'\benglish\b', _I)),
)

# ── logo ──────────────────────────────────────────────────────────────────

_LOGO = r'(?:the\s+|my\s+|our\s+|a\s+|any\s+)?(?:studio\s+|brand\s+|salon\s+)?(?:logo|watermark)s?\b'
_LOGO_OFF = (
    re.compile(r'\b(?:no|without|skip|remove|hide|drop)\s+' + _LOGO, _I),
    re.compile(r"\b(?:don'?t|do\s+not|never)\s+(?:add|put|use|show|include|place)\s+" + _LOGO, _I),
)
_LOGO_ON = (
    re.compile(r'\b(?:add|put|use|show|include|place|stamp|with)\s+' + _LOGO, _I),
    re.compile(r'\b(?:logo|watermark)\s+(?:on|in|at)\b', _I),
)
_CORNER = re.compile(r'\b(top|upper|bottom|lower)[\s-]+(left|right)\b', _I)
_LOGO_WORD = re.compile(r'\b(?:logo|watermark)\b', _I)
#: How near a corner must be to the word "logo" to be the logo's corner.
_CORNER_REACH = 40

# ── pace ──────────────────────────────────────────────────────────────────

_CALM = (
    re.compile(r'\b(?:calm|slow|relaxed|gentle|soft|smooth)[\s-]+'
               r'(?:pace|paced|cuts?|edit(?:ing|s)?|reels?|transitions?)\b', _I),
    re.compile(r'\bnot\s+too\s+fast\b', _I),
)
_BEAT = (
    re.compile(r'\b(?:fast|quick|snappy|energetic|punchy|rapid)[\s-]+'
               r'(?:pace|paced|cuts?|edit(?:ing|s)?|reels?|transitions?)\b', _I),
    re.compile(r'\bon\s+(?:the\s+)?beats?\b', _I),
)


def _number(text: str):
    value = float(text)
    return int(value) if value.is_integer() else value


def _target_s(text: str):
    for m in _SECONDS.finditer(text):
        if m.group(2).lower() == 's' and _AGE_BEFORE.search(text[max(0, m.start() - 24):m.start()]):
            continue
        value = _number(m.group(1))
        if value > 0:
            return value
    if _HALF_MINUTE.search(text):
        return 30
    m = _MINUTES.search(text)
    if m and int(m.group(1)) > 0:
        return int(m.group(1)) * 60
    if _ONE_MINUTE.search(text):
        return 60
    return None


def _negated(text: str, start: int) -> bool:
    return _NEGATION_BEFORE.search(text[max(0, start - 30):start]) is not None


def _hook_first(text: str) -> Optional[bool]:
    asked = refused = False
    for pattern in _HOOK_PHRASES:
        for m in pattern.finditer(text):
            if _negated(text, m.start()):
                refused = True
            else:
                asked = True
    if _NO_TEASER.search(text):
        refused = True
    if refused:
        return False
    return True if asked else None


def _language(text: str) -> Optional[str]:
    found = [(m.start(), code) for code, pattern in _LANGUAGES
             for m in [pattern.search(text)] if m is not None]
    return min(found)[1] if found else None


def _use_logo(text: str) -> Optional[bool]:
    if any(p.search(text) for p in _LOGO_OFF):
        return False
    if any(p.search(text) for p in _LOGO_ON):
        return True
    return None


def _logo_corner(text: str) -> Optional[str]:
    logos = [m.start() for m in _LOGO_WORD.finditer(text)]
    for m in _CORNER.finditer(text):
        if not any(abs(m.start() - at) <= _CORNER_REACH for at in logos):
            continue
        side = 'top' if m.group(1).lower() in ('top', 'upper') else 'bottom'
        corner = '%s_%s' % (side, m.group(2).lower())
        if corner in LOGO_CORNERS:
            return corner
    return None


def _pace(text: str) -> Optional[str]:
    if any(p.search(text) for p in _CALM):
        return 'calm'
    if any(p.search(text) for p in _BEAT):
        return 'beat'
    return None


def parse_style_hints(text: Optional[str]) -> Dict[str, Any]:
    """The style her words ask for: only the keys found (see the module note)."""
    if not isinstance(text, str) or not text.strip():
        return {}
    found = {
        'target_s': _target_s(text),
        'hook_first': _hook_first(text),
        'language': _language(text),
        'use_logo': _use_logo(text),
        'logo_corner': _logo_corner(text),
        'pace': _pace(text),
    }
    return {k: v for k, v in found.items() if v is not None}


# ── her agent record ──────────────────────────────────────────────────────

def _texts(value) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [s for s in value if isinstance(s, str) and s.strip()]
    return []


def agent_record_text(prompt_id) -> str:
    """Her agent record's goal, each flow's sub-goal and actions, and the
    extra information, one per line; '' when there is no readable record.

    Read-only, through core.cache_loaders.load_agent_config (the one reader:
    path-safe, mtime-cached).  Persona and flow names are left out: they are
    names, not instructions.
    """
    if prompt_id is None or isinstance(prompt_id, bool):
        return ''
    try:
        from core.cache_loaders import load_agent_config
        record = load_agent_config(str(prompt_id).strip())
    except Exception as e:      # a broken record never stops a reel
        logger.info('style: agent record %r unreadable: %s', prompt_id, e)
        return ''
    if not isinstance(record, Mapping):
        return ''
    parts = _texts(record.get('goal'))
    flows = record.get('flows')
    for flow in flows if isinstance(flows, list) else ():
        if isinstance(flow, Mapping):
            parts += _texts(flow.get('sub_goal')) + _texts(flow.get('actions'))
    parts += _texts(record.get('extra_information'))
    return '\n'.join(parts)


# ── who decides ───────────────────────────────────────────────────────────

def _from_profile(profile: Optional[Mapping]) -> Dict[str, Any]:
    """The style keys her voice profile can fill, when they are values the
    style takes."""
    if not isinstance(profile, Mapping):
        return {}
    out = {}
    if profile.get('logo_corner') in LOGO_CORNERS:
        out['logo_corner'] = profile['logo_corner']
    if profile.get('caption_language') in STYLE_LANGUAGES:
        out['language'] = profile['caption_language']
    return out


def build_style(*, args: Optional[Mapping] = None, hints: Optional[Mapping] = None,
                profile: Optional[Mapping] = None) -> Dict[str, Any]:
    """The style dict for the planner.

    ``args`` (the tool's arguments; None means "not given") over ``hints``
    (parse_style_hints of her record) over ``profile`` (her voice profile)
    over contracts.STYLE_DEFAULTS.  Raises ValueError for an argument that is
    not a style key or a value contracts.make_style refuses.
    """
    merged: Dict[str, Any] = {}
    merged.update(_from_profile(profile))
    merged.update({k: v for k, v in (hints or {}).items() if k in STYLE_DEFAULTS})
    merged.update({k: v for k, v in (args or {}).items() if v is not None})
    return make_style(merged)


__all__ = ['parse_style_hints', 'agent_record_text', 'build_style']
