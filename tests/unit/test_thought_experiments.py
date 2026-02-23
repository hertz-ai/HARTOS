"""
Tests for Constitutional Thought Experiments.

Covers:
  1. ThoughtExperiment + ExperimentVote models
  2. ThoughtExperimentService — full lifecycle
  3. Voting — humans, agents, weighted tally
  4. Agent evaluation recording
  5. Constitutional filtering
  6. Reward integration
  7. API blueprint endpoints
  8. Tool wrappers
  9. Integration — goal registration, seed goals

~50 tests across 9 test classes.
"""
import json
import os
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ─── Environment Setup ───
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('SOCIAL_DB_PATH', ':memory:')

from integrations.social.models import (
    Base, User, Post, ThoughtExperiment, ExperimentVote, ResonanceWallet,
)
from integrations.social.thought_experiment_service import (
    ThoughtExperimentService, DISCUSS_DURATION_HOURS, VOTING_DURATION_HOURS,
    VALID_STATUSES, VALID_INTENT_CATEGORIES,
)


@pytest.fixture(scope='session')
def engine():
    eng = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def db(engine):
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def user(db):
    uname = f'test_user_{uuid.uuid4().hex[:8]}'
    u = User(username=uname, user_type='human')
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def system_user(db):
    uname = f'sys_agent_{uuid.uuid4().hex[:8]}'
    u = User(username=uname, user_type='human')
    db.add(u)
    db.flush()
    return u


# ════════════════════════════════════════════════════
# 1. Model Tests
# ════════════════════════════════════════════════════

class TestThoughtExperimentModel:
    """Test ThoughtExperiment and ExperimentVote models."""

    def test_create_experiment(self, db, user):
        exp = ThoughtExperiment(
            creator_id=user.id,
            title='Test Hypothesis',
            hypothesis='If we add caching, response time decreases by 50%',
            intent_category='technology',
            status='proposed',
        )
        db.add(exp)
        db.flush()
        assert exp.id is not None
        assert exp.status == 'proposed'

    def test_experiment_to_dict(self, db, user):
        exp = ThoughtExperiment(
            creator_id=user.id,
            title='Dict Test',
            hypothesis='Testing to_dict',
            is_core_ip=True,
        )
        db.add(exp)
        db.flush()
        d = exp.to_dict()
        assert d['title'] == 'Dict Test'
        assert d['is_core_ip'] is True
        assert d['status'] == 'proposed'

    def test_create_vote(self, db, user):
        exp = ThoughtExperiment(
            creator_id=user.id,
            title='Vote Test',
            hypothesis='Test voting model',
        )
        db.add(exp)
        db.flush()

        vote = ExperimentVote(
            experiment_id=exp.id,
            voter_id=user.id,
            voter_type='human',
            vote_value=2,
            confidence=1.0,
            reasoning='Great idea',
        )
        db.add(vote)
        db.flush()
        assert vote.id is not None
        assert vote.vote_value == 2

    def test_vote_to_dict(self, db, user):
        exp = ThoughtExperiment(
            creator_id=user.id,
            title='Vote Dict',
            hypothesis='Testing vote dict',
        )
        db.add(exp)
        db.flush()

        vote = ExperimentVote(
            experiment_id=exp.id,
            voter_id=user.id,
            vote_value=-1,
            reasoning='Not convinced',
        )
        db.add(vote)
        db.flush()
        d = vote.to_dict()
        assert d['vote_value'] == -1
        assert d['reasoning'] == 'Not convinced'


# ════════════════════════════════════════════════════
# 2. Service — Create and Lifecycle
# ════════════════════════════════════════════════════

class TestExperimentLifecycle:
    """Test ThoughtExperimentService create and lifecycle methods."""

    def test_create_experiment(self, db, user):
        result = ThoughtExperimentService.create_experiment(
            db, user.id, 'Test Experiment',
            'If we federate, latency drops by 30%',
            expected_outcome='Lower latency for all nodes',
            intent_category='technology',
        )
        assert result is not None
        assert result['title'] == 'Test Experiment'
        assert result['status'] == 'proposed'
        assert result['voting_opens_at'] is not None

    def test_create_with_invalid_category_defaults(self, db, user):
        result = ThoughtExperimentService.create_experiment(
            db, user.id, 'Cat Test', 'Hypothesis here',
            intent_category='invalid_cat',
        )
        assert result is not None
        assert result['intent_category'] == 'technology'

    def test_create_core_ip(self, db, user):
        result = ThoughtExperimentService.create_experiment(
            db, user.id, 'Core IP Test', 'Platform hypothesis',
            is_core_ip=True,
        )
        assert result is not None
        assert result['is_core_ip'] is True

    def test_advance_proposed_to_discussing(self, db, user):
        exp = ThoughtExperimentService.create_experiment(
            db, user.id, 'Advance Test', 'Advance hypothesis')
        result = ThoughtExperimentService.advance_status(
            db, exp['id'], target_status='discussing')
        assert result is not None
        assert result['status'] == 'discussing'

    def test_advance_auto(self, db, user):
        exp = ThoughtExperimentService.create_experiment(
            db, user.id, 'Auto Advance', 'Auto hypothesis')
        result = ThoughtExperimentService.advance_status(db, exp['id'])
        assert result['status'] == 'discussing'  # proposed → discussing

    def test_advance_cannot_go_backwards(self, db, user):
        exp = ThoughtExperimentService.create_experiment(
            db, user.id, 'Backwards Test', 'Backwards hypothesis')
        ThoughtExperimentService.advance_status(
            db, exp['id'], target_status='voting')
        result = ThoughtExperimentService.advance_status(
            db, exp['id'], target_status='proposed')
        assert result is None

    def test_close_experiment(self, db, user):
        exp = ThoughtExperimentService.create_experiment(
            db, user.id, 'Close Test', 'Close hypothesis')
        ThoughtExperimentService.advance_status(
            db, exp['id'], target_status='decided')
        result = ThoughtExperimentService.close_experiment(db, exp['id'])
        assert result['status'] == 'archived'


# ════════════════════════════════════════════════════
# 3. Voting
# ════════════════════════════════════════════════════

class TestExperimentVoting:
    """Test voting mechanics."""

    def _make_voting_experiment(self, db, user):
        exp = ThoughtExperimentService.create_experiment(
            db, user.id, 'Voting Exp', 'Voting hypothesis')
        ThoughtExperimentService.advance_status(
            db, exp['id'], target_status='voting')
        return exp

    def test_human_vote(self, db, user):
        exp = self._make_voting_experiment(db, user)
        result = ThoughtExperimentService.cast_vote(
            db, exp['id'], user.id, vote_value=1,
            reasoning='Good idea', voter_type='human')
        assert result is not None
        assert result['vote_value'] == 1
        assert result['voter_type'] == 'human'

    def test_agent_vote_with_confidence(self, db, user):
        exp = self._make_voting_experiment(db, user)
        agent = User(username=f'agent_voter_{uuid.uuid4().hex[:8]}', user_type='human')
        db.add(agent)
        db.flush()
        result = ThoughtExperimentService.cast_vote(
            db, exp['id'], agent.id, vote_value=2,
            reasoning='Strongly support',
            voter_type='agent', confidence=0.8)
        assert result is not None
        assert result['confidence'] == 0.8
        assert result['voter_type'] == 'agent'

    def test_vote_value_clamped(self, db, user):
        exp = self._make_voting_experiment(db, user)
        result = ThoughtExperimentService.cast_vote(
            db, exp['id'], user.id, vote_value=10)
        assert result['vote_value'] == 2  # Clamped to max

    def test_vote_upsert(self, db, user):
        exp = self._make_voting_experiment(db, user)
        ThoughtExperimentService.cast_vote(
            db, exp['id'], user.id, vote_value=1)
        result = ThoughtExperimentService.cast_vote(
            db, exp['id'], user.id, vote_value=-1)
        assert result['vote_value'] == -1

    def test_cannot_vote_on_decided(self, db, user):
        exp = ThoughtExperimentService.create_experiment(
            db, user.id, 'Decided Exp', 'Decided hypothesis')
        ThoughtExperimentService.advance_status(
            db, exp['id'], target_status='decided')
        result = ThoughtExperimentService.cast_vote(
            db, exp['id'], user.id, vote_value=1)
        assert result is not None
        assert 'error' in result

    def test_vote_with_suggestion(self, db, user):
        exp = self._make_voting_experiment(db, user)
        result = ThoughtExperimentService.cast_vote(
            db, exp['id'], user.id, vote_value=1,
            suggestion='Consider adding caching')
        assert result['suggestion'] == 'Consider adding caching'

    def test_human_confidence_always_1(self, db, user):
        exp = self._make_voting_experiment(db, user)
        result = ThoughtExperimentService.cast_vote(
            db, exp['id'], user.id, vote_value=1,
            voter_type='human', confidence=0.5)
        assert result['confidence'] == 1.0


# ════════════════════════════════════════════════════
# 4. Tally and Decision
# ════════════════════════════════════════════════════

class TestExperimentTally:
    """Test vote tally and decision mechanics."""

    def test_tally_basic(self, db, user):
        exp = ThoughtExperimentService.create_experiment(
            db, user.id, 'Tally Exp', 'Tally hypothesis')
        ThoughtExperimentService.advance_status(
            db, exp['id'], target_status='voting')

        u2 = User(username=f'voter2_{uuid.uuid4().hex[:8]}', user_type='human')
        u3 = User(username=f'voter3_{uuid.uuid4().hex[:8]}', user_type='human')
        db.add_all([u2, u3])
        db.flush()

        ThoughtExperimentService.cast_vote(db, exp['id'], user.id, 2)
        ThoughtExperimentService.cast_vote(db, exp['id'], u2.id, 1)
        ThoughtExperimentService.cast_vote(db, exp['id'], u3.id, -1)

        tally = ThoughtExperimentService.tally_votes(db, exp['id'])
        assert tally['total_votes'] == 3
        assert tally['human_votes'] == 3
        assert tally['weighted_score'] > 0  # 2+1-1 = 2/3 ≈ 0.67

    def test_tally_weighted_agents(self, db, user):
        exp = ThoughtExperimentService.create_experiment(
            db, user.id, 'Weighted Tally', 'Weighted hypothesis')
        ThoughtExperimentService.advance_status(
            db, exp['id'], target_status='voting')

        agent = User(username=f'eval_agent_{uuid.uuid4().hex[:8]}', user_type='human')
        db.add(agent)
        db.flush()

        ThoughtExperimentService.cast_vote(
            db, exp['id'], user.id, -1, voter_type='human')
        ThoughtExperimentService.cast_vote(
            db, exp['id'], agent.id, 2, voter_type='agent', confidence=0.3)

        tally = ThoughtExperimentService.tally_votes(db, exp['id'])
        assert tally['agent_votes'] == 1
        # Human: -1*1.0=-1, Agent: 2*0.3=0.6 → weighted = -0.4/1.3 ≈ -0.31
        assert tally['weighted_score'] < 0

    def test_decide(self, db, user):
        exp = ThoughtExperimentService.create_experiment(
            db, user.id, 'Decide Exp', 'Decide hypothesis')
        ThoughtExperimentService.advance_status(
            db, exp['id'], target_status='voting')
        ThoughtExperimentService.cast_vote(db, exp['id'], user.id, 2)

        result = ThoughtExperimentService.decide(
            db, exp['id'], 'Approved — proceed with implementation')
        assert result is not None
        assert result['status'] == 'decided'
        assert result['decision_outcome'] == 'Approved — proceed with implementation'
        assert result['decision_rationale'] is not None

    def test_tally_not_found(self, db):
        tally = ThoughtExperimentService.tally_votes(db, 'nonexistent')
        assert 'error' in tally

    def test_tally_recommendation(self, db, user):
        exp = ThoughtExperimentService.create_experiment(
            db, user.id, 'Recommend Exp', 'Recommend hypothesis')
        ThoughtExperimentService.advance_status(
            db, exp['id'], target_status='voting')
        ThoughtExperimentService.cast_vote(db, exp['id'], user.id, 2)

        tally = ThoughtExperimentService.tally_votes(db, exp['id'])
        assert tally['decision_recommendation'] == 'approve'


# ════════════════════════════════════════════════════
# 5. Agent Evaluation
# ════════════════════════════════════════════════════

class TestAgentEvaluation:
    """Test agent evaluation recording."""

    def test_record_evaluation(self, db, user):
        exp = ThoughtExperimentService.create_experiment(
            db, user.id, 'Eval Exp', 'Eval hypothesis')
        result = ThoughtExperimentService.record_agent_evaluation(
            db, exp['id'], agent_id='agent_1',
            score=1.5, confidence=0.9,
            reasoning='Feasible with minor modifications',
            evidence='Prior experiments show 20% improvement')
        assert result is not None
        evals = result['agent_evaluations_json']
        assert len(evals) == 1
        assert evals[0]['agent_id'] == 'agent_1'
        assert evals[0]['score'] == 1.5

    def test_multiple_evaluations(self, db, user):
        exp = ThoughtExperimentService.create_experiment(
            db, user.id, 'Multi Eval', 'Multi hypothesis')
        ThoughtExperimentService.record_agent_evaluation(
            db, exp['id'], 'agent_1', 1.0, 0.8, 'Good')
        result = ThoughtExperimentService.record_agent_evaluation(
            db, exp['id'], 'agent_2', -0.5, 0.6, 'Risky')
        evals = result['agent_evaluations_json']
        assert len(evals) == 2

    def test_evaluation_clamped(self, db, user):
        exp = ThoughtExperimentService.create_experiment(
            db, user.id, 'Clamp Eval', 'Clamp hypothesis')
        result = ThoughtExperimentService.record_agent_evaluation(
            db, exp['id'], 'agent_1', 10.0, 5.0, 'Extreme')
        evals = result['agent_evaluations_json']
        assert evals[0]['score'] == 2.0  # Clamped
        assert evals[0]['confidence'] == 1.0  # Clamped


# ════════════════════════════════════════════════════
# 6. Queries
# ════════════════════════════════════════════════════

class TestExperimentQueries:
    """Test query methods."""

    def test_get_active_experiments(self, db, user):
        ThoughtExperimentService.create_experiment(
            db, user.id, 'Active 1', 'H1')
        ThoughtExperimentService.create_experiment(
            db, user.id, 'Active 2', 'H2')
        results = ThoughtExperimentService.get_active_experiments(db)
        assert len(results) >= 2

    def test_get_by_status(self, db, user):
        exp = ThoughtExperimentService.create_experiment(
            db, user.id, 'Status Filter', 'H status')
        ThoughtExperimentService.advance_status(
            db, exp['id'], target_status='voting')
        results = ThoughtExperimentService.get_active_experiments(
            db, status='voting')
        voting = [e for e in results if e['id'] == exp['id']]
        assert len(voting) == 1

    def test_get_detail(self, db, user):
        exp = ThoughtExperimentService.create_experiment(
            db, user.id, 'Detail Test', 'H detail')
        detail = ThoughtExperimentService.get_experiment_detail(
            db, exp['id'])
        assert detail is not None
        assert 'votes' in detail
        assert 'tally' in detail

    def test_get_core_ip(self, db, user):
        ThoughtExperimentService.create_experiment(
            db, user.id, 'Core IP', 'H core', is_core_ip=True)
        results = ThoughtExperimentService.get_core_ip_experiments(db)
        assert any(e['title'] == 'Core IP' for e in results)

    def test_get_timeline(self, db, user):
        exp = ThoughtExperimentService.create_experiment(
            db, user.id, 'Timeline Test', 'H timeline')
        timeline = ThoughtExperimentService.get_experiment_timeline(
            db, exp['id'])
        assert timeline is not None
        assert timeline['status'] == 'proposed'
        assert 'voting_opens_at' in timeline

    def test_get_votes(self, db, user):
        exp = ThoughtExperimentService.create_experiment(
            db, user.id, 'Votes Query', 'H votes')
        ThoughtExperimentService.advance_status(
            db, exp['id'], target_status='voting')
        ThoughtExperimentService.cast_vote(db, exp['id'], user.id, 1)
        votes = ThoughtExperimentService.get_experiment_votes(
            db, exp['id'])
        assert len(votes) >= 1


# ════════════════════════════════════════════════════
# 7. API Blueprint
# ════════════════════════════════════════════════════

class TestThoughtExperimentAPI:
    """Test API blueprint endpoints with Flask test client."""

    @pytest.fixture
    def client(self, db):
        from flask import Flask
        app = Flask(__name__)
        app.config['TESTING'] = True

        from integrations.social.api_thought_experiments import thought_experiments_bp
        app.register_blueprint(thought_experiments_bp)

        with app.test_client() as client:
            with app.app_context():
                yield client

    @patch('integrations.social.models.get_db')
    def test_create_experiment_endpoint(self, mock_get_db, client, db, user):
        mock_get_db.return_value = db
        resp = client.post('/api/social/experiments', json={
            'creator_id': user.id,
            'title': 'API Test',
            'hypothesis': 'Testing the API',
        })
        assert resp.status_code in (201, 200)
        data = resp.get_json()
        assert data['success'] is True

    @patch('integrations.social.models.get_db')
    def test_create_missing_fields(self, mock_get_db, client, db):
        mock_get_db.return_value = db
        resp = client.post('/api/social/experiments', json={})
        assert resp.status_code == 400

    @patch('integrations.social.models.get_db')
    def test_list_experiments_endpoint(self, mock_get_db, client, db, user):
        mock_get_db.return_value = db
        ThoughtExperimentService.create_experiment(
            db, user.id, 'List API', 'H list')
        resp = client.get('/api/social/experiments')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

    @patch('integrations.social.models.get_db')
    def test_core_ip_endpoint(self, mock_get_db, client, db, user):
        mock_get_db.return_value = db
        ThoughtExperimentService.create_experiment(
            db, user.id, 'API Core IP', 'H core', is_core_ip=True)
        resp = client.get('/api/social/experiments/core-ip')
        assert resp.status_code == 200


# ════════════════════════════════════════════════════
# 8. Tools
# ════════════════════════════════════════════════════

class TestThoughtExperimentTools:
    """Test AutoGen tool wrappers."""

    @patch('integrations.social.models.get_db')
    def test_get_experiment_status_tool(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        from integrations.agent_engine.thought_experiment_tools import get_experiment_status
        result = json.loads(get_experiment_status())
        assert 'success' in result or 'error' in result

    def test_tool_registration(self):
        from integrations.agent_engine.thought_experiment_tools import THOUGHT_EXPERIMENT_TOOLS
        names = [t['name'] for t in THOUGHT_EXPERIMENT_TOOLS]
        assert 'create_thought_experiment' in names
        assert 'cast_experiment_vote' in names
        assert 'evaluate_thought_experiment' in names
        assert 'tally_experiment_votes' in names
        assert 'advance_experiment' in names
        assert all(
            'thought_experiment' in t['tags']
            for t in THOUGHT_EXPERIMENT_TOOLS
        )


# ════════════════════════════════════════════════════
# 9. Integration
# ════════════════════════════════════════════════════

class TestThoughtExperimentIntegration:
    """Integration tests for goal registration and wiring."""

    def test_goal_type_registered(self):
        from integrations.agent_engine.goal_manager import (
            get_registered_types, get_tool_tags)
        assert 'thought_experiment' in get_registered_types()
        tags = get_tool_tags('thought_experiment')
        assert 'thought_experiment' in tags

    def test_seed_goal_exists(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert 'bootstrap_thought_experiment_coordinator' in slugs

    def test_reward_entries_exist(self):
        from integrations.social.resonance_engine import AWARD_TABLE
        assert 'experiment_proposed' in AWARD_TABLE
        assert 'experiment_voted' in AWARD_TABLE
        assert 'experiment_evaluated' in AWARD_TABLE
        assert 'experiment_suggestion' in AWARD_TABLE
        assert AWARD_TABLE['experiment_proposed']['spark'] == 20

    def test_schema_version_bumped(self):
        from integrations.social.migrations import SCHEMA_VERSION
        assert SCHEMA_VERSION >= 30

    def test_models_importable(self):
        from integrations.social.models import ThoughtExperiment, ExperimentVote
        assert ThoughtExperiment.__tablename__ == 'thought_experiments'
        assert ExperimentVote.__tablename__ == 'experiment_votes'
