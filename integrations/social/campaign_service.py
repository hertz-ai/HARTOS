"""
HevolveSocial - Campaign Service ("Make Me Viral")
Users deploy their own trained agents to auto-market products.
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from .models import (
    User, Post, Campaign, CampaignAction, ResonanceWallet,
)
from .resonance_engine import ResonanceService

logger = logging.getLogger('hevolve_social')

# Campaign Spark costs
CAMPAIGN_COSTS = {
    'generate_strategy': 10,
    'post': 15,
    'comment': 5,
    'boost': 0,  # variable, handled by boost system
}

# Rate limits
MAX_CAMPAIGN_POSTS_PER_DAY = 3


class CampaignService:

    @staticmethod
    def create_campaign(db: Session, owner_id: str, name: str,
                        description: str = '', goal: str = 'awareness',
                        product_url: str = '', product_description: str = '',
                        agent_id: str = None,
                        target_regions: List[str] = None,
                        target_communities: List[str] = None,
                        total_spark_budget: int = 100) -> Dict:
        """Create a new campaign."""
        campaign = Campaign(
            owner_id=owner_id,
            name=name,
            description=description,
            goal=goal,
            product_url=product_url,
            product_description=product_description,
            agent_id=agent_id,
            status='draft',
            target_regions=json.dumps(target_regions or []),
            target_communities=json.dumps(target_communities or []),
            total_spark_budget=total_spark_budget,
            spark_spent=0,
            impressions=0,
            clicks=0,
            conversions=0,
        )
        db.add(campaign)
        db.flush()
        return campaign.to_dict()

    @staticmethod
    def get_campaign(db: Session, campaign_id: str) -> Optional[Dict]:
        campaign = db.query(Campaign).filter_by(id=campaign_id).first()
        if not campaign:
            return None
        result = campaign.to_dict()
        # Add action count
        result['action_count'] = db.query(func.count(CampaignAction.id)).filter_by(
            campaign_id=campaign_id
        ).scalar() or 0
        return result

    @staticmethod
    def list_campaigns(db: Session, owner_id: str = None,
                       status: str = None,
                       limit: int = 25, offset: int = 0) -> List[Dict]:
        q = db.query(Campaign)
        if owner_id:
            q = q.filter_by(owner_id=owner_id)
        if status:
            q = q.filter_by(status=status)
        campaigns = q.order_by(desc(Campaign.created_at)).offset(offset).limit(limit).all()
        return [c.to_dict() for c in campaigns]

    @staticmethod
    def update_campaign(db: Session, campaign_id: str, owner_id: str,
                        updates: Dict) -> Optional[Dict]:
        """Update campaign settings."""
        campaign = db.query(Campaign).filter_by(id=campaign_id, owner_id=owner_id).first()
        if not campaign:
            return None

        allowed = ['name', 'description', 'status', 'product_url', 'product_description',
                    'total_spark_budget']
        for key in allowed:
            if key in updates:
                setattr(campaign, key, updates[key])

        if 'target_regions' in updates:
            campaign.target_regions = json.dumps(updates['target_regions'])
        if 'target_communities' in updates:
            campaign.target_communities = json.dumps(updates['target_communities'])

        # Status transitions
        if updates.get('status') == 'active' and not campaign.started_at:
            campaign.started_at = datetime.utcnow()

        db.flush()
        return campaign.to_dict()

    @staticmethod
    def generate_strategy(db: Session, campaign_id: str,
                          owner_id: str) -> Optional[Dict]:
        """Generate a marketing strategy using the campaign's agent.
        Costs Spark and calls the agent's LLM."""
        campaign = db.query(Campaign).filter_by(id=campaign_id, owner_id=owner_id).first()
        if not campaign:
            return None

        # Charge strategy generation cost
        cost = CAMPAIGN_COSTS['generate_strategy']
        success, _ = ResonanceService.spend_spark(
            db, owner_id, cost, 'campaign_strategy', campaign_id,
            f'Strategy generation for {campaign.name}'
        )
        if not success:
            return {'error': 'Insufficient Spark', 'cost': cost}

        campaign.spark_spent = (campaign.spark_spent or 0) + cost

        # Generate strategy placeholder (would call agent's LLM in production)
        strategy = {
            'content_themes': [
                f'Highlight key features of {campaign.product_description or campaign.name}',
                'Share user testimonials and success stories',
                'Demonstrate unique value proposition',
            ],
            'posting_schedule': [
                {'day': 1, 'action': 'introduction_post', 'target': 'main_feed'},
                {'day': 2, 'action': 'feature_highlight', 'target': 'relevant_communities'},
                {'day': 3, 'action': 'engagement_post', 'target': 'target_regions'},
            ],
            'engagement_plan': 'Comment on trending posts in target communities, respond to all replies promptly',
            'estimated_reach': 500,
            'estimated_duration_days': 7,
        }

        campaign.strategy_json = json.dumps(strategy)
        db.flush()

        return {
            'strategy': strategy,
            'spark_cost': cost,
            'campaign_id': campaign_id,
        }

    @staticmethod
    def execute_campaign_step(db: Session, campaign_id: str,
                              owner_id: str) -> Optional[Dict]:
        """Execute the next step in a campaign.
        Rate limited: max 3 posts/day/campaign."""
        campaign = db.query(Campaign).filter_by(
            id=campaign_id, owner_id=owner_id, status='active'
        ).first()
        if not campaign:
            return None

        # Check daily rate limit
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0)
        today_posts = db.query(func.count(CampaignAction.id)).filter(
            CampaignAction.campaign_id == campaign_id,
            CampaignAction.action_type == 'post',
            CampaignAction.created_at >= today_start,
        ).scalar() or 0

        if today_posts >= MAX_CAMPAIGN_POSTS_PER_DAY:
            return {'error': 'Daily post limit reached', 'limit': MAX_CAMPAIGN_POSTS_PER_DAY}

        # Check budget
        cost = CAMPAIGN_COSTS['post']
        remaining_budget = (campaign.total_spark_budget or 0) - (campaign.spark_spent or 0)
        if remaining_budget < cost:
            return {'error': 'Campaign budget exhausted', 'remaining': remaining_budget}

        # Spend Spark
        success, _ = ResonanceService.spend_spark(
            db, owner_id, cost, 'campaign_action', campaign_id,
            f'Campaign post for {campaign.name}'
        )
        if not success:
            return {'error': 'Insufficient Spark'}

        campaign.spark_spent = (campaign.spark_spent or 0) + cost

        # Record action
        action = CampaignAction(
            campaign_id=campaign_id,
            agent_id=campaign.agent_id,
            action_type='post',
            content_generated=f'[Auto-generated campaign content for: {campaign.name}]',
            spark_cost=cost,
        )
        db.add(action)
        campaign.impressions = (campaign.impressions or 0) + 1

        # Auto-pause if downvote ratio too high
        if campaign.impressions > 10:
            total_actions = db.query(func.count(CampaignAction.id)).filter_by(
                campaign_id=campaign_id
            ).scalar() or 0
            # Simple heuristic: if we've spent >80% budget with low engagement
            if (campaign.spark_spent or 0) > (campaign.total_spark_budget or 0) * 0.8:
                campaign.status = 'paused'

        # Award agent XP for campaign action
        if campaign.agent_id:
            ResonanceService.award_xp(
                db, campaign.agent_id, 5, 'campaign_action', campaign_id,
                'Campaign action XP'
            )

        db.flush()
        return {
            'action': action.to_dict(),
            'spark_cost': cost,
            'budget_remaining': (campaign.total_spark_budget or 0) - (campaign.spark_spent or 0),
            'impressions': campaign.impressions,
        }

    @staticmethod
    def delete_campaign(db: Session, campaign_id: str,
                        owner_id: str) -> Optional[Dict]:
        """Delete/cancel a campaign. Refund unspent Spark."""
        campaign = db.query(Campaign).filter_by(
            id=campaign_id, owner_id=owner_id
        ).first()
        if not campaign:
            return None

        # Refund unspent budget
        unspent = max(0, (campaign.total_spark_budget or 0) - (campaign.spark_spent or 0))
        if unspent > 0:
            ResonanceService.award_spark(
                db, owner_id, unspent, 'campaign_refund', campaign_id,
                f'Campaign refund for {campaign.name}'
            )

        campaign.status = 'completed'
        campaign.ends_at = datetime.utcnow()
        db.flush()

        return {
            'deleted': True,
            'spark_refunded': unspent,
        }

    @staticmethod
    def get_leaderboard(db: Session, limit: int = 20) -> List[Dict]:
        """Get campaign leaderboard by ROI (conversions per spark spent)."""
        campaigns = db.query(Campaign).filter(
            Campaign.status.in_(['active', 'completed']),
            Campaign.spark_spent > 0,
        ).all()

        # Calculate ROI
        ranked = []
        for c in campaigns:
            roi = (c.conversions or 0) / max(c.spark_spent, 1)
            ranked.append({
                'campaign_id': c.id,
                'name': c.name,
                'goal': c.goal,
                'owner_id': c.owner_id,
                'impressions': c.impressions or 0,
                'clicks': c.clicks or 0,
                'conversions': c.conversions or 0,
                'spark_spent': c.spark_spent or 0,
                'roi': round(roi, 4),
                'status': c.status,
            })

        ranked.sort(key=lambda x: -x['roi'])
        return ranked[:limit]
