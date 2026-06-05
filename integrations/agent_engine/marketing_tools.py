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


# Consent type that gates EXTERNAL publishing. The platform keeps humans in
# control: nothing leaves to a third-party site under the operator's identity
# unless the operator has granted standing ``public_exposure`` consent. The
# tool docstrings have always *promised* this gate ("Gate every EXTERNAL post on
# operator consent before calling"); this makes the promise real + enforced
# instead of prose the LLM may ignore. Grant/revoke via POST /api/social/consent
# or ConsentService.grant_consent — revoking is the kill-switch that stops all
# autonomous external posting immediately.
_EXTERNAL_POST_CONSENT = 'public_exposure'


def _external_post_allowed(user_id: str) -> bool:
    """True iff the operator granted standing ``public_exposure`` consent.

    Single gate for every EXTERNAL channel publish (``post_to_channel``,
    ``post_to_channel_via_browser``). Internal on-platform posts
    (``create_social_post``) are NOT gated — they never leave the platform.

    Fail-closed: any error (no consent row, DB unavailable) denies the external
    post, so the default posture is "do not post off-platform" until a human
    opts in. That is the correct posture for an autonomous agent that can reach
    the public internet under the operator's identity.
    """
    try:
        from integrations.social.consent_service import ConsentService
        from integrations.social.models import db_session
        with db_session() as db:
            return bool(ConsentService.check_consent(
                db, str(user_id), _EXTERNAL_POST_CONSENT, scope='*'))
    except Exception as e:
        logger.warning(
            "external-post consent check failed for user %s (denying): %s",
            user_id, e)
        return False


def _consent_required_result(channel: str) -> str:
    """Canonical 'blocked — needs operator consent' result for external posts."""
    return json.dumps({
        'success': False,
        'ok': False,
        'consent_required': _EXTERNAL_POST_CONSENT,
        'channel': channel,
        'error': (
            f"External posting to {channel} requires standing operator consent "
            f"('{_EXTERNAL_POST_CONSENT}'). Enable autonomous external posting "
            f"via POST /api/social/consent "
            f"{{'consent_type':'{_EXTERNAL_POST_CONSENT}','scope':'*'}}. Until "
            f"then, post on-platform with create_social_post and note the gap."
        ),
    })


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
        content: Annotated[str, "Post content/body — write as one real person to another; never AI/marketing-speak (it gets scrolled past)"],
        community_id: Annotated[Optional[str], "Community to post in (optional)"] = None,
        media_url: Annotated[Optional[str], "URL of media attachment (optional)"] = None,
    ) -> str:
        """Create a post on the HART social platform."""
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
        content: Annotated[str, "Ad content/copy — write like a person recommending something real; never AI/marketing-speak"],
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
        content: Annotated[str, "Content to post — write as one real person to another; never AI/marketing-speak (it gets scrolled past)"],
        media_url: Annotated[Optional[str], "Media URL to include (optional)"] = None,
        extra_config: Annotated[Optional[str], "JSON string with channel-specific config (optional)"] = None,
    ) -> str:
        """Post content to an external channel via the unified channel adapter system.

        Routes to the appropriate channel adapter (Twitter, Instagram, Email, etc.).
        If the channel adapter is not available, delegates to a specialist agent.

        Gated on standing operator ``public_exposure`` consent — returns a
        consent_required result (no post) until a human opts in.
        """
        if not _external_post_allowed(user_id):
            return _consent_required_result(channel)
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
        referral_message: Annotated[str, "Shareable referral message for users"] = "Join me on HART!",
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
                # Store referral mechanics in strategy.  Use the canonical
                # ``invite_share_url`` builder (single source of truth) so
                # G1's Invite_Friend tool, this referral campaign, and any
                # future invite surface all emit the same shape.  Honors
                # ``HEVOLVE_INVITE_BASE_URL`` env override automatically.
                if result and isinstance(result, dict):
                    from integrations.social.distribution_service import (
                        invite_share_url,
                    )
                    result['referral_code'] = ref_code
                    result['referral_message'] = referral_message
                    result['referral_link'] = (
                        invite_share_url(ref_code) if ref_code else ''
                    )

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

    def record_demo_video(
        duration_s: Annotated[int, "Seconds to record (2-60). Open + arrange the app you want to demo FIRST."] = 15,
        fps: Annotated[int, "Frames per second (5-12 is plenty for a UI demo)."] = 8,
    ) -> str:
        """Record a short screen-capture DEMO VIDEO of whatever is on screen right
        now (e.g. Nunba / HART OS running) and return the saved file path.

        Arrange the demo BEFORE calling: open the Nunba window (ideally maximized),
        and either start a scripted interaction with the desktop executor or have
        the thing you want to show already visible.  This captures the primary
        screen for `duration_s` and assembles a shareable mp4 (gif fallback).

        Then publish it: pass the returned ``path`` as ``media_url`` to
        ``post_to_channel(...)`` (external) or ``create_social_post(...)`` (platform).
        Returns JSON {ok, path, format, frames, fps, duration_s} or {ok, error}.
        """
        try:
            from integrations.remote_desktop.frame_capture import FrameCapture, FrameConfig
            _fps = max(1, min(int(fps), 30))
            _dur = max(2, min(int(duration_s), 60))
            cap = FrameCapture(FrameConfig(max_fps=_fps, scale_factor=0.75))
            res = cap.record_to_video(duration_s=_dur, fps=_fps)
            return json.dumps(res)
        except Exception as e:
            return json.dumps({'ok': False, 'error': str(e)})

    def post_to_channel_via_browser(
        channel: Annotated[str, "Platform with a web composer the user is logged into: twitter|linkedin|reddit|hackernews"],
        content: Annotated[Optional[str], "Text to post (defaults to the canonical body for that channel from marketing/intents)"] = None,
    ) -> str:
        """Post to a channel through the user's LOGGED-IN BROWSER via the VLM loop —
        the credential-free path for channels that have no API token.  Generic
        across every platform in marketing/intents (NOT LinkedIn-only): it opens
        the platform's composer URL, types the content, and clicks Post.

        Use this when post_to_channel reports the adapter has no credentials but
        the user is logged into that site in their desktop browser.  Gated on
        standing operator ``public_exposure`` consent — returns a consent_required
        result (no post) until a human opts in.  Returns JSON
        {ok, platform, code, status, detail}.
        """
        if not _external_post_allowed(user_id):
            return _consent_required_result(channel)
        try:
            from integrations.marketing.browser_poster import post_to_platform_via_browser
            return json.dumps(post_to_platform_via_browser(
                channel, body=content, user_id=str(user_id)))
        except Exception as e:
            return json.dumps({'ok': False, 'error': str(e)})

    # Register all marketing tools
    tools = [
        ('create_social_post', 'Create a post on the HART social platform for marketing', create_social_post),
        ('create_campaign', 'Create a marketing campaign with strategy, targeting, and budget', create_campaign),
        ('create_ad', 'Create a targeted ad unit with budget and audience targeting', create_ad),
        ('post_to_channel', 'Post content to external channels (Twitter, Instagram, Email, Discord, etc.)', post_to_channel),
        ('create_referral_campaign', 'Create a referral-driven growth campaign with auto-generated referral code', create_referral_campaign),
        ('get_growth_metrics', 'Get platform growth metrics including viral coefficient (K factor)', get_growth_metrics),
        ('record_demo_video', 'Record a short screen demo video of the app running; returns a file path to attach via post_to_channel/create_social_post media_url', record_demo_video),
        ('post_to_channel_via_browser', 'Post to a channel via the logged-in browser (VLM loop) when it has no API token — generic across twitter/linkedin/reddit/hackernews', post_to_channel_via_browser),
    ]

    for name, desc, func in tools:
        helper.register_for_llm(name=name, description=desc)(func)
        assistant.register_for_execution(name=name)(func)

    logger.info(f"Registered {len(tools)} marketing tools for user {user_id}")

    # Register skills so OTHER agents can discover and delegate TO this agent
    try:
        from integrations.internal_comm.internal_agent_communication import register_agent_with_skills
        register_agent_with_skills(f"marketing_{user_id}", [
            {'name': 'social_posting', 'description': 'Create posts on HART platform', 'proficiency': 0.9},
            {'name': 'campaign_management', 'description': 'Create and manage marketing campaigns', 'proficiency': 0.9},
            {'name': 'ad_creation', 'description': 'Create targeted ad units', 'proficiency': 0.9},
            {'name': 'channel_distribution', 'description': 'Post to external channels', 'proficiency': 0.8},
            {'name': 'referral_campaigns', 'description': 'Create referral-driven growth campaigns', 'proficiency': 0.9},
            {'name': 'growth_analytics', 'description': 'Analyze growth metrics and viral coefficient', 'proficiency': 0.85},
            {'name': 'demo_recording', 'description': 'Record screen demo videos of apps running for marketing', 'proficiency': 0.8},
        ])
    except Exception as e:
        logger.debug(f"Marketing skill registration skipped: {e}")


def detect_goal_tags(prompt) -> list:
    """Detect goal type tags from a prompt for category-based tool loading.

    Returns list of tags like ['marketing'], ['coding'], or [] for general.

    Accepts any input — the autogen agent-creation path
    (``create_recipe.py:1735``) sometimes hands HARTOS's own
    ``helper.Action`` object (``helper.py:1363``, the multi-step task
    tracker) instead of a raw string.  Without coercion,
    ``prompt.lower()`` raises ``AttributeError: 'Action' object has no
    attribute 'lower'`` and the surrounding ``except`` swallows it
    silently — leaving the agent with NO goal-specific tools
    registered, so the LLM produces prose instead of taking real
    actions.  Root cause for ~6+ weeks of marketing goals "completing"
    without any outreach side-effects.
    """
    lower = str(prompt).lower()
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

    self_build_keywords = [
        'self-build', 'self_build', 'nixos', 'nix package', 'install package',
        'runtime.nix', 'os rebuild', 'nixos-rebuild', 'system package',
        'hart-pkg', 'nix-env', 'system generation', 'rollback generation',
    ]
    if any(kw in lower for kw in self_build_keywords):
        tags.append('self_build')

    outreach_keywords = [
        'outreach', 'prospect', 'cold email', 'follow up', 'follow-up',
        'sales email', 'pipeline', 'crm', 'lead gen', 'deal stage',
        'send email to', 'contact ', 'outbound email', 'drip', 'sequence',
        'partnership outreach', 'b2b', 'sales funnel',
    ]
    if any(kw in lower for kw in outreach_keywords):
        tags.append('outreach')

    sales_keywords = [
        'sales agent', 'marketing agent', 'flywheel', 'user journey',
        'a/b test', 'ab test', 'prospect journey', 'sales pipeline',
        'meeting schedule', 'deal close', 'partner onboard',
        'channel outreach', 'multi-channel', 'nurture',
    ]
    if any(kw in lower for kw in sales_keywords):
        tags.append('sales')

    return tags
