"""
HevolveSocial - Distribution Service
Referral codes, boost system, federation contribution, onboarding.
"""
import logging
import secrets
import string
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from .models import (
    User, Referral, ReferralCode, Boost, Post, ResonanceWallet,
)
from .resonance_engine import ResonanceService

logger = logging.getLogger('hevolve_social')


def _generate_code(length: int = 8) -> str:
    chars = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))


class DistributionService:

    # ─── Referrals ───

    @staticmethod
    def get_or_create_referral_code(db: Session, user_id: str) -> Dict:
        """Get or create a referral code for a user."""
        code_obj = db.query(ReferralCode).filter_by(
            user_id=user_id, is_active=True
        ).first()
        if code_obj:
            return code_obj.to_dict()

        code = _generate_code()
        # Ensure unique
        while db.query(ReferralCode).filter_by(code=code).first():
            code = _generate_code()

        code_obj = ReferralCode(
            user_id=user_id,
            code=code,
            is_active=True,
            max_uses=100,
        )
        db.add(code_obj)
        db.flush()

        # Also set on user
        user = db.query(User).filter_by(id=user_id).first()
        if user and not user.referral_code:
            user.referral_code = code

        return code_obj.to_dict()

    @staticmethod
    def use_referral_code(db: Session, referred_user_id: str,
                          code: str) -> Optional[Dict]:
        """Use a referral code during registration."""
        code_obj = db.query(ReferralCode).filter_by(code=code, is_active=True).first()
        if not code_obj:
            return None
        if code_obj.user_id == referred_user_id:
            return None  # Can't refer yourself
        if code_obj.uses >= code_obj.max_uses:
            return None

        # Check not already referred
        existing = db.query(Referral).filter_by(referred_id=referred_user_id).first()
        if existing:
            return None

        referral = Referral(
            referrer_id=code_obj.user_id,
            referred_id=referred_user_id,
            referral_code=code,
            status='pending',
        )
        db.add(referral)
        code_obj.uses += 1

        # Set referred_by on user
        user = db.query(User).filter_by(id=referred_user_id).first()
        if user:
            user.referred_by_id = code_obj.user_id

        db.flush()
        return referral.to_dict()

    @staticmethod
    def check_referral_activation(db: Session, referred_user_id: str) -> Optional[Dict]:
        """Check if a referred user qualifies for activation.
        Criteria: 3+ days old, 1+ post or 3+ comments, 5+ upvotes received."""
        referral = db.query(Referral).filter_by(
            referred_id=referred_user_id, status='pending'
        ).first()
        if not referral:
            return None

        user = db.query(User).filter_by(id=referred_user_id).first()
        if not user:
            return None

        days_old = (datetime.utcnow() - (user.created_at or datetime.utcnow())).days
        has_content = (user.post_count or 0) >= 1 or (user.comment_count or 0) >= 3
        has_upvotes = (user.karma_score or 0) >= 5

        if days_old >= 3 and has_content and has_upvotes:
            referral.status = 'activated'
            # Award referrer
            ResonanceService.award_action(db, referral.referrer_id,
                                          'referral_activated', referral.id)
            return {'activated': True, 'referrer_id': referral.referrer_id}

        return {'activated': False, 'days_old': days_old, 'has_content': has_content, 'has_upvotes': has_upvotes}

    @staticmethod
    def get_referral_stats(db: Session, user_id: str) -> Dict:
        """Get referral statistics for a user."""
        total = db.query(func.count(Referral.id)).filter_by(referrer_id=user_id).scalar() or 0
        activated = db.query(func.count(Referral.id)).filter_by(
            referrer_id=user_id, status='activated'
        ).scalar() or 0
        pending = total - activated

        code_obj = db.query(ReferralCode).filter_by(user_id=user_id, is_active=True).first()
        code = code_obj.code if code_obj else None

        return {
            'code': code,
            'total_referrals': total,
            'activated': activated,
            'pending': pending,
        }

    # ─── Boosts ───

    @staticmethod
    def create_boost(db: Session, user_id: str, target_type: str,
                     target_id: str, spark_amount: int) -> Tuple[bool, Dict]:
        """Create a boost by spending Spark.
        multiplier = min(1.0 + spark*0.01, 2.0)
        duration = spark hours"""
        success, remaining = ResonanceService.spend_spark(
            db, user_id, spark_amount, 'boost', target_id,
            f'Boost {target_type} {target_id}'
        )
        if not success:
            return False, {'error': 'Insufficient Spark', 'spark_balance': remaining}

        multiplier = min(1.0 + spark_amount * 0.01, 2.0)
        expires_at = datetime.utcnow() + timedelta(hours=spark_amount)

        boost = Boost(
            user_id=user_id,
            target_type=target_type,
            target_id=target_id,
            spark_spent=spark_amount,
            boost_multiplier=multiplier,
            expires_at=expires_at,
        )
        db.add(boost)

        # If boosting a post, update its boost_score
        if target_type == 'post':
            post = db.query(Post).filter_by(id=target_id).first()
            if post:
                post.boost_score = (post.boost_score or 0) + multiplier

        db.flush()
        return True, boost.to_dict()

    @staticmethod
    def get_active_boosts(db: Session, target_type: str,
                          target_id: str) -> List[Dict]:
        """Get active boosts for a target."""
        now = datetime.utcnow()
        boosts = db.query(Boost).filter(
            Boost.target_type == target_type,
            Boost.target_id == target_id,
            Boost.expires_at > now,
        ).all()
        return [b.to_dict() for b in boosts]
