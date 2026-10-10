"""A coding goal its builder declines must not take an idle agent.

CodingAgentDaemon._tick picked the next idle agent, reserved it
(``used_agents.add``), and only THEN asked GoalManager.build_prompt for the
goal's prompt.  When the builder declined the goal (returned None) the loop
skipped it, but the agent stayed reserved, so every goal behind it in the
tick found that agent taken.

Measured on the live desktop DB 2026-09-29 (read-only): the autoresearch
coordinator seed, active, config {"mode": "coordinator", ...} with no
run_command, so _build_autoresearch_prompt returns None for it on every
tick.  Its last_dispatched_at has been frozen at 2026-03-24 02:55 because a
declined goal is never stamped, and the daemon orders by
last_dispatched_at ascending, so it sits FIRST in the queue and takes the
first idle agent on every coding tick.

agent_daemon had exactly this defect and fixed it on 2026-09-03 (reserve
the agent only after the goal clears build_prompt, agent_daemon.py
"Reserve the agent only now that the goal has cleared every gate"); the
coding daemon was never brought along.

The daemon runs for real here.  Only its boundaries are replaced: the DB
session, the idle-agent query, the yield and affordability gates, the
host-core ceiling, and the /chat dispatch.  The prompt builders are the real
registered ones.
"""
import os
import sys
from unittest.mock import MagicMock, patch

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from integrations.coding_agent.coding_daemon import CodingAgentDaemon  # noqa: E402


class _Goal:
    """Stands in for an AgentGoal row: to_dict() flattens config into the
    top level, exactly as AgentGoal.to_dict() does."""

    def __init__(self, gid, goal_type, config):
        self.id = gid
        self.goal_type = goal_type
        self.title = f'goal {gid}'
        self.description = ''
        self.status = 'active'
        self.priority = 0
        self.last_dispatched_at = None
        self.config_json = dict(config)
        # Every AgentGoal row has it; the completion gate a dispatched coding
        # goal now passes through reads it.
        self.spark_spent = 0

    def to_dict(self):
        d = {'id': self.id, 'goal_type': self.goal_type, 'title': self.title,
             'description': self.description, 'status': self.status,
             'priority': self.priority, 'last_dispatched_at': None}
        d.update(self.config_json)
        return d


def _declined_goal():
    # The live shape: an autoresearch coordinator with nowhere to run.
    # repo_path is set only so the builder never shells out to git.
    return _Goal('declined', 'autoresearch',
                 {'mode': 'coordinator', 'continuous': True,
                  'repo_path': '/nowhere'})


def _fix_goal(gid='fix'):
    # The shape SelfHealingDispatcher._create_fix_goal writes.
    return _Goal(gid, 'self_heal', {
        'mode': 'self_heal',
        'pattern_key': f'KeyError::mod_{gid}::fn',
        'exc_type': 'KeyError',
        'source_module': f'mod_{gid}',
        'source_function': 'fn',
        'occurrence_count': 3,
        'sample_traceback': 'Traceback (most recent call last): ...',
    })


def _run_tick(goals, agents):
    """One real _tick over ``goals`` with ``agents`` idle.  Returns the
    dispatch_to_chat mock."""
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value \
        .all.return_value = goals
    dispatch = MagicMock(return_value='ok')
    with patch('integrations.social.models.get_db', return_value=db), \
            patch('integrations.coding_agent.idle_detection.'
                  'IdleDetectionService.get_idle_agent_personas',
                  return_value=agents), \
            patch('integrations.agent_engine.dispatch.should_yield_to_user',
                  return_value=False), \
            patch('integrations.agent_engine.budget_gate.'
                  'check_platform_affordability', return_value=(True, {})), \
            patch('integrations.agent_engine.dispatch.'
                  'max_autonomous_concurrency', side_effect=lambda cap: cap), \
            patch('integrations.coding_agent.task_distributor.'
                  'dispatch_to_chat', dispatch):
        CodingAgentDaemon()._tick()
    return dispatch


def test_a_declined_goal_leaves_its_agent_for_the_next_goal():
    """FAILS BEFORE THE FIX: the declined goal kept the only idle agent, so
    the fix goal behind it was never dispatched."""
    fix = _fix_goal()
    dispatch = _run_tick([_declined_goal(), fix],
                         [{'user_id': 'agent-1', 'username': 'a1'}])

    assert dispatch.call_count == 1, (
        f'expected the fix goal to be dispatched once, got '
        f'{dispatch.call_count} dispatch(es): a declined goal kept the agent')
    prompt, user_id, goal_id = dispatch.call_args.args[:3]
    assert goal_id == 'fix'
    assert user_id == 'agent-1'
    assert 'KeyError' in prompt
    assert fix.last_dispatched_at is not None


def test_a_declined_goal_is_not_stamped_as_dispatched():
    """Nothing was sent, so nothing may say it was."""
    declined = _declined_goal()
    dispatch = _run_tick([declined],
                         [{'user_id': 'agent-1', 'username': 'a1'}])

    assert dispatch.call_count == 0
    assert declined.last_dispatched_at is None


def test_one_agent_is_never_handed_two_goals():
    """The other side of the invariant: whatever moved, the agent is still
    reserved before its goal is dispatched, so one agent gets one goal."""
    dispatch = _run_tick([_fix_goal('a'), _fix_goal('b')],
                         [{'user_id': 'agent-1', 'username': 'a1'}])

    assert dispatch.call_count == 1
    assert dispatch.call_args.args[2] == 'a'


def test_each_idle_agent_takes_its_own_goal():
    dispatch = _run_tick(
        [_declined_goal(), _fix_goal('a'), _fix_goal('b')],
        [{'user_id': 'agent-1', 'username': 'a1'},
         {'user_id': 'agent-2', 'username': 'a2'}])

    sent = [(c.args[1], c.args[2]) for c in dispatch.call_args_list]
    assert sent == [('agent-1', 'a'), ('agent-2', 'b')]
