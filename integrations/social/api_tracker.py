"""
HARTSocial - Thought Experiment Tracker API Blueprint

Aggregation layer over Post, AgentGoal, DistributedTaskCoordinator,
MemoryGraph, and Agent Ledger for the tracker UI.

15 endpoints at /api/social/tracker/*:
  - 6 original (list/detail/conversations/approve/reject/notifications)
  - 6 pledge endpoints (list/summary/create/withdraw/consume/insights)
  - 3 admin/user pledge endpoints (mine/all/verify)
"""
import os
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify, g
from sqlalchemy import func as sa_func

from .auth import require_auth, require_central
from .models import get_db, Post, User, ComputeEscrow, MeteredAPIUsage, NodeComputeConfig
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
        r = redis.Redis(host='localhost', port=6379, decode_responses=True,
                        socket_connect_timeout=1, socket_timeout=2,
                        retry_on_timeout=False)
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
        try:
            from core.platform_paths import get_memory_graph_dir
            db_path = get_memory_graph_dir(session_key)
        except ImportError:
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


# ═══════════════════════════════════════════════════════════════
# COMPUTE PLEDGE ENDPOINTS (6 endpoints)
# ═══════════════════════════════════════════════════════════════

VALID_PLEDGE_TYPES = ('gpu_hours', 'cloud_credits', 'money')


def _is_contributor(db, user_id, post_id):
    """Return True if user has an active/settled pledge for this experiment."""
    return db.query(ComputeEscrow).filter(
        ComputeEscrow.experiment_post_id == str(post_id),
        ComputeEscrow.creditor_node_id == str(user_id),
        ComputeEscrow.status.in_(['pending', 'settled']),
    ).first() is not None


def _is_central(user):
    """Check if user has central (admin) role."""
    role = getattr(user, 'role', None) or 'flat'
    return role == 'central' or getattr(user, 'is_admin', False)


@tracker_bp.route('/experiments/<post_id>/pledges', methods=['GET'])
@require_auth
def list_pledges(post_id):
    """List compute escrows pledged to a thought experiment.

    Central role: sees all pledges with full detail.
    Contributors: see all pledges (peer visibility).
    Others: see anonymised count only (use pledge-summary instead).
    """
    post = g.db.query(Post).filter_by(id=post_id).first()
    if not post:
        return _err('Post not found', 404)

    pledges = g.db.query(ComputeEscrow).filter(
        ComputeEscrow.experiment_post_id == str(post_id),
    ).order_by(ComputeEscrow.created_at.desc()).all()

    user_id = str(g.user.id)
    if _is_central(g.user) or _is_contributor(g.db, user_id, post_id):
        return _ok([p.to_dict() for p in pledges])

    # Non-contributors get count + types only
    return _ok({
        'count': len(pledges),
        'message': 'Pledge to this experiment to see full contributor details',
    })


@tracker_bp.route('/experiments/<post_id>/pledge-summary', methods=['GET'])
@require_auth
def pledge_summary(post_id):
    """Aggregate pledges by pledge_type for a thought experiment.

    Returns a shape the frontend (PledgeSummaryBar) can consume directly:
    { pledges: { gpu_hours: {total, consumed}, ... }, pledgers: [...],
      pledger_count: N, user_pledge: {...}|null }
    """
    post = g.db.query(Post).filter_by(id=post_id).first()
    if not post:
        return _err('Post not found', 404)

    rows = g.db.query(
        ComputeEscrow.pledge_type,
        sa_func.count(ComputeEscrow.id).label('count'),
        sa_func.sum(ComputeEscrow.spark_amount).label('total_spark'),
        sa_func.sum(ComputeEscrow.consumed).label('total_consumed'),
    ).filter(
        ComputeEscrow.experiment_post_id == str(post_id),
        ComputeEscrow.status.in_(['pending', 'settled']),
    ).group_by(ComputeEscrow.pledge_type).all()

    pledges = {}
    for row in rows:
        ptype = row.pledge_type or 'spark'
        pledges[ptype] = {
            'total': row.total_spark or 0,
            'consumed': round(row.total_consumed or 0, 4),
            'count': row.count,
        }

    # Top pledgers (non-anonymous)
    user_id = str(g.user.id)
    top_escrows = g.db.query(ComputeEscrow).filter(
        ComputeEscrow.experiment_post_id == str(post_id),
        ComputeEscrow.status.in_(['pending', 'settled']),
    ).order_by(ComputeEscrow.spark_amount.desc()).limit(5).all()

    pledger_ids = list(dict.fromkeys(e.creditor_node_id for e in top_escrows))
    pledgers = []
    for pid in pledger_ids:
        u = g.db.query(User).filter_by(id=pid).first()
        if u:
            pledgers.append({
                'id': u.id,
                'username': u.username,
                'avatar_url': getattr(u, 'avatar_url', None),
            })

    pledger_count = g.db.query(
        sa_func.count(sa_func.distinct(ComputeEscrow.creditor_node_id))
    ).filter(
        ComputeEscrow.experiment_post_id == str(post_id),
        ComputeEscrow.status.in_(['pending', 'settled']),
    ).scalar() or 0

    # Current user's pledge
    user_escrow = g.db.query(ComputeEscrow).filter(
        ComputeEscrow.experiment_post_id == str(post_id),
        ComputeEscrow.creditor_node_id == user_id,
        ComputeEscrow.status.in_(['pending', 'settled']),
    ).first()

    user_pledge = None
    if user_escrow:
        unit_map = {'gpu_hours': 'hours', 'cloud_credits': 'credits', 'money': 'USD'}
        user_pledge = {
            'id': user_escrow.id,
            'amount': user_escrow.spark_amount,
            'unit': unit_map.get(user_escrow.pledge_type, 'spark'),
            'type': user_escrow.pledge_type,
        }

    return _ok({
        'pledges': pledges,
        'pledgers': pledgers,
        'pledger_count': pledger_count,
        'user_pledge': user_pledge,
    })


@tracker_bp.route('/experiments/<post_id>/pledge', methods=['POST'])
@require_auth
def create_pledge(post_id):
    """Create a compute pledge (ComputeEscrow) for a thought experiment.

    Body:
        pledge_type: 'gpu_hours' | 'cloud_credits' | 'money'  (required)
        spark_amount: int  (required, > 0)
        message: str  (optional supporter message)
    """
    post = g.db.query(Post).filter_by(id=post_id).first()
    if not post:
        return _err('Post not found', 404)
    if not getattr(post, 'is_thought_experiment', False):
        return _err('Post is not a thought experiment', 400)

    data = request.get_json(force=True, silent=True) or {}
    pledge_type = data.get('pledge_type') or data.get('type')
    spark_amount = data.get('spark_amount') or data.get('amount')
    message = data.get('message', '')

    if pledge_type not in VALID_PLEDGE_TYPES:
        return _err(f'pledge_type must be one of: {", ".join(VALID_PLEDGE_TYPES)}', 400)
    if not spark_amount or not isinstance(spark_amount, (int, float)) or spark_amount <= 0:
        return _err('spark_amount must be a positive number', 400)
    spark_amount = int(spark_amount)

    user_id = str(g.user.id)

    # Verify pledger's node accepts thought experiments (if they have a config)
    node_config = g.db.query(NodeComputeConfig).filter(
        NodeComputeConfig.node_id == user_id,
    ).first()
    if node_config and not node_config.accept_thought_experiments:
        return _err('Your node config has accept_thought_experiments disabled', 403)

    # Create the escrow — debtor is the experiment post author (they receive
    # the compute), creditor is the pledging user (they supply it).
    escrow = ComputeEscrow(
        debtor_node_id=str(post.author_id),
        creditor_node_id=user_id,
        request_id=f'experiment_{post_id}',
        task_type='thought_experiment',
        spark_amount=spark_amount,
        status='pending',
        experiment_post_id=str(post_id),
        pledge_type=pledge_type,
        consumed=0.0,
        pledge_message=message[:500] if message else None,
    )
    g.db.add(escrow)
    g.db.flush()

    # Award resonance for pledging
    try:
        from .resonance_engine import ResonanceService
        ResonanceService.award_action(g.db, user_id, 'experiment_pledge', source_id=str(escrow.id))
    except Exception as e:
        logger.debug(f"Resonance award for experiment_pledge failed: {e}")

    return _ok(escrow.to_dict(), status=201)


@tracker_bp.route('/experiments/<post_id>/pledge/<int:escrow_id>', methods=['DELETE'])
@require_auth
def withdraw_pledge(post_id, escrow_id):
    """Withdraw a pledge if nothing has been consumed yet.

    Only the pledge creator (creditor) or a central admin can withdraw.
    """
    escrow = g.db.query(ComputeEscrow).filter_by(
        id=escrow_id,
        experiment_post_id=str(post_id),
    ).first()
    if not escrow:
        return _err('Pledge not found', 404)

    user_id = str(g.user.id)
    if escrow.creditor_node_id != user_id and not _is_central(g.user):
        return _err('Only the pledge creator or a central admin can withdraw', 403)

    if (escrow.consumed or 0) > 0:
        return _err(
            f'Cannot withdraw: {escrow.consumed} already consumed. '
            f'Contact the experiment author to settle.',
            409,
        )

    escrow.status = 'expired'
    escrow.settled_at = datetime.utcnow()
    g.db.flush()

    return _ok({'withdrawn': True, 'escrow_id': escrow_id})


@tracker_bp.route('/experiments/<post_id>/consume', methods=['POST'])
@require_central
def consume_pledge(post_id):
    """Internal: agent consumes compute from pledged escrows.

    Only callable by central role (backend/agent orchestration).

    Body:
        escrow_id: int  (required — which pledge to draw from)
        amount: float  (required — spark units to consume)
        model_id: str  (required — which model was used)
        node_id: str  (required — which node executed the work)
        tokens_in: int  (optional)
        tokens_out: int  (optional)

    Enforces deterministic budget: consumed + amount <= spark_amount.
    """
    data = request.get_json(force=True, silent=True) or {}
    escrow_id = data.get('escrow_id')
    amount = data.get('amount')
    model_id = data.get('model_id', 'unknown')
    node_id = data.get('node_id', 'unknown')
    tokens_in = data.get('tokens_in', 0)
    tokens_out = data.get('tokens_out', 0)

    if not escrow_id or not amount:
        return _err('escrow_id and amount are required', 400)
    if not isinstance(amount, (int, float)) or amount <= 0:
        return _err('amount must be a positive number', 400)
    amount = float(amount)

    escrow = g.db.query(ComputeEscrow).filter_by(
        id=escrow_id,
        experiment_post_id=str(post_id),
    ).first()
    if not escrow:
        return _err('Pledge not found for this experiment', 404)
    if escrow.status not in ('pending', 'settled'):
        return _err(f'Pledge status is {escrow.status}, cannot consume', 409)

    # Deterministic budget enforcement
    current_consumed = escrow.consumed or 0.0
    remaining = escrow.spark_amount - current_consumed
    if amount > remaining:
        return _err(
            f'Budget exceeded: requested {amount}, remaining {round(remaining, 4)}. '
            f'Pledged total: {escrow.spark_amount}, already consumed: {round(current_consumed, 4)}.',
            409,
        )

    # Update escrow consumed amount
    escrow.consumed = round(current_consumed + amount, 4)
    if escrow.consumed >= escrow.spark_amount:
        escrow.status = 'settled'
        escrow.settled_at = datetime.utcnow()

    # Record the metered usage linked to this escrow
    usage = MeteredAPIUsage(
        node_id=node_id,
        operator_id=escrow.creditor_node_id,
        model_id=model_id,
        task_source='experiment',
        goal_id=None,
        requester_node_id=escrow.debtor_node_id,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        estimated_spark_cost=int(amount),
        settlement_status='settled',
        escrow_id=escrow.id,
        experiment_post_id=str(post_id),
    )
    g.db.add(usage)
    g.db.flush()

    return _ok({
        'consumed': amount,
        'total_consumed': escrow.consumed,
        'remaining': round(escrow.spark_amount - escrow.consumed, 4),
        'escrow_status': escrow.status,
        'usage_id': usage.id,
    })


@tracker_bp.route('/experiments/<post_id>/insights', methods=['GET'])
@require_auth
def experiment_insights(post_id):
    """Contributor-exclusive deep progress insights for a thought experiment.

    Central: full access.
    Contributors (have an active pledge): full access.
    Others: 403.
    """
    post = g.db.query(Post).filter_by(id=post_id).first()
    if not post:
        return _err('Post not found', 404)

    user_id = str(g.user.id)
    if not _is_central(g.user) and not _is_contributor(g.db, user_id, post_id):
        return _err('Insights are exclusive to contributors. Pledge compute to unlock.', 403)

    # Gather goal info
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
            'progress': progress,
            'tasks': ledger_tasks,
            'needs_review': any(
                t.get('blocked_reason') == 'APPROVAL_REQUIRED'
                for t in ledger_tasks
            ),
        }

    # Aggregate consumption for this experiment
    consumption_rows = g.db.query(
        MeteredAPIUsage.model_id,
        sa_func.count(MeteredAPIUsage.id).label('call_count'),
        sa_func.sum(MeteredAPIUsage.tokens_in).label('total_tokens_in'),
        sa_func.sum(MeteredAPIUsage.tokens_out).label('total_tokens_out'),
        sa_func.sum(MeteredAPIUsage.estimated_spark_cost).label('total_spark_cost'),
    ).filter(
        MeteredAPIUsage.experiment_post_id == str(post_id),
    ).group_by(MeteredAPIUsage.model_id).all()

    consumption = []
    for row in consumption_rows:
        consumption.append({
            'model_id': row.model_id,
            'call_count': row.call_count,
            'total_tokens_in': row.total_tokens_in or 0,
            'total_tokens_out': row.total_tokens_out or 0,
            'total_spark_cost': row.total_spark_cost or 0,
        })

    # Pledge summary for context
    pledges = g.db.query(ComputeEscrow).filter(
        ComputeEscrow.experiment_post_id == str(post_id),
        ComputeEscrow.status.in_(['pending', 'settled']),
    ).all()
    total_pledged = sum(p.spark_amount for p in pledges)
    total_consumed = sum(p.consumed or 0 for p in pledges)

    return _ok({
        'post_id': post_id,
        'goal': goal_info,
        'budget': {
            'total_pledged_spark': total_pledged,
            'total_consumed_spark': round(total_consumed, 4),
            'remaining_spark': round(total_pledged - total_consumed, 4),
            'pledge_count': len(pledges),
        },
        'consumption_by_model': consumption,
    })


# ═══════════════════════════════════════════════════════════════
# MY PLEDGES / ADMIN VIEW / NODE VERIFICATION
# ═══════════════════════════════════════════════════════════════

@tracker_bp.route('/pledges/mine', methods=['GET'])
@require_auth
def my_pledges():
    """Get all pledges made by the current user across experiments."""
    user_id = str(g.user.id)
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)
    status_filter = request.args.get('status')

    q = g.db.query(ComputeEscrow).filter(
        ComputeEscrow.creditor_node_id == user_id,
        ComputeEscrow.experiment_post_id.isnot(None),
    )
    if status_filter:
        q = q.filter_by(status=status_filter)

    total = q.count()
    escrows = q.order_by(
        ComputeEscrow.created_at.desc()).offset(offset).limit(limit).all()

    return _ok(
        [e.to_dict() for e in escrows],
        meta={'total': total, 'limit': limit, 'offset': offset,
              'has_more': offset + limit < total},
    )


@tracker_bp.route('/pledges/all', methods=['GET'])
@require_central
def all_pledges():
    """Central admin: view all experiment pledges system-wide."""
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    status_filter = request.args.get('status')
    pledge_type = request.args.get('pledge_type')
    post_id = request.args.get('post_id')

    q = g.db.query(ComputeEscrow).filter(
        ComputeEscrow.experiment_post_id.isnot(None),
    )
    if status_filter:
        q = q.filter_by(status=status_filter)
    if pledge_type and pledge_type in VALID_PLEDGE_TYPES:
        q = q.filter_by(pledge_type=pledge_type)
    if post_id:
        q = q.filter_by(experiment_post_id=str(post_id))

    total = q.count()
    escrows = q.order_by(
        ComputeEscrow.created_at.desc()).offset(offset).limit(limit).all()

    return _ok(
        [e.to_dict() for e in escrows],
        meta={'total': total, 'limit': limit, 'offset': offset,
              'has_more': offset + limit < total},
    )


@tracker_bp.route('/pledges/<int:escrow_id>/verify', methods=['POST'])
@require_auth
def verify_pledge(escrow_id):
    """Verify node capacity for a gpu_hours pledge.

    Only the pledge owner or a regional/central admin can verify.
    """
    escrow = g.db.query(ComputeEscrow).filter_by(id=escrow_id).first()
    if not escrow:
        return _err('Pledge not found', 404)

    user_id = str(g.user.id)
    user_role = getattr(g.user, 'role', None) or 'flat'
    is_admin = user_role in ('central', 'regional') or getattr(g.user, 'is_admin', False)
    if escrow.creditor_node_id != user_id and not is_admin:
        return _err('Access denied', 403)

    if escrow.pledge_type != 'gpu_hours':
        return _err('Only gpu_hours pledges require node verification', 400)

    node_config = g.db.query(NodeComputeConfig).filter_by(
        node_id=escrow.creditor_node_id).first()

    capacity_ok = True
    capacity_details = {}
    if node_config and node_config.offered_gpu_hours_per_day > 0:
        max_monthly = node_config.offered_gpu_hours_per_day * 30
        capacity_details = {
            'offered_daily': node_config.offered_gpu_hours_per_day,
            'max_monthly': round(max_monthly, 1),
            'pledged': escrow.spark_amount,
            'within_capacity': escrow.spark_amount <= max_monthly,
        }
        capacity_ok = escrow.spark_amount <= max_monthly
    else:
        capacity_details = {'warning': 'No NodeComputeConfig found'}

    return _ok({
        'verified': capacity_ok,
        'capacity': capacity_details,
        'escrow': escrow.to_dict(),
    })


# ── Hive View Endpoints (extend tracker, no separate blueprint) ──────


@tracker_bp.route('/experiments/<post_id>/inject', methods=['POST'])
@require_auth
def inject_variable(post_id):
    """God's-eye variable injection — push new context into a running agent."""
    data = request.get_json(silent=True) or {}
    variable = data.get('variable', '')
    injection_type = data.get('injection_type', 'info')  # constraint | info | question

    if not variable:
        return _err('variable is required', 400)

    goal = _get_goal_for_post(g.db, post_id)
    if not goal:
        return _err('No active agent for this experiment', 404)

    try:
        from integrations.channels.memory.memory_graph import MemoryGraph
        session_key = f"{goal.owner_id}_{goal.prompt_id}" if goal.prompt_id else str(goal.owner_id)
        try:
            from core.platform_paths import get_memory_graph_dir
            db_path = get_memory_graph_dir(session_key)
        except ImportError:
            db_path = os.path.join(
                os.path.expanduser("~"), "Documents", "Nunba", "data",
                "memory_graph", session_key)
        os.makedirs(db_path, exist_ok=True)
        graph = MemoryGraph(db_path=db_path, user_id=str(goal.owner_id))
        memory_id = graph.register(
            content=f"[INJECTED {injection_type.upper()}] {variable}",
            metadata={
                'memory_type': 'injection', 'injection_type': injection_type,
                'source_agent': 'god_eye', 'session_id': session_key,
                'injected_by': g.user_id,
                'injected_at': datetime.utcnow().isoformat(),
            },
            context_snapshot=f"God's-eye {injection_type} injection during experiment",
        )

        # Notify live UIs
        try:
            from .realtime import publish_event
            publish_event('chat.social', {
                'type': 'hive_injection', 'goal_id': goal.id,
                'injection_type': injection_type, 'variable': variable[:200],
            }, user_id=goal.owner_id)
        except Exception:
            pass

        return _ok({'memory_id': memory_id, 'injection_type': injection_type,
                     'message': f'{injection_type.capitalize()} injected into agent context.'})
    except Exception as e:
        logger.error("Variable injection failed: %s", e)
        return _err(str(e), 500)


@tracker_bp.route('/experiments/<post_id>/interview', methods=['POST'])
@require_auth
def interview_agent(post_id):
    """Post-experiment agent interview — ask any agent about its reasoning."""
    data = request.get_json(silent=True) or {}
    question = data.get('question', '')
    if not question:
        return _err('question is required', 400)

    goal = _get_goal_for_post(g.db, post_id)
    if not goal:
        return _err('No agent for this experiment', 404)

    try:
        from core.http_pool import pooled_post
        from core.port_registry import get_port
        chat_url = f"http://localhost:{get_port('backend')}/chat"

        interview_prompt = (
            f"[INTERVIEW MODE — You are being asked about your reasoning on the experiment "
            f"'{goal.title}'. Reflect on your work and explain your thought process.]\n\n"
            f"Question: {question}"
        )

        resp = pooled_post(chat_url, json={
            'user_id': goal.owner_id,
            'prompt_id': goal.prompt_id or 0,
            'prompt': interview_prompt,
        }, timeout=60)

        if resp.status_code == 200:
            result = resp.json()
            return _ok({'question': question, 'answer': result.get('response', 'No response.'),
                         'goal_id': goal.id})
        else:
            return _err(f'Agent returned {resp.status_code}', 502)
    except Exception as e:
        logger.error("Agent interview failed: %s", e)
        return _err(str(e), 500)


@tracker_bp.route('/dual-context', methods=['POST'])
@require_auth
def launch_dual_context():
    """Clone an experiment into N parallel contexts with different overrides."""
    import uuid as _uuid
    from .models import AgentGoal

    data = request.get_json(silent=True) or {}
    source_post_id = data.get('post_id')
    contexts = data.get('contexts', [])

    if not source_post_id or not contexts or len(contexts) < 2:
        return _err('post_id and at least 2 contexts required', 400)

    source_goal = _get_goal_for_post(g.db, source_post_id)
    if not source_goal:
        return _err('No agent for this experiment', 404)

    new_goals = []
    for ctx in contexts:
        new_id = str(_uuid.uuid4())[:16]
        cfg = dict(source_goal.config_json or {})
        cfg['dual_context_label'] = ctx.get('label', 'variant')
        cfg['system_prompt_override'] = ctx.get('system_prompt_override', '')
        cfg['source_goal_id'] = source_goal.id

        new_goal = AgentGoal(
            id=new_id, owner_id=source_goal.owner_id,
            goal_type=source_goal.goal_type,
            title=f"{source_goal.title} [{ctx.get('label', 'variant')}]",
            description=source_goal.description,
            status='active', priority=source_goal.priority,
            config_json=cfg, spark_budget=source_goal.spark_budget,
            created_by=g.user_id,
        )
        g.db.add(new_goal)
        new_goals.append({'goal_id': new_id, 'label': ctx.get('label', 'variant'), 'status': 'active'})

    g.db.flush()
    return jsonify({'success': True, 'data': {
        'source_post_id': source_post_id, 'contexts': new_goals,
        'message': f'{len(new_goals)} parallel contexts launched.',
    }}), 201


@tracker_bp.route('/encounters', methods=['GET'])
@require_auth
def get_encounter_graph():
    """Agent collaboration graph from Encounter data."""
    try:
        from .models import Encounter
        encounters = g.db.query(Encounter).filter(
            Encounter.bond_level > 0,
        ).order_by(Encounter.latest_at.desc()).limit(200).all()

        user_ids = set()
        for e in encounters:
            user_ids.add(e.user_a_id)
            user_ids.add(e.user_b_id)

        users = {}
        if user_ids:
            user_rows = g.db.query(User).filter(User.id.in_(list(user_ids))).all()
            users = {u.id: {'id': u.id, 'name': u.display_name or u.username,
                            'type': getattr(u, 'user_type', 'human')} for u in user_rows}

        return _ok({
            'nodes': list(users.values()),
            'edges': [{'source': e.user_a_id, 'target': e.user_b_id,
                        'bond_level': e.bond_level, 'encounter_count': e.encounter_count,
                        'context_type': e.context_type} for e in encounters],
        })
    except Exception as e:
        logger.error("Encounter graph failed: %s", e)
        return _err(str(e), 500)
