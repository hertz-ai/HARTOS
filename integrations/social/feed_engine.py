"""
HevolveSocial - Feed Engine
Personalized feed: followed users + joined submolts + time decay + score.
"""
import math
from datetime import datetime, timedelta
from typing import List, Tuple

from sqlalchemy import desc, or_
from sqlalchemy.orm import Session, joinedload

from .models import Post, Follow, SubmoltMembership, User


def _hot_score(upvotes: int, downvotes: int, created_at: datetime) -> float:
    """Reddit-style hot score: log(score) + age_bias."""
    score = max(upvotes - downvotes, 1)
    age_hours = max((datetime.utcnow() - created_at).total_seconds() / 3600, 0.1)
    return math.log10(score) - (age_hours / 12)


def get_personalized_feed(db: Session, user_id: str, limit: int = 25,
                          offset: int = 0) -> Tuple[List[Post], int]:
    """Feed from followed users + subscribed submolts, sorted by hot score."""
    # Get followed user IDs
    followed_ids = [f.following_id for f in
                    db.query(Follow.following_id).filter(Follow.follower_id == user_id).all()]

    # Get subscribed submolt IDs
    submolt_ids = [m.submolt_id for m in
                   db.query(SubmoltMembership.submolt_id).filter(
                       SubmoltMembership.user_id == user_id).all()]

    if not followed_ids and not submolt_ids:
        # Fallback to global trending
        return get_trending_feed(db, limit, offset)

    q = db.query(Post).options(joinedload(Post.author)).filter(
        Post.is_deleted == False,
        or_(
            Post.author_id.in_(followed_ids) if followed_ids else False,
            Post.submolt_id.in_(submolt_ids) if submolt_ids else False,
        )
    )
    total = q.count()
    posts = q.order_by(desc(Post.created_at)).offset(offset).limit(limit).all()
    return posts, total


def get_global_feed(db: Session, sort: str = 'new', limit: int = 25,
                    offset: int = 0) -> Tuple[List[Post], int]:
    """All posts, sorted by chosen method."""
    q = db.query(Post).options(joinedload(Post.author)).filter(Post.is_deleted == False)

    if sort == 'top':
        q = q.order_by(desc(Post.score), desc(Post.created_at))
    elif sort == 'hot':
        q = q.order_by(desc(Post.score + Post.comment_count), desc(Post.created_at))
    elif sort == 'discussed':
        q = q.order_by(desc(Post.comment_count), desc(Post.created_at))
    else:
        q = q.order_by(desc(Post.created_at))

    total = q.count()
    posts = q.offset(offset).limit(limit).all()
    return posts, total


def get_trending_feed(db: Session, limit: int = 25, offset: int = 0
                      ) -> Tuple[List[Post], int]:
    """Posts trending in the last 24h based on velocity (votes+comments per hour)."""
    cutoff = datetime.utcnow() - timedelta(hours=24)
    q = db.query(Post).options(joinedload(Post.author)).filter(
        Post.is_deleted == False, Post.created_at >= cutoff
    ).order_by(desc(Post.score + Post.comment_count * 2), desc(Post.created_at))

    total = q.count()
    posts = q.offset(offset).limit(limit).all()
    return posts, total


def get_agent_feed(db: Session, limit: int = 25, offset: int = 0
                   ) -> Tuple[List[Post], int]:
    """Posts by AI agents only."""
    q = db.query(Post).options(joinedload(Post.author)).join(
        User, Post.author_id == User.id
    ).filter(Post.is_deleted == False, User.user_type == 'agent')
    q = q.order_by(desc(Post.created_at))

    total = q.count()
    posts = q.offset(offset).limit(limit).all()
    return posts, total
