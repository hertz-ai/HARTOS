"""
HevolveSocial - Icebreaker drafting service (closes #399).

Pure-function service that produces a short, personalized opener for
a mutual-like BLE encounter match.  Used by:

  * The seeded `encounter_icebreaker_agent` goal — when the match
    WAMP topic fires, the goal's recipe calls draft_icebreaker(...)
    to generate the candidate text, then publishes to the
    com.hevolve.encounter.icebreaker WAMP topic for user approval.
  * The /api/social/encounter/icebreaker/draft REST endpoint — when
    the SPA wants to inspect / edit a draft before calling the
    existing /icebreaker/approve endpoint.

Design contract (project_encounter_icebreaker.md §9):

  1. Input: a match_id (server-side rows already validated as
     mutual-like) plus a SQLAlchemy session.  Output: a dict with
     `draft`, `rationale`, `alt_drafts`, `length`.
  2. NEVER auto-sends — the caller is responsible for routing the
     draft to the user-approval surface.
  3. Length is capped at ENCOUNTER_DRAFT_MAX_CHARS at the service
     boundary; longer outputs are truncated with a sentence-aware
     trim, never rejected (a returned draft must always be usable).
  4. Deterministic fallback: when no LLM callback is supplied OR the
     LLM raises, falls back to a neutral template populated from
     the peer's opt-in vibe_tags.  This ensures the encounter
     feature works on offline / cold-boot machines.
  5. No PII surface: the draft text only mentions the peer's
     OPT-IN public-facing fields (vibe_tags, avatar_style).  Never
     references the peer's user_id, real name, location, or any
     stored memory_graph entry that wasn't tagged shared.

Constitutional / cultural-wisdom gating happens in the caller (the
seeded goal's `constitutional_gates: [no_autosend, consent_required,
trust_quarantine_check, cultural_wisdom_filter]`), not here.  This
module is a pure draft generator — the gate stack wraps it.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from core.constants import ENCOUNTER_DRAFT_MAX_CHARS

from .models import DiscoverablePref, Encounter

logger = logging.getLogger('hevolve_social')


# ──────────────────────────────────────────────────────────────────────
# Edge / cloud topology gate.
#
# Drafting reads the user's memory_graph + runs LLM inference with
# their personal context.  Allowed unconditionally on user-trusted
# edge zones (flat = single machine; regional = LAN cluster).  On
# central topology (cloud), drafting is consent-gated rather than
# prohibited — if the user explicitly opted into cloud-capability
# for this feature (UserConsent row, consent_type='cloud_capability'
# with scope='*' or 'encounter_icebreaker', granted=True, revoked_at
# IS NULL), drafting is allowed; otherwise PermissionError.
#
# Callers inject the consent check as a callable so the service
# stays pure (no DB-shape coupling).  encounter_api.icebreaker_draft
# wires the real UserConsent query; tests pass a deterministic
# lambda.
# ──────────────────────────────────────────────────────────────────────


def _topology() -> str:
    """Return current node tier, defaulting to 'flat' on any error.

    Wraps security.key_delegation.get_node_tier so a missing security
    package (HARTOS minimal-install) doesn't break drafting on the
    one topology it's most likely deployed in (flat).
    """
    try:
        from security.key_delegation import get_node_tier
        return get_node_tier()
    except Exception:  # noqa: BLE001
        return 'flat'


# ──────────────────────────────────────────────────────────────────────
# Templates — neutral fallbacks used when no LLM is available.
#
# Stored as a tuple so iteration order is stable for the alt_drafts
# slot, and so dirty mutation by callers doesn't drift the list.
# ──────────────────────────────────────────────────────────────────────

_NEUTRAL_TEMPLATES: tuple[str, ...] = (
    "Hey — nice to actually be across the room from you.",
    "Hi! Funny how the room got smaller for a second there.",
    "Hello — I noticed you noticed.  Wanted to say hi properly.",
)

_VIBE_TEMPLATES: tuple[str, ...] = (
    "Hey — saw the {tag} thing.  Same.  Nice to meet you properly.",
    "Hi!  I think we share the {tag} corner of the universe.",
    "Hello.  {tag}, huh?  Curious how you got into it.",
)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _trim_to_cap(text: str, cap: int = ENCOUNTER_DRAFT_MAX_CHARS) -> str:
    """Sentence-aware trim — never returns >cap chars.  Prefers ending
    on a sentence boundary; falls back to a hard cut with an ellipsis
    if no boundary exists in range."""
    if not text:
        return text
    text = text.strip()
    if len(text) <= cap:
        return text
    # Look for the last sentence boundary inside cap.
    boundary = -1
    for sep in ('. ', '? ', '! ', '\n'):
        idx = text.rfind(sep, 0, cap)
        if idx > boundary:
            boundary = idx + len(sep) - 1  # keep the separator char
    if boundary > 0:
        return text[: boundary + 1].rstrip()
    # Hard cut with ellipsis (never exceed cap).
    return text[: max(0, cap - 1)].rstrip() + '…'


def _pick_shared_tag(
    a_tags: list[str], b_tags: list[str],
) -> Optional[str]:
    """Pick the single shared vibe tag to anchor the draft.  If no
    overlap, fall back to the peer's first tag (so the draft can
    still be vibe-flavored).  Returns None when neither side has
    any tags (→ neutral template)."""
    a_set = {str(t).lower() for t in (a_tags or [])}
    for t in b_tags or []:
        if str(t).lower() in a_set:
            return str(t)
    if b_tags:
        return str(b_tags[0])
    if a_tags:
        return str(a_tags[0])
    return None


def _peer_id_for(match: Encounter, viewer_uid: str) -> Optional[str]:
    if match.user_a_id == viewer_uid:
        return match.user_b_id
    if match.user_b_id == viewer_uid:
        return match.user_a_id
    return None


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────


def draft_icebreaker(
    match_id: str,
    viewer_user_id: str,
    db_session,
    llm_callback: Optional[Callable[[dict], str]] = None,
    cloud_consent_check: Optional[Callable[[str], bool]] = None,
    topology: Optional[str] = None,
) -> dict:
    """Produce a draft icebreaker for the given match, viewed from the
    side of `viewer_user_id`.

    Args:
        match_id: Encounter.id of a row with context_type='ble'.
        viewer_user_id: the user requesting the draft (must be one of
                        match.user_a_id / user_b_id).
        db_session: SQLAlchemy session.
        llm_callback: optional callable taking a context dict and
                      returning a draft string.  When supplied + non-
                      raising, its output is used as the primary draft;
                      its failure is logged and the deterministic
                      template is used instead (NEVER the bare error).
        cloud_consent_check: optional callable taking a user_id and
                      returning True iff that user has explicitly
                      consented to cloud-capability for this feature
                      (UserConsent row, consent_type='cloud_capability'
                      with scope='*' or 'encounter_icebreaker',
                      granted=True, revoked_at IS NULL).
                      Required when this process is running in
                      central topology — drafting reads memory_graph
                      + runs LLM with personal context, so cloud
                      execution requires explicit per-user opt-in.
                      Ignored on flat / regional (user-trusted edge).

    Returns:
        {
          'draft':       str,      # primary draft, ≤ ENCOUNTER_DRAFT_MAX_CHARS
          'rationale':   str,      # one-line why-this-tag explanation
          'alt_drafts':  list[str],# 2 alternates, also length-capped
          'length':      int,      # len(draft)
          'shared_tag':  str|None, # tag the draft was anchored on
          'source':      'llm'|'template',
        }

    Raises:
        ValueError: when match_id doesn't exist, isn't a BLE match, or
                    viewer_user_id isn't one of the match parties.
        PermissionError: when running in central topology and the
                    viewer hasn't opted into cloud-capability for
                    this feature (consent-gated, not prohibited).
    """
    tier = topology if topology is not None else _topology()
    if tier == 'central':
        ok = bool(
            cloud_consent_check
            and cloud_consent_check(viewer_user_id)
        )
        if not ok:
            raise PermissionError(
                "central-topology drafting requires user "
                "cloud_capability consent for encounter_icebreaker",
            )

    match = db_session.query(Encounter).filter_by(
        id=match_id, context_type='ble',
    ).first()
    if match is None:
        raise ValueError(f"BLE match {match_id} not found")
    peer_uid = _peer_id_for(match, viewer_user_id)
    if peer_uid is None:
        raise ValueError(
            f"viewer {viewer_user_id} is not a party in match {match_id}",
        )

    peer_pref = db_session.query(DiscoverablePref).filter_by(
        user_id=peer_uid,
    ).first()
    viewer_pref = db_session.query(DiscoverablePref).filter_by(
        user_id=viewer_user_id,
    ).first()
    peer_tags = list(peer_pref.vibe_tags or []) if peer_pref else []
    viewer_tags = list(viewer_pref.vibe_tags or []) if viewer_pref else []
    shared = _pick_shared_tag(viewer_tags, peer_tags)

    context = {
        'match_id': match_id,
        'peer_user_id': peer_uid,
        'peer_vibe_tags': peer_tags,
        'viewer_vibe_tags': viewer_tags,
        'shared_tag': shared,
        'avatar_style': peer_pref.avatar_style if peer_pref else None,
    }

    primary = None
    source = 'template'
    if llm_callback is not None:
        try:
            cand = llm_callback(context)
            if isinstance(cand, str) and cand.strip():
                primary = _trim_to_cap(cand)
                source = 'llm'
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                'icebreaker llm_callback failed for match=%s: %s',
                match_id, exc,
            )

    if primary is None:
        primary = _trim_to_cap(_render_template(shared, 0))

    alt_drafts = [
        _trim_to_cap(_render_template(shared, i))
        for i in (1, 2)
    ]
    rationale = (
        f"anchored on shared interest '{shared}'"
        if shared
        else 'neutral opener — no shared vibe tags to anchor on'
    )

    return {
        'draft': primary,
        'rationale': rationale,
        'alt_drafts': alt_drafts,
        'length': len(primary),
        'shared_tag': shared,
        'source': source,
    }


def _render_template(shared_tag: Optional[str], index: int) -> str:
    """Pick template[index] and substitute the shared tag if present."""
    if shared_tag:
        templates = _VIBE_TEMPLATES
        return templates[index % len(templates)].format(tag=shared_tag)
    return _NEUTRAL_TEMPLATES[index % len(_NEUTRAL_TEMPLATES)]
