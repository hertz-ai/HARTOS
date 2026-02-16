"""
Agent Dashboard API Blueprint

GET /api/social/dashboard/agents  — Truth-grounded unified agent view (auth required)
GET /api/social/dashboard/health  — Node health from watchdog (public)
"""
import logging

from flask import Blueprint, jsonify

logger = logging.getLogger('hevolve_social')

dashboard_bp = Blueprint('social_dashboard', __name__)


@dashboard_bp.route('/api/social/dashboard/agents', methods=['GET'])
def get_agent_dashboard():
    """Return truth-grounded dashboard of all agents, goals, and daemons.

    Priority-ordered: what matters most RIGHT NOW appears first.
    Status reflects reality, not cache.
    """
    from .dashboard_service import DashboardService
    from .models import get_db

    db = get_db()
    try:
        data = DashboardService.get_dashboard(db)
        return jsonify({'success': True, 'data': data}), 200
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@dashboard_bp.route('/api/social/dashboard/health', methods=['GET'])
def get_node_health():
    """Public health endpoint showing watchdog + crawl4ai status."""
    data = {'watchdog': 'not_started', 'threads': {}, 'world_model': {}}
    try:
        from security.node_watchdog import get_watchdog
        wd = get_watchdog()
        if wd:
            data.update(wd.get_health())
    except Exception:
        pass

    try:
        from integrations.agent_engine.world_model_bridge import (
            get_world_model_bridge)
        bridge = get_world_model_bridge()
        data['world_model'] = bridge.check_health()
    except Exception:
        data['world_model'] = {'healthy': False}

    return jsonify({'success': True, 'data': data}), 200


@dashboard_bp.route('/api/social/node/capabilities', methods=['GET'])
def get_node_capabilities():
    """Public endpoint: this node's hardware profile, contribution tier,
    and enabled features.  Part of the Hyve OS equilibrium system."""
    try:
        from security.system_requirements import get_capabilities
        caps = get_capabilities()
        if caps is None:
            return jsonify({
                'success': False,
                'error': 'System requirements not yet checked',
            }), 503
        return jsonify({'success': True, 'data': caps.to_dict()}), 200
    except Exception as e:
        logger.error(f"Capabilities endpoint error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
