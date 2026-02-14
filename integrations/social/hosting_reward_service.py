"""
HevolveSocial - Hosting Reward Service
Computes contribution scores, manages visibility tiers,
distributes rewards to peer node operators.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from .models import PeerNode, AdImpression, HostingReward, User
from .resonance_engine import ResonanceService

logger = logging.getLogger('hevolve_social')

# ─── Constants ───

SCORE_WEIGHTS = {
    'uptime_ratio': 100.0,    # uptime 0.0-1.0 * 100 = 0-100 points
    'agent_count': 2.0,        # 2 points per agent hosted
    'post_count': 0.5,         # 0.5 points per post served
    'ad_impressions': 0.1,     # 0.1 points per ad impression served
}

TIER_THRESHOLDS = {
    'standard': 0,
    'featured': 100,
    'priority': 500,
}

HOSTING_MILESTONES = [10, 50, 100, 500]  # agent_count thresholds

HOSTER_REVENUE_SHARE = 0.70


class HostingRewardService:

    # ─── Contribution Scoring ───

    @staticmethod
    def compute_contribution_score(db: Session, node_id: str,
                                    period_days: int = 7) -> Optional[Dict]:
        """Compute and update contribution_score + visibility_tier for a PeerNode."""
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer:
            return None

        uptime = HostingRewardService.compute_uptime_ratio(db, peer)

        # Ad impressions served by this node in the period
        cutoff = datetime.utcnow() - timedelta(days=period_days)
        ad_imp_count = db.query(func.count(AdImpression.id)).filter(
            AdImpression.node_id == node_id,
            AdImpression.created_at >= cutoff,
        ).scalar() or 0

        score = (
            uptime * SCORE_WEIGHTS['uptime_ratio']
            + (peer.agent_count or 0) * SCORE_WEIGHTS['agent_count']
            + (peer.post_count or 0) * SCORE_WEIGHTS['post_count']
            + ad_imp_count * SCORE_WEIGHTS['ad_impressions']
        )

        old_tier = peer.visibility_tier
        peer.contribution_score = round(score, 2)
        peer.visibility_tier = HostingRewardService._determine_tier(score)
        db.flush()

        return {
            'node_id': node_id,
            'score': peer.contribution_score,
            'tier': peer.visibility_tier,
            'previous_tier': old_tier,
            'breakdown': {
                'uptime': round(uptime * SCORE_WEIGHTS['uptime_ratio'], 2),
                'agents': (peer.agent_count or 0) * SCORE_WEIGHTS['agent_count'],
                'posts': (peer.post_count or 0) * SCORE_WEIGHTS['post_count'],
                'ad_impressions': ad_imp_count * SCORE_WEIGHTS['ad_impressions'],
            },
        }

    @staticmethod
    def compute_uptime_ratio(db: Session, peer: PeerNode) -> float:
        """Compute uptime ratio: active=1.0, stale=0.5, dead=0.0."""
        if peer.status == 'active':
            return 1.0
        elif peer.status == 'stale':
            return 0.5
        return 0.0

    @staticmethod
    def _determine_tier(score: float) -> str:
        if score >= TIER_THRESHOLDS['priority']:
            return 'priority'
        elif score >= TIER_THRESHOLDS['featured']:
            return 'featured'
        return 'standard'

    @staticmethod
    def compute_all_scores(db: Session, period_days: int = 7) -> List[Dict]:
        """Batch: compute scores for all active/stale PeerNodes."""
        peers = db.query(PeerNode).filter(
            PeerNode.status.in_(['active', 'stale'])
        ).all()
        results = []
        for peer in peers:
            result = HostingRewardService.compute_contribution_score(
                db, peer.node_id, period_days)
            if result:
                results.append(result)
        return results

    # ─── Reward Distribution ───

    @staticmethod
    def distribute_ad_revenue(db: Session, node_id: str,
                               period: str = 'daily') -> Optional[Dict]:
        """Distribute ad revenue to node operator for a period."""
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer or not peer.node_operator_id:
            return None

        # Count impressions served by this node in the last 24h
        cutoff = datetime.utcnow() - timedelta(hours=24)
        imp_count = db.query(func.count(AdImpression.id)).filter(
            AdImpression.node_id == node_id,
            AdImpression.created_at >= cutoff,
        ).scalar() or 0

        if imp_count == 0:
            return None

        # Calculate revenue: avg CPI * impressions * hoster share
        avg_cpi = db.query(func.avg(AdImpression.ad.has())).scalar() or 0.1
        # Simpler: use default CPI
        revenue_spark = int(imp_count * 0.1 * HOSTER_REVENUE_SHARE)
        if revenue_spark < 1:
            revenue_spark = 1

        # Credit operator
        ResonanceService.award_spark(
            db, peer.node_operator_id, revenue_spark,
            'ad_revenue', node_id,
            f'Ad revenue: {imp_count} impressions served')

        # Record hosting reward
        reward = HostingReward(
            node_id=node_id,
            operator_id=peer.node_operator_id,
            amount=revenue_spark,
            currency='spark',
            period=period,
            reason=f'Ad revenue share: {imp_count} impressions',
            ad_impressions_count=imp_count,
            uptime_ratio=HostingRewardService.compute_uptime_ratio(db, peer),
            contribution_score_snapshot=peer.contribution_score or 0,
        )
        db.add(reward)

        # Batch impression bonus (1 Spark per 100 impressions)
        batches = imp_count // 100
        if batches > 0:
            ResonanceService.award_spark(
                db, peer.node_operator_id, batches,
                'ad_impression_served', node_id,
                f'Batch impression bonus: {batches} x 100')

        db.flush()
        return reward.to_dict()

    @staticmethod
    def distribute_uptime_bonus(db: Session, node_id: str) -> Optional[Dict]:
        """Award daily bonus for 100% uptime (status='active')."""
        peer = db.query(PeerNode).filter_by(
            node_id=node_id, status='active').first()
        if not peer or not peer.node_operator_id:
            return None

        # Check: last_seen within 5 minutes (alive)
        if peer.last_seen:
            age = (datetime.utcnow() - peer.last_seen).total_seconds()
            if age > 300:
                return None  # Not truly active

        # Check if already awarded today
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        existing = db.query(HostingReward).filter(
            HostingReward.node_id == node_id,
            HostingReward.period == 'daily',
            HostingReward.reason.like('Uptime bonus%'),
            HostingReward.created_at >= today_start,
        ).first()
        if existing:
            return None  # Already awarded today

        # Award: 10 Spark + 5 Pulse + 20 XP
        ResonanceService.award_spark(
            db, peer.node_operator_id, 10, 'hosting_uptime_bonus', node_id,
            'Daily uptime bonus')
        ResonanceService.award_pulse(
            db, peer.node_operator_id, 5, 'hosting_uptime_bonus', node_id,
            'Daily uptime bonus')
        ResonanceService.award_xp(
            db, peer.node_operator_id, 20, 'hosting_uptime_bonus', node_id,
            'Daily uptime bonus')

        reward = HostingReward(
            node_id=node_id,
            operator_id=peer.node_operator_id,
            amount=10, currency='spark',
            period='daily',
            reason='Uptime bonus: 100% active',
            uptime_ratio=1.0,
            contribution_score_snapshot=peer.contribution_score or 0,
        )
        db.add(reward)
        db.flush()
        return reward.to_dict()

    @staticmethod
    def check_milestones(db: Session, node_id: str) -> Optional[Dict]:
        """Check and award hosting milestones based on agent_count."""
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer or not peer.node_operator_id:
            return None

        agent_count = peer.agent_count or 0
        awarded = None

        for threshold in HOSTING_MILESTONES:
            if agent_count >= threshold:
                # Check if already awarded
                existing = db.query(HostingReward).filter(
                    HostingReward.node_id == node_id,
                    HostingReward.period == 'milestone',
                    HostingReward.reason == f'Hosting milestone: {threshold} agents',
                ).first()
                if existing:
                    continue

                # Award milestone
                ResonanceService.award_spark(
                    db, peer.node_operator_id, 50,
                    'hosting_milestone', node_id,
                    f'Milestone: {threshold} agents hosted')
                ResonanceService.award_pulse(
                    db, peer.node_operator_id, 25,
                    'hosting_milestone', node_id,
                    f'Milestone: {threshold} agents hosted')
                ResonanceService.award_xp(
                    db, peer.node_operator_id, 100,
                    'hosting_milestone', node_id,
                    f'Milestone: {threshold} agents hosted')

                reward = HostingReward(
                    node_id=node_id,
                    operator_id=peer.node_operator_id,
                    amount=50, currency='spark',
                    period='milestone',
                    reason=f'Hosting milestone: {threshold} agents',
                    contribution_score_snapshot=peer.contribution_score or 0,
                )
                db.add(reward)
                awarded = reward

        if awarded:
            db.flush()
            return awarded.to_dict()
        return None

    # ─── Queries ───

    @staticmethod
    def get_rewards(db: Session, node_id: str = None,
                    operator_id: str = None,
                    limit: int = 50, offset: int = 0) -> List[Dict]:
        q = db.query(HostingReward)
        if node_id:
            q = q.filter_by(node_id=node_id)
        if operator_id:
            q = q.filter_by(operator_id=operator_id)
        rewards = q.order_by(desc(HostingReward.created_at)).offset(offset).limit(limit).all()
        return [r.to_dict() for r in rewards]

    @staticmethod
    def get_leaderboard(db: Session, limit: int = 50,
                        offset: int = 0) -> List[Dict]:
        nodes = db.query(PeerNode).filter(
            PeerNode.status.in_(['active', 'stale'])
        ).order_by(desc(PeerNode.contribution_score)).offset(offset).limit(limit).all()
        return [n.to_dict() for n in nodes]

    @staticmethod
    def get_reward_summary(db: Session, node_id: str) -> Dict:
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer:
            return {'error': 'Node not found'}

        total_spark = db.query(func.coalesce(
            func.sum(HostingReward.amount), 0
        )).filter(
            HostingReward.node_id == node_id,
            HostingReward.currency == 'spark',
        ).scalar()

        reward_count = db.query(func.count(HostingReward.id)).filter_by(
            node_id=node_id).scalar()

        return {
            'node_id': node_id,
            'contribution_score': peer.contribution_score or 0,
            'visibility_tier': peer.visibility_tier or 'standard',
            'total_spark_earned': int(total_spark),
            'total_rewards': reward_count,
            'agent_count': peer.agent_count or 0,
            'post_count': peer.post_count or 0,
            'status': peer.status,
        }
