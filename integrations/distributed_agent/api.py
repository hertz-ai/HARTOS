"""
Distributed Agent API — generic endpoints for ANY agent type.

Not tied to coding agents. Any agent (coding, research, music, teaching)
can use these endpoints for distributed task coordination, host registration,
verification, and baselining.

Backend-agnostic: coordinator auto-selects Redis when available, falls
back to in-memory + JSON. No external dependency required for single-node.
Multi-node without Redis uses peer gossip (HTTP REST).

Routes: /api/distributed/*
Blueprint: distributed_agent_bp
"""

import os
import time
import logging
import threading
from collections import deque
from flask import Blueprint, request, jsonify, g

from integrations.social.auth import require_auth, require_admin

logger = logging.getLogger(__name__)

distributed_agent_bp = Blueprint('distributed_agent', __name__)

# Track which backend is active
_coordinator_backend_type = None

# ─── Delegation subscriber state ───────────────────────────────────
# When Redis is available, a daemon thread subscribes to
# `LedgerPubSub.CHANNEL_DELEGATION` so distributed nodes see real-time
# delegation broadcasts from create_recipe / reuse_recipe ledgers.
# Ring buffer capped at 100 so memory cannot grow unbounded.
# Gated entirely on Redis presence — no Redis → no subscriber, and the
# rest of /api/distributed/* keeps working via in-memory coordinator.
_delegation_sub_started: bool = False
_delegation_sub_lock = threading.Lock()
_recent_delegations: "deque[dict]" = deque(maxlen=100)

# Backoff between failed bootstrap attempts.  Kept separate from
# _delegation_sub_started because that flag is returned to clients as
# delegation_subscriber_active; setting it on failure would report a
# subscriber that isn't running.
#
# The probe costs ~15s when Redis is unreachable, and the before_request hook
# runs it on every /api/distributed/* request, ahead of @require_auth.
# Measured 2026-08-16: unauthenticated requests took 13.8-15.0s to return 401;
# control routes on the same app took ~20ms.
_DELEGATION_RETRY_COOLDOWN_S: float = 300.0
_delegation_sub_last_attempt: float = 0.0


def _ensure_delegation_subscriber(blocking: bool = True) -> bool:
    """Lazy-start the CHANNEL_DELEGATION subscriber.

    Idempotent (the `_delegation_sub_started` flag prevents double
    starts).  Gated on:

      * Redis reachable via `_get_redis_client()` + `.ping()`.
      * `agent_ledger.pubsub` importable.

    Fails silently at debug level.  Returns True iff the subscriber is
    active after the call; callers (endpoints, tests) can use the
    return value to surface status without raising.

    A failed attempt is remembered for `_DELEGATION_RETRY_COOLDOWN_S`, so a
    dead Redis costs one timeout per window instead of one per request.  It
    is a backoff, not a permanent disable: Redis can appear after boot, and
    the next window picks it up.

    `blocking=False` returns False instead of waiting when another thread
    holds the lock.  before_request uses that so concurrent requests don't
    queue behind the thread paying the probe cost.  The cooldown alone isn't
    enough -- the first attempt in each window would still block every
    concurrent caller.
    """
    global _delegation_sub_started, _delegation_sub_last_attempt
    if _delegation_sub_started:
        return True
    if not _delegation_sub_lock.acquire(blocking=blocking):
        return False
    try:
        # Re-check under the lock: another thread may have started it while
        # we waited.
        if _delegation_sub_started:
            return True
        now = time.monotonic()
        if (_delegation_sub_last_attempt
                and (now - _delegation_sub_last_attempt)
                < _DELEGATION_RETRY_COOLDOWN_S):
            return False
        # Stamp BEFORE the probe: the cost we are guarding against is the
        # probe itself, so an attempt that dies mid-timeout must still count.
        _delegation_sub_last_attempt = now
        try:
            redis_client = _get_redis_client()
        except Exception as e:
            logger.debug(f"[delegation_subscriber] redis client failed: {e}")
            return False
        if not redis_client:
            return False
        try:
            redis_client.ping()
        except Exception as e:
            logger.debug(f"[delegation_subscriber] redis ping failed: {e}")
            return False
        try:
            from agent_ledger.pubsub import LedgerPubSub
        except Exception as e:
            logger.debug(f"[delegation_subscriber] pubsub import failed: {e}")
            return False
        try:
            node_id = os.environ.get('HEVOLVE_NODE_ID', 'distributed_node')
            pubsub = LedgerPubSub(redis_client, agent_id=node_id)

            def _on_message(channel: str, data: dict) -> None:
                try:
                    _recent_delegations.append({
                        'channel': channel,
                        'data': data,
                        'received_at': time.time(),
                    })
                    logger.info(
                        f"[delegation_subscriber] {channel}: "
                        f"{str(data)[:160]}"
                    )
                except Exception as e:
                    logger.debug(
                        f"[delegation_subscriber] handler error: {e}"
                    )

            pubsub.subscribe(
                [LedgerPubSub.CHANNEL_DELEGATION], _on_message,
            )
            _delegation_sub_started = True
            logger.info(
                f"[delegation_subscriber] started as node={node_id}"
            )
            return True
        except Exception as e:
            logger.debug(f"[delegation_subscriber] start failed: {e}")
            return False
    finally:
        _delegation_sub_lock.release()


@distributed_agent_bp.before_request
def _bp_before_request_ensure_subscriber():
    """Subscriber bootstrap attempt on /api/distributed/* requests.

    Flask runs before_request ahead of view decorators, so this executes
    before @require_auth and must stay cheap for unauthenticated callers.
    Two things keep it cheap, and both are needed:

      * blocking=False -- don't wait on the bootstrap lock, so concurrent
        requests don't queue behind the thread paying a Redis timeout.
      * the failure cooldown in _ensure_delegation_subscriber -- a dead Redis
        is probed once per window, not once per request.

    Without them this hook cost 13.8-15.0s per unauthenticated request
    (measured 2026-08-16); control routes answered in ~20ms.
    Covered by tests/unit/test_delegation_subscriber_cooldown.py.
    """
    if not _delegation_sub_started:
        _ensure_delegation_subscriber(blocking=False)


# ─── Shared helpers ───

def _get_redis_client():
    """Get Redis client from environment or return None."""
    try:
        import redis
        host = os.environ.get('REDIS_HOST', 'localhost')
        port = int(os.environ.get('REDIS_PORT', 6379))
        return redis.Redis(host=host, port=port, decode_responses=True,
                           socket_connect_timeout=1, socket_timeout=1,
                           retry_on_timeout=False)
    except Exception:
        return None


def _get_coordinator():
    """Lazy-init DistributedTaskCoordinator (singleton).

    Backend priority: Redis → in-memory. No Redis required.
    Single-node hives work with in-memory backend.
    """
    global _coordinator_backend_type
    if not hasattr(_get_coordinator, '_instance'):
        from .coordinator_backends import create_coordinator
        coordinator, backend_type = create_coordinator()
        _get_coordinator._instance = coordinator
        _coordinator_backend_type = backend_type
    return _get_coordinator._instance


def get_coordinator_backend_type() -> str:
    """Return which backend the coordinator is using ('redis', 'inmemory', or None)."""
    return _coordinator_backend_type


def _get_host_registry(host_id: str = "query", host_url: str = ""):
    """Get host registry (Redis-backed or in-memory)."""
    redis_client = _get_redis_client()
    if redis_client:
        from .host_registry import RegionalHostRegistry
        return RegionalHostRegistry(redis_client, host_id=host_id, host_url=host_url)
    else:
        from .coordinator_backends import InMemoryHostRegistry
        # Singleton in-memory registry
        if not hasattr(_get_host_registry, '_instance'):
            _get_host_registry._instance = InMemoryHostRegistry(
                host_id=host_id, host_url=host_url
            )
        return _get_host_registry._instance


def _no_coordinator():
    return jsonify({
        'success': False,
        'error': 'Coordinator not available (neither Redis nor in-memory could initialize)',
    }), 503


# ─── Task announcement (gossip-based distribution) ───

@distributed_agent_bp.route('/api/distributed/tasks/announce', methods=['POST'])
@require_auth
def announce_tasks():
    """Receive task announcements from peer nodes (gossip protocol).
    Supports E2E encrypted envelopes."""
    coordinator = _get_coordinator()
    if not coordinator:
        return _no_coordinator()

    data = request.get_json() or {}
    # Decrypt E2E encrypted task announcement
    if data.get('encrypted') and data.get('envelope'):
        try:
            from security.channel_encryption import decrypt_json_from_peer
            decrypted = decrypt_json_from_peer(data['envelope'])
            if decrypted:
                data = decrypted
        except Exception:
            pass  # Decryption failed, try using data as-is
    goal_id = data.get('goal_id')
    objective = data.get('objective', '')
    tasks = data.get('tasks', [])
    context = data.get('context', {})

    sender_host = data.get('sender_host', '')

    if not goal_id or not tasks:
        return jsonify({'success': False, 'error': 'goal_id and tasks required'}), 400

    # Add tasks to local coordinator (idempotent — skip if goal already exists)
    try:
        existing = coordinator.get_goal_progress(goal_id)
        if 'error' not in existing:
            return jsonify({
                'success': True,
                'message': 'goal already known',
                'goal_id': goal_id,
                'local_goal_id': goal_id,
            })
    except Exception:
        pass

    try:
        # Preserve the sender's goal_id so multi-node coordination can
        # correlate tasks across peers.  submit_goal accepts an optional
        # goal_id when the caller already has one (gossip case).
        local_goal_id = coordinator.submit_goal(objective, tasks, context, goal_id=goal_id)
        return jsonify({
            'success': True,
            'goal_id': goal_id,
            'local_goal_id': local_goal_id,
            'sender_host': sender_host,
        })
    except TypeError:
        # Fallback: coordinator.submit_goal does not accept goal_id kwarg yet
        local_goal_id = coordinator.submit_goal(objective, tasks, context)
        return jsonify({
            'success': True,
            'goal_id': goal_id,
            'local_goal_id': local_goal_id,
            'sender_host': sender_host,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@distributed_agent_bp.route('/api/distributed/tasks/available', methods=['GET'])
@require_auth
def list_available_tasks():
    """List unclaimed tasks (for gossip pull by peers)."""
    coordinator = _get_coordinator()
    if not coordinator:
        return _no_coordinator()

    from agent_ledger.core import TaskStatus
    available = []
    for task_id in coordinator._ledger.task_order:
        task = coordinator._ledger.get_task(task_id)
        if task and task.status == TaskStatus.PENDING:
            available.append({
                'task_id': task.task_id,
                'description': task.description,
                'capabilities_required': task.context.get('capabilities_required', []),
                'context': task.context,
            })

    return jsonify({'success': True, 'tasks': available})


# ─── Hosts ───

@distributed_agent_bp.route('/api/distributed/hosts', methods=['GET'])
@require_auth
def list_hosts():
    """List all regional hosts contributing compute."""
    registry = _get_host_registry()
    hosts = registry.get_all_hosts()
    return jsonify({'success': True, 'hosts': hosts})


@distributed_agent_bp.route('/api/distributed/hosts/register', methods=['POST'])
@require_auth
def register_host():
    """Register this node as a compute contributor."""
    data = request.get_json() or {}
    host_id = data.get('host_id', os.environ.get('HEVOLVE_HOST_ID', 'unknown'))
    host_url = data.get('host_url', '')
    capabilities = data.get('capabilities', [])
    compute_budget = data.get('compute_budget', {})

    registry = _get_host_registry(host_id=host_id, host_url=host_url)
    success = registry.register_host(capabilities, compute_budget)
    return jsonify({'success': success, 'host_id': host_id})


# ─── Tasks ───

@distributed_agent_bp.route('/api/distributed/tasks/claim', methods=['POST'])
@require_auth
def claim_task():
    """Claim the next available task matching this agent's capabilities."""
    coordinator = _get_coordinator()
    if not coordinator:
        return _no_coordinator()

    data = request.get_json() or {}
    agent_id = data.get('agent_id', str(g.user.id))
    if agent_id != str(g.user.id) and not getattr(g.user, 'is_admin', False):
        return jsonify({'success': False, 'error': 'Cannot act as another agent'}), 403
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
        return _no_coordinator()

    data = request.get_json() or {}
    agent_id = data.get('agent_id', str(g.user.id))
    if agent_id != str(g.user.id) and not getattr(g.user, 'is_admin', False):
        return jsonify({'success': False, 'error': 'Cannot act as another agent'}), 403
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
        return _no_coordinator()

    data = request.get_json() or {}
    verifying_agent = data.get('agent_id', str(g.user.id))
    if verifying_agent != str(g.user.id) and not getattr(g.user, 'is_admin', False):
        return jsonify({'success': False, 'error': 'Cannot act as another agent'}), 403

    passed = coordinator.verify_result(task_id, verifying_agent)
    return jsonify({'success': True, 'task_id': task_id, 'verified': passed})


# ─── Goals ───

@distributed_agent_bp.route('/api/distributed/goals', methods=['POST'])
@require_auth
def submit_goal():
    """Submit a goal with decomposed tasks. Works for any agent type."""
    coordinator = _get_coordinator()
    if not coordinator:
        return _no_coordinator()

    data = request.get_json() or {}
    objective = data.get('objective')
    tasks = data.get('tasks', [])
    context = data.get('context', {})

    if not objective:
        return jsonify({'success': False, 'error': 'objective is required'}), 400
    if not tasks:
        return jsonify({'success': False, 'error': 'tasks list is required'}), 400

    goal_id = coordinator.submit_goal(objective, tasks, context)

    # Announce to peers via gossip if we have peers
    try:
        from .coordinator_backends import GossipTaskBridge
        bridge = GossipTaskBridge()
        bridge.announce_goal(goal_id, objective, tasks, context)
    except Exception:
        pass

    return jsonify({'success': True, 'goal_id': goal_id})


@distributed_agent_bp.route('/api/distributed/goals/<goal_id>/progress', methods=['GET'])
@require_auth
def goal_progress(goal_id):
    """Get distributed progress for a goal."""
    coordinator = _get_coordinator()
    if not coordinator:
        return _no_coordinator()

    progress = coordinator.get_goal_progress(goal_id)
    return jsonify({'success': True, **progress})


# ─── Baselines ───

@distributed_agent_bp.route('/api/distributed/baselines', methods=['POST'])
@require_auth
def create_baseline():
    """Create a progress baseline snapshot."""
    coordinator = _get_coordinator()
    if not coordinator:
        return _no_coordinator()

    data = request.get_json() or {}
    label = data.get('label', '')

    snapshot_id = coordinator.create_baseline(label)
    return jsonify({'success': True, 'snapshot_id': snapshot_id})


# ─── Status ───

@distributed_agent_bp.route('/api/distributed/status', methods=['GET'])
@require_auth
def coordinator_status():
    """Report coordinator status and backend type."""
    coordinator = _get_coordinator()
    # Attempt lazy subscriber start here too — /status is typically the
    # first endpoint admin UIs hit, so this doubles as a bootstrap.
    _ensure_delegation_subscriber()
    return jsonify({
        'success': True,
        'coordinator_active': coordinator is not None,
        'backend_type': _coordinator_backend_type,
        'delegation_subscriber_active': _delegation_sub_started,
    })


@distributed_agent_bp.route('/api/distributed/delegations/recent', methods=['GET'])
@require_auth
def recent_delegations():
    """Return the most recent delegation broadcasts received over the
    ``agent_ledger:delegation`` Redis channel.

    Response shape::

        {
          "success": true,
          "subscriber_active": bool,
          "delegations": [
            {"channel": str, "data": {...}, "received_at": float},
            ...
          ]
        }

    When Redis is unavailable the subscriber never starts and the list
    stays empty — this is the correct degradation for single-node /
    flat-tier deployments (Nunba desktop).  Admin UIs can poll this to
    visualize the distributed ledger traffic.
    """
    # Best-effort start on read so admins can see status even if no
    # prior distributed endpoint has been hit yet.
    _ensure_delegation_subscriber()
    return jsonify({
        'success': True,
        'subscriber_active': _delegation_sub_started,
        'delegations': list(_recent_delegations),
    })
