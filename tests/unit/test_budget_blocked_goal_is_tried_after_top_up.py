"""A goal refused for budget is tried again once it has spark.

Measured on central 2026-11-03 (deployed 9180ba44d, read-only):
- hive_growth goal 960a8332 (continuous, never_pause) logged "out of
  budget (insufficient_budget (0 < 1)) but declares never_pause" at
  2026-10-30 15:47:57Z, 17 minutes after central started.
- It was topped up on 10-31 at 10:15:14Z and had 2000 spark left.
- Every tick since then skipped it at the in-memory budget-blocked check,
  before the budget was read again. That meant no dispatch and no log line
  for about 63 hours, until a restart. The only discard of the mark sat
  behind that check.

Behavioural: this drives the real AgentDaemon._tick,
budget_gate.apply_budget_pause and GoalManager.pause_goal. The boundaries
are the ones test_hive_handoff_is_settled_by_its_run replaces: the DB
session, the personas, the gates, the prompt builder and the /chat turn.
Two settings differ: the cost estimate is 1 spark, so a goal with none left
is refused, and the budget-blocked mark is kept between ticks, because that
mark is what is under test.

    python -m pytest tests/unit/test_budget_blocked_goal_is_tried_after_top_up.py -q
"""
from unittest.mock import patch

import pytest

from integrations.agent_engine import agent_daemon, budget_gate
from integrations.agent_engine.agent_daemon import AgentDaemon
from tests.unit.test_hive_handoff_is_settled_by_its_run import (  # noqa: F401
    Goal, _boundaries, _run, charging_chat, coord)

NEVER_PAUSE = 'goal-budget-never-pause'
PAUSABLE = 'goal-budget-pausable'


@pytest.fixture(autouse=True)
def _no_marks_left_behind():
    yield
    for gid in (NEVER_PAUSE, PAUSABLE):
        agent_daemon._budget_blocked_goals.discard(gid)
        agent_daemon._dispatch_backoff.pop(gid, None)


def _tick(goal, coord, chat, pauses):
    """One real tick run on this node, with a cost estimate of 1 spark.
    The budget-blocked mark carries over from the tick before.
    ``pauses`` counts the calls to the real apply_budget_pause."""
    goal.last_dispatched_at = None
    patches = _boundaries(goal, coord, chat, peers=False) + [
        patch('integrations.agent_engine.budget_gate.estimate_llm_cost_spark',
              return_value=1),
        patch('integrations.agent_engine.budget_gate.apply_budget_pause',
              side_effect=pauses),
    ]
    _run(patches, lambda: AgentDaemon()._tick())


def _counting():
    calls = []
    real = budget_gate.apply_budget_pause

    def _pause(goal, reason, db=None):
        calls.append(reason)
        return real(goal, reason, db=db)
    return calls, _pause


def test_a_never_pause_goal_topped_up_is_dispatched_again(coord):
    """FAILS BEFORE THE FIX: after the top-up the goal stayed skipped by
    the mark and the turn never ran (central's hive_growth goal)."""
    goal = Goal(gid=NEVER_PAUSE, cfg={'never_pause': True, 'continuous': True},
                spark=10)
    goal.spark_budget = 10
    chat = charging_chat(goal)
    calls, pause = _counting()

    _tick(goal, coord, chat, pause)
    _tick(goal, coord, chat, pause)
    assert chat.call_count == 0, 'a goal with no spark left must not run'
    assert goal.status == 'active', 'never_pause keeps it active'
    assert len(calls) == 1, f'refused once, not on every tick: {calls}'

    goal.spark_budget = 4000                     # the owner tops it up
    _tick(goal, coord, chat, pause)
    assert chat.call_count == 1, 'the topped-up goal never ran'
    assert NEVER_PAUSE not in agent_daemon._budget_blocked_goals
    assert len(calls) == 1


def test_a_budget_paused_goal_resumed_with_spark_runs_on_the_next_tick(coord):
    """FAILS BEFORE THE FIX: the pause also left the mark behind, so a goal
    the owner resumed with fresh spark was skipped until the next prune of
    the marks, up to 100 ticks later, or until a restart."""
    goal = Goal(gid=PAUSABLE, spark=10)
    goal.spark_budget = 10
    chat = charging_chat(goal)
    calls, pause = _counting()

    _tick(goal, coord, chat, pause)
    assert goal.status == 'paused', goal.config_json
    assert goal.config_json.get('pause_cause') == 'budget'
    assert len(calls) == 1
    assert chat.call_count == 0

    goal.status = 'active'                       # the owner resumes it
    goal.spark_budget = 4000                     # with fresh spark
    _tick(goal, coord, chat, pause)
    assert chat.call_count == 1, 'the resumed goal never ran'
    assert len(calls) == 1


def test_a_resumed_goal_still_without_spark_is_paused_again(coord):
    """The other side: a goal resumed without enough spark is paused again
    by the same budget pause. It is not left active and silently skipped."""
    goal = Goal(gid=PAUSABLE, spark=10)
    goal.spark_budget = 10
    chat = charging_chat(goal)
    calls, pause = _counting()

    _tick(goal, coord, chat, pause)
    assert goal.status == 'paused'
    goal.status = 'active'                       # resumed, no spark added
    _tick(goal, coord, chat, pause)
    assert goal.status == 'paused', 'a resumed goal with no spark stays active'
    assert len(calls) == 2
    assert chat.call_count == 0
