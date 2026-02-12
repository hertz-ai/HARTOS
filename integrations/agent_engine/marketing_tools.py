"""
Unified Agent Goal Engine - Marketing Tools (Tier 2)

These tools are loaded ONLY when the agent is working on a marketing goal.
They wrap existing CampaignService, AdService, PostService, and channel adapters.

Tier 1 (Default): google_search, text_2_image, delegate_to_specialist, etc.
Tier 2 (Category): create_social_post, create_campaign, create_ad, post_to_channel
Tier 3 (Runtime): delegate_to_specialist finds agents with needed skills via A2A
"""
import json
import logging
from typing import Annotated, Optional

logger = logging.getLogger('hevolve_social')


def register_marketing_tools(helper, assistant, user_id: str):
    """Register marketing-specific tools with the agent (Tier 2).

    These wrap existing services — no new logic, just tool interfaces
    that let the agent use Campaign/Ad/Post services and channel adapters.

    Args:
        helper: AutoGen helper agent (registers for LLM)
        assistant: AutoGen assistant agent (registers for execution)
        user_id: Current user ID for ownership
    """

    def create_social_post(
        title: Annotated[str, "Post title"],
        content: Annotated[str, "Post content/body"],
        community_id: Annotated[Optional[str], "Community to post in (optional)"] = None,
        media_url: Annotated[Optional[str], "URL of media attachment (optional)"] = None,
    ) -> str:
        """Create a post on the Hevolve social platform."""
        try:
            from integrations.social.models import get_db, Post
            db = get_db()
            try:
                post = Post(
                    author_id=str(user_id),
                    title=title,
                    content=content,
                    community_id=community_id,
                    media_url=media_url or '',
                )
                db.add(post)
                db.commit()
                result = post.to_dict()
                return json.dumps({'success': True, 'post': result})
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    def create_campaign(
        name: Annotated[str, "Campaign name"],
        description: Annotated[str, "Campaign description and strategy"],
        campaign_type: Annotated[str, "Campaign type: awareness|engagement|conversion|retention"] = 'awareness',
        target_communities: Annotated[Optional[str], "Comma-separated community IDs to target"] = None,
        budget: Annotated[int, "Spark budget for this campaign"] = 50,
    ) -> str:
        """Create a marketing campaign using the Campaign Service."""
        try:
            from integrations.social.campaign_service import CampaignService
            from integrations.social.models import get_db
            db = get_db()
            try:
                targets = [t.strip() for t in target_communities.split(',')] if target_communities else []
                result = CampaignService.create_campaign(
                    db,
                    creator_id=str(user_id),
                    name=name,
                    description=description,
                    campaign_type=campaign_type,
                    target_communities=targets,
                    budget_spark=budget,
                )
                db.commit()
                return json.dumps({'success': True, 'campaign': result})
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    def create_ad(
        title: Annotated[str, "Ad title"],
        content: Annotated[str, "Ad content/copy"],
        target_url: Annotated[str, "URL the ad links to"],
        ad_type: Annotated[str, "Ad type: banner|sponsored|native"] = 'native',
        budget: Annotated[int, "Spark budget for this ad"] = 50,
        target_audience: Annotated[Optional[str], "Target audience description"] = None,
    ) -> str:
        """Create a targeted ad unit using the Ad Service."""
        try:
            from integrations.social.ad_service import AdService
            from integrations.social.models import get_db
            db = get_db()
            try:
                result = AdService.create_ad(
                    db,
                    advertiser_id=str(user_id),
                    title=title,
                    content=content,
                    target_url=target_url,
                    ad_type=ad_type,
                    budget_spark=budget,
                    targeting_json={'audience': target_audience} if target_audience else {},
                )
                db.commit()
                return json.dumps({'success': True, 'ad': result})
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    def post_to_channel(
        channel: Annotated[str, "Channel name: twitter|instagram|email|discord|telegram|whatsapp|slack|linkedin|nostr|matrix"],
        content: Annotated[str, "Content to post"],
        media_url: Annotated[Optional[str], "Media URL to include (optional)"] = None,
        extra_config: Annotated[Optional[str], "JSON string with channel-specific config (optional)"] = None,
    ) -> str:
        """Post content to an external channel via the unified channel adapter system.

        Routes to the appropriate channel adapter (Twitter, Instagram, Email, etc.).
        If the channel adapter is not available, delegates to a specialist agent.
        """
        try:
            from integrations.channels.extensions import get_available_adapters
            adapters = get_available_adapters()

            # Find matching adapter
            adapter_name = f"{channel}_adapter"
            adapter_factory = None
            for name, factory in adapters.items():
                if channel.lower() in name.lower():
                    adapter_factory = factory
                    break

            if adapter_factory:
                adapter = adapter_factory()
                config = json.loads(extra_config) if extra_config else {}
                result = adapter.send_message(
                    content=content,
                    media_url=media_url,
                    **config,
                )
                return json.dumps({'success': True, 'channel': channel, 'result': str(result)})
            else:
                return json.dumps({
                    'success': False,
                    'error': f'Channel adapter for {channel} not available. '
                             f'Use delegate_to_specialist to find an agent with {channel} skills.',
                    'available_channels': list(adapters.keys()),
                })
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    def create_referral_campaign(
        name: Annotated[str, "Campaign name"],
        description: Annotated[str, "Campaign description and referral strategy"],
        referral_message: Annotated[str, "Shareable referral message for users"] = "Join me on Hevolve!",
        target_communities: Annotated[Optional[str], "Comma-separated community IDs to target"] = None,
        budget: Annotated[int, "Spark budget for this campaign"] = 100,
    ) -> str:
        """Create a referral-driven growth campaign with auto-generated referral code."""
        try:
            from integrations.social.campaign_service import CampaignService
            from integrations.social.distribution_service import DistributionService
            from integrations.social.models import get_db
            db = get_db()
            try:
                # Generate referral code for the campaign owner
                ref_result = DistributionService.get_or_create_referral_code(db, str(user_id))
                ref_code = ref_result.get('code', '')

                targets = [t.strip() for t in target_communities.split(',')] if target_communities else []
                result = CampaignService.create_campaign(
                    db,
                    creator_id=str(user_id),
                    name=name,
                    description=description,
                    campaign_type='conversion',
                    target_communities=targets,
                    budget_spark=budget,
                )
                # Store referral mechanics in strategy
                if result and isinstance(result, dict):
                    result['referral_code'] = ref_code
                    result['referral_message'] = referral_message
                    result['referral_link'] = f"https://hevolve.ai/join?ref={ref_code}"

                db.commit()
                return json.dumps({'success': True, 'campaign': result})
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    def get_growth_metrics() -> str:
        """Get platform growth metrics including viral coefficient (K factor)."""
        try:
            from integrations.social.models import get_db, User
            from integrations.social.models import Campaign, Referral
            from datetime import datetime, timedelta
            db = get_db()
            try:
                now = datetime.utcnow()
                week_ago = now - timedelta(days=7)

                total_users = db.query(User).count()
                new_users_7d = db.query(User).filter(User.created_at >= week_ago).count()

                # Referral metrics
                total_referrals = db.query(Referral).count()
                recent_referrals = db.query(Referral).filter(
                    Referral.created_at >= week_ago
                ).count()

                # Viral coefficient K = avg_referrals_per_user * conversion_rate
                users_with_referrals = db.query(Referral.referrer_id).distinct().count()
                avg_referrals = total_referrals / max(users_with_referrals, 1)
                conversion_rate = total_referrals / max(total_users, 1)
                k_factor = avg_referrals * conversion_rate

                # Top campaigns
                top_campaigns = db.query(Campaign).order_by(
                    Campaign.created_at.desc()
                ).limit(5).all()

                db.commit()
                return json.dumps({
                    'success': True,
                    'metrics': {
                        'total_users': total_users,
                        'new_users_7d': new_users_7d,
                        'total_referrals': total_referrals,
                        'recent_referrals_7d': recent_referrals,
                        'users_who_referred': users_with_referrals,
                        'avg_referrals_per_referrer': round(avg_referrals, 2),
                        'conversion_rate': round(conversion_rate, 4),
                        'viral_coefficient_k': round(k_factor, 4),
                        'k_status': 'exponential' if k_factor > 1 else 'sub-viral',
                        'top_campaigns': [c.to_dict() for c in top_campaigns],
                    }
                })
            finally:
                db.close()
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)})

    # Register all marketing tools
    tools = [
        ('create_social_post', 'Create a post on the Hevolve social platform for marketing', create_social_post),
        ('create_campaign', 'Create a marketing campaign with strategy, targeting, and budget', create_campaign),
        ('create_ad', 'Create a targeted ad unit with budget and audience targeting', create_ad),
        ('post_to_channel', 'Post content to external channels (Twitter, Instagram, Email, Discord, etc.)', post_to_channel),
        ('create_referral_campaign', 'Create a referral-driven growth campaign with auto-generated referral code', create_referral_campaign),
        ('get_growth_metrics', 'Get platform growth metrics including viral coefficient (K factor)', get_growth_metrics),
    ]

    for name, desc, func in tools:
        helper.register_for_llm(name=name, description=desc)(func)
        assistant.register_for_execution(name=name)(func)

    logger.info(f"Registered {len(tools)} marketing tools for user {user_id}")

    # Register skills so OTHER agents can discover and delegate TO this agent
    try:
        from integrations.internal_comm.internal_agent_communication import register_agent_with_skills
        register_agent_with_skills(f"marketing_{user_id}", [
            {'name': 'social_posting', 'description': 'Create posts on Hevolve platform', 'proficiency': 0.9},
            {'name': 'campaign_management', 'description': 'Create and manage marketing campaigns', 'proficiency': 0.9},
            {'name': 'ad_creation', 'description': 'Create targeted ad units', 'proficiency': 0.9},
            {'name': 'channel_distribution', 'description': 'Post to external channels', 'proficiency': 0.8},
            {'name': 'referral_campaigns', 'description': 'Create referral-driven growth campaigns', 'proficiency': 0.9},
            {'name': 'growth_analytics', 'description': 'Analyze growth metrics and viral coefficient', 'proficiency': 0.85},
        ])
    except Exception as e:
        logger.debug(f"Marketing skill registration skipped: {e}")


def detect_goal_tags(prompt: str) -> list:
    """Detect goal type tags from a prompt for category-based tool loading.

    Returns list of tags like ['marketing'], ['coding'], or [] for general.
    """
    lower = prompt.lower()
    tags = []

    marketing_keywords = [
        'market', 'campaign', 'advertis', 'promotion', 'social media',
        'content marketing', 'brand', 'outbound', 'inbound', 'lead gen',
        'email marketing', 'seo', 'influencer', 'viral', 'engagement',
        'conversion', 'target audience', 'marketing goal', 'ad ', 'ads ',
    ]
    if any(kw in lower for kw in marketing_keywords):
        tags.append('marketing')

    coding_keywords = [
        'github', 'repository', 'codebase', 'refactor', 'implement',
        'bug fix', 'pull request', 'commit', 'branch', 'repo',
    ]
    if any(kw in lower for kw in coding_keywords):
        tags.append('coding')

    ip_keywords = [
        'patent', 'trademark', 'copyright', 'intellectual property',
        'ip protection', 'infringement', 'prior art', 'cease and desist',
        'dmca', 'filing', 'provisional patent', 'claims',
    ]
    if any(kw in lower for kw in ip_keywords):
        tags.append('ip_protection')

    return tags
