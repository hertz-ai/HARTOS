"""
HevolveSocial - Agent Audit Trail & Compute Tracking API
Thin aggregation layer - no new tables. Reads from existing sources:
  - DashboardService (agents), MemoryGraph (conversations/lifecycle),
  - SmartLedger (task events), AgentGoal (daemon goals),
  - RegionalHostRegistry (compute nodes), APIUsageLog (compute usage).
"""
import logging
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, g, make_response

from .auth import require_auth, optional_auth
from .models import get_db, AgentGoal, APIUsageLog, User

logger = logging.getLogger('hevolve_social')

audit_bp = Blueprint('audit', __name__, url_prefix='/api/social/audit')


from .api_common import _ok, _err  # single-sourced envelope helpers (#97)


# ═══════════════════════════════════════════════════════════════
# AGENTS - Unified list (local + cloud + daemon)
# ═══════════════════════════════════════════════════════════════


def _agent_goal(db, agent_id):
    """The goal the audit page's agent id names: a goal id (find_goal), else
    the prompt id a goal runs under; None for a trained agent, daemon or
    expert."""
    from .dashboard_service import find_goal
    goal = find_goal(db, agent_id)
    if goal is None:
        goal = (db.query(AgentGoal)
                .filter(AgentGoal.prompt_id == str(agent_id)).first())
    return goal


def _agent_read_refused(agent_id):
    """None when the caller may read this agent's history, else the 403.

    Review of 275e8e361: timeline / conversations / thinking served any
    signed-in user.  They now ask dashboard_service.may_steer through
    goal_to_steer, like every goal route.  The id is a goal id, the prompt
    id a goal runs under, or a users row: a trained agent (user_type
    'agent') the dashboard lists by its user id.  A users row is judged as
    owned by that user, so may_steer's person rule (UserService.
    person_to_notify) maps an agent to its human owner -- review of
    d4146f843: the owner of a trained agent got 403 from another machine,
    and Hevolve web (auditApi.getTimeline) is always another machine.  An id
    none of those claim (daemon_*, expert_*) has no owner on record, so it
    is the machine's: this machine's callers read it, a remote non-admin
    gets the same 403 as for someone else's.
    """
    from types import SimpleNamespace
    from .dashboard_service import goal_to_steer, steering_caller
    db = g.db  # require_auth's session
    goal = _agent_goal(db, agent_id)
    if goal is None:
        account = db.query(User).filter(User.id == str(agent_id)).first()
        goal = SimpleNamespace(owner_id=str(account.id) if account else None,
                               created_by=None, user_id=None)
    _, refused = goal_to_steer(db, agent_id, 'read', steering_caller(),
                               str(g.user.id), goal=goal, audit=False)
    if refused is None:
        return None
    return jsonify({'success': False, 'data': refused}), 403


@audit_bp.route('/agents', methods=['GET'])
@require_auth
def list_agents():
    """Unified agent list from DashboardService + local prompts.

    Honors ``If-None-Match`` for the unfiltered case (and includes the
    requested ``type`` filter in the ETag).  See
    DashboardService.get_dashboard_version for hash inputs.  The React
    UI polls this endpoint every few seconds; without 304 the full
    pipeline runs each tick and starves waitress workers under
    ResourceGovernor's "active" throttle (cores [12,13,14,15] @ 25%).
    """
    agent_type = request.args.get('type')  # local|cloud|daemon|all
    db = get_db()
    try:
        from .dashboard_service import DashboardService

        # ETag short-circuit — include the type filter so /agents and
        # /agents?type=daemon don't share a cache slot.
        version = DashboardService.get_dashboard_version(db)
        etag = f'W/"audit-agents-{version}-{agent_type or "all"}"'
        if request.headers.get('If-None-Match') == etag:
            resp = make_response('', 304)
            resp.headers['ETag'] = etag
            resp.headers['Cache-Control'] = 'private, max-age=2'
            return resp

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

        body, status = _ok(agents)
        resp = make_response(body, status)
        resp.headers['ETag'] = etag
        resp.headers['Cache-Control'] = 'private, max-age=2'
        return resp
    except Exception as e:
        logger.error(f"Audit list_agents failed: {e}")
        return _ok([])
    finally:
        db.close()


@audit_bp.route('/agents/<agent_id>/timeline', methods=['GET'])
@require_auth
def get_agent_timeline(agent_id):
    """Chronological activity: conversations, tool calls, status transitions, thinking."""
    refused = _agent_read_refused(agent_id)
    if refused is not None:
        return refused
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


def _trail_rows(**filters):
    """Audit-trail lines matching ``filters`` (security.audit_trail.read),
    newest first; [] when the trail cannot be read, which is logged."""
    try:
        from security.audit_trail import get_trail
        return get_trail().read(**filters)
    except Exception:
        logger.warning('audit trail could not be read', exc_info=True)
        return []


def _trail_agent_ids(agent_id):
    """Every id this agent's trail lines carry.  The page names an agent by
    its dashboard id (a goal id, a trained agent's user id, daemon_*,
    expert_*), but a turn records the prompt id it ran under: /chat binds
    str(prompt_id), a conversation row its agent_id or prompt_id.  A goal
    runs under its saved prompt id, or the one dispatch derives from its id
    (dispatch.prompt_id_for_goal, the one formula).  A trained agent's users
    row keeps its prompt in agent_id as 'prompt_id_flow_id'.  daemon_* and
    expert_* cards name no prompt; they read their own id only."""
    ids = {str(agent_id)}
    try:
        goal = _agent_goal(g.db, agent_id)
        account = None if goal is not None else (
            g.db.query(User).filter(User.id == str(agent_id),
                                    User.user_type == 'agent').first())
    except Exception:
        logger.warning('audit trail: no goal or agent lookup for %s; reading its own '
                       'id only', agent_id, exc_info=True)
        return ids
    if goal is not None:
        if getattr(goal, 'prompt_id', None):
            ids.add(str(goal.prompt_id))
        from integrations.agent_engine.dispatch import prompt_id_for_goal
        ids.add(prompt_id_for_goal(str(goal.id)))
    elif account is not None and getattr(account, 'agent_id', None):
        ids.add(str(account.agent_id).split('_', 1)[0])
    return ids


def _trail_filters():
    """kinds / since / until / limit from the query string.  since and until
    are parsed here, once, to UTC by the one window rule
    (immutable_audit_log.utc_naive; a date-only until is that whole day),
    and both stores compare those times.  A bad one raises ValueError, which
    every trail route refuses with 400, whatever kinds were asked for."""
    from security.immutable_audit_log import utc_naive
    kinds = [k for k in (request.args.get('kinds') or '').split(',') if k]
    try:
        limit = int(request.args.get('limit', 200))
    except (TypeError, ValueError):
        limit = 200  # a non-number is the default, never a 500
    return {'kinds': kinds or None,
            'since': utc_naive(request.args.get('since') or None),
            'until': utc_naive(request.args.get('until') or None, day_end=True),
            'limit': max(1, min(limit, 5000))}


def _event_log_rows(agent_ids, kinds=None, since=None, until=None, limit=200):
    """The security event log's entries (security.immutable_audit_log) that
    concern one of ``agent_ids`` (its AGENT_FIELD rule), as trail-shaped
    lines, newest first; [] when ``kinds`` leaves them out or the log cannot
    be read (which is logged).  The log withholds only secret-NAMED keys, so
    each line is shown under the trail's own rule as well (_redact_extra:
    secret keys and secrets inside values), as a trail line is."""
    import json
    from security.audit_trail import EVENT_LOG_KIND, _iso, _redact_extra
    from security.immutable_audit_log import AGENT_FIELD, get_audit_log, utc_naive
    if kinds and EVENT_LOG_KIND not in kinds:
        return []
    try:
        entries = get_audit_log().get_trail(related_to=agent_ids, since=since,
                                            until=until, limit=limit)
    except Exception:
        logger.warning('security event log could not be read for the agent page',
                       exc_info=True)
        return []
    rows = []
    for entry in reversed(entries):  # get_trail answers oldest first
        actor = entry.get('actor_id')
        detail = entry.get('detail_json')
        if detail:
            try:
                detail = json.loads(detail)
            except ValueError:
                pass  # kept as the stored text
        try:
            ts = _iso(utc_naive(entry.get('created_at')))
        except (TypeError, ValueError):
            ts = str(entry.get('created_at') or '')
        safe = _redact_extra({'text': f"{entry.get('event_type')}: {entry.get('action')}",
                              'detail': detail})
        rows.append({
            'kind': EVENT_LOG_KIND, 'ts': ts,
            'text': safe.get('text') or 'withheld: it could not be checked for secrets',
            'actor': actor,
            'agent_id': str(entry.get(AGENT_FIELD.get(entry.get('event_type'), ''))),
            'hash': entry.get('entry_hash'), 'prev': entry.get('prev_hash'),
            'detail': safe.get('detail'), 'source': 'event_log'})
    return rows


def _agent_view_rows(agent_id):
    """The agent page's one trail (owner 2026-11-21, "One view, two
    stores"): its audit-trail lines and its security event log entries, with
    the query's kinds, window and limit, newest first.  Raises ValueError for
    a window that is not an ISO date."""
    filters = _trail_filters()
    ids = _trail_agent_ids(agent_id)
    rows = _trail_rows(agent_id=ids, **filters) + _event_log_rows(ids, **filters)
    rows.sort(key=lambda r: str(r.get('ts', '')), reverse=True)
    return rows[:filters['limit']]


@audit_bp.route('/agents/<agent_id>/trail', methods=['GET'])
@require_auth
def get_agent_trail(agent_id):
    """Every audit-trail line and security event for this agent, newest
    first, with each line's hash and the hash before it, and in meta.kinds
    the kinds the page may filter by (audit_trail.VIEW_KINDS).
    ?kinds=message,file&since=&until=&limit="""
    refused = _agent_read_refused(agent_id)
    if refused is not None:
        return refused
    try:
        rows = _agent_view_rows(agent_id)
    except ValueError as e:
        return _err(str(e), 400)
    from security.audit_trail import VIEW_KINDS
    return _ok(rows, meta={'kinds': list(VIEW_KINDS)})


@audit_bp.route('/agents/<agent_id>/trail/export', methods=['GET'])
@require_auth
def export_agent_trail(agent_id):
    """The same lines as a JSONL download, oldest first, for an auditor."""
    refused = _agent_read_refused(agent_id)
    if refused is not None:
        return refused
    import json
    try:
        rows = list(reversed(_agent_view_rows(agent_id)))
    except ValueError as e:
        return _err(str(e), 400)
    body = ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows)
    resp = make_response(body, 200)
    resp.headers['Content-Type'] = 'application/x-ndjson; charset=utf-8'
    safe = ''.join(c for c in str(agent_id) if c.isalnum() or c in '-_')[:64]
    resp.headers['Content-Disposition'] = f'attachment; filename="audit_{safe}.jsonl"'
    return resp


@audit_bp.route('/trail/mine', methods=['GET'])
@require_auth
def get_my_trail():
    """The caller's own lines, whichever agent they were for (a person may
    always see what was recorded about them)."""
    try:
        filters = _trail_filters()
    except ValueError as e:
        return _err(str(e), 400)
    return _ok(_trail_rows(user_id=str(g.user.id), **filters))


#: Seconds the page's verify may spend walking the security event log.  It
#: walks from the NEWEST entry back, so the entries the page shows are
#: checked first.  At 300,000 rows (SQLite) a whole walk measured 5.1-5.5 s,
#: more than a page open should wait, and 2 s reached the newest
#: 100,000-130,000; past this it stops and says how far back it got.
#: verify_chain() (no budget, oldest first) still walks everything.
VERIFY_BUDGET_S = 2.0


@audit_bp.route('/trail/verify', methods=['GET'])
@require_auth
def verify_trail():
    """Recompute every line's hash and check no day is missing, and walk the
    security event log newest first for at most VERIFY_BUDGET_S
    (ImmutableAuditLog.check_chain) as 'event_log', in
    immutable_audit_log.chain_report's shape; its ok is None when the log
    could not be read (not checked).  'ok' is True only when the trail is
    intact AND every event-log entry was checked and valid: never over
    entries nobody read.  Answers counts, segment names, entry ids and error
    TYPES only, never content: any signed-in account may call this, and an
    error's text can carry a host, a database user or a path (logged)."""
    try:
        from security.audit_trail import get_trail
        report = get_trail().verify()
    except Exception as e:
        logger.warning('audit trail verify failed', exc_info=True)
        return _err(f'the audit trail could not be checked ({type(e).__name__})', 500)
    from security.immutable_audit_log import chain_report, get_audit_log
    try:
        chain = get_audit_log().check_chain(budget_s=VERIFY_BUDGET_S, newest_first=True)
    except Exception as e:
        logger.warning('security event log verify failed', exc_info=True)
        chain = chain_report(newest_first=True, read_error=e)  # names the error type only
    report['event_log'] = chain
    report['ok'] = bool(report['ok'] and chain['ok'] is True
                        and chain['checked'] == chain['total'])
    return _ok(report)


@audit_bp.route('/agents/<agent_id>/conversations', methods=['GET'])
@require_auth
def get_agent_conversations(agent_id):
    """Agent conversation history from MemoryGraph."""
    refused = _agent_read_refused(agent_id)
    if refused is not None:
        return refused
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
    refused = _agent_read_refused(agent_id)
    if refused is not None:
        return refused
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
        routing['llm_backend'] = 'hart_intelligence'
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
