"""
HevolveSocial - Ad Service
Ad creation, serving, impression/click tracking, anti-fraud, revenue sharing.
Advertisers spend Spark to run ads; peer node hosters earn 90% of ad revenue (90/9/1 split).
"""
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

from sqlalchemy import desc, func, and_
from sqlalchemy.orm import Session

from .models import (
    User, AdUnit, AdPlacement, AdImpression, PeerNode,
    CommunityMembership, HostingReward,
)
from .resonance_engine import ResonanceService

logger = logging.getLogger('hevolve_social')

# ─── Constants ───

AD_COSTS = {
    'default_cpi': 0.1,   # Spark per impression
    'default_cpc': 1.0,   # Spark per click
    'min_budget': 50,      # Minimum Spark budget to create an ad
}

MAX_IMPRESSIONS_PER_USER_PER_AD_PER_HOUR = 3
MAX_CLICKS_PER_USER_PER_AD_PER_HOUR = 1

try:
    from integrations.agent_engine.revenue_aggregator import REVENUE_SPLIT_USERS
    HOSTER_REVENUE_SHARE = REVENUE_SPLIT_USERS          # 0.90 (was 0.70)
except ImportError:
    HOSTER_REVENUE_SHARE = 0.90
HOSTER_UNWITNESSED_SHARE = 0.50   # fraud-penalty rate (unchanged)
PLATFORM_REVENUE_SHARE = 1.0 - HOSTER_REVENUE_SHARE    # 0.10 (was 0.30)

# Default placements to seed
DEFAULT_PLACEMENTS = [
    {'name': 'feed_top', 'display_name': 'Feed Top Banner',
     'description': 'Banner ad at top of main feed', 'max_ads': 1},
    {'name': 'sidebar', 'display_name': 'Sidebar Ad',
     'description': 'Sidebar advertisement panel', 'max_ads': 2},
    {'name': 'region_page', 'display_name': 'Region Page Ad',
     'description': 'Ad shown on region landing pages', 'max_ads': 1},
    {'name': 'post_interstitial', 'display_name': 'Post Interstitial',
     'description': 'Ad between posts in feed', 'max_ads': 1},
]


class AdService:

    # ─── CRUD ───

    @staticmethod
    def create_ad(db: Session, advertiser_id: str, title: str,
                  click_url: str, content: str = '',
                  image_url: str = '', ad_type: str = 'banner',
                  targeting: Dict = None,
                  budget_spark: int = 100,
                  cost_per_impression: float = 0.1,
                  cost_per_click: float = 1.0,
                  starts_at: str = None, ends_at: str = None) -> Dict:
        """Create an ad unit. Debits Spark budget from advertiser."""
        if budget_spark < AD_COSTS['min_budget']:
            return {'error': f"Minimum budget is {AD_COSTS['min_budget']} Spark"}

        if not title or not click_url:
            return {'error': 'Title and click_url are required'}

        # Debit Spark budget
        success, remaining = ResonanceService.spend_spark(
            db, advertiser_id, budget_spark, 'ad_budget', None,
            f'Ad budget: {title[:50]}')
        if not success:
            return {'error': 'Insufficient Spark', 'spark_balance': remaining}

        ad = AdUnit(
            advertiser_id=advertiser_id,
            title=title, content=content,
            image_url=image_url, click_url=click_url,
            ad_type=ad_type,
            targeting_json=targeting or {},
            budget_spark=budget_spark,
            cost_per_impression=cost_per_impression,
            cost_per_click=cost_per_click,
            status='active',
        )

        if starts_at:
            try:
                ad.starts_at = datetime.fromisoformat(starts_at)
            except (ValueError, TypeError):
                pass
        if ends_at:
            try:
                ad.ends_at = datetime.fromisoformat(ends_at)
            except (ValueError, TypeError):
                pass

        db.add(ad)
        db.flush()
        return ad.to_dict()

    @staticmethod
    def get_ad(db: Session, ad_id: str) -> Optional[Dict]:
        ad = db.query(AdUnit).filter_by(id=ad_id).first()
        return ad.to_dict() if ad else None

    @staticmethod
    def list_my_ads(db: Session, advertiser_id: str,
                    status: str = None,
                    limit: int = 25, offset: int = 0) -> List[Dict]:
        q = db.query(AdUnit).filter_by(advertiser_id=advertiser_id)
        if status:
            q = q.filter_by(status=status)
        ads = q.order_by(desc(AdUnit.created_at)).offset(offset).limit(limit).all()
        return [a.to_dict() for a in ads]

    @staticmethod
    def update_ad(db: Session, ad_id: str, advertiser_id: str,
                  updates: Dict) -> Optional[Dict]:
        ad = db.query(AdUnit).filter_by(id=ad_id, advertiser_id=advertiser_id).first()
        if not ad:
            return None
        for key in ['title', 'content', 'image_url', 'click_url', 'ad_type',
                     'targeting_json', 'status']:
            if key in updates:
                setattr(ad, key, updates[key])
        db.flush()
        return ad.to_dict()

    @staticmethod
    def pause_ad(db: Session, ad_id: str, advertiser_id: str) -> Optional[Dict]:
        ad = db.query(AdUnit).filter_by(id=ad_id, advertiser_id=advertiser_id).first()
        if not ad:
            return None
        ad.status = 'paused'
        db.flush()
        return ad.to_dict()

    @staticmethod
    def delete_ad(db: Session, ad_id: str, advertiser_id: str) -> Optional[Dict]:
        """Cancel ad and refund unspent Spark."""
        ad = db.query(AdUnit).filter_by(id=ad_id, advertiser_id=advertiser_id).first()
        if not ad:
            return None
        unspent = max(0, (ad.budget_spark or 0) - (ad.spent_spark or 0))
        if unspent > 0:
            ResonanceService.award_spark(
                db, advertiser_id, unspent, 'ad_refund', ad.id,
                f'Ad refund: {ad.title[:50]}')
        ad.status = 'completed'
        db.flush()
        return {'deleted': True, 'spark_refunded': unspent}

    # ─── Ad Serving ───

    @staticmethod
    def serve_ad(db: Session, user_id: str = None,
                 region_id: str = None,
                 placement_name: str = 'feed_top',
                 node_id: str = None) -> Optional[Dict]:
        """Select the best ad for a given context."""
        now = datetime.utcnow()

        # Active ads with remaining budget
        q = db.query(AdUnit).filter(
            AdUnit.status == 'active',
            AdUnit.spent_spark < AdUnit.budget_spark,
        )
        q = q.filter(
            (AdUnit.starts_at.is_(None)) | (AdUnit.starts_at <= now),
            (AdUnit.ends_at.is_(None)) | (AdUnit.ends_at > now),
        )
        candidates = q.all()
        if not candidates:
            return None

        # Targeting filters
        user_communities = set()
        user_type = None
        if user_id:
            memberships = db.query(CommunityMembership.community_id).filter_by(
                user_id=user_id).all()
            user_communities = {m[0] for m in memberships}
            user_obj = db.query(User).filter_by(id=user_id).first()
            user_type = user_obj.user_type if user_obj else None

        scored = []
        for ad in candidates:
            targeting = ad.targeting_json or {}
            target_regions = targeting.get('region_ids', [])
            if target_regions and region_id and region_id not in target_regions:
                continue
            target_communities = targeting.get('community_ids', [])
            if target_communities and not user_communities.intersection(set(target_communities)):
                continue
            target_user_types = targeting.get('user_types', [])
            if target_user_types and user_type and user_type not in target_user_types:
                continue
            scored.append(ad)

        if not scored:
            return None

        # Anti-fraud: exclude ads user has seen too many times
        if user_id:
            one_hour_ago = now - timedelta(hours=1)
            filtered = []
            for ad in scored:
                count = db.query(func.count(AdImpression.id)).filter(
                    AdImpression.ad_id == ad.id,
                    AdImpression.user_id == user_id,
                    AdImpression.impression_type == 'view',
                    AdImpression.created_at >= one_hour_ago,
                ).scalar() or 0
                if count < MAX_IMPRESSIONS_PER_USER_PER_AD_PER_HOUR:
                    filtered.append(ad)
            scored = filtered

        if not scored:
            return None

        # Sort by remaining budget descending, pick top
        scored.sort(key=lambda a: (a.budget_spark - a.spent_spark), reverse=True)
        winner = scored[0]

        return {
            'ad': winner.to_dict(),
            'placement': placement_name,
        }

    # ─── Impressions & Clicks ───

    @staticmethod
    def record_impression(db: Session, ad_id: str,
                          user_id: str = None,
                          node_id: str = None,
                          region_id: str = None,
                          placement_id: str = None,
                          ip_hash: str = None) -> Optional[Dict]:
        """Record an ad view impression. Credits node hoster."""
        ad = db.query(AdUnit).filter_by(id=ad_id, status='active').first()
        if not ad:
            return None

        # Anti-fraud
        if user_id and not AdService._check_rate_limit(
            db, ad_id, user_id, 'view',
            MAX_IMPRESSIONS_PER_USER_PER_AD_PER_HOUR
        ):
            return {'error': 'Rate limit exceeded', 'ad_id': ad_id}

        # Budget check
        cost = ad.cost_per_impression or AD_COSTS['default_cpi']
        if (ad.spent_spark or 0) + cost > (ad.budget_spark or 0):
            ad.status = 'exhausted'
            db.flush()
            return {'error': 'Ad budget exhausted'}

        imp = AdImpression(
            ad_id=ad_id, placement_id=placement_id,
            node_id=node_id, region_id=region_id,
            user_id=user_id, impression_type='view',
            ip_hash=ip_hash,
        )
        db.add(imp)
        db.flush()

        ad.spent_spark = int((ad.spent_spark or 0) + cost)
        ad.impression_count = (ad.impression_count or 0) + 1

        # Request peer witness (best-effort, non-blocking)
        witnessed = False
        if node_id:
            try:
                from .integrity_service import IntegrityService
                witness_result = IntegrityService.request_nearest_witness(
                    db, imp.id, ad_id, node_id)
                if witness_result:
                    witnessed = True
                    # Seal the impression with witness data
                    imp.witness_node_id = witness_result.get(
                        'attester_node_id', '')
                    imp.witness_signature = witness_result.get(
                        'signature', '')
                    imp.sealed_hash = imp.compute_seal_hash
                    imp.sealed_at = datetime.utcnow()
            except Exception:
                pass

            # Credit node hoster: 90% if witnessed, 50% if not (fraud penalty)
            share = HOSTER_REVENUE_SHARE if witnessed else HOSTER_UNWITNESSED_SHARE
            hoster_share = cost * share
            AdService._credit_node_hoster(
                db, node_id, hoster_share,
                f'Ad impression revenue: ad {ad_id[:8]}')

        db.flush()
        result = imp.to_dict()
        result['witnessed'] = witnessed
        return result

    @staticmethod
    def record_click(db: Session, ad_id: str,
                     user_id: str = None,
                     node_id: str = None,
                     ip_hash: str = None) -> Optional[Dict]:
        """Record an ad click. Credits node hoster."""
        ad = db.query(AdUnit).filter_by(id=ad_id, status='active').first()
        if not ad:
            return None

        # Anti-fraud
        if user_id and not AdService._check_rate_limit(
            db, ad_id, user_id, 'click',
            MAX_CLICKS_PER_USER_PER_AD_PER_HOUR
        ):
            return {'error': 'Click rate limit exceeded', 'ad_id': ad_id}

        cost = ad.cost_per_click or AD_COSTS['default_cpc']
        if (ad.spent_spark or 0) + cost > (ad.budget_spark or 0):
            ad.status = 'exhausted'
            db.flush()
            return {'error': 'Ad budget exhausted'}

        imp = AdImpression(
            ad_id=ad_id, node_id=node_id,
            user_id=user_id, impression_type='click',
            ip_hash=ip_hash,
        )
        db.add(imp)
        db.flush()

        ad.spent_spark = int((ad.spent_spark or 0) + cost)
        ad.click_count = (ad.click_count or 0) + 1

        # Request peer witness for click (best-effort)
        witnessed = False
        if node_id:
            try:
                from .integrity_service import IntegrityService
                witnessed = IntegrityService.request_nearest_witness(
                    db, imp.id, ad_id, node_id)
            except Exception:
                pass

            share = HOSTER_REVENUE_SHARE if witnessed else HOSTER_UNWITNESSED_SHARE
            hoster_share = cost * share
            AdService._credit_node_hoster(
                db, node_id, hoster_share,
                f'Ad click revenue: ad {ad_id[:8]}')

        db.flush()
        result = imp.to_dict()
        result['witnessed'] = witnessed
        return result

    # ─── Analytics ───

    @staticmethod
    def get_analytics(db: Session, ad_id: str,
                      advertiser_id: str) -> Optional[Dict]:
        ad = db.query(AdUnit).filter_by(
            id=ad_id, advertiser_id=advertiser_id).first()
        if not ad:
            return None

        impressions = ad.impression_count or 0
        clicks = ad.click_count or 0
        ctr = (clicks / impressions * 100) if impressions > 0 else 0.0

        # Per-node breakdown
        node_stats = db.query(
            AdImpression.node_id,
            func.count(AdImpression.id),
        ).filter_by(ad_id=ad_id).group_by(
            AdImpression.node_id,
        ).all()

        return {
            'ad': ad.to_dict(),
            'impressions': impressions,
            'clicks': clicks,
            'ctr': round(ctr, 2),
            'spent_spark': ad.spent_spark or 0,
            'remaining_spark': max(0, (ad.budget_spark or 0) - (ad.spent_spark or 0)),
            'node_breakdown': [
                {'node_id': nid, 'count': cnt}
                for nid, cnt in node_stats if nid
            ],
        }

    # ─── Internal Helpers ───

    @staticmethod
    def _check_rate_limit(db: Session, ad_id: str, user_id: str,
                          impression_type: str, max_count: int) -> bool:
        """Returns True if under rate limit."""
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        count = db.query(func.count(AdImpression.id)).filter(
            AdImpression.ad_id == ad_id,
            AdImpression.user_id == user_id,
            AdImpression.impression_type == impression_type,
            AdImpression.created_at >= one_hour_ago,
        ).scalar() or 0
        return count < max_count

    @staticmethod
    def _credit_node_hoster(db: Session, node_id: str,
                            spark_amount: float, reason: str):
        """Credit node operator with their revenue share."""
        peer = db.query(PeerNode).filter_by(node_id=node_id).first()
        if not peer or not peer.node_operator_id:
            return
        if spark_amount >= 1:
            ResonanceService.award_spark(
                db, peer.node_operator_id, int(spark_amount),
                'ad_revenue', node_id, reason)

    # ─── Seeding ───

    @staticmethod
    def seed_placements(db: Session) -> int:
        """Seed default ad placements. Returns count created."""
        created = 0
        for p in DEFAULT_PLACEMENTS:
            existing = db.query(AdPlacement).filter_by(name=p['name']).first()
            if not existing:
                db.add(AdPlacement(**p))
                created += 1
        if created > 0:
            db.flush()
        return created
