"""No single identity can approve a thought experiment, and no identity counts twice.

Owner principle (relayed by cc12, 2026-09-24): "whoever controls the capital
flow controls the world ... people should not worry about ever having to worry
about one person monopolising AI ever."  For the vote this means a minimum
quorum of DISTINCT voters, checkable as:
  - no experiment is approved with fewer than MIN_DISTINCT_VOTERS decisive
    identities, and
  - no experiment is approved with fewer than MIN_DISTINCT_SUPPORTERS
    identities voting FOR (so one identity never approves alone), and
  - no identity counts twice: an agent counts as the human who owns it.

Measured before (live DB, 2026-09-24, read-only): every one of the 4 voted
experiments had exactly ONE voter, and all 4 had passed the gate into
'evaluating'.  The 2/3 ratio alone cannot stop that: 1 FOR / 0 AGAINST is a
ratio of 1.0.  Weighting cannot stop it either: one human FOR (1.0) against two
confidence-0.2 agents AGAINST gives 1.0 / 1.4 = 0.71 >= 2/3 with one supporter.
"""
import os
import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')

from integrations.social.models import Base, User
from integrations.social.thought_experiment_service import ThoughtExperimentService
from integrations.social.voting_rules import (
    MIN_DISTINCT_SUPPORTERS, MIN_DISTINCT_VOTERS)


@pytest.fixture(scope='module')
def engine():
    eng = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def db(engine):
    session = sessionmaker(bind=engine)()
    yield session
    session.rollback()
    session.close()


def _user(db, user_type='human', owner_id=None):
    u = User(username=f'u_{uuid.uuid4().hex[:10]}', user_type=user_type,
             owner_id=owner_id)
    db.add(u)
    db.flush()
    return u


def _voting_experiment(db):
    creator = _user(db)
    exp = ThoughtExperimentService.create_experiment(
        db, creator.id, 'Quorum probe', 'Faster cache warmup lowers latency')
    ThoughtExperimentService.advance_status(db, exp['id'], target_status='voting')
    return exp['id']


def _vote(db, exp_id, voter, value, confidence=1.0):
    ThoughtExperimentService.cast_vote(
        db, exp_id, voter.id, value, voter_type=voter.user_type,
        confidence=confidence)


class TestTheQuorumIsCountedByIdentity:
    def test_the_constants_encode_not_alone(self):
        """The invariant the principle needs, stated as numbers."""
        assert MIN_DISTINCT_SUPPORTERS >= 2
        assert MIN_DISTINCT_VOTERS >= MIN_DISTINCT_SUPPORTERS

    def test_one_voter_is_no_quorum(self, db):
        exp_id = _voting_experiment(db)
        _vote(db, exp_id, _user(db), 2)
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        assert (tally['distinct_voters'], tally['distinct_supporters']) == (1, 1)
        assert tally['quorum_met'] is False
        assert tally['decision_recommendation'] == 'no_quorum'

    def test_enough_distinct_humans_meet_it(self, db):
        exp_id = _voting_experiment(db)
        for value in [2] * MIN_DISTINCT_SUPPORTERS + [-1] * (
                MIN_DISTINCT_VOTERS - MIN_DISTINCT_SUPPORTERS):
            _vote(db, exp_id, _user(db), value)
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        assert tally['distinct_voters'] == MIN_DISTINCT_VOTERS
        assert tally['quorum_met'] is True

    def test_agents_count_as_their_owner(self, db):
        """One person with many agents is one identity."""
        exp_id = _voting_experiment(db)
        owner = _user(db)
        _vote(db, exp_id, owner, 2)
        for _ in range(MIN_DISTINCT_VOTERS + 2):
            _vote(db, exp_id, _user(db, 'agent', owner_id=owner.id), 2)
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        assert tally['total_votes'] == MIN_DISTINCT_VOTERS + 3
        assert tally['distinct_voters'] == 1
        assert tally['quorum_met'] is False

    def test_an_unowned_agent_is_its_own_identity(self, db):
        """Agents may vote (owner ruling 2026-09-23); an agent with no owner
        is a distinct identity like any registered user."""
        exp_id = _voting_experiment(db)
        for _ in range(MIN_DISTINCT_VOTERS):
            _vote(db, exp_id, _user(db, 'agent'), 2, confidence=0.9)
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        assert tally['distinct_voters'] == MIN_DISTINCT_VOTERS
        assert tally['quorum_met'] is True

    def test_an_unregistered_voter_id_does_not_count(self, db):
        """The live 'toolsweep' vote has no users row: a string anyone can
        pass.  Its weight still counts in the tally; it is not an identity."""
        exp_id = _voting_experiment(db)
        for i in range(MIN_DISTINCT_VOTERS):
            ThoughtExperimentService.cast_vote(
                db, exp_id, f'ghost_{i}_{uuid.uuid4().hex[:6]}', 2,
                voter_type='agent', confidence=0.9)
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        assert tally['total_votes'] == MIN_DISTINCT_VOTERS
        assert tally['distinct_voters'] == 0
        assert tally['quorum_met'] is False

    def test_a_zero_weight_or_abstain_vote_is_not_decisive(self, db):
        exp_id = _voting_experiment(db)
        _vote(db, exp_id, _user(db), 2)
        _vote(db, exp_id, _user(db), 0)
        _vote(db, exp_id, _user(db, 'agent'), 2, confidence=0.0)
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        assert tally['distinct_voters'] == 1

    def test_one_supporter_is_not_enough_even_when_the_ratio_passes(self, db):
        """The weighted case the 2/3 ratio misses: 1.0 / 1.4 = 0.71."""
        exp_id = _voting_experiment(db)
        _vote(db, exp_id, _user(db), 2)
        for _ in range(max(2, MIN_DISTINCT_VOTERS - 1)):
            _vote(db, exp_id, _user(db, 'agent'), -1, confidence=0.2)
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        decisive = tally['total_for'] + tally['total_against']
        assert tally['total_for'] / decisive >= 2 / 3
        assert tally['distinct_supporters'] == 1
        assert tally['quorum_met'] is False


class TestAutoEvolveRequiresTheQuorum:
    def _rank(self, tally):
        from integrations.agent_engine.auto_evolve import (
            AutoEvolveOrchestrator, EvolveSession)
        context = MagicMock()
        context.__enter__.return_value = MagicMock()
        context.__exit__.return_value = False
        with patch('integrations.social.models.db_session',
                   return_value=context), \
                patch('integrations.social.thought_experiment_service.'
                      'ThoughtExperimentService.tally_votes',
                      return_value=tally):
            return AutoEvolveOrchestrator()._rank_by_votes(
                EvolveSession(), [{'id': 'e'}], 0.3)

    def test_a_unanimous_single_voter_is_not_dispatched(self):
        assert self._rank({'weighted_score': 2.0, 'total_for': 1.0,
                           'total_against': 0.0, 'quorum_met': False}) == []

    def test_a_tally_without_the_answer_fails_closed(self):
        assert self._rank({'weighted_score': 2.0, 'total_for': 1.0,
                           'total_against': 0.0}) == []

    def test_a_quorate_supermajority_is_dispatched(self):
        ranked = self._rank({'weighted_score': 1.0, 'total_for': 3.0,
                             'total_against': 0.0, 'quorum_met': True})
        assert [e['id'] for e in ranked] == ['e']
