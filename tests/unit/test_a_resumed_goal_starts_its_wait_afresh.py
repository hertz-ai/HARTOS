"""A resumed goal's verification wait starts at the resume, whatever paused it.

Measured on central 2026-11-20: goal 61e48abc paused needs_help at 05:29Z.
At 07:41:26 it was resumed through GoalManager.resume_goal; the daemon
dispatched it again and the coordinator released its task held for help.
At 07:45:49, four minutes after the resume, the settle gate logged
"AUTO-PAUSED after 1839s awaiting ledger grounding".  The settle gate's
wait marks (awaiting_verification_since, last_verification_check) had
survived the needs_help pause and the resume, so the resumed goal's first
settles read the wait from before the pause as already 30 minutes old.
The ledger-grounding pause drops its own mark; every other pause kept it,
and resume_goal never cleared it.

Behavioural: a real SQLite AgentGoal, the real GoalManager.pause_goal and
resume_goal, and the real agent_daemon._settle_dispatched_goal.  Replaced
at the boundaries only: the coordinator's ledger-grounding read (tasks
outstanding), its held-for-help reader (nothing held) and the provider
check (dispatch.goal_turn_held: not held).

    python -m pytest tests/unit/test_a_resumed_goal_starts_its_wait_afresh.py -q
"""
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from integrations.social.models import AgentGoal, Base  # noqa: E402
from integrations.agent_engine import agent_daemon, dispatch  # noqa: E402
from integrations.agent_engine.goal_manager import (  # noqa: E402
    GoalManager, PAUSE_CAUSE_NEEDS_HELP)

GID = '61e48abc-1b3c-48c1-a771-ae5883fed33b'


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv('HEVOLVE_GOAL_VERIFY_TIMEOUT_S', '1800')
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _ago(seconds):
    return (datetime.utcnow() - timedelta(seconds=seconds)).isoformat()


def _goal(db, cfg, status='active'):
    db.add(AgentGoal(id=GID, goal_type='marketing', title='email desk',
                     status=status, spark_spent=5, config_json=dict(cfg)))
    db.commit()
    return db.get(AgentGoal, GID)


@contextmanager
def _hive_run_outstanding():
    """The hive run of the goal holds a task nobody finished, and no task
    waits on a person: the wait counts."""
    with patch.object(agent_daemon, '_goal_ledger_grounding', return_value=False), \
            patch.object(agent_daemon, '_hive_work_held_for', return_value=None), \
            patch.object(dispatch, 'goal_turn_held', return_value=''):
        yield


def _settle(db, goal):
    with _hive_run_outstanding():
        agent_daemon._settle_dispatched_goal(db, goal, GID, handed_to_hive=True)
    db.commit()
    db.expire_all()
    return db.get(AgentGoal, GID)


def test_a_goal_resumed_after_a_needs_help_pause_is_not_paused_on_its_old_wait(db):
    """FAILS BEFORE THE FIX: the first settle after the resume read the
    40-minute wait from before the pause and paused the goal again with
    cause ledger_grounding."""
    goal = _goal(db, {'awaiting_verification_since': _ago(2400),
                      'last_verification_check': _ago(2350),
                      'spark_at_handoff': 5})
    assert GoalManager.pause_goal(
        db, goal, cause=PAUSE_CAUSE_NEEDS_HELP, by='daemon',
        reason='Paused for help: step 3 could not be finished autonomously')['success']
    db.commit()
    assert GoalManager.resume_goal(db, GID, resumed_by='person:owner')['success']
    db.commit()

    goal = _settle(db, db.get(AgentGoal, GID))
    cfg = goal.config_json
    assert goal.status == 'active', (cfg.get('pause_cause'), cfg.get('pause_reason'))
    assert 'pause_cause' not in cfg and cfg['last_pause_cause'] == PAUSE_CAUSE_NEEDS_HELP
    waited = (datetime.utcnow()
              - datetime.fromisoformat(cfg['awaiting_verification_since'])).total_seconds()
    assert 0 <= waited < 60, f'the wait did not start at the resume ({waited:.0f}s old)'
    checked = (datetime.utcnow()
               - datetime.fromisoformat(cfg['last_verification_check'])).total_seconds()
    assert 0 <= checked < 60
    # The hive run's baseline is not a wait mark: the run is still the
    # goal's, so the spark its worker charges is counted from its handoff.
    assert cfg['spark_at_handoff'] == 5


def test_an_unpaused_goal_whose_wait_really_expired_still_pauses(db):
    """No pause, no resume: a wait older than the timeout still pauses the
    goal as ledger_grounding, as before."""
    goal = _goal(db, {'awaiting_verification_since': _ago(2400),
                      'last_verification_check': _ago(60),
                      'spark_at_handoff': 5})
    goal = _settle(db, goal)
    cfg = goal.config_json
    assert goal.status == 'paused'
    assert cfg['pause_cause'] == 'ledger_grounding'
    assert 'ledger never reported every task done' in cfg['pause_reason']


def test_a_resume_of_a_goal_with_no_wait_marks_is_unchanged(db):
    """Nothing to drop: the resume writes only its own record."""
    goal = _goal(db, {'pause_reason': 'r', 'paused_at': 'x',
                      'pause_cause': 'person', 'paused_by': 'person:owner',
                      'noop_dispatch_count': 2, 'continuous': False},
                 status='paused')
    assert GoalManager.resume_goal(db, GID, resumed_by='person:owner')['success']
    db.commit()
    db.expire_all()
    cfg = db.get(AgentGoal, GID).config_json
    assert set(cfg) == {'last_pause_reason', 'last_paused_at', 'last_pause_cause',
                        'last_paused_by', 'noop_dispatch_count', 'continuous',
                        'resume_count', 'resumed_at', 'resumed_by'}
    assert cfg['noop_dispatch_count'] == 2 and cfg['continuous'] is False
