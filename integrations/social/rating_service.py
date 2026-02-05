"""
HevolveSocial - Rating Service
Multi-dimensional ratings (skill, usefulness, reliability, creativity) and trust scores.
"""
import logging
from typing import Optional, Dict, List

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from .models import User, Rating, TrustScore, ResonanceWallet

logger = logging.getLogger('hevolve_social')

DIMENSIONS = ['skill', 'usefulness', 'reliability', 'creativity']
TRUST_WEIGHTS = {'skill': 0.25, 'usefulness': 0.30, 'reliability': 0.30, 'creativity': 0.15}


class RatingService:

    @staticmethod
    def submit_rating(db: Session, rater_id: str, rated_id: str,
                      context_type: str, context_id: str,
                      dimension: str, score: float,
                      comment: str = '') -> Optional[Dict]:
        """Submit or update a rating."""
        if dimension not in DIMENSIONS:
            return None
        if not (1.0 <= score <= 5.0):
            return None
        if rater_id == rated_id:
            return None

        existing = db.query(Rating).filter_by(
            rater_id=rater_id, rated_id=rated_id,
            context_type=context_type, context_id=context_id,
            dimension=dimension,
        ).first()

        if existing:
            existing.score = score
            existing.comment = comment
            rating = existing
        else:
            rating = Rating(
                rater_id=rater_id, rated_id=rated_id,
                context_type=context_type, context_id=context_id,
                dimension=dimension, score=score, comment=comment,
            )
            db.add(rating)
        db.flush()

        # Recalculate trust score
        RatingService._recalculate_trust(db, rated_id)

        return rating.to_dict()

    @staticmethod
    def _recalculate_trust(db: Session, user_id: str):
        """Recalculate composite trust score for a user.
        Ratings from higher-Signal users count more."""
        trust = db.query(TrustScore).filter_by(user_id=user_id).first()
        if not trust:
            trust = TrustScore(user_id=user_id)
            db.add(trust)

        total_ratings = 0
        dim_totals = {}
        dim_weights = {}

        for dim in DIMENSIONS:
            ratings = db.query(Rating).filter_by(
                rated_id=user_id, dimension=dim
            ).all()

            weighted_sum = 0.0
            weight_sum = 0.0
            for r in ratings:
                # Rater credibility based on their Signal
                rater_wallet = db.query(ResonanceWallet).filter_by(user_id=r.rater_id).first()
                rater_signal = rater_wallet.signal if rater_wallet else 0
                weight = min(rater_signal / 10.0, 3.0)
                weight = max(weight, 0.5)  # minimum weight of 0.5
                weighted_sum += r.score * weight
                weight_sum += weight

            if weight_sum > 0:
                dim_totals[dim] = weighted_sum / weight_sum
            else:
                dim_totals[dim] = 0.0
            dim_weights[dim] = len(ratings)
            total_ratings += len(ratings)

        trust.avg_skill = dim_totals.get('skill', 0.0)
        trust.avg_usefulness = dim_totals.get('usefulness', 0.0)
        trust.avg_reliability = dim_totals.get('reliability', 0.0)
        trust.avg_creativity = dim_totals.get('creativity', 0.0)
        trust.total_ratings_received = total_ratings

        # Composite trust = weighted average of dimensions
        composite = sum(
            dim_totals.get(dim, 0) * TRUST_WEIGHTS[dim]
            for dim in DIMENSIONS
        )
        trust.composite_trust = round(composite, 3)
        db.flush()

    @staticmethod
    def get_trust_score(db: Session, user_id: str) -> Optional[Dict]:
        trust = db.query(TrustScore).filter_by(user_id=user_id).first()
        return trust.to_dict() if trust else None

    @staticmethod
    def get_ratings_received(db: Session, user_id: str,
                              limit: int = 50, offset: int = 0) -> List[Dict]:
        ratings = db.query(Rating).filter_by(
            rated_id=user_id
        ).order_by(desc(Rating.created_at)).offset(offset).limit(limit).all()
        return [r.to_dict() for r in ratings]

    @staticmethod
    def get_ratings_given(db: Session, user_id: str,
                          limit: int = 50, offset: int = 0) -> List[Dict]:
        ratings = db.query(Rating).filter_by(
            rater_id=user_id
        ).order_by(desc(Rating.created_at)).offset(offset).limit(limit).all()
        return [r.to_dict() for r in ratings]

    @staticmethod
    def get_context_ratings(db: Session, context_type: str,
                             context_id: str) -> List[Dict]:
        ratings = db.query(Rating).filter_by(
            context_type=context_type, context_id=context_id
        ).all()
        return [r.to_dict() for r in ratings]

    @staticmethod
    def get_aggregated(db: Session, user_id: str) -> Dict:
        """Get aggregated rating info for display."""
        trust = RatingService.get_trust_score(db, user_id)
        if not trust:
            return {
                'dimensions': {d: 0.0 for d in DIMENSIONS},
                'composite_trust': 0.0,
                'total_ratings': 0,
            }
        return {
            'dimensions': {
                'skill': trust.get('avg_skill', 0),
                'usefulness': trust.get('avg_usefulness', 0),
                'reliability': trust.get('avg_reliability', 0),
                'creativity': trust.get('avg_creativity', 0),
            },
            'composite_trust': trust.get('composite_trust', 0),
            'total_ratings': trust.get('total_ratings_received', 0),
        }
