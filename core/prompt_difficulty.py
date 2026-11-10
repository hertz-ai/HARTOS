"""How hard is this prompt? A cheap, deterministic answer.

Why this exists
===============

Routing between the fast path and the full path was decided by the caller.
``/chat`` read ``casual_conv`` straight from the request body and believed it,
and the web client set that flag from whether an agent prompt happened to be
configured -- which has nothing to do with how hard the question is. So a
one-word greeting could take the full agentic path, and a question needing
real reasoning could be answered by the smallest model in the ladder. Both
directions are failures: one wastes a machine's memory and the user's time,
the other hands back a worse answer than the hardware could have produced.

The system should decide this, not the caller. A caller may hint, but a hint
that a genuinely hard prompt is "casual" is not honoured -- being wrong in
that direction is the expensive one.

Relationship to the two assessors that already exist
----------------------------------------------------

This is deliberately not a third opinion on the same question:

* ``speculative_dispatcher.dispatch_draft_first`` is the *model-based*
  assessor and remains the primary one. The draft model answers and emits a
  ``delegate`` signal, and its escalation guards (refusal patterns, low
  confidence, agent-bound prompts) are richer than anything a regex can do.
  When a draft model is loaded, that path decides what the ANSWER is.
  CORRECTED 2026-11-10: this module is no longer unconsulted -- /chat
  resolves ``casual_conv`` through ``resolve_casual_conv`` before the
  draft runs. Precisely: the resolved flag selects the 0.8B draft with
  the casual tool set over the 4B with the full set, and on the Nunba
  path it changes that selection ONLY for a prompt that assesses HARD
  -- five of the reviewer's 84 probes. The two no longer sit on the
  same question, and that is the point: the draft cannot escalate
  itself out of a tier it was not given.
* ``hive_task_protocol.estimate_complexity`` scores *coding tasks* in Spark
  units from file references and refactor keywords. Different domain,
  different output, not applicable to a sentence someone typed.

What this covers is the case neither handles: no draft model available. That
is not an edge case -- the central cloud node runs an external model with no
local draft in the ladder, so ``dispatch_draft_first`` returns
``no_draft_model`` and falls through with nothing assessing difficulty at all.
Every hosted user is on that path.

Design
------

No LLM call, no network, no model load. Deciding whether to spend a model call
must not itself cost a model call, and this runs on every turn. Pure string
inspection, a few microseconds, deterministic and testable.

The signals are intentionally conservative. It is far better to send an easy
prompt down the full path than to send a hard one to the small model: the
first costs some compute, the second costs the user a bad answer. So
``HARD`` wins ties, and ``EASY`` is only returned when the prompt is short and
carries no marker of reasoning at all.
"""
from __future__ import annotations

import re
from typing import Tuple

EASY = 'easy'
HARD = 'hard'
ORDINARY = 'ordinary'

# Social openers and closers. Matched whole-string (after stripping) rather
# than by substring: "hi" is a greeting, but "which hint helps here" is not,
# and a substring test cannot tell them apart.
_PLEASANTRIES = frozenset({
    'hi', 'hii', 'hey', 'hello', 'yo', 'sup', 'hiya',
    'good morning', 'good afternoon', 'good evening', 'good night',
    'thanks', 'thank you', 'thanks!', 'ty', 'cheers', 'nice', 'cool', 'ok',
    'okay', 'k', 'got it', 'sure', 'yes', 'no', 'yep', 'nope', 'bye',
    'goodbye', 'see you', 'later', 'lol', 'haha', 'great', 'awesome',
    'perfect', 'sorry', 'oops', 'test', 'ping', 'hello?', 'you there',
    'how are you', "how's it going", 'whats up', "what's up",
})

# Asking for reasoning, not recall. "why" and "how does" want an explanation;
# "compare"/"trade-off" want judgement; "prove"/"derive" want a chain.
_REASONING = re.compile(
    r'\b(why|how (?:do|does|would|can|should|could)|explain|reason(?:ing)? '
    r'about|compare|contrast|trade[- ]?offs?|pros and cons|evaluate|assess|'
    r'critique|prove|derive|deduce|infer|justify|analy[sz]e|design|architect|'
    r'implement|optimi[sz]e|refactor|debug|troubleshoot|diagnose|root cause|'
    r'implication|consequence|strategy|approach|best way|should i|which is '
    r'better|what if|suppose|given that)\b', re.I)

# Work that unfolds in steps is work a small model loses track of.
_MULTI_STEP = re.compile(
    r'\b(step[- ]by[- ]step|first.{0,40}\bthen\b|then.{0,40}\bfinally\b|'
    r'walk me through|in detail|thoroughly|comprehensive|end[- ]to[- ]end|'
    r'plan for|roadmap)\b', re.I)

# Technical density. Not proof of difficulty on its own, but a strong hint
# when it shows up with anything else.
_TECHNICAL = re.compile(
    r'\b(algorithm|complexity|concurren\w+|distributed|latency|throughput|'
    r'race condition|deadlock|memory leak|stack trace|traceback|exception|'
    r'segfault|kernel|compiler|gradient|tensor|embedding|quantiz\w+|'
    r'inference|schema|migration|encryption|authentication|protocol)\b', re.I)

_CODE = re.compile(r'```|\bdef \w+\(|\bclass \w+|;\s*$|=>|\bimport \b|'
                   r'\bSELECT\b.+\bFROM\b|\{\s*"\w+"\s*:', re.I | re.M)

_MATH = re.compile(r'[∑∫√≈≤≥≠∂π]|\b\d+\s*[\^*/+-]\s*\d+|\bO\(n')

# A short prompt is not automatically an easy one. "What is the capital of
# France" is six words and still wants a correct answer rather than the
# smallest model's guess; "summarise this" is three words of real work. An
# early version returned EASY for both on length alone, which violated this
# module's own rule that being wrong toward the small model is the expensive
# direction. So a question opener or an imperative disqualifies a prompt from
# EASY no matter how short it is, and brevity alone never qualifies it.
_INTERROGATIVE = re.compile(
    r'^\s*(what|when|where|who|which|whose|whom|whats|what\'s|hows|how|is|are|'
    r'was|were|can|could|do|does|did|will|would|should|has|have|had|may|might|'
    r'am)\b', re.I)

# Imperatives that act on the person's OWN data -- her memory, her files,
# her library -- were missing from this list, and they are the ones whose
# EASY verdict costs the most.  MEASURED 2026-11-07 by calling
# resolve_casual_conv(prompt, client_value=None) directly: "remember that
# my exam is on friday" and "open my downloads folder" both came back
# casual=True / 'short-and-plain' (score 0, <=8 words, no '?', and no
# opener in this regex), i.e. the casual tier.  WITHDRAWN 2026-11-10, same
# unmeasured claim 37e0781 pulled out of Nunba: this used to add "the fast
# path with NO tools -- so remember() and open_file_gui() can never run,
# and a tool-less model then answers as though they had".  Nobody measured
# that.  The casual set carries the memory tools and Shell_Command, so
# whether those calls actually happen is UNCHECKED and needs one live
# turn.  What IS established is the flag.  Those are the exact two cases
# the route-level keyword
# stopgap at Nunba chatbot_routes.py:3478 was added for on 2026-09-25, and
# the reason that shadow table could not just be deleted in favour of this
# module.  CORRECTED 2026-11-10 -- this said "adding them here is what
# lets it be deleted", and that was WRONG.  Verbs in this list only
# matter when the caller has NO opinion; the override fires at HARD
# only, these score ORDINARY, and the live caller always sends a bool,
# so on the Nunba path these verbs do NOTHING:
#     resolve_casual_conv('remember that my exam is on friday', True)
#         -> (True, 'client:True')
# The table was deleted anyway (the owner's order) and that capability
# is currently LOST.  Closing it means overriding on TOOL-NEED as well
# as on difficulty -- difficulty is not tool-need -- which changes the
# owner's 2026-08-24 session-shape design, so it is with the owner.
# What IS true here: a question about
# the person's book ("how tall is the ridge in chapter 3", "what does my
# textbook say about nesting") already resolves to the full path via
# _INTERROGATIVE, with no phrase table anywhere.  "don'?t forget" is
# spelled out because the anchor is ^ and the negation comes first.
_TASK_VERB = re.compile(
    r'^\s*(summari[sz]e|write|draft|translate|explain|list|find|search|'
    r'calculate|compute|convert|fix|debug|generate|create|make|build|rewrite|'
    r'review|check|compare|tell|show|give|help|suggest|recommend|plan|'
    r"describe|define|outline|remember|don'?t forget|forget|open)\b", re.I)


def assess(prompt: str) -> Tuple[str, int, str]:
    """Return ``(label, score, why)``.

    ``label`` is EASY, ORDINARY or HARD. ``score`` is 0-100 and is exposed so
    a caller can apply its own threshold. ``why`` names the signals that
    fired, so a routing decision can be explained after the fact rather than
    being an unexplained verdict -- the same reason the attribution log
    records why it classified a client as automated.
    """
    text = (prompt or '').strip()
    if not text:
        return EASY, 0, 'empty'

    low = text.lower().rstrip('.!')
    words = text.split()

    # A bare pleasantry is the one case that is unambiguously easy.
    if low in _PLEASANTRIES:
        return EASY, 0, 'pleasantry'

    score = 0
    why = []

    if _CODE.search(text):
        score += 35
        why.append('code')
    if _MATH.search(text):
        score += 25
        why.append('math')
    if _REASONING.search(text):
        score += 30
        why.append('reasoning')
    if _MULTI_STEP.search(text):
        score += 25
        why.append('multi-step')
    if _TECHNICAL.search(text):
        score += 20
        why.append('technical')

    # Length, in words rather than characters: a long prompt carries more
    # constraints to satisfy, and characters would just measure verbosity.
    if len(words) > 120:
        score += 25
        why.append('very-long')
    elif len(words) > 45:
        score += 15
        why.append('long')

    # Several questions in one turn is several answers to keep consistent.
    if text.count('?') > 1:
        score += 10
        why.append('multi-question')

    # Newlines usually mean structure: a list, a snippet, a spec.
    if text.count('\n') >= 2:
        score += 10
        why.append('structured')

    score = max(0, min(100, score))

    if score >= 30:
        return HARD, score, '+'.join(why)
    # Short, no signal, and not actually asking for anything: a stray remark
    # like "nice one" or "that worked". Anything shaped like a question or an
    # instruction falls through to ORDINARY, however brief.
    if (score == 0 and len(words) <= 8 and '?' not in text
            and not _INTERROGATIVE.match(text)
            and not _TASK_VERB.match(text)):
        return EASY, score, 'short-and-plain'
    return ORDINARY, score, '+'.join(why) or 'no-signal'


def is_casual(prompt: str) -> bool:
    """True when the fast path is enough.

    Only EASY qualifies. ORDINARY deliberately does not: an unremarkable
    question with no reasoning markers is still a question someone wants a
    correct answer to, and the cost of being wrong here is asymmetric.
    """
    return assess(prompt)[0] == EASY


def resolve_casual_conv(prompt: str, client_value=None) -> Tuple[bool, str]:
    """Decide the fast/full routing for one turn. Returns ``(casual, why)``.

    The caller's value is a hint, never the decision:

    * client says casual, prompt is hard  -> full path. Overriding here is the
      whole point; the caller cannot see how hard the question is, and the
      web client historically derived this flag from whether an agent prompt
      was configured, which is unrelated.
    * client says full, prompt is easy    -> honoured. Asking for more care
      than needed is a caller's prerogative and costs only compute.
    * client says nothing                 -> assessed.
    """
    label, score, why = assess(prompt)
    if client_value is None:
        return label == EASY, 'assessed:%s(%d):%s' % (label, score, why)
    if bool(client_value) and label == HARD:
        return False, 'override:client-said-casual-but-%s(%d):%s' % (
            label, score, why)
    return bool(client_value), 'client:%s' % bool(client_value)
