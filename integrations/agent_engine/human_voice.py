"""The human-voice guide: the ONE source of published-text style.

``goal_manager.HUMAN_VOICE_GUIDE`` is this module's HUMAN_VOICE_GUIDE (the
same object).  Every goal builder that writes PUBLISHED text (marketing,
content, news, SEO, paper explanations) puts it in its prompt.  The rule the
user set: AI-sounding content is exactly what people scroll past and
discard, so HOW it reads matters more than what it says.  Import it; never
re-state it inline (no parallel copy).

Owner ruling A10c (2026-10-14) keeps the guide the single source for:

* hashtags: a reel caption may carry up to REEL_HASHTAG_LIMIT; every other
  published text keeps POST_HASHTAG_LIMIT or fewer.  Reel Studio's contracts
  (their HASHTAG_LIMIT), its caption linter and its caption skill read
  REEL_HASHTAG_LIMIT from here, so no second literal can drift.  A text her
  agent reads that states the cap (a skill file, her profile's hashtag-bank
  label and help) writes REEL_HASHTAG_LIMIT_TOKEN, which
  with_reel_hashtag_limit fills; reel_make's parameter text is built from
  contracts.HASHTAG_LIMIT.
* the AI-sounding word list: WORDS and PHRASES (the tells and the plain
  words they become), plus the INVISIBLE and TYPOGRAPHIC characters and the
  sentence STRUCTURES that give machine text away.  The guide names the
  tells marked ``quoted``; Reel Studio's humanizer and detector
  (integrations.reel_studio.igkit.humanize) read the whole list through
  machine_text_lexicon().  The list used to be igkit's resources/slop.json,
  a second lexicon beside the guide's; that file is gone.
* the public docs' tells: scripts/check_docs_claims.py reports WORDS and
  PHRASES in the docs a stranger lands on, and DOCS_TELLS, this list's
  docs-only extension (amendment A20, D30: the checker's own FILLER list
  folded in).  CI runs that checker on a bare Python and it loads this file
  directly, so this module must keep importing only the standard library.
* how a tell matches: tell_pattern, for every reader of the list.

The guide is kept deliberately tight: the local ~4B model executes these
prompts and a wall of rules hurts its completion rate, so it names a few
tells "and their close cousins", never the whole list.

Why a module and not goal_manager.py itself: Reel Studio reads these facts
when it builds an agent's tools, and goal_manager.py pulls in SQLAlchemy and
the channel registry; this module imports only the standard library.  It
also keeps goal_manager.py under the repo's 3,000-line ratchet
(tests/unit/test_source_guard_repo_health_ratchet.py).

THIRD-PARTY.  The tables are derived from instagram-agent-skill @d03c56b
skills/ig-human/slop.json, Copyright (c) 2026 Jake Schincariol, MIT License.
The full license text is integrations/reel_studio/igkit/LICENSE.txt; HARTOS's
NOTICE names this file.  The last block of each upstream table is
Instagram-specific: caption and voice-over slop that does not show up on
other platforms.  Changed for HARTOS:
  - U+200C and U+200D are 'keep_joining': kept inside Indic words and emoji
    sequences, deleted elsewhere.  Upstream deleted them, breaking the bride
    and family emoji, Malayalam chillu letters and Hindi conjuncts.
  - Upstream's 'hashtag-wall' structure is removed: it fired at five tags,
    which is a reel's cap, and its advice was written for LinkedIn.
  - Structures marked ``emoji`` ignore the emoji in her allowlist.
  - The entries commented ``guide`` are HARTOS's: tells the guide banned by
    name before the fold that upstream's list lacked.
  - The entries commented ``docs`` are HARTOS's: forms the docs checker's
    FILLER list caught that the tables lacked ('effortless' beside
    'effortlessly', 'revolutionizing' beside 'revolutionize').
  - Characters a reader cannot tell apart are written as escapes.
"""
from __future__ import annotations

import functools
import re
from types import MappingProxyType
from typing import Any, Mapping, NamedTuple

#: Hashtags a reel caption may carry (owner ruling A10c).
REEL_HASHTAG_LIMIT = 5
#: Hashtags any other published text may carry (zero is fine).
POST_HASHTAG_LIMIT = 2
#: Where a text an agent reads states the reel cap (a Reel Studio skill
#: file, her profile's field labels), it writes this token, and
#: with_reel_hashtag_limit puts REEL_HASHTAG_LIMIT in its place: no such
#: text holds a second copy of the number.
REEL_HASHTAG_LIMIT_TOKEN = '{reel_hashtag_limit}'


def with_reel_hashtag_limit(text: str) -> str:
    """``text`` with each REEL_HASHTAG_LIMIT_TOKEN replaced by
    REEL_HASHTAG_LIMIT, read at the call."""
    return text.replace(REEL_HASHTAG_LIMIT_TOKEN, str(REEL_HASHTAG_LIMIT))


class Tell(NamedTuple):
    """An AI-sounding word or phrase and the plain words it becomes.

    ``find`` matches as tell_pattern says: whole words, in any case, across
    any run of spaces; ``replace`` '' deletes it; ``quoted`` means the guide
    names it.
    """
    find: str
    replace: str
    family: str
    quoted: bool = False


def tell_pattern(find: str, any_ending: bool = False) -> re.Pattern:
    """The one way a tell's ``find`` matches text: as whole words, in any
    case, across any run of spaces.  A find that ends on punctuation ('what
    are your thoughts?') ends there: the next character only has to be one
    that is not part of a word.

    ``any_ending`` also matches the word with any ending ('honest' finds
    'honestly', 'leverage' finds 'leveraged'), for REPORTING a word's family
    as scripts/check_docs_claims.py does.  A rewrite must match the exact
    form, since each form has its own replacement, so igkit.humanize never
    sets it.
    """
    ending = r'\w*' if any_ending and find[-1:].isalnum() else ''
    return re.compile(r'(?<!\w)' + re.escape(find).replace(r'\ ', r'\s+') + ending + r'(?!\w)',
                      re.IGNORECASE)


class Structure(NamedTuple):
    """A sentence shape that reads as machine-written.  Reported for a person
    to rewrite, never rewritten automatically.  ``emoji`` shapes ignore the
    emoji in her allowlist."""
    id: str
    regex: str
    name: str
    fix: str
    emoji: bool = False


#: Invisible characters: (code point or range, name, action).  'delete'
#: drops it, 'space' makes it a plain space, 'keep_joining' keeps a joiner
#: inside an Indic word or an emoji sequence and drops it anywhere else.
INVISIBLE = (
    ('U+200B', 'ZERO WIDTH SPACE', 'delete'),
    ('U+200C', 'ZERO WIDTH NON-JOINER', 'keep_joining'),
    ('U+200D', 'ZERO WIDTH JOINER', 'keep_joining'),
    ('U+2060', 'WORD JOINER', 'delete'),
    ('U+FEFF', 'ZERO WIDTH NO-BREAK SPACE / BOM', 'delete'),
    ('U+00AD', 'SOFT HYPHEN', 'delete'),
    ('U+180E', 'MONGOLIAN VOWEL SEPARATOR', 'delete'),
    ('U+061C', 'ARABIC LETTER MARK', 'delete'),
    ('U+200E', 'LEFT-TO-RIGHT MARK', 'delete'),
    ('U+200F', 'RIGHT-TO-LEFT MARK', 'delete'),
    ('U+E0000-U+E007F', 'TAG CHARACTERS (invisible ASCII smuggling)', 'delete'),
    ('U+00A0', 'NO-BREAK SPACE', 'space'),
    ('U+202F', 'NARROW NO-BREAK SPACE', 'space'),
    ('U+2009', 'THIN SPACE', 'space'),
    ('U+2007', 'FIGURE SPACE', 'space'),
    ('U+2003', 'EM SPACE', 'space'),
    ('U+2002', 'EN SPACE', 'space'),
    ('U+2063', 'INVISIBLE SEPARATOR', 'delete'),
)

#: Typographic characters a phone keyboard does not type: (character, name,
#: what it becomes).
TYPOGRAPHIC = (
    ('\N{EM DASH}', 'EM DASH', ', '),
    ('\N{EN DASH}', 'EN DASH', '-'),
    ('\N{LEFT SINGLE QUOTATION MARK}', 'LEFT SINGLE QUOTE', "'"),
    ('\N{RIGHT SINGLE QUOTATION MARK}', 'RIGHT SINGLE QUOTE', "'"),
    ('\N{LEFT DOUBLE QUOTATION MARK}', 'LEFT DOUBLE QUOTE', '"'),
    ('\N{RIGHT DOUBLE QUOTATION MARK}', 'RIGHT DOUBLE QUOTE', '"'),
    ('\N{HORIZONTAL ELLIPSIS}', 'HORIZONTAL ELLIPSIS', '...'),
    ('\N{PRIME}', 'PRIME', "'"),
    ('\N{MIDDLE DOT}', 'MIDDLE DOT', '-'),
    ('\N{BULLET}', 'BULLET', '-'),
    ('\N{RIGHTWARDS ARROW}', 'RIGHTWARDS ARROW', '->'),
)

#: Stock words and short phrases, each with plain words that keep the
#: sentence grammatical.
WORDS = (
    Tell('delve into', 'look at', 'verbs'),
    Tell('delve', 'look', 'verbs', quoted=True),
    Tell('dive deep into', 'get into', 'verbs'),
    Tell('dive into', 'look at', 'verbs', quoted=True),               # guide
    Tell('dive in', 'start', 'verbs', quoted=True),                   # guide
    Tell('deep dive', 'breakdown', 'nouns'),
    Tell('leverage', 'use', 'verbs', quoted=True),
    Tell('leveraging', 'using', 'verbs'),
    Tell('utilize', 'use', 'verbs'),
    Tell('utilizing', 'using', 'verbs'),
    Tell('harness', 'use', 'verbs'),
    Tell('harness the power of', 'use', 'verbs', quoted=True),        # guide
    Tell('the power of', '', 'nouns', quoted=True),                   # guide
    Tell('embark on', 'start', 'verbs'),
    Tell('foster', 'build', 'verbs'),
    Tell('facilitate', 'help', 'verbs'),
    Tell('showcase', 'show', 'verbs'),
    Tell('unlock', 'get', 'verbs', quoted=True),
    Tell('elevate', 'improve', 'verbs', quoted=True),
    Tell('streamline', 'simplify', 'verbs'),
    Tell('spearhead', 'lead', 'verbs'),
    Tell('underscore', 'show', 'verbs'),
    Tell('underscores', 'shows', 'verbs'),
    Tell('cultivate', 'build', 'verbs'),
    Tell('amplify', 'boost', 'verbs'),
    Tell('curate', 'pick', 'verbs'),
    Tell('curated', 'picked', 'verbs'),
    Tell('empower', 'let', 'verbs'),
    Tell('revolutionize', 'change', 'verbs'),
    Tell('revolutionizing', 'changing', 'verbs'),                     # docs
    Tell('revolutionary', 'new', 'adjectives', quoted=True),          # guide
    Tell('transformative', 'big', 'adjectives', quoted=True),
    Tell('robust', 'solid', 'adjectives', quoted=True),
    Tell('seamless', 'clean', 'adjectives', quoted=True),
    Tell('seamlessly', 'cleanly', 'adjectives'),
    Tell('pivotal', 'key', 'adjectives'),
    Tell('crucial', 'important', 'adjectives'),
    Tell('vital', 'important', 'adjectives'),
    Tell('comprehensive', 'complete', 'adjectives'),
    Tell('cutting-edge', 'new', 'adjectives', quoted=True),
    Tell('game-changing', 'big', 'adjectives'),
    Tell('game-changer', 'big deal', 'nouns', quoted=True),
    Tell('groundbreaking', 'new', 'adjectives'),
    Tell('unparalleled', 'unmatched', 'adjectives'),
    Tell('invaluable', 'useful', 'adjectives'),
    Tell('meticulous', 'careful', 'adjectives'),
    Tell('meticulously', 'carefully', 'adjectives'),
    Tell('myriad of', 'many', 'adjectives'),
    Tell('myriad', 'many', 'adjectives'),
    Tell('multifaceted', 'complicated', 'adjectives'),
    Tell('holistic', 'whole', 'adjectives'),
    Tell('bespoke', 'custom', 'adjectives'),
    Tell('innovative', 'new', 'adjectives'),
    Tell('profound', 'big', 'adjectives'),
    Tell('remarkable', 'notable', 'adjectives'),
    Tell('compelling', 'convincing', 'adjectives'),
    Tell('tapestry', 'mix', 'nouns'),
    Tell('realm', 'world', 'nouns'),
    Tell('landscape', 'market', 'nouns'),
    Tell('journey', 'process', 'nouns'),
    Tell('testament to', 'proof of', 'nouns'),
    Tell('cornerstone', 'base', 'nouns'),
    Tell('beacon', 'signal', 'nouns'),
    Tell('paradigm', 'model', 'nouns'),
    Tell('synergy', 'overlap', 'nouns'),
    Tell('ecosystem', 'system', 'nouns'),
    Tell('treasure trove', 'pile', 'nouns'),
    Tell('plethora of', 'lots of', 'nouns'),
    Tell('plethora', 'lots', 'nouns'),
    Tell('arsenal', 'set', 'nouns'),
    Tell('north star', 'goal', 'nouns'),
    Tell('moreover', 'also', 'connectives', quoted=True),
    Tell('furthermore', 'also', 'connectives', quoted=True),
    Tell('additionally', 'also', 'connectives'),
    Tell('nevertheless', 'still', 'connectives'),
    Tell('consequently', 'so', 'connectives'),
    Tell('thus', 'so', 'connectives'),
    Tell('hence', 'so', 'connectives'),
    Tell('notably', '', 'connectives'),
    Tell('importantly', '', 'connectives'),
    Tell('ultimately', 'in the end', 'connectives'),
    Tell('in essence', 'basically', 'connectives'),
    Tell('in conclusion', 'so', 'connectives', quoted=True),
    Tell('when it comes to', 'with', 'connectives'),
    Tell('in order to', 'to', 'connectives'),
    Tell('a wide range of', 'many', 'connectives'),
    Tell('it is worth noting that', 'note that', 'connectives'),
    Tell("it's worth noting that", 'note that', 'connectives', quoted=True),  # guide
    Tell('supercharge', 'speed up', 'verbs'),
    Tell('skyrocket', 'raise', 'verbs'),
    Tell('unleash', 'release', 'verbs', quoted=True),
    Tell('effortless', 'easy', 'adjectives'),                         # docs
    Tell('effortlessly', 'easily', 'adjectives'),
    Tell('captivating', 'interesting', 'adjectives'),
    Tell('stunning', 'good', 'adjectives'),
    Tell('breathtaking', 'striking', 'adjectives'),
    Tell('iconic', 'known', 'adjectives'),
    Tell('must-have', 'worth having', 'adjectives'),
    Tell('no-brainer', 'obvious', 'nouns'),
    Tell('hidden gem', 'good one', 'nouns'),
    Tell('secret sauce', 'the actual method', 'nouns'),
    Tell('ultimate guide', 'guide', 'nouns'),
    Tell('level up', 'improve', 'verbs'),
    Tell('hack', 'trick', 'nouns'),
)

#: Stock openers, closers and connectives; '' deletes the phrase.
PHRASES = (
    Tell("in today's fast-paced world", 'right now', 'openers'),
    Tell("in today's digital age", 'right now', 'openers'),
    Tell("in today's world", 'right now', 'openers', quoted=True),               # guide
    Tell('in the ever-evolving landscape of', 'in', 'openers'),
    Tell('in the rapidly evolving landscape of', 'in', 'openers', quoted=True),  # guide
    Tell('in the ever-changing world of', 'in', 'openers'),
    Tell("let's face it", '', 'openers'),
    Tell("here's the thing", '', 'openers'),
    Tell('picture this', '', 'openers'),
    Tell('imagine a world where', 'imagine if', 'openers'),
    Tell("it's no secret that", '', 'openers'),
    Tell("we've all been there", '', 'openers'),
    Tell('let that sink in', '', 'closers'),
    Tell('the choice is yours', '', 'closers'),
    Tell('food for thought', '', 'closers'),
    Tell('the future is here', '', 'closers'),
    Tell('what are your thoughts?', '', 'closers'),
    Tell('drop a comment below', '', 'closers'),
    Tell('at the end of the day', '', 'closers'),
    Tell('needle-moving', 'useful', 'closers'),
    Tell('move the needle', 'make a difference', 'closers'),
    Tell('a testament to', 'proof of', 'nouns'),
    Tell('had the opportunity to', 'got to', 'connectives'),
    Tell('i am excited to announce', '', 'openers'),
    Tell("i'm thrilled to share", '', 'openers'),
    Tell('i am humbled to', '', 'openers'),
    Tell('without further ado', '', 'openers'),
    Tell('buckle up', '', 'openers'),
    Tell('the harsh truth is', '', 'openers'),
    Tell('spoiler alert', '', 'openers'),
    Tell('read that again', '', 'closers'),
    Tell("you're welcome", '', 'closers'),
    Tell('save this for later', '', 'closers'),
    Tell("who's with me", '', 'closers'),
    Tell('stop scrolling', '', 'openers'),
    Tell("don't scroll past this", '', 'openers'),
    Tell("in this video i'm going to show you", '', 'openers'),
    Tell("in today's video", '', 'openers'),
    Tell('watch till the end', '', 'openers'),
    Tell("you won't believe", '', 'openers'),
    Tell('nobody is talking about this', 'few people do this', 'openers'),
    Tell('no one is talking about this', 'few people do this', 'openers'),
    Tell('this changed everything', 'this worked', 'openers'),
    Tell('this is your sign to', '', 'openers'),
    Tell('little did i know', '', 'connectives'),
    Tell('fast forward to today', 'now', 'connectives'),
    Tell('and just like that', '', 'connectives'),
    Tell('the rest is history', '', 'closers'),
    Tell("run don't walk", '', 'closers'),
    Tell('trust me on this one', '', 'closers'),
    Tell("don't sleep on this", '', 'closers'),
    Tell('follow for more', '', 'closers'),
    Tell('hit follow for more', '', 'closers'),
    Tell('double tap if', '', 'closers'),
    Tell('tag someone who needs this', '', 'closers'),
    Tell('let me know in the comments', '', 'closers'),
    Tell('share this with someone who needs it', '', 'closers'),
    Tell('the algorithm loves', 'Instagram pushes', 'nouns'),
    Tell('beat the algorithm', 'get reach', 'nouns'),
    Tell('go viral overnight', 'get reach', 'nouns'),
)

#: The docs-only extension of WORDS and PHRASES (HARTOS's own): the words
#: the Hacker News thread on HARTOS's README (2026-07-27) counted, or
#: grepped for, to call the docs machine-written.  scripts/check_docs_claims.py
#: reports them as tells, beside WORDS and PHRASES, each with any ending.
#: Never rewritten, so machine_text_lexicon() leaves them out: in a reel
#: caption "an honest review" or "genuine lashes" is plain speech.
DOCS_TELLS = ('load-bearing', 'load bearing', 'honest', 'genuine',
              'earns its keep', 'earned its keep')

#: Sentence shapes, reported for a person to rewrite.
STRUCTURES = (
    Structure('not-just', r"(?i)\bit'?s not (?:just|only) [^.!?\n]{2,60}[,.] it'?s\b",
              '"It\'s not just X, it\'s Y"',
              'Say the one thing you actually mean. Delete the setup half.'),
    Structure('not-only', r'(?i)\bnot only\b[^.!?\n]{2,80}\bbut also\b',
              '"Not only X but also Y"',
              'Split into two sentences, or drop the weaker half.'),
    Structure('isnt-about',
              r"(?i)\b(?:this|it) is(?:n'?t| not) about [^.!?\n]{2,60}[.!] it'?s about\b",
              '"It isn\'t about X. It\'s about Y."',
              'Lead with Y. The reversal is the tell.'),
    Structure('the-result',
              r'(?i)^\s*(?:the result|the best part|the kicker|the catch)\?\s*$',
              'One-word rhetorical question line',
              'Answer it inline instead of staging a reveal.'),
    Structure('heres-what', r"(?i)\bhere'?s what (?:i|we) learned\b",
              '"Here\'s what I learned"',
              'State the lesson. Skip the announcement.'),
    Structure('triad', r'(?i)\b\w+, \w+, and \w+\.',
              'Rule-of-three triad',
              "Two items or four. Three is the model's default rhythm."),
    Structure('rocket', r'[\U0001F680\U0001F525\U0001F4A1\N{SPARKLES}\U0001F3AF]',
              'Rocket / fire / bulb / sparkle / dart emoji',
              'One emoji max, and not one of these five.', emoji=True),
    Structure('as-an-ai', r'(?i)\bas an ai\b|\bas a language model\b',
              'Model self-reference',
              'Delete the sentence.'),
    Structure('in-summary', r'(?i)^\s*(?:in summary|to summarize|in conclusion)\b',
              'Essay-closer opener',
              'A post does not need a conclusion paragraph.'),
    Structure('engagement-bait', r'(?i)\b(?:agree\?|thoughts\?|who else\b|am i wrong\?)\s*$',
              'Reflex engagement bait',
              'Ask a question only you could ask, about the specific thing above it.'),
    Structure('stop-scroll',
              r"(?im)^\s*(?:stop scrolling|don'?t scroll|wait[,.]? don'?t scroll)\b",
              '"Stop scrolling" opener',
              'The scroll-stop has to be the claim itself. Asking for attention is the '
              'tell that you have not earned it.'),
    Structure('in-this-video',
              r"(?i)\b(?:in (?:this|today'?s) (?:video|reel|post)|today i'?m going to show you"
              r"|i'?m going to (?:show|teach) you how)\b",
              'Video preamble ("in this video I\'m going to show you")',
              'Delete it and start at the payoff. The first two seconds decide the whole reel.'),
    # Not a raw string: Python turns the five named symbols after the emoji
    # range into the characters themselves; the range stays a regex escape.
    Structure('emoji-bullets',
              '(?m)^\\s*[\\U0001F300-\\U0001FAFF\N{WHITE HEAVY CHECK MARK}'
              '\N{BLACK RIGHTWARDS ARROW}\N{LEFTWARDS BLACK ARROW}\N{HEAVY CHECK MARK}'
              '\N{SPARKLES}]\\s*\\S',
              'Emoji bullet list',
              'Use a hyphen or a line break. Emoji bullets are the single most '
              'template-looking thing in a caption.', emoji=True),
    Structure('caps-shout', r'\b[A-Z]{3,}\b(?:\s+\b[A-Z]{3,}\b){2,}',
              'Three or more shouted words in a row',
              'Caps are a volume knob, not a highlighter. One word, at most.'),
    Structure('follow-bait',
              r'(?i)\b(?:follow for more|tag someone who|double tap if|share this if'
              r'|comment below if)\b',
              'Reflex follow or tag bait',
              'Ask for the one action this post actually earns. The generic ask trains the '
              'audience to ignore every ask.'),
)


@functools.lru_cache(maxsize=1)
def machine_text_lexicon() -> Mapping[str, Any]:
    """The whole list, read-only, in the shape Reel Studio's humanizer reads:
    ``{'invisible': ({'cp', 'name', 'action'}, ...), 'typographic': ({'from',
    'name', 'to'}, ...), 'words' and 'phrases': ({'find', 'replace',
    'family'}, ...), 'structures': ({'id', 'regex', 'name', 'fix', 'emoji'},
    ...)}``.  Built once; every caller shares it, so nothing may change it."""
    def rows(table, keys):
        return tuple(MappingProxyType(dict(zip(keys, row))) for row in table)

    tell_keys = ('find', 'replace', 'family')
    return MappingProxyType({
        'invisible': rows(INVISIBLE, ('cp', 'name', 'action')),
        'typographic': rows(TYPOGRAPHIC, ('from', 'name', 'to')),
        'words': rows(WORDS, tell_keys),
        'phrases': rows(PHRASES, tell_keys),
        'structures': rows(STRUCTURES, Structure._fields),
    })


def _named_tells() -> str:
    """The tells the guide names, quoted, in list order."""
    return ', '.join("'%s'" % t.find for t in WORDS + PHRASES if t.quoted)


HUMAN_VOICE_GUIDE = (
    "HOW TO WRITE (this decides whether anyone reads it):\n"
    "Write as ONE real person typing to another, not a brand, not an AI. "
    "Text that reads like AI gets scrolled past and discarded; that is the "
    "single biggest reason good work goes unseen. Earn the read:\n"
    "- HARD RULE, no exceptions: never use an em dash (the '—' character). "
    "Use a period, a comma, a colon, parentheses, or the word 'and' instead. "
    "A stray em dash is the loudest AI tell there is.\n"
    "- Ground every claim in the source in front of you: the paper's abstract, "
    "or the feature that actually shipped. Invent nothing. No made-up numbers, "
    "benchmarks, or results. When the source is thin, stay honest and general "
    "instead of filling the gap with something that merely sounds impressive.\n"
    "- Write like a specific person who knows the subject explaining it to a "
    "smart friend. Reach for a concrete analogy or bit of intuition when one "
    "genuinely fits, and skip it when it doesn't. Vary sentence length and how "
    "each sentence and paragraph opens. No templated cadence, and don't start "
    "every item the same way (for example, not always 'This paper...').\n"
    "- Open on one concrete, specific thing: a real moment, a number, a thing "
    "that actually happened. Never a mission statement or a rhetorical question.\n"
    "- Short sentences. Plain words. One idea per line. Cut every word that "
    "isn't load-bearing.\n"
    f"- Ban these tells and their close cousins: {_named_tells()}. "
    "No 'it's not just X, it's Y'. No '\U0001F680 Exciting news!'.\n"
    "- No hype and no marketing voice: explain, don't sell. Say so plainly when "
    "something is narrow, preliminary, or incremental. A real, slightly "
    "imperfect opinion beats a polished, balanced summary. Specific beats "
    "grand: let people feel the point through ONE useful, true detail (a "
    "result, a tip they can use today), not a restated vision.\n"
    f"- At most one emoji, only if it's natural. At most {POST_HASHTAG_LIMIT} "
    "hashtags, or none.\n"
    "- Reels are the one exception: a reel caption may carry up to "
    f"{REEL_HASHTAG_LIMIT} hashtags, on its last line.\n"
    "- Last check before posting: would a real person say this out loud to a "
    "friend? If not, rewrite it until they would.\n\n"
)
