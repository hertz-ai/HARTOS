"""Canonical taxonomy of *why* the draft-first dispatcher escalated a turn
to the expert background path.

Design intent
=============

`dispatch_draft_first` runs several short-circuit guards that promote a
turn from "draft answered → done" to "draft was a standby, expert will
take over": refusal pattern in the reply, low-confidence ``delegate=none``,
agent-bound prompt, classifier-surfaced actionable intent, or the draft's
own explicit ``delegate=local/hive``.

Today each guard logs its decision and that's it — once we leave the
function, callers / observers / telemetry / WorldModelBridge can't tell
*why* a given speculation_id ended up in expert-pending state.  That
matters for three downstream consumers:

  1. **WorldModelBridge / continual learning** — distillation should weight
     refusal-overridden draft replies very differently from
     parse-failure-defaulted ones.  Without the reason, the bridge has to
     re-derive the heuristic, which is exactly the parallel-path
     anti-pattern Gate 4 warns against.
  2. **Telemetry / admin diag** — calibration of the draft model's
     classifier needs the breakdown of *which* guard fires most often.
     Without persisted reasons, calibration has to be re-run against logs.
  3. **The collapsed expert path** (next commit) — when we route to the
     full langchain path, the system prompt needs to know whether to
     bind the full tool registry (actionable intent / agent-bound) or
     reason in-band (refusal override / low-confidence verifier).

This module is the single source of truth.  Everywhere downstream that
mentions an escalation reason imports from here — no string-literal
parallel paths.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional, Union


class EscalationReason(str, Enum):
    """Why dispatch_draft_first escalated this turn to expert.

    Inherits ``str`` so the value round-trips cleanly through JSON
    serialisation (telemetry, WorldModelBridge, SSE) and ``==`` works
    against both the enum member and the raw string.  Matches the
    pattern ``ModelTier`` already uses in this package.
    """

    #: The draft's own classifier returned ``delegate='local'`` or
    #: ``delegate='hive'`` — the model itself decided to hand off.  This
    #: is the baseline / "expected" reason.  All other reasons describe a
    #: *forced* escalation where the dispatcher overrode a
    #: ``delegate='none'`` decision.
    CLASSIFIER_DELEGATE = 'classifier_delegate'

    #: The draft emitted a refusal pattern ("I can't…", "Sorry, I'm
    #: unable…") which violates the role contract.  Standby reply
    #: replaces it and expert (with full tool registry) takes the turn.
    REFUSAL_OVERRIDE = 'refusal_override'

    #: Draft said ``delegate='none'`` but its self-reported confidence
    #: fell below ``_DRAFT_CONFIDENCE_FLOOR``.  Promote to a local
    #: verifier rather than ship an uncertain answer as final.
    LOW_CONFIDENCE = 'low_confidence'

    #: The prompt_id binds this turn to a specific agent (recipe on
    #: disk, not request-id fallback).  The user picked a specialist;
    #: even trivial replies must pass through that specialist's
    #: persona / system prompt / tool registry.
    AGENT_BOUND = 'agent_bound'

    #: Classifier surfaced an actionable-intent flag (channel_connect,
    #: is_create_agent, language_change, invite_intent, join_room_intent,
    #: memory_query).  Answering in-band would orphan the action — only
    #: the expert path has the matching tools bound.
    ACTIONABLE_INTENT = 'actionable_intent'

    #: Draft envelope failed to parse as JSON.  Defaults to a local
    #: escalation so we don't ship an unparseable draft as final.  Often
    #: indicates the draft model needs re-prompting or replacement.
    PARSE_FAILURE = 'parse_failure'


def coerce(value: Union[None, str, EscalationReason]) -> Optional[EscalationReason]:
    """Coerce a possibly-string value to ``EscalationReason``.

    Returns ``None`` when ``value`` is None / empty / unrecognised so
    callers can use this in legacy paths that may have stamped a raw
    string in a dict.  Never raises — escalation_reason is observability
    metadata; a bad value must not break the chat path.
    """
    if value is None or value == '':
        return None
    if isinstance(value, EscalationReason):
        return value
    try:
        return EscalationReason(str(value))
    except ValueError:
        return None
