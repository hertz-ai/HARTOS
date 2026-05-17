"""
Unified Agent Goal Engine - API Blueprint

Unified endpoints for products + goals of any type.
10 endpoints total (5 product + 5 goal).
"""
import logging
from flask import Blueprint, request, jsonify, g

from integrations.social.auth import require_auth, require_admin
from core.auth_local import require_local_or_token

logger = logging.getLogger('hevolve_social')

agent_engine_bp = Blueprint('agent_engine', __name__)


# ─── Products ───

@agent_engine_bp.route('/api/marketing/products', methods=['POST'])
@require_auth
def create_product():
    from .goal_manager import ProductManager

    data = request.get_json() or {}
    if not data.get('name'):
        return jsonify({'success': False, 'error': 'name is required'}), 400

    result = ProductManager.create_product(
        g.db,
        name=data['name'],
        owner_id=str(g.user.id),
        description=data.get('description', ''),
        tagline=data.get('tagline', ''),
        product_url=data.get('product_url', ''),
        logo_url=data.get('logo_url', ''),
        category=data.get('category', 'general'),
        target_audience=data.get('target_audience', ''),
        unique_value_prop=data.get('unique_value_prop', ''),
        keywords=data.get('keywords', []),
        is_platform_product=data.get('is_platform_product', False),
    )
    return jsonify(result), 201 if result.get('success') else 400


@agent_engine_bp.route('/api/marketing/products', methods=['GET'])
@require_auth
def list_products():
    from .goal_manager import ProductManager

    owner_id = request.args.get('owner_id', str(g.user.id))
    status = request.args.get('status')
    products = ProductManager.list_products(g.db, owner_id=owner_id, status=status)
    return jsonify({'success': True, 'products': products})


@agent_engine_bp.route('/api/marketing/products/<product_id>', methods=['GET'])
@require_auth
def get_product(product_id):
    from .goal_manager import ProductManager
    return jsonify(ProductManager.get_product(g.db, product_id))


@agent_engine_bp.route('/api/marketing/products/<product_id>', methods=['PUT'])
@require_auth
def update_product(product_id):
    from .goal_manager import ProductManager
    from integrations.social.models import Product

    product = g.db.query(Product).filter_by(id=product_id).first()
    if not product:
        return jsonify({'success': False, 'error': 'Product not found'}), 404
    if str(product.owner_id) != str(g.user.id):
        return jsonify({'success': False, 'error': 'Not authorized'}), 403

    data = request.get_json() or {}
    result = ProductManager.update_product(g.db, product_id, **data)
    return jsonify(result)


@agent_engine_bp.route('/api/marketing/products/<product_id>', methods=['DELETE'])
@require_auth
def delete_product(product_id):
    from .goal_manager import ProductManager
    from integrations.social.models import Product

    product = g.db.query(Product).filter_by(id=product_id).first()
    if not product:
        return jsonify({'success': False, 'error': 'Product not found'}), 404
    if str(product.owner_id) != str(g.user.id):
        return jsonify({'success': False, 'error': 'Not authorized'}), 403

    return jsonify(ProductManager.delete_product(g.db, product_id))


# ─── Goals (unified — any goal_type) ───

@agent_engine_bp.route('/api/goals', methods=['POST'])
@require_auth
def create_goal():
    from .goal_manager import GoalManager, get_registered_types

    data = request.get_json() or {}
    goal_type = data.get('goal_type', '')
    if not goal_type:
        return jsonify({'success': False, 'error': 'goal_type is required'}), 400
    if goal_type not in get_registered_types():
        return jsonify({'success': False,
                        'error': f'Unknown goal_type: {goal_type}. '
                                 f'Available: {get_registered_types()}'}), 400
    if not data.get('title'):
        return jsonify({'success': False, 'error': 'title is required'}), 400

    result = GoalManager.create_goal(
        g.db,
        goal_type=goal_type,
        title=data['title'],
        description=data.get('description', ''),
        config=data.get('config', {}),
        product_id=data.get('product_id'),
        spark_budget=data.get('spark_budget', 200),
        created_by=str(g.user.id),
    )
    return jsonify(result), 201 if result.get('success') else 400


@agent_engine_bp.route('/api/goals', methods=['GET'])
@require_auth
def list_goals():
    from .goal_manager import GoalManager

    goal_type = request.args.get('goal_type')
    status = request.args.get('status')
    product_id = request.args.get('product_id')
    goals = GoalManager.list_goals(g.db, goal_type=goal_type,
                                   status=status, product_id=product_id)
    return jsonify({'success': True, 'goals': goals})


@agent_engine_bp.route('/api/goals/<goal_id>', methods=['GET'])
@require_auth
def get_goal(goal_id):
    from .goal_manager import GoalManager
    return jsonify(GoalManager.get_goal(g.db, goal_id))


@agent_engine_bp.route('/api/goals/<goal_id>/status', methods=['PATCH'])
@require_auth
def update_goal_status(goal_id):
    from .goal_manager import GoalManager
    from integrations.social.models import AgentGoal

    goal = g.db.query(AgentGoal).filter_by(id=goal_id).first()
    if not goal:
        return jsonify({'success': False, 'error': 'Goal not found'}), 404
    if goal.created_by and str(goal.created_by) != str(g.user.id):
        return jsonify({'success': False, 'error': 'Not authorized'}), 403

    data = request.get_json() or {}
    status = data.get('status')
    if not status:
        return jsonify({'success': False, 'error': 'status is required'}), 400
    return jsonify(GoalManager.update_goal_status(g.db, goal_id, status))


@agent_engine_bp.route('/api/goals/<goal_id>', methods=['DELETE'])
@require_auth
def delete_goal(goal_id):
    from .goal_manager import GoalManager
    from integrations.social.models import AgentGoal

    goal = g.db.query(AgentGoal).filter_by(id=goal_id).first()
    if not goal:
        return jsonify({'success': False, 'error': 'Goal not found'}), 404
    if goal.created_by and str(goal.created_by) != str(g.user.id):
        return jsonify({'success': False, 'error': 'Not authorized'}), 403

    return jsonify(GoalManager.update_goal_status(g.db, goal_id, 'archived'))


# ─── Speculative Execution ───

@agent_engine_bp.route('/api/agent-engine/speculation/<speculation_id>', methods=['GET'])
@require_auth
def get_speculation_status(speculation_id):
    """Get the status of a speculative dispatch (expert background task)."""
    from .speculative_dispatcher import get_speculative_dispatcher
    dispatcher = get_speculative_dispatcher()
    return jsonify(dispatcher.get_speculation_status(speculation_id))


@agent_engine_bp.route('/api/agent-engine/stats', methods=['GET'])
@require_auth
def get_engine_stats():
    """Get agent engine stats: active speculations, energy consumed, models."""
    from .speculative_dispatcher import get_speculative_dispatcher
    from .model_registry import model_registry
    dispatcher = get_speculative_dispatcher()
    return jsonify({
        'success': True,
        'speculation': dispatcher.get_stats(),
        'models': [m.to_dict() for m in model_registry.list_models()],
    })


@agent_engine_bp.route('/api/agent-engine/guardrails', methods=['GET'])
@require_auth
def get_guardrail_status():
    """Get guardrail system status."""
    from security.hive_guardrails import (
        HiveCircuitBreaker, CONSTITUTIONAL_RULES, COMPUTE_CAPS,
        WORLD_MODEL_BOUNDS,
    )
    return jsonify({
        'success': True,
        'circuit_breaker': HiveCircuitBreaker.get_status(),
        'constitutional_rules_count': len(CONSTITUTIONAL_RULES),
        'compute_caps': COMPUTE_CAPS,
        'world_model_bounds': WORLD_MODEL_BOUNDS,
    })


# ─── Agent Ledger ───
#
# SmartLedger is per-(agent_id, session_id): each agent goal creates
# its own ledger file at ``<get_agent_data_dir()>/ledger_<agent>_<session>.json``
# (see ``agent_ledger.core.SmartLedger.__init__``).  The admin Task
# Ledger view wants the UNION across all of them, so these handlers
# walk the storage dir and aggregate in-memory.
#
# Original T18 commit (4e4554e) used ``SmartLedger.get_instance()`` /
# ``ledger.list_tasks()`` / ``ledger.get_stats()`` — none of which
# exist on SmartLedger; the route family never worked.  Rewritten to
# use the actual public API: ``ledger.tasks``, ``ledger.get_task``,
# ``ledger.get_progress_summary``.
#
# JSON-backend only.  When a deployment switches to RedisBackend, the
# filesystem walk misses Redis-resident tasks; that path needs a
# separate Redis SCAN-based aggregator (TODO).
#
# Filename pattern is strict UUID_UUID to reject path-traversal
# attempts (``ledger_..%2F..%2Fetc.json``) and also to safely skip
# sibling files like ``benchmark_ledger.json`` that share the
# directory.

import re as _re

# Real on-disk session_id formats observed in production
# (see ~/Documents/Nunba/data/agent_data/):
#   ledger_<uuid>_<uuid>.json         — encounter / icebreaker agents
#   ledger_<uuid>_<numeric>.json      — autoresearch / langchain sessions
#   ledger_<uuid>_<arbitrary>.json    — possible future formats
# So agent_id is anchored to strict UUID (every visible filename has one),
# but session_id is any safe identifier — alphanumeric / hyphen /
# underscore.  Anchored ^...$ + restricted charset prevents both
# path traversal (no ``..%2F``, no ``/``) and matches against sibling
# files like ``benchmark_ledger.json`` that share the directory.
_LEDGER_UUID_RE = (r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}'
                   r'-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
_LEDGER_FILENAME = _re.compile(
    rf'^ledger_(?P<agent>{_LEDGER_UUID_RE})_(?P<session>[A-Za-z0-9_\-]+)\.json$'
)


def _iter_ledgers(agent_filter=None):
    """Yield ``(agent_id, session_id, SmartLedger)`` for every JSON
    ledger file in ``get_agent_data_dir()``.

    Honours the per-(agent, session) cache from
    ``agent_ledger.factory.get_or_create_ledger`` so repeat polls
    don't re-parse JSON each time.  ``prefer_redis=False`` because
    we are explicitly walking on-disk JSON files; a RedisBackend
    instance would not surface them via ``Path.glob``.
    """
    from agent_ledger.factory import get_or_create_ledger
    from core.platform_paths import get_agent_data_dir
    from pathlib import Path

    storage_dir = Path(get_agent_data_dir())
    if not storage_dir.is_dir():
        return
    for ledger_file in storage_dir.glob('ledger_*.json'):
        m = _LEDGER_FILENAME.match(ledger_file.name)
        if not m:
            # benchmark_ledger.json, malformed names, traversal attempts.
            continue
        agent_id, session_id = m.group('agent'), m.group('session')
        if agent_filter and agent_id != agent_filter:
            continue
        try:
            ledger = get_or_create_ledger(
                agent_id=agent_id,
                session_id=session_id,
                use_cache=True,
                storage_dir=str(storage_dir),
                prefer_redis=False,
            )
            yield agent_id, session_id, ledger
        except Exception as e:
            logger.warning(f"Skipped ledger {ledger_file.name}: {e}")
            continue


@agent_engine_bp.route('/api/agent-engine/ledger/tasks', methods=['GET'])
@require_local_or_token
def list_ledger_tasks():
    """List tasks aggregated across all per-session SmartLedgers.

    Query params:
      ``status``    — TaskStatus value (e.g. ``in_progress``, ``completed``)
      ``agent_id``  — filter to one agent's ledgers (UUID)
      ``limit``     — max tasks returned (1..1000, default 50)
    """
    try:
        from agent_ledger import TaskStatus
    except ImportError:
        return jsonify({'success': False,
                        'error': 'agent_ledger not installed'}), 501
    try:
        status_filter = request.args.get('status')
        agent_filter = request.args.get('agent_id')
        try:
            limit = max(1, min(int(request.args.get('limit', 50)), 1000))
        except (TypeError, ValueError):
            limit = 50

        status_enum = None
        if status_filter:
            try:
                status_enum = TaskStatus(status_filter)
            except ValueError:
                return jsonify({'success': False,
                                'error': f'Unknown status: {status_filter}'}), 400

        all_tasks = []
        for agent_id, session_id, ledger in _iter_ledgers(agent_filter):
            for task in ledger.tasks.values():
                if status_enum is not None and task.status != status_enum:
                    continue
                all_tasks.append({
                    **task.to_dict(),
                    'agent_id': agent_id,
                    'session_id': session_id,
                })
                if len(all_tasks) >= limit:
                    break
            if len(all_tasks) >= limit:
                break

        return jsonify({
            'success': True,
            'tasks': all_tasks,
            'total': len(all_tasks),
        })
    except Exception:
        logger.exception("list_ledger_tasks failed")
        return jsonify({'success': False,
                        'error': 'Internal server error'}), 500


@agent_engine_bp.route('/api/agent-engine/ledger/tasks/<task_id>', methods=['GET'])
@require_local_or_token
def get_ledger_task(task_id):
    """Get a single task by ID, searching across all per-session ledgers."""
    try:
        for agent_id, session_id, ledger in _iter_ledgers():
            task = ledger.get_task(task_id)
            if task is not None:
                return jsonify({
                    'success': True,
                    'task': {
                        **task.to_dict(),
                        'agent_id': agent_id,
                        'session_id': session_id,
                    },
                })
        return jsonify({'success': False, 'error': 'Task not found'}), 404
    except ImportError:
        return jsonify({'success': False,
                        'error': 'agent_ledger not installed'}), 501
    except Exception:
        logger.exception("get_ledger_task failed")
        return jsonify({'success': False,
                        'error': 'Internal server error'}), 500


@agent_engine_bp.route('/api/agent-engine/ledger/stats', methods=['GET'])
@require_local_or_token
def get_ledger_stats():
    """Aggregate ledger stats across all per-session SmartLedgers.

    Sums per-status counts and totals over every JSON ledger in
    ``get_agent_data_dir()``.  ``by_status`` keys are TaskStatus
    string values (``pending``, ``in_progress``, ...) — the dict
    returned by ``SmartLedger.get_task_state_summary`` uses enum
    keys, so we coerce to ``.value`` for JSON serialization.
    """
    try:
        total = 0
        sessions = 0
        by_status = {}
        for _agent_id, _session_id, ledger in _iter_ledgers():
            sessions += 1
            for task in ledger.tasks.values():
                total += 1
                key = task.status.value if hasattr(task.status, 'value') else str(task.status)
                by_status[key] = by_status.get(key, 0) + 1
        return jsonify({
            'success': True,
            'stats': {
                'total': total,
                'sessions': sessions,
                'by_status': by_status,
            },
        })
    except ImportError:
        return jsonify({'success': False,
                        'error': 'agent_ledger not installed'}), 501
    except Exception:
        logger.exception("get_ledger_stats failed")
        return jsonify({'success': False,
                        'error': 'Internal server error'}), 500


# ─── IP Protection ───

@agent_engine_bp.route('/api/ip/patents', methods=['GET'])
@require_auth
def list_patents():
    from .ip_service import IPService
    status = request.args.get('status')
    patents = IPService.list_patents(g.db, status=status)
    return jsonify({'success': True, 'patents': patents})


@agent_engine_bp.route('/api/ip/patents', methods=['POST'])
@require_auth
def create_patent():
    from .ip_service import IPService
    data = request.get_json() or {}
    if not data.get('title'):
        return jsonify({'success': False, 'error': 'title is required'}), 400
    result = IPService.create_patent(
        g.db,
        title=data['title'],
        claims=data.get('claims', []),
        abstract=data.get('abstract', ''),
        description=data.get('description', ''),
        filing_type=data.get('filing_type', 'provisional'),
        verification_metrics=data.get('verification_metrics'),
        evidence=data.get('evidence'),
        goal_id=data.get('goal_id'),
        created_by=str(g.user.id),
    )
    return jsonify({'success': True, 'patent': result}), 201


@agent_engine_bp.route('/api/ip/patents/<patent_id>', methods=['GET'])
@require_auth
def get_patent(patent_id):
    from .ip_service import IPService
    result = IPService.get_patent(g.db, patent_id)
    if not result:
        return jsonify({'success': False, 'error': 'Patent not found'}), 404
    return jsonify({'success': True, 'patent': result})


@agent_engine_bp.route('/api/ip/patents/<patent_id>/status', methods=['PATCH'])
@require_auth
def update_patent_status(patent_id):
    from .ip_service import IPService
    data = request.get_json() or {}
    status = data.get('status')
    if not status:
        return jsonify({'success': False, 'error': 'status is required'}), 400
    result = IPService.update_patent_status(
        g.db, patent_id, status,
        application_number=data.get('application_number'),
        patent_number=data.get('patent_number'),
    )
    if not result:
        return jsonify({'success': False, 'error': 'Patent not found'}), 404
    return jsonify({'success': True, 'patent': result})


@agent_engine_bp.route('/api/ip/infringements', methods=['GET'])
@require_auth
def list_infringements():
    from .ip_service import IPService
    patent_id = request.args.get('patent_id')
    status = request.args.get('status')
    infringements = IPService.list_infringements(g.db, patent_id=patent_id, status=status)
    return jsonify({'success': True, 'infringements': infringements})


@agent_engine_bp.route('/api/ip/infringements', methods=['POST'])
@require_auth
def create_infringement():
    from .ip_service import IPService
    data = request.get_json() or {}
    if not data.get('infringer_name'):
        return jsonify({'success': False, 'error': 'infringer_name is required'}), 400
    result = IPService.create_infringement(
        g.db,
        patent_id=data.get('patent_id', ''),
        infringer_name=data['infringer_name'],
        infringer_url=data.get('infringer_url', ''),
        evidence_summary=data.get('evidence_summary', ''),
        risk_level=data.get('risk_level', 'low'),
    )
    return jsonify({'success': True, 'infringement': result}), 201


@agent_engine_bp.route('/api/ip/loop-health', methods=['GET'])
@require_auth
def get_loop_health():
    """Self-improving loop dashboard — flywheel health + detected loopholes."""
    from .ip_service import IPService
    return jsonify({'success': True, 'data': IPService.get_loop_health()})


@agent_engine_bp.route('/api/ip/verify', methods=['GET'])
@require_auth
def verify_loop():
    """Verify exponential improvement — gates patent filing."""
    from .ip_service import IPService
    days = request.args.get('days', 30, type=int)
    result = IPService.verify_exponential_improvement(g.db, days=days)
    return jsonify({'success': True, 'data': result})


@agent_engine_bp.route('/api/ip/moat', methods=['GET'])
@require_auth
def get_moat_depth():
    """Technical irreproducibility — how far ahead of a code clone."""
    from .ip_service import IPService
    return jsonify({'success': True, 'data': IPService.measure_moat_depth()})


# ─── Defensive Publications ───

@agent_engine_bp.route('/api/ip/defensive-publications', methods=['GET'])
@require_auth
def list_defensive_publications():
    """List all defensive publications — timestamped prior art evidence."""
    from .ip_service import IPService
    pubs = IPService.list_defensive_publications(g.db)
    return jsonify({'success': True, 'publications': pubs})


@agent_engine_bp.route('/api/ip/defensive-publications', methods=['POST'])
@require_auth
def create_defensive_publication():
    """Create a new defensive publication — signed prior art proof."""
    from .ip_service import IPService
    data = request.get_json() or {}
    if not data.get('title') or not data.get('content'):
        return jsonify({'success': False, 'error': 'title and content required'}), 400
    result = IPService.create_defensive_publication(
        g.db,
        title=data['title'],
        content=data['content'],
        abstract=data.get('abstract', ''),
        git_commit=data.get('git_commit'),
        created_by=str(g.user.id),
    )
    return jsonify({'success': True, 'publication': result}), 201


@agent_engine_bp.route('/api/ip/provenance', methods=['GET'])
@require_auth
def get_provenance():
    """Full provenance chain — all publications, patents, moat, evidence."""
    from .ip_service import IPService
    return jsonify({'success': True, 'data': IPService.get_provenance_record(g.db)})


@agent_engine_bp.route('/api/ip/milestone', methods=['GET'])
@require_auth
def check_milestone():
    """Check intelligence milestone — auto-patent filing trigger status."""
    from .ip_service import IPService
    days = request.args.get('days', 14, type=int)
    result = IPService.check_intelligence_milestone(g.db, consecutive_days_required=days)
    return jsonify({'success': True, 'data': result})


# ─── World Model Health ───

@agent_engine_bp.route('/api/world-model/health', methods=['GET'])
def world_model_health():
    """World model bridge health check — no auth required for monitoring."""
    try:
        from .world_model_bridge import get_world_model_bridge
        bridge = get_world_model_bridge()
        return jsonify({
            'success': True,
            'health': bridge.check_health(),
            'stats': bridge.get_learning_stats(),
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'health': {'healthy': False, 'error': str(e)},
        })
