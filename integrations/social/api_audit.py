"""
HevolveSocial - Agent Audit Trail & Compute Tracking API
Thin aggregation layer - no new tables. Reads from existing sources:
  - DashboardService (agents), MemoryGraph (conversations/lifecycle),
  - SmartLedger (task events), AgentGoal (daemon goals),
  - RegionalHostRegistry (compute nodes), APIUsageLog (compute usage).
"""
import logging
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, g

from .auth import require_auth, optional_auth
from .models import get_db, AgentGoal, APIUsageLog, User

logger = logging.getLogger('hevolve_social')

audit_bp = Blueprint('audit', __name__, url_prefix='/api/social/audit')


def _ok(data=None, meta=None, status=200):
    r = {'success': True}
    if data is not None:
        r['data'] = data
    if meta is not None:
        r['meta'] = meta
    return jsonify(r), status


def _err(msg, status=400):
    return jsonify({'success': False, 'error': msg}), status


# ═══════════════════════════════════════════════════════════════
# AGENTS - Unified list (local + cloud + daemon)
# ═══════════════════════════════════════════════════════════════

@audit_bp.route('/agents', methods=['GET'])
@require_auth
def list_agents():
    """Unified agent list from DashboardService + local prompts."""
    agent_type = request.args.get('type')  # local|cloud|daemon|all
    db = get_db()
    try:
        from .dashboard_service import DashboardService
        dashboard = DashboardService.get_dashboard(db)
        agents = dashboard.get('agents', [])

        # Filter by type if requested
        if agent_type and agent_type != 'all':
            agents = [a for a in agents if a.get('type') == agent_type]

        # Enrich with user info if agent has a social user record
        for agent in agents:
            if agent.get('social_user_id'):
                user = db.query(User).filter_by(id=agent['social_user_id']).first()
                if user:
                    agent['display_name'] = user.display_name
                    agent['avatar_url'] = user.avatar_url

        return _ok(agents)
    except Exception as e:
        logger.error(f"Audit list_agents failed: {e}")
        return _ok([])
    finally:
        db.close()


@audit_bp.route('/agents/<agent_id>/timeline', methods=['GET'])
@require_auth
def get_agent_timeline(agent_id):
    """Chronological activity: conversations, tool calls, status transitions, thinking."""
    limit = min(int(request.args.get('limit', 50)), 200)
    events = []

    # 1. MemoryGraph lifecycle + conversation events
    try:
        from integrations.channels.memory.memory_graph import MemoryGraph
        # Try common session key patterns
        user_id = g.user.id
        for session_key in [f"{user_id}_{agent_id}", f"default_{agent_id}", agent_id]:
            try:
                mg = MemoryGraph(session_key)
                memories = mg.get_session_memories(session_key, limit=limit)
                for m in memories:
                    md = m.to_dict() if hasattr(m, 'to_dict') else {'content': str(m)}
                    events.append({
                        'type': md.get('memory_type', 'conversation'),
                        'timestamp': md.get('created_at'),
                        'content': md.get('content', ''),
                        'metadata': md.get('metadata', {}),
                        'source': 'memory_graph',
                    })
                if events:
                    break
            except Exception:
                continue
    except ImportError:
        pass

    # 2. Ledger task events
    try:
        from agent_ledger.core import SmartLedger
        ledger = SmartLedger(agent_id=agent_id)
        ledger_events = ledger.get_events(limit=limit)
        for ev in ledger_events:
            events.append({
                'type': 'task_event',
                'timestamp': ev.get('timestamp'),
                'content': ev.get('description', ev.get('event_type', '')),
                'metadata': ev,
                'source': 'ledger',
            })
    except Exception:
        pass

    # Sort chronologically
    events.sort(key=lambda e: e.get('timestamp') or '', reverse=True)
    return _ok(events[:limit])


@audit_bp.route('/agents/<agent_id>/conversations', methods=['GET'])
@require_auth
def get_agent_conversations(agent_id):
    """Agent conversation history from MemoryGraph."""
    conversations = []
    try:
        from integrations.channels.memory.memory_graph import MemoryGraph
        user_id = g.user.id
        for session_key in [f"{user_id}_{agent_id}", f"default_{agent_id}", agent_id]:
            try:
                mg = MemoryGraph(session_key)
                memories = mg.get_session_memories(session_key, limit=100)
                for m in memories:
                    md = m.to_dict() if hasattr(m, 'to_dict') else {'content': str(m)}
                    if md.get('memory_type') == 'conversation':
                        conversations.append({
                            'id': md.get('id'),
                            'role': md.get('metadata', {}).get('role', 'assistant'),
                            'content': md.get('content', ''),
                            'timestamp': md.get('created_at'),
                        })
                if conversations:
                    break
            except Exception:
                continue
    except ImportError:
        pass
    return _ok(conversations)


@audit_bp.route('/agents/<agent_id>/thinking', methods=['GET'])
@require_auth
def get_agent_thinking(agent_id):
    """Agent reasoning chain from MemoryGraph lifecycle events."""
    thinking = []
    try:
        from integrations.channels.memory.memory_graph import MemoryGraph
        user_id = g.user.id
        for session_key in [f"{user_id}_{agent_id}", f"default_{agent_id}", agent_id]:
            try:
                mg = MemoryGraph(session_key)
                memories = mg.get_session_memories(session_key, limit=100)
                for m in memories:
                    md = m.to_dict() if hasattr(m, 'to_dict') else {'content': str(m)}
                    if md.get('memory_type') in ('lifecycle', 'thinking', 'tool_call'):
                        thinking.append({
                            'type': md.get('memory_type'),
                            'content': md.get('content', ''),
                            'timestamp': md.get('created_at'),
                            'metadata': md.get('metadata', {}),
                        })
                if thinking:
                    break
            except Exception:
                continue
    except ImportError:
        pass
    return _ok(thinking)


# ═══════════════════════════════════════════════════════════════
# DAEMON - Background agent activity
# ═══════════════════════════════════════════════════════════════

@audit_bp.route('/daemon/activity', methods=['GET'])
@require_auth
def get_daemon_activity():
    """Recent daemon actions: goal dispatches, idle agent detection, remediation."""
    limit = min(int(request.args.get('limit', 30)), 100)
    activity = []

    # Recent goal dispatches
    db = get_db()
    try:
        cutoff = datetime.utcnow() - timedelta(hours=24)
        goals = (db.query(AgentGoal)
                 .filter(AgentGoal.created_at >= cutoff)
                 .order_by(AgentGoal.created_at.desc())
                 .limit(limit)
                 .all())
        for goal in goals:
            activity.append({
                'type': 'goal_dispatch',
                'timestamp': goal.created_at.isoformat() if goal.created_at else None,
                'goal_id': goal.id,
                'goal_type': goal.goal_type,
                'status': goal.status,
                'description': goal.description,
                'assigned_agent_id': goal.assigned_agent_id,
            })
    except Exception as e:
        logger.debug(f"Daemon activity query failed: {e}")
    finally:
        db.close()

    # Ledger daemon events
    try:
        from agent_ledger.core import SmartLedger
        ledger = SmartLedger(agent_id='daemon')
        events = ledger.get_events(limit=limit)
        for ev in events:
            activity.append({
                'type': ev.get('event_type', 'daemon_tick'),
                'timestamp': ev.get('timestamp'),
                'content': ev.get('description', ''),
                'metadata': ev,
            })
    except Exception:
        pass

    activity.sort(key=lambda a: a.get('timestamp') or '', reverse=True)
    return _ok(activity[:limit])


@audit_bp.route('/daemon/goals', methods=['GET'])
@require_auth
def get_daemon_goals():
    """Active and completed goals with progress."""
    status_filter = request.args.get('status', 'active')
    db = get_db()
    try:
        q = db.query(AgentGoal)
        if status_filter != 'all':
            q = q.filter(AgentGoal.status == status_filter)
        goals = q.order_by(AgentGoal.created_at.desc()).limit(50).all()

        result = []
        for goal in goals:
            gd = goal.to_dict()

            # Try to get progress from distributed coordinator
            try:
                from integrations.distributed_agent.task_coordinator import DistributedTaskCoordinator
                coord = DistributedTaskCoordinator()
                progress = coord.get_goal_progress(goal.id)
                gd['progress'] = progress
            except Exception:
                gd['progress'] = None

            result.append(gd)

        return _ok(result)
    except Exception as e:
        logger.error(f"Daemon goals query failed: {e}")
        return _ok([])
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# COMPUTE - Node tracking (regional/central only for full view)
# ═══════════════════════════════════════════════════════════════

@audit_bp.route('/compute/nodes', methods=['GET'])
@require_auth
def get_compute_nodes():
    """All compute nodes from RegionalHostRegistry."""
    nodes = []
    try:
        from integrations.distributed_agent.host_registry import RegionalHostRegistry
        registry = RegionalHostRegistry()
        nodes = registry.get_all_hosts()
    except Exception as e:
        logger.debug(f"Compute nodes query failed: {e}")

    # For flat users, only show their own node info
    if g.user.role not in ('central', 'regional'):
        nodes = [n for n in nodes if n.get('owner_id') == g.user.id]

    return _ok(nodes)


@audit_bp.route('/compute/usage', methods=['GET'])
@require_auth
def get_compute_usage():
    """Compute usage aggregated by user/agent from api_usage_log."""
    days = min(int(request.args.get('days', 7)), 30)
    cutoff = datetime.utcnow() - timedelta(days=days)

    db = get_db()
    try:
        from sqlalchemy import func as sqlfunc
        query = (db.query(
            APIUsageLog.api_key_id,
            sqlfunc.count(APIUsageLog.id).label('request_count'),
            sqlfunc.sum(APIUsageLog.tokens_in).label('total_tokens_in'),
            sqlfunc.sum(APIUsageLog.tokens_out).label('total_tokens_out'),
            sqlfunc.sum(APIUsageLog.compute_ms).label('total_compute_ms'),
            sqlfunc.sum(APIUsageLog.cost_credits).label('total_cost'),
        ).filter(APIUsageLog.created_at >= cutoff)
         .group_by(APIUsageLog.api_key_id)
         .all())

        usage = []
        for row in query:
            usage.append({
                'api_key_id': row.api_key_id,
                'request_count': row.request_count or 0,
                'total_tokens_in': row.total_tokens_in or 0,
                'total_tokens_out': row.total_tokens_out or 0,
                'total_compute_ms': row.total_compute_ms or 0,
                'total_cost': float(row.total_cost or 0),
            })

        # For flat users, filter to own usage
        if g.user.role not in ('central', 'regional'):
            # Get user's API keys
            from .models import CommercialAPIKey
            user_keys = {k.id for k in
                         db.query(CommercialAPIKey.id).filter_by(user_id=g.user.id).all()}
            usage = [u for u in usage if u['api_key_id'] in user_keys]

        return _ok(usage)
    except Exception as e:
        logger.error(f"Compute usage query failed: {e}")
        return _ok([])
    finally:
        db.close()


@audit_bp.route('/compute/routing', methods=['GET'])
@require_auth
def get_compute_routing():
    """Current routing info: node tier, LLM backend, routing reasons."""
    import os
    routing = {
        'node_tier': 'flat',
        'llm_backend': 'unknown',
        'routing_reasons': [],
    }

    # Detect node tier
    try:
        from security.key_delegation import get_node_tier
        routing['node_tier'] = get_node_tier()
    except Exception:
        pass

    # Detect LLM backend
    hevolve_url = os.environ.get('HEVOLVE_BACKEND_URL', '')
    if hevolve_url:
        routing['llm_backend'] = 'langchain_gpt_api'
        routing['llm_url'] = hevolve_url
    else:
        routing['llm_backend'] = 'direct_llama'

    # Check local LLM availability
    try:
        from core.http_pool import pooled_get
        resp = pooled_get('http://localhost:8080/health', timeout=2)
        routing['local_llm_available'] = resp.status_code == 200
    except Exception:
        routing['local_llm_available'] = False

    # Routing reasons
    if not routing['local_llm_available']:
        routing['routing_reasons'].append({
            'reason': 'compute_unavailable',
            'description': 'Local LLM not running - requests routed to regional/cloud',
        })

    # Check host registry for connected nodes
    try:
        from integrations.distributed_agent.host_registry import RegionalHostRegistry
        registry = RegionalHostRegistry()
        hosts = registry.get_all_hosts()
        routing['connected_nodes'] = len(hosts)
    except Exception:
        routing['connected_nodes'] = 0

    return _ok(routing)
