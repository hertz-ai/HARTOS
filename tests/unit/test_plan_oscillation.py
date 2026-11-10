"""Gate-5 pin for the deterministic plan-oscillation guard (#57).

The guard force-approves the latest plan when the reviewer yo-yos the planner's
action count (the 2026-05-28 palindrome-cycle regression).  Extracted from
hart_intelligence_entry to core.plan_oscillation so the decision logic is tested
in isolation — these assertions are what stop that regression from silently
returning.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.plan_oscillation import (  # noqa: E402
    OSC_DELTA, plan_action_count, is_plan_oscillating,
)


def test_plan_action_count_walks_every_flow():
    assert plan_action_count({'flows': [{'actions': [1, 2, 3]}]}) == 3
    assert plan_action_count({'flows': [{'actions': [1, 2, 3]},
                                        {'actions': [4, 5]}, {}]}) == 5
    assert plan_action_count({'flows': [{'actions': [1]}, 'not-a-flow']}) == 1
    assert plan_action_count({'flows': []}) == 0          # no flow
    assert plan_action_count({'flows': [{}]}) == 0        # flow without actions
    assert plan_action_count({}) == 0                     # no flows key
    assert plan_action_count('not-a-dict') is None        # non-dict → no plan
    assert plan_action_count(None) is None


def test_a_seven_flow_plan_folded_into_one_is_a_swing_and_a_kept_one_is_not():
    """A task that names its flows (the owner's speech-therapy note, Flow 1 to
    Flow 7) keeps one flow per named flow.  A refinement that folds them back
    into one flow of 9 steps, or doubles a later flow, is a large swing in the
    plan's size even when the first flow is unchanged; the same seven flows
    re-emitted with one step more are not."""
    seven = {'flows': [{'actions': ['a', 'b', 'c']} for _ in range(7)]}       # 21
    folded = {'flows': [{'actions': list('abcdefghi')}]}                        # 9
    fifth_grew = {'flows': seven['flows'][:4] + [{'actions': list('abcdefgh')}]
                  + seven['flows'][5:]}                                         # 26
    one_more = {'flows': seven['flows'][:6] + [{'actions': ['a', 'b', 'c', 'd']}]}
    assert is_plan_oscillating(plan_action_count(seven), plan_action_count(folded))
    assert is_plan_oscillating(plan_action_count(seven), plan_action_count(fifth_grew))
    assert not is_plan_oscillating(plan_action_count(seven), plan_action_count(one_more))


def test_is_plan_oscillating_needs_two_known_counts():
    # Not enough history → never force-approve.
    assert is_plan_oscillating(None, 5) is False
    assert is_plan_oscillating(5, None) is False
    assert is_plan_oscillating(None, None) is False


def test_is_plan_oscillating_threshold_is_strictly_greater_than_delta():
    assert OSC_DELTA == 3
    # delta exactly == OSC_DELTA must NOT trip (strictly greater-than).
    assert is_plan_oscillating(2, 5) is False   # |5-2| = 3
    assert is_plan_oscillating(5, 2) is False
    assert is_plan_oscillating(5, 5) is False   # no change
    # delta > OSC_DELTA trips.
    assert is_plan_oscillating(2, 6) is True    # |6-2| = 4
    assert is_plan_oscillating(9, 2) is True     # the palindrome repro: 9 -> 2 swing
    assert is_plan_oscillating(0, 10) is True


def test_is_plan_oscillating_custom_delta():
    assert is_plan_oscillating(0, 2, delta=1) is True   # |2-0|=2 > 1
    assert is_plan_oscillating(0, 1, delta=1) is False  # |1-0|=1, not > 1
