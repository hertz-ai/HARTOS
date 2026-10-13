"""igkit.caption: lint an Instagram caption and show what the feed shows
before "... more".

Derived from instagram-agent-skill @d03c56b skills/ig-caption/caption.py,
Copyright (c) 2026 Jake Schincariol, MIT License (see LICENSE.txt in this
package).  Modified for HARTOS Reel Studio: a pure function, hashtags in any
script counted against the one cap (contracts.HASHTAG_LIMIT), the shared
definition of "concrete", Indian booking asks (DM for bookings, bookings
open, a WhatsApp number), more generic tags named, search terms matched
case-insensitively in any script, and igkit's one emoji definition and one
hashtag rule (an emoji counted once with its variation selector, skin tone
and joined parts; glued tags each counted).

Instagram shows about 125 characters of a caption in the feed and hides the
rest behind a tap; the real cut moves with the device and font, so aim for a
margin rather than a caption engineered to end at 125.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from integrations.reel_studio.contracts import HASHTAG_LIMIT
from integrations.reel_studio.igkit import EMOJI_RE, concrete_markers, hashtags

LIMIT = 2200             # Instagram's hard caption limit.
TRUNCATE = 125           # Roughly where the feed cuts to "... more".

MENTION_RE = re.compile(r'(?:^|\s)(@[A-Za-z0-9_.]+)')
#: A bare domain starts where no letter, digit or '-' is before it, not at
#: every word boundary: "\b" let a run like "a-a-a-..." restart at each "-"
#: (3.8 s on 20,000 characters); this is one pass.
LINK_RE = re.compile(r'https?://\S+|\bwww\.\S+'
                     r'|(?<![\w-])[a-z0-9-]+\.(?:com|co|io|net|org|ai|app)/\S*',
                     re.IGNORECASE)

#: Calls to action, each with the name the result reports.  The last three
#: are the asks Indian makeup artists use; a WhatsApp number is not
#: clickable in a caption, but people copy it.
ASKS = (
    (re.compile(r'(?i)\bcomment (?:the word |")?[A-Z0-9]{2,}\b'), 'comment a keyword'),
    (re.compile(r'(?i)\b(?:dm|message) me\b'), 'DM me'),
    (re.compile(r'(?i)\bsave (?:this|it)\b'), 'save this'),
    (re.compile(r'(?i)\bshare (?:this|it)\b'), 'share this'),
    (re.compile(r'(?i)\bfollow (?:me|for)\b'), 'follow'),
    (re.compile(r'(?i)\blink in (?:my )?bio\b'), 'link in bio'),
    (re.compile(r'(?i)\b(?:swipe|tap) (?:through|left|right|for|to)\b'), 'swipe or tap'),
    (re.compile(r'(?i)\btell me\b|\bwhat would you\b|\bwhich one\b'), 'answer a question'),
    (re.compile(r'(?i)\b(?:dm|message|whats\s?app|call)\s+(?:me\s+|us\s+)?(?:for|to)\s+'
                r'(?:book|enquir|inquir|detail|price|rate|date|slot)'), 'DM for bookings'),
    (re.compile(r'(?i)\b(?:bookings?|slots?)\s+(?:are\s+)?(?:now\s+)?open\b'), 'bookings open'),
    (re.compile(r'(?i)\bwhats\s?app\b\D{0,20}\+?\d[\d \-]{7,}\d'), 'WhatsApp number'),
)

#: Tags that describe nothing.
FILLER_TAGS = frozenset({
    '#viral', '#fyp', '#explore', '#explorepage', '#foryou', '#foryoupage', '#trending',
    '#instagood', '#love', '#follow', '#like4like', '#reels', '#reelsinstagram', '#viralreels',
    '#instadaily', '#reelitfeelit', '#reelkarofeelkaro', '#trendingreels', '#viralvideo',
    '#viralvideos', '#instareels',
})


def _visible_window(text: str, cut: int) -> str:
    """What the feed shows. Instagram cuts mid-word, so this does too."""
    return text if len(text) <= cut else text[:cut]


def lint_caption(text: str, keywords: Optional[Iterable[str]] = None, truncate: int = TRUNCATE,
                 hashtag_limit: int = HASHTAG_LIMIT) -> Dict[str, Any]:
    """Check a caption the way a scrolling stranger reads it.

    Checks LENGTH, FIRST LINE, HOOK IS CONCRETE, HASHTAGS, TAG PLACEMENT,
    LINKS, ONE ASK, EMOJI and, when ``keywords`` are given, SEARCH TERMS;
    each is PASS, WARN or FAIL.  The verdict is FIX with any FAIL, REVIEW
    with any WARN, else READY.

    Returns ``{'characters', 'limit', 'truncate_at', 'hashtag_limit',
    'visible', 'truncated', 'first_line_chars', 'hashtags', 'mentions',
    'links', 'emoji', 'asks', 'checks': [{'check', 'status', 'detail'}],
    'verdict'}``.
    """
    stripped = (text if isinstance(text, str) else '').strip()
    chars = len(stripped)
    first_line = stripped.split('\n')[0].strip() if stripped else ''
    found_tags = hashtags(stripped)
    tags = [t for t, _, _ in found_tags]
    mentions = MENTION_RE.findall(stripped)
    links = LINK_RE.findall(stripped)
    emoji = EMOJI_RE.findall(stripped)
    window = _visible_window(stripped, truncate)
    truncated = chars > truncate
    asks = [name for pattern, name in ASKS if pattern.search(stripped)]
    filler = [t for t in tags if t.casefold() in FILLER_TAGS]
    keywords = [k.strip() for k in (keywords or []) if isinstance(k, str) and k.strip()]

    checks: List[Dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({'check': name, 'status': status, 'detail': detail})

    add('LENGTH', 'FAIL' if chars > LIMIT else 'PASS',
        '%d / %d characters' % (chars, LIMIT)
        + (', %d over the limit' % (chars - LIMIT) if chars > LIMIT else ''))

    if not first_line:
        add('FIRST LINE', 'FAIL', 'the caption opens on a blank line')
    elif first_line.startswith('#') or first_line.startswith('@'):
        add('FIRST LINE', 'FAIL',
            'opens on a hashtag or a mention, which is the one position worth a sentence')
    elif len(first_line) > truncate:
        add('FIRST LINE', 'WARN',
            '%d characters, so it gets cut at %d mid-thought. Fine if the cut is a '
            'cliffhanger, bad if it is a subordinate clause' % (len(first_line), truncate))
    else:
        add('FIRST LINE', 'PASS', '%d characters, lands whole' % len(first_line))

    in_window = concrete_markers(window)
    add('HOOK IS CONCRETE', 'PASS' if in_window else 'WARN',
        '%d number(s), name(s) or occasion(s) in the visible window' % len(in_window)
        + ('' if in_window else ' - nothing checkable before the tap'))

    if len(tags) > hashtag_limit:
        add('HASHTAGS', 'FAIL', '%d tags, over the cap of %d. Tags past the cap do not count '
                                'and the block reads as old' % (len(tags), hashtag_limit))
    elif len(tags) == hashtag_limit and filler:
        add('HASHTAGS', 'WARN', '%d tags, at the cap, and %d of them generic. Spend them on '
                                'topics' % (len(tags), len(filler)))
    elif filler:
        add('HASHTAGS', 'WARN', '%d tags, %d of them generic (%s). Those describe nothing'
            % (len(tags), len(filler), ', '.join(filler[:3])))
    else:
        add('HASHTAGS', 'PASS', '%d tag(s)' % len(tags) + (': ' + ' '.join(tags) if tags else ''))

    if not tags:
        add('TAG PLACEMENT', 'PASS', 'no tags to place')
    elif any(end <= truncate for _, _, end in found_tags):
        add('TAG PLACEMENT', 'WARN', 'a hashtag is inside the visible window, spending feed '
                                     'space on a label')
    else:
        add('TAG PLACEMENT', 'PASS', 'tags are below the fold')

    add('LINKS', 'WARN' if links else 'PASS',
        '%d link(s) in the caption, and captions are not clickable. Move it to the bio or '
        'the DM' % len(links) if links else 'no dead links in the body')

    if len(asks) == 1:
        add('ONE ASK', 'PASS', 'one call to action: %s' % asks[0])
    elif not asks:
        add('ONE ASK', 'WARN', 'no call to action. Decide what this post is for')
    else:
        add('ONE ASK', 'WARN', '%d asks (%s). Two asks is the same as none'
            % (len(asks), ', '.join(asks)))

    density = len(emoji) * 100 / max(chars, 1)
    add('EMOJI', 'WARN' if density > 4 else 'PASS',
        '%d emoji, %.1f per 100 characters' % (len(emoji), density)
        + (' - reads as decoration' if density > 4 else ''))

    if keywords:
        low, low_window = stripped.casefold(), window.casefold()
        present = [k for k in keywords if k.casefold() in low]
        missing = [k for k in keywords if k.casefold() not in low]
        shown = [k for k in present if k.casefold() in low_window]
        status = 'PASS' if not missing else ('WARN' if present else 'FAIL')
        add('SEARCH TERMS', status,
            '%d/%d present' % (len(present), len(keywords))
            + (', %d in the visible window' % len(shown) if present else '')
            + ('. Missing: %s' % ', '.join(missing) if missing else ''))

    fails = sum(1 for c in checks if c['status'] == 'FAIL')
    warns = sum(1 for c in checks if c['status'] == 'WARN')
    verdict = 'FIX' if fails else ('REVIEW' if warns else 'READY')
    return {
        'characters': chars, 'limit': LIMIT, 'truncate_at': truncate,
        'hashtag_limit': hashtag_limit, 'visible': window, 'truncated': truncated,
        'first_line_chars': len(first_line), 'hashtags': tags, 'mentions': mentions,
        'links': links, 'emoji': len(emoji), 'asks': asks, 'checks': checks,
        'verdict': verdict,
    }
