"""budget_gate.charge_goal_work_completed — spark metered on COMPLETED work.

Steward decision 2026-06-10 (option (a)): local compute is free at dispatch
(estimate_llm_cost_spark prices local at 0), so goal.spark_spent could never
rise on an all-local box and the daemon's spark-only completion gate was
structurally unsatisfiable (completed stuck at 13; 45 goals noop-paused).
The fix meters work when a flow ACTUALLY finishes (CREATE: _save_flow_recipe;
REUSE: _advance_reuse_action all-done branch) — never at dispatch, which
would complete goals before their work ran.

Behavioural: mocks the DB boundary (get_db/AgentGoal query chain), calls the
real function, asserts observable spark mutations + commit/rollback calls.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.agent_engine.budget_gate import charge_goal_work_completed  # noqa: E402


def _db_with_goal(goal):
    """Fake session whose AgentGoal query resolves to `goal`."""
    db = MagicMock()
    (db.query.return_value
       .filter.return_value
       .with_for_update.return_value
       .first.return_value) = goal
    return db


def _goal(budget=100, spent=0, goal_id='g-1'):
    g = MagicMock()
    g.id = goal_id
    g.spark_budget = budget
    g.spark_spent = spent
    return g


class TestChargeGoalWorkCompleted:
    def test_charges_actions_count_and_commits(self):
        goal = _goal(budget=100, spent=0)
        db = _db_with_goal(goal)
        with patch('integrations.social.models.get_db', return_value=db), \
             patch('integrations.social.models.AgentGoal', MagicMock()):
            ok = charge_goal_work_completed('12345', actions_completed=4)
        assert ok is True
        assert goal.spark_spent == 4
        db.commit.assert_called_once()

    def test_minimum_charge_is_one(self):
        goal = _goal(budget=100, spent=10)
        db = _db_with_goal(goal)
        with patch('integrations.social.models.get_db', return_value=db), \
             patch('integrations.social.models.AgentGoal', MagicMock()):
            ok = charge_goal_work_completed('12345', actions_completed=0)
        assert ok is True
        assert goal.spark_spent == 11  # +max(1, 0)

    def test_clamps_to_remaining_budget(self):
        goal = _goal(budget=10, spent=8)
        db = _db_with_goal(goal)
        with patch('integrations.social.models.get_db', return_value=db), \
             patch('integrations.social.models.AgentGoal', MagicMock()):
            ok = charge_goal_work_completed('12345', actions_completed=5)
        assert ok is True
        assert goal.spark_spent == 10  # clamped: +2, not +5
        db.commit.assert_called_once()

    def test_budget_exhausted_records_nothing(self):
        goal = _goal(budget=10, spent=10)
        db = _db_with_goal(goal)
        with patch('integrations.social.models.get_db', return_value=db), \
             patch('integrations.social.models.AgentGoal', MagicMock()):
            ok = charge_goal_work_completed('12345', actions_completed=3)
        assert ok is False
        assert goal.spark_spent == 10  # unchanged
        db.commit.assert_not_called()
        db.rollback.assert_called_once()

    def test_no_matching_active_goal_is_noop(self):
        db = _db_with_goal(None)
        with patch('integrations.social.models.get_db', return_value=db), \
             patch('integrations.social.models.AgentGoal', MagicMock()):
            ok = charge_goal_work_completed('does-not-exist', 2)
        assert ok is False
        db.commit.assert_not_called()

    def test_none_prompt_id_is_noop_without_db(self):
        with patch('integrations.social.models.get_db') as gd:
            assert charge_goal_work_completed(None, 3) is False
            gd.assert_not_called()

    def test_db_failure_never_raises(self):
        with patch('integrations.social.models.get_db',
                   side_effect=RuntimeError('db down')):
            assert charge_goal_work_completed('12345', 2) is False
