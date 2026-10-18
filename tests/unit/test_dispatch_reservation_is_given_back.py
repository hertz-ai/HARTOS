"""Spark reserved at dispatch is given back when the dispatch ends.

check_goal_budget reserves the estimated cost by adding it to
goal.spark_spent before the turn runs ("check and reserve").  Nothing gave
it back, so the reservation stood in for work in two places:

- the completion gate reads "spark spent this dispatch" as evidence; on
  central 2026-10-17 18 goals whose every turn failed were completed on the
  4 spark the expert reservation added (review of aeb5d4831, finding 1);
- budgets drained by 4 spark per failed or waiting dispatch until the
  budget gate paused the goal: "insufficient_budget (0 < 4)", five goals on
  central the same evening.

The steward's rule (budget_gate.charge_goal_work_completed, 2026-06-10) is
that spark_spent rises only on work done.  The reservation is a pre-check:
held while the dispatch runs, released when it ends.

Behavioural: the real check_goal_budget, charge_goal_work_completed and
release_goal_reservations against a stand-in session (the DB boundary), as
tests/unit/test_charge_goal_work_completed.py does.

    python -m pytest tests/unit/test_dispatch_reservation_is_given_back.py -q
"""
import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.agent_engine import budget_gate  # noqa: E402

GOAL = 'goal-res-1'


class Goal:
    def __init__(self, budget=300, spent=100):
        self.id = GOAL
        self.spark_budget = budget
        self.spark_spent = spent
        self.status = 'active'


def _db(goal):
    """A session whose AgentGoal lookups (by id, and by prompt id with
    status) resolve to ``goal``."""
    db = MagicMock()
    db.query.return_value.filter_by.return_value.with_for_update \
        .return_value.first.return_value = goal
    db.query.return_value.filter.return_value.with_for_update \
        .return_value.first.return_value = goal
    return db


@pytest.fixture(autouse=True)
def _clean():
    budget_gate.invalidate_goal_budget_cache()
    budget_gate._held_reservations.pop(GOAL, None)
    yield
    budget_gate.invalidate_goal_budget_cache()
    budget_gate._held_reservations.pop(GOAL, None)


def test_a_reservation_is_held_until_released():
    goal = Goal(spent=100)
    db = _db(goal)
    with patch('integrations.social.models.get_db', return_value=db):
        allowed, remaining, reason = budget_gate.check_goal_budget(GOAL, 4)
        assert (allowed, reason) == (True, 'budget_reserved')
        assert goal.spark_spent == 104
        assert budget_gate.held_reservation(GOAL) == 4
        released = budget_gate.release_goal_reservations(GOAL)
    assert released == 4
    assert goal.spark_spent == 100, 'the reservation is not work'
    assert budget_gate.held_reservation(GOAL) == 0


def test_work_charged_during_the_dispatch_stays():
    goal = Goal(spent=100)
    db = _db(goal)
    with patch('integrations.social.models.get_db', return_value=db):
        budget_gate.check_goal_budget(GOAL, 4)
        assert budget_gate.charge_goal_work_completed('12345', 2) is True
        budget_gate.release_goal_reservations(GOAL)
    assert goal.spark_spent == 102, 'only the completed-work charge remains'


def test_a_cached_check_reserves_nothing_and_holds_nothing():
    goal = Goal(spent=100)
    db = _db(goal)
    with patch('integrations.social.models.get_db', return_value=db):
        budget_gate.check_goal_budget(GOAL, 4)
        budget_gate.check_goal_budget(GOAL, 4)      # within the cache window
    assert goal.spark_spent == 104
    assert budget_gate.held_reservation(GOAL) == 4


def test_release_never_takes_spent_below_zero():
    goal = Goal(spent=100)
    db = _db(goal)
    with patch('integrations.social.models.get_db', return_value=db):
        budget_gate.check_goal_budget(GOAL, 4)
        goal.spark_spent = 1                         # an operator reset it
        budget_gate.release_goal_reservations(GOAL)
    assert goal.spark_spent == 0


def test_release_clears_the_cache_so_the_next_check_reads_the_row():
    goal = Goal(budget=104, spent=100)
    db = _db(goal)
    with patch('integrations.social.models.get_db', return_value=db):
        budget_gate.check_goal_budget(GOAL, 4)      # leaves 0: cached 'allowed'
        budget_gate.release_goal_reservations(GOAL)
        allowed, remaining, reason = budget_gate.check_goal_budget(GOAL, 4)
    assert (allowed, reason) == (True, 'budget_reserved'), \
        'a stale cached verdict was served after the release'
    assert goal.spark_spent == 104


def test_a_failed_release_keeps_the_hold_for_the_next_one():
    goal = Goal(spent=100)
    with patch('integrations.social.models.get_db', return_value=_db(goal)):
        budget_gate.check_goal_budget(GOAL, 4)
    broken = MagicMock()
    broken.query.side_effect = RuntimeError('database is locked')
    with patch('integrations.social.models.get_db', return_value=broken):
        assert budget_gate.release_goal_reservations(GOAL) == 0
    assert budget_gate.held_reservation(GOAL) == 4
    with patch('integrations.social.models.get_db', return_value=_db(goal)):
        assert budget_gate.release_goal_reservations(GOAL) == 4
    assert goal.spark_spent == 100


def test_two_releases_at_once_give_the_reservation_back_once():
    """Both read the hold, both wrote it back, and spent dropped below what
    the goal had before the reservation (review of e8b6bf675, P5)."""
    goal = Goal(spent=100)
    with patch('integrations.social.models.get_db', return_value=_db(goal)):
        budget_gate.check_goal_budget(GOAL, 4)

    def _slow_row():
        time.sleep(0.3)
        return goal

    slow = MagicMock()
    slow.query.return_value.filter_by.return_value.with_for_update \
        .return_value.first.side_effect = _slow_row
    results = []
    with patch('integrations.social.models.get_db', return_value=slow):
        threads = [threading.Thread(target=lambda: results.append(
            budget_gate.release_goal_reservations(GOAL))) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
    assert sorted(results) == [0, 4]
    assert goal.spark_spent == 100
    assert budget_gate.held_reservation(GOAL) == 0


def test_a_hold_whose_goal_row_is_gone_gives_nothing_back():
    goal = Goal(spent=100)
    with patch('integrations.social.models.get_db', return_value=_db(goal)):
        budget_gate.check_goal_budget(GOAL, 4)
    with patch('integrations.social.models.get_db', return_value=_db(None)):
        assert budget_gate.release_goal_reservations(GOAL) == 0
    assert budget_gate.held_reservation(GOAL) == 0, \
        'a hold on a deleted goal is dropped, not retried forever'


def test_spark_spent_on_work_leaves_the_held_reservation_out():
    goal = Goal(spent=100)
    with patch('integrations.social.models.get_db', return_value=_db(goal)):
        budget_gate.check_goal_budget(GOAL, 4)
    assert goal.spark_spent == 104
    assert budget_gate.spark_spent_on_work(GOAL, goal.spark_spent) == 100
    assert budget_gate.spark_spent_on_work(GOAL, None) == 0
    assert budget_gate.spark_spent_on_work('nothing-held', 7) == 7


def test_nothing_held_releases_nothing():
    broken = MagicMock()
    broken.query.side_effect = AssertionError('must not touch the database')
    with patch('integrations.social.models.get_db', return_value=broken):
        assert budget_gate.release_goal_reservations(GOAL) == 0
