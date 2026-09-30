"""An evaluation goal starts only for an experiment the vote approved.

MEASURED LIVE 2026-09-25 (HARTOS 20bfca03d, AE-7): a non-admin user whose
POST /advance returns 403 ran POST /api/social/experiments/<id>/evaluate on
another user's experiment with ZERO votes and got 200 + a goal_id.  The row
went to 'evaluating' and an active agent_goals row appeared.  One identity
started an autonomous agent goal alone -- the thing the owner ruling of
2026-09-24 ("no single identity approves") forbids.

Root cause: the quorum + super-majority gate lived only in the auto-evolve
caller (AutoEvolveOrchestrator._rank_by_votes).  The single writer of the
evaluation goal, ThoughtExperimentService.request_agent_evaluation, checked
neither the vote nor the lifecycle, and set status='evaluating' from ANY
status (a second status writer beside advance_status, so 'decided' could go
backwards).  The fix puts the gate in that writer, via the one approval rule
in voting_rules, so every caller -- the REST route, auto-evolve, anything
later -- is gated.

Each check: {what, check, expected, tolerance 0 (exact invariant)}.
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')

from integrations.social.models import (  # noqa: E402
    AgentGoal, Base, ThoughtExperiment, User)
from integrations.social.thought_experiment_service import (  # noqa: E402
    ThoughtExperimentService)
from integrations.social.voting_rules import (  # noqa: E402
    MIN_DISTINCT_SUPPORTERS, MIN_DISTINCT_VOTERS)


@pytest.fixture
def db():
    eng = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _user(db):
    u = User(username=f'livetest_ae7_{uuid.uuid4().hex[:8]}', user_type='human')
    db.add(u)
    db.flush()
    return u


def _experiment(db, status='voting'):
    e = ThoughtExperiment(
        id=str(uuid.uuid4()), creator_id=_user(db).id, title='Cache warmup',
        hypothesis='Faster cache warmup lowers latency', expected_outcome='o',
        status=status)
    db.add(e)
    db.commit()
    return e


def _vote(db, exp_id, value):
    """A decisive human vote, written directly so the vote exists whatever
    the experiment's status (cast_vote refuses outside discussing/voting)."""
    from integrations.social.models import ExperimentVote
    db.add(ExperimentVote(
        experiment_id=exp_id, voter_id=_user(db).id, voter_type='human',
        vote_value=value, confidence=1.0))
    db.commit()


def _approve(db, exp_id):
    """The smallest vote that passes: MIN_DISTINCT_SUPPORTERS FOR and the
    rest of the quorum AGAINST -> 2 FOR / 1 AGAINST = exactly 2/3."""
    for v in [2] * MIN_DISTINCT_SUPPORTERS + [-1] * (
            MIN_DISTINCT_VOTERS - MIN_DISTINCT_SUPPORTERS):
        _vote(db, exp_id, v)


def _goals(db):
    return db.query(AgentGoal).count()


class TestTheWriterRefusesAnUnapprovedExperiment:
    def test_an_unvoted_experiment_starts_no_goal(self, db):
        """AE-7 exactly: zero votes."""
        e = _experiment(db)
        result = ThoughtExperimentService.request_agent_evaluation(db, e.id)
        db.commit()
        assert result['success'] is False
        assert result['reason'] == 'not_approved'
        assert _goals(db) == 0
        db.refresh(e)
        assert e.status == 'voting'

    def test_one_unanimous_voter_is_not_an_approval(self, db):
        e = _experiment(db)
        _vote(db, e.id, 2)
        result = ThoughtExperimentService.request_agent_evaluation(db, e.id)
        assert result['success'] is False
        assert _goals(db) == 0

    def test_a_quorum_without_the_super_majority_is_refused(self, db):
        """2 FOR / 2 AGAINST: quorum met (4 voters, 2 supporters), ratio 0.5."""
        e = _experiment(db)
        for v in (2, 2, -2, -2):
            _vote(db, e.id, v)
        assert ThoughtExperimentService.tally_votes(db, e.id)['quorum_met'] is True
        result = ThoughtExperimentService.request_agent_evaluation(db, e.id)
        assert result['success'] is False
        assert _goals(db) == 0

    def test_an_approved_experiment_starts_its_goal(self, db):
        """Control: the gate is not a blanket refusal."""
        e = _experiment(db)
        _approve(db, e.id)
        result = ThoughtExperimentService.request_agent_evaluation(db, e.id)
        db.commit()
        assert result['success'] is True and result['goal_id']
        assert _goals(db) == 1
        db.refresh(e)
        assert e.status == 'evaluating'

    @pytest.mark.parametrize('ended', ['decided', 'archived'])
    def test_a_closed_experiment_does_not_go_backwards(self, db, ended):
        """Approved votes, but the lifecycle is over: advance_status never
        moves backwards, and neither may the evaluation writer."""
        e = _experiment(db, status=ended)
        _approve(db, e.id)
        result = ThoughtExperimentService.request_agent_evaluation(db, e.id)
        db.commit()
        assert result['success'] is False
        assert _goals(db) == 0
        db.refresh(e)
        assert e.status == ended


class TestTheRestRouteIsGatedByTheWriter:
    """The live repro, through the real route: an authenticated non-admin."""

    @pytest.fixture
    def client(self, db, monkeypatch):
        import importlib

        import flask
        from flask import Flask

        from integrations.social import auth as auth_mod

        class _NonAdmin:
            id = 'livetest_autoevolve_q'
            is_admin = False
            is_moderator = False
            role = 'flat'

        def _passthrough(fn):
            from functools import wraps

            @wraps(fn)
            def wrapper(*a, **k):
                flask.g.user = _NonAdmin()
                flask.g.user_id = _NonAdmin.id
                return fn(*a, **k)
            return wrapper

        import integrations.social.api_thought_experiments as te_mod
        monkeypatch.setattr(auth_mod, 'require_auth', _passthrough)
        importlib.reload(te_mod)
        monkeypatch.setattr('integrations.social.models.get_db', lambda: db)
        monkeypatch.setattr(db, 'close', lambda: None)
        app = Flask(__name__)
        app.config['TESTING'] = True
        app.register_blueprint(te_mod.thought_experiments_bp)
        yield app.test_client()
        monkeypatch.undo()
        importlib.reload(te_mod)

    def test_non_admin_evaluate_on_an_unvoted_experiment_is_refused(
            self, db, client):
        e = _experiment(db)
        resp = client.post(f'/api/social/experiments/{e.id}/evaluate')
        body = resp.get_json()
        assert resp.status_code == 400
        assert body['success'] is False and body['reason'] == 'not_approved'
        assert _goals(db) == 0
        db.refresh(e)
        assert e.status == 'voting'
