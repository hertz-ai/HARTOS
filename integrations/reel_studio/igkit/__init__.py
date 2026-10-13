"""igkit: Reel Studio's port of instagram-agent-skill, plus the text
primitives its tools share.

Derived from instagram-agent-skill
(https://github.com/Jakeschincariol/instagram-agent-skill), commit d03c56b
(2026-09-13), Copyright (c) 2026 Jake Schincariol, MIT License.  The full
license text is LICENSE.txt beside this file; HARTOS's NOTICE names it.

Where each part came from:

  hooks.py               skills/ig-reel/hookscore.py and skills/ig-reel/beats.py
  caption.py             skills/ig-caption/caption.py
  humanize.py            skills/ig-human/humanize.py and skills/ig-human/detect.py
  swipe.py               skills/ig-viral/swipe.py (the outlier maths)
  resources/hooks.json   skills/ig-reel/hooks.json
  resources/slop.json    skills/ig-human/slop.json
  resources/rubric.json  skills/ig-profile/rubric.json
  resources/safe_zones.json  the on-screen text rules of ig-reel and ig-story SKILL.md
  resources/voice_template.json  templates/voice.md, as typed fields
  skills/ig_reel, ig_caption, ig_human   the three SKILL.md texts, rewritten

What was changed for HARTOS:

 1. Pure functions that return dicts.  No command line and no printing
    (upstream crashed printing Indic text to a Windows console), no file
    paths, no ``sys.path`` import hack.
 2. Words in any script.  A word is a run of letters, combining marks,
    digits and joiners; upstream's ``[A-Za-z0-9]`` pattern counted a Tamil
    hook as 0 words.  Sentences also end at the danda (U+0964) and double
    danda (U+0965), and not at a decimal point ("₹1.5 lakh").
 3. One definition of "concrete" (``concrete_markers``) for the hook
    checker, the beat sheet, the caption linter and the detector, where
    upstream had four slightly different regexes.  It knows rupees, lakh and
    crore, number words in English, Tamil, Hindi and Punjabi, wedding
    occasions (muhurtham, mehendi, haldi, sangeet, reception) and makeup
    brands, because Indian scripts have no capital letters to find names by.
 4. Zero-width joiners are kept inside Indic words and emoji sequences
    (upstream deleted them, breaking the bride and family emoji, Malayalam
    chillu letters and Hindi conjuncts); a joiner beside a Latin, Greek or
    Cyrillic letter, accented or not, is still a watermark and still goes.
    Invisible characters are written as escapes in this package's source,
    never as literals, so no editor can strip them unseen.
 5. Hook checks are shown as WEAK/OK plus the weakest check, never as a
    score: upstream measured AUC 0.56 at telling a creator's hits from her
    misses.  Two upstream bugs are fixed: "hi" no longer marks "highlighter"
    as a weak opener, and a capitalised name now counts as the payload word.
 6. Hashtags in any script, against the one cap
    (``contracts.HASHTAG_LIMIT``).  The "hashtag wall" tell is gone: five
    tags is her cap, not a wall.
 7. Per-user allowlists: emoji she uses (a sparkle is a staple of makeup
    posts) and words she uses are not reported as machine tells.
 8. Indian booking asks: "DM for bookings", "bookings open", a WhatsApp
    number.
 9. Makeup hook formulas (bare face to bride, shade match, jewellery or
    drape reveal, client reaction), makeup risk words as stakes (cakey,
    oxidise, flash, humidity, sweat) and greetings in Tamil, Hindi and
    Punjabi.
10. swipe takes rows as dicts, drops muted songs, and groups and scores
    songs (``top_songs``) for the central trends job, which uses this one
    implementation of the outlier maths.  Counts written as text keep their
    decimals and multipliers ("1.2M", "4.5k", "1.5 lakh"); upstream deleted
    every non-digit and read "1.2M" as 12.
11. caption counts an emoji once with its variation selector, skin tone and
    joined parts; upstream counted the selector as an emoji of its own.

This module imports only the standard library.
"""
from __future__ import annotations

import re
import unicodedata
from typing import List

#: The upstream revision this package was derived from.
UPSTREAM = 'instagram-agent-skill@d03c56b'

#: Zero-width non-joiner and joiner: they belong inside Indic conjuncts and
#: emoji sequences.
JOINERS = frozenset('\u200c\u200d')

#: Characters kept inside a word beside letters and digits: "$18,000",
#: "₹15k", "12-hour", "don't".
_WORD_EXTRA = frozenset("$%'’-₹_")

#: "$18,000" is one word when it is spoken, so it is one word here too.
_DIGIT_COMMA = re.compile(r'(?<=\d),(?=\d)')

#: A sentence: up to . ! ? the danda, the double danda or a line break; a
#: point between two digits ("₹1.5 lakh") is a decimal point, not an end.
_SENTENCE = re.compile(r'(?:[^.!?।॥\n]|(?<=\d)\.(?=\d))+[.!?।॥]*')

#: A figure: money ($, ₹, k, lakh, crore) or a number with an optional unit.
FIGURE_RE = re.compile(
    r'[$₹]\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:k|l|lakhs?|cr|crores?)\b)?'
    r'|\b\d[\d,]*(?:\.\d+)?\s?(?:%|k\b|x\b|hrs?\b|hours?\b|mins?\b|minutes?\b|secs?\b'
    r'|seconds?\b|days?\b|weeks?\b|months?\b|years?\b|lakhs?\b|crores?\b|am\b|pm\b)?',
    re.IGNORECASE)

#: A capitalised Latin word that does not start a line or a sentence.
NAME_RE = re.compile(r'(?<![.!?।॥]\s)(?<!^)\b[A-Z][a-z]{2,}\b', re.MULTILINE)

#: Number words said out loud.  "one" and "first" are deliberately absent
#: (upstream: they are filler far more often than a quantity).
NUMBER_WORDS = frozenset({
    'zero', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten',
    'eleven', 'twelve', 'fifteen', 'twenty', 'thirty', 'forty', 'fifty', 'sixty',
    'seventy', 'eighty', 'ninety', 'hundred', 'thousand', 'million', 'billion', 'dozen',
    'half', 'twice', 'triple',
    # Tamil
    'இரண்டு', 'மூன்று', 'நான்கு', 'ஐந்து', 'ஆறு', 'ஏழு', 'எட்டு', 'ஒன்பது', 'பத்து',
    'பன்னிரண்டு', 'பதினைந்து', 'இருபது', 'முப்பது', 'நாற்பது', 'ஐம்பது', 'நூறு', 'ஆயிரம்',
    # Hindi
    'दो', 'तीन', 'चार', 'पाँच', 'पांच', 'छह', 'छः', 'सात', 'आठ', 'नौ', 'दस', 'बारह',
    'पंद्रह', 'बीस', 'तीस', 'चालीस', 'पचास', 'सौ', 'हज़ार', 'हजार', 'दर्जन', 'आधा',
    # Punjabi
    'ਦੋ', 'ਤਿੰਨ', 'ਚਾਰ', 'ਪੰਜ', 'ਛੇ', 'ਸੱਤ', 'ਅੱਠ', 'ਨੌਂ', 'ਦਸ', 'ਵੀਹ', 'ਤੀਹ', 'ਪੰਜਾਹ',
    'ਸੌ', 'ਹਜ਼ਾਰ',
})

MONEY_WORDS = frozenset({
    'dollars', 'dollar', 'bucks', 'grand', 'percent', 'cents', 'millionaire',
    'billionaire', 'revenue', 'profit', 'salary', 'rent',
    'rupees', 'rupee', 'rs', 'inr', 'lakh', 'lakhs', 'crore', 'crores',
    'ரூபாய்', 'லட்சம்', 'கோடி',
    'रुपये', 'रुपए', 'रुपया', 'लाख', 'करोड़', 'करोड',
    'ਰੁਪਏ', 'ਲੱਖ', 'ਕਰੋੜ',
})

#: Wedding occasions: the concrete "when" of a bridal reel.
OCCASIONS = frozenset({
    'muhurtham', 'muhurtam', 'muhurat', 'mehendi', 'mehndi', 'haldi', 'sangeet',
    'reception', 'nischayathartham', 'roka',
    'முகூர்த்தம்', 'மெஹந்தி', 'மெஹெந்தி', 'ஹல்தி', 'சங்கீத்', 'ரிசப்ஷன்', 'நிச்சயதார்த்தம்',
    'मुहूर्त', 'मेहंदी', 'मेहँदी', 'हल्दी', 'संगीत', 'रिसेप्शन', 'सगाई', 'रोका',
    'ਮਹਿੰਦੀ', 'ਹਲਦੀ', 'ਸੰਗੀਤ', 'ਰਿਸੈਪਸ਼ਨ', 'ਮੰਗਣੀ',
})

#: Makeup brands, matched in lower case ("MAC" has no lower-case letters for
#: the name pattern to find).
BRANDS = frozenset({
    'mac', 'kryolan', 'huda', 'nykaa', 'lakme', 'lakmé', 'maybelline', 'nars', 'fenty',
    'dior', 'clinique', 'revlon', 'loreal', "l'oreal", 'sephora', 'colorbar', 'pac',
    'inglot', 'anastasia',
})

#: Words that make a line concrete on their own.
CONCRETE_WORDS = NUMBER_WORDS | MONEY_WORDS | OCCASIONS | BRANDS

#: Second person: a line aimed at the viewer.
YOU_WORDS = frozenset({
    'you', 'your', "you're", 'youre', 'yourself', "you've", "you'll", "you'd", 'yours',
    'நீங்க', 'நீங்கள்', 'உங்க', 'உங்கள்', 'உங்களுக்கு', 'உங்களோட', 'நீ', 'உன்', 'உனக்கு',
    'आप', 'आपका', 'आपकी', 'आपके', 'आपको', 'तुम', 'तुम्हारा', 'तुम्हारी', 'तुम्हें',
    'ਤੁਸੀਂ', 'ਤੁਹਾਡਾ', 'ਤੁਹਾਡੀ', 'ਤੁਹਾਨੂੰ',
})

#: First person.
ME_WORDS = frozenset({
    'i', 'my', 'me', 'we', 'our', 'mine', 'us',
    'நான்', 'என்', 'என்னோட', 'எனக்கு', 'என்னை', 'நாங்க', 'நாங்கள்', 'எங்க', 'எங்கள்',
    'मैं', 'मैंने', 'मेरा', 'मेरी', 'मेरे', 'मुझे', 'मुझसे', 'हम', 'हमने', 'हमारा', 'हमारी',
    'ਮੈਂ', 'ਮੇਰਾ', 'ਮੇਰੀ', 'ਮੈਨੂੰ', 'ਅਸੀਂ', 'ਸਾਡਾ',
})


def _is_word_char(ch: str) -> bool:
    return ch in _WORD_EXTRA or is_tag_char(ch)


def is_tag_char(ch: str) -> bool:
    """Whether ``ch`` may follow the '#' of a hashtag: a letter, combining
    mark or digit in any script, '_' or a joiner ("#சென்னைமேக்கப்" is one
    tag)."""
    return ch == '_' or ch in JOINERS or unicodedata.category(ch)[0] in 'LMN'


def _has_letter_or_digit(token: str) -> bool:
    return any(unicodedata.category(ch)[0] in 'LN' for ch in token)


def words(text: str) -> List[str]:
    """The words of ``text`` in any script.

    A word is a run of letters, combining marks (Tamil and Devanagari vowel
    signs and viramas), digits, zero-width joiners and ``$ % ' ’ - ₹ _``.
    Thousands separators between digits are dropped, so "₹1,50,000" is one
    word.  A run with no letter or digit (a lone "-") is not a word.
    """
    out: List[str] = []
    cur: List[str] = []
    for ch in _DIGIT_COMMA.sub('', text or ''):
        if _is_word_char(ch):
            cur.append(ch)
        elif cur:
            out.append(''.join(cur))
            cur = []
    if cur:
        out.append(''.join(cur))
    return [w for w in out if _has_letter_or_digit(w)]


def word_keys(text: str) -> List[str]:
    """``words(text)`` lower-cased with edge apostrophes stripped: the form
    the word lists in this package are written in."""
    return [w.lower().strip("'’") for w in words(text)]


def sentences(text: str) -> List[str]:
    """Sentences and lines of ``text``, each with its closing mark.

    A sentence ends at . ! ? the danda or the double danda, or at a line
    break.  A point between two digits ("₹1.5 lakh") does not end one.
    """
    return [s.strip() for s in _SENTENCE.findall(text or '') if s.strip()]


def concrete_markers(text: str) -> List[str]:
    """The checkable things in ``text``: figures, then names, then words.

    Figures are numbers with an optional unit and money ("$18,000", "₹15k",
    "40 minutes").  Names are capitalised Latin words that do not start a
    line or a sentence, each once.  Words are number words, money words,
    wedding occasions and makeup brands in any of the scripts above; a word
    already found as a name is not counted twice.
    """
    text = text or ''
    found = [m.strip() for m in FIGURE_RE.findall(text) if m.strip()]
    names: List[str] = []
    for name in NAME_RE.findall(text):
        if name not in names:
            names.append(name)
    named = {n.casefold() for n in names}
    found += names
    found += [w for w in words(text)
              if w.lower().strip("'’") in CONCRETE_WORDS and w.casefold() not in named]
    return found
