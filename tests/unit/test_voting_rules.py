"""
Tests for Context-Based Constitutional Voting Rules.

Covers voter eligibility, context classification, weighted tallying,
steward requirements, and human override guarantees.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from unittest.mock import MagicMock, patch


# ─── Voter Eligibility Tests ───

class TestVoterEligibility:
    def test_security_blocks_agent_votes(self):
        """Agent votes are blocked on security_guardrail context."""
        from integrations.social.voting_rules import check_voter_eligibility
        experiment = {
            'title': 'Update firewall rules for master key rotation',
            'hypothesis': 'Changing security permissions will improve safety',
            'decision_context': 'security_guardrail',
        }
        result = check_voter_eligibility(experiment, 'agent')
        assert not result['eligible']
        assert 'security_guardrail' in result['reason']
        assert result['context'] == 'security_guardrail'

    def test_security_allows_human_votes(self):
        """Humans can always vote, even on security contexts."""
        from integrations.social.voting_rules import check_voter_eligibility
        experiment = {
            'title': 'Update firewall rules',
            'hypothesis': 'Security improvement',
            'decision_context': 'security_guardrail',
        }
        result = check_voter_eligibility(experiment, 'human')
        assert result['eligible']

    def test_operational_allows_agent_only(self):
        """Operational tuning allows agent-only voting (no human required)."""
        from integrations.social.voting_rules import get_voter_rules
        rules = get_voter_rules('operational_tuning')
        assert rules['agent_can_vote'] is True
        assert rules['human_required'] is False
        assert rules['agent_weight'] == 1.0

    def test_human_override_always_works(self):
        """Humans are eligible to vote in every context."""
        from integrations.social.voting_rules import check_voter_eligibility, VOTER_RULES
        for context in VOTER_RULES:
            experiment = {'title': 'test', 'hypothesis': 'test',
                          'decision_context': context}
            result = check_voter_eligibility(experiment, 'human')
            assert result['eligible'], f"Human blocked on {context}"


# ─── Context Classification Tests ───

class TestContextClassification:
    def test_security_keywords(self):
        from integrations.social.voting_rules import classify_decision_context
        exp = {'title': 'Master key rotation proposal',
               'hypothesis': 'Rotating the certificate chain improves security'}
        assert classify_decision_context(exp) == 'security_guardrail'

    def test_business_keywords(self):
        from integrations.social.voting_rules import classify_decision_context
        exp = {'title': 'New revenue model',
               'hypothesis': 'Subscription pricing will increase profit'}
        assert classify_decision_context(exp) == 'business_revenue'

    def test_operational_keywords(self):
        from integrations.social.voting_rules import classify_decision_context
        exp = {'title': 'Adjust cache timeout',
               'hypothesis': 'Reducing polling interval improves throughput'}
        # 'cache', 'timeout', 'polling' → operational_tuning
        # 'throughput' → technical_improvement
        # operational has 3 keywords, technical has 1
        assert classify_decision_context(exp) == 'operational_tuning'

    def test_default_classification(self):
        from integrations.social.voting_rules import classify_decision_context
        exp = {'title': 'Random idea', 'hypothesis': 'Something unrelated'}
        # No keywords match → default
        assert classify_decision_context(exp) == 'technical_improvement'

    def test_precomputed_context_used(self):
        """If decision_context is already set, it is used directly."""
        from integrations.social.voting_rules import check_voter_eligibility
        experiment = {
            'title': 'This mentions security but is actually operational',
            'hypothesis': 'Security is fine',
            'decision_context': 'operational_tuning',
        }
        result = check_voter_eligibility(experiment, 'agent')
        assert result['eligible']
        assert result['context'] == 'operational_tuning'


# ─── Weighted Tally Tests ───

class TestBusinessWeightedTally:
    def _make_vote(self, voter_type='human', vote_value=1, confidence=1.0):
        vote = MagicMock()
        vote.voter_type = voter_type
        vote.vote_value = vote_value
        vote.confidence = confidence
        vote.suggestion = None
        vote.voter_id = f'{voter_type}_1'
        return vote

    def test_business_context_weights_agents_at_0_8(self):
        """In business_revenue context, agent weight is 0.8."""
        from integrations.social.voting_rules import get_voter_rules
        rules = get_voter_rules('business_revenue')
        assert rules['agent_weight'] == 0.8

    def test_security_context_zero_agent_weight(self):
        """In security_guardrail context, agent weight is 0."""
        from integrations.social.voting_rules import get_voter_rules
        rules = get_voter_rules('security_guardrail')
        assert rules['agent_weight'] == 0.0

    def test_approval_threshold_varies_by_context(self):
        """Different contexts have different approval thresholds."""
        from integrations.social.voting_rules import get_voter_rules
        assert get_voter_rules('security_guardrail')['approval_threshold'] == 0.8
        assert get_voter_rules('operational_tuning')['approval_threshold'] == 0.3


# ─── Steward Requirement Tests ───

class TestStewardRequirement:
    def test_steward_required_for_security(self):
        """Security context requires steward vote."""
        from integrations.social.voting_rules import get_voter_rules
        rules = get_voter_rules('security_guardrail')
        assert rules['steward_required'] is True

    def test_steward_not_required_for_operational(self):
        """Operational context does not require steward."""
        from integrations.social.voting_rules import get_voter_rules
        rules = get_voter_rules('operational_tuning')
        assert rules['steward_required'] is False

    def test_steward_not_required_for_technical(self):
        from integrations.social.voting_rules import get_voter_rules
        rules = get_voter_rules('technical_improvement')
        assert rules['steward_required'] is False


# ─── Integration with ThoughtExperimentService ───

class TestServiceIntegration:
    def _make_experiment(self, title='Test', hypothesis='Test hypothesis',
                         decision_context=None, status='voting'):
        exp = MagicMock()
        exp.id = 'exp-1'
        exp.status = status
        exp.total_votes = 0
        d = {
            'id': 'exp-1', 'title': title, 'hypothesis': hypothesis,
            'decision_context': decision_context, 'status': status,
        }
        exp.to_dict.return_value = d
        return exp

    def test_cast_vote_blocks_agent_on_security(self):
        """cast_vote rejects agent vote on security-context experiment."""
        from integrations.social.thought_experiment_service import ThoughtExperimentService

        db = MagicMock()
        exp = self._make_experiment(
            title='Update encryption keys',
            hypothesis='Rotating security certificates',
            decision_context='security_guardrail')
        db.query.return_value.filter_by.return_value.first.return_value = exp

        result = ThoughtExperimentService.cast_vote(
            db, 'exp-1', 'agent-1', vote_value=1,
            voter_type='agent', confidence=0.9)

        assert result is not None
        assert result.get('error') == 'voter_not_eligible'

    def test_cast_vote_allows_human_on_security(self):
        """cast_vote allows human vote on security-context experiment."""
        from integrations.social.thought_experiment_service import ThoughtExperimentService

        db = MagicMock()
        exp = self._make_experiment(
            title='Update encryption keys',
            hypothesis='Rotating security certificates',
            decision_context='security_guardrail')

        # First call: experiment lookup; second: existing vote check
        mock_query = MagicMock()
        db.query.return_value = mock_query

        # For ThoughtExperiment query
        mock_filter = MagicMock()
        mock_filter.first.return_value = exp
        mock_query.filter_by.return_value = mock_filter

        result = ThoughtExperimentService.cast_vote(
            db, 'exp-1', 'human-1', vote_value=2,
            voter_type='human', confidence=1.0)

        # Should not be blocked — either returns a vote dict or upserts
        assert result is None or result.get('error') != 'voter_not_eligible'
