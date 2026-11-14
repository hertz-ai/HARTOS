"""This node's own experiments evolve here without a vote.

OWNER RULING 2026-11-14 (verbatim): "voting shd not be a prerequisite to
evolve locally untill we cross the intelligence barrier".

MEASURED the same day on the owner's desktop (read-only queries of
~/Documents/Nunba/data/hevolve_database.db and frozen_debug.log): 812
thought experiments, all 'evaluating', 9 votes ever cast.  505 of them were
created by the node's own 'nunba' system account.  Only one experiment -- a
September test probe -- passed the vote, so every auto-evolve cycle
(15:55, 20:21, 21:22, 22:01, 22:38 IST) re-dispatched that one probe and
nothing else ever ran.

The rule now (voting_rules.may_evaluate_here, asked by both auto-evolve's
ranking and the evaluation-goal writer):
  - this node's OWN experiment -- its creator counts as this node's
    identity (the node's owner, an agent the owner owns, or a machine
    account no person owns) -- evaluates here with no vote;
  - anyone else's unvoted experiment is still refused (AE-7, 2026-09-25);
  - a steward-required context (security_guardrail) still needs the vote.

Real SQLite file, the real service, the real orchestrator; only db_session
is pointed at the test database.
Each check: {what, check, expected, tolerance 0 (exact invariant)}.
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
    AgentGoal, Base, ExperimentVote, ThoughtExperiment, User)
from integrations.social.thought_experiment_service import (  # noqa: E402
    ThoughtExperimentService)

DEFAULT = ('Cache warmup', 'Faster cache warmup lowers latency')
SECURITY = ('Tighten the security guardrail',
            'A stricter guardrail blocks the vulnerability')


@pytest.fixture
def db(tmp_path, monkeypatch):
    # A file, not :memory: -- the cycle dispatches on a worker thread, and
    # an in-memory SQLite database is a different, empty one per thread.
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
    eng = create_engine(f"sqlite:///{tmp_path / 'evolve.db'}",
                        connect_args={'check_same_thread': False})
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


def _user(db, user_type='human', owner_id=None, username=None):
    u = User(username=username or f'livetest_local_{uuid.uuid4().hex[:8]}',
             user_type=user_type, owner_id=owner_id)
    db.add(u)
    db.flush()
    return u


def _system(db):
    """The node's own machine account, as request_agent_evaluation and the
    benchmark prover create it (UserService.ensure_system_user)."""
    return _user(db, user_type='system', username='nunba')


def _experiment(db, creator_id, status='voting', title_hypothesis=DEFAULT,
                created_at=None):
    title, hypothesis = title_hypothesis
    e = ThoughtExperiment(
        id=str(uuid.uuid4()), creator_id=creator_id, title=title,
        hypothesis=hypothesis, expected_outcome='o', status=status,
        created_at=created_at or datetime.utcnow())
    db.add(e)
    db.commit()
    return e


def _approve(db, exp_id):
    """Three distinct people FOR: quorum met, ratio 1.0."""
    for _ in range(3):
        db.add(ExperimentVote(
            experiment_id=exp_id, voter_id=_user(db).id, voter_type='human',
            vote_value=2, confidence=1.0))
    db.commit()


def _evaluate(db, e):
    result = ThoughtExperimentService.request_agent_evaluation(db, e.id)
    db.commit()
    return result, db.query(AgentGoal).count()


@contextmanager
def _pointed_at(db):
    @contextmanager
    def _session(commit=False):
        yield db
    with patch('integrations.social.models.db_session', _session):
        yield


# ── the evaluation-goal writer (every caller goes through it) ───────────

class TestTheWriter:
    def test_the_nodes_system_account_experiment_starts_without_a_vote(self, db):
        """RED on HEAD (not_approved): the 505 'nunba' rows never ran."""
        e = _experiment(db, _system(db).id)
        result, goals = _evaluate(db, e)
        assert result['success'] is True and result['goal_id']
        assert result['approved_by_vote'] is False
        assert goals == 1
        db.refresh(e)
        assert e.status == 'evaluating'

    def test_the_owners_own_experiment_starts_without_a_vote(self, db, monkeypatch):
        owner = _user(db)
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', owner.id)
        result, goals = _evaluate(db, _experiment(db, owner.id))
        assert result['success'] is True
        assert goals == 1

    def test_an_agent_the_owner_owns_counts_as_the_node(self, db, monkeypatch):
        owner = _user(db)
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', owner.id)
        agent = _user(db, user_type='agent', owner_id=owner.id)
        result, goals = _evaluate(db, _experiment(db, agent.id))
        assert result['success'] is True
        assert goals == 1

    def test_another_persons_unvoted_experiment_is_still_refused(self, db, monkeypatch):
        """Preservation of AE-7: GREEN on both trees."""
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', _user(db).id)
        e = _experiment(db, _user(db).id)
        result, goals = _evaluate(db, e)
        assert result['success'] is False
        assert result['reason'] == 'not_approved'
        assert goals == 0
        db.refresh(e)
        assert e.status == 'voting'

    def test_an_agent_another_person_owns_is_not_the_node(self, db, monkeypatch):
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', _user(db).id)
        stranger = _user(db)
        agent = _user(db, user_type='agent', owner_id=stranger.id)
        result, goals = _evaluate(db, _experiment(db, agent.id))
        assert result['success'] is False and result['reason'] == 'not_approved'
        assert goals == 0

    def test_a_creator_with_no_account_here_is_not_the_node(self, db):
        """A made-up creator id must not be a free pass."""
        result, goals = _evaluate(db, _experiment(db, 'no-such-account'))
        assert result['success'] is False and result['reason'] == 'not_approved'
        assert goals == 0

    def test_the_nodes_security_experiment_still_needs_the_vote(self, db):
        """security_guardrail requires the steward: changes to security
        stay with people even on this node."""
        e = _experiment(db, _system(db).id, title_hypothesis=SECURITY)
        assert ThoughtExperimentService.tally_votes(
            db, e.id)['decision_context'] == 'security_guardrail'
        result, goals = _evaluate(db, e)
        assert result['success'] is False and result['reason'] == 'not_approved'
        assert goals == 0

    @pytest.mark.parametrize('ended', ['decided', 'archived'])
    def test_the_nodes_closed_experiment_does_not_go_backwards(self, db, ended):
        """RED on HEAD by reason: HEAD refused it as not_approved before
        ever reaching the lifecycle guard."""
        e = _experiment(db, _system(db).id, status=ended)
        result, goals = _evaluate(db, e)
        assert result['success'] is False
        assert result['reason'] == 'invalid_status'
        assert goals == 0
        db.refresh(e)
        assert e.status == ended


# ── the auto-evolve cycle ───────────────────────────────────────────────

class TestTheCycle:
    def _orch(self):
        from integrations.agent_engine.auto_evolve import (
            AutoEvolveOrchestrator, EvolveSession)
        return AutoEvolveOrchestrator(), EvolveSession(status='selecting')

    def test_the_nodes_own_unvoted_experiment_is_gathered_behind_newer_rows(self, db):
        """RED on HEAD: only voted rows were gathered."""
        now = datetime.utcnow()
        own = _experiment(db, _system(db).id, status='evaluating',
                          created_at=now - timedelta(days=150))
        stranger = _user(db)
        for i in range(60):
            _experiment(db, stranger.id, status='evaluating',
                        created_at=now - timedelta(minutes=i))
        orch, session = self._orch()
        with _pointed_at(db):
            ids = [c['id'] for c in orch._gather_candidates(
                session, ['voting', 'evaluating'])]
        assert own.id in ids
        assert len(ids) == 1, 'a stranger\'s unvoted rows are still not gathered'

    def test_vote_approved_rank_first_then_the_nodes_own_newest_first(self, db):
        """RED on HEAD: the two own rows were rejected by the vote gate."""
        now = datetime.utcnow()
        system = _system(db)
        old_own = _experiment(db, system.id, status='evaluating',
                              created_at=now - timedelta(days=3))
        new_own = _experiment(db, system.id, status='evaluating',
                              created_at=now - timedelta(days=1))
        approved = _experiment(db, _user(db).id, status='evaluating',
                               created_at=now - timedelta(days=9))
        _approve(db, approved.id)
        orch, session = self._orch()
        with _pointed_at(db), patch.object(
                type(orch), '_active_learning_multiplier', return_value=1.0):
            ranked = orch._rank_by_votes(
                session, orch._gather_candidates(session, ['evaluating']), 0.3)
        assert [e['id'] for e in ranked] == [approved.id, new_own.id, old_own.id]
        assert [e['_approved_by_vote'] for e in ranked] == [True, False, False]
        assert [e['_this_nodes_own'] for e in ranked] == [False, True, True]

    def test_a_full_cycle_dispatches_the_nodes_own_experiment(self, db):
        """RED on HEAD: the cycle ended 'No eligible experiments found'."""
        e = _experiment(db, _system(db).id, status='evaluating')
        orch, session = self._orch()
        with _pointed_at(db), patch.object(
                type(orch), '_active_learning_multiplier', return_value=1.0), \
                patch.object(type(orch), '_emit_event'):
            orch._execute_cycle(session, 5, 0.3, ['voting', 'evaluating'],
                                'system')
        assert session.dispatched == 1, session.errors
        assert session.status == 'running'
        item = session.experiments[0]
        assert item['id'] == e.id
        assert item['this_nodes_own'] is True
        assert item['approved_by_vote'] is False
        goal = db.query(AgentGoal).filter(AgentGoal.id == item['goal_id']).one()
        assert (goal.config_json or {}).get('experiment_id') == e.id


# ── the one rule ────────────────────────────────────────────────────────

@pytest.mark.parametrize('approved, steward_required, own, expected', [
    (True, False, False, True),    # the vote approved: runs, as before
    (True, True, False, True),     # approved with its steward: runs
    (False, False, True, True),    # this node's own: no vote needed
    (False, True, True, False),    # own, but security: still needs the vote
    (False, False, False, False),  # someone else's, unvoted: refused
])
def test_may_evaluate_here(approved, steward_required, own, expected):
    from integrations.social.voting_rules import may_evaluate_here
    verdict = {'approved': approved, 'steward_required': steward_required}
    assert may_evaluate_here(verdict, own) is expected
