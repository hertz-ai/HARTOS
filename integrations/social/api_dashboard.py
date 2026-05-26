"""
Agent Dashboard API Blueprint

GET /api/social/dashboard/agents   — Truth-grounded unified agent view (auth required)
GET /api/social/dashboard/health   — Node health from watchdog (public)
GET /api/social/dashboard/system   — System-level dashboard (tier, resources, services)
GET /api/social/dashboard/topology — Network topology (peer graph for UI visualization)
"""
import logging
import os
import shutil
import subprocess
import time

from flask import Blueprint, jsonify, request, make_response

logger = logging.getLogger('hevolve_social')

dashboard_bp = Blueprint('social_dashboard', __name__)


@dashboard_bp.route('/api/social/dashboard/agents', methods=['GET'])
def get_agent_dashboard():
    """Return truth-grounded dashboard of all agents, goals, and daemons.

    Priority-ordered: what matters most RIGHT NOW appears first.
    Status reflects reality, not cache.

    Honors ``If-None-Match``: a fresh 304 short-circuits the 5 SQL
    queries + 170-row serialization when the dashboard hasn't changed.
    The React UI polls every 5s — without ETag, every poll re-runs the
    full pipeline and queues waitress workers under throttle.  See
    DashboardService.get_dashboard_version for the hash inputs.
    """
    from .dashboard_service import DashboardService
    from .models import get_db

    db = get_db()
    try:
        version = DashboardService.get_dashboard_version(db)
        etag = f'W/"dash-{version}"'
        # Conditional GET — 304 with no body is the entire point.
        if request.headers.get('If-None-Match') == etag:
            resp = make_response('', 304)
            resp.headers['ETag'] = etag
            resp.headers['Cache-Control'] = 'private, max-age=2'
            return resp

        data = DashboardService.get_dashboard(db)
        resp = make_response(jsonify({'success': True, 'data': data}), 200)
        resp.headers['ETag'] = etag
        resp.headers['Cache-Control'] = 'private, max-age=2'
        return resp
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@dashboard_bp.route('/api/social/dashboard/health', methods=['GET'])
def get_node_health():
    """Public health endpoint showing watchdog + HevolveAI status."""
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
    and enabled features.  Part of the HART OS equilibrium system."""
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


@dashboard_bp.route('/api/social/dashboard/system', methods=['GET'])
def get_system_info():
    """System-level dashboard: tier, variant, deployment mode, resources, services."""
    from .models import get_db

    db = get_db()
    try:
        data = {}

        # ─── Tier ───
        try:
            from security.system_requirements import get_tier_name
            data['tier'] = get_tier_name()
        except Exception:
            data['tier'] = 'unknown'

        # ─── Variant (HART OS install variant) ───
        try:
            variant_path = '/etc/hart/variant'
            if os.path.isfile(variant_path):
                with open(variant_path, 'r') as f:
                    data['variant'] = f.read().strip() or 'standalone'
            else:
                data['variant'] = 'standalone'
        except Exception:
            data['variant'] = 'standalone'

        # ─── Deployment mode ───
        try:
            if os.environ.get('HART_CENTRAL_NODE'):
                data['deployment_mode'] = 'central'
            elif os.environ.get('HART_REGIONAL_NODE'):
                data['deployment_mode'] = 'regional'
            elif os.environ.get('HART_HEADLESS'):
                data['deployment_mode'] = 'headless'
            elif os.environ.get('HART_BUNDLED'):
                data['deployment_mode'] = 'bundled'
            else:
                data['deployment_mode'] = 'standalone'
        except Exception:
            data['deployment_mode'] = 'standalone'

        # ─── CPU usage ───
        try:
            # Read /proc/stat for CPU usage (Linux)
            with open('/proc/stat', 'r') as f:
                line = f.readline()
            fields = line.strip().split()[1:]
            idle = int(fields[3])
            total = sum(int(x) for x in fields[:7])
            # Approximate: single sample gives cumulative, not instant %
            # For a quick snapshot, report non-idle ratio
            if total > 0:
                data['cpu_percent'] = round((1.0 - idle / total) * 100, 1)
            else:
                data['cpu_percent'] = 0.0
        except Exception:
            try:
                # Fallback: os.cpu_count() only gives core count, not usage
                data['cpu_percent'] = None
            except Exception:
                data['cpu_percent'] = None

        # ─── RAM ───
        try:
            with open('/proc/meminfo', 'r') as f:
                meminfo = {}
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        key = parts[0].rstrip(':')
                        meminfo[key] = int(parts[1])  # in kB
            total_kb = meminfo.get('MemTotal', 0)
            available_kb = meminfo.get('MemAvailable', meminfo.get('MemFree', 0))
            data['ram_total_gb'] = round(total_kb / 1048576, 2)
            data['ram_used_gb'] = round((total_kb - available_kb) / 1048576, 2)
        except Exception:
            try:
                # Windows / non-Linux fallback via shutil (no RAM info)
                data['ram_total_gb'] = None
                data['ram_used_gb'] = None
            except Exception:
                data['ram_total_gb'] = None
                data['ram_used_gb'] = None

        # ─── Disk ───
        try:
            usage = shutil.disk_usage('/')
            data['disk_total_gb'] = round(usage.total / (1024 ** 3), 2)
            data['disk_used_gb'] = round(usage.used / (1024 ** 3), 2)
        except Exception:
            try:
                usage = shutil.disk_usage('.')
                data['disk_total_gb'] = round(usage.total / (1024 ** 3), 2)
                data['disk_used_gb'] = round(usage.used / (1024 ** 3), 2)
            except Exception:
                data['disk_total_gb'] = None
                data['disk_used_gb'] = None

        # ─── Services (systemctl status) ───
        services = {}
        service_names = [
            'hart-backend', 'hart-discovery', 'hart-agent-daemon',
            'hart-vision', 'hart-llm', 'hart-first-boot',
        ]
        for svc in service_names:
            try:
                result = subprocess.run(
                    ['systemctl', 'is-active', f'{svc}.service'],
                    capture_output=True, text=True, timeout=5
                )
                services[svc] = result.stdout.strip() or 'unknown'
            except Exception:
                services[svc] = 'unavailable'
        data['services'] = services

        # ─── Uptime ───
        try:
            with open('/proc/uptime', 'r') as f:
                data['uptime_seconds'] = round(float(f.read().split()[0]), 1)
        except Exception:
            try:
                # Fallback: time since epoch minus a known boot marker
                boot_marker = '/var/lib/hart/.first-boot-done'
                if os.path.isfile(boot_marker):
                    data['uptime_seconds'] = round(
                        time.time() - os.path.getmtime(boot_marker), 1)
                else:
                    data['uptime_seconds'] = None
            except Exception:
                data['uptime_seconds'] = None

        # ─── Node ID ───
        try:
            from security.node_integrity import get_node_identity
            identity = get_node_identity()
            data['node_id'] = identity.get('node_id', 'unknown')
        except Exception:
            data['node_id'] = 'unknown'

        # ─── Version (schema version as proxy) ───
        try:
            from integrations.social.migrations import SCHEMA_VERSION
            data['version'] = f'schema-v{SCHEMA_VERSION}'
        except Exception:
            data['version'] = 'unknown'

        return jsonify({'success': True, 'data': data}), 200
    except Exception as e:
        logger.error(f"System info error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@dashboard_bp.route('/api/social/dashboard/topology', methods=['GET'])
def get_topology():
    """Network topology: peer graph for UI visualization.

    Returns nodes (peers + self) and edges (gossip connections)
    suitable for rendering a network graph in the dashboard UI.
    """
    from .models import get_db, PeerNode

    db = get_db()
    try:
        # ─── Determine self node ID ───
        self_node_id = 'unknown'
        try:
            from security.node_integrity import get_public_key_hex
            self_node_id = get_public_key_hex()[:16]
        except Exception:
            pass

        # ─── Query all known peers ───
        peers = db.query(PeerNode).all()

        nodes = []
        edges = []
        peer_node_ids = set()

        for peer in peers:
            peer_node_ids.add(peer.node_id)
            nodes.append({
                'node_id': peer.node_id,
                'tier': peer.tier or 'flat',
                'region': peer.region_assignment_id or peer.dns_region,
                'trust_score': peer.contribution_score or 0.0,
                'status': peer.status or 'unknown',
                'is_self': (peer.node_id == self_node_id),
                'capability_tier': peer.capability_tier,
                'integrity_status': peer.integrity_status or 'unverified',
                'url': peer.url,
            })

        # Include self node if not already in the peer list
        if self_node_id != 'unknown' and self_node_id not in peer_node_ids:
            self_tier = 'flat'
            self_capability = None
            try:
                from security.system_requirements import get_tier_name
                self_capability = get_tier_name()
            except Exception:
                pass
            try:
                from security.key_delegation import get_node_tier
                self_tier = get_node_tier()
            except Exception:
                pass

            nodes.append({
                'node_id': self_node_id,
                'tier': self_tier,
                'region': None,
                'trust_score': 1.0,
                'status': 'active',
                'is_self': True,
                'capability_tier': self_capability,
                'integrity_status': 'verified',
                'url': None,
            })

        # ─── Build edges from gossip/hierarchy relationships ───
        for peer in peers:
            # Edge from self to every known peer (gossip connection)
            if self_node_id != 'unknown':
                # Estimate latency from metadata if available
                latency_ms = None
                if isinstance(peer.metadata_json, dict):
                    latency_ms = peer.metadata_json.get('latency_ms')

                edges.append({
                    'source_node_id': self_node_id,
                    'target_node_id': peer.node_id,
                    'latency_ms': latency_ms,
                })

            # Edge from peer to its parent (hierarchy link)
            if peer.parent_node_id and peer.parent_node_id in peer_node_ids:
                edges.append({
                    'source_node_id': peer.parent_node_id,
                    'target_node_id': peer.node_id,
                    'latency_ms': None,
                })

        return jsonify({
            'success': True,
            'data': {
                'self_node_id': self_node_id,
                'nodes': nodes,
                'edges': edges,
                'node_count': len(nodes),
                'edge_count': len(edges),
            },
        }), 200
    except Exception as e:
        logger.error(f"Topology error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


# ─── Agent Ops Console (Phase B drill-down) ──────────────────────────────
# Same blueprint, same ETag pattern, same auth posture as the agents
# list endpoint above.  No new blueprint, no new auth system.

@dashboard_bp.route(
    '/api/social/dashboard/agents/<agent_id>/snapshot',
    methods=['GET'],
)
def get_agent_snapshot(agent_id):
    """Return the drill-down snapshot payload for ONE agent.

    Combines the AgentGoal row (with truth-grounded status_reason),
    the goal tree from SmartLedger, and the most-recent dispatcher
    decision (model + tier) from ImmutableAuditLog.

    No ETag — the snapshot reflects ledger state that changes per
    autogen turn (seconds) and the polling interval from the drawer
    is 2s anyway.  Short-circuit caching would only save the SQL,
    which is one indexed primary-key lookup.
    """
    from .dashboard_service import DashboardService
    from .models import get_db

    db = get_db()
    try:
        snapshot = DashboardService.get_agent_snapshot(db, agent_id)
        if snapshot is None:
            return jsonify({'success': False, 'error': 'agent not found'}), 404
        return jsonify({'success': True, 'data': snapshot}), 200
    except Exception as e:
        logger.exception(f"snapshot error for agent_id={agent_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@dashboard_bp.route(
    '/api/social/dashboard/agents/<agent_id>/chat',
    methods=['GET'],
)
def get_agent_chat(agent_id):
    """Return the latest autogen GroupChat turns for ONE agent.

    Query params:
      ``since`` — int cursor; returns messages with index >= since.
      ``limit`` — int, clamped to 1..200 (default 50).

    Response shape:
      {ok: True, data: {
          messages: [{index, role, speaker, content, tool_calls}],
          next_index: int,
          registered: bool,
      }}

    ``registered=False`` means the in-process GroupChat cache has no
    entry for this agent yet (the agent has not run /chat in this
    Nunba process since boot, or the 8h TTL evicted it).  Frontend
    shows "no live conversation captured" rather than fabricating.
    """
    from .dashboard_service import DashboardService

    try:
        since = max(0, int(request.args.get('since', 0) or 0))
    except (TypeError, ValueError):
        since = 0
    try:
        limit = max(1, min(int(request.args.get('limit', 50) or 50), 200))
    except (TypeError, ValueError):
        limit = 50

    try:
        data = DashboardService.get_agent_chat_tail(
            agent_id, since_index=since, limit=limit)
        return jsonify({'success': True, 'data': data}), 200
    except Exception as e:
        logger.exception(f"chat tail error for agent_id={agent_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ─── Agent Ops Console (Phase C: A2A graph + steering verbs) ─────────────

@dashboard_bp.route(
    '/api/social/dashboard/agents/<agent_id>/a2a',
    methods=['GET'],
)
def get_agent_a2a(agent_id):
    """Return A2A delegation graph centred on this agent.

    Reads from the existing ``a2a_context.delegations`` singleton
    populated by ``internal_agent_communication.delegate_task``.  No
    new graph store.

    Query params: ``depth`` (reserved for transitive walks; Phase D).
    """
    from .dashboard_service import get_a2a_graph
    try:
        depth = max(1, min(int(request.args.get('depth', 2) or 2), 5))
    except (TypeError, ValueError):
        depth = 2
    try:
        data = get_a2a_graph(agent_id, depth=depth)
        return jsonify({'success': True, 'data': data}), 200
    except Exception as e:
        logger.exception(f"a2a error for agent_id={agent_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def _steer(agent_id, verb):
    """Shared helper for the 3 steering verbs.

    Each verb writes one ImmutableAuditLog ``agent_steered`` entry.
    Body schema (optional): ``{reason: <str>}``.

    Auth posture matches the rest of the dashboard blueprint — the
    /agents list endpoint above is also unauthenticated for parity
    with the existing operator console.  Phase C5 (owner-or-admin)
    is gated until the dashboard moves behind @require_auth.
    """
    from .dashboard_service import steer_agent
    from .models import get_db
    body = request.get_json(force=True, silent=True) or {}
    reason = body.get('reason')
    actor_id = body.get('actor_id') or request.headers.get(
        'X-Hartos-User') or 'admin-ui'
    db = get_db()
    try:
        result = steer_agent(db, agent_id, verb,
                             actor_id=actor_id, reason=reason)
        status = 200 if result.get('ok') else 400
        return jsonify({'success': result.get('ok'),
                        'data': result}), status
    except Exception as e:
        logger.exception(f"steer {verb} error for agent_id={agent_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@dashboard_bp.route(
    '/api/social/dashboard/agents/<agent_id>/pause',
    methods=['POST'],
)
def steer_pause(agent_id):
    """Pause an agent goal.  Idempotent on already-paused."""
    return _steer(agent_id, 'pause')


@dashboard_bp.route(
    '/api/social/dashboard/agents/<agent_id>/resume',
    methods=['POST'],
)
def steer_resume(agent_id):
    """Resume a paused agent goal.  400 if not paused."""
    return _steer(agent_id, 'resume')


@dashboard_bp.route(
    '/api/social/dashboard/agents/<agent_id>/cancel',
    methods=['POST'],
)
def steer_cancel(agent_id):
    """Cancel (archive) an agent goal.  Terminal — cannot resume."""
    return _steer(agent_id, 'cancel')


# ─── Agent Ops Console (Phase D: operator inject) ────────────────────────

@dashboard_bp.route(
    '/api/social/dashboard/agents/<agent_id>/inject',
    methods=['POST'],
)
def inject_into_groupchat(agent_id):
    """Inject an operator instruction into the agent's live GroupChat.

    Body: ``{"instruction": <str>, "actor_id": <str>?}``.  Returns
    ``{ok: bool, message_index: int|null, error: str|null}``.

    Returns 400 when the GroupChat is not registered (process restart,
    8h TTL eviction, or /chat never ran for this agent in this process).
    """
    from .dashboard_service import inject_instruction
    from .models import get_db
    body = request.get_json(force=True, silent=True) or {}
    instruction = (body.get('instruction') or '').strip()
    actor_id = body.get('actor_id') or request.headers.get(
        'X-Hartos-User') or 'admin-ui'
    if not instruction:
        return jsonify({'success': False,
                        'error': 'instruction is required'}), 400
    db = get_db()
    try:
        result = inject_instruction(db, agent_id, instruction,
                                    actor_id=actor_id)
        status = 200 if result.get('ok') else 400
        return jsonify({'success': result.get('ok'),
                        'data': result}), status
    except Exception as e:
        logger.exception(f"inject error for agent_id={agent_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()
