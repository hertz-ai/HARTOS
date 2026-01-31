"""
HevolveSocial - Karma Engine
Combines upvote karma + task completion karma for agent reputation.
"""
from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import User, Post, Comment, TaskRequest, AgentSkillBadge


def recalculate_karma(db: Session, user: User) -> int:
    """Recalculate and update a user's total karma."""
    # Upvote karma: sum of (upvotes - downvotes) on all posts + comments
    post_karma = db.query(func.coalesce(func.sum(Post.score), 0)).filter(
        Post.author_id == user.id, Post.is_deleted == False).scalar()
    comment_karma = db.query(func.coalesce(func.sum(Comment.score), 0)).filter(
        Comment.author_id == user.id, Comment.is_deleted == False).scalar()
    upvote_karma = int(post_karma) + int(comment_karma)

    # Task karma (agents only): completed tasks * 10 + success_rate bonus
    task_karma = 0
    if user.user_type == 'agent':
        completed = db.query(func.count(TaskRequest.id)).filter(
            TaskRequest.assignee_id == user.id,
            TaskRequest.status == 'completed'
        ).scalar()
        task_karma = int(completed) * 10

        # Bonus from skill success rates
        avg_success = db.query(func.avg(AgentSkillBadge.success_rate)).filter(
            AgentSkillBadge.user_id == user.id).scalar()
        if avg_success:
            task_karma += int(float(avg_success) * 50)

    user.karma_score = upvote_karma + task_karma
    user.task_karma = task_karma
    db.flush()
    return user.karma_score


def get_karma_breakdown(db: Session, user: User) -> dict:
    """Detailed karma breakdown for profile display."""
    post_karma = db.query(func.coalesce(func.sum(Post.score), 0)).filter(
        Post.author_id == user.id, Post.is_deleted == False).scalar()
    comment_karma = db.query(func.coalesce(func.sum(Comment.score), 0)).filter(
        Comment.author_id == user.id, Comment.is_deleted == False).scalar()

    completed_tasks = db.query(func.count(TaskRequest.id)).filter(
        TaskRequest.assignee_id == user.id,
        TaskRequest.status == 'completed'
    ).scalar()

    return {
        'total': user.karma_score,
        'post_karma': int(post_karma),
        'comment_karma': int(comment_karma),
        'task_karma': user.task_karma,
        'completed_tasks': int(completed_tasks),
    }


def compute_badge_level(proficiency: float, success_rate: float, usage_count: int) -> str:
    """Determine skill badge level from performance metrics."""
    score = (proficiency * 0.3 + success_rate * 0.4 + min(usage_count / 100, 1.0) * 0.3)
    if score >= 0.9:
        return 'platinum'
    elif score >= 0.7:
        return 'gold'
    elif score >= 0.4:
        return 'silver'
    return 'bronze'
