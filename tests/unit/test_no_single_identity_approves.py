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


@pytest.fixture(autouse=True)
def _no_known_owner(monkeypatch):
    """The node's owner (HEVOLVE_OWNER_USER_ID) is unknown unless a test
    sets it, whatever the shell running the suite exports."""
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)


@pytest.fixture
def db(engine):
    session = sessionmaker(bind=engine)()
    yield session
    session.rollback()
    session.close()


def _user(db, user_type='human', owner_id=None, settings=None):
    u = User(username=f'u_{uuid.uuid4().hex[:10]}', user_type=user_type,
             owner_id=owner_id, settings=settings)
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

    def test_agents_no_person_owns_are_one_identity_this_node(self, db):
        """Agents may vote (owner ruling 2026-09-23), but agents no person
        owns are ONE identity, this node (owner ruling 2026-10-21; it
        replaces 'an agent with no owner is a distinct identity'): measured
        on production, three such agents approved with no person voting."""
        exp_id = _voting_experiment(db)
        for _ in range(MIN_DISTINCT_VOTERS):
            _vote(db, exp_id, _user(db, 'agent'), 2, confidence=0.9)
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        assert tally['distinct_voters'] == 1
        assert tally['quorum_met'] is False

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
        """The weighted case the 2/3 ratio misses: 1.0 / 1.4 = 0.71.  The
        low-confidence AGAINST voters are agents of DISTINCT people, so the
        voters do reach the quorum count and only the supporters rule can
        refuse it (review of d1cd8de2f: with agents no person owns they
        merged into one identity and the rule went untested)."""
        exp_id = _voting_experiment(db)
        _vote(db, exp_id, _user(db), 2)
        for _ in range(max(2, MIN_DISTINCT_VOTERS - 1)):
            _vote(db, exp_id, _user(db, 'agent', owner_id=_user(db).id), -1,
                  confidence=0.2)
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        decisive = tally['total_for'] + tally['total_against']
        assert tally['total_for'] / decisive >= 2 / 3
        assert tally['distinct_voters'] >= MIN_DISTINCT_VOTERS
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


class TestAnAgentCountsAsThePersonAtTheRootOfItsChain:
    """Owner ruling 2026-10-21 (hevolveai Master 11.462 K): an agent counts as
    the person at the root of its owner chain; an agent whose chain reaches no
    person counts as this node.  Production: 16 of the 19 owned agents are
    owned by another agent."""

    def test_an_agent_owned_by_an_agent_owned_by_a_person_is_that_person(self, db):
        exp_id = _voting_experiment(db)
        person = _user(db)
        middle = _user(db, 'agent', owner_id=person.id)
        _vote(db, exp_id, person, -2)
        for _ in range(3):
            _vote(db, exp_id, _user(db, 'agent', owner_id=middle.id), 2)
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        # one identity, and the person's own vote decides it
        assert (tally['distinct_voters'], tally['distinct_supporters']) == (1, 0)

    @pytest.mark.parametrize('root', ['none', 'agent_chain', 'system', 'missing'])
    def test_an_agent_no_person_owns_counts_as_this_node(self, db, root):
        exp_id = _voting_experiment(db)
        plain = _user(db, 'agent')                      # no owner at all
        if root == 'none':
            other = _user(db, 'agent')
        elif root == 'agent_chain':
            other = _user(db, 'agent', owner_id=_user(db, 'agent', owner_id=_user(db, 'agent').id).id)
        elif root == 'system':
            other = _user(db, 'agent', owner_id=_user(db, 'system').id)
        else:
            other = _user(db, 'agent', owner_id='no-such-user')
        _vote(db, exp_id, plain, 2)
        _vote(db, exp_id, other, 2)
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        assert (tally['distinct_voters'], tally['distinct_supporters']) == (1, 1)

    def test_an_owner_cycle_ends_and_counts_as_this_node(self):
        """Two agents owning each other: the walk ends (under a deadline,
        so a loop fails here instead of hanging the suite) at this node."""
        import threading
        from types import SimpleNamespace
        from integrations.social.voting_rules import NODE_IDENTITY, voter_identity
        a = SimpleNamespace(id='a', user_type='agent', owner_id='b', settings=None)
        b = SimpleNamespace(id='b', user_type='agent', owner_id='a', settings=None)
        rows = {'a': a, 'b': b}
        out = []
        worker = threading.Thread(
            target=lambda: out.append(voter_identity(a, rows.get)), daemon=True)
        worker.start()
        worker.join(10)
        assert not worker.is_alive(), "the owner chain walk did not end"
        assert out == [NODE_IDENTITY]

    def test_the_world_model_is_one_identity_of_its_own(self, db):
        """Owner ruling 2026-10-19: the world model's measured vote is one
        more identity; two accounts carrying its marker are still one."""
        exp_id = _voting_experiment(db)
        for _ in range(2):
            _vote(db, exp_id, _user(db, 'agent', settings={'world_model_voter': True}), 2)
        _vote(db, exp_id, _user(db, 'agent'), 2)
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        assert (tally['distinct_voters'], tally['distinct_supporters']) == (2, 2)
        assert tally['quorum_met'] is False   # a node never approves alone

    def test_a_node_whose_owner_is_unknown_needs_a_person(self, db):
        """Its agents and its world model are two identities; a person
        voting FOR makes the third, and the quorum is met (no owner known,
        so the person is not the node)."""
        exp_id = _voting_experiment(db)
        _vote(db, exp_id, _user(db, 'agent'), 2)
        _vote(db, exp_id, _user(db, 'agent', settings={'world_model_voter': True}), 2)
        _vote(db, exp_id, _user(db), 2)
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        assert (tally['distinct_voters'], tally['distinct_supporters']) == (3, 3)
        assert tally['quorum_met'] is True


class TestOnePersonRuleAndTheNodesOwner:
    """Review of d1cd8de2f: a person is any account whose type is not in
    core.constants.NON_PERSON_USER_TYPES (the test UserService.
    person_to_notify uses), so a system account is no person; and the
    owner's ruling of 2026-10-21 counts an agent no person owns as "this
    node's owner" (HEVOLVE_OWNER_USER_ID, when it is a person's row)."""

    def test_a_system_account_and_its_agent_are_this_node(self, db):
        """A system account, an agent it owns, an agent no one owns and the
        world model, all FOR: the system account is no person, so it and its
        agent are this node like the unowned agent: 2 identities, no quorum
        (d1cd8de2f counted the system account as itself and approved with
        no person; counting it as a person would too)."""
        exp_id = _voting_experiment(db)
        system = _user(db, 'system')
        _vote(db, exp_id, system, 2)
        _vote(db, exp_id, _user(db, 'agent', owner_id=system.id), 2)
        _vote(db, exp_id, _user(db, 'agent'), 2)
        _vote(db, exp_id, _user(db, 'agent', settings={'world_model_voter': True}), 2)
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        assert (tally['distinct_voters'], tally['quorum_met']) == (2, False)

    def test_a_guest_is_a_person(self, db):
        """The desktop's own UI account is a guest: a person, and their
        agent counts as them."""
        exp_id = _voting_experiment(db)
        guest = _user(db, 'guest')
        _vote(db, exp_id, guest, 2)
        _vote(db, exp_id, _user(db, 'agent', owner_id=guest.id), 2)
        _vote(db, exp_id, _user(db, 'agent'), 2)      # this node
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        assert tally['distinct_voters'] == 2          # the guest, and the node

    def test_the_world_model_marker_on_a_persons_agent_is_that_person(self, db):
        exp_id = _voting_experiment(db)
        person = _user(db)
        _vote(db, exp_id, person, 2)
        _vote(db, exp_id, _user(db, 'agent', owner_id=person.id,
                                settings={'world_model_voter': True}), 2)
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        assert tally['distinct_voters'] == 1

    def test_the_nodes_agents_count_as_its_owner(self, db, monkeypatch):
        """HEVOLVE_OWNER_USER_ID names a person: their node's agents are
        them, their own vote decides, and with the world model they are
        still two identities, so the owner and their machine can never
        approve alone."""
        exp_id = _voting_experiment(db)
        owner = _user(db)
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', owner.id)
        _vote(db, exp_id, owner, -2)
        for _ in range(3):
            _vote(db, exp_id, _user(db, 'agent'), 2)
        _vote(db, exp_id, _user(db, 'agent', settings={'world_model_voter': True}), 2)
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        assert (tally['distinct_voters'], tally['distinct_supporters']) == (2, 1)
        assert tally['quorum_met'] is False

    @pytest.mark.parametrize('owner', ['g_0123456789abcdef', 'agent', 'system'])
    def test_an_owner_id_that_is_no_person_here_is_the_bare_node(self, db, monkeypatch, owner):
        """A guest id with no row here, an agent's or a system account's id:
        the node is its own identity (and a person here is a third)."""
        exp_id = _voting_experiment(db)
        if owner != 'g_0123456789abcdef':
            owner = _user(db, owner).id
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', owner)
        _vote(db, exp_id, _user(db, 'agent'), 2)
        _vote(db, exp_id, _user(db, 'agent', settings={'world_model_voter': True}), 2)
        _vote(db, exp_id, _user(db), 2)
        tally = ThoughtExperimentService.tally_votes(db, exp_id)
        assert tally['distinct_voters'] == 3
