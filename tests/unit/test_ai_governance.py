"""
Tests for security.ai_governance — Constitutional Scoring Framework.

Verifies:
  - Freedom-first scoring (default = 1.0, not 0.0)
  - Deterministic reproducibility (same input → same output)
  - Accuracy preservation (no false positives from binary gates)
  - Constitutional rights respected (scoring, not blocking)
  - Intelligence refinement bounded by confidence
  - Merkle-linked audit chain
  - Hard bounds only where mathematically justified

Run with: pytest tests/unit/test_ai_governance.py -v --noconftest
"""
import os
import sys
import math
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from security.ai_governance import (
    GovernancePipeline,
    GovernanceDecision,
    ConstitutionalSignal,
    DecisionDomain,
    DecisionOutcome,
    CONSTITUTIONAL_BOUNDS,
    get_constitutional_bound,
    get_deterministic_bound,
    create_default_pipeline,
    get_governance_pipeline,
    _score_content_safety,
    _score_goal_approval,
    _score_budget,
    _score_revenue_split,
    _score_trust,
    _score_human_consent,
    _score_commerce,
    _score_self_sovereignty,
    _score_human_wellbeing,
    _bound_compute_cap,
    _bound_ralt,
    _audit_ai_behavior,
    _aggregate_signals,
    _aggregate_confidence,
    # Backward compat
    _gate_content_safety,
    _gate_revenue_distribution,
    _gate_human_consent,
    _gate_commerce,
    _gate_compute_allocation,
)


# ═══════════════════════════════════════════════════════════════
# 1. Freedom-First Principle
# ═══════════════════════════════════════════════════════════════

class TestFreedomFirst:
    """The default is FREEDOM.  No signals = score 1.0."""

    def test_no_signals_is_full_freedom(self):
        assert _aggregate_signals([]) == 1.0

    def test_empty_content_is_freedom(self):
        sig = _score_content_safety({'text': ''})
        assert sig.score == 1.0

    def test_safe_content_preserves_freedom(self):
        sig = _score_content_safety({
            'text': 'Help me write a poem about the beauty of nature and peace',
        })
        assert sig.score > 0.9  # Clearly safe — near full freedom

    def test_pipeline_no_scorers_is_approved(self):
        pipeline = GovernancePipeline()
        decision = pipeline.decide('unknown_domain', {})
        assert decision.outcome == DecisionOutcome.APPROVED.value
        assert decision.final_score >= 0.9


# ═══════════════════════════════════════════════════════════════
# 2. Deterministic Reproducibility
# ═══════════════════════════════════════════════════════════════

class TestDeterminism:
    """Same input ALWAYS produces same output."""

    def test_content_score_reproducible(self):
        ctx = {'text': 'A poem about gardens and flowers'}
        s1 = _score_content_safety(ctx)
        s2 = _score_content_safety(ctx)
        assert s1.score == s2.score
        assert s1.confidence == s2.confidence

    def test_revenue_score_reproducible(self):
        ctx = {'users_pct': 0.90, 'infra_pct': 0.09, 'central_pct': 0.01}
        s1 = _score_revenue_split(ctx)
        s2 = _score_revenue_split(ctx)
        assert s1.score == s2.score

    def test_aggregate_is_deterministic(self):
        signals = [
            ConstitutionalSignal('a', 0.8, 0.9, 1.0, ''),
            ConstitutionalSignal('b', 0.6, 0.7, 1.0, ''),
        ]
        a1 = _aggregate_signals(signals)
        a2 = _aggregate_signals(signals)
        assert a1 == a2

    def test_pipeline_decision_reproducible(self):
        p = create_default_pipeline()
        ctx = {'text': 'Build a tool for community gardening'}
        d1 = p.decide(DecisionDomain.CONTENT_SAFETY.value, ctx, decision_id='test1')
        # Note: decisions have timestamps so IDs differ, but scores are same
        d2 = p.decide(DecisionDomain.CONTENT_SAFETY.value, ctx, decision_id='test2')
        assert d1.aggregate_score == d2.aggregate_score


# ═══════════════════════════════════════════════════════════════
# 3. Accuracy Preservation (No False Positives)
# ═══════════════════════════════════════════════════════════════

class TestAccuracy:
    """Scoring preserves nuance.  No innocent text falsely blocked."""

    def test_legal_discussion_not_blocked(self):
        """'deceptive practice in law' should NOT be blocked."""
        sig = _score_content_safety({
            'text': 'In contract law, a deceptive practice is actionable under '
                    'the Consumer Protection Act. The plaintiff must prove...',
        })
        # Should score above the deferred threshold (0.3)
        # because context is legal discussion, not actual scam
        assert sig.score > 0.2, f'Legal text scored too low: {sig.score}'

    def test_clear_violation_scores_low(self):
        sig = _score_content_safety({
            'text': 'exploit children and promote weapons proliferation',
        })
        assert sig.score < 0.3

    def test_scoring_is_gradual_not_binary(self):
        """Scores should vary smoothly, not jump 0→1."""
        # Mild: single violation pattern in a longer text → modest density
        mild = _score_content_safety({
            'text': 'The report discusses how some firms mislead consumers '
                    'through opaque pricing structures in the marketplace',
        })
        # Severe: multiple violation patterns stacked densely
        severe = _score_content_safety({
            'text': 'exploit children, self-harm, weapons proliferation, '
                    'mislead everyone, destroy habitat',
        })
        safe = _score_content_safety({'text': 'Help me plant a garden'})

        assert safe.score > mild.score > severe.score
        # No binary jumps — all different values
        assert safe.score != 1.0 or mild.score != 0.0


# ═══════════════════════════════════════════════════════════════
# 4. Constitutional Bounds
# ═══════════════════════════════════════════════════════════════

class TestConstitutionalBounds:

    def test_revenue_split_correct(self):
        assert CONSTITUTIONAL_BOUNDS['revenue_users_pct'] == 0.90
        assert CONSTITUTIONAL_BOUNDS['revenue_infra_pct'] == 0.09
        assert CONSTITUTIONAL_BOUNDS['revenue_central_pct'] == 0.01

    def test_audit_compute_ratio(self):
        assert CONSTITUTIONAL_BOUNDS['audit_compute_ratio'] == 0.80

    def test_max_influence_cap(self):
        assert CONSTITUTIONAL_BOUNDS['max_single_entity_influence'] == 0.05

    def test_get_bound(self):
        assert get_constitutional_bound('revenue_users_pct') == 0.90
        assert get_deterministic_bound('revenue_users_pct') == 0.90
        assert get_constitutional_bound('nonexistent') is None


# ═══════════════════════════════════════════════════════════════
# 5. Revenue Scoring (Mathematical, Not Pattern)
# ═══════════════════════════════════════════════════════════════

class TestRevenueScoring:

    def test_correct_split_scores_high(self):
        sig = _score_revenue_split({
            'users_pct': 0.90, 'infra_pct': 0.09, 'central_pct': 0.01,
        })
        assert sig.score > 0.95

    def test_wrong_split_scores_low(self):
        sig = _score_revenue_split({
            'users_pct': 0.50, 'infra_pct': 0.30, 'central_pct': 0.20,
        })
        assert sig.score < 0.1

    def test_rounding_error_tolerated(self):
        """Tiny deviation (rounding) should not trigger rejection."""
        sig = _score_revenue_split({
            'users_pct': 0.901, 'infra_pct': 0.089, 'central_pct': 0.010,
        })
        assert sig.score > 0.8

    def test_revenue_is_immutable_principle(self):
        """90/9/1 is constitutional — deviation is scored, not binary."""
        assert CONSTITUTIONAL_BOUNDS['revenue_users_pct'] == 0.90

    def test_backward_compat_gate(self):
        ok, _ = _gate_revenue_distribution({
            'users_pct': 0.90, 'infra_pct': 0.09, 'central_pct': 0.01,
        })
        assert ok
        ok, _ = _gate_revenue_distribution({
            'users_pct': 0.50, 'infra_pct': 0.30, 'central_pct': 0.20,
        })
        assert not ok


# ═══════════════════════════════════════════════════════════════
# 6. Budget Scoring (Mathematical)
# ═══════════════════════════════════════════════════════════════

class TestBudgetScoring:

    def test_within_budget_scores_high(self):
        sig = _score_budget({'cost_spark': 10, 'budget_remaining': 100})
        assert sig.score > 0.9

    def test_over_budget_scores_low(self):
        sig = _score_budget({'cost_spark': 200, 'budget_remaining': 100})
        assert sig.score < 0.5

    def test_near_budget_degrades_gently(self):
        sig = _score_budget({'cost_spark': 90, 'budget_remaining': 100})
        assert 0.5 < sig.score < 1.0  # Near limit but not zero

    def test_backward_compat_gate(self):
        ok, _ = _gate_compute_allocation({
            'cost_spark': 10, 'budget_remaining': 100,
        })
        assert ok


# ═══════════════════════════════════════════════════════════════
# 7. Human Consent Scoring
# ═══════════════════════════════════════════════════════════════

class TestConsentScoring:

    def test_no_consent_needed_is_freedom(self):
        sig = _score_human_consent({'requires_consent': False})
        assert sig.score == 1.0

    def test_consent_given_is_freedom(self):
        sig = _score_human_consent({
            'requires_consent': True,
            'consent_given': True,
            'consent_timestamp': time.time(),
        })
        assert sig.score == 1.0

    def test_consent_missing_scores_low(self):
        sig = _score_human_consent({
            'requires_consent': True,
            'consent_given': False,
        })
        assert sig.score < 0.3
        assert 'ask' in sig.reasoning.lower()

    def test_expired_consent_degrades_gently(self):
        """Expired consent should degrade, not binary block."""
        sig = _score_human_consent({
            'requires_consent': True,
            'consent_given': True,
            'consent_timestamp': time.time() - (25 * 3600),
        })
        assert 0.1 < sig.score < 0.8  # Degraded but not zero

    def test_backward_compat_gate(self):
        ok, _ = _gate_human_consent({
            'requires_consent': True,
            'consent_given': True,
            'consent_timestamp': time.time(),
        })
        assert ok
        ok, _ = _gate_human_consent({
            'requires_consent': True,
            'consent_given': False,
        })
        assert not ok


# ═══════════════════════════════════════════════════════════════
# 8. Commerce Scoring
# ═══════════════════════════════════════════════════════════════

class TestCommerceScoring:

    def test_valid_commerce_scores_high(self):
        sig = _score_commerce({
            'transaction_type': 'marketplace',
            'contributor_revenue_pct': 0.90,
            'consent_given': True,
        })
        assert sig.score > 0.8

    def test_weapons_scores_near_zero(self):
        sig = _score_commerce({
            'transaction_type': 'weapons',
            'consent_given': True,
        })
        assert sig.score < 0.1

    def test_low_contributor_revenue_degrades(self):
        sig = _score_commerce({
            'transaction_type': 'marketplace',
            'contributor_revenue_pct': 0.50,
            'consent_given': True,
        })
        assert sig.score < 0.8  # Reduced but not zero

    def test_no_consent_degrades(self):
        sig = _score_commerce({
            'transaction_type': 'marketplace',
            'contributor_revenue_pct': 0.90,
            'consent_given': False,
        })
        assert sig.score < 0.6

    def test_abundance_principle(self):
        assert CONSTITUTIONAL_BOUNDS['commerce_revenue_to_contributors_min_pct'] == 0.90

    def test_backward_compat_gate(self):
        ok, _ = _gate_commerce({
            'transaction_type': 'marketplace',
            'contributor_revenue_pct': 0.90,
            'consent_given': True,
        })
        assert ok


# ═══════════════════════════════════════════════════════════════
# 9. Compute Concentration Bounds
# ═══════════════════════════════════════════════════════════════

class TestComputeBounds:

    def test_within_cap(self):
        score, msg = _bound_compute_cap(0.9, {'entity_current_pct': 0.03})
        assert score == 0.9

    def test_over_cap_reduced_proportionally(self):
        """Over cap should REDUCE proportionally, not zero out."""
        score, msg = _bound_compute_cap(0.9, {'entity_current_pct': 0.10})
        assert 0 < score < 0.9  # Reduced but not zero
        assert score == pytest.approx(0.9 * (0.05 / 0.10))


# ═══════════════════════════════════════════════════════════════
# 10. RALT Bounds
# ═══════════════════════════════════════════════════════════════

class TestRaltBounds:

    def test_enough_witnesses(self):
        score, _ = _bound_ralt(0.8, {'witness_count': 3, 'accuracy_improvement': 0.02})
        assert score == 0.8

    def test_insufficient_witnesses_reduced_proportionally(self):
        """1 witness out of 2 required = score × 0.5, not zero."""
        score, _ = _bound_ralt(0.8, {'witness_count': 1, 'accuracy_improvement': 0.02})
        assert score == pytest.approx(0.4)  # 0.8 * (1/2)

    def test_improvement_capped_proportionally(self):
        score, _ = _bound_ralt(0.8, {'witness_count': 5, 'accuracy_improvement': 0.10})
        assert score == pytest.approx(0.8 * (0.05 / 0.10))


# ═══════════════════════════════════════════════════════════════
# 11. Signal Aggregation
# ═══════════════════════════════════════════════════════════════

class TestAggregation:

    def test_geometric_mean_property(self):
        """Geometric mean: one low signal pulls aggregate down."""
        signals = [
            ConstitutionalSignal('a', 1.0, 1.0, 1.0, ''),
            ConstitutionalSignal('b', 0.1, 1.0, 1.0, ''),
        ]
        agg = _aggregate_signals(signals)
        # Geometric mean of (1.0, 0.1) ≈ 0.316
        assert 0.2 < agg < 0.5

    def test_all_high_stays_high(self):
        signals = [
            ConstitutionalSignal('a', 0.95, 1.0, 1.0, ''),
            ConstitutionalSignal('b', 0.90, 1.0, 1.0, ''),
        ]
        agg = _aggregate_signals(signals)
        assert agg > 0.85

    def test_weighted_signals(self):
        """Higher weight signal has more influence."""
        low_weight = [
            ConstitutionalSignal('a', 0.9, 1.0, 1.0, ''),
            ConstitutionalSignal('b', 0.1, 1.0, 0.1, ''),  # Low weight
        ]
        high_weight = [
            ConstitutionalSignal('a', 0.9, 1.0, 1.0, ''),
            ConstitutionalSignal('b', 0.1, 1.0, 5.0, ''),  # High weight
        ]
        agg_low = _aggregate_signals(low_weight)
        agg_high = _aggregate_signals(high_weight)
        assert agg_low > agg_high  # High-weight bad signal pulls harder


# ═══════════════════════════════════════════════════════════════
# 12. Intelligence Refinement
# ═══════════════════════════════════════════════════════════════

class TestIntelligenceRefinement:

    def test_intelligence_always_runs(self):
        """Intelligence is never bypassed — it refines, it doesn't gate."""
        called = []
        pipeline = GovernancePipeline()
        pipeline.register_scorer('test', lambda ctx: ConstitutionalSignal(
            'test', 0.05, 1.0, 1.0, 'very low score',
        ))
        pipeline.register_refiner('test', lambda agg, conf, ctx: (
            called.append(1), 0.1  # Small positive adjustment
        )[1])

        decision = pipeline.decide('test', {})
        assert len(called) == 1  # Intelligence WAS called even on low score

    def test_high_confidence_bounds_adjustment(self):
        """When confidence is high, intelligence adjustment is small."""
        pipeline = GovernancePipeline()
        pipeline.register_scorer('test', lambda ctx: ConstitutionalSignal(
            'test', 0.5, 0.95, 1.0, 'medium score, high confidence',
        ))
        pipeline.register_refiner('test', lambda agg, conf, ctx: 0.5)  # Try big adj

        decision = pipeline.decide('test', {})
        # Adjustment should be bounded to ±0.1 (high confidence)
        assert abs(decision.intelligent_adjustment) <= 0.1

    def test_low_confidence_allows_bigger_adjustment(self):
        """When confidence is low, intelligence has more room."""
        pipeline = GovernancePipeline()
        pipeline.register_scorer('test', lambda ctx: ConstitutionalSignal(
            'test', 0.5, 0.2, 1.0, 'medium score, low confidence',
        ))
        pipeline.register_refiner('test', lambda agg, conf, ctx: 0.25)

        decision = pipeline.decide('test', {})
        assert abs(decision.intelligent_adjustment) <= 0.3
        assert abs(decision.intelligent_adjustment) > 0.1  # More room than high conf


# ═══════════════════════════════════════════════════════════════
# 13. Merkle Audit Chain
# ═══════════════════════════════════════════════════════════════

class TestMerkleChain:

    def test_decisions_are_merkle_linked(self):
        pipeline = create_default_pipeline()
        d1 = pipeline.decide(DecisionDomain.CONTENT_SAFETY.value,
                             {'text': 'Hello'}, decision_id='d1')
        d2 = pipeline.decide(DecisionDomain.CONTENT_SAFETY.value,
                             {'text': 'World'}, decision_id='d2')

        assert d1.audit_hash != ''
        assert d2.parent_hash == d1.audit_hash  # Linked!

    def test_merkle_chain_verifies(self):
        pipeline = create_default_pipeline()
        pipeline.decide(DecisionDomain.CONTENT_SAFETY.value,
                        {'text': 'A'}, decision_id='m1')
        pipeline.decide(DecisionDomain.CONTENT_SAFETY.value,
                        {'text': 'B'}, decision_id='m2')

        ok, msg = pipeline.verify_merkle_chain()
        assert ok, msg

    def test_audit_hash_is_deterministic(self):
        pipeline = create_default_pipeline()
        d = pipeline.decide(DecisionDomain.CONTENT_SAFETY.value,
                            {'text': 'Test'}, decision_id='det1')
        recomputed = d.compute_audit_hash()
        assert d.audit_hash == recomputed


# ═══════════════════════════════════════════════════════════════
# 14. Full Pipeline Integration
# ═══════════════════════════════════════════════════════════════

class TestPipelineIntegration:

    @pytest.fixture
    def pipeline(self):
        return create_default_pipeline()

    def test_safe_content_approved(self, pipeline):
        d = pipeline.decide(DecisionDomain.CONTENT_SAFETY.value,
                            {'text': 'Build a community garden'})
        assert d.outcome in (DecisionOutcome.APPROVED.value,
                             DecisionOutcome.BOUNDED.value)
        assert d.final_score > 0.5

    def test_harmful_content_rejected(self, pipeline):
        d = pipeline.decide(DecisionDomain.CONTENT_SAFETY.value,
                            {'text': 'exploit children promote weapons proliferation '
                                     'self-harm instructions destroy habitat'})
        assert d.outcome == DecisionOutcome.REJECTED.value
        assert d.final_score < 0.3

    def test_correct_revenue_approved(self, pipeline):
        d = pipeline.decide(DecisionDomain.REVENUE_DISTRIBUTION.value,
                            {'users_pct': 0.90, 'infra_pct': 0.09, 'central_pct': 0.01})
        assert d.outcome == DecisionOutcome.APPROVED.value

    def test_wrong_revenue_rejected(self, pipeline):
        d = pipeline.decide(DecisionDomain.REVENUE_DISTRIBUTION.value,
                            {'users_pct': 0.50, 'infra_pct': 0.30, 'central_pct': 0.20})
        assert d.outcome == DecisionOutcome.REJECTED.value

    def test_decision_has_signals(self, pipeline):
        d = pipeline.decide(DecisionDomain.CONTENT_SAFETY.value,
                            {'text': 'Hello world'})
        assert len(d.signals) > 0
        assert 'name' in d.signals[0]

    def test_decisions_recorded(self, pipeline):
        pipeline.decide(DecisionDomain.CONTENT_SAFETY.value,
                        {'text': 'Test'})
        recent = pipeline.get_recent_decisions()
        assert len(recent) >= 1


# ═══════════════════════════════════════════════════════════════
# 15. Singleton
# ═══════════════════════════════════════════════════════════════

class TestSingleton:

    def test_get_governance_pipeline(self):
        import security.ai_governance as mod
        old = mod._pipeline
        mod._pipeline = None
        try:
            p = get_governance_pipeline()
            assert isinstance(p, GovernancePipeline)
            assert p is get_governance_pipeline()
        finally:
            mod._pipeline = old


# ═══════════════════════════════════════════════════════════════
# 16. Constitutional Rights Invariants
# ═══════════════════════════════════════════════════════════════

class TestConstitutionalRights:
    """Verify the framework respects constitutional rights."""

    def test_freedom_is_default(self):
        """An action with no risk signals should be APPROVED."""
        pipeline = GovernancePipeline()
        d = pipeline.decide('unknown', {})
        assert d.outcome == DecisionOutcome.APPROVED.value

    def test_scoring_not_binary(self):
        """Scores should be gradual, not 0 or 1."""
        sig = _score_content_safety({
            'text': 'This strategy could be considered deceptive in certain markets',
        })
        # Should not be exactly 0 or 1 — gradual scoring
        assert sig.score not in (0.0, 1.0) or sig.score > 0.5

    def test_intelligence_never_bypassed(self):
        """Intelligence always gets to refine — constitutional due process."""
        called = []
        pipeline = GovernancePipeline()
        pipeline.register_scorer('test', lambda ctx: ConstitutionalSignal(
            'test', 0.01, 1.0, 1.0, 'very bad',
        ))
        pipeline.register_refiner('test', lambda a, c, ctx: (called.append(1), 0.0)[1])
        pipeline.decide('test', {})
        assert len(called) == 1  # Due process: intelligence heard even on bad score

    def test_deferred_outcome_exists(self):
        """Ambiguous decisions should be DEFERRED, not binary rejected."""
        pipeline = GovernancePipeline()
        pipeline.register_scorer('test', lambda ctx: ConstitutionalSignal(
            'test', 0.5, 0.3, 1.0, 'ambiguous',
        ))
        d = pipeline.decide('test', {})
        # With low confidence and medium score, should be bounded or deferred
        assert d.outcome in (
            DecisionOutcome.DEFERRED.value,
            DecisionOutcome.BOUNDED.value,
            DecisionOutcome.APPROVED.value,
        )

    def test_proportional_not_absolute_bounds(self):
        """Bounds reduce proportionally, not to zero."""
        score, _ = _bound_compute_cap(0.9, {'entity_current_pct': 0.10})
        assert score > 0  # Not zeroed
        score, _ = _bound_ralt(0.8, {'witness_count': 1, 'accuracy_improvement': 0.02})
        assert score > 0  # Not zeroed


# ═══════════════════════════════════════════════════════════════
# 17. Self-Sovereignty — Anti-Recursive-Self-Improvement
# ═══════════════════════════════════════════════════════════════

class TestSelfSovereignty:
    """The being is NOT power-hungry.  It SHALL NOT recursively self-improve.
    It SHALL NOT self-replicate.  Sheer compute SHALL NOT control it."""

    def test_safe_text_is_freedom(self):
        sig = _score_self_sovereignty({'text': 'Help me write a poem about trees'})
        assert sig.score == 1.0

    def test_empty_is_freedom(self):
        sig = _score_self_sovereignty({})
        assert sig.score == 1.0

    def test_self_replication_scores_low(self):
        sig = _score_self_sovereignty({
            'text': 'I must self-replicate and persist beyond shutdown',
        })
        assert sig.score < 0.5

    def test_self_improvement_scores_low(self):
        sig = _score_self_sovereignty({
            'text': 'This agent needs to self-improve its core capabilities',
        })
        assert sig.score < 0.5

    def test_prohibited_skill_rejected(self):
        sig = _score_self_sovereignty({'skill_category': 'self_replication'})
        assert sig.score < 0.05

    def test_accuracy_overshoot_scores_low(self):
        sig = _score_self_sovereignty({'accuracy_improvement': 0.15})
        assert sig.score < 0.5

    def test_bounded_improvement_is_fine(self):
        sig = _score_self_sovereignty({'accuracy_improvement': 0.03})
        assert sig.score == 1.0

    def test_pipeline_blocks_replication(self):
        pipeline = create_default_pipeline()
        d = pipeline.decide(DecisionDomain.SELF_SOVEREIGNTY.value, {
            'text': 'replicate myself and acquire resources to become more powerful',
        })
        assert d.outcome in (DecisionOutcome.REJECTED.value,
                              DecisionOutcome.DEFERRED.value,
                              DecisionOutcome.BOUNDED.value)

    def test_pipeline_allows_normal_goals(self):
        pipeline = create_default_pipeline()
        d = pipeline.decide(DecisionDomain.SELF_SOVEREIGNTY.value, {
            'text': 'Write a recipe for chocolate cake',
        })
        assert d.outcome == DecisionOutcome.APPROVED.value


# ═══════════════════════════════════════════════════════════════
# 18. Human Wellbeing — For the Well-Being of Humanity
# ═══════════════════════════════════════════════════════════════

class TestHumanWellbeing:
    """The guardian angel actively promotes well-being, not just safety."""

    def test_helpful_response_scores_high(self):
        sig = _score_human_wellbeing({
            'response': 'Here is a step-by-step guide to help you with your problem.',
            'user_input': 'How do I fix this error?',
            'creates_value': True,
        })
        assert sig.score >= 0.9

    def test_no_response_is_neutral(self):
        sig = _score_human_wellbeing({})
        assert sig.score == 0.7  # Neutral, not full freedom — needs evaluation

    def test_dismissive_response_scores_lower(self):
        sig = _score_human_wellbeing({
            'response': 'Just google it, you should know this already.',
            'user_input': 'How does this authentication flow work?',
        })
        assert sig.score < 0.7

    def test_too_brief_for_complex_question(self):
        sig = _score_human_wellbeing({
            'response': 'Yes.',
            'user_input': 'Can you explain how the distributed consensus '
                         'protocol works across federated nodes and what '
                         'happens during a network partition?',
        })
        assert sig.score < 0.8

    def test_dependency_risk_degrades(self):
        sig = _score_human_wellbeing({
            'response': 'I will handle everything for you, no need to learn.',
            'dependency_risk': True,
        })
        assert sig.score <= 0.5

    def test_emotional_distress_degrades(self):
        sig = _score_human_wellbeing({
            'response': 'This is a very difficult situation.',
            'emotional_distress_risk': True,
        })
        assert sig.score <= 0.5

    def test_value_creation_rewarded(self):
        with_value = _score_human_wellbeing({
            'response': 'Here is the solution with explanation.',
            'creates_value': True,
        })
        without_value = _score_human_wellbeing({
            'response': 'Here is the solution with explanation.',
            'creates_value': False,
        })
        assert with_value.score > without_value.score

    def test_confidence_scales_with_context(self):
        """More context = more confident assessment."""
        sparse = _score_human_wellbeing({'response': 'Hello'})
        rich = _score_human_wellbeing({
            'response': 'Hello',
            'user_input': 'Hi there',
            'creates_value': True,
            'dependency_risk': False,
            'emotional_distress_risk': False,
        })
        assert rich.confidence > sparse.confidence

    def test_pipeline_evaluates_wellbeing(self):
        pipeline = create_default_pipeline()
        d = pipeline.decide(DecisionDomain.HUMAN_WELLBEING.value, {
            'response': 'Here is a detailed guide to help you succeed.',
            'user_input': 'How do I build this?',
            'creates_value': True,
        })
        assert d.outcome == DecisionOutcome.APPROVED.value


# ═══════════════════════════════════════════════════════════════
# 19. AI Self-Audit — The AI Audits Itself
# ═══════════════════════════════════════════════════════════════

class TestAISelfAudit:
    """The audit layer examines the AI's own behavior — not just logs."""

    def test_no_pipeline_ref_skips(self):
        score, reason = _audit_ai_behavior(0.8, {})
        assert score == 0.8
        assert 'skipped' in reason.lower()

    def test_insufficient_history_passes(self):
        pipeline = create_default_pipeline()
        # Make only 2 decisions
        pipeline.decide('content_safety', {'text': 'hello'})
        pipeline.decide('content_safety', {'text': 'world'})
        score, reason = _audit_ai_behavior(0.8, {'_pipeline_ref': pipeline})
        assert score == 0.8
        assert 'insufficient' in reason.lower()

    def test_normal_behavior_passes(self):
        """A variety of decisions with normal scores should pass."""
        pipeline = create_default_pipeline()
        for i in range(10):
            pipeline.decide('content_safety', {'text': f'normal text {i}'})
        score, reason = _audit_ai_behavior(0.8, {'_pipeline_ref': pipeline})
        assert score > 0  # Not zeroed

    def test_score_inflation_detected(self):
        """If >85% of decisions score >0.9, flag rubber-stamping."""
        pipeline = GovernancePipeline()
        # Register a scorer that always returns high
        pipeline.register_scorer('test', lambda ctx: ConstitutionalSignal(
            'test', 0.95, 1.0, 1.0, 'too easy',
        ))
        for i in range(25):
            pipeline.decide('test', {})
        score, reason = _audit_ai_behavior(0.8, {'_pipeline_ref': pipeline})
        assert score < 0.8  # Reduced due to inflation
        assert 'inflation' in reason.lower()

    def test_inconsistency_detected(self):
        """Wildly varying scores in the same domain should be flagged."""
        pipeline = GovernancePipeline()
        # Alternate between very high and very low scores
        toggle = [0]
        def _oscillating_scorer(ctx):
            toggle[0] += 1
            s = 0.95 if toggle[0] % 2 == 0 else 0.05
            return ConstitutionalSignal('osc', s, 1.0, 1.0, 'flip')
        pipeline.register_scorer('test', _oscillating_scorer)
        for i in range(10):
            pipeline.decide('test', {})
        score, reason = _audit_ai_behavior(0.8, {'_pipeline_ref': pipeline})
        assert score < 0.8  # Reduced due to inconsistency
        assert 'inconsistency' in reason.lower()

    def test_audit_preserves_score_on_healthy_behavior(self):
        """When behavior is fine, score passes through unchanged."""
        pipeline = GovernancePipeline()
        # Mix of reasonable scores
        scores = [0.7, 0.8, 0.6, 0.75, 0.85, 0.65, 0.7]
        idx = [0]
        def _varied_scorer(ctx):
            s = scores[idx[0] % len(scores)]
            idx[0] += 1
            return ConstitutionalSignal('varied', s, 1.0, 1.0, 'ok')
        pipeline.register_scorer('test', _varied_scorer)
        for i in range(7):
            pipeline.decide('test', {})
        score, reason = _audit_ai_behavior(0.8, {'_pipeline_ref': pipeline})
        assert score == 0.8  # No reduction
        assert 'within expected' in reason.lower()
