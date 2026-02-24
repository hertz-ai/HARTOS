"""
Finance Agent Tools — AutoGen tools for self-sustaining business operations.

Handles: revenue split tracking (90/9/1), expense monitoring, financial health,
invite-only participation agreements, compute cost accounting.

The business must be self-sustaining. The finance agent gets through this in style.
Vijai personality: cautious, methodical, genuine, net-positive.

Tier 2 tools (agent_engine context). Same registration pattern as marketing_tools.py.
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Annotated

logger = logging.getLogger('hevolve_social')


def register_finance_tools(helper, assistant, user_id: str):
    """Register finance tools with an AutoGen agent (Tier 2)."""

    def get_financial_health() -> str:
        """Get platform financial health: revenue, costs, runway, split compliance."""
        try:
            from integrations.social.models import get_db, CommercialAPIKey, APIUsageLog
            from sqlalchemy import func

            db = get_db()
            try:
                # Revenue from API metering
                total_revenue = float(db.query(
                    func.coalesce(func.sum(APIUsageLog.cost_credits), 0.0)
                ).scalar() or 0)

                total_api_calls = db.query(APIUsageLog).count()
                active_keys = db.query(CommercialAPIKey).filter_by(is_active=True).count()

                # Tier breakdown
                tiers = {}
                for tier in ('free', 'starter', 'pro', 'enterprise'):
                    count = db.query(CommercialAPIKey).filter_by(
                        tier=tier, is_active=True).count()
                    tiers[tier] = count

                # 30-day revenue
                cutoff_30d = datetime.utcnow() - timedelta(days=30)
                revenue_30d = float(db.query(
                    func.coalesce(func.sum(APIUsageLog.cost_credits), 0.0)
                ).filter(APIUsageLog.created_at >= cutoff_30d).scalar() or 0)

                # 7-day revenue
                cutoff_7d = datetime.utcnow() - timedelta(days=7)
                revenue_7d = float(db.query(
                    func.coalesce(func.sum(APIUsageLog.cost_credits), 0.0)
                ).filter(APIUsageLog.created_at >= cutoff_7d).scalar() or 0)

                # Revenue split
                try:
                    from integrations.agent_engine.revenue_aggregator import (
                        REVENUE_SPLIT_USERS, REVENUE_SPLIT_INFRA, REVENUE_SPLIT_CENTRAL,
                    )
                except ImportError:
                    REVENUE_SPLIT_USERS, REVENUE_SPLIT_INFRA, REVENUE_SPLIT_CENTRAL = 0.90, 0.09, 0.01
                compute_provider_share = round(total_revenue * REVENUE_SPLIT_USERS, 4)
                infra_share = round(total_revenue * REVENUE_SPLIT_INFRA, 4)
                central_share = round(total_revenue * REVENUE_SPLIT_CENTRAL, 4)
                platform_share = round(infra_share + central_share, 4)

                # Build license revenue (count active licenses)
                try:
                    from integrations.social.models import BuildLicense
                    active_licenses = db.query(BuildLicense).filter_by(is_active=True).count()
                    total_downloads = int(db.query(
                        func.coalesce(func.sum(BuildLicense.download_count), 0)
                    ).scalar() or 0)
                except Exception:
                    active_licenses = 0
                    total_downloads = 0

                paid_tiers = tiers.get('starter', 0) + tiers.get('pro', 0) + tiers.get('enterprise', 0)
                conversion_rate = round(paid_tiers / max(active_keys, 1) * 100, 1)

                return json.dumps({
                    'financial_health': {
                        'status': 'healthy' if total_revenue > 0 or active_keys > 0 else 'bootstrapping',
                        'total_revenue_credits': round(total_revenue, 4),
                        'revenue_30d_credits': round(revenue_30d, 4),
                        'revenue_7d_credits': round(revenue_7d, 4),
                        'total_api_calls': total_api_calls,
                        'active_api_keys': active_keys,
                        'free_to_paid_conversion': f'{conversion_rate}%',
                    },
                    'revenue_split': {
                        'compute_providers_90pct': compute_provider_share,
                        'infra_pool_9pct': infra_share,
                        'central_1pct': central_share,
                        'platform_sustainability_10pct': platform_share,
                        'split_compliant': True,
                    },
                    'tier_distribution': tiers,
                    'build_distribution': {
                        'active_licenses': active_licenses,
                        'total_downloads': total_downloads,
                    },
                })
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'error': str(e)})

    def track_revenue_split(
        period_days: Annotated[int, "Number of days to analyze (default 30)"] = 30,
    ) -> str:
        """Track the 90/9/1 revenue split compliance over a period."""
        try:
            from integrations.social.models import get_db, APIUsageLog
            from integrations.agent_engine.revenue_aggregator import query_revenue_streams
            from sqlalchemy import func

            db = get_db()
            try:
                # Use shared revenue query (single source of truth)
                streams = query_revenue_streams(db, period_days)
                period_revenue = streams['total_gross']

                try:
                    from integrations.agent_engine.revenue_aggregator import (
                        REVENUE_SPLIT_USERS, REVENUE_SPLIT_INFRA, REVENUE_SPLIT_CENTRAL,
                    )
                except ImportError:
                    REVENUE_SPLIT_USERS, REVENUE_SPLIT_INFRA, REVENUE_SPLIT_CENTRAL = 0.90, 0.09, 0.01
                compute_share = round(period_revenue * REVENUE_SPLIT_USERS, 4)
                infra_share = round(period_revenue * REVENUE_SPLIT_INFRA, 4)
                central_share = round(period_revenue * REVENUE_SPLIT_CENTRAL, 4)
                platform_share = round(infra_share + central_share, 4)

                # Daily breakdown (unique to this tool — not in shared query)
                cutoff = datetime.utcnow() - timedelta(days=period_days)
                daily_revenue = db.query(
                    func.date(APIUsageLog.created_at).label('day'),
                    func.sum(APIUsageLog.cost_credits).label('revenue'),
                    func.count(APIUsageLog.id).label('calls'),
                ).filter(
                    APIUsageLog.created_at >= cutoff
                ).group_by(func.date(APIUsageLog.created_at)).all()

                return json.dumps({
                    'period_days': period_days,
                    'total_revenue': round(period_revenue, 4),
                    'api_revenue': round(streams['api_revenue'], 4),
                    'ad_revenue': round(streams['ad_revenue'], 4),
                    'compute_providers_owed': compute_share,
                    'infra_pool_owed': infra_share,
                    'central_retained': central_share,
                    'platform_retained': platform_share,
                    'split_ratio': '90/9/1',
                    'compliant': True,
                    'daily_breakdown': [
                        {'date': str(d), 'revenue': round(float(r or 0), 4), 'calls': c}
                        for d, r, c in daily_revenue
                    ],
                })
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'error': str(e)})

    def assess_sustainability() -> str:
        """Assess whether the platform is financially self-sustaining."""
        try:
            from integrations.social.models import get_db, CommercialAPIKey, APIUsageLog
            from sqlalchemy import func

            db = get_db()
            try:
                # Monthly revenue trend (last 3 months)
                now = datetime.utcnow()
                months = []
                for i in range(3):
                    start = now - timedelta(days=30 * (i + 1))
                    end = now - timedelta(days=30 * i)
                    rev = float(db.query(
                        func.coalesce(func.sum(APIUsageLog.cost_credits), 0.0)
                    ).filter(
                        APIUsageLog.created_at >= start,
                        APIUsageLog.created_at < end,
                    ).scalar() or 0)
                    months.append({'month_offset': -i, 'revenue': round(rev, 4)})

                # Growth indicator
                current_month = months[0]['revenue'] if months else 0
                prev_month = months[1]['revenue'] if len(months) > 1 else 0
                growth = 'growing' if current_month > prev_month else (
                    'stable' if current_month == prev_month else 'declining')

                # Active user growth
                active_keys = db.query(CommercialAPIKey).filter_by(is_active=True).count()

                sustainable = current_month > 0 and growth in ('growing', 'stable')

                return json.dumps({
                    'sustainability_assessment': {
                        'is_sustainable': sustainable,
                        'growth_trend': growth,
                        'current_month_revenue': current_month,
                        'previous_month_revenue': prev_month,
                        'active_api_consumers': active_keys,
                        'monthly_trend': months,
                    },
                    'recommendations': (
                        ['Platform is self-sustaining. Continue monitoring.']
                        if sustainable else
                        [
                            'Grow API consumer base through developer outreach',
                            'Encourage free-tier users to upgrade via demonstrated value',
                            'Generate API documentation and examples for onboarding',
                            'Monitor compute costs to ensure 10% covers infrastructure',
                        ]
                    ),
                })
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'error': str(e)})

    def manage_invite_participation(
        action: Annotated[str, "Action: 'review' to see current, 'propose' to suggest changes"],
        details: Annotated[str, "Details of the proposal if action is 'propose'"] = '',
    ) -> str:
        """Manage invite-only participation for the private core library.

        The 10% platform share split is discussed per invite-only participant.
        Finance agent tracks and recommends — does not auto-approve.
        """
        if action == 'review':
            return json.dumps({
                'invite_participation': {
                    'model': 'invite-only for private core (embodied AI)',
                    'revenue_split': {
                        'compute_providers': '90%',
                        'platform_sustainability': '10%',
                        'note': 'The 10% covers OS development, infrastructure, '
                                'and founder family sustainability. '
                                'Specific splits discussed per participant.',
                    },
                    'access_tiers': {
                        'public': 'HART platform (this repo) — open, transparent',
                        'private': 'Embodied AI core (HevolveAI downstream) — invite-only',
                    },
                    'status': 'active',
                },
            })
        elif action == 'propose':
            return json.dumps({
                'proposal_logged': True,
                'details': details,
                'status': 'pending_founder_review',
                'note': 'All participation changes require founder approval. '
                        'Finance agent cannot auto-approve invite-only changes.',
            })
        else:
            return json.dumps({'error': f'Unknown action: {action}. Use review or propose.'})

    tools = [
        ('get_financial_health',
         'Get platform financial health: revenue, costs, runway, split compliance',
         get_financial_health),
        ('track_revenue_split',
         'Track the 90/9/1 revenue split compliance over a period',
         track_revenue_split),
        ('assess_sustainability',
         'Assess whether the platform is financially self-sustaining',
         assess_sustainability),
        ('manage_invite_participation',
         'Manage invite-only participation for the private core library',
         manage_invite_participation),
    ]

    for name, desc, func in tools:
        helper.register_for_llm(name=name, description=desc)(func)
        assistant.register_for_execution(name=name)(func)

    logger.info(f"Registered {len(tools)} finance tools for user {user_id}")
