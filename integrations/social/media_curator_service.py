"""
HevolveSocial - Conversational social-media curator service (closes #401).

Parses natural-language user feedback into structured curator intents
so the seeded social_media_curator_agent can act on them deterministi-
cally (or via richer LLM parsing when the model is available).

Design contract (project_encounter_icebreaker.md §11 + the seeded
goal at goal_seeding.SEED_BOOTSTRAP_GOALS slug='social_media_
curator_agent'):

  * INPUT: a user utterance (voice → STT'd to text, or typed).
  * OUTPUT: a CuratorIntent — what the user is asking for, in
    structured form, plus confidence + the raw text for audit.
  * NEVER auto-publishes — caller stages the action behind a
    user-approval tap, same flow as the icebreaker (no_autosend +
    consent_required gates from the seeded goal).
  * Topology gate: drafting / scheduling work that READS user memory
    or runs an LLM is consent-gated on central topology, same rule
    as icebreaker_service.draft_icebreaker.  This module's parser
    itself is pure (no memory access, no LLM by default), so it
    runs unconditionally on any topology — the consent gate fires
    one layer up when the parser's intent triggers a downstream
    memory read or LLM call.

The deterministic keyword classifier covers the bulk of the
"this one's cool / skip / caption it / post Friday" vocabulary the
seeded goal lists.  Callers that have an LLM available can pass
``llm_callback`` to upgrade the classifier — same fall-through
contract as draft_icebreaker (LLM result wins; on raise/empty/
non-string, falls back to the deterministic path).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger('hevolve_social')


# Recognised intent kinds.  Stable strings — clients pattern-match.
INTENT_APPROVE = 'approve'        # "post this", "share it", "looks great"
INTENT_REJECT = 'reject'          # "skip", "not this", "pass"
INTENT_CAPTION_STYLE = 'caption_style'  # "caption with hiking vibe"
INTENT_SCHEDULE = 'schedule'      # "post Friday morning"
INTENT_CHANNELS = 'channels'      # "on twitter and linkedin"
INTENT_UNKNOWN = 'unknown'

VALID_INTENTS = frozenset({
    INTENT_APPROVE, INTENT_REJECT, INTENT_CAPTION_STYLE,
    INTENT_SCHEDULE, INTENT_CHANNELS, INTENT_UNKNOWN,
})


# Recognised platform names.  Lowercased substring match against the
# user utterance.  Add aliases here, single source — never inline a
# platform list elsewhere in the curator pipeline.
_PLATFORM_TOKENS: tuple[str, ...] = (
    'twitter', 'x.com', 'linkedin', 'mastodon', 'bluesky',
    'instagram', 'facebook', 'reddit', 'threads',
)

_PLATFORM_ALIASES: dict[str, str] = {
    'x.com': 'twitter',
    'x ': 'twitter',
}


@dataclass(frozen=True)
class CuratorIntent:
    """One parsed user utterance."""
    kind: str                             # one of VALID_INTENTS
    payload: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    raw_text: str = ''


# ──────────────────────────────────────────────────────────────────────
# Deterministic keyword/regex classifiers — one per intent.  Each
# returns (payload_dict_or_None, confidence) where None means "no match".
# Pure functions; tested in isolation.
# ──────────────────────────────────────────────────────────────────────


_APPROVE_RX = re.compile(
    r'\b(post(?:\s+(?:this|that|it))?|share(?:\s+(?:this|that|it))?|'
    r'looks\s+(?:great|cool|good|nice)|approve|keep(?:\s+(?:this|that|it))?'
    r')\b',
    re.IGNORECASE,
)

_REJECT_RX = re.compile(
    r'\b(skip(?:\s+(?:this|that|it))?|not\s+(?:this|that|it)|pass'
    r'|reject|drop(?:\s+(?:this|that|it))?|nope?'
    r')\b',
    re.IGNORECASE,
)

_CAPTION_RX = re.compile(
    r'caption(?:\s+(?:it|this|that))?\s+'
    r'(?:with|in|using)?\s*(?:a\s+)?'
    r'([a-z][\w\-]{1,30})\s*(?:vibe|tone|style|mood)?',
    re.IGNORECASE,
)

# Time hints — naive but covers the seeded vocabulary.
_DAY_TOKENS = (
    'monday', 'tuesday', 'wednesday', 'thursday',
    'friday', 'saturday', 'sunday',
)
_TIME_OF_DAY = ('morning', 'noon', 'afternoon', 'evening', 'night')


def _classify_approve(text: str) -> Optional[tuple[dict, float]]:
    if _APPROVE_RX.search(text):
        return {}, 0.85
    return None


def _classify_reject(text: str) -> Optional[tuple[dict, float]]:
    if _REJECT_RX.search(text):
        return {}, 0.85
    return None


def _classify_caption_style(text: str) -> Optional[tuple[dict, float]]:
    m = _CAPTION_RX.search(text)
    if not m:
        return None
    style = m.group(1).lower().strip()
    # Reject caption-style hits where the captured token is itself a
    # filler word — guards against "caption it nicely" matching the
    # adverb instead of a real style.
    if style in {'it', 'this', 'that', 'a', 'with', 'in'}:
        return None
    return {'style': style}, 0.75


def _classify_schedule(text: str) -> Optional[tuple[dict, float]]:
    lc = text.lower()
    day = next((d for d in _DAY_TOKENS if d in lc), None)
    tod = next((t for t in _TIME_OF_DAY if t in lc), None)
    # "tomorrow" / "today" hints also count as a schedule signal.
    relative = next(
        (r for r in ('tomorrow', 'today', 'tonight') if r in lc),
        None,
    )
    if not (day or tod or relative):
        return None
    payload: dict[str, Any] = {}
    if day:
        payload['day'] = day
    if tod:
        payload['time_of_day'] = tod
    if relative:
        payload['relative'] = relative
    # Confidence scales with how specific the schedule hint is.
    confidence = 0.55 + 0.15 * len(payload)
    return payload, min(confidence, 0.95)


def _classify_channels(text: str) -> Optional[tuple[dict, float]]:
    lc = text.lower()
    found: list[str] = []
    for token in _PLATFORM_TOKENS:
        if token in lc:
            canonical = _PLATFORM_ALIASES.get(token, token)
            if canonical not in found:
                found.append(canonical)
    if not found:
        return None
    return {'channels': found}, 0.7


# Order matters: more specific classifiers first.  approve / reject
# beat schedule / channels because "post Friday morning" should resolve
# as schedule, not approve, even though "post" is in the approve regex.
_CLASSIFIERS: tuple[tuple[str, Callable[[str], Optional[tuple[dict, float]]]], ...] = (
    (INTENT_CAPTION_STYLE, _classify_caption_style),
    (INTENT_SCHEDULE, _classify_schedule),
    (INTENT_CHANNELS, _classify_channels),
    (INTENT_APPROVE, _classify_approve),
    (INTENT_REJECT, _classify_reject),
)


def parse_curator_command(
    text: str,
    *,
    llm_callback: Optional[Callable[[str], dict]] = None,
) -> CuratorIntent:
    """Parse a user utterance into a structured CuratorIntent.

    Args:
        text: user utterance (voice → STT'd, or typed).
        llm_callback: optional richer parser; takes the raw text and
                      returns a dict {'kind', 'payload', 'confidence'}.
                      When supplied AND non-raising AND returns a kind
                      in VALID_INTENTS, its result is used.  Otherwise
                      falls back to the deterministic classifier.

    Returns:
        CuratorIntent.  Always returns SOME intent — `kind=INTENT_UNKNOWN`
        when nothing matches (so callers can branch on .kind without
        None-checking the return).
    """
    raw = (text or '').strip()
    if not raw:
        return CuratorIntent(kind=INTENT_UNKNOWN, raw_text='', confidence=0.0)

    if llm_callback is not None:
        try:
            cand = llm_callback(raw)
            if isinstance(cand, dict):
                k = str(cand.get('kind', '')).lower()
                if k in VALID_INTENTS:
                    return CuratorIntent(
                        kind=k,
                        payload=dict(cand.get('payload') or {}),
                        confidence=float(cand.get('confidence', 0.5)),
                        raw_text=raw,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                'media_curator: llm_callback raised, falling back: %s',
                exc,
            )

    for kind, classify in _CLASSIFIERS:
        result = classify(raw)
        if result is not None:
            payload, confidence = result
            return CuratorIntent(
                kind=kind,
                payload=payload,
                confidence=confidence,
                raw_text=raw,
            )
    return CuratorIntent(kind=INTENT_UNKNOWN, raw_text=raw, confidence=0.0)
