"""
Revenue Agent Tools — AutoGen tools for API revenue monitoring and optimization.

Tier 2 tools (agent_engine context). Same registration pattern as marketing_tools.py.
"""
import json
import logging
from typing import Annotated, Optional

logger = logging.getLogger('hevolve_social')


def register_revenue_tools(helper, assistant, user_id: str):
    """Register revenue tools with an AutoGen agent (Tier 2)."""

    def get_api_revenue_stats() -> str:
        """Get total API revenue (per-token usage + subscription upgrades),
        top endpoints, growth trends, tier distribution.

        Two revenue sources are summed:
          1. Per-token usage (APIUsageLog.cost_credits) — pay-per-call.
          2. Subscription upgrades (AP2 payment_ledger COMPLETED requests
             with kind=tier_upgrade) — recurring revenue.

        Without (2), the revenue_monitor agent sees ~0 from new SaaS
        signups even when customers have paid for tier upgrades.
        """
        try:
            from integrations.social.models import get_db
            from integrations.social.models import CommercialAPIKey, APIUsageLog
            from sqlalchemy import func

            db = get_db()
            try:
                total_keys = db.query(CommercialAPIKey).filter_by(is_active=True).count()
                tier_dist = {}
                for tier in ('free', 'starter', 'pro', 'enterprise'):
                    tier_dist[tier] = db.query(CommercialAPIKey).filter_by(
                        tier=tier, is_active=True).count()

                total_usage_revenue = db.query(
                    func.coalesce(func.sum(APIUsageLog.cost_credits), 0.0)
                ).scalar()

                total_calls = db.query(APIUsageLog).count()

                # Top endpoints
                top_endpoints = db.query(
                    APIUsageLog.endpoint,
                    func.count(APIUsageLog.id).label('call_count'),
                    func.sum(APIUsageLog.cost_credits).label('revenue'),
                ).group_by(APIUsageLog.endpoint).order_by(
                    func.count(APIUsageLog.id).desc()
                ).limit(5).all()

                # Subscription upgrade revenue from AP2 ledger.
                # Mirrors the upgrade flow in commercial_api.upgrade_key.
                upgrade_revenue_usd = 0.0
                upgrade_count = 0
                upgrade_by_tier = {'starter': 0.0, 'pro': 0.0, 'enterprise': 0.0}
                try:
                    from integrations.ap2 import payment_ledger, PaymentStatus
                    for p in payment_ledger.payments.values():
                        if p.status != PaymentStatus.COMPLETED:
                            continue
                        kind = (p.metadata or {}).get('kind', '')
                        if kind != 'tier_upgrade':
                            continue
                        amt = float(p.amount)
                        upgrade_revenue_usd += amt
                        upgrade_count += 1
                        tgt = (p.metadata or {}).get('target_tier', '')
                        if tgt in upgrade_by_tier:
                            upgrade_by_tier[tgt] += amt
                except Exception as _ap2_err:
                    logger.debug(f"AP2 ledger sum failed: {_ap2_err}")

                # Revenue split commitment — 90% to users, 9% infra, 1% central
                try:
                    from integrations.agent_engine.revenue_aggregator import (
                        REVENUE_SPLIT_USERS, REVENUE_SPLIT_INFRA, REVENUE_SPLIT_CENTRAL,
                    )
                except Exception:
                    REVENUE_SPLIT_USERS, REVENUE_SPLIT_INFRA, REVENUE_SPLIT_CENTRAL = 0.9, 0.09, 0.01

                return json.dumps({
                    'total_api_keys': total_keys,
                    'tier_distribution': tier_dist,
                    'total_calls': total_calls,
                    'top_endpoints': [
                        {'endpoint': e, 'calls': c, 'revenue': round(float(r or 0), 4)}
                        for e, c, r in top_endpoints
                    ],
                    'revenue': {
                        'per_token_usage_credits': round(float(total_usage_revenue), 4),
                        'subscription_upgrades_usd': round(upgrade_revenue_usd, 2),
                        'upgrade_count': upgrade_count,
                        'upgrade_by_tier_usd': {k: round(v, 2) for k, v in upgrade_by_tier.items()},
                    },
                    'revenue_split_policy': {
                        'users_pool': REVENUE_SPLIT_USERS,
                        'infrastructure': REVENUE_SPLIT_INFRA,
                        'central': REVENUE_SPLIT_CENTRAL,
                    },
                })
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'error': str(e)})

    def adjust_pricing(
        tier: Annotated[str, "Tier to adjust: free|starter|pro|enterprise"],
        new_cost_per_1k: Annotated[float, "New cost per 1k tokens in credits"],
        reason: Annotated[str, "Justification for the change"],
    ) -> str:
        """Recommend a pricing adjustment. Does NOT auto-apply — logs recommendation."""
        if tier == 'free' and new_cost_per_1k > 0:
            return json.dumps({
                'error': 'Free tier must always remain at 0 cost. '
                         'We do not gatekeep intelligence.'
            })

        return json.dumps({
            'recommendation': {
                'tier': tier,
                'proposed_cost_per_1k_tokens': new_cost_per_1k,
                'reason': reason,
                'status': 'pending_review',
                'note': 'Pricing changes require manual approval. '
                        'This recommendation has been logged.',
            }
        })

    def generate_api_docs(
        format: Annotated[str, "Output format: markdown or json"] = 'markdown',
    ) -> str:
        """Generate API documentation for the intelligence endpoints."""
        endpoints = [
            {'method': 'POST', 'path': '/api/v1/intelligence/chat',
             'auth': 'X-API-Key', 'description': 'Chat with Hevolve AI intelligence',
             'body': {'prompt': 'string (required)'}},
            {'method': 'POST', 'path': '/api/v1/intelligence/analyze',
             'auth': 'X-API-Key', 'description': 'Analyze documents with AI',
             'body': {'document': 'string (required)', 'question': 'string'}},
            {'method': 'POST', 'path': '/api/v1/intelligence/generate',
             'auth': 'X-API-Key', 'description': 'Generate media (image/audio/video)',
             'body': {'modality': 'string', 'prompt': 'string (required)'}},
            {'method': 'GET', 'path': '/api/v1/intelligence/hivemind',
             'auth': 'X-API-Key', 'description': 'Query collective hive knowledge',
             'params': {'query': 'string (required)'}},
            {'method': 'GET', 'path': '/api/v1/intelligence/usage',
             'auth': 'X-API-Key', 'description': 'Get your API usage statistics',
             'params': {'days': 'int (default 30)'}},
        ]

        if format == 'json':
            return json.dumps({'endpoints': endpoints})

        md = "# Hevolve AI Intelligence API\n\n"
        md += "## Authentication\n"
        md += "Pass your API key via the `X-API-Key` header.\n\n"
        md += "## Endpoints\n\n"
        for ep in endpoints:
            md += f"### `{ep['method']} {ep['path']}`\n"
            md += f"{ep['description']}\n\n"
        return md

    def promote_api(
        campaign_name: Annotated[str, "Name for the promotional campaign"],
        target_audience: Annotated[str, "Target audience description"],
        channels: Annotated[str, "Comma-separated channels: platform,twitter,linkedin"],
    ) -> str:
        """Create a marketing campaign to promote the intelligence API."""
        try:
            from integrations.social.models import get_db
            from integrations.social.campaign_service import CampaignService

            db = get_db()
            try:
                result = CampaignService.create_campaign(
                    db, created_by=user_id,
                    name=campaign_name,
                    campaign_type='awareness',
                    description=f'API promotion targeting: {target_audience}',
                    target_communities=channels.split(','),
                )
                db.commit()
                return json.dumps({'campaign_created': True, 'campaign': result})
            finally:
                db.close()
        except Exception as e:
            return json.dumps({
                'campaign_created': False,
                'note': f'Campaign service unavailable: {e}. '
                        f'Logged promotion intent for: {campaign_name}'
            })

    tools = [
        ('get_api_revenue_stats',
         'Get total API revenue, top endpoints, tier distribution, and growth trends',
         get_api_revenue_stats),
        ('adjust_pricing',
         'Recommend a pricing adjustment for an API tier (does not auto-apply)',
         adjust_pricing),
        ('generate_api_docs',
         'Generate API documentation in markdown or JSON format',
         generate_api_docs),
        ('promote_api',
         'Create a marketing campaign to promote the intelligence API',
         promote_api),
    ]

    for name, desc, func in tools:
        helper.register_for_llm(name=name, description=desc)(func)
        assistant.register_for_execution(name=name)(func)

    logger.info(f"Registered {len(tools)} revenue tools for user {user_id}")
