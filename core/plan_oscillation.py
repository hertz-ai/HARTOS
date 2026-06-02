"""Deterministic plan-oscillation guard (palindrome-cycle safety net).

Extracted from ``hart_intelligence_entry._autonomous_gather_info`` (#57, Gate 5)
so the deterministic check that stopped the 2026-05-28 palindrome cycle — where
the plan reviewer yo-yoed the planner's action count (round 1 "exceeds 5-step
limit", round 2 "2 violates >=5 requirement") instead of converging — can be
unit-tested in isolation, WITHOUT importing the heavy Flask entry module.

Behaviour is identical to the original inline guard (absolute delta > OSC_DELTA);
a relative-swing threshold (review note M2) is left as a deliberate follow-up so
this extraction introduces no behavioural change.
"""
from typing import Optional

# Force-approve the latest plan when consecutive action counts swing by MORE
# than this between proposed_plans (the reviewer is not converging).
OSC_DELTA = 3


def plan_action_count(parsed) -> Optional[int]:
    """Action count of a proposed_plan's first flow, or None if not derivable.

    Mirrors the cheap dict-walk the guard ran inline; never raises.  A non-dict
    input returns None (no plan); a dict with no/empty flows returns 0.
    """
    if not isinstance(parsed, dict):
        return None
    try:
        flows = parsed.get('flows') or []
        actions = (flows[0].get('actions') if flows else []) or []
        return len(actions)
    except Exception:
        return None


def is_plan_oscillating(prev_count, cur_count, delta: int = OSC_DELTA) -> bool:
    """True when the action count swings by MORE than ``delta`` between two
    consecutive proposed_plans — i.e. the reviewer is yo-yoing the planner and
    the caller should force-approve to break the cycle.

    Both counts must be known; a None on either side means there isn't enough
    history yet, so it returns False (no force-approve).
    """
    if prev_count is None or cur_count is None:
        return False
    return abs(cur_count - prev_count) > delta
