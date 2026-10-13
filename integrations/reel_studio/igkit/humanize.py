"""igkit.humanize: strip the machine fingerprint out of a draft, and score
how machine-written it looks.

Derived from instagram-agent-skill @d03c56b skills/ig-human/humanize.py and
skills/ig-human/detect.py, Copyright (c) 2026 Jake Schincariol, MIT License
(see LICENSE.txt in this package).  Modified for HARTOS Reel Studio: pure
functions; zero-width joiners kept inside Indic words and emoji sequences;
words and sentences (with the danda) in any script; the shared definition
of "concrete"; first/second-person words in Tamil, Hindi and Punjabi;
contractions measured only for Latin-script text; per-user allowlists of
emoji and words; the result text without a trailing newline.  The full list
is in igkit/__init__.py.

humanize_text, three passes in this order:
  1. INVISIBLE   delete or normalise characters a keyboard never produces
                 (zero-width spaces, word joiners, soft hyphens, BOMs, tag
                 characters, non-breaking spaces).  Joiners inside Indic
                 words and emoji stay.  Whitespace is then folded as a
                 phone writes it (line breaks, at most one blank line,
                 single spaces).
  2. TYPOGRAPHIC em dash -> comma, en dash -> hyphen, curly quotes ->
                 straight, ellipsis -> three dots, bullet -> hyphen.
  3. LEXICAL     the slop lexicon (resources/slop.json) -> plain words, keeping
                 capitalisation and leaving links alone.
Structural tells (rule-of-three, "not just X, it's Y", emoji bullets) are
REPORTED, never rewritten: changing a sentence's shape needs judgement.

detect_machine_text: five local checks modelled on what public detectors
measure.  It is not GPTZero or any other detector, calls nothing, and
cannot promise their verdict.
"""
from __future__ import annotations

import functools
import logging
import re
import statistics
import unicodedata
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from integrations.reel_studio.igkit import (EMOJI_RE, JOINERS, ME_WORDS, YOU_WORDS, clamp_score,
                                            concrete_markers, sentences, word_keys, words)
from integrations.reel_studio.igkit.resources import load_json

logger = logging.getLogger(__name__)

#: Links and e-mail addresses, left alone by every pass.  An address starts
#: where its run of non-space, non-'@' characters does; upstream's
#: "\S+@\S+\.\S+" restarted inside every run and backtracked over its own
#: '@'s (5.9 s on a 2,200-character caption of "a@a@...", 7 s on 40,000
#: letters with no space).  This is one pass.
URL_RE = re.compile(r'https?://\S+|www\.\S+|(?<![^\s@])[^\s@]+@[^\s@]+\.[^\s@]+')
#: A run of ASCII whitespace holding a line break, or of two or more spaces
#: and tabs.  Hard spaces are not in it: the invisible pass reports and
#: normalises them, and the detector counts them.
_SPACE_RUN = re.compile(r'[ \t\f\v]*(?:\r\n?|\n)[ \t\r\n\f\v]*|[ \t\f\v]{2,}')
#: The detector's five checks, in report order.
DETECT_CHECKS = ('BURSTINESS', 'SPECIFICITY', 'SLOP DENSITY', 'FINGERPRINT', 'VOICE')

_CONTRACTIONS = re.compile(r"\b\w+'(?:s|t|re|ve|ll|d|m)\b", re.IGNORECASE)
_PERSON_WORDS = YOU_WORDS | ME_WORDS
_HARD_SPACES = '\u00a0\u202f\u2009'
_VS16 = '\ufe0f'


@functools.lru_cache(maxsize=1)
def _default_lexicon() -> Mapping[str, Any]:
    return load_json('slop.json')


def _lexicon(lexicon: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    return _default_lexicon() if lexicon is None else lexicon


# ── joiners ─────────────────────────────────────────────────────────────────

#: Alphabets whose letters never join: a joiner beside one ("é", Greek,
#: Cyrillic) is a watermark, just as between ASCII letters.
_NON_JOINING_SCRIPTS = ('LATIN', 'GREEK', 'CYRILLIC')


def _joinable(ch: str) -> bool:
    """A neighbour a joiner may legitimately sit next to: a letter of a
    script that joins (Indic, Arabic and the rest; not Latin, Greek or
    Cyrillic), a mark other than a combining accent (Indic viramas and vowel
    signs, the emoji variation selector) or a symbol (emoji, skin tones)."""
    if not ch or ord(ch) < 0x80:
        return False
    cat = unicodedata.category(ch)
    if cat in ('So', 'Sk'):
        return True
    name = unicodedata.name(ch, '')
    if cat[0] == 'L':
        return not name.startswith(_NON_JOINING_SCRIPTS)
    if cat[0] == 'M':
        return not name.startswith('COMBINING')
    return False


def _joins(text: str, i: int) -> bool:
    """Whether the joiner at ``text[i]`` sits inside an Indic word or an
    emoji sequence (kept), rather than beside Latin, Greek or Cyrillic
    letters, digits or spaces (a watermark)."""
    before = text[i - 1] if i > 0 else ''
    after = text[i + 1] if i + 1 < len(text) else ''
    return _joinable(before) or _joinable(after)


def _invisible(text: str, i: int) -> bool:
    """An invisible format character that is not a joiner doing its job."""
    ch = text[i]
    return unicodedata.category(ch) == 'Cf' and not (ch in JOINERS and _joins(text, i))


# ── humanize ────────────────────────────────────────────────────────────────

def _fold_space(text: str) -> str:
    """Whitespace as a phone writes it, in one pass: "\\n" line breaks, at
    most one blank line, no spaces or tabs beside a line break, and one
    space for a run of spaces or tabs.

    The lexical pass ends with the same folds; doing them first means no
    later pattern meets a long whitespace run, where patterns such as
    "\\s*X" restart at every character of it (6 s on 20,000 blank lines).
    """
    def fold(m: 're.Match') -> str:
        run = m.group(0)
        breaks = run.count('\n') + run.count('\r') - run.count('\r\n')
        return '\n\n' if breaks > 1 else ('\n' if breaks else ' ')

    return _SPACE_RUN.sub(fold, text)


def _cp(spec: str):
    """'U+200B' -> 0x200B;  'U+E0000-U+E007F' -> (0xE0000, 0xE007F)."""
    if '-' in spec:
        a, b = spec.split('-')
        return int(a[2:], 16), int(b[2:], 16)
    return int(spec[2:], 16)


def _protect_urls(text: str) -> Tuple[str, List[str]]:
    """Swap links for placeholders so no pass rewrites inside a link."""
    found: List[str] = []

    def stash(m):
        found.append(m.group(0))
        return '\x00URL%d\x00' % (len(found) - 1)

    return URL_RE.sub(stash, text), found


def _restore_urls(text: str, found: List[str]) -> str:
    for i, url in enumerate(found):
        text = text.replace('\x00URL%d\x00' % i, url)
    return text


def _pass_invisible(text: str, lex: Mapping[str, Any]) -> Tuple[str, List[dict]]:
    hits = []
    for entry in lex['invisible']:
        cp = _cp(entry['cp'])
        if isinstance(cp, tuple):
            pattern = re.compile('[' + re.escape(chr(cp[0])) + '-' + re.escape(chr(cp[1])) + ']')
        else:
            pattern = re.compile(re.escape(chr(cp)))
        name = entry['cp'] + ' ' + entry['name']
        if entry['action'] == 'keep_joining':
            drop = {m.start() for m in pattern.finditer(text) if not _joins(text, m.start())}
            if drop:
                hits.append({'name': name, 'count': len(drop), 'action': 'delete'})
                text = ''.join(c for i, c in enumerate(text) if i not in drop)
            continue
        n = len(pattern.findall(text))
        if n:
            hits.append({'name': name, 'count': n, 'action': entry['action']})
            text = pattern.sub('' if entry['action'] == 'delete' else ' ', text)
    # Any other format character is invisible by definition.
    stray = {i for i in range(len(text)) if _invisible(text, i)}
    if stray:
        hits.append({'name': 'other invisible format chars', 'count': len(stray),
                     'action': 'delete'})
        text = ''.join(c for i, c in enumerate(text) if i not in stray)
    return text, hits


def _pass_typographic(text: str, lex: Mapping[str, Any]) -> Tuple[str, List[dict]]:
    hits = []
    for entry in lex['typographic']:
        ch = entry['from']
        n = text.count(ch)
        if not n:
            continue
        hits.append({'name': '%s %s' % (ch, entry['name']), 'count': n,
                     'to': entry['to'].strip() or '(space)'})
        if ch == '—':
            # " word — word " and "word—word" both become a comma and a space.
            text = re.sub(r'\s*—\s*', ', ', text)
        elif ch == '–':
            text = re.sub(r'\s*–\s*(?=\d)', '-', text)      # 5–10 -> 5-10
            text = re.sub(r'\s+–\s+', ', ', text)            # used as an em dash
            text = text.replace('–', '-')
        else:
            text = text.replace(ch, entry['to'])
    # A comma inserted before existing punctuation reads wrong.
    text = re.sub(r',\s*([,.;:!?])', r'\1', text)
    text = re.sub(r',\s*\n', '\n', text)
    return text, hits


def _match_case(src: str, repl: str) -> str:
    if not repl:
        return repl
    if src.isupper() and len(src) > 1:
        return repl.upper()
    if src[0].isupper():
        return repl[0].upper() + repl[1:]
    return repl


def _lexicon_pattern(find: str) -> 're.Pattern':
    return re.compile(r'\b' + re.escape(find).replace(r'\ ', r'\s+') + r'\b', re.IGNORECASE)


def _kept(keep_words: Iterable[str]) -> set:
    return {w.strip().casefold() for w in (keep_words or ()) if isinstance(w, str) and w.strip()}


def _pass_lexical(text: str, lex: Mapping[str, Any],
                  keep_words: Iterable[str] = ()) -> Tuple[str, List[dict]]:
    """Replace slop words and phrases, longest first so phrases win."""
    hits = []
    kept = _kept(keep_words)
    entries = sorted(list(lex['phrases']) + list(lex['words']), key=lambda e: len(e['find']),
                     reverse=True)
    for entry in entries:
        find = entry['find']
        if find.casefold() in kept:
            continue
        pattern = _lexicon_pattern(find)
        found = pattern.findall(text)
        if not found:
            continue
        hits.append({'find': find, 'replace': entry['replace'] or '(deleted)',
                     'count': len(found), 'family': entry['family']})
        text = pattern.sub(lambda m, r=entry['replace']: _match_case(m.group(0), r), text)
    # Clean up after deletions: a deleted clause leaves orphaned punctuation
    # behind ("system. ." or a line that now opens on a comma).
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'(?m)^[ \t]*(?:[,.;:]+[ \t]*)+', '', text)
    text = re.sub(r'(?m)^[ \t](?=\S)', '', text)          # one space left by a deletion
    text = re.sub(r'\s+([,.;:!?])', r'\1', text)
    text = re.sub(r',\s*([,.;:!?])', r'\1', text)        # an em dash became a comma, then
    #                                                      the clause after it went
    text = text.replace('...', '\x00ELL\x00')            # protect real ellipses
    text = re.sub(r'\.\s*\.+', '.', text)
    text = re.sub(r'([!?])\s*\.', r'\1', text)
    text = text.replace('\x00ELL\x00', '...')
    text = re.sub(r'(?m)^[ \t]+$', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    # An em dash that became a comma, followed by a connective, leaves a
    # splice ("is important, also, it's proof"): promote it to a full stop.
    text = re.sub(r',\s*(also|so|still|basically|in the end)\s*,\s*',
                  lambda m: '. ' + m.group(1)[0].upper() + m.group(1)[1:] + ', ', text)
    return text, hits


def _without_emoji(text: str, keep_emoji: Iterable[str]) -> str:
    """``text`` with her allowlisted emoji removed, for the emoji tells.

    Each emoji of an item counts on its own (an item "✨💄" keeps both, not
    only the pair as written), with or without its variation selector.
    """
    for item in keep_emoji or ():
        for emoji in EMOJI_RE.findall(item) if isinstance(item, str) else ():
            text = text.replace(emoji, '').replace(emoji.replace(_VS16, ''), '')
    return text


def _structures(text: str, lex: Mapping[str, Any], keep_emoji: Iterable[str]) -> List[dict]:
    """``[{'id', 'name', 'count', 'fix'}]`` for each structural tell found."""
    flags = []
    plain = _without_emoji(text, keep_emoji)
    for s in lex['structures']:
        try:
            pattern = re.compile(s['regex'], re.MULTILINE)
        except re.error as exc:
            logger.warning('igkit slop structure %r has a bad regex, skipped: %s',
                           s.get('id'), exc)
            continue
        found = pattern.findall(plain if s.get('emoji') else text)
        if found:
            flags.append({'id': s['id'], 'name': s['name'], 'count': len(found), 'fix': s['fix']})
    return flags


def _uniformity(text: str) -> Optional[dict]:
    """Sentence-length uniformity is a structural tell too."""
    lens = [len(s.split()) for s in sentences(text) if len(s.split()) > 2]
    if len(lens) < 4:
        return None
    mean = sum(lens) / len(lens)
    cv = statistics.pstdev(lens) / mean if mean else 0
    if cv >= 0.35:
        return None
    return {'id': 'uniform-sentences', 'name': 'Uniform sentence length (variation %.2f)' % cv,
            'count': len(lens),
            'fix': 'Break one sentence in half. Let another run long. Machines write even.'}


def _restore_capitals(original: str, text: str) -> str:
    """Deleting an opener leaves the next word lower case.  Fixed only for
    writers who capitalise their sentences: a deliberately lower-case voice
    is a style, not an artefact."""
    starts = re.findall(r'(?:^|[.!?]\s+|\n)\s*([A-Za-z])', original)
    if not starts or sum(1 for c in starts if c.isupper()) * 2 < len(starts):
        return text
    return re.sub(r'(?:^|(?<=[.!?] )|(?<=[.!?]\n)|(?<=\n))\s*([a-z])',
                  lambda m: m.group(0)[:-1] + m.group(1).upper(), text)


def humanize_text(text: str, lexicon: Optional[Mapping[str, Any]] = None, *,
                  keep_emoji: Iterable[str] = (),
                  keep_words: Iterable[str] = ()) -> Dict[str, Any]:
    """Clean a caption or script; report what changed and what to rewrite.

    ``lexicon`` defaults to resources/slop.json.  ``keep_emoji`` (her voice
    profile's emoji; each emoji of an item counts on its own) are never
    reported as tells; ``keep_words`` (words she uses) are never replaced.
    Emoji are never deleted.  Whitespace comes back as a phone writes it:
    "\\n" line breaks, at most one blank line, single spaces.

    Returns ``{'text', 'report': {'invisible', 'typographic', 'lexical',
    'structures'}}``; ``structures`` entries are ``{'id', 'name', 'count',
    'fix'}`` and are for her agent to rewrite by hand.
    """
    lex = _lexicon(lexicon)
    original = text if isinstance(text, str) else ''
    out, urls = _protect_urls(original)
    out, invisible = _pass_invisible(out, lex)
    out = _fold_space(out)
    out, typographic = _pass_typographic(out, lex)
    out, lexical = _pass_lexical(out, lex, keep_words)
    out = _restore_capitals(_fold_space(original), out)
    out = _restore_urls(out, urls).strip()
    structures = _structures(out, lex, keep_emoji)
    uniform = _uniformity(out)
    if uniform:
        structures.append(uniform)
    return {'text': out, 'report': {'invisible': invisible, 'typographic': typographic,
                                    'lexical': lexical, 'structures': structures}}


# ── detect ──────────────────────────────────────────────────────────────────

def _scale(value: float, human: float, machine: float) -> float:
    """``value`` on 0-100 where ``human`` -> 100 and ``machine`` -> 0."""
    if human == machine:
        return 50.0
    return clamp_score((value - machine) / (human - machine) * 100)


def _mostly_latin(text: str) -> bool:
    letters = [c for c in text if unicodedata.category(c)[0] == 'L']
    ascii_letters = sum(1 for c in letters if ord(c) < 0x80)
    return ascii_letters * 2 >= len(letters)


def _check_burstiness(text: str) -> Tuple[float, str]:
    """People vary sentence length hard. Models write even."""
    lens = [len(s.split()) for s in sentences(text) if len(s.split()) > 2]
    if len(lens) < 4:
        return 50.0, 'too short to judge'
    mean = statistics.mean(lens)
    cv = statistics.pstdev(lens) / mean if mean else 0
    return (_scale(cv, human=0.70, machine=0.22),
            'variation %.2f across %d sentences (want 0.55+)' % (cv, len(lens)))


def _check_specificity(text: str, ws: List[str]) -> Tuple[float, str]:
    """Numbers, names, places and occasions. Slop is abstract."""
    if len(ws) < 25:
        return 50.0, 'too short to judge'
    hits = len(concrete_markers(text))
    density = hits * 100 / len(ws)
    return (_scale(density, human=6.0, machine=0.5),
            '%d concrete markers, %.1f per 100 words (want 4+)' % (hits, density))


def _check_slop(text: str, ws: List[str], lex: Mapping[str, Any],
                keep_words: Iterable[str]) -> Tuple[float, str]:
    """Stock vocabulary density against the lexicon."""
    if not ws:
        return 50.0, 'empty'
    kept = _kept(keep_words)
    hits, found = 0, []
    for entry in list(lex['words']) + list(lex['phrases']):
        if entry['find'].casefold() in kept:
            continue
        n = len(_lexicon_pattern(entry['find']).findall(text))
        if n:
            hits += n
            found.append(entry['find'])
    density = hits * 100 / len(ws)
    detail = '%d stock terms, %.1f per 100 words' % (hits, density)
    if found:
        detail += ' (' + ', '.join(sorted(found)[:4]) + (', ...' if len(found) > 4 else '') + ')'
    return _scale(density, human=0.0, machine=4.0), detail


def _check_fingerprint(text: str) -> Tuple[float, str]:
    """Characters a phone keyboard does not produce."""
    invisible = sum(1 for i in range(len(text)) if _invisible(text, i))
    em = text.count('—')
    curly = sum(text.count(c) for c in '‘’“”')
    ellip = text.count('…')
    hard = sum(text.count(c) for c in _HARD_SPACES)
    total = invisible * 4 + em * 2 + curly + ellip + hard
    per1k = total * 1000 / max(len(text), 1)
    return (_scale(per1k, human=0.0, machine=12.0),
            '%d invisible, %d em dash, %d curly quote, %d ellipsis, %d hard space'
            % (invisible, em, curly, ellip, hard))


def _check_voice(text: str, ws: List[str], lex: Mapping[str, Any],
                 keep_emoji: Iterable[str]) -> Tuple[float, str]:
    """Person, contractions, and the shapes models default to."""
    if len(ws) < 25:
        return 50.0, 'too short to judge'
    per100 = 100 / len(ws)
    person = sum(1 for k in word_keys(text) if k in _PERSON_WORDS) * per100
    tells = _structures(text, lex, keep_emoji)
    n_tells = sum(t['count'] for t in tells)
    names = [t['id'] for t in tells]
    bullets = [len(b.split()) for b in re.findall(r'(?m)^\s*[-*•]\s+(.+)$', text)]
    uniform = len(bullets) >= 3 and statistics.pstdev(bullets) < 1.6
    shape = clamp_score(100 - n_tells * 22)
    if _mostly_latin(text):
        contractions = len(_CONTRACTIONS.findall(text)) * per100
        score = (_scale(contractions, human=3.0, machine=0.0) * 0.35
                 + _scale(person, human=8.0, machine=1.0) * 0.35 + shape * 0.30)
        detail = '%.1f contractions, %.1f personal pronouns per 100 words' % (contractions,
                                                                             person)
    else:
        # Contractions are an English signal; Tamil, Hindi and Punjabi have
        # none to count, so person and shape carry the check.
        score = _scale(person, human=8.0, machine=1.0) * 0.5 + shape * 0.5
        detail = '%.1f personal pronouns per 100 words (contractions not measured)' % person
    if uniform:
        score -= 12
        names.append('uniform-bullets')
    detail += ', %d structural tell(s)' % n_tells
    if names:
        detail += ' [' + ', '.join(names[:4]) + ']'
    return clamp_score(score), detail


def _detect(text: str, lex: Mapping[str, Any], keep_emoji: Iterable[str],
            keep_words: Iterable[str]) -> Tuple[Dict[str, Any], float]:
    # The structure patterns would restart inside a long run of blank lines.
    text = _fold_space(text)
    ws = words(text)
    results = {
        'BURSTINESS': _check_burstiness(text),
        'SPECIFICITY': _check_specificity(text, ws),
        'SLOP DENSITY': _check_slop(text, ws, lex, keep_words),
        'FINGERPRINT': _check_fingerprint(text),
        'VOICE': _check_voice(text, ws, lex, keep_emoji),
    }
    scores = [results[c][0] for c in DETECT_CHECKS]
    # The weakest check drags the verdict: a detector only needs one signal.
    overall = statistics.mean(scores) * 0.6 + min(scores) * 0.4
    verdict = 'PASS' if overall >= 70 and min(scores) >= 55 else (
        'REVIEW' if overall >= 50 else 'FLAGGED')
    checks = {c: {'score': round(results[c][0], 1), 'detail': results[c][1]}
              for c in DETECT_CHECKS}
    weakest = min(DETECT_CHECKS, key=lambda c: checks[c]['score'])
    return {'checks': checks, 'human_score': round(overall, 1), 'verdict': verdict,
            'weakest': weakest}, overall


def detect_machine_text(text: str, before: Optional[str] = None, *,
                        keep_emoji: Iterable[str] = (),
                        keep_words: Iterable[str] = ()) -> Dict[str, Any]:
    """Score how machine-written ``text`` looks, 0-100 per check, higher is
    more human.

    The overall score weights the mean at 60% and the weakest check at 40%;
    PASS needs 70+ with no check below 55, REVIEW 50+, else FLAGGED.  With
    ``before`` (an earlier draft) the result also carries ``before``
    (``{'human_score', 'verdict'}``) and ``delta`` (this minus that).

    Returns ``{'checks': {name: {'score', 'detail'}}, 'human_score',
    'verdict', 'weakest'}``.  These are local heuristics, not any public
    detector's verdict; never tell her a text is undetectable.  Runs of
    blank lines and spaces are folded first, as humanize_text does.
    """
    lex = _default_lexicon()
    text = text if isinstance(text, str) else ''
    result, overall = _detect(text, lex, keep_emoji, keep_words)
    if before is not None:
        prev, prev_overall = _detect(before if isinstance(before, str) else '', lex,
                                     keep_emoji, keep_words)
        result['before'] = {'human_score': prev['human_score'], 'verdict': prev['verdict']}
        result['delta'] = round(overall - prev_overall, 1)
    return result
