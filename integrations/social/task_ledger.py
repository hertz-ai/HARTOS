"""
TaskLedger — pure-data record of the morphable Nunba conversation.

This module's ONLY job is to remember:
  - the current state of the conversation,
  - which agent persona is currently driving it,
  - the recent history of turns.

It does NOT classify user intent.  It does NOT pick rotation patterns.
Those decisions live in:
  - `integrations.agentic_router.find_matching_agent` — LLM-based
    semantic match against the 96 expert agent catalogue, already
    used by every other dispatch path in HARTOS.
  - The autogen `GroupChat.speaker_selection_method` — autogen's own
    rotation primitive, which we hand a callback that *reads* the
    ledger state to pick the next speaker.

Plan reference: sunny-gliding-eich.md, Part B.4 + Part E.5 +
docs/R2-HARTOS-MORPHABLE-AGENT-PLAN.md.

States are free-form strings — the ledger imposes no schema.  The
morphable_agent module is the one that establishes the convention
('casual', 'specialist', etc.); the ledger just stores whatever it's
told to store, so a future caller can use a different vocabulary
without touching the ledger.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve_social.task_ledger')


@dataclass
class TaskLedger:
    """Per-user, per-conversation state record.  Pure data — no
    classification, no routing, no decisions."""
    user_id: str
    conversation_id: str = 'nunba'
    state: str = 'casual'
    active_specialist: Optional[str] = None
    history: List[Dict] = field(default_factory=list)
    max_history: int = 50

    def record(self, *, state: str, prompt: str = '',
               specialist: Optional[str] = None) -> None:
        """Append a new turn to the ledger.  The caller has already
        decided `state` + `specialist`; we just write them down.
        """
        prev = self.state
        self.state = state
        self.active_specialist = specialist
        self.history.append({
            'state': state,
            'prompt': (prompt or '')[:200],
            'specialist': specialist,
        })
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]
        logger.debug(
            "TaskLedger[%s/%s] %s → %s (specialist=%s)",
            self.user_id, self.conversation_id, prev, state, specialist)

    def to_metadata(self) -> Dict:
        """Serialisable summary for embedding in Message.metadata_json."""
        return {
            'state': self.state,
            'specialist': self.active_specialist,
        }


# ── Per-user singleton store (in-memory) ─────────────────────────────────

_LEDGERS: Dict[str, TaskLedger] = {}


def get_or_create(user_id: str,
                  conversation_id: str = 'nunba') -> TaskLedger:
    """Return the user's ledger for the given conversation, creating
    one on first call.  Keyed by (user_id, conversation_id)."""
    key = f"{user_id}:{conversation_id}"
    if key not in _LEDGERS:
        _LEDGERS[key] = TaskLedger(
            user_id=str(user_id), conversation_id=conversation_id)
    return _LEDGERS[key]


def reset(user_id: str, conversation_id: str = 'nunba') -> None:
    """Drop the user's ledger.  Used by tests + the
    /api/social/conversations/<id>/reset endpoint."""
    _LEDGERS.pop(f"{user_id}:{conversation_id}", None)


def _all() -> Dict[str, TaskLedger]:
    """Test-only accessor."""
    return _LEDGERS
