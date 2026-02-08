"""
Distributed Agent API — generic endpoints for ANY agent type.

Not tied to coding agents. Any agent (coding, research, music, teaching)
can use these endpoints for distributed task coordination, host registration,
verification, and baselining.

Routes: /api/distributed/*
Blueprint: distributed_agent_bp
"""

import os
import logging
from flask import Blueprint, request, jsonify, g

from integrations.social.auth import require_auth, require_admin

logger = logging.getLogger(__name__)

distributed_agent_bp = Blueprint('distributed_agent', __name__)


# ─── Shared helpers ───

def _get_redis_client():
    """Get Redis client from environment or return None."""
    try:
        import redis
        host = os.environ.get('REDIS_HOST', 'localhost')
        port = int(os.environ.get('REDIS_PORT', 6379))
        return redis.Redis(host=host, port=port, decode_responses=True)
    except Exception:
        return None


def _get_coordinator():
    """Lazy-init DistributedTaskCoordinator (singleton)."""
    if not hasattr(_get_coordinator, '_instance'):
        redis_client = _get_redis_client()
        if not redis_client:
            return None

        from agent_ledger import SmartLedger, RedisBackend
        from agent_ledger.distributed import DistributedTaskLock
        from agent_ledger.verification import TaskVerification, TaskBaseline

        host = os.environ.get('REDIS_HOST', 'localhost')
        port = int(os.environ.get('REDIS_PORT', 6379))

        backend = RedisBackend(host=host, port=port)
        # Reuse the backend's redis_client for all distributed components
        # so we don't open a third connection
        shared_redis = backend.redis_client

        ledger = SmartLedger(
            agent_id=os.environ.get('HEVOLVE_AGENT_ID', 'central'),
            session_id='distributed',
            backend=backend,
        )
        ledger.enable_pubsub(shared_redis)

        from .task_coordinator import DistributedTaskCoordinator
        _get_coordinator._instance = DistributedTaskCoordinator(
            ledger=ledger,
            task_lock=DistributedTaskLock(shared_redis),
            verifier=TaskVerification(shared_redis),
            baseline=TaskBaseline(backend),
        )
    return _get_coordinator._instance


def _no_redis():
    return jsonify({'success': False, 'error': 'Redis not available'}), 503


# ─── Hosts ───

@distributed_agent_bp.route('/api/distributed/hosts', methods=['GET'])
@require_auth
def list_hosts():
    """List all regional hosts contributing compute."""
    redis_client = _get_redis_client()
    if not redis_client:
        return _no_redis()

    from .host_registry import RegionalHostRegistry
    registry = RegionalHostRegistry(redis_client, host_id="query")
    hosts = registry.get_all_hosts()
    return jsonify({'success': True, 'hosts': hosts})


@distributed_agent_bp.route('/api/distributed/hosts/register', methods=['POST'])
@require_auth
def register_host():
    """Register this node as a compute contributor."""
    redis_client = _get_redis_client()
    if not redis_client:
        return _no_redis()

    data = request.get_json() or {}
    host_id = data.get('host_id', os.environ.get('HEVOLVE_HOST_ID', 'unknown'))
    host_url = data.get('host_url', '')
    capabilities = data.get('capabilities', [])
    compute_budget = data.get('compute_budget', {})

    from .host_registry import RegionalHostRegistry
    registry = RegionalHostRegistry(redis_client, host_id=host_id, host_url=host_url)
    success = registry.register_host(capabilities, compute_budget)
    return jsonify({'success': success, 'host_id': host_id})


# ─── Tasks ───

@distributed_agent_bp.route('/api/distributed/tasks/claim', methods=['POST'])
@require_auth
def claim_task():
    """Claim the next available task matching this agent's capabilities."""
    coordinator = _get_coordinator()
    if not coordinator:
        return _no_redis()

    data = request.get_json() or {}
    agent_id = data.get('agent_id', str(g.user.id))
    capabilities = data.get('capabilities', [])

    task = coordinator.claim_next_task(agent_id, capabilities)
    if task:
        return jsonify({
            'success': True,
            'task_id': task.task_id,
            'description': task.description,
            'context': task.context,
        })
    return jsonify({'success': True, 'task_id': None, 'message': 'No tasks available'})


@distributed_agent_bp.route('/api/distributed/tasks/<task_id>/submit', methods=['POST'])
@require_auth
def submit_task_result(task_id):
    """Submit a task result for verification."""
    coordinator = _get_coordinator()
    if not coordinator:
        return _no_redis()

    data = request.get_json() or {}
    agent_id = data.get('agent_id', str(g.user.id))
    result = data.get('result')

    if result is None:
        return jsonify({'success': False, 'error': 'result is required'}), 400

    info = coordinator.submit_result(task_id, agent_id, result)
    return jsonify({'success': True, **info})


@distributed_agent_bp.route('/api/distributed/tasks/<task_id>/verify', methods=['POST'])
@require_auth
def verify_task_result(task_id):
    """Verify another agent's task result."""
    coordinator = _get_coordinator()
    if not coordinator:
        return _no_redis()

    data = request.get_json() or {}
    verifying_agent = data.get('agent_id', str(g.user.id))

    passed = coordinator.verify_result(task_id, verifying_agent)
    return jsonify({'success': True, 'task_id': task_id, 'verified': passed})


# ─── Goals ───

@distributed_agent_bp.route('/api/distributed/goals', methods=['POST'])
@require_auth
def submit_goal():
    """Submit a goal with decomposed tasks. Works for any agent type."""
    coordinator = _get_coordinator()
    if not coordinator:
        return _no_redis()

    data = request.get_json() or {}
    objective = data.get('objective')
    tasks = data.get('tasks', [])
    context = data.get('context', {})

    if not objective:
        return jsonify({'success': False, 'error': 'objective is required'}), 400
    if not tasks:
        return jsonify({'success': False, 'error': 'tasks list is required'}), 400

    goal_id = coordinator.submit_goal(objective, tasks, context)
    return jsonify({'success': True, 'goal_id': goal_id})


@distributed_agent_bp.route('/api/distributed/goals/<goal_id>/progress', methods=['GET'])
@require_auth
def goal_progress(goal_id):
    """Get distributed progress for a goal."""
    coordinator = _get_coordinator()
    if not coordinator:
        return _no_redis()

    progress = coordinator.get_goal_progress(goal_id)
    return jsonify({'success': True, **progress})


# ─── Baselines ───

@distributed_agent_bp.route('/api/distributed/baselines', methods=['POST'])
@require_auth
def create_baseline():
    """Create a progress baseline snapshot."""
    coordinator = _get_coordinator()
    if not coordinator:
        return _no_redis()

    data = request.get_json() or {}
    label = data.get('label', '')

    snapshot_id = coordinator.create_baseline(label)
    return jsonify({'success': True, 'snapshot_id': snapshot_id})
