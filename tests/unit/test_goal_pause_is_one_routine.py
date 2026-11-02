"""One pause routine: GoalManager.pause_goal is how a goal becomes paused,
for every writer (review of 20dc8c996 by hartos-73: only two of the seven
pause writers set a cause, so the daemon resumed goals it did not pause).

Pinned against a real SQLite AgentGoal through the real writers:
  * pause_goal replaces the WHOLE pause record (cause, by, at, reason), keeps
    the goal's runtime state, carries no previous pause's signature unless
    given, writes the status through update_goal_status, renews the record
    of an already-paused goal without a status write, refuses a finished
    goal, and leaves a refused status write exactly as it was;
  * every writer stamps its cause: the daemon's dispatch-failure pause,
    the settlement gate's ledger-grounding and no-op pauses, the budget
    gate, escalate_goal's hand to a person, and the dashboard's pause verb;
  * update_goal_status moving a paused goal to active goes through
    resume_goal (so no caller can resume without the history), and
    resume_goal itself still works.
"""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from integrations.social.models import AgentGoal, Base  # noqa: E402
from integrations.agent_engine import goal_manager as gm  # noqa: E402
from integrations.agent_engine.goal_manager import GoalManager  # noqa: E402
import integrations.agent_engine.agent_daemon as ad  # noqa: E402


@pytest.fixture
def db():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    ad._dispatch_backoff.clear()
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    ad._dispatch_backoff.clear()


def _goal(db, gid, cfg=None, status='active', goal_type='marketing'):
    db.add(AgentGoal(id=gid, goal_type=goal_type, title=gid, status=status,
                     config_json=dict(cfg or {})))
    db.commit()
    return db.get(AgentGoal, gid)


def _record(goal):
    cfg = goal.config_json or {}
    return (cfg.get('pause_cause'), cfg.get('paused_by'), bool(cfg.get('paused_at')),
            cfg.get('pause_reason'))


def test_pause_goal_replaces_the_whole_record_through_the_one_status_writer(db):
    goal = _goal(db, 'g1', {'noop_dispatch_count': 2, 'pause_signature': 'old sig'})
    with patch.object(GoalManager, 'update_goal_status',
                      wraps=GoalManager.update_goal_status) as writer:
        result = GoalManager.pause_goal(db, goal, cause='budget', by='daemon',
                                        reason='Auto-paused: budget gate blocked')
    assert result['success'] and writer.call_args[0][1:3] == ('g1', 'paused')
    db.commit()
    db.expire_all()
    goal = db.get(AgentGoal, 'g1')
    assert goal.status == 'paused'
    assert _record(goal) == ('budget', 'daemon', True, 'Auto-paused: budget gate blocked')
    assert goal.config_json['noop_dispatch_count'] == 2          # runtime state kept
    assert 'pause_signature' not in goal.config_json             # a previous pause's detail dropped
    assert result['config'] == goal.config_json


def test_pause_goal_renews_an_already_paused_goals_record_without_a_status_write(db):
    goal = _goal(db, 'g2', {'pause_cause': 'dispatch_failures', 'paused_by': 'daemon',
                            'pause_reason': 'Auto-paused: 5 consecutive dispatch failures',
                            'paused_at': 'x'}, status='paused')
    with patch.object(GoalManager, 'update_goal_status') as writer:
        result = GoalManager.pause_goal(db, goal, cause='needs_help', by='daemon',
                                        reason='Needs help: action 3')
    assert result['success'] and not writer.called
    assert _record(goal) == ('needs_help', 'daemon', True, 'Needs help: action 3')


def test_pause_goal_refuses_a_finished_goal_and_keeps_a_refused_write_as_it_was(db):
    done = _goal(db, 'g3', {'completed_at': 'x'}, status='completed')
    assert not GoalManager.pause_goal(db, done, cause='person', by='person:a', reason='r')['success']
    assert done.status == 'completed' and 'pause_cause' not in done.config_json
    live = _goal(db, 'g4', {'k': 1})
    before = dict(live.config_json)
    with patch.object(GoalManager, 'update_goal_status',
                      return_value={'success': False, 'error': 'refused'}):
        assert not GoalManager.pause_goal(db, live, cause='person', by='p', reason='r')['success']
    assert live.status == 'active' and live.config_json == before


def test_pause_goal_takes_a_caller_prepared_config_and_extra_detail(db):
    goal = _goal(db, 'g5', {'awaiting_verification_since': 'old', 'spark_at_handoff': 3})
    prepared = dict(goal.config_json)
    prepared.pop('awaiting_verification_since')
    result = GoalManager.pause_goal(db, goal, cause='ledger_grounding', by='daemon',
                                    reason='Auto-paused: ledger', config=prepared,
                                    extra={'pause_signature': 'sig'})
    assert result['success']
    cfg = goal.config_json
    assert 'awaiting_verification_since' not in cfg and cfg['spark_at_handoff'] == 3
    assert cfg['pause_signature'] == 'sig' and cfg['pause_cause'] == 'ledger_grounding'


def test_the_daemons_dispatch_failure_pause_stamps_its_cause(db):
    goal = _goal(db, 'd1')
    ad._auto_pause_for_dispatch_failures(db, goal, 'd1', 5, 'turn failed: 400')
    assert _record(goal)[:2] == (gm.PAUSE_CAUSE_DISPATCH_FAILURES, 'daemon')
    assert goal.config_json['pause_signature'] == 'turn failed: 400'


def test_the_settlement_gates_noop_pause_stamps_its_cause(db):
    goal = _goal(db, 'n1', {'noop_dispatch_count': 4})
    ad._settle_dispatched_goal(db, goal, 'n1')
    assert goal.status == 'paused'
    assert _record(goal)[:2] == (gm.PAUSE_CAUSE_NOOP_DISPATCHES, 'daemon')
    assert goal.config_json['noop_dispatch_count'] == 5


def test_the_settlement_gates_ledger_pause_stamps_its_cause(db):
    stale = (datetime.utcnow() - timedelta(seconds=3600)).isoformat()
    goal = _goal(db, 'l1', {'spark_at_dispatch': 101, 'awaiting_verification_since': stale})
    goal.spark_spent = 141
    db.commit()

    class _Coordinator:
        def get_goal_progress(self, goal_id):
            return {'total_tasks': 1, 'completed': 0}

    with patch('integrations.agent_engine.dispatch._get_distributed_coordinator',
               return_value=_Coordinator()):
        ad._settle_dispatched_goal(db, goal, 'l1')
    assert goal.status == 'paused'
    assert _record(goal)[:2] == (gm.PAUSE_CAUSE_LEDGER_GROUNDING, 'daemon')
    assert 'awaiting_verification_since' not in goal.config_json
    assert 'ledger never reported' in goal.config_json['pause_reason']


def test_the_budget_gates_pause_stamps_its_cause(db):
    from integrations.agent_engine.budget_gate import apply_budget_pause
    goal = _goal(db, 'b1')
    assert apply_budget_pause(goal, 'insufficient_budget (0 < 1)', db=db) is True
    assert goal.status == 'paused'
    assert _record(goal)[:2] == (gm.PAUSE_CAUSE_BUDGET, 'daemon')
    assert 'budget gate blocked' in goal.config_json['pause_reason']
    assert apply_budget_pause(goal, 'again', db=db) is False       # already paused: no second pause


def test_escalate_goals_hand_to_a_person_stamps_its_cause_and_keeps_the_record(db):
    goal = _goal(db, 'e1', {'pause_cause': 'dispatch_failures', 'paused_by': 'daemon',
                            'pause_reason': 'Auto-paused: 5 consecutive dispatch failures',
                            'paused_at': 'x', 'pause_signature': 's'}, status='paused')
    with patch.object(GoalManager, '_escalation_expert', return_value=None), \
            patch.object(GoalManager, '_tell_its_person', return_value=True):
        result = GoalManager.escalate_goal(db, 'e1', {
            'action_id': 3, 'action': 'google_search', 'reason': 'blocked', 'tried': ['local']})
    assert result.get('stage') == 'human'
    db.commit()
    db.expire_all()
    goal = db.get(AgentGoal, 'e1')
    cfg = goal.config_json
    assert goal.status == 'paused'
    assert cfg['pause_cause'] == gm.PAUSE_CAUSE_NEEDS_HELP and cfg['paused_by'] == 'daemon'
    assert cfg['pause_reason'].startswith('Needs help: action 3')
    assert cfg['escalation']['next'] == 'human' and cfg['escalation']['told_at']
    assert 'pause_signature' not in cfg


def test_the_dashboards_pause_verb_stamps_the_person(db):
    from integrations.social.dashboard_service import _write_goal_status
    goal = _goal(db, 'p1')
    _write_goal_status(db, goal, 'paused', actor_id='owner')
    assert _record(goal)[:2] == (gm.PAUSE_CAUSE_PERSON, 'person:owner')


def test_update_goal_status_paused_to_active_goes_through_resume_goal(db):
    goal = _goal(db, 'u1', {'pause_cause': 'person', 'paused_by': 'person:a',
                            'pause_reason': 'r', 'paused_at': 'x'}, status='paused')
    with patch.object(GoalManager, 'resume_goal', wraps=GoalManager.resume_goal) as resume:
        result = GoalManager.update_goal_status(db, 'u1', 'active')
    assert result['success'] and resume.called
    cfg = goal.config_json
    assert goal.status == 'active'
    assert cfg['last_pause_cause'] == 'person' and 'pause_cause' not in cfg
    assert cfg['resume_count'] == 1 and cfg['resumed_by'].startswith('status_writer')
    # The ordinary writes are untouched.
    assert GoalManager.update_goal_status(db, 'u1', 'completed')['success']
    assert goal.status == 'completed'


def test_an_escalation_renewal_keeps_a_persons_pause(db):
    """Review of fbc54ddd6, minor: a hive worker re-asking for help on a goal
    its person paused from the dashboard (#194) restamped the pause as the
    daemon's.  The ask is recorded; the person's cause, name and words
    stand."""
    goal = _goal(db, 'pp1', {'pause_cause': gm.PAUSE_CAUSE_PERSON, 'paused_by': 'person:owner',
                             'pause_reason': 'Paused by its person from the dashboard',
                             'paused_at': 'x'}, status='paused')
    with patch.object(GoalManager, '_escalation_expert', return_value=None), \
            patch.object(GoalManager, '_tell_its_person', return_value=True):
        result = GoalManager.escalate_goal(db, 'pp1', {
            'action_id': 4, 'action': 'post', 'reason': 'needs a login', 'tried': ['local']})
    assert result.get('stage') == 'human'
    cfg = goal.config_json
    assert goal.status == 'paused'
    assert (cfg['pause_cause'], cfg['paused_by']) == (gm.PAUSE_CAUSE_PERSON, 'person:owner')
    assert cfg['pause_reason'] == 'Paused by its person from the dashboard'
    assert cfg['escalation']['next'] == 'human' and cfg['escalation']['action_id'] == 4
    assert cfg['escalation']['told_at']


def test_the_consent_gates_park_stamps_its_cause(db, monkeypatch):
    """The seventh writer (review of fbc54ddd6): hive_guardrails parks a
    consent-flagged goal on a node with nobody to ask through the one
    routine, with its own cause, and leaves an already-paused goal alone."""
    import contextlib
    from security import hive_guardrails as hg
    from integrations.social import models as social_models
    goal = _goal(db, 'c1', {'require_consent': True})

    @contextlib.contextmanager
    def db_session(commit=False):
        yield db
    monkeypatch.setattr(social_models, 'db_session', db_session)
    assert hg._park_goal_for_no_owner({'id': 'c1'}) is True
    assert goal.status == 'paused'
    assert _record(goal)[:2] == (gm.PAUSE_CAUSE_NO_OWNER, 'consent_gate')
    assert 'no owner to ask' in goal.config_json['pause_reason']
    assert goal.config_json['require_consent'] is True
    assert hg._park_goal_for_no_owner({'id': 'c1'}) is False
    assert _record(goal)[:2] == (gm.PAUSE_CAUSE_NO_OWNER, 'consent_gate')
