"""
HevolveSocial - Region Service
Regional governance, membership, auto-promotion, feeds, leaderboards.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from .models import (
    User, Post, Region, RegionMembership, ResonanceWallet,
)

logger = logging.getLogger('hevolve_social')

# Role progression thresholds
ROLE_THRESHOLDS = {
    'contributor': {'signal': 1.0, 'posts': 5, 'days': 7},
    'moderator': {'signal': 5.0, 'score': 100, 'days': 30},
    'admin': {'signal': 15.0, 'score': 500, 'days': 90},
    'steward': {'signal': 50.0, 'score': 2000, 'days': 180},
}


class RegionService:

    @staticmethod
    def create_region(db: Session, creator_id: str, name: str,
                      display_name: str = '', description: str = '',
                      region_type: str = 'thematic',
                      lat: float = None, lon: float = None,
                      radius_km: float = None) -> Dict:
        """Create a new region."""
        region = Region(
            name=name,
            display_name=display_name or name,
            description=description,
            region_type=region_type,
            lat=lat, lon=lon, radius_km=radius_km,
            member_count=1,
        )
        db.add(region)
        db.flush()

        # Creator becomes steward
        membership = RegionMembership(
            user_id=creator_id,
            region_id=region.id,
            role='steward',
            contribution_score=0.0,
        )
        db.add(membership)
        db.flush()

        # Update user's region_id
        user = db.query(User).filter_by(id=creator_id).first()
        if user and not user.region_id:
            user.region_id = region.id

        return region.to_dict()

    @staticmethod
    def get_region(db: Session, region_id: str) -> Optional[Dict]:
        region = db.query(Region).filter_by(id=region_id).first()
        return region.to_dict() if region else None

    @staticmethod
    def list_regions(db: Session, limit: int = 50, offset: int = 0,
                     region_type: str = None) -> List[Dict]:
        q = db.query(Region)
        if region_type:
            q = q.filter_by(region_type=region_type)
        regions = q.order_by(desc(Region.member_count)).offset(offset).limit(limit).all()
        return [r.to_dict() for r in regions]

    @staticmethod
    def join_region(db: Session, user_id: str, region_id: str) -> Dict:
        """Join a region as a member."""
        existing = db.query(RegionMembership).filter_by(
            user_id=user_id, region_id=region_id
        ).first()
        if existing:
            return {'already_member': True, 'role': existing.role}

        membership = RegionMembership(
            user_id=user_id,
            region_id=region_id,
            role='member',
        )
        db.add(membership)

        region = db.query(Region).filter_by(id=region_id).first()
        if region:
            region.member_count = (region.member_count or 0) + 1

        # Set as user's primary region if they don't have one
        user = db.query(User).filter_by(id=user_id).first()
        if user and not user.region_id:
            user.region_id = region_id

        db.flush()
        return {'joined': True, 'role': 'member'}

    @staticmethod
    def leave_region(db: Session, user_id: str, region_id: str) -> bool:
        membership = db.query(RegionMembership).filter_by(
            user_id=user_id, region_id=region_id
        ).first()
        if not membership:
            return False

        db.delete(membership)
        region = db.query(Region).filter_by(id=region_id).first()
        if region:
            region.member_count = max(0, (region.member_count or 0) - 1)

        user = db.query(User).filter_by(id=user_id).first()
        if user and user.region_id == region_id:
            user.region_id = None

        return True

    @staticmethod
    def get_members(db: Session, region_id: str,
                    limit: int = 50, offset: int = 0) -> List[Dict]:
        rows = db.query(RegionMembership, User).join(
            User, User.id == RegionMembership.user_id
        ).filter(
            RegionMembership.region_id == region_id
        ).order_by(
            desc(RegionMembership.contribution_score)
        ).offset(offset).limit(limit).all()

        result = []
        for membership, user in rows:
            result.append({
                'user_id': user.id,
                'username': user.username,
                'display_name': user.display_name,
                'avatar_url': user.avatar_url,
                'role': membership.role,
                'contribution_score': membership.contribution_score,
                'promoted_at': membership.promoted_at.isoformat() if membership.promoted_at else None,
            })
        return result

    @staticmethod
    def get_regional_feed(db: Session, region_id: str,
                          limit: int = 25, offset: int = 0) -> List[Dict]:
        """Get posts from a specific region."""
        posts = db.query(Post).filter_by(
            region_id=region_id, is_removed=False
        ).order_by(
            desc(Post.created_at)
        ).offset(offset).limit(limit).all()
        return [p.to_dict(include_author=True) for p in posts]

    @staticmethod
    def get_regional_leaderboard(db: Session, region_id: str,
                                  currency: str = 'pulse',
                                  limit: int = 50, offset: int = 0) -> List[Dict]:
        from .resonance_engine import ResonanceService
        return ResonanceService.get_leaderboard(
            db, currency=currency, limit=limit, offset=offset, region_id=region_id
        )

    @staticmethod
    def check_promotion_eligibility(db: Session, user_id: str,
                                     region_id: str) -> Dict:
        """Check if a user is eligible for promotion in a region."""
        membership = db.query(RegionMembership).filter_by(
            user_id=user_id, region_id=region_id
        ).first()
        if not membership:
            return {'eligible': False, 'reason': 'Not a member'}

        wallet = db.query(ResonanceWallet).filter_by(user_id=user_id).first()
        user = db.query(User).filter_by(id=user_id).first()

        current_role = membership.role
        next_role_map = {
            'member': 'contributor',
            'contributor': 'moderator',
            'moderator': 'admin',
            'admin': 'steward',
        }
        next_role = next_role_map.get(current_role)
        if not next_role:
            return {'eligible': False, 'reason': 'Already at max role', 'role': current_role}

        reqs = ROLE_THRESHOLDS.get(next_role, {})
        days_member = (datetime.utcnow() - (membership.created_at or datetime.utcnow())).days

        checks = {
            'signal': (wallet.signal if wallet else 0) >= reqs.get('signal', 0),
            'days': days_member >= reqs.get('days', 0),
        }
        if 'posts' in reqs:
            checks['posts'] = (user.post_count if user else 0) >= reqs['posts']
        if 'score' in reqs:
            checks['score'] = (membership.contribution_score or 0) >= reqs['score']

        eligible = all(checks.values())
        return {
            'eligible': eligible,
            'current_role': current_role,
            'next_role': next_role,
            'checks': checks,
            'requirements': reqs,
        }

    @staticmethod
    def promote_member(db: Session, user_id: str, region_id: str,
                       new_role: str, promoter_id: str = None) -> Optional[Dict]:
        """Promote a member to a higher role."""
        membership = db.query(RegionMembership).filter_by(
            user_id=user_id, region_id=region_id
        ).first()
        if not membership:
            return None

        role_order = ['member', 'contributor', 'moderator', 'admin', 'steward']
        if new_role not in role_order:
            return None
        if role_order.index(new_role) <= role_order.index(membership.role):
            return None

        membership.role = new_role
        membership.promoted_at = datetime.utcnow()
        return {'user_id': user_id, 'new_role': new_role}

    @staticmethod
    def demote_member(db: Session, user_id: str, region_id: str,
                      new_role: str) -> Optional[Dict]:
        """Demote a member to a lower role."""
        membership = db.query(RegionMembership).filter_by(
            user_id=user_id, region_id=region_id
        ).first()
        if not membership:
            return None

        role_order = ['member', 'contributor', 'moderator', 'admin', 'steward']
        if new_role not in role_order:
            return None

        membership.role = new_role
        return {'user_id': user_id, 'new_role': new_role}

    @staticmethod
    def nearby_regions(db: Session, lat: float, lon: float,
                       radius_km: float = 50.0) -> List[Dict]:
        """Find regions near a geographic point."""
        deg = radius_km / 111.0  # rough degrees per km
        regions = db.query(Region).filter(
            Region.lat.isnot(None),
            Region.lon.isnot(None),
            Region.lat.between(lat - deg, lat + deg),
            Region.lon.between(lon - deg, lon + deg),
        ).all()
        return [r.to_dict() for r in regions]

    @staticmethod
    def get_governance_info(db: Session, region_id: str) -> Dict:
        """Get governance information for a region."""
        region = db.query(Region).filter_by(id=region_id).first()
        if not region:
            return {}

        # Count by role
        role_counts = db.query(
            RegionMembership.role, func.count(RegionMembership.id)
        ).filter_by(region_id=region_id).group_by(RegionMembership.role).all()

        roles = {role: count for role, count in role_counts}

        # Get council (moderators and above)
        council = db.query(RegionMembership, User).join(
            User, User.id == RegionMembership.user_id
        ).filter(
            RegionMembership.region_id == region_id,
            RegionMembership.role.in_(['moderator', 'admin', 'steward']),
        ).all()

        council_list = [{
            'user_id': u.id,
            'username': u.username,
            'display_name': u.display_name,
            'role': m.role,
            'contribution_score': m.contribution_score,
        } for m, u in council]

        return {
            'region_id': region_id,
            'member_count': region.member_count,
            'role_distribution': roles,
            'council': council_list,
            'thresholds': ROLE_THRESHOLDS,
        }
