"""
HARTSocial - Compute Pledge API Blueprint

Allows users to pledge compute resources (GPU hours, money, cloud credits)
to thought experiments.  Agents deterministically consume only what has been
pledged -- the consume endpoint is the hard budget enforcement point.

POST   /api/social/experiments/<post_id>/pledge          -- Create pledge
DELETE /api/social/experiments/<post_id>/pledge/<id>      -- Withdraw pledge
GET    /api/social/experiments/<post_id>/pledges          -- List pledges
GET    /api/social/experiments/<post_id>/pledge-summary   -- Aggregated budget view
GET    /api/social/experiments/<post_id>/insights         -- Contributor-exclusive progress
POST   /api/social/experiments/<post_id>/consume          -- Agent consumes pledged resources
GET    /api/social/pledges/mine                           -- My pledges across experiments
GET    /api/social/pledges/all                            -- Central admin: all pledges
POST   /api/social/pledges/<id>/verify                    -- Verify node capacity
"""
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, g
from sqlalchemy import func as sa_func

from .auth import require_auth, require_central
from .rate_limiter import rate_limit

logger = logging.getLogger('hevolve_social')

compute_pledge_bp = Blueprint('compute_pledge', __name__,
                              url_prefix='/api/social')


# ── helpers ──────────────────────────────────────────────────────────

def _ok(data=None, meta=None, status=200):
    r = {'success': True}
    if data is not None:
        r['data'] = data
    if meta is not None:
        r['meta'] = meta
    return jsonify(r), status


def _err(msg, status=400):
    return jsonify({'success': False, 'error': msg}), status


def _get_json():
    return request.get_json(force=True, silent=True) or {}


VALID_PLEDGE_TYPES = ('gpu_hours', 'cloud_credits', 'money')
UNIT_FOR_TYPE = {
    'gpu_hours': 'hours',
    'cloud_credits': 'credits',
    'money': 'USD',
}


def _validate_post_is_experiment(db, post_id):
    """Return the Post if it is a thought experiment, else None."""
    from .models import Post
    post = db.query(Post).filter_by(id=str(post_id)).first()
    if not post:
        return None
    if not post.is_thought_experiment:
        return None
    return post


# ═══════════════════════════════════════════════════════════════
# PLEDGE MANAGEMENT
# ═══════════════════════════════════════════════════════════════

@compute_pledge_bp.route('/experiments/<post_id>/pledge', methods=['POST'])
@require_auth
@rate_limit(10)
def create_pledge(post_id):
    """Pledge compute resources to a thought experiment.

    Body: {pledge_type, amount, unit?, message?, anonymous?, node_id?}
    """
    from .models import get_db, ComputePledge, PeerNode
    from .resonance_engine import ResonanceService

    db = get_db()
    try:
        post = _validate_post_is_experiment(db, post_id)
        if not post:
            return _err("Post not found or is not a thought experiment", 404)

        data = _get_json()
        pledge_type = data.get('pledge_type', '')
        if pledge_type not in VALID_PLEDGE_TYPES:
            return _err(f"pledge_type must be one of: {', '.join(VALID_PLEDGE_TYPES)}")

        amount = data.get('amount')
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return _err("amount must be a positive number")
        if amount <= 0:
            return _err("amount must be a positive number")

        unit = data.get('unit') or UNIT_FOR_TYPE.get(pledge_type, pledge_type)

        # For gpu_hours: check user has a PeerNode and verify capacity
        node_id = None
        node_tier = None
        verified = False
        if pledge_type == 'gpu_hours':
            node = db.query(PeerNode).filter_by(
                node_operator_id=g.user_id,
            ).first()
            if not node:
                return _err(
                    "You must have an active PeerNode to pledge GPU hours. "
                    "Enable compute sharing first.", 422)
            node_id = node.node_id
            node_tier = node.tier or 'flat'

            # Basic capacity check: offered_gpu_hours_per_day from NodeComputeConfig
            try:
                from .models import NodeComputeConfig
                config = db.query(NodeComputeConfig).filter_by(
                    node_id=node.node_id).first()
                if config and config.offered_gpu_hours_per_day > 0:
                    # Check that pledge doesn't exceed daily capacity x 30 (monthly)
                    max_pledge = config.offered_gpu_hours_per_day * 30
                    if amount > max_pledge:
                        return _err(
                            f"Pledge exceeds your node capacity. "
                            f"Max ~{max_pledge:.0f} hours/month based on "
                            f"{config.offered_gpu_hours_per_day:.1f} hrs/day.", 422)
                    verified = True
            except Exception:
                pass  # NodeComputeConfig not available

        pledge = ComputePledge(
            user_id=g.user_id,
            post_id=str(post_id),
            pledge_type=pledge_type,
            amount=amount,
            unit=unit,
            remaining=amount,
            consumed=0.0,
            status='pledged',
            node_id=node_id,
            node_tier=node_tier,
            verified=verified,
            verified_at=datetime.utcnow() if verified else None,
            message=data.get('message'),
            anonymous=bool(data.get('anonymous', False)),
        )
        db.add(pledge)
        db.flush()

        # Award resonance for pledging
        try:
            ResonanceService.award_action(db, g.user_id, 'experiment_pledge')
        except Exception:
            pass  # Non-fatal

        db.commit()

        # Build summary for response
        summary = _build_pledge_summary(db, str(post_id))
        return _ok({
            'pledge': pledge.to_dict(),
            'experiment_summary': summary,
        }), 201

    except Exception as e:
        db.rollback()
        logger.exception("Error creating compute pledge")
        return _err("Failed to create pledge", 500)
    finally:
        db.close()


@compute_pledge_bp.route('/experiments/<post_id>/pledge/<int:pledge_id>',
                         methods=['DELETE'])
@require_auth
def withdraw_pledge(post_id, pledge_id):
    """Withdraw a pledge if nothing has been consumed yet."""
    from .models import get_db, ComputePledge

    db = get_db()
    try:
        pledge = db.query(ComputePledge).filter_by(
            id=pledge_id, post_id=str(post_id)).first()
        if not pledge:
            return _err("Pledge not found", 404)

        # Only owner can withdraw (or central)
        user_role = getattr(g.user, 'role', None) or 'flat'
        is_central = user_role == 'central' or g.user.is_admin
        if pledge.user_id != g.user_id and not is_central:
            return _err("Only the pledge owner can withdraw", 403)

        # Cannot withdraw if anything has been consumed
        if pledge.consumed > 0:
            return _err(
                f"Cannot withdraw: {pledge.consumed} {pledge.unit} already consumed. "
                f"Status: {pledge.status}", 409)

        if pledge.status in ('fulfilled', 'refunded'):
            return _err(f"Pledge already {pledge.status}", 409)

        pledge.status = 'refunded'
        pledge.remaining = 0.0
        db.commit()
        return _ok({'withdrawn': True, 'pledge': pledge.to_dict()})

    except Exception as e:
        db.rollback()
        logger.exception("Error withdrawing pledge")
        return _err("Failed to withdraw pledge", 500)
    finally:
        db.close()


@compute_pledge_bp.route('/experiments/<post_id>/pledges', methods=['GET'])
@require_auth
def list_pledges(post_id):
    """List all pledges for an experiment."""
    from .models import get_db, ComputePledge

    db = get_db()
    try:
        post = _validate_post_is_experiment(db, post_id)
        if not post:
            return _err("Post not found or is not a thought experiment", 404)

        limit = request.args.get('limit', 50, type=int)
        offset = request.args.get('offset', 0, type=int)
        pledge_type = request.args.get('pledge_type')

        q = db.query(ComputePledge).filter_by(post_id=str(post_id))
        if pledge_type and pledge_type in VALID_PLEDGE_TYPES:
            q = q.filter_by(pledge_type=pledge_type)

        total = q.count()
        pledges = q.order_by(ComputePledge.created_at.desc()).offset(
            offset).limit(limit).all()

        # Anonymize if the pledge owner opted in
        user_role = getattr(g.user, 'role', None) or 'flat'
        is_central = user_role == 'central' or g.user.is_admin

        result = []
        for p in pledges:
            d = p.to_dict(include_user=True)
            if p.anonymous and p.user_id != g.user_id and not is_central:
                d['user_id'] = None
                d['user'] = None
                d['message'] = None
            result.append(d)

        return _ok(result, meta={
            'total': total, 'limit': limit, 'offset': offset,
            'has_more': offset + limit < total,
        })

    except Exception as e:
        logger.exception("Error listing pledges")
        return _err("Failed to list pledges", 500)
    finally:
        db.close()


@compute_pledge_bp.route('/experiments/<post_id>/pledge-summary', methods=['GET'])
@require_auth
def pledge_summary(post_id):
    """Aggregate pledge summary for an experiment, grouped by type."""
    from .models import get_db, ComputePledge

    db = get_db()
    try:
        post = _validate_post_is_experiment(db, post_id)
        if not post:
            return _err("Post not found or is not a thought experiment", 404)

        summary = _build_pledge_summary(db, str(post_id))
        return _ok(summary)

    except Exception as e:
        logger.exception("Error building pledge summary")
        return _err("Failed to get summary", 500)
    finally:
        db.close()


def _build_pledge_summary(db, post_id):
    """Build aggregated pledge summary for a thought experiment."""
    from .models import ComputePledge

    by_type = {}
    for ptype in VALID_PLEDGE_TYPES:
        rows = db.query(
            sa_func.count(ComputePledge.id).label('pledger_count'),
            sa_func.coalesce(sa_func.sum(ComputePledge.amount), 0.0).label('total_pledged'),
            sa_func.coalesce(sa_func.sum(ComputePledge.consumed), 0.0).label('total_consumed'),
            sa_func.coalesce(sa_func.sum(ComputePledge.remaining), 0.0).label('total_remaining'),
        ).filter(
            ComputePledge.post_id == post_id,
            ComputePledge.pledge_type == ptype,
            ComputePledge.status.notin_(['refunded']),
        ).first()

        if rows and rows.pledger_count > 0:
            by_type[ptype] = {
                'pledgers': rows.pledger_count,
                'total_pledged': round(float(rows.total_pledged), 2),
                'total_consumed': round(float(rows.total_consumed), 2),
                'total_remaining': round(float(rows.total_remaining), 2),
                'unit': UNIT_FOR_TYPE.get(ptype, ptype),
            }

    # Top contributors (non-anonymous)
    top = db.query(ComputePledge).filter(
        ComputePledge.post_id == post_id,
        ComputePledge.anonymous == False,  # noqa: E712
        ComputePledge.status.notin_(['refunded']),
    ).order_by(ComputePledge.amount.desc()).limit(5).all()

    top_contributors = []
    for p in top:
        entry = {
            'pledge_type': p.pledge_type,
            'amount': p.amount,
            'unit': p.unit,
        }
        if p.user:
            entry['user'] = {
                'id': p.user.id,
                'username': p.user.username,
                'display_name': p.user.display_name,
                'avatar_url': p.user.avatar_url,
            }
        top_contributors.append(entry)

    return {
        'post_id': post_id,
        'by_type': by_type,
        'top_contributors': top_contributors,
    }


# ═══════════════════════════════════════════════════════════════
# CONTRIBUTOR-EXCLUSIVE INSIGHTS
# ═══════════════════════════════════════════════════════════════

@compute_pledge_bp.route('/experiments/<post_id>/insights', methods=['GET'])
@require_auth
def experiment_insights(post_id):
    """Return progress insights -- deep details for contributors, basic for others."""
    from .models import get_db, ComputePledge, AgentGoal

    db = get_db()
    try:
        post = _validate_post_is_experiment(db, post_id)
        if not post:
            return _err("Post not found or is not a thought experiment", 404)

        user_role = getattr(g.user, 'role', None) or 'flat'
        is_central = user_role == 'central' or g.user.is_admin

        # Check if user has pledged
        user_pledge = db.query(ComputePledge).filter_by(
            post_id=str(post_id), user_id=g.user_id,
        ).filter(ComputePledge.status.notin_(['refunded'])).first()

        has_access = bool(user_pledge) or is_central

        # Build basic progress (everyone gets this)
        summary = _build_pledge_summary(db, str(post_id))

        # Try to get AgentGoal progress
        goal = None
        try:
            goals = db.query(AgentGoal).filter(
                AgentGoal.goal_type == 'thought_experiment',
                AgentGoal.status.in_(['active', 'paused', 'completed']),
            ).all()
            for g_obj in goals:
                cfg = g_obj.config_json or {}
                if cfg.get('post_id') == str(post_id):
                    goal = g_obj
                    break
        except Exception:
            pass

        basic_progress = {
            'stage': 'unknown',
            'percentage': 0,
        }
        if goal:
            basic_progress['stage'] = goal.status
            if goal.spark_budget > 0:
                basic_progress['percentage'] = min(
                    100, round(goal.spark_spent / goal.spark_budget * 100, 1))

        if not has_access:
            # Non-contributors get only basic info
            return _ok({
                'access_level': 'basic',
                'progress': basic_progress,
                'pledge_summary': summary,
                'message': 'Pledge compute resources to unlock detailed insights.',
            })

        # Contributor / central: full details
        full_data = {
            'access_level': 'full',
            'progress': basic_progress,
            'pledge_summary': summary,
        }

        # Agent goal details
        if goal:
            full_data['goal'] = {
                'id': goal.id,
                'title': goal.title,
                'description': goal.description,
                'status': goal.status,
                'spark_budget': goal.spark_budget,
                'spark_spent': goal.spark_spent,
                'config': goal.config_json,
                'last_dispatched_at': (goal.last_dispatched_at.isoformat()
                                       if goal.last_dispatched_at else None),
                'created_at': goal.created_at.isoformat() if goal.created_at else None,
            }

        # Consumption log
        from .models import PledgeConsumption
        consumptions = db.query(PledgeConsumption).join(
            ComputePledge
        ).filter(
            ComputePledge.post_id == str(post_id),
        ).order_by(PledgeConsumption.consumed_at.desc()).limit(50).all()

        full_data['consumption_log'] = [c.to_dict() for c in consumptions]

        # Agent conversations (from MemoryGraph, best effort)
        if goal and goal.prompt_id:
            try:
                import os
                from integrations.channels.memory.memory_graph import MemoryGraph
                creator_id = goal.owner_id or (goal.created_by if goal.created_by else None)
                if creator_id:
                    session_key = f"{creator_id}_{goal.prompt_id}"
                    db_path = os.path.join(
                        os.path.expanduser("~"), "Documents", "Nunba", "data",
                        "memory_graph", session_key,
                    )
                    if os.path.exists(db_path):
                        graph = MemoryGraph(db_path=db_path, user_id=str(creator_id))
                        nodes = graph.get_session_memories(session_key, limit=20)
                        full_data['agent_conversations'] = nodes
            except Exception as e:
                logger.debug(f"Insights: agent conversations unavailable: {e}")

        return _ok(full_data)

    except Exception as e:
        logger.exception("Error getting experiment insights")
        return _err("Failed to get insights", 500)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# DETERMINISTIC CONSUMPTION (agent budget enforcement)
# ═══════════════════════════════════════════════════════════════

@compute_pledge_bp.route('/experiments/<post_id>/consume', methods=['POST'])
@require_auth
def consume_pledged(post_id):
    """Consume pledged resources -- THE deterministic enforcement point.

    Called by the agent execution system (internal).
    Body: {amount, pledge_type, task_description?, agent_goal_id?, override?}

    Consumes in FIFO order (oldest pledges first).
    Returns 402 if not enough pledged resources are available.
    Central users can set override=true for emergency overrides.
    """
    from .models import get_db, ComputePledge, PledgeConsumption

    db = get_db()
    try:
        post = _validate_post_is_experiment(db, post_id)
        if not post:
            return _err("Post not found or is not a thought experiment", 404)

        data = _get_json()
        pledge_type = data.get('pledge_type', '')
        if pledge_type not in VALID_PLEDGE_TYPES:
            return _err(f"pledge_type must be one of: {', '.join(VALID_PLEDGE_TYPES)}")

        try:
            requested = float(data.get('amount', 0))
        except (TypeError, ValueError):
            return _err("amount must be a positive number")
        if requested <= 0:
            return _err("amount must be a positive number")

        task_description = data.get('task_description', '')
        agent_goal_id = data.get('agent_goal_id')

        # Check override permission
        user_role = getattr(g.user, 'role', None) or 'flat'
        is_central = user_role == 'central' or g.user.is_admin
        override = bool(data.get('override', False)) and is_central

        # Find available pledges (FIFO: oldest first, with remaining > 0)
        available_pledges = db.query(ComputePledge).filter(
            ComputePledge.post_id == str(post_id),
            ComputePledge.pledge_type == pledge_type,
            ComputePledge.remaining > 0,
            ComputePledge.status.in_(['pledged', 'active']),
        ).order_by(ComputePledge.created_at.asc()).all()

        total_available = sum(p.remaining for p in available_pledges)

        if total_available < requested and not override:
            return jsonify({
                'success': False,
                'error': 'Insufficient pledged resources',
                'needed': requested,
                'available': round(total_available, 4),
                'shortfall': round(requested - total_available, 4),
                'pledge_type': pledge_type,
                'unit': UNIT_FOR_TYPE.get(pledge_type, pledge_type),
            }), 402

        # Consume in FIFO order
        still_needed = requested
        consumptions = []

        for pledge in available_pledges:
            if still_needed <= 0:
                break

            draw = min(still_needed, pledge.remaining)
            pledge.consumed += draw
            pledge.remaining = round(pledge.amount - pledge.consumed, 6)
            pledge.status = 'active'

            if pledge.remaining <= 0:
                pledge.remaining = 0.0
                pledge.status = 'fulfilled'

            consumption = PledgeConsumption(
                pledge_id=pledge.id,
                amount=draw,
                task_description=task_description,
                agent_goal_id=agent_goal_id,
            )
            db.add(consumption)
            consumptions.append({
                'pledge_id': pledge.id,
                'drawn': round(draw, 4),
                'pledge_remaining': round(pledge.remaining, 4),
                'pledge_status': pledge.status,
            })

            still_needed -= draw

        # If override and still_needed > 0, log the overspend
        if still_needed > 0 and override:
            logger.warning(
                f"CENTRAL OVERRIDE: consume overspend of {still_needed} "
                f"{pledge_type} on post {post_id} by user {g.user_id}")

        db.commit()

        return _ok({
            'consumed': round(requested - max(still_needed, 0), 4),
            'pledge_type': pledge_type,
            'unit': UNIT_FOR_TYPE.get(pledge_type, pledge_type),
            'consumptions': consumptions,
            'override_used': override and still_needed > 0,
        })

    except Exception as e:
        db.rollback()
        logger.exception("Error consuming pledged resources")
        return _err("Failed to consume resources", 500)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# MY PLEDGES / ADMIN VIEW
# ═══════════════════════════════════════════════════════════════

@compute_pledge_bp.route('/pledges/mine', methods=['GET'])
@require_auth
def my_pledges():
    """Get all pledges made by the current user across experiments."""
    from .models import get_db, ComputePledge

    db = get_db()
    try:
        limit = request.args.get('limit', 50, type=int)
        offset = request.args.get('offset', 0, type=int)
        status_filter = request.args.get('status')

        q = db.query(ComputePledge).filter_by(user_id=g.user_id)
        if status_filter:
            q = q.filter_by(status=status_filter)

        total = q.count()
        pledges = q.order_by(
            ComputePledge.created_at.desc()).offset(offset).limit(limit).all()

        return _ok(
            [p.to_dict() for p in pledges],
            meta={'total': total, 'limit': limit, 'offset': offset,
                  'has_more': offset + limit < total},
        )

    except Exception as e:
        logger.exception("Error getting my pledges")
        return _err("Failed to get pledges", 500)
    finally:
        db.close()


@compute_pledge_bp.route('/pledges/all', methods=['GET'])
@require_central
def all_pledges():
    """Central admin: view all pledges system-wide."""
    from .models import get_db, ComputePledge

    db = get_db()
    try:
        limit = request.args.get('limit', 100, type=int)
        offset = request.args.get('offset', 0, type=int)
        status_filter = request.args.get('status')
        pledge_type = request.args.get('pledge_type')
        post_id = request.args.get('post_id')

        q = db.query(ComputePledge)
        if status_filter:
            q = q.filter_by(status=status_filter)
        if pledge_type and pledge_type in VALID_PLEDGE_TYPES:
            q = q.filter_by(pledge_type=pledge_type)
        if post_id:
            q = q.filter_by(post_id=str(post_id))

        total = q.count()
        pledges = q.order_by(
            ComputePledge.created_at.desc()).offset(offset).limit(limit).all()

        return _ok(
            [p.to_dict(include_user=True) for p in pledges],
            meta={'total': total, 'limit': limit, 'offset': offset,
                  'has_more': offset + limit < total},
        )

    except Exception as e:
        logger.exception("Error getting all pledges")
        return _err("Failed to get pledges", 500)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# NODE CAPACITY VERIFICATION
# ═══════════════════════════════════════════════════════════════

@compute_pledge_bp.route('/pledges/<int:pledge_id>/verify', methods=['POST'])
@require_auth
def verify_pledge(pledge_id):
    """Verify node capacity for a gpu_hours pledge.

    Called by the pledge owner or a regional/central admin.
    """
    from .models import get_db, ComputePledge, PeerNode, NodeComputeConfig

    db = get_db()
    try:
        pledge = db.query(ComputePledge).filter_by(id=pledge_id).first()
        if not pledge:
            return _err("Pledge not found", 404)

        user_role = getattr(g.user, 'role', None) or 'flat'
        is_admin = user_role in ('central', 'regional') or g.user.is_admin or g.user.is_moderator
        if pledge.user_id != g.user_id and not is_admin:
            return _err("Access denied", 403)

        if pledge.pledge_type != 'gpu_hours':
            return _err("Only gpu_hours pledges require node verification", 400)

        if pledge.verified:
            return _ok({'already_verified': True, 'pledge': pledge.to_dict()})

        # Find the node
        if not pledge.node_id:
            return _err("No node_id associated with this pledge", 422)

        node = db.query(PeerNode).filter_by(node_id=pledge.node_id).first()
        if not node:
            return _err("PeerNode not found", 404)

        if node.status not in ('active',):
            return _err(f"Node status is '{node.status}', must be 'active'", 422)

        # Capacity check
        config = db.query(NodeComputeConfig).filter_by(
            node_id=pledge.node_id).first()
        capacity_ok = True
        capacity_details = {}

        if config:
            max_monthly = config.offered_gpu_hours_per_day * 30
            capacity_details = {
                'offered_daily': config.offered_gpu_hours_per_day,
                'max_monthly': round(max_monthly, 1),
                'pledged': pledge.amount,
                'within_capacity': pledge.amount <= max_monthly,
            }
            capacity_ok = pledge.amount <= max_monthly
        else:
            capacity_details = {'warning': 'No NodeComputeConfig found'}

        if node.compute_gpu_count:
            capacity_details['gpu_count'] = node.compute_gpu_count
        if node.compute_ram_gb:
            capacity_details['ram_gb'] = node.compute_ram_gb

        if capacity_ok:
            pledge.verified = True
            pledge.verified_at = datetime.utcnow()
            pledge.node_tier = node.tier or 'flat'
            db.commit()

        return _ok({
            'verified': capacity_ok,
            'capacity': capacity_details,
            'pledge': pledge.to_dict(),
        })

    except Exception as e:
        db.rollback()
        logger.exception("Error verifying pledge")
        return _err("Failed to verify pledge", 500)
    finally:
        db.close()
