"""
Tests for integrations/agent_engine/budget_gate.py — pre-dispatch spend control.
"""
import sys
import os
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


# ── WS1: estimate_llm_cost_spark ─────────────────────────────────────

class TestEstimateLLMCostSpark:

    def test_estimate_returns_positive_int(self):
        from integrations.agent_engine.budget_gate import estimate_llm_cost_spark
        cost = estimate_llm_cost_spark("Hello world", "gpt-4o")
        assert isinstance(cost, int)
        assert cost >= 1

    def test_longer_prompt_costs_more(self):
        from integrations.agent_engine.budget_gate import estimate_llm_cost_spark
        short = estimate_llm_cost_spark("Hi", "gpt-4o")
        long_prompt = "word " * 2000
        long_cost = estimate_llm_cost_spark(long_prompt, "gpt-4o")
        assert long_cost > short

    def test_gpt4_costs_more_than_mini(self):
        from integrations.agent_engine.budget_gate import estimate_llm_cost_spark
        prompt = "Explain quantum computing in detail " * 50
        gpt4 = estimate_llm_cost_spark(prompt, "gpt-4")
        mini = estimate_llm_cost_spark(prompt, "gpt-4o-mini")
        assert gpt4 >= mini

    def test_empty_prompt_returns_min_1(self):
        from integrations.agent_engine.budget_gate import estimate_llm_cost_spark
        cost = estimate_llm_cost_spark("", "gpt-4o")
        assert cost >= 1

    def test_unknown_model_uses_default_cost(self):
        from integrations.agent_engine.budget_gate import estimate_llm_cost_spark
        cost = estimate_llm_cost_spark("Test prompt " * 100, "some-unknown-model")
        assert isinstance(cost, int)
        assert cost >= 1


# ── WS1: check_goal_budget ───────────────────────────────────────────

class TestCheckGoalBudget:

    def test_no_goal_id_always_allowed(self):
        from integrations.agent_engine.budget_gate import check_goal_budget
        allowed, remaining, reason = check_goal_budget(None, 100)
        assert allowed is True
        assert reason == 'no_goal_constraint'

    def test_empty_goal_id_always_allowed(self):
        from integrations.agent_engine.budget_gate import check_goal_budget
        allowed, remaining, reason = check_goal_budget('', 100)
        assert allowed is True

    def test_insufficient_budget_blocked(self):
        """Goal with insufficient budget should be blocked."""
        mock_db = MagicMock()
        mock_goal = MagicMock()
        mock_goal.spark_budget = 10
        mock_goal.spark_spent = 8
        mock_db.query.return_value.filter_by.return_value.with_for_update.return_value.first.return_value = mock_goal

        mock_models = MagicMock()
        mock_models.get_db.return_value = mock_db

        with patch.dict('sys.modules', {'integrations.social.models': mock_models}):
            from importlib import reload
            import integrations.agent_engine.budget_gate as bg
            reload(bg)
            allowed, remaining, reason = bg.check_goal_budget('goal_123', 5)
            assert allowed is False
            assert 'insufficient_budget' in reason

    def test_sufficient_budget_reserved(self):
        """Goal with sufficient budget should be allowed and deducted."""
        mock_db = MagicMock()
        mock_goal = MagicMock()
        mock_goal.spark_budget = 100
        mock_goal.spark_spent = 10
        mock_db.query.return_value.filter_by.return_value.with_for_update.return_value.first.return_value = mock_goal

        mock_models = MagicMock()
        mock_models.get_db.return_value = mock_db

        with patch.dict('sys.modules', {'integrations.social.models': mock_models}):
            from importlib import reload
            import integrations.agent_engine.budget_gate as bg
            reload(bg)
            allowed, remaining, reason = bg.check_goal_budget('goal_123', 5)
            assert allowed is True
            assert 'budget_reserved' in reason

    def test_import_error_does_not_block(self):
        """If social models unavailable, budget check should pass."""
        # Remove social models from path to simulate ImportError
        with patch.dict('sys.modules', {
            'integrations.social.models': None
        }):
            from integrations.agent_engine.budget_gate import check_goal_budget
            allowed, remaining, reason = check_goal_budget('goal_123', 5)
            assert allowed is True


# ── WS1: check_platform_affordability ────────────────────────────────

class TestPlatformAffordability:

    def test_platform_affordability_positive(self):
        """Platform with positive net revenue should be affordable."""
        mock_db = MagicMock()
        with patch('integrations.agent_engine.budget_gate._affordability_cache', {}):
            with patch.dict('sys.modules', {
                'integrations.social.models': MagicMock(
                    get_db=MagicMock(return_value=mock_db)
                ),
                'integrations.agent_engine.revenue_aggregator': MagicMock(
                    query_revenue_streams=MagicMock(return_value={
                        'total_gross': 1000.0,
                        'hosting_payouts': 500.0,
                    })
                ),
            }):
                from importlib import reload
                import integrations.agent_engine.budget_gate as bg
                reload(bg)
                bg._affordability_cache.clear()
                can_afford, details = bg.check_platform_affordability()
                assert can_afford is True
                assert details['net_7d'] > 0

    def test_platform_affordability_negative(self):
        """Platform with negative net flow should not be affordable."""
        mock_db = MagicMock()
        with patch('integrations.agent_engine.budget_gate._affordability_cache', {}):
            with patch.dict('sys.modules', {
                'integrations.social.models': MagicMock(
                    get_db=MagicMock(return_value=mock_db)
                ),
                'integrations.agent_engine.revenue_aggregator': MagicMock(
                    query_revenue_streams=MagicMock(return_value={
                        'total_gross': 100.0,
                        'hosting_payouts': 500.0,
                    })
                ),
            }):
                from importlib import reload
                import integrations.agent_engine.budget_gate as bg
                reload(bg)
                bg._affordability_cache.clear()
                can_afford, details = bg.check_platform_affordability()
                assert can_afford is False
                assert details['net_7d'] < 0


# ── WS1: pre_dispatch_budget_gate ────────────────────────────────────

class TestPreDispatchBudgetGate:

    def test_combined_gate_allows_when_both_pass(self):
        from integrations.agent_engine.budget_gate import pre_dispatch_budget_gate
        with patch('integrations.agent_engine.budget_gate.check_goal_budget',
                   return_value=(True, 90, 'budget_reserved')):
            with patch('integrations.agent_engine.budget_gate.check_platform_affordability',
                       return_value=(True, {'net_7d': 100})):
                allowed, reason = pre_dispatch_budget_gate('goal_1', "test prompt")
                assert allowed is True

    def test_combined_gate_blocks_on_budget(self):
        from integrations.agent_engine.budget_gate import pre_dispatch_budget_gate
        with patch('integrations.agent_engine.budget_gate.check_goal_budget',
                   return_value=(False, 0, 'insufficient_budget')):
            allowed, reason = pre_dispatch_budget_gate('goal_1', "test prompt")
            assert allowed is False
            assert 'goal_budget_exceeded' in reason

    def test_combined_gate_blocks_on_platform(self):
        from integrations.agent_engine.budget_gate import pre_dispatch_budget_gate
        with patch('integrations.agent_engine.budget_gate.check_goal_budget',
                   return_value=(True, 90, 'budget_reserved')):
            with patch('integrations.agent_engine.budget_gate.check_platform_affordability',
                       return_value=(False, {'net_7d': -100})):
                allowed, reason = pre_dispatch_budget_gate('goal_1', "test prompt")
                assert allowed is False
                assert 'platform_not_affordable' in reason

    def test_no_goal_passes_budget_check(self):
        from integrations.agent_engine.budget_gate import pre_dispatch_budget_gate
        with patch('integrations.agent_engine.budget_gate.check_platform_affordability',
                   return_value=(True, {'net_7d': 100})):
            allowed, reason = pre_dispatch_budget_gate(None, "test prompt")
            assert allowed is True


# ── WS6: Revenue split constants ─────────────────────────────────────

class TestRevenueSplitConstants:

    def test_split_constants_sum_to_one(self):
        from integrations.agent_engine.revenue_aggregator import (
            REVENUE_SPLIT_USERS, REVENUE_SPLIT_INFRA, REVENUE_SPLIT_CENTRAL
        )
        total = REVENUE_SPLIT_USERS + REVENUE_SPLIT_INFRA + REVENUE_SPLIT_CENTRAL
        assert abs(total - 1.0) < 1e-10, f"Split must sum to 1.0, got {total}"

    def test_user_split_is_90_percent(self):
        from integrations.agent_engine.revenue_aggregator import REVENUE_SPLIT_USERS
        assert REVENUE_SPLIT_USERS == 0.90

    def test_infra_split_is_9_percent(self):
        from integrations.agent_engine.revenue_aggregator import REVENUE_SPLIT_INFRA
        assert REVENUE_SPLIT_INFRA == 0.09

    def test_central_split_is_1_percent(self):
        from integrations.agent_engine.revenue_aggregator import REVENUE_SPLIT_CENTRAL
        assert REVENUE_SPLIT_CENTRAL == 0.01

    def test_ad_service_imports_central_constant(self):
        from integrations.social.ad_service import HOSTER_REVENUE_SHARE
        assert HOSTER_REVENUE_SHARE == 0.90, f"Expected 0.90, got {HOSTER_REVENUE_SHARE}"

    def test_hosting_service_imports_central_constant(self):
        from integrations.social.hosting_reward_service import HOSTER_REVENUE_SHARE
        assert HOSTER_REVENUE_SHARE == 0.90, f"Expected 0.90, got {HOSTER_REVENUE_SHARE}"

    def test_unwitnessed_share_below_user_share(self):
        from integrations.social.ad_service import HOSTER_REVENUE_SHARE, HOSTER_UNWITNESSED_SHARE
        assert HOSTER_UNWITNESSED_SHARE < HOSTER_REVENUE_SHARE

    def test_query_revenue_streams_returns_three_shares(self):
        """query_revenue_streams should return user_pool, infra_pool, central shares."""
        mock_db = MagicMock()
        # Mock APIUsageLog, AdUnit, HostingReward queries
        mock_db.query.return_value.filter.return_value.scalar.return_value = 1000.0

        from integrations.agent_engine.revenue_aggregator import query_revenue_streams
        result = query_revenue_streams(mock_db, period_days=7)
        assert 'user_pool_share' in result
        assert 'infra_pool_share' in result
        assert 'central_share' in result
        assert 'platform_share' in result


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
