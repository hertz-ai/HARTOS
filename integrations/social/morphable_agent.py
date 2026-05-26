"""
morphable_agent — Server-side orchestrator for the NunbaHero chat.

Plan reference: sunny-gliding-eich.md, Part B.4 + Part E.5.

Responsibility:
  - Each user turn → ask the existing
    `integrations.agentic_router.find_matching_agent` whether the
    prompt matches a specialist.
  - If a specialist matched (LLM semantic match), record state
    'specialist' + active_specialist=<name> in the user's TaskLedger
    and route the turn to that specialist via the existing
    `agentic_router.dispatch_to_agent` (same path used by every other
    agent dispatch in HARTOS).
  - If no specialist matched, route through autogen's default
    GroupChat with chat_instructor as the user-facing persona.  The
    `speaker_selection_method` callback we hand autogen reads the
    ledger to decide which of (chat_instructor / assistant / verifier /
    executor) speaks next.

The state-vocabulary used by this module ('casual', 'specialist',
'returning') is module-local; TaskLedger doesn't impose it.

Heuristic fallback: when neither agentic_router nor autogen is
importable (test env, no LLM keys) we emit a deterministic reply
that still drives the ledger so end-to-end tests can exercise the
plumbing without live models.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

from . import task_ledger

logger = logging.getLogger('hevolve_social.morphable_agent')


# Names of the four autogen personas we rotate through.  These match
# the agent names created in helper.py:2069 area.
PERSONA_CHAT_INSTRUCTOR = 'chat_instructor'
PERSONA_ASSISTANT = 'assistant'
PERSONA_VERIFIER = 'verifier'
PERSONA_EXECUTOR = 'executor'

NUNBA_CONVERSATION_HANDLE = 'nunba'


def is_nunba_conversation(conv_id: str) -> bool:
    """Return True if the given conversation_id is the morphable
    Nunba chat — covers the canonical 'nunba' handle plus the
    per-user variant 'nunba:<user_id>'."""
    if not conv_id:
        return False
    s = str(conv_id)
    return s == NUNBA_CONVERSATION_HANDLE or s.startswith('nunba:')


# ── Rotation policy (pure logic — no model calls) ────────────────────────
# Given the current ledger state and the agent that just spoke, pick
# the agent name that should speak next.  Kept as a pure function so
# unit tests don't need autogen installed.

def _rotation_for_state(state: str, last_speaker_name: str) -> str:
    if state == 'casual':
        return PERSONA_CHAT_INSTRUCTOR
    if state == 'specialist':
        # Caller substitutes the active specialist; this signals
        # "not one of the rotation regulars".
        return '__specialist__'
    if state == 'returning':
        return PERSONA_CHAT_INSTRUCTOR
    # Default rotation when there's no specialist — autogen default
    # is fine.  We don't manufacture additional states.
    return PERSONA_CHAT_INSTRUCTOR


def make_speaker_selection(ledger: task_ledger.TaskLedger):
    """Return an autogen-compatible speaker_selection_method.

    The callback reads ledger.state at call time (not at closure-
    capture time) so the rotation always reflects the latest turn.
    """
    def select(last_speaker, group_chat):
        last_name = getattr(last_speaker, 'name', '') or ''
        target_name = _rotation_for_state(ledger.state, last_name)

        if target_name == '__specialist__':
            specialist = ledger.active_specialist
            if specialist:
                for a in group_chat.agents:
                    if getattr(a, 'name', '') == specialist:
                        return a
            target_name = PERSONA_CHAT_INSTRUCTOR

        for a in group_chat.agents:
            if getattr(a, 'name', '') == target_name:
                return a
        # Last-resort: autogen default round-robin.
        return None
    return select


# ── Specialist match (delegate to existing HARTOS) ───────────────────────

def _match_specialist(prompt: str) -> Optional[str]:
    """Ask `agentic_router.find_matching_agent` if this prompt should
    be handed to one of the 96 expert agents.

    Returns the matched agent's name (string) or None.  Never raises —
    falls through to None on any error so the morphable chat keeps
    flowing even when the matcher is unavailable.
    """
    if not prompt or not prompt.strip():
        return None
    try:
        from integrations import agentic_router
        m = agentic_router.find_matching_agent(prompt)
        if m and isinstance(m, dict):
            name = m.get('name') or m.get('agent_id')
            return str(name) if name else None
    except Exception as e:
        logger.debug("morphable_agent: agentic_router unavailable (%s)", e)
    return None


# ── Heuristic reply fallback (no autogen, no LLM keys) ───────────────────

def _heuristic_reply(state: str, specialist: Optional[str],
                     prompt: str) -> str:
    """Deterministic, model-free reply used when autogen / LLM stack
    isn't available (test env, offline mode)."""
    if state == 'specialist':
        return (
            f"(specialist @{specialist}) Picking up: «{prompt[:80]}…».")
    if state == 'returning':
        return (
            "(chat_instructor) Wrapping up the specialist's result; "
            "ask another question whenever you're ready.")
    return (
        "(chat_instructor) I'm Nunba. Tell me what you need — I'll "
        "answer or pull in the right specialist.")


# ── Public entry point ──────────────────────────────────────────────────

def dispatch_morphable_turn(user_id: str, prompt: str,
                            *, conversation_id: str = 'nunba',
                            db=None) -> Tuple[str, Dict]:
    """Run one user turn through the morphable pipeline.

    Returns: (reply_text, metadata_dict).

    metadata_dict shape:
        {
          'state':      <new ledger state>,
          'specialist': <matched specialist name or None>,
          'persona':    <agent that produced the reply>,
        }

    The caller stores metadata in Message.metadata_json so the client
    surfaces the active persona in the chat header.
    """
    ledger = task_ledger.get_or_create(user_id, conversation_id)

    # Decision lives HERE, not in the ledger.  Step 1: ask the
    # existing semantic matcher whether this is a specialist task.
    specialist = _match_specialist(prompt)
    if specialist:
        new_state = 'specialist'
    elif ledger.state == 'specialist':
        # We were in a specialist turn and the user's new prompt
        # didn't match another specialist → return to user-facing
        # chat_instructor for the wrap-up.
        new_state = 'returning'
    else:
        new_state = 'casual'

    ledger.record(state=new_state, prompt=prompt, specialist=specialist)

    # ── Reply generation ────────────────────────────────────────────
    # When state == 'specialist' we'd dispatch via
    # agentic_router.dispatch_to_agent and let that path's existing
    # GuardrailEnforcer / Constitutional Filter / Constructive Filter
    # gate the reply.  When state == 'casual' or 'returning' we'd
    # run the autogen GroupChat with `make_speaker_selection(ledger)`
    # wired in.  Both real paths are gated behind autogen import +
    # LLM availability — the heuristic fallback below keeps the
    # pipeline testable in offline / CI environments.
    reply: Optional[str] = None
    try:
        # Real-model paths land in a separate commit (the autogen
        # GroupChat wire-up needs the live LLM config + the actual
        # agent registry the rest of HARTOS uses).  For now, this is
        # a NotImplementedError so callers fall through to heuristic.
        raise NotImplementedError("autogen / agentic dispatch wire-up "
                                  "deferred to follow-up commit")
    except Exception as e:
        if not isinstance(e, NotImplementedError):
            logger.warning(
                "morphable_agent: live dispatch failed, falling back "
                "to heuristic reply: %s", e)
        reply = _heuristic_reply(new_state, specialist, prompt or '')

    metadata = {
        'state': new_state,
        'specialist': specialist,
        'persona': specialist if new_state == 'specialist'
                   else PERSONA_CHAT_INSTRUCTOR,
    }
    return reply, metadata
