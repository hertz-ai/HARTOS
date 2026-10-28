"""One resume routine: GoalManager.resume_goal is how a paused goal becomes
active again, for the dashboard's resume verb and for the agent daemon alike
(review of 26b5f8177, finding 7: two resume paths with different semantics,
the dashboard's leaving the old pause reason and strike count on the goal).

Pinned against a real SQLite AgentGoal through the real writers:
  * resume_goal moves the pause record (pause_reason, paused_at, pause_cause,
    paused_by) to its last_* history, counts the resume, stamps who did it,
    and writes the status through update_goal_status;
  * it refuses a goal that is not paused and leaves a refused status write
    exactly as it was;
  * the dashboard's resume verb goes through it (a person's resume carries
    'person:<actor>'), and the dashboard's pause verb records the person as
    the cause, so the daemon never undoes a person's pause;
  * a CodingGoal is still set directly, as before.
"""
import os
import sys
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from integrations.social.models import AgentGoal, Base  # noqa: E402
from integrations.agent_engine.goal_manager import GoalManager  # noqa: E402
from integrations.social.dashboard_service import _write_goal_status  # noqa: E402

PAUSED = {'pause_reason': 'Auto-paused: 5 consecutive dispatch failures',
          'paused_at': datetime(2026, 10, 25, 10, 0).isoformat(),
          'pause_cause': 'dispatch_failures', 'paused_by': 'daemon',
          'noop_dispatch_count': 2}


@pytest.fixture
def db():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _goal(db, gid, cfg, status='paused'):
    db.add(AgentGoal(id=gid, goal_type='marketing', title=gid, status=status,
                     config_json=dict(cfg)))
    db.commit()
    return db.get(AgentGoal, gid)


def test_resume_goal_moves_the_pause_record_to_history_and_writes_through_the_one_writer(db):
    goal = _goal(db, 'g1', PAUSED)
    with patch.object(GoalManager, 'update_goal_status',
                      wraps=GoalManager.update_goal_status) as writer:
        result = GoalManager.resume_goal(db, 'g1', resumed_by='person:owner')
    assert result['success'] and writer.call_args[0][1:] == ('g1', 'active')
    db.commit()
    db.expire_all()
    cfg = db.get(AgentGoal, 'g1').config_json
    assert db.get(AgentGoal, 'g1').status == 'active'
    assert cfg['last_pause_reason'] == PAUSED['pause_reason']
    assert cfg['last_paused_at'] == PAUSED['paused_at']
    assert cfg['last_pause_cause'] == 'dispatch_failures'
    assert cfg['last_paused_by'] == 'daemon'
    assert not {'pause_reason', 'paused_at', 'pause_cause', 'paused_by'} & set(cfg)
    assert cfg['resume_count'] == 1 and cfg['resumed_by'] == 'person:owner'
    assert cfg['resumed_at'] and cfg['noop_dispatch_count'] == 2  # runtime state kept


def test_resume_goal_refuses_a_goal_that_is_not_paused(db):
    _goal(db, 'g2', {}, status='active')
    result = GoalManager.resume_goal(db, 'g2', resumed_by='person:owner')
    assert not result['success'] and 'not paused' in result['error']
    assert GoalManager.resume_goal(db, 'missing', resumed_by='x')['error'] == 'Goal not found'


def test_a_refused_status_write_leaves_the_config_as_it_was(db):
    goal = _goal(db, 'g3', PAUSED)
    before = dict(goal.config_json)
    with patch.object(GoalManager, 'update_goal_status',
                      return_value={'success': False, 'error': 'refused'}):
        assert not GoalManager.resume_goal(db, 'g3', resumed_by='x')['success']
    assert goal.status == 'paused' and goal.config_json == before


def test_the_dashboard_resume_verb_goes_through_resume_goal(db):
    goal = _goal(db, 'g4', PAUSED)
    with patch.object(GoalManager, 'resume_goal', wraps=GoalManager.resume_goal) as resume:
        _write_goal_status(db, goal, 'active', actor_id='owner')
    assert resume.call_args.kwargs == {'resumed_by': 'person:owner'}
    assert goal.status == 'active' and 'pause_reason' not in goal.config_json


def test_the_dashboard_pause_verb_records_the_person_as_the_cause(db):
    goal = _goal(db, 'g5', {'resume_count': 1}, status='active')
    _write_goal_status(db, goal, 'paused', actor_id='owner')
    cfg = goal.config_json
    assert goal.status == 'paused'
    assert cfg['pause_cause'] == 'person' and cfg['paused_by'] == 'person:owner'
    assert cfg['paused_at'] and cfg['resume_count'] == 1


def test_a_second_dashboard_resume_starts_from_the_persons_pause(db):
    """After a person's pause, a resume carries no daemon record: the next
    pause of any kind is read on its own merits."""
    goal = _goal(db, 'g6', PAUSED)
    _write_goal_status(db, goal, 'active', actor_id='a')
    _write_goal_status(db, goal, 'paused', actor_id='b')
    _write_goal_status(db, goal, 'active', actor_id='c')
    cfg = goal.config_json
    assert cfg['last_pause_cause'] == 'person' and cfg['last_paused_by'] == 'person:b'
    assert cfg['resume_count'] == 2 and cfg['resumed_by'] == 'person:c'


def test_a_coding_goal_is_still_set_directly():
    class CodingGoal:
        status = 'paused'
    goal = CodingGoal()
    _write_goal_status(None, goal, 'active', actor_id='owner')
    assert goal.status == 'active'
