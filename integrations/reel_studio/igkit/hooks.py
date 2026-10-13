"""igkit.hooks: check a reel's hook lines and time a voice-over script.

Derived from instagram-agent-skill @d03c56b skills/ig-reel/hookscore.py and
skills/ig-reel/beats.py, Copyright (c) 2026 Jake Schincariol, MIT License
(see LICENSE.txt in this package).  Modified for HARTOS Reel Studio: pure
functions, words and sentences in any script, Indian money, number and
occasion words as concrete markers, makeup risk words as stakes, greetings
and imperatives in Tamil, Hindi and Punjabi, makeup hook formulas, and hook
results shown as WEAK/OK only.  The full list is in igkit/__init__.py.

What the hook check is, in upstream's words: five local heuristics computed
from the text alone (length, a concrete marker, something at stake, the
payload at the front, a viewer to aim at).  Separating a real hook from a
deliberately bad one it does well (AUC 0.83); separating a creator's hits
from her misses it barely does (AUC 0.56).  So her agent shows a weak-hook
flag and the weakest check to fix, never a score or a ranking.
"""
from __future__ import annotations

import functools
import math
import re
import statistics
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from integrations.reel_studio.igkit import (CONCRETE_WORDS, EMOJI_RE, FIGURE_RE, ME_WORDS,
                                            YOU_WORDS, clamp_score, concrete_markers, hashtags,
                                            sentences, word_keys, words)
from integrations.reel_studio.igkit.resources import load_json

#: The five hook checks, in report order.
CHECKS = ('LENGTH', 'SPECIFICITY', 'STAKES', 'FRONTLOAD', 'ADDRESS')
#: Below this a hook is WEAK (upstream's line between WEAK and OK).
WEAK_BELOW = 50.0
#: Most hooks one score_hooks call looks at.
MAX_HOOKS = 20
#: Words that fit one on-screen text card (resources/safe_zones.json).
CARD_MAX_WORDS = int(load_json('safe_zones.json')['text_card']['max_words'])
#: What every score_hooks result says about itself.
HOOK_NOTE = ('Flags a weak hook; it does not predict performance. Fix the weakest check '
             'named, or drop the line.')

#: Words that put something on the line: upstream's list, then the risks a
#: bride worries about, then Tamil, Hindi and Punjabi negation and contrast.
STAKES = frozenset({
    'stop', 'never', 'wrong', 'mistake', 'mistakes', 'lost', 'lose', 'losing', 'cost',
    'costs', 'broke', 'broken', 'failed', 'failure', 'fail', 'nobody', 'no', 'not', "don't",
    'dont', "doesn't", "didn't", "can't", "won't", 'quit', 'quitting', 'fired', 'deleted',
    'delete', 'killed', 'kills', 'kill', 'replaced', 'replaces', 'cut', 'beat', 'free',
    'paid', 'charged', 'hired', 'saved', 'first', 'banned', 'illegal', 'worst', 'hate',
    'hated', 'wasted', 'waste', 'scam', 'lie', 'lied', 'lying', 'truth', 'secret', 'hidden',
    'stole', 'stolen', 'before', 'until', 'instead', 'but', 'except', 'unless', 'problem',
    'risk', 'danger', 'warning', 'regret', 'wish', 'should', "shouldn't", 'still', 'already',
    'only', 'without', 'versus', 'vs', 'actually',
    # makeup: the transformation and what can go wrong on the day
    'bare', 'transformation', 'cakey', 'oxidise', 'oxidize', 'oxidised', 'oxidized',
    'patchy', 'flash', 'flashback', 'humidity', 'humid', 'sweat', 'sweaty', 'sweating',
    'melt', 'melted', 'melting', 'crease', 'creased', 'creasing', 'smudge', 'smudged',
    'grey', 'gray', 'ashy', 'waterproof', 'sweatproof', 'sweat-proof', 'smudge-proof',
    'smudgeproof', 'transfer-proof', 'long-lasting',
    # Tamil, Hindi, Punjabi
    'வேண்டாம்', 'இல்லை', 'இல்லாமல்', 'தப்பு', 'ஆனால்', 'வியர்வை',
    'मत', 'नहीं', 'गलती', 'लेकिन', 'बिना', 'पसीना', 'पसीने', 'कभी',
    'ਨਾ', 'ਨਹੀਂ', 'ਗਲਤੀ', 'ਪਰ', 'ਬਿਨਾਂ',
})

#: Openers that spend the first second saying nothing (whole words only).
WEAK_OPENERS = (
    'so', 'ok', 'okay', 'hey', 'hi', 'hii', 'hai', 'hello', 'guys', 'yo', 'alright',
    'welcome', 'today', 'basically', 'honestly', 'look', 'listen', 'um', 'just', 'let',
    'lets', "let's", 'i wanted', 'i want', 'one of', 'have you', 'did you', 'do you',
    'are you', 'in this', 'in today', 'the thing', 'a lot', 'there is', 'there are',
    'this is', 'it is', 'as a', 'when it', "if you've", 'you know', 'vanakkam', 'namaste',
    'வணக்கம்', 'ஹாய்', 'नमस्ते', 'हाय', 'हेलो', 'ਸਤ',
)

#: English imperatives that earn the front position.
IMPERATIVES = frozenset({
    'stop', 'steal', 'copy', 'delete', 'try', 'watch', 'read', 'save', 'use', 'build',
    'make', 'write', 'send', 'take', 'start', 'quit', 'never', 'always', "don't", 'dont',
    'do', 'put', 'run', 'check', 'see', 'wait', 'book',
})

#: Polite imperatives in verb-final languages: they address the viewer
#: wherever they sit in the line ("இந்த லுக் பாருங்க", "look at this look").
INDIC_IMPERATIVES = frozenset({
    'பாருங்க', 'பாருங்கள்', 'பாரு', 'பாருங்கோ', 'காத்திருங்க',
    'देखिए', 'देखो', 'देखें', 'रुकिए',
    'ਦੇਖੋ', 'ਵੇਖੋ',
})

_CAPITALISED = re.compile(r'[A-Z][a-z]{2,}')
_PRICE = re.compile(r'[$₹]\s?\d')

#: ``(found(text) -> truthy, message)``: anything one of these finds makes a
#: hook WEAK.  Hashtags and emoji use igkit's one definition of each, so a
#: tag the caption linter counts is a tag here too.
DEALBREAKERS = (
    (re.compile(r"(?i)^\s*(?:stop scrolling|don'?t scroll)").search,
     'Opens with "stop scrolling". Asking for attention proves you have not earned it.'),
    (re.compile(r"(?i)\b(?:in (?:this|today'?s) (?:video|reel)|i'?m going to show you"
                r"|i'?ll show you how)\b").search,
     'Video preamble. Delete it and open on the payoff.'),
    (re.compile(r"(?i)^\s*(?:hey |hi |hello |what'?s up |welcome |vanakkam\b|namaste\b"
                r'|வணக்கம்|नमस्ते|ਸਤ ਸ੍ਰੀ ਅਕਾਲ)').search,
     'Greeting. Nobody came to the feed to be greeted.'),
    (hashtags,
     'Hashtag in the hook. Hashtags belong at the bottom of the caption, if anywhere.'),
    (EMOJI_RE.search,
     'Emoji in the hook. On-screen text at hook size has room for words or for an emoji, '
     'not both.'),
)


# ── the five checks ─────────────────────────────────────────────────────────

def _check_length(ws: List[str], text: str) -> Tuple[float, str]:
    """A hook has to land before the thumb moves: roughly two seconds."""
    n = len(ws)
    secs = n / 2.75                      # ~165 words per minute, spoken
    chars = len(text.strip())
    if 5 <= n <= 12:
        score = 100.0
    elif n < 5:
        score = clamp_score(100 - (5 - n) * 20)
    else:
        score = clamp_score(100 - (n - 12) * 11)
    if chars > 60:                       # two lines of big on-screen text
        score -= 12
    return clamp_score(score), '%d words, %d chars, ~%.1fs spoken (want 5-12 words)' % (
        n, chars, secs)


def _check_specificity(text: str) -> Tuple[float, str]:
    """One concrete thing beats three abstract ones."""
    found = concrete_markers(text)
    hits = len(found)
    score = 15.0 if hits == 0 else clamp_score(45 + hits * 30)
    detail = '%d concrete marker(s)' % hits
    detail += (': ' + ', '.join(found[:4])) if found else (
        ' - no number, no name, no occasion, nothing checkable')
    return score, detail


def _check_stakes(keys: List[str], text: str) -> Tuple[float, str]:
    """Tension, cost, negation: something the viewer might lose."""
    markers = sorted({k for k in keys if k in STAKES})
    if _PRICE.search(text):
        markers.append('a price')
    n = len(markers)
    score = {0: 20.0, 1: 70.0}.get(n, 100.0)
    detail = '%d tension marker(s)' % n + (
        (': ' + ', '.join(markers[:4])) if markers else ' - nothing is at stake in this line')
    return clamp_score(score), detail


def _weak_opener(keys: List[str]) -> Optional[str]:
    for weak in WEAK_OPENERS:
        parts = weak.split()
        if keys[:len(parts)] == parts:
            return weak
    return None


def _is_payload(i: int, token: str, key: str) -> bool:
    return (key in STAKES or key in CONCRETE_WORDS or FIGURE_RE.match(token) is not None
            or (i > 0 and _CAPITALISED.fullmatch(token) is not None))


def _check_frontload(ws: List[str], keys: List[str]) -> Tuple[float, str]:
    """The interesting word cannot be in position nine."""
    if not ws:
        return 0.0, 'empty'
    opener = _weak_opener(keys)
    penalty = 30 if opener else 0
    payload = next((i for i, (w, k) in enumerate(zip(ws, keys)) if _is_payload(i, w, k)),
                   None)
    if payload is None:
        base, where = 30.0, 'no payload word anywhere in the line'
    elif payload <= 3:
        base, where = 100.0, 'payload at word %d' % (payload + 1)
    elif payload <= 6:
        base, where = 70.0, 'payload at word %d, could move forward' % (payload + 1)
    else:
        base, where = 40.0, 'payload at word %d, too late' % (payload + 1)
    detail = where + ('; weak opener "%s"' % opener if opener else '')
    return clamp_score(base - penalty), detail


def _check_address(keys: List[str]) -> Tuple[float, str]:
    """Aimed at one viewer, or floating in the air."""
    if any(k in YOU_WORDS for k in keys):
        return 100.0, 'speaks to the viewer'
    if keys and keys[0] in IMPERATIVES:
        return 90.0, 'imperative opener ("%s")' % keys[0]
    told = next((k for k in keys if k in INDIC_IMPERATIVES), None)
    if told:
        return 90.0, 'imperative ("%s")' % told
    if any(k in ME_WORDS for k in keys):
        return 70.0, 'first person, no viewer named'
    return 35.0, 'third person, nobody in the room'


# ── hooks ───────────────────────────────────────────────────────────────────

def rate_hook(text: str) -> Dict[str, Any]:
    """All five checks for one hook line, with the overall number.

    The number is for research ranking (swipe, the trends job) and is never
    shown to her; score_hooks is what her agent sees.  Returns
    ``{'score', 'checks': {name: {'score', 'detail'}}, 'flags', 'weakest',
    'words'}``.
    """
    text = text if isinstance(text, str) else ''
    ws = words(text)
    keys = word_keys(text)
    results = {
        'LENGTH': _check_length(ws, text),
        'SPECIFICITY': _check_specificity(text),
        'STAKES': _check_stakes(keys, text),
        'FRONTLOAD': _check_frontload(ws, keys),
        'ADDRESS': _check_address(keys),
    }
    flags = [msg for found, msg in DEALBREAKERS if found(text)]
    scores = [results[c][0] for c in CHECKS]
    # The weakest property caps the hook: one bad property is enough for the
    # thumb to keep moving.
    overall = clamp_score(statistics.mean(scores) * 0.6 + min(scores) * 0.4 - len(flags) * 15)
    checks = {c: {'score': round(results[c][0], 1), 'detail': results[c][1]} for c in CHECKS}
    weakest = min(CHECKS, key=lambda c: checks[c]['score'])
    return {'score': round(overall, 1), 'checks': checks, 'flags': flags, 'weakest': weakest,
            'words': len(ws)}


@functools.lru_cache(maxsize=1)
def _formulas() -> Tuple[Tuple[int, str, 're.Pattern'], ...]:
    doc = load_json('hooks.json')
    by_id = {h['id']: h for h in doc['hooks']}
    order = doc.get('classify_order') or sorted(by_id)
    return tuple((i, by_id[i]['name'], re.compile(by_id[i]['match'], re.IGNORECASE))
                 for i in order if i in by_id)


def classify_hook(text: str) -> Tuple[Optional[int], str]:
    """``(formula id, formula name)`` from resources/hooks.json, most specific
    first; ``(None, 'unclassified')`` when no formula matches."""
    text = text if isinstance(text, str) else ''
    for fid, name, pattern in _formulas():
        if pattern.search(text):
            return fid, name
    return None, 'unclassified'


def score_hooks(hooks: Union[str, Sequence[str], None]) -> Dict[str, Any]:
    """Check hook lines and say which are weak, in the order given.

    ``hooks`` is one string with a line per hook, a list of lines, or None
    (no hooks); anything else (a number, bytes, a mapping) raises TypeError
    naming its type.  Blank lines and items that are not text are dropped,
    and at most MAX_HOOKS are checked.  Each result is
    ``{'hook', 'verdict': 'WEAK'|'OK', 'weakest': {'check', 'detail'},
    'flags', 'formula_id', 'formula', 'card_ok', 'card_words'}``: no score
    and no ranking, because neither predicts how a reel does.  A hook with a
    dealbreaker (a greeting, a preamble, a hashtag, an emoji) is WEAK.
    ``card_ok`` says whether the line fits one on-screen card
    (CARD_MAX_WORDS words).
    """
    if hooks is None:
        hooks = ()
    elif isinstance(hooks, str):
        hooks = hooks.splitlines()
    elif isinstance(hooks, (bytes, bytearray, Mapping)) or not isinstance(hooks, Iterable):
        raise TypeError('hooks must be text with a line per hook or a list of lines, got %s'
                        % type(hooks).__name__)
    lines = [h.strip() for h in hooks if isinstance(h, str) and h.strip()]
    out = []
    for hook in lines[:MAX_HOOKS]:
        rated = rate_hook(hook)
        fid, formula = classify_hook(hook)
        weakest = rated['weakest']
        out.append({
            'hook': hook,
            'verdict': 'WEAK' if rated['score'] < WEAK_BELOW or rated['flags'] else 'OK',
            'weakest': {'check': weakest, 'detail': rated['checks'][weakest]['detail']},
            'flags': rated['flags'],
            'formula_id': fid,
            'formula': formula,
            'card_ok': rated['words'] <= CARD_MAX_WORDS,
            'card_words': rated['words'],
        })
    return {'note': HOOK_NOTE, 'hooks': out}


# ── the beat sheet ──────────────────────────────────────────────────────────

HOOK_WINDOW = 3.0        # seconds. Past this, the thumb has already decided.
MAX_BEAT = 4.0           # seconds on one idea with no change on screen.
ABSTRACT_RUN = 3         # beats in a row with nothing checkable in them.
DEFAULT_WPM = 165

#: Words that do not make a loop between the first and the last beat.
STOPWORDS = frozenset({
    'the', 'a', 'an', 'and', 'or', 'but', 'if', 'of', 'to', 'in', 'on', 'for', 'with',
    'that', 'this', 'it', 'is', 'are', 'was', 'were', 'be', 'been', 'you', 'your', 'i',
    'my', 'me', 'we', 'our', 'they', 'them', 'he', 'she', 'so', 'just', 'not', 'no', 'do',
    'did', 'does', 'have', 'has', 'had', 'will', 'can', 'at', 'as', 'by', 'from', 'out',
    'up', 'off', 'one', 'all',
    'இந்த', 'அந்த', 'ஒரு', 'மற்றும்', 'நான்', 'நீங்க',
    'है', 'हैं', 'का', 'की', 'के', 'में', 'और', 'यह', 'वह', 'से', 'को', 'तो',
    'ਹੈ', 'ਦਾ', 'ਦੀ', 'ਦੇ', 'ਵਿੱਚ', 'ਅਤੇ',
})


def _rate_wps(wpm) -> float:
    if (isinstance(wpm, bool) or not isinstance(wpm, (int, float)) or not math.isfinite(wpm)
            or wpm <= 0):
        raise ValueError('wpm must be a positive number of words per minute, got %r' % (wpm,))
    return wpm / 60.0


#: The digits a token ends with, tried only where their run starts.
_TRAILING_DIGITS = re.compile(r'(?<!\d)\d+$')


def _pretty(token: str) -> str:
    """Put the thousands separator back for display ("1500000" ->
    "1,500,000"), in one pass: upstream's "(\\d)(?=(\\d{3})+$)" scanned to
    the end of the token from every digit."""
    m = _TRAILING_DIGITS.search(token)
    if not m:
        return token
    digits = m.group()
    lead = len(digits) % 3 or 3
    groups = [digits[:lead]] + [digits[i:i + 3] for i in range(lead, len(digits), 3)]
    return token[:m.start()] + ','.join(groups)


def _split_beats(raw: str, wps: float) -> List[str]:
    """One line is one beat, unless a line is too long to be one."""
    beats: List[str] = []
    for line in (ln.strip() for ln in raw.splitlines()):
        if not line:
            continue
        if len(words(line)) / wps <= MAX_BEAT * 1.5:
            beats.append(line)
            continue
        # A long paragraph breaks at sentence ends (., !, ?, the danda) so
        # the timings mean something.
        buf = ''
        for part in sentences(line):
            candidate = (buf + ' ' + part).strip()
            if buf and len(words(candidate)) / wps > MAX_BEAT:
                beats.append(buf)
                buf = part
            else:
                buf = candidate
        if buf:
            beats.append(buf)
    return beats


def beat_sheet(script: str, wpm: float = DEFAULT_WPM,
               target: Optional[float] = None) -> Dict[str, Any]:
    """Time a voice-over script into beats before she records it.

    Each line is a beat (a long line splits at sentence ends), timed by its
    word count at ``wpm``.  Flags a hook past HOOK_WINDOW seconds, a beat
    past MAX_BEAT, ABSTRACT_RUN beats in a row with nothing concrete, a last
    beat that does not loop back to the hook, and a length off ``target``
    seconds.  Timings are an estimate from word count, not a recording; her
    own rate (voice profile ``wpm``) beats the default.

    Returns ``{'wpm', 'target', 'total_seconds', 'total_words', 'beats':
    [{'n', 'start', 'dur', 'words', 'text', 'concrete', 'label', 'flags'}],
    'notes'}``.  Raises ValueError for a ``wpm`` that is not a positive
    number.  Cut points of the reel itself come from the music beat grid,
    not from this.
    """
    wps = _rate_wps(wpm)
    if isinstance(target, bool) or not isinstance(target, (int, float)) or not (
            math.isfinite(target) and target > 0):
        target = None
    beats = _split_beats(script if isinstance(script, str) else '', wps)
    if not beats:
        return {'wpm': wpm, 'target': target, 'total_seconds': 0, 'total_words': 0,
                'beats': [], 'notes': ['The script is empty.']}

    rows, clock = [], 0.0
    for i, text in enumerate(beats):
        n = len(words(text))
        dur = n / wps
        rows.append({'n': i + 1, 'start': round(clock, 2), 'dur': round(dur, 2), 'words': n,
                     'text': text, 'concrete': len(concrete_markers(text)), 'label': '',
                     'flags': []})
        clock += dur
    total = clock

    # Label the structural positions a reel is built around.
    for r in rows:
        if r['n'] == 1 or r['start'] + r['dur'] <= HOOK_WINDOW:
            r['label'] = 'HOOK'
    rows[-1]['label'] = 'CTA' if rows[-1]['label'] != 'HOOK' else 'HOOK/CTA'
    half = total / 2
    for r in rows:
        if not r['label'] and r['start'] <= half < r['start'] + r['dur']:
            r['label'] = 'MID'

    notes = []
    if rows[0]['dur'] > HOOK_WINDOW:
        rows[0]['flags'].append('hook runs %.1fs, past the %.0fs mark'
                                % (rows[0]['dur'], HOOK_WINDOW))
        notes.append('Beat 1 takes %.1fs to say. Cut it to %d words or fewer, or the hook '
                     'lands after the decision has been made.'
                     % (rows[0]['dur'], int(HOOK_WINDOW * wps)))
    if rows[0]['concrete'] == 0:
        notes.append('Beat 1 has no number, no name and no occasion in it. Hooks without '
                     'something checkable are the ones that get scrolled.')

    for r in rows:
        if r['dur'] > MAX_BEAT:
            r['flags'].append('%.1fs on one beat' % r['dur'])
    long_beats = [r['n'] for r in rows if r['dur'] > MAX_BEAT]
    if long_beats:
        notes.append('Beat(s) %s run past %.0fs. Either split the line or change what is on '
                     'screen inside it. A static frame is where people leave.'
                     % (', '.join(map(str, long_beats)), MAX_BEAT))

    run, start = 0, None
    for r in rows:
        if r['concrete'] == 0:
            run += 1
            start = start if start is not None else r['n']
            if run == ABSTRACT_RUN:
                notes.append('Beats %d-%d have nothing concrete in them. Put a number, a '
                             'name, a price or the occasion in one of them.' % (start, r['n']))
        else:
            run, start = 0, None

    # Does the last line hand you back to the first one?
    first = {k for k in word_keys(rows[0]['text']) if k not in STOPWORDS}
    last = {k for k in word_keys(rows[-1]['text']) if k not in STOPWORDS}
    loop = sorted(_pretty(w) for w in first & last)
    if loop:
        notes.append('Loops: the last beat repeats "%s" from the hook. Second watches are '
                     'free reach.' % ', '.join(loop[:3]))
    else:
        notes.append('No loop. The last beat shares no word with the hook, so the video ends '
                     'flat. Echoing one word from beat 1 is the cheapest replay you get.')

    if target:
        delta = total - target
        if abs(delta) <= target * 0.1:
            notes.append('Length is on target (%.1fs against %ss).' % (total, target))
        elif delta > 0:
            notes.append('%.1fs over target. Cut about %d words.' % (delta, int(delta * wps)))
        else:
            notes.append('%.1fs under target. Either add %d words or shoot it short. Short is '
                         'usually right.' % (-delta, int(-delta * wps)))

    return {'wpm': wpm, 'target': target, 'total_seconds': round(total, 2),
            'total_words': sum(r['words'] for r in rows), 'beats': rows, 'notes': notes}
