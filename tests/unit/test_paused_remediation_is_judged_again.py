"""A remediation goal paused because its loophole was still there is judged
again by the daemon's scan (review of 6047bcf19, finding 1).

6047bcf19 made the settlement gate pause, not complete, a remediation goal
whose loophole is still reported: central had completed 269 'Remediate
Single Node' goals by 2026-11-02 while it stayed one node.  Nothing looked
at a paused one again.  A paused goal blocks the re-seed of its type
(auto_remediate_loopholes skips active and paused goals), so a loophole
that later went away left its goal paused for good, and one that stayed was
never put to anyone.

The scan now completes such a goal once its loophole is gone and the checks
that report it ran; otherwise resumes it once per cooldown, up to a cap,
while the loophole is still reported; never resumes it while the loophole
cannot be measured; then tells its person in plain words.  Driven as the
daemon's scan tick drives it (recheck_paused_remediations, then
auto_remediate_loopholes with the same health) on real goal rows; the
person's notification is the boundary.

Review of 3bb05b2e8 added: a re-check that fails must not wipe the tick
(M1), a kept "gone" never completes a goal (M2), an unmeasured loophole is
not resumed (M3).
"""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from sqlalchemy import Column, String, create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from integrations.agent_engine import goal_seeding  # noqa: E402
from integrations.agent_engine.goal_manager import GoalManager  # noqa: E402
from integrations.social.models import AgentGoal, Base, Product, User  # noqa: E402

# The tenant filter can add tenant_id to the shared metadata at runtime; with
# the column already there create_all is the same in either order (the guard
# test_agent_engine.py carries, for the same reason).
for _cls in (User, Product, AgentGoal):
    if 'tenant_id' not in _cls.__table__.c:
        _cls.__table__.append_column(
            Column('tenant_id', String(64), nullable=True, index=True))

HEALTH = 'integrations.agent_engine.ip_service.IPService.get_loop_health'
_CAP = getattr(goal_seeding, 'REMEDIATION_RESUME_CAP', 3)
OPEN = 'remediation_open'


@pytest.fixture
def db():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def _no_health_kept_between_tests(monkeypatch):
    """The settle may read the scan's last result; no test inherits one."""
    monkeypatch.setattr(goal_seeding, '_loop_health_seen',
                        {'at': None, 'health': None}, raising=False)


def _paused(db, remediation='single_node', paused_minutes_ago=90, **extra):
    cfg = {'remediation': remediation, 'pause_cause': OPEN, 'paused_by': 'daemon',
           'pause_reason': f"Paused: its remediation ran, but the loophole "
                           f"'{remediation}' is still reported.",
           'paused_at': (datetime.utcnow()
                         - timedelta(minutes=paused_minutes_ago)).isoformat()}
    cfg.update(extra)
    goal = AgentGoal(goal_type='marketing', title=f'Remediate {remediation}',
                     description='', config_json=cfg, status='paused',
                     created_by='auto_remediation', spark_budget=200,
                     created_at=datetime.utcnow() - timedelta(days=1))
    db.add(goal)
    db.flush()
    return goal


def _health(*types, severity='high', failed=()):
    return {'flywheel_loopholes': [
                {'type': t, 'severity': severity, 'message': f'{t} still here'}
                for t in types],
            'failed_checks': list(failed)}


def _scan(db, health):
    """One scan tick, as agent_daemon._tick runs it: the re-check reads the
    loop health, and the seeding scan gets what it read."""
    with patch(HEALTH, return_value=health):
        read = goal_seeding.recheck_paused_remediations(db)
        return goal_seeding.auto_remediate_loopholes(db, health=read)


def test_a_loophole_that_has_gone_completes_its_paused_goal(db):
    goal = _paused(db)
    _scan(db, _health())                      # nothing reported, every check ran
    db.refresh(goal)
    assert goal.status == 'completed'
    cfg = goal.config_json
    assert cfg['completion_grounding'] == 'loophole_gone'
    assert 'completed_at' in cfg
    assert 'pause_cause' not in cfg and cfg['last_pause_cause'] == OPEN


def test_a_loophole_its_checks_could_not_measure_keeps_the_goal_paused(db):
    goal = _paused(db, paused_minutes_ago=5)
    _scan(db, _health(failed=['hivemind_agents']))
    db.refresh(goal)
    assert goal.status == 'paused'
    assert goal.config_json['pause_cause'] == OPEN


def test_a_check_that_cannot_report_the_type_does_not_hold_it(db):
    """recipe_drift is reported only by the recipe check; the hive-agent
    check failing says nothing about it."""
    goal = _paused(db, remediation='recipe_drift')
    _scan(db, _health(failed=['hivemind_agents']))
    db.refresh(goal)
    assert goal.status == 'completed'


def test_a_loophole_still_there_resumes_its_goal_once_the_cooldown_has_passed(db):
    recent = _paused(db, 'single_node', paused_minutes_ago=5)
    old = _paused(db, 'cold_start', paused_minutes_ago=61)
    _scan(db, _health('single_node', 'cold_start'))
    db.refresh(recent)
    db.refresh(old)
    assert recent.status == 'paused', 'resumed inside its cooldown'
    assert old.status == 'active'
    assert old.config_json['remediation_resume_count'] == 1
    assert old.config_json['resumed_by'] == 'daemon:remediation_recheck'


def test_after_the_cap_its_person_is_told_once_and_it_stays_paused(db):
    goal = _paused(db, paused_minutes_ago=600,
                   remediation_resume_count=_CAP)
    with patch.object(GoalManager, '_tell_its_person', return_value=True) as tell:
        _scan(db, _health('single_node'))
        _scan(db, _health('single_node'))
    db.refresh(goal)
    assert goal.status == 'paused'
    tell.assert_called_once()
    reason = tell.call_args.args[2]
    assert 'ran again and the problem it works on is still there' in reason
    assert 'single_node still here' in reason          # the check's own message
    assert 'loophole' not in reason and 'times' not in reason
    assert goal.config_json.get('told_at')


def test_nobody_to_tell_is_tried_again_only_after_the_cooldown(db):
    goal = _paused(db, paused_minutes_ago=600,
                   remediation_resume_count=_CAP)
    with patch.object(GoalManager, '_tell_its_person', return_value=False) as tell:
        _scan(db, _health('single_node'))
        _scan(db, _health('single_node'))
    assert tell.call_count == 1, 'a scan every five minutes re-tried the notice'
    db.refresh(goal)
    assert not goal.config_json.get('told_at')


def test_nobody_to_tell_is_tried_again_once_the_cooldown_has_passed(db):
    goal = _paused(db, paused_minutes_ago=600, remediation_resume_count=_CAP,
                   tell_tried_at=(datetime.utcnow()
                                  - timedelta(minutes=61)).isoformat())
    with patch.object(GoalManager, '_tell_its_person', return_value=True) as tell:
        _scan(db, _health('single_node'))
    tell.assert_called_once()
    db.refresh(goal)
    assert goal.config_json.get('told_at')


def test_a_loophole_that_cannot_be_measured_is_never_resumed(db):
    """Review of 3bb05b2e8, M3: with the hive-agent check unable to run
    (no world model answering), central resumed a paused single_node goal
    three times for marketing turns nothing could judge, then sent a push
    naming internal check ids."""
    goal = _paused(db, paused_minutes_ago=90)
    with patch.object(GoalManager, '_tell_its_person', return_value=True) as tell:
        _scan(db, _health(failed=['hivemind_agents']))
    db.refresh(goal)
    assert goal.status == 'paused'
    assert not goal.config_json.get('remediation_resume_count')
    tell.assert_called_once()
    reason = tell.call_args.args[2]
    assert 'cannot check whether the problem it works on is fixed' in reason
    for internal in ('hivemind_agents', 'not measured', 'loophole'):
        assert internal not in reason


def test_a_refused_resume_does_not_count_toward_the_cap(db):
    goal = _paused(db, 'cold_start', paused_minutes_ago=61)
    with patch.object(GoalManager, 'resume_goal',
                      return_value={'success': False, 'error': 'refused'}):
        _scan(db, _health('cold_start'))
    db.refresh(goal)
    assert goal.status == 'paused'
    assert not goal.config_json.get('remediation_resume_count')


def test_a_refused_completion_leaves_the_goal_as_it_was(db):
    goal = _paused(db)
    before = dict(goal.config_json)
    with patch.object(GoalManager, 'update_goal_status',
                      return_value={'success': False, 'error': 'refused'}):
        _scan(db, _health())
    db.flush()                  # what the session would write, read back
    db.refresh(goal)
    assert goal.status == 'paused'
    assert goal.config_json == before


def test_the_goal_volume_check_failing_does_not_hold_a_single_node_goal(db):
    """That check reports single_node only at medium, which never holds a
    goal, so its failing says nothing about the high report (review of
    3bb05b2e8)."""
    goal = _paused(db)
    _scan(db, _health(failed=['agent_performance']))
    db.refresh(goal)
    assert goal.status == 'completed'


def test_a_goal_paused_for_another_reason_is_left_alone(db):
    goal = _paused(db, pause_cause='person', paused_by='owner')
    _scan(db, _health())
    db.refresh(goal)
    assert goal.status == 'paused'
    assert goal.config_json['pause_cause'] == 'person'


class _SettleGoal:
    id = 'g1'
    goal_type = 'marketing'
    spark_spent = 4
    status = 'active'

    def __init__(self):
        self.config_json = {'remediation': 'single_node', 'spark_at_dispatch': 0}

    def to_dict(self):
        return {'id': self.id, 'status': self.status}


class _SettleDb:
    """refresh/flush seams for the gate; an empty table for the scan."""

    def refresh(self, goal, attribute_names=None):
        pass

    def flush(self):
        pass

    def query(self, model):
        q = MagicMock()
        q.filter.return_value.all.return_value = []
        return q


def test_a_kept_still_there_pauses_the_settle_with_no_new_check():
    """Review of 6047bcf19, finding 4: every settle ran every check again,
    up to 109 s with the HevolveAI address an unreachable host.  The scan's
    result, while fresh, is enough to PAUSE."""
    from integrations.agent_engine.agent_daemon import _settle_dispatched_goal
    goal = _SettleGoal()
    with patch(HEALTH, return_value=_health('single_node')) as health:
        goal_seeding.recheck_paused_remediations(_SettleDb())   # the scan keeps it
        _settle_dispatched_goal(_SettleDb(), goal, 'g1')
    assert health.call_count == 1, 'the settle ran every check again'
    assert goal.status == 'paused'


def test_a_kept_gone_is_checked_again_before_a_goal_completes():
    """Review of 3bb05b2e8, M2: a "gone" kept up to ten minutes completed
    a goal whose loophole was back."""
    from integrations.agent_engine.agent_daemon import _settle_dispatched_goal
    goal = _SettleGoal()
    with patch(HEALTH, side_effect=[_health(), _health('single_node')]) as health:
        goal_seeding.recheck_paused_remediations(_SettleDb())   # kept: gone
        _settle_dispatched_goal(_SettleDb(), goal, 'g1')        # now: back
    assert health.call_count == 2
    assert goal.status == 'paused', 'a kept "gone" completed the goal'


def test_a_fresh_gone_completes_the_goal():
    from integrations.agent_engine.agent_daemon import _settle_dispatched_goal
    goal = _SettleGoal()
    with patch(HEALTH, side_effect=[_health(), _health()]):
        goal_seeding.recheck_paused_remediations(_SettleDb())
        _settle_dispatched_goal(_SettleDb(), goal, 'g1')
    assert goal.status == 'completed'


def test_a_stale_result_is_not_used(monkeypatch):
    import time
    from integrations.agent_engine.agent_daemon import _settle_dispatched_goal
    fresh_s = getattr(goal_seeding, 'LOOP_HEALTH_FRESH_S', 600)
    monkeypatch.setattr(goal_seeding, '_loop_health_seen',
                        {'at': time.monotonic() - fresh_s - 1,
                         'health': _health()}, raising=False)
    goal = _SettleGoal()
    with patch(HEALTH, return_value=_health('single_node')) as health:
        _settle_dispatched_goal(_SettleDb(), goal, 'g1')
    assert health.call_count == 1
    assert goal.status == 'paused', 'an old "gone" completed the goal'


class _Bridge:
    """The world model bridge's read surface, answering as told."""

    def __init__(self, answering, agents=(), distributed=0):
        self.answering = answering
        self.agents = list(agents)
        self.distributed = distributed
        self.asked_for_agents = 0

    def check_health(self):
        return {'healthy': self.answering}

    def get_learning_stats(self):
        return {'learning': {}}

    def get_stats(self):
        return {'queue_size': 0, 'total_skills_distributed': self.distributed,
                'total_skills_blocked': 0}

    def get_hivemind_agents(self):
        self.asked_for_agents += 1
        return self.agents


def _loop_health_with(bridge):
    from integrations.agent_engine.ip_service import IPService

    def _no_db(*a, **k):
        raise RuntimeError('no database in this test')

    with patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge',
               return_value=bridge), \
            patch('integrations.social.models.get_db', side_effect=_no_db):
        return IPService.get_loop_health()


def test_a_world_model_that_does_not_answer_measures_no_agents_and_no_skills():
    """The root of the 269 seeds: with HevolveAI absent the bridge answers
    0 agents and 0 skills from its own defaults, and those read as a
    single_node and a cold_start loophole every hour."""
    bridge = _Bridge(answering=False)
    health = _loop_health_with(bridge)
    assert {'hivemind_agents', 'ralt_propagation'} <= set(health['failed_checks'])
    reported = {(l['type'], l['severity']) for l in health['flywheel_loopholes']}
    assert ('single_node', 'high') not in reported
    assert ('cold_start', 'critical') in reported, \
        'a world model that does not answer is the cold start itself'
    assert bridge.asked_for_agents == 0


def test_a_world_model_that_answers_still_reports_too_few_agents():
    health = _loop_health_with(_Bridge(answering=True, agents=['a1']))
    reported = {(l['type'], l['severity']) for l in health['flywheel_loopholes']}
    assert ('single_node', 'high') in reported
    assert ('cold_start', 'high') in reported          # 0 skills, measured
    assert 'hivemind_agents' not in health['failed_checks']
    assert 'ralt_propagation' not in health['failed_checks']


def test_a_node_whose_goals_are_all_paused_still_judges_them():
    """The scan's re-check runs before the tick's no-goals stop, as the
    daemon's own resume does: a node whose goals are all paused must still
    notice a loophole that has gone."""
    from integrations.agent_engine import agent_daemon

    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []   # no active goals
    daemon = agent_daemon.AgentDaemon()
    daemon._tick_count = daemon._remediate_every - 1                 # a scan tick
    with patch('integrations.social.models.get_db', return_value=db), \
            patch('integrations.agent_engine.dispatch.should_yield_to_user',
                  return_value=False), \
            patch.object(agent_daemon.AgentDaemon, '_settle_metered_usage'), \
            patch.object(agent_daemon.AgentDaemon, '_trends_tick'), \
            patch.object(goal_seeding, 'recheck_paused_remediations',
                         create=True) as recheck, \
            patch.object(goal_seeding, 'auto_remediate_loopholes') as seed:
        daemon._tick()
    recheck.assert_called_once()
    assert recheck.call_args.args[0] is db
    seed.assert_not_called()      # no active goals: nothing new is seeded


def test_a_recheck_that_fails_does_not_wipe_the_tick(db, monkeypatch, caplog):
    """Review of 3bb05b2e8, M1: the re-check's error was caught without a
    rollback, so every later query in the tick failed on the failed
    transaction and the tick's reset emptied _dispatch_backoff: a backed-off
    failing goal was dispatched again at once.  Real session; the re-check
    leaves it failed as a refused write does."""
    import logging
    from integrations.agent_engine import agent_daemon

    def _write_refused(session, health=None):
        for _ in range(2):          # the second insert of one id is refused
            session.add(AgentGoal(id='same-id', goal_type='marketing',
                                  title='t', description='', config_json={},
                                  status='paused', created_by='auto_remediation',
                                  spark_budget=1))
            session.flush()

    monkeypatch.setitem(agent_daemon._dispatch_backoff, 'goal-backing-off',
                        {'failures': 2})
    daemon = agent_daemon.AgentDaemon()
    daemon._tick_count = daemon._remediate_every - 1                 # a scan tick
    with patch('integrations.social.models.get_db', return_value=db), \
            patch('integrations.agent_engine.dispatch.should_yield_to_user',
                  return_value=False), \
            patch.object(agent_daemon.AgentDaemon, '_settle_metered_usage'), \
            patch.object(agent_daemon.AgentDaemon, '_trends_tick'), \
            patch.object(goal_seeding, 'recheck_paused_remediations',
                         _write_refused), \
            caplog.at_level(logging.WARNING):
        daemon._tick()
    assert 'Paused remediation re-check skipped' in caplog.text
    assert 'state reset' not in caplog.text
    assert agent_daemon._dispatch_backoff.get('goal-backing-off') == {'failures': 2}


def test_every_seeded_loophole_names_the_checks_that_judge_it():
    """A type with a remediation template and no LOOPHOLE_CHECKS entry
    (learning_stall) could be seeded and never judged."""
    from integrations.agent_engine.ip_service import LOOPHOLE_CHECKS
    assert set(goal_seeding.LOOPHOLE_REMEDIATION_MAP) <= set(LOOPHOLE_CHECKS)


def test_the_improvement_verdict_says_a_skill_count_was_not_measured():
    """With no world model answering, the RALT check does not run; its 0 is
    no count of skills."""
    from integrations.agent_engine.ip_service import IPService
    health = _loop_health_with(_Bridge(answering=False))
    with patch(HEALTH, return_value=health):
        verdict = IPService.verify_exponential_improvement(None)
    evidence = ' | '.join(verdict['evidence'])
    assert 'RALT propagation not measured' in evidence
    assert 'Only 0 RALT' not in evidence
