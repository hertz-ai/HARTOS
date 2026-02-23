"""
Revenue Aggregator — connects ad/API revenue to trading agent funding.

Monitors platform revenue streams, automatically funds paper trading
goals when thresholds are exceeded, and tracks profit distribution.

Revenue → Spark accumulation → threshold → fund trading goals → reinvestment.

NOTE: Revenue query logic lives in query_revenue_streams() (shared with
finance_tools.get_financial_health). Do not duplicate DB queries here.
"""
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Configurable via environment
_FUNDING_THRESHOLD = float(os.environ.get('HEVOLVE_REVENUE_FUNDING_THRESHOLD', '1000'))
_FUNDING_ALLOCATION_PCT = float(os.environ.get('HEVOLVE_REVENUE_FUNDING_PCT', '0.10'))
_PROFIT_PLATFORM_SHARE = 0.10  # Platform keeps 10% of trading profits


# ─── Shared revenue query (used by both RevenueAggregator and finance_tools) ──

def query_revenue_streams(db, period_days: int = 30) -> Dict:
    """Core revenue query — single source of truth for revenue data.

    Returns: {period_days, api_revenue, ad_revenue, hosting_payouts,
              total_gross, platform_share}

    Used by: RevenueAggregator.get_revenue_streams(), finance_tools.get_financial_health()
    """
    from sqlalchemy import func
    cutoff = datetime.utcnow() - timedelta(days=period_days)

    result = {
        'period_days': period_days,
        'api_revenue': 0.0,
        'ad_revenue': 0.0,
        'hosting_payouts': 0.0,
        'total_gross': 0.0,
        'platform_share': 0.0,
    }

    # API revenue
    try:
        from integrations.social.models import APIUsageLog
        api_total = db.query(
            func.coalesce(func.sum(APIUsageLog.cost_credits), 0.0)
        ).filter(APIUsageLog.created_at >= cutoff).scalar() or 0.0
        result['api_revenue'] = float(api_total)
    except Exception as e:
        logger.debug(f"API revenue query: {e}")

    # Ad revenue (Spark spent by advertisers)
    try:
        from integrations.social.models import AdUnit
        ad_total = db.query(
            func.coalesce(func.sum(AdUnit.spent_spark), 0)
        ).filter(AdUnit.created_at >= cutoff).scalar() or 0
        result['ad_revenue'] = float(ad_total)
    except Exception as e:
        logger.debug(f"Ad revenue query: {e}")

    # Hosting payouts (outgoing — for tracking net)
    try:
        from integrations.social.models import HostingReward
        hosting_total = db.query(
            func.coalesce(func.sum(HostingReward.amount), 0.0)
        ).filter(HostingReward.created_at >= cutoff).scalar() or 0.0
        result['hosting_payouts'] = float(hosting_total)
    except Exception as e:
        logger.debug(f"Hosting reward query: {e}")

    gross = result['api_revenue'] + result['ad_revenue']
    result['total_gross'] = gross
    result['platform_share'] = gross * 0.10  # Platform's 10%
    return result


class RevenueAggregator:
    """Connects revenue streams to trading goal funding."""

    @staticmethod
    def get_revenue_streams(db, period_days: int = 30) -> Dict:
        """Aggregate all revenue streams over a period.

        Delegates to shared query_revenue_streams() — single source of truth.
        """
        return query_revenue_streams(db, period_days)

    @staticmethod
    def check_and_fund_trading(db) -> Dict:
        """If platform wallet excess > threshold, allocate to trading.

        Creates a new paper trading goal via GoalManager when threshold hit.
        Returns: {funded: bool, amount: float, goal_id: str}
        """
        revenue = RevenueAggregator.get_revenue_streams(db, period_days=30)
        platform_excess = revenue['platform_share'] - revenue['hosting_payouts']

        if platform_excess < _FUNDING_THRESHOLD:
            return {
                'funded': False,
                'platform_excess': round(platform_excess, 2),
                'threshold': _FUNDING_THRESHOLD,
            }

        funding_amount = platform_excess * _FUNDING_ALLOCATION_PCT

        # Check if there's already an auto-funded active trading goal
        try:
            from integrations.social.models import AgentGoal
            existing = db.query(AgentGoal).filter(
                AgentGoal.goal_type == 'trading',
                AgentGoal.status == 'active',
                AgentGoal.created_by == 'revenue_aggregator',
            ).first()
            if existing:
                return {
                    'funded': False,
                    'reason': 'active_trading_goal_exists',
                    'goal_id': existing.id,
                }
        except Exception:
            pass

        # Create trading goal
        try:
            from .goal_manager import GoalManager
            result = GoalManager.create_goal(
                db,
                goal_type='trading',
                title='Revenue-Funded Paper Trading',
                description=(
                    f'Auto-funded from platform revenue excess. '
                    f'Budget: {int(funding_amount)} Spark. '
                    f'Strategy: long_term diversified. '
                    f'Paper trading only — live requires constitutional vote.'
                ),
                config={
                    'strategy': 'long_term',
                    'paper_trading': True,
                    'market': 'crypto',
                    'max_budget': int(funding_amount),
                    'max_loss_pct': 10,
                    'auto_funded': True,
                    'funding_source': 'platform_revenue',
                },
                spark_budget=int(funding_amount),
                created_by='revenue_aggregator',
            )
            if result.get('success'):
                logger.info(
                    f"Revenue aggregator: funded trading goal with "
                    f"{int(funding_amount)} Spark")
                return {
                    'funded': True,
                    'amount': round(funding_amount, 2),
                    'goal_id': result['goal'].get('id'),
                }
        except Exception as e:
            logger.debug(f"Revenue funding failed: {e}")

        return {'funded': False, 'error': 'goal_creation_failed'}

    @staticmethod
    def distribute_trading_profits(db, portfolio_id: str) -> Dict:
        """Record profit distribution from a paper portfolio.

        Paper profits are tracked only. Live profits follow 90/10 split.
        """
        try:
            from integrations.social.models import PaperPortfolio
            portfolio = db.query(PaperPortfolio).filter_by(id=portfolio_id).first()
            if not portfolio:
                return {'error': 'portfolio_not_found'}

            if portfolio.total_pnl <= 0:
                return {'profit': 0.0, 'distributed': False,
                        'reason': 'no_profit'}

            platform_share = portfolio.total_pnl * _PROFIT_PLATFORM_SHARE
            provider_share = portfolio.total_pnl * (1 - _PROFIT_PLATFORM_SHARE)

            return {
                'portfolio_id': portfolio_id,
                'total_pnl': round(portfolio.total_pnl, 2),
                'platform_share': round(platform_share, 2),
                'provider_share': round(provider_share, 2),
                'distributed': True,
                'note': 'Paper trading — profits are simulated',
            }
        except Exception as e:
            return {'error': str(e)}

    @staticmethod
    def get_dashboard(db) -> Dict:
        """Full revenue dashboard for /api/revenue/dashboard."""
        revenue = RevenueAggregator.get_revenue_streams(db)

        # Trading P&L
        trading_pnl = 0.0
        active_portfolios = 0
        try:
            from integrations.social.models import PaperPortfolio
            portfolios = db.query(PaperPortfolio).filter_by(status='active').all()
            active_portfolios = len(portfolios)
            trading_pnl = sum(p.total_pnl or 0 for p in portfolios)
        except Exception:
            pass

        return {
            'revenue': revenue,
            'trading': {
                'active_portfolios': active_portfolios,
                'total_pnl': round(trading_pnl, 2),
            },
            'funding': {
                'threshold': _FUNDING_THRESHOLD,
                'allocation_pct': _FUNDING_ALLOCATION_PCT,
                'platform_excess': round(
                    revenue['platform_share'] - revenue['hosting_payouts'], 2),
            },
        }


# Module singleton
_revenue_aggregator = None


def get_revenue_aggregator() -> RevenueAggregator:
    global _revenue_aggregator
    if _revenue_aggregator is None:
        _revenue_aggregator = RevenueAggregator()
    return _revenue_aggregator
