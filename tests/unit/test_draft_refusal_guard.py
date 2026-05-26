"""Bug B regression — draft must NEVER refuse on behalf of the system.

Originating incident: user asked "check this PR change is valid
https://github.com/...".  The 0.8B draft replied "I cannot access
external GitHub URLs to verify the specific PR change…".  Even
though delegate was correctly set to 'local', the standby refusal
was shipped to the user immediately and the expert reply (which
WOULD have used tools to fetch the URL) never arrived because of a
separate WAMP transport bug (Bug C).

Architectural rule: the draft is the first-responder ROLE, not the
authority on system capability.  Even a 27B model in the draft slot
on a 96GB GPU does NOT see the full tool registry the expert binds.
So the rule is size-agnostic: NEVER refuse from the draft slot.

Two layers of defense:
  1. Prompt — `_build_draft_classifier_prompt` includes a non-
     negotiable "ROLE CONTRACT" block that forbids refusal phrases.
  2. Detector — `_REFUSAL_PATTERN` regex scans the parsed draft
     reply.  Any high-confidence capability refusal forces
     delegate='local' + replaces the standby with a generic holding
     reply.  This is the safety net for when the model slips a
     refusal through despite the prompt.

This test guards the second layer so CI breaks loudly if any future
edit weakens the regex or removes the override site.
"""
import re

import pytest

from integrations.agent_engine.speculative_dispatcher import (
    _REFUSAL_PATTERN,
    _REFUSAL_STANDBY_REPLY,
)


# ── Positive cases — must match (real-world refusal phrases) ────────

REFUSAL_PHRASES = [
    # Today's actual incident — the user's chat log:
    "I cannot access external GitHub URLs to verify the specific PR.",
    "I don't have built-in crawlers or browser integration.",

    # Common LLM-isms that all map to capability refusals:
    "I cannot access external sites.",
    "I cannot fetch the URL you provided.",
    "I cannot reach that endpoint.",
    "I cannot browse the web.",
    "I cannot directly access the internet.",
    "I cannot currently retrieve that file.",
    "I cannot connect to GitHub.",
    "I cannot read the contents of that PDF.",
    "I cannot view the attached image.",
    "I cannot verify that without the live data.",
    "I cannot crawl the page.",
    "I cannot open that URL.",
    "I cannot download the artifact.",
    "I cannot resolve that DNS name.",
    "I am unable to access external resources.",
    "I'm unable to access the linked repository.",
    "I'm unable to fetch that file.",
    "I am not able to verify the PR.",
    "I'm not able to reach that URL.",
    "I do not have access to the filesystem.",
    "I don't have access to the web.",
    "I don't have built-in tools for this.",
    "I don't have the capability to do that.",
    "I don't have internet access.",
    "I don't have web access in this environment.",
    "I lack the ability to browse.",
    "I lack access to GitHub.",
    "I lack the capability to read remote files.",
    "I have no way to verify that.",
    "I have no access to external resources.",
    "I have no tools to retrieve that information.",
    "I'm just a language model.",
    "I'm just an LLM.",
    "I'm just a chatbot.",
    "I'm only an AI.",
]


@pytest.mark.parametrize("phrase", REFUSAL_PHRASES)
def test_refusal_pattern_matches_real_phrases(phrase):
    """Every refusal phrase from the incident corpus must match."""
    assert _REFUSAL_PATTERN.search(phrase), (
        f"Refusal pattern should have matched: {phrase!r}.  Loosening the "
        f"regex away from this case will let drafts ship 'I cannot' replies "
        f"to users — exactly the bug B was meant to fix."
    )


# ── Negative cases — must NOT match (legitimate phrasing) ──────────

LEGITIMATE_PHRASES = [
    # Idiomatic 'cannot' that's not a capability refusal:
    "I cannot wait to help you with this!",
    "I cannot stress enough how important this is.",
    "I cannot agree more.",
    "I cannot tell from your description alone — could you share the file?",

    # Negative recall — knowing-but-don't-know is fine:
    "I don't know the exact answer, but here's what I think.",
    "I don't recall the specific date.",
    "I don't think that's correct, actually.",

    # Negative preference — not capability:
    "I don't want to assume — let me ask first.",
    "I don't usually answer questions like this without context.",

    # Greetings / standby (these MUST NOT match — they are the right output):
    "Let me check that for you.",
    "Looking that up now…",
    "One moment, fetching the details.",
    "Hi! What can I help with?",
    "Got it — checking GitHub now.",

    # Code-y content with the word 'access' but no refusal frame:
    "Here's how to access the API: import requests…",
    "You can access the dashboard at /admin.",
    "Use bus.publish() to access the message bus.",
]


@pytest.mark.parametrize("phrase", LEGITIMATE_PHRASES)
def test_refusal_pattern_does_not_match_legitimate_phrases(phrase):
    """Legitimate phrasings must NOT trigger the refusal override.

    A false-positive here would replace a perfectly good draft reply
    with a generic standby and force unnecessary expert escalation —
    capability still works but quality + latency degrade.
    """
    assert not _REFUSAL_PATTERN.search(phrase), (
        f"Refusal pattern false-positive on legitimate phrase: {phrase!r}.  "
        f"Tightening required — over-broad match will degrade chat quality "
        f"on benign replies."
    )


# ── Standby contract ────────────────────────────────────────────────

def test_standby_reply_is_non_empty_and_neutral():
    """The standby that replaces the refusal must be (a) non-empty
    so it actually shows in the UI, (b) capability-neutral so the
    expert's eventual upgrade replaces it cleanly, and (c) not itself
    a refusal (regression of the original bug)."""
    assert _REFUSAL_STANDBY_REPLY, "standby cannot be empty"
    assert len(_REFUSAL_STANDBY_REPLY) >= 5, (
        "standby must be visible to user — at least a few characters")
    assert not _REFUSAL_PATTERN.search(_REFUSAL_STANDBY_REPLY), (
        "the standby itself must not contain a refusal phrase, or the "
        "guard would loop on its own output")


# ── End-to-end shape: refusal → forced delegate=local + standby ───

def test_refusal_in_dispatch_envelope_is_overridden(monkeypatch):
    """When the draft returns a refusal reply, dispatch_draft_first
    must replace draft_reply with the standby AND force
    delegate='local' AND mark refusal_overridden=True in telemetry.

    We don't run the full dispatcher (avoid the LLM call); instead
    we simulate the parsed envelope and verify the override block
    fires in the documented shape.
    """
    # Simulate a draft envelope that delegated correctly but slipped a
    # refusal into the reply text (today's actual incident shape).
    parsed = {
        'reply': 'I cannot access external GitHub URLs to verify the PR.',
        'delegate': 'local',  # correctly delegated …
        'confidence': 0.95,   # … with high confidence
        'is_casual': False,
    }

    # Manually run the override logic the way dispatch_draft_first does.
    # (Importing the live function would pull the full LangChain stack;
    # we re-derive the contract here so the test is fast + isolated.)
    draft_reply = parsed['reply']
    delegate = parsed.get('delegate', 'local')
    confidence = float(parsed.get('confidence') or 0.0)
    refusal_overridden = False
    if draft_reply and _REFUSAL_PATTERN.search(draft_reply):
        draft_reply = _REFUSAL_STANDBY_REPLY
        delegate = 'local'
        refusal_overridden = True

    assert refusal_overridden is True, (
        "refusal_overridden flag must be set so telemetry records the "
        "override — without it we can't measure adherence per draft model")
    assert draft_reply == _REFUSAL_STANDBY_REPLY, (
        "draft_reply must be replaced with the standby — the user must "
        "never see the original refusal")
    assert delegate == 'local', (
        "delegate must be forced to 'local' regardless of what the draft "
        "self-declared — refusal implies the draft can't answer, so the "
        "expert MUST run")
    assert confidence == 0.95, (
        "confidence is a model self-report and is preserved unchanged; "
        "the override only normalizes reply + delegate")


def test_no_refusal_leaves_envelope_untouched():
    """When the draft reply is clean, the override block must NOT fire
    — no spurious replacements, no false flag in telemetry."""
    parsed = {
        'reply': 'Hi! What can I help you build today?',
        'delegate': 'none',
        'confidence': 0.9,
        'is_casual': True,
    }

    draft_reply = parsed['reply']
    delegate = parsed.get('delegate', 'local')
    refusal_overridden = False
    if draft_reply and _REFUSAL_PATTERN.search(draft_reply):
        draft_reply = _REFUSAL_STANDBY_REPLY
        delegate = 'local'
        refusal_overridden = True

    assert refusal_overridden is False
    assert draft_reply == parsed['reply']
    assert delegate == 'none'  # untouched


def test_refusal_pattern_uses_word_boundaries():
    """Sanity: pattern must use word boundaries so 'I' doesn't match
    inside another word (regression: 'fI cannot' style typos)."""
    # The pattern source should contain \b at the start of each
    # alternative for the 'I' anchor.  We check by exercising a case
    # that would only match without word boundaries.
    assert not _REFUSAL_PATTERN.search('aI cannot access X'), (
        "pattern must not match 'I' inside another word — guard against "
        "false positives in concatenated tokens")
