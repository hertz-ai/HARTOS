"""The harness proposes its own improvement: when ONE dispatch-failure
signature has paused three or more of this daemon's goals, the daemon files
ONE thought experiment about that shared cause, so the auto-evolve loop has
a real candidate from the harness itself.

Measured on central 2026-10-28 (owner /goal): every auto-evolve cycle for
weeks gathered 0 candidates (834 experiments, all the prover's August
duplicates, 0 votes, none created since 08-12) while 17 goals paused on the
SAME 400 ('Reasoning is mandatory for this endpoint') in one day.  The loop
had nothing to work on, and the one systemic defect on the node was written
17 times into pause_reason prose and proposed to nobody.

Pinned, against a real SQLite AgentGoal + ThoughtExperiment through the real
ThoughtExperimentService.create_experiment and the prover's
_open_experiment_exists (the existing proposer path, reused, not a second
one):
  * the pause site records the failure's signature (why[:80]) as a
    structured key beside pause_cause;
  * the third goal paused on one signature files one experiment titled
    'Harness: <signature>' as the 'nunba' system user; the first two file
    nothing; a fourth files no duplicate while the first is unresolved;
  * coding goals (coding_daemon's rows) do not count toward the three;
  * a different signature counts separately;
  * the proposer never raises: a refused or failing create leaves the pause
    exactly as written.
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from integrations.social.models import AgentGoal, Base, ThoughtExperiment  # noqa: E402
import integrations.agent_engine.agent_daemon as ad  # noqa: E402
from integrations.agent_engine.agent_daemon import (  # noqa: E402
    _auto_pause_for_dispatch_failures, _propose_harness_experiment_for_signature)

WHY = ("turn failed: I couldn't finish that: Error code: 400 - {'error': "
       "{'message': 'Reasoning is mandatory for this endpoint and cannot be disabled.'}}")


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


def _active(db, gid, goal_type='marketing'):
    db.add(AgentGoal(id=gid, goal_type=goal_type, title=gid, status='active',
                     config_json={}))
    db.commit()
    return db.get(AgentGoal, gid)


def _pause(db, goal, why=WHY):
    _auto_pause_for_dispatch_failures(db, goal, goal.id, 5, why)
    db.commit()


def _harness_experiments(db):
    return db.query(ThoughtExperiment).filter(
        ThoughtExperiment.title.like('Harness: %')).all()


def test_the_pause_site_records_the_failure_signature(db):
    g = _active(db, 'g1')
    _pause(db, g)
    cfg = db.get(AgentGoal, 'g1').config_json
    assert cfg['pause_signature'] == WHY[:80]
    assert cfg['pause_cause'] == ad.PAUSE_CAUSE_DISPATCH_FAILURES


def test_the_third_goal_on_one_signature_files_one_experiment_and_no_duplicate(db):
    for gid in ('g1', 'g2'):
        _pause(db, _active(db, gid))
    assert _harness_experiments(db) == []
    _pause(db, _active(db, 'g3'))
    exps = _harness_experiments(db)
    assert len(exps) == 1
    exp = exps[0]
    assert exp.title == 'Harness: ' + WHY[:80]
    assert exp.status == 'proposed'
    assert '3 goals' in exp.hypothesis and WHY[:80] in exp.hypothesis
    assert exp.creator_id  # the 'nunba' system user, made by the service
    _pause(db, _active(db, 'g4'))
    assert len(_harness_experiments(db)) == 1


def test_coding_goals_do_not_count_toward_the_three(db):
    _pause(db, _active(db, 'c1', goal_type='coding'))
    _pause(db, _active(db, 'c2', goal_type='coding'))
    _pause(db, _active(db, 'g1'))
    assert _harness_experiments(db) == []


def test_a_different_signature_counts_separately(db):
    _pause(db, _active(db, 'g1'))
    _pause(db, _active(db, 'g2'))
    _pause(db, _active(db, 'h1'), why='turn failed: 429 rate limit')
    assert _harness_experiments(db) == []
    _pause(db, _active(db, 'g3'))
    assert [e.title for e in _harness_experiments(db)] == ['Harness: ' + WHY[:80]]


def test_a_refused_or_failing_create_leaves_the_pause_as_written(db):
    _pause(db, _active(db, 'g1'))
    _pause(db, _active(db, 'g2'))
    with patch('integrations.social.thought_experiment_service.ThoughtExperimentService.'
               'create_experiment', return_value=None):
        g3 = _active(db, 'g3')
        _pause(db, g3)
    assert g3.status == 'paused' and g3.config_json['pause_cause'] == ad.PAUSE_CAUSE_DISPATCH_FAILURES
    assert _harness_experiments(db) == []
    with patch('integrations.social.thought_experiment_service.ThoughtExperimentService.'
               'create_experiment', side_effect=RuntimeError('db locked')):
        g4 = _active(db, 'g4')
        _pause(db, g4)
    assert g4.status == 'paused'
    # Called directly with no goals at all: nothing, no raise.
    assert _propose_harness_experiment_for_signature(db, 'never seen') is None
