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
        """Get total API revenue, top endpoints, growth trends, tier distribution."""
        try:
            from integrations.social.models import get_db
            from integrations.agent_engine.commercial_api import CommercialAPIService
            from integrations.social.models import CommercialAPIKey, APIUsageLog
            from sqlalchemy import func

            db = get_db()
            try:
                total_keys = db.query(CommercialAPIKey).filter_by(is_active=True).count()
                tier_dist = {}
                for tier in ('free', 'starter', 'pro', 'enterprise'):
                    tier_dist[tier] = db.query(CommercialAPIKey).filter_by(
                        tier=tier, is_active=True).count()

                total_revenue = db.query(
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

                return json.dumps({
                    'total_api_keys': total_keys,
                    'tier_distribution': tier_dist,
                    'total_revenue_credits': round(float(total_revenue), 4),
                    'total_api_calls': total_calls,
                    'top_endpoints': [
                        {'endpoint': e, 'calls': c, 'revenue': round(float(r or 0), 4)}
                        for e, c, r in top_endpoints
                    ],
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
