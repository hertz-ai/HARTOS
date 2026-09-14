"""#106d: the daemon runs a stuck action's next turn on the expert, and judges
that turn in the same tick.

GoalManager.escalate_goal hands a stuck action to this node's expert model
first (config 'escalation' next='expert', the goal stays active).  The daemon
then dispatches the goal's next turn with the expert's model_config, looked up
by model_id for that one dispatch and never stored.  Dispatch is synchronous,
so _settle_dispatched_goal judges the turn straight after: if the action's
recipe was banked the escalation is dropped and the goal settles as usual;
if not, the action goes to a person.  Only the escalation the turn served is
judged: one raised during the turn waits for its own expert turn.

These drive the real functions with seam fakes, as test_goal_settlement_gate
does.

    python -m pytest tests/unit/test_expert_turn.py --noconftest -q
"""
import os
import sys
import types
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from integrations.agent_engine import agent_daemon as daemon  # noqa: E402
from integrations.agent_engine.model_registry import ModelBackend, ModelTier  # noqa: E402

_GM = 'integrations.agent_engine.goal_manager.GoalManager.escalate_goal'
_GET = 'integrations.agent_engine.model_registry.model_registry.get_model'


class Goal:
    def __init__(self, spark=0, cfg=None, status='active', goal_id='g1'):
        self.id = goal_id
        self.spark_spent = spark
        self.config_json = cfg or {}
        self.status = status


class Db:
    def refresh(self, goal):
        pass


def _esc(action_id=3, at='t1', expert='claude-code'):
    return {'action_id': action_id, 'action': 'Post the thread', 'reason': 'stuck',
            'tried': ['local'], 'user_prompt': 'u1_42', 'prompt_id': 42, 'flow': 0,
            'next': 'expert', 'expert': expert, 'at': at}


def _claude():
    return ModelBackend(model_id='claude-code', display_name='Claude Code',
                        tier=ModelTier.EXPERT, config_list_entry={
                            'model': 'claude-code', 'api_key': 'dummy',
                            'base_url': 'http://127.0.0.1:5000/api/claude/v1'})


def _parking(goal, calls):
    """escalate_goal as it behaves for a same-action ask: park for a person."""
    def escalate(db, goal_id, escalation):
        calls.append(escalation)
        goal.status = 'paused'
        return {'success': True, 'stage': 'human'}
    return escalate


def test_a_goal_with_no_expert_escalation_dispatches_as_before():
    assert daemon._escalation_model_config(Db(), Goal()) == (None, False)
    human = Goal(cfg={'escalation': dict(_esc(), next='human')})
    assert daemon._escalation_model_config(Db(), human) == (None, False)


def test_the_expert_is_looked_up_by_id_for_the_one_turn():
    goal = Goal(cfg={'escalation': _esc()})
    before = repr(goal.config_json)
    with patch(_GET, return_value=_claude()):
        config, parked = daemon._escalation_model_config(Db(), goal)
    assert parked is False
    assert config == [_claude().config_list_entry]
    assert repr(goal.config_json) == before, 'the entry is used, never stored'


def test_an_expert_that_is_gone_hands_the_action_to_a_person():
    goal = Goal(cfg={'escalation': _esc()})
    calls = []
    with patch(_GET, return_value=None), patch(_GM, side_effect=_parking(goal, calls)):
        config, parked = daemon._escalation_model_config(Db(), goal)
    assert (config, parked) == (None, True)
    assert calls[0]['action_id'] == 3
    assert 'no longer available' in calls[0]['reason']
    assert goal.status == 'paused'


def test_a_banked_action_clears_the_escalation_and_settles_as_usual():
    served = _esc()
    goal = Goal(cfg={'escalation': dict(served)})
    with patch.object(daemon, '_action_banked', return_value=True), \
            patch(_GM) as escalate:
        daemon._settle_dispatched_goal(Db(), goal, 'g1', served_escalation=served)
    escalate.assert_not_called()
    assert 'escalation' not in goal.config_json
    assert goal.status == 'active'
    assert goal.config_json.get('noop_dispatch_count') == 1, 'the usual settle ran'


def test_an_unbanked_action_goes_to_a_person_and_nothing_else_is_settled():
    served = _esc()
    goal = Goal(cfg={'escalation': dict(served)})
    calls = []
    with patch.object(daemon, '_action_banked', return_value=False), \
            patch(_GM, side_effect=_parking(goal, calls)):
        daemon._settle_dispatched_goal(Db(), goal, 'g1', served_escalation=served)
    assert [c['reason'] for c in calls] == ['the expert model did not finish it']
    assert (calls[0]['action_id'], calls[0]['prompt_id'], calls[0]['flow']) == (3, 42, 0)
    assert goal.status == 'paused'
    assert 'noop_dispatch_count' not in goal.config_json


def test_an_escalation_raised_during_the_turn_waits_for_its_own_turn():
    served = _esc(action_id=3, at='t1')
    raised = _esc(action_id=4, at='t2')
    goal = Goal(cfg={'escalation': raised})
    with patch.object(daemon, '_action_banked', return_value=False), \
            patch(_GM) as escalate:
        daemon._settle_dispatched_goal(Db(), goal, 'g1', served_escalation=served)
    escalate.assert_not_called()
    assert goal.config_json['escalation'] == raised
    assert goal.status == 'active'


def test_a_turn_that_served_no_expert_is_settled_as_before():
    goal = Goal(cfg={'escalation': _esc()})
    with patch.object(daemon, '_action_banked') as banked, patch(_GM) as escalate:
        daemon._settle_dispatched_goal(Db(), goal, 'g1')
    banked.assert_not_called()
    escalate.assert_not_called()
    assert goal.config_json['escalation']['next'] == 'expert'


def test_banked_means_the_actions_recipe_file_exists(tmp_path):
    recipe = tmp_path / '42_0_3.json'

    def safe_prompt_path(*parts, ext='.json'):
        return str(tmp_path / ('_'.join(str(p) for p in parts) + ext))
    with patch.dict(sys.modules, {'hartos.helper': types.SimpleNamespace(
            safe_prompt_path=safe_prompt_path)}):
        assert daemon._action_banked(_esc()) is False
        recipe.write_text('{}')
        assert daemon._action_banked(_esc()) is True
        assert daemon._action_banked({'action_id': 3}) is False, 'no path, not banked'
