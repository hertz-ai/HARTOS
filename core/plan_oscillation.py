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
    """Action count of a proposed_plan over ALL its flows, or None if not
    derivable.

    A plan may carry several flows (a task that names its own flows keeps one
    per flow, gather_agentdetails' autonomous prompt).  Counting only the first
    let a refinement that folded seven flows back into one look like a jump of
    the first flow's steps, and a swing inside any later flow go unseen.
    Never raises.  A non-dict input returns None (no plan); a dict with
    no/empty flows returns 0; a flow that is not a dict counts 0.  Flows
    that are not a list (the model wrote them as JSON text) return None:
    iterating the text counted every character's actions as 0, so a format
    slip read as a swing to 0 and fired the guard (review of 494d5a4e1, Y2).
    """
    if not isinstance(parsed, dict):
        return None
    flows = parsed.get('flows')
    if flows is None:
        return 0
    if not isinstance(flows, list):
        return None
    try:
        return sum(len((f.get('actions') if isinstance(f, dict) else None) or [])
                   for f in flows)
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
