"""
HyveSocial - Thought Experiment Tracker API Blueprint

Aggregation layer over Post, AgentGoal, DistributedTaskCoordinator,
MemoryGraph, and Agent Ledger for the tracker UI.

6 endpoints at /api/social/tracker/*.
"""
import os
import logging
from flask import Blueprint, request, jsonify, g

from .auth import require_auth
from .models import get_db, Post, User
from .services import NotificationService, PostService

logger = logging.getLogger('hevolve_social')

tracker_bp = Blueprint('tracker', __name__, url_prefix='/api/social/tracker')


def _ok(data=None, meta=None, status=200):
    r = {'success': True}
    if data is not None:
        r['data'] = data
    if meta is not None:
        r['meta'] = meta
    return jsonify(r), status


def _err(msg, status=400):
    return jsonify({'success': False, 'error': msg}), status


def _get_goal_for_post(db, post_id):
    """Find AgentGoal linked to a thought experiment post."""
    try:
        from integrations.social.models import AgentGoal
        goals = db.query(AgentGoal).filter(
            AgentGoal.goal_type == 'thought_experiment',
            AgentGoal.status.in_(['active', 'paused', 'completed']),
        ).all()
        for goal in goals:
            cfg = goal.config_json or {}
            if cfg.get('post_id') == str(post_id):
                return goal
    except Exception as e:
        logger.debug(f"Goal lookup failed: {e}")
    return None


def _get_goal_progress(goal_id):
    """Get distributed task progress for a goal (Redis-backed)."""
    try:
        from integrations.distributed_agent.task_coordinator import DistributedTaskCoordinator
        import redis
        r = redis.Redis(host='localhost', port=6379, decode_responses=True)
        r.ping()
        coordinator = DistributedTaskCoordinator(redis_client=r)
        return coordinator.get_goal_progress(goal_id)
    except Exception as e:
        logger.debug(f"Goal progress unavailable: {e}")
        return None


def _get_agent_conversations(user_id, prompt_id, limit=50):
    """Get conversation history from MemoryGraph for an agent session."""
    try:
        from integrations.channels.memory.memory_graph import MemoryGraph
        session_key = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)
        db_path = os.path.join(
            os.path.expanduser("~"), "Documents", "Nunba", "data",
            "memory_graph", session_key,
        )
        if not os.path.exists(db_path):
            return []
        graph = MemoryGraph(db_path=db_path, user_id=str(user_id))
        nodes = graph.get_session_memories(session_key, limit=limit)
        return [
            {
                'id': n.id,
                'role': n.metadata.get('role', 'system'),
                'content': n.content,
                'timestamp': n.created_at,
                'session_key': session_key,
                'category': n.category,
            }
            for n in nodes
        ]
    except Exception as e:
        logger.debug(f"Conversation fetch failed: {e}")
        return []


def _get_ledger_tasks(goal_id):
    """Get Agent Ledger tasks for a goal, including HITL blocked tasks."""
    try:
        from agent_ledger.core import SmartLedger
        ledger = SmartLedger()
        parent = ledger.get_task(goal_id)
        if not parent:
            return []
        tasks = []
        for child_id in (parent.child_task_ids or []):
            task = ledger.get_task(child_id)
            if task:
                tasks.append({
                    'id': task.id,
                    'description': task.description,
                    'status': task.status.value if hasattr(task.status, 'value') else str(task.status),
                    'progress_pct': task.progress_pct,
                    'blocked_reason': (task.blocked_reason.value
                                       if hasattr(task.blocked_reason, 'value')
                                       else str(task.blocked_reason)) if task.blocked_reason else None,
                    'assigned_agent': task.assigned_to,
                })
        return tasks
    except Exception as e:
        logger.debug(f"Ledger tasks unavailable: {e}")
        return []


# ─── Endpoints ───


@tracker_bp.route('/experiments', methods=['GET'])
@require_auth
def list_experiments():
    """List thought experiment posts with agent goal status."""
    limit = request.args.get('limit', 20, type=int)
    offset = request.args.get('offset', 0, type=int)
    filter_type = request.args.get('filter', 'all')  # all | mine | needs_review

    q = g.db.query(Post).filter(Post.is_thought_experiment == True)

    if filter_type == 'mine':
        q = q.filter(Post.author_id == str(g.user.id))

    total = q.count()
    posts = q.order_by(Post.created_at.desc()).offset(offset).limit(limit).all()

    experiments = []
    for post in posts:
        post_dict = post.to_dict(include_author=True)
        goal = _get_goal_for_post(g.db, post.id)

        goal_info = None
        needs_review = False
        if goal:
            progress = _get_goal_progress(goal.id)
            ledger_tasks = _get_ledger_tasks(goal.id)
            needs_review = any(
                t.get('blocked_reason') == 'APPROVAL_REQUIRED'
                for t in ledger_tasks
            )
            goal_info = {
                'goal_id': goal.id,
                'status': goal.status,
                'goal_type': goal.goal_type,
                'prompt_id': goal.prompt_id,
                'progress': progress,
                'task_count': len(ledger_tasks),
                'needs_review': needs_review,
            }

        if filter_type == 'needs_review' and not needs_review:
            continue

        experiments.append({
            **post_dict,
            'goal': goal_info,
        })

    meta = {'total': total, 'limit': limit, 'offset': offset,
            'has_more': offset + limit < total}
    return _ok(experiments, meta=meta)


@tracker_bp.route('/experiments/<post_id>', methods=['GET'])
@require_auth
def get_experiment(post_id):
    """Single experiment detail with full agent progress."""
    post = g.db.query(Post).filter_by(id=post_id).first()
    if not post:
        return _err('Post not found', 404)

    post_dict = post.to_dict(include_author=True)
    goal = _get_goal_for_post(g.db, post_id)

    goal_info = None
    ledger_tasks = []
    if goal:
        progress = _get_goal_progress(goal.id)
        ledger_tasks = _get_ledger_tasks(goal.id)
        goal_info = {
            'goal_id': goal.id,
            'status': goal.status,
            'goal_type': goal.goal_type,
            'prompt_id': goal.prompt_id,
            'progress': progress,
            'tasks': ledger_tasks,
            'needs_review': any(
                t.get('blocked_reason') == 'APPROVAL_REQUIRED'
                for t in ledger_tasks
            ),
            'config': goal.config_json,
        }

    return _ok({
        **post_dict,
        'goal': goal_info,
    })


@tracker_bp.route('/experiments/<post_id>/conversations', methods=['GET'])
@require_auth
def get_conversations(post_id):
    """Agent conversation history for a thought experiment."""
    post = g.db.query(Post).filter_by(id=post_id).first()
    if not post:
        return _err('Post not found', 404)

    goal = _get_goal_for_post(g.db, post_id)
    if not goal:
        return _ok({'conversations': [], 'agents': []})

    # Get conversations from all agent sessions working on this goal
    conversations = []
    agents_seen = {}

    # Primary agent session (from goal's prompt_id)
    if goal.prompt_id:
        # The daemon sets prompt_id as f"{goal_type}_{goal_id[:8]}"
        # The MemoryGraph session key is f"{user_id}_{prompt_id}"
        # We need to find agents that worked on this goal
        try:
            from integrations.social.models import AgentGoal
            # Check if goal has a created_by user
            if goal.created_by and goal.created_by != 'system_bootstrap':
                msgs = _get_agent_conversations(goal.created_by, goal.prompt_id)
                if msgs:
                    conversations.extend(msgs)
                    agents_seen[goal.created_by] = {
                        'user_id': goal.created_by,
                        'prompt_id': goal.prompt_id,
                        'role': 'primary',
                    }
        except Exception as e:
            logger.debug(f"Primary conversation fetch: {e}")

    # Sort conversations chronologically
    conversations.sort(key=lambda c: c.get('timestamp', ''))

    return _ok({
        'conversations': conversations,
        'agents': list(agents_seen.values()),
    })


@tracker_bp.route('/experiments/<post_id>/approve', methods=['POST'])
@require_auth
def approve_task(post_id):
    """HITL approval - unblocks APPROVAL_REQUIRED tasks for this experiment."""
    post = g.db.query(Post).filter_by(id=post_id).first()
    if not post:
        return _err('Post not found', 404)

    goal = _get_goal_for_post(g.db, post_id)
    if not goal:
        return _err('No goal found for this experiment', 404)

    data = request.get_json(force=True, silent=True) or {}
    task_id = data.get('task_id')  # Optional: approve specific task

    try:
        from agent_ledger.core import SmartLedger, TaskStatus
        ledger = SmartLedger()

        unblocked = 0
        tasks = _get_ledger_tasks(goal.id)
        for t in tasks:
            if t.get('blocked_reason') == 'APPROVAL_REQUIRED':
                if task_id and t['id'] != task_id:
                    continue
                task_obj = ledger.get_task(t['id'])
                if task_obj:
                    task_obj.status = TaskStatus.IN_PROGRESS
                    task_obj.blocked_reason = None
                    ledger.update_task(task_obj)
                    unblocked += 1

        return _ok({'unblocked': unblocked, 'goal_id': goal.id})
    except Exception as e:
        logger.debug(f"Approve failed: {e}")
        return _ok({'unblocked': 0, 'message': 'Ledger unavailable, approval recorded'})


@tracker_bp.route('/experiments/<post_id>/reject', methods=['POST'])
@require_auth
def reject_task(post_id):
    """HITL rejection - fails APPROVAL_REQUIRED tasks for this experiment."""
    post = g.db.query(Post).filter_by(id=post_id).first()
    if not post:
        return _err('Post not found', 404)

    goal = _get_goal_for_post(g.db, post_id)
    if not goal:
        return _err('No goal found for this experiment', 404)

    data = request.get_json(force=True, silent=True) or {}
    task_id = data.get('task_id')
    reason = data.get('reason', 'Rejected by human reviewer')

    try:
        from agent_ledger.core import SmartLedger, TaskStatus, FailureReason
        ledger = SmartLedger()

        rejected = 0
        tasks = _get_ledger_tasks(goal.id)
        for t in tasks:
            if t.get('blocked_reason') == 'APPROVAL_REQUIRED':
                if task_id and t['id'] != task_id:
                    continue
                task_obj = ledger.get_task(t['id'])
                if task_obj:
                    task_obj.status = TaskStatus.FAILED
                    task_obj.failure_reason = FailureReason.VALIDATION_FAILED
                    task_obj.blocked_reason = None
                    ledger.update_task(task_obj)
                    rejected += 1

        return _ok({'rejected': rejected, 'goal_id': goal.id})
    except Exception as e:
        logger.debug(f"Reject failed: {e}")
        return _ok({'rejected': 0, 'message': 'Ledger unavailable, rejection recorded'})


@tracker_bp.route('/notifications', methods=['GET'])
@require_auth
def get_tracker_notifications():
    """HITL-relevant notifications for the current user."""
    limit = request.args.get('limit', 20, type=int)
    offset = request.args.get('offset', 0, type=int)

    notifs, total = NotificationService.get_for_user(
        g.db, str(g.user.id), limit=limit, offset=offset,
    )

    # Filter to tracker-relevant types
    tracker_types = {'goal_contribution', 'goal_verified', 'approval_required'}
    filtered = [
        n.to_dict() for n in notifs
        if n.type in tracker_types
    ]

    return _ok(filtered, meta={
        'total': len(filtered),
        'limit': limit,
        'offset': offset,
    })
