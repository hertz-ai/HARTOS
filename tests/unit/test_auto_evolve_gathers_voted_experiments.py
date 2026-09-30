"""auto-evolve considers the experiments that HAVE votes, however old.

MEASURED 2026-09-23 on a copy of the live node's database: 807 experiments,
all 'evaluating', 4 votes ever. _gather_candidates took the NEWEST 50 by
created_at, so the three human-voted experiments -- at rank ~693 -- were
never even looked at, and every cycle ranked 49 zero-vote rows and ended
in auto_evolve.none_approved (live: 08:23, 08:40, 08:55).

A zero-vote experiment can never pass the VOTE gate (weighted_score 0,
super-majority 0), so gathering it only spends a tally query to reject it.
The gather now asks the question the gate asks -- which experiments have
votes -- through ThoughtExperimentService.get_active_experiments(
with_votes_only=True). The gate itself (score >= min, 2/3 super-majority,
fail closed) is untouched: this changes what is LOOKED AT, never what is
approved.

Real in-memory SQLite, real service, real orchestrator; only db_session is
pointed at the test database.
"""
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')

from integrations.social.models import (  # noqa: E402
    Base, ExperimentVote, ThoughtExperiment, User)


@pytest.fixture
def db():
    eng = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _exp(db, creator, created_at, evaluated=False):
    e = ThoughtExperiment(
        id=str(uuid.uuid4()), creator_id=creator.id,
        title=f'exp {uuid.uuid4().hex[:6]}', hypothesis='h',
        expected_outcome='o', status='evaluating', created_at=created_at)
    if evaluated:
        e.agent_evaluations_json = [{'score': 0.9}]
    db.add(e)
    db.flush()
    return e


def _vote(db, exp, value=1):
    db.add(ExperimentVote(
        id=str(uuid.uuid4()), experiment_id=exp.id,
        voter_id=f'v-{uuid.uuid4().hex[:6]}', voter_type='human',
        vote_value=value))
    db.flush()


@pytest.fixture
def backlog(db):
    """The live shape: many newer unvoted rows, the voted one old."""
    u = User(username=f'u_{uuid.uuid4().hex[:8]}', user_type='human')
    db.add(u)
    db.flush()
    now = datetime.utcnow()
    old_voted = _exp(db, u, now - timedelta(days=150))
    _vote(db, old_voted)
    evaluated_voted = _exp(db, u, now - timedelta(days=30), evaluated=True)
    _vote(db, evaluated_voted)
    for i in range(60):                          # newer than both, no votes
        _exp(db, u, now - timedelta(minutes=i))
    db.commit()
    return old_voted, evaluated_voted


def _gather(db):
    from integrations.agent_engine.auto_evolve import (
        AutoEvolveOrchestrator, EvolveSession)

    @contextmanager
    def _session(commit=False):
        yield db

    with patch('integrations.social.models.db_session', _session):
        return AutoEvolveOrchestrator()._gather_candidates(
            EvolveSession(), ['voting', 'evaluating'])


def test_an_old_voted_experiment_is_gathered(db, backlog):
    old_voted, _ = backlog
    ids = [c['id'] for c in _gather(db)]
    assert old_voted.id in ids, (
        'the only voted experiment was older than the newest 50 and never '
        'considered -- every cycle ranked zero-vote rows and approved nothing')


def test_unvoted_rows_are_not_gathered(db, backlog):
    """They cannot pass the gate; gathering them only costs tally queries."""
    old_voted, _ = backlog
    assert [c['id'] for c in _gather(db)] == [old_voted.id]


def test_an_evaluated_experiment_is_still_not_redispatched(db, backlog):
    """Preservation: the existing rule (one evaluation goal per experiment)
    still holds for voted rows."""
    _, evaluated_voted = backlog
    assert evaluated_voted.id not in [c['id'] for c in _gather(db)]


def test_the_listing_order_is_unchanged_for_other_callers(db, backlog):
    """The API/UI listing keeps newest-first and still shows unvoted rows."""
    from integrations.social.thought_experiment_service import (
        ThoughtExperimentService)
    rows = ThoughtExperimentService.get_active_experiments(
        db, status='evaluating', limit=5)
    created = [r['created_at'] for r in rows]
    assert len(rows) == 5 and created == sorted(created, reverse=True)
