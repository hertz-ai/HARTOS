"""
Tests for Revenue Pipeline — revenue aggregation, funding threshold,
compute borrowing, Spark settlement, and profit distribution.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from unittest.mock import patch, MagicMock


# ─── Revenue Stream Aggregation Tests ───

class TestRevenueStreams:
    def test_revenue_streams_aggregation(self):
        """get_revenue_streams returns all expected keys."""
        from integrations.agent_engine.revenue_aggregator import RevenueAggregator

        db = MagicMock()
        # Mock queries: sum returns scalar
        db.query.return_value.filter.return_value.scalar.return_value = 500.0

        result = RevenueAggregator.get_revenue_streams(db, period_days=30)
        assert 'api_revenue' in result
        assert 'ad_revenue' in result
        assert 'hosting_payouts' in result
        assert 'total_gross' in result
        assert 'platform_share' in result
        assert result['period_days'] == 30


# ─── Funding Threshold Tests ───

class TestFundingThreshold:
    def test_funding_threshold_not_reached(self):
        """No funding when platform excess below threshold."""
        from integrations.agent_engine.revenue_aggregator import RevenueAggregator

        db = MagicMock()
        # Mock get_revenue_streams to return low revenue
        with patch.object(RevenueAggregator, 'get_revenue_streams',
                          return_value={
                              'api_revenue': 100.0, 'ad_revenue': 50.0,
                              'hosting_payouts': 80.0, 'total_gross': 150.0,
                              'platform_share': 15.0, 'period_days': 30,
                          }):
            result = RevenueAggregator.check_and_fund_trading(db)
        assert not result.get('funded')

    def test_funding_threshold_exceeded(self):
        """Trading goal created when platform excess exceeds threshold."""
        from integrations.agent_engine.revenue_aggregator import RevenueAggregator

        db = MagicMock()
        # No existing trading goal
        db.query.return_value.filter.return_value.first.return_value = None

        mock_goal_result = {
            'success': True,
            'goal': {'id': 'goal-123', 'goal_type': 'trading'},
        }

        mock_gm = MagicMock()
        mock_gm.create_goal.return_value = mock_goal_result

        with patch.object(RevenueAggregator, 'get_revenue_streams',
                          return_value={
                              'api_revenue': 20000.0, 'ad_revenue': 5000.0,
                              'hosting_payouts': 500.0, 'total_gross': 25000.0,
                              'platform_share': 2500.0, 'period_days': 30,
                          }), \
             patch('integrations.agent_engine.goal_manager.GoalManager',
                   mock_gm):
            result = RevenueAggregator.check_and_fund_trading(db)

        assert result.get('funded')
        assert result.get('amount') > 0


# ─── Compute Borrowing Tests ───

class TestComputeBorrowing:
    def test_compute_borrowing_offer(self):
        """offer_compute stores and returns offer."""
        from integrations.agent_engine.compute_borrowing import ComputeBorrowingService
        db = MagicMock()
        result = ComputeBorrowingService.offer_compute(
            db, 'node-1', {'cpu_pct_free': 60, 'ram_gb_free': 8})
        assert result['success']
        assert result['offer']['node_id'] == 'node-1'

    def test_compute_borrowing_request_no_match(self):
        """request_compute returns no_match when no offers available."""
        from integrations.agent_engine import compute_borrowing
        # Clear any leftover state
        compute_borrowing._compute_offers.clear()

        db = MagicMock()
        result = compute_borrowing.ComputeBorrowingService.request_compute(
            db, 'node-2', 'inference', {'min_cpu_pct': 50, 'min_ram_gb': 16})
        assert not result['matched']

    def test_compute_borrowing_request_with_match(self):
        """request_compute matches available offer."""
        from integrations.agent_engine import compute_borrowing
        compute_borrowing._compute_offers.clear()

        db = MagicMock()
        # First create an offer
        compute_borrowing.ComputeBorrowingService.offer_compute(
            db, 'provider-1', {'cpu_pct_free': 80, 'ram_gb_free': 32})

        # Then request
        result = compute_borrowing.ComputeBorrowingService.request_compute(
            db, 'requester-1', 'inference', {'min_cpu_pct': 50, 'min_ram_gb': 16})
        assert result['matched']
        assert result['provider'] == 'provider-1'

    def test_compute_status(self):
        """get_status returns expected structure."""
        from integrations.agent_engine.compute_borrowing import ComputeBorrowingService
        status = ComputeBorrowingService.get_status()
        assert 'active_offers' in status
        assert 'active_requests' in status
        assert 'total_debt_spark' in status


# ─── Spark Settlement Tests ───

class TestSparkSettlement:
    def test_spark_settlement_provider_not_found(self):
        """Settlement fails if provider node not found."""
        from integrations.agent_engine.compute_borrowing import ComputeBorrowingService
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None

        result = ComputeBorrowingService.settle_compute_debt(
            db, 'debtor-1', 'creditor-1', 50.0)
        assert 'error' in result


# ─── Profit Distribution Tests ───

class TestProfitDistribution:
    def test_profit_distribution_with_profit(self):
        """Profits split 90/9/1 between users, infra, and central."""
        from integrations.agent_engine.revenue_aggregator import RevenueAggregator

        db = MagicMock()
        mock_portfolio = MagicMock()
        mock_portfolio.total_pnl = 1000.0
        db.query.return_value.filter_by.return_value.first.return_value = mock_portfolio

        result = RevenueAggregator.distribute_trading_profits(db, 'port-1')
        assert result['distributed']
        assert result['platform_share'] == 100.0  # 10%
        assert result['provider_share'] == 900.0  # 90%

    def test_profit_distribution_no_profit(self):
        """No distribution when P&L is negative."""
        from integrations.agent_engine.revenue_aggregator import RevenueAggregator

        db = MagicMock()
        mock_portfolio = MagicMock()
        mock_portfolio.total_pnl = -200.0
        db.query.return_value.filter_by.return_value.first.return_value = mock_portfolio

        result = RevenueAggregator.distribute_trading_profits(db, 'port-1')
        assert not result.get('distributed')
        assert result['reason'] == 'no_profit'
