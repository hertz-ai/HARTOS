"""
Unified Agent Goal Engine - API Blueprint

Unified endpoints for products + goals of any type.
10 endpoints total (5 product + 5 goal).
"""
import logging
from flask import Blueprint, request, jsonify, g

from integrations.social.auth import require_auth, require_admin

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

    data = request.get_json() or {}
    result = ProductManager.update_product(g.db, product_id, **data)
    return jsonify(result)


@agent_engine_bp.route('/api/marketing/products/<product_id>', methods=['DELETE'])
@require_auth
def delete_product(product_id):
    from .goal_manager import ProductManager
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

    data = request.get_json() or {}
    status = data.get('status')
    if not status:
        return jsonify({'success': False, 'error': 'status is required'}), 400
    return jsonify(GoalManager.update_goal_status(g.db, goal_id, status))


@agent_engine_bp.route('/api/goals/<goal_id>', methods=['DELETE'])
@require_auth
def delete_goal(goal_id):
    from .goal_manager import GoalManager
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
