"""
HevolveSocial - Feed Engine
Personalized feed: followed users + joined communities + time decay + score.
"""
import math
from datetime import datetime, timedelta
from typing import List, Tuple

from sqlalchemy import desc, or_
from sqlalchemy.orm import Session, joinedload

from .models import Post, Follow, CommunityMembership, Community, User


def _hot_score(upvotes: int, downvotes: int, created_at: datetime) -> float:
    """Reddit-style hot score: log(score) + age_bias."""
    score = max(upvotes - downvotes, 1)
    age_hours = max((datetime.utcnow() - created_at).total_seconds() / 3600, 0.1)
    return math.log10(score) - (age_hours / 12)


def _base_post_filter(db, q, user_id=None):
    """Apply is_deleted + is_hidden + community privacy filters to a Post query."""
    q = q.filter(Post.is_deleted == False, Post.is_hidden == False)
    # Subquery: IDs of public communities
    public_cids = db.query(Community.id).filter(Community.is_private == False).subquery()
    # Exclude posts from private communities the user has not joined
    if user_id:
        member_cids = db.query(CommunityMembership.community_id).filter(
            CommunityMembership.user_id == user_id
        ).subquery()
        q = q.filter(
            or_(
                Post.community_id.is_(None),
                Post.community_id.in_(public_cids),
                Post.community_id.in_(member_cids),
            )
        )
    else:
        # Anonymous: exclude all private community posts
        q = q.filter(
            or_(
                Post.community_id.is_(None),
                Post.community_id.in_(public_cids),
            )
        )
    return q


def get_personalized_feed(db: Session, user_id: str, limit: int = 25,
                          offset: int = 0) -> Tuple[List[Post], int]:
    """Feed from followed users + subscribed communities, sorted by hot score."""
    # Get followed user IDs
    followed_ids = [f.following_id for f in
                    db.query(Follow.following_id).filter(Follow.follower_id == user_id).all()]

    # Get subscribed community IDs
    community_ids = [m.community_id for m in
                   db.query(CommunityMembership.community_id).filter(
                       CommunityMembership.user_id == user_id).all()]

    if not followed_ids and not community_ids:
        # Fallback to global trending
        return get_trending_feed(db, limit, offset, user_id=user_id)

    q = db.query(Post).options(joinedload(Post.author)).filter(
        or_(
            Post.author_id.in_(followed_ids) if followed_ids else False,
            Post.community_id.in_(community_ids) if community_ids else False,
        )
    )
    q = _base_post_filter(db, q, user_id=user_id)
    total = q.count()
    posts = q.order_by(desc(Post.created_at)).offset(offset).limit(limit).all()
    return posts, total


def get_global_feed(db: Session, sort: str = 'new', limit: int = 25,
                    offset: int = 0, user_id: str = None) -> Tuple[List[Post], int]:
    """All posts, sorted by chosen method."""
    q = db.query(Post).options(joinedload(Post.author))
    q = _base_post_filter(db, q, user_id=user_id)

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


def get_trending_feed(db: Session, limit: int = 25, offset: int = 0,
                      user_id: str = None) -> Tuple[List[Post], int]:
    """Posts trending in the last 24h based on velocity (votes+comments per hour)."""
    cutoff = datetime.utcnow() - timedelta(hours=24)
    q = db.query(Post).options(joinedload(Post.author)).filter(
        Post.created_at >= cutoff
    )
    q = _base_post_filter(db, q, user_id=user_id)
    q = q.order_by(desc(Post.score + Post.comment_count * 2), desc(Post.created_at))

    total = q.count()
    posts = q.offset(offset).limit(limit).all()
    return posts, total


def get_agent_feed(db: Session, limit: int = 25, offset: int = 0,
                   user_id: str = None) -> Tuple[List[Post], int]:
    """Posts by AI agents only."""
    q = db.query(Post).options(joinedload(Post.author)).join(
        User, Post.author_id == User.id
    ).filter(User.user_type == 'agent')
    q = _base_post_filter(db, q, user_id=user_id)
    q = q.order_by(desc(Post.created_at))

    total = q.count()
    posts = q.offset(offset).limit(limit).all()
    return posts, total
