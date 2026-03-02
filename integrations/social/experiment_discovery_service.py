"""
Experiment Discovery Service — Interest-based recommendations and live metrics.

Recommendation scoring:
  score = intent_match * 3.0
        + recency_decay (10 pts at 0h → 0 at 7d)
        + log(contributor_count + 1) * 2.0
        + log(total_votes + 1) * 1.5
        + log(funding_total + 1) * 0.5
        + bond_boost (5 if creator bonded)
        + status_weight

Service Pattern: static methods, db: Session, db.flush() not db.commit().
"""
import math
import logging
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session

logger = logging.getLogger('hevolve_social')


class ExperimentDiscoveryService:

    @staticmethod
    def discover(db: Session, user_id: str = None,
                 intent_filter: str = None,
                 experiment_type: str = None,
                 status_filter: str = None,
                 limit: int = 25, offset: int = 0) -> Dict:
        """Interest-based experiment discovery with personalised ranking."""
        from .models import ThoughtExperiment, ExperimentVote, Post, Encounter

        # 1. Build user interest profile from past votes + posts
        user_intents: Dict[str, int] = {}
        if user_id:
            vote_counts = db.query(
                ThoughtExperiment.intent_category,
                func.count(ExperimentVote.id)
            ).join(
                ExperimentVote,
                ExperimentVote.experiment_id == ThoughtExperiment.id
            ).filter(
                ExperimentVote.voter_id == user_id
            ).group_by(ThoughtExperiment.intent_category).all()

            for cat, cnt in vote_counts:
                if cat:
                    user_intents[cat] = cnt

            post_counts = db.query(
                Post.intent_category, func.count(Post.id)
            ).filter(
                Post.author_id == user_id,
                Post.intent_category.isnot(None)
            ).group_by(Post.intent_category).all()

            for cat, cnt in post_counts:
                if cat:
                    user_intents[cat] = user_intents.get(cat, 0) + cnt

        # 2. Query experiments (fetch more than needed for scoring)
        q = db.query(ThoughtExperiment).filter(
            ThoughtExperiment.status != 'archived'
        )
        if intent_filter:
            q = q.filter(ThoughtExperiment.intent_category == intent_filter)
        if experiment_type:
            q = q.filter(ThoughtExperiment.experiment_type == experiment_type)
        if status_filter:
            q = q.filter(ThoughtExperiment.status == status_filter)

        experiments = q.order_by(desc(ThoughtExperiment.created_at)).limit(200).all()

        # 3. Get bonded user IDs for boost
        bond_user_ids: set = set()
        if user_id:
            bonds = db.query(Encounter).filter(
                or_(Encounter.user_a_id == user_id,
                    Encounter.user_b_id == user_id),
                Encounter.bond_level >= 3
            ).all()
            for b in bonds:
                other = b.user_b_id if b.user_a_id == user_id else b.user_a_id
                bond_user_ids.add(other)

        # 4. Score and rank
        now = datetime.utcnow()
        status_weights = {
            'voting': 3.0, 'discussing': 2.0, 'proposed': 1.0,
            'evaluating': 2.5, 'decided': 0.5,
        }

        scored: List = []
        for exp in experiments:
            score = 0.0

            # Intent match
            if exp.intent_category and exp.intent_category in user_intents:
                score += 3.0 * math.log1p(user_intents[exp.intent_category])

            # Recency decay (10 points at 0h → 0 at 7 days)
            if exp.created_at:
                age_hours = (now - exp.created_at).total_seconds() / 3600
                score += max(0.0, 10.0 - (age_hours / 16.8))

            # Contributor popularity
            score += math.log1p(exp.contributor_count or 0) * 2.0

            # Vote engagement
            score += math.log1p(exp.total_votes or 0) * 1.5

            # Funding signal
            score += math.log1p(exp.funding_total or 0) * 0.5

            # Bond boost
            if exp.creator_id in bond_user_ids:
                score += 5.0

            # Active status boost
            score += status_weights.get(exp.status, 0.0)

            scored.append((score, exp))

        scored.sort(key=lambda x: -x[0])

        # 5. Paginate
        page = scored[offset:offset + limit]

        # 6. Enrich with post metrics
        results = []
        for score_val, exp in page:
            d = exp.to_dict()
            d['discovery_score'] = round(score_val, 2)

            if exp.post_id:
                post = db.query(Post).filter_by(id=exp.post_id).first()
                if post:
                    d['view_count'] = post.view_count or 0
                    d['comment_count'] = post.comment_count or 0
                    d['upvotes'] = post.upvotes or 0
                    d['downvotes'] = post.downvotes or 0
                    if hasattr(post, 'author') and post.author:
                        d['author'] = {
                            'id': post.author.id,
                            'username': post.author.username,
                            'display_name': getattr(post.author, 'display_name', post.author.username),
                        }
            results.append(d)

        return {
            'experiments': results,
            'meta': {
                'total': len(scored),
                'limit': limit,
                'offset': offset,
                'has_more': offset + limit < len(scored),
                'user_intents': user_intents if user_id else {},
            }
        }

    @staticmethod
    def get_experiment_metrics(db: Session, experiment_id: str) -> Optional[Dict]:
        """Get live metrics for a specific experiment, varying by experiment_type."""
        from .models import ThoughtExperiment, ExperimentVote, Post

        exp = db.query(ThoughtExperiment).filter_by(id=experiment_id).first()
        if not exp:
            return None

        metrics: Dict = {
            'experiment_id': experiment_id,
            'experiment_type': exp.experiment_type or 'traditional',
            'contributor_count': exp.contributor_count or 0,
            'funding_total': exp.funding_total or 0,
            'total_votes': exp.total_votes or 0,
            'status': exp.status,
        }

        # Voter breakdown
        votes = db.query(ExperimentVote).filter_by(experiment_id=experiment_id).all()
        metrics['human_voters'] = sum(1 for v in votes if v.voter_type == 'human')
        metrics['agent_voters'] = sum(1 for v in votes if v.voter_type == 'agent')

        # Vote distribution
        support = sum(1 for v in votes if v.vote_value > 0)
        oppose = sum(1 for v in votes if v.vote_value < 0)
        neutral = sum(1 for v in votes if v.vote_value == 0)
        metrics['vote_distribution'] = {
            'support': support, 'oppose': oppose, 'neutral': neutral,
        }

        # Post engagement
        if exp.post_id:
            post = db.query(Post).filter_by(id=exp.post_id).first()
            if post:
                metrics['view_count'] = post.view_count or 0
                metrics['comment_count'] = post.comment_count or 0

        # Type-specific metrics
        if exp.experiment_type == 'physical_ai':
            metrics['camera_feed_url'] = exp.camera_feed_url
            metrics['has_camera'] = bool(exp.camera_feed_url)

        elif exp.experiment_type == 'software':
            metrics['build_stats'] = _get_build_stats(db, experiment_id)

        # Compute contribution from hive nodes
        metrics.update(_get_compute_stats(db))

        return metrics

    @staticmethod
    def record_contribution(db: Session, experiment_id: str, user_id: str,
                            spark_amount: int = 0) -> Optional[Dict]:
        """Record a user contributing to / believing in an experiment."""
        from .models import ThoughtExperiment

        exp = db.query(ThoughtExperiment).filter_by(id=experiment_id).first()
        if not exp:
            return None

        exp.contributor_count = (exp.contributor_count or 0) + 1
        if spark_amount > 0:
            exp.funding_total = (exp.funding_total or 0) + spark_amount

        db.flush()
        return exp.to_dict()


# ─── Internal Helpers ───

def _get_build_stats(db: Session, experiment_id: str) -> Dict:
    """Get build success rates from CodingTask model linked via AgentGoal."""
    try:
        from .models import CodingTask
        # CodingTasks linked to experiment via goal config
        # For now, aggregate all coding tasks (can filter by experiment later)
        tasks = db.query(CodingTask).filter(
            CodingTask.status.in_(['merged', 'failed', 'in_progress', 'review', 'assigned'])
        ).limit(100).all()

        total = len(tasks)
        merged = sum(1 for t in tasks if t.status == 'merged')
        failed = sum(1 for t in tasks if t.status == 'failed')
        in_progress = sum(1 for t in tasks if t.status in ('assigned', 'in_progress'))
        in_review = sum(1 for t in tasks if t.status == 'review')

        return {
            'total_tasks': total,
            'merged': merged,
            'failed': failed,
            'in_review': in_review,
            'in_progress': in_progress,
            'success_rate': round(merged / total, 3) if total else 0.0,
        }
    except Exception as e:
        logger.debug("Build stats unavailable: %s", e)
        return {}


def _get_compute_stats(db: Session) -> Dict:
    """Get hive compute stats from PeerNode + NodeComputeConfig."""
    try:
        from .models import PeerNode, NodeComputeConfig
        nodes = db.query(PeerNode).join(
            NodeComputeConfig, NodeComputeConfig.node_id == PeerNode.node_id
        ).filter(
            NodeComputeConfig.accept_thought_experiments == True,  # noqa: E712
            PeerNode.status == 'active',
        ).all()
        return {
            'compute_nodes': len(nodes),
            'total_gpu_hours': round(sum(n.gpu_hours_served or 0 for n in nodes), 1),
            'total_inferences': sum(n.total_inferences or 0 for n in nodes),
        }
    except Exception as e:
        logger.debug("Compute stats unavailable: %s", e)
        return {'compute_nodes': 0, 'total_gpu_hours': 0, 'total_inferences': 0}
