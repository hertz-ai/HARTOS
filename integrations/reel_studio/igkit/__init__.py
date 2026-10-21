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
  (the word list)        skills/ig-human/slop.json, folded into the human-voice
                         guide's one list (integrations/agent_engine/
                         human_voice.py, owner ruling A10c)
  resources/rubric.json  skills/ig-profile/rubric.json
  resources/safe_zones.json  the on-screen text rules of ig-reel and ig-story SKILL.md
  resources/voice_template.json  templates/voice.md, as typed fields
  skills/ig_reel, ig_caption, ig_human   the three SKILL.md texts, rewritten

skills/reel_edit is not derived from upstream: it is HARTOS's own text,
under HARTOS's Apache-2.0 license (see igkit.skills).

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
 6. Hashtags in any script, against the one reel cap, the human-voice
    guide's ``REEL_HASHTAG_LIMIT`` (``contracts.HASHTAG_LIMIT`` is the same
    constant).  The "hashtag wall" tell is gone: five tags is a reel's cap,
    not a wall.
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
11. One emoji definition (``EMOJI_RE``) for the caption linter, the hook
    check, the humanizer and her voice profile, where upstream's caption and
    hook scripts each had their own list.  An emoji counts once with its
    variation selector, skin tone and joined parts (upstream counted the
    selector as an emoji of its own, so a red heart was 2); keycaps and flags
    are emoji; a plain arrow, '!!' or '-' is not.
12. One hashtag rule (``hashtags``) for the caption linter, the hook check
    and her hashtag bank, where upstream had two: tags in any script, each of
    "#a#b" counted (upstream counted it once, so leaving out spaces hid tags
    from the cap), and a tag right after an emoji or a bracket counted;
    a '#' inside a word ("page#rates") is not a tag.
13. One list of AI-sounding words, the human-voice guide's
    (``integrations.agent_engine.human_voice``): the tells HARTOS's prompts
    ban by name are tells the humanizer replaces, and upstream's slop.json
    is no longer a second list beside it.

This module imports only the standard library.
"""
from __future__ import annotations

import re
import unicodedata
from typing import List, Tuple

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


def hashtags(text: str) -> List[Tuple[str, int, int]]:
    """``[(tag, start, end)]`` for every hashtag in ``text``, in order.

    A tag is '#' and the tag characters after it (``is_tag_char``), so
    "#சென்னைமேக்கப்" is one tag.  A '#' starts a tag when it does not follow
    a tag character, or right where the tag before it ends: "#a#b" is two
    tags (leaving out the spaces never hides a tag from the cap), "✨#bridal"
    and "(#bridal)" are tags, and "page#rates" and "C#" are not.  One pass
    over the text.
    """
    text = text if isinstance(text, str) else ''
    out: List[Tuple[str, int, int]] = []
    end = -1
    i = text.find('#')
    while i != -1:
        if i == end or i == 0 or not is_tag_char(text[i - 1]):
            j = i + 1
            while j < len(text) and is_tag_char(text[j]):
                j += 1
            if j > i + 1:
                out.append((text[i:j], i, j))
                end = j
                i = text.find('#', j)
                continue
        i = text.find('#', i + 1)
    return out


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


# ── emoji ───────────────────────────────────────────────────────────────────

#: Code points that are an emoji on their own: the pictograph planes (faces,
#: hands, objects, food, and the regional-indicator letters of flags), the
#: miscellaneous-symbol and dingbat blocks (sparkle, heart, check mark), and
#: the few emoji in other blocks (watch, hourglass, play buttons, squares,
#: star, circle).
_EMOJI_BASE = ('\U0001F000-\U0001FAFF☀-➿⌚⌛⏩-⏳⏸-⏺'
               '◽◾⬛⬜⭐⭕')
#: Symbols that are text unless the emoji variation selector follows: (c),
#: (r), !!, !?, TM, i, the arrows from left-right to hooked, keyboard, eject,
#: circled M, small squares, play and reverse, curving arrows, left/up/down
#: arrows, wavy dash, part mark, and the circled ideographs.
_TEXT_SYMBOLS = ('©®‼⁉™ℹ↔-↙↩↪⌨⏏'
                 'Ⓜ▪▫▶◀◻◼⤴⤵⬅-⬇'
                 '〰〽㊗㊙')
_ONE_EMOJI = (
    '[0-9#*]\ufe0f?\u20e3'                               # keycap: 1, # or * in a box
    '|[\U0001F1E6-\U0001F1FF]{2}'                        # flag: two regional indicators
    '|\U0001F3F4[\U000E0020-\U000E007E]+\U000E007F'      # flag of a region (England)
    '|[' + _EMOJI_BASE + '](?:\ufe0f|[\U0001F3FB-\U0001F3FF])?'  # with selector or skin tone
    '|[' + _TEXT_SYMBOLS + ']\ufe0f'
)
#: One emoji as she sees it: a keycap, a flag, or a code point with its
#: variation selector or skin tone and any more that a zero-width joiner adds
#: (a bride, a family).  The one emoji definition for the caption linter, the
#: hook check, the humanizer and her voice profile.
EMOJI_RE = re.compile('(?:' + _ONE_EMOJI + ')(?:\u200d(?:' + _ONE_EMOJI + '))*')


# ── small shared values ─────────────────────────────────────────────────────

def clamp_score(n: float) -> float:
    """``n`` within 0-100, the range of every igkit score, as a float.  The
    one clamp in igkit (hooks and humanize import it).  A yes/no is not read
    here: core.config_cache.flag_value is the one reading in HARTOS."""
    return float(max(0.0, min(100.0, n)))
