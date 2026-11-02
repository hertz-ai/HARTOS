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
  * the pause site records the failure's signature (_failure_signature:
    stripped, cut to 80, stripped again) as a structured key beside
    pause_cause, and none for a failure with no reason; the proposer matches
    the stored key as written, so a cut on whitespace still matches;
  * the title and hypothesis pass the one egress scrub
    (security.edge_privacy.scrub_text) before they leave the goal's row;
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


def test_a_signature_cut_on_whitespace_or_led_by_it_still_matches(db):
    """Review of 3d4c5b3a3: the proposer stripped the signature but the
    stored one was why[:80] as written, so a cut landing on a space, or a
    reason led by whitespace, matched nothing and three identical pauses
    filed NO experiment, silently.  One normaliser for the pause that stores
    it and the proposer that matches it."""
    cut_on_space = 'x' * 79 + ' tail of the error text'
    for gid in ('w1', 'w2'):
        _pause(db, _active(db, gid), why=cut_on_space)
    _pause(db, _active(db, 'w3'), why='   ' + cut_on_space)
    assert [e.title for e in _harness_experiments(db)] == ['Harness: ' + 'x' * 79]
    assert db.get(AgentGoal, 'w3').config_json['pause_signature'] == 'x' * 79


def test_a_secret_in_the_failure_text_never_reaches_the_experiment(db):
    """Review of 3d4c5b3a3: the turn's raw error text became a public Post
    title and hypothesis with no egress scrub, so a 401 body echoing a key
    fragment would be published.  The one egress scrub
    (security.edge_privacy.scrub_text) runs before the text leaves the
    goal's own row; the stored signature is matched as written."""
    key = 'sk-proj-' + 'A' * 50
    for gid in ('k1', 'k2', 'k3'):
        _pause(db, _active(db, gid), why=f'turn failed: 401 for key {key}')
    [exp] = _harness_experiments(db)
    assert exp.title.startswith('Harness: turn failed: 401 for key ')
    assert 'sk-proj-' not in exp.title and 'AAAA' not in exp.title
    assert 'sk-proj-' not in (exp.hypothesis or '') and 'AAAA' not in (exp.hypothesis or '')


def test_no_signature_is_stored_for_a_failure_with_no_reason(db):
    """Minor from the review: an empty signature is no key at all."""
    _pause(db, _active(db, 'n1'), why=None)
    cfg = db.get(AgentGoal, 'n1').config_json
    assert 'pause_signature' not in cfg
    assert cfg['pause_cause'] == 'dispatch_failures'
