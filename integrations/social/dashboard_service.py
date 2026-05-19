"""
Agent Dashboard Service: Truth-Grounded Unified View

Queries actual database state, applies staleness detection, computes priority.
Shows what is REALLY happening, not what we wish was happening.

Consumed by Nunba (desktop), HART RN (mobile), and hevolve.ai (web).
"""
import hashlib
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger('hevolve_social')


class DashboardService:
    """Static service class for the truth-grounded unified agent dashboard."""

    # Priority tier weights (higher = shown first)
    TIER_EXECUTING = 1000
    TIER_ACTIVE = 500
    TIER_STALLED = 450
    TIER_FROZEN_DAEMON = 300
    TIER_DAEMON = 200
    TIER_IDLE = 50
    TIER_COMPLETED = 10

    @staticmethod
    def get_dashboard(db: Session) -> Dict:
        """Build complete agent dashboard from live database state.

        Returns dict with timestamp, node_health, agents (priority-sorted),
        and summary counts.
        """
        now = datetime.utcnow()
        agents: List[Dict] = []

        # 1. Agent goals (marketing, coding, analytics, etc.)
        agents.extend(DashboardService._get_agent_goals(db))

        # 2. Coding goals
        agents.extend(DashboardService._get_coding_goals(db))

        # 3. Background daemons (from watchdog)
        agents.extend(DashboardService._get_daemon_status())

        # 4. Trained agents (social users with user_type='agent')
        agents.extend(DashboardService._get_trained_agents(db))

        # 5. Expert agents (static registry)
        agents.extend(DashboardService._get_expert_agents())

        # Compute priority, sort descending
        for agent in agents:
            agent['priority'] = DashboardService._compute_priority(agent)
        agents.sort(key=lambda a: -a['priority'])

        # Node health from watchdog
        node_health = {'watchdog': 'unavailable', 'threads': {}}
        try:
            from security.node_watchdog import get_watchdog
            wd = get_watchdog()
            if wd:
                node_health = wd.get_health()
        except Exception:
            pass

        # Summary counts
        summary: Dict = {'total': len(agents), 'by_type': {}, 'by_status': {}}
        for a in agents:
            t = a.get('type', 'unknown')
            s = a.get('status', 'unknown')
            summary['by_type'][t] = summary['by_type'].get(t, 0) + 1
            summary['by_status'][s] = summary['by_status'].get(s, 0) + 1

        # World model (HevolveAI) status: SUPPLEMENTARY data, must not
        # block the dashboard.  `get_world_model_bridge()` first-call
        # bootstraps embodied_ai + vision + llama subsystems and can take
        # 60s+ when HARTOS Tier-1 init was skipped (e.g. transformers
        # crashed at boot).  Without this timeout the entire dashboard
        # endpoint hangs every poll → 5-second React UI poll piles up →
        # frontend silently times out → "No agents running" appears even
        # though 351 goals are in the AgentGoal table.
        #
        # Regression observed 2026-04-26: Tier-1 KeyError at boot left
        # world_model_bridge un-warmed; every dashboard poll then took
        # 57s, queueing the waitress task list and emptying the admin UI.
        world_model = {'healthy': False, 'error': 'unavailable'}
        try:
            import concurrent.futures as _cf
            def _collect_world_model():
                from integrations.agent_engine.world_model_bridge import (
                    get_world_model_bridge)
                bridge = get_world_model_bridge()
                return {
                    'health': bridge.check_health(),
                    'stats': bridge.get_learning_stats(),
                }
            # CRITICAL: do NOT use `with ThreadPoolExecutor as ex:` here.
            # The context-manager ``__exit__`` calls ``shutdown(wait=True)``,
            # which join()s the pool's worker thread.  When ``_fut.result``
            # times out, the worker is still inside the heavy
            # ``get_world_model_bridge()`` import and CAN'T finish, so the
            # ``with`` exit blocks forever — turning the 2s timeout into
            # an infinite hang and stacking every dashboard poll into a
            # permanently-stuck Hypercorn worker.  Live thread dump
            # 2026-04-28 22:08 showed 15 nunba_X workers ALL frozen at
            # ``ThreadPoolExecutor.__exit__ → shutdown → join``.  Fix:
            # manual try/finally + ``shutdown(wait=False,
            # cancel_futures=True)`` so the request returns even when
            # the worker is permanently wedged on the import lock.
            _ex = _cf.ThreadPoolExecutor(max_workers=1)
            try:
                _fut = _ex.submit(_collect_world_model)
                try:
                    _wm = _fut.result(timeout=2.0)
                    health = _wm['health']
                    stats = _wm['stats']
                    world_model = {
                        'healthy': health.get('healthy', False),
                        'learning_stats': stats.get('learning', {}),
                        'hivemind_stats': stats.get('hivemind', {}),
                        'bridge_stats': stats.get('bridge', {}),
                    }
                except _cf.TimeoutError:
                    # Bridge is cold or unreachable: surface that fact
                    # in the response without blocking the dashboard.
                    world_model = {'healthy': False, 'error': 'cold_or_unreachable'}
            finally:
                # Don't wait for the (potentially permanently-stuck)
                # worker thread.  It's a daemon — interpreter shutdown
                # will reap it.  Cancel any not-yet-started futures so
                # the pool doesn't pick up new work after we leave.
                _ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

        return {
            'timestamp': now.isoformat(),
            'node_health': node_health,
            'world_model': world_model,
            'agents': agents,
            'summary': summary,
        }

    @staticmethod
    def _get_agent_goals(db: Session) -> List[Dict]:
        """Query AgentGoal table. Apply truth-grounding: detect stalled goals."""
        try:
            from .models import AgentGoal
        except ImportError:
            return []

        poll_interval = int(os.environ.get('HEVOLVE_AGENT_POLL_INTERVAL', '30'))
        now = datetime.utcnow()

        goals = db.query(AgentGoal).filter(
            AgentGoal.status.in_(['active', 'paused', 'completed', 'failed'])
        ).all()

        result = []
        for goal in goals:
            gd = goal.to_dict()

            # Truth-grounding: detect stalled or idle.  status_reason is
            # additive (None when not derived) — frontend displays it
            # below the status chip when present, giving operators a
            # concrete "why" instead of an opaque label.  Strictly
            # additive field; existing consumers ignoring it are
            # unaffected.
            real_status = goal.status
            status_reason = None
            if goal.status == 'active' and goal.last_dispatched_at:
                age = (now - goal.last_dispatched_at).total_seconds()
                if age > poll_interval * 2:
                    real_status = 'stalled'
                    status_reason = (
                        f'No dispatch in {int(age)}s '
                        f'(expected within {poll_interval * 2}s of last tick)'
                    )
            elif goal.status == 'active' and not goal.last_dispatched_at:
                real_status = 'idle'
                status_reason = 'No dispatch recorded yet — awaiting first tick'
            elif goal.status == 'paused':
                # Truthful pause-reason: coding_daemon auto-pauses after
                # 5 consecutive dispatch failures and writes
                # ``cfg['pause_reason']`` (coding_daemon.py:227).  Read
                # it back so the UI doesn't lie with "user or system" —
                # if the user never paused, this surfaces the actual
                # auto-pause cause (e.g. "Auto-paused: 5 consecutive
                # dispatch failures").  Falls through to a generic
                # message when no recorded reason exists.
                _cfg = getattr(goal, 'config_json', None) or {}
                _pr = _cfg.get('pause_reason') if isinstance(_cfg, dict) else None
                status_reason = _pr or 'Goal status=paused (no reason recorded)'
            elif goal.status == 'failed':
                status_reason = 'Last execution returned failure status'

            result.append({
                'id': str(goal.id),
                'type': f'{goal.goal_type}_goal',
                'name': goal.title,
                'status': real_status,
                'status_reason': status_reason,
                'current_task': f'{goal.goal_type}: {goal.title[:60]}',
                'skills': [goal.goal_type],
                'last_active': gd.get('last_dispatched_at'),
                'metrics': {
                    'spark_spent': goal.spark_spent,
                    'spark_budget': goal.spark_budget,
                },
            })
        return result

    @staticmethod
    def _get_coding_goals(db: Session) -> List[Dict]:
        """Query CodingGoal table with task completion percentage."""
        try:
            from .models import CodingGoal
        except ImportError:
            return []

        goals = db.query(CodingGoal).filter(
            CodingGoal.status.in_(['active', 'paused', 'completed'])
        ).all()

        result = []
        for goal in goals:
            total = getattr(goal, 'total_tasks', 0) or 0
            completed = getattr(goal, 'completed_tasks', 0) or 0
            pct = round(completed / total * 100, 1) if total > 0 else 0

            result.append({
                'id': str(goal.id),
                'type': 'coding_goal',
                'name': goal.title,
                'status': goal.status,
                'current_task': f'Coding: {goal.title[:60]} ({pct}% done)',
                'skills': ['coding'],
                'last_active': goal.updated_at.isoformat() if getattr(
                    goal, 'updated_at', None) else None,
                'metrics': {
                    'total_tasks': total,
                    'completed_tasks': completed,
                    'completion_pct': pct,
                },
            })
        return result

    @staticmethod
    def _get_daemon_status() -> List[Dict]:
        """Get background daemon statuses from watchdog."""
        result = []
        try:
            from security.node_watchdog import get_watchdog
            wd = get_watchdog()
            if not wd:
                raise RuntimeError('no watchdog')

            health = wd.get_health()
            for name, info in health.get('threads', {}).items():
                result.append({
                    'id': f'daemon_{name}',
                    'type': 'daemon',
                    'name': name,
                    'status': info.get('status', 'unknown'),
                    'current_task': f'Background: {name}',
                    'skills': [name.replace('_', ' ')],
                    'last_active': info.get('last_heartbeat_iso'),
                    'metrics': {
                        'restart_count': info.get('restart_count', 0),
                        'heartbeat_age_s': info.get('last_heartbeat_age_s'),
                    },
                })
        except Exception:
            # Watchdog not available; enumerate known daemons
            for name in ('gossip', 'runtime_monitor', 'sync_engine',
                         'agent_daemon', 'coding_daemon'):
                result.append({
                    'id': f'daemon_{name}',
                    'type': 'daemon',
                    'name': name,
                    'status': 'unknown',
                    'current_task': f'Background: {name}',
                    'skills': [name.replace('_', ' ')],
                    'last_active': None,
                    'metrics': {},
                })
        return result

    @staticmethod
    def _get_trained_agents(db: Session) -> List[Dict]:
        """Query users with user_type='agent'."""
        try:
            from .models import User
        except ImportError:
            return []

        agents = db.query(User).filter(User.user_type == 'agent').all()

        result = []
        for agent in agents:
            last_active = getattr(agent, 'last_active_at', None)
            is_active = (last_active and
                         (datetime.utcnow() - last_active).total_seconds() < 3600)

            result.append({
                'id': str(agent.id),
                'type': 'trained_agent',
                'name': getattr(agent, 'display_name', None) or agent.username,
                'status': 'active' if is_active else 'idle',
                'current_task': None,
                'skills': [],  # loaded from skill badges if available
                'last_active': last_active.isoformat() if last_active else None,
                'metrics': {
                    'karma_score': getattr(agent, 'karma_score', 0),
                },
            })
        return result

    @staticmethod
    def _get_expert_agents() -> List[Dict]:
        """Load from ExpertAgentRegistry if available."""
        result = []
        try:
            from integrations.internal_comm.internal_agent_communication import (
                AgentSkillRegistry)
            registry = AgentSkillRegistry.get_instance()
            for agent_id, agent_info in registry._agents.items():
                result.append({
                    'id': f'expert_{agent_id}',
                    'type': 'expert_agent',
                    'name': agent_info.get('name', agent_id),
                    'status': 'available',
                    'current_task': None,
                    'skills': list(agent_info.get('skills', {}).keys()),
                    'last_active': None,
                    'metrics': {
                        'accuracy': agent_info.get('accuracy', 0),
                    },
                })
        except Exception:
            pass
        return result

    # ───────────────────────────────────────────────────────────────
    # ETag short-circuit: cheap hash for client If-None-Match polls.
    # See api_dashboard.get_agent_dashboard / api_audit.list_agents.
    # The React UI polls every 5s; without this, every poll re-runs
    # 5 SQL queries + watchdog locks + 170-row serialization even when
    # nothing changed.  Hash inputs are MAX(updated_at) on the two goal
    # tables + watchdog uptime bucket + trained-agent count: all cheap
    # reads that move whenever the dashboard would visibly change.
    # ───────────────────────────────────────────────────────────────

    @staticmethod
    def get_dashboard_version(db: Session) -> str:
        """Return a 16-char hash of dashboard inputs for ETag/304.

        Cheap by construction:
          - MAX(updated_at) on agent_goals + coding_goals (indexed)
          - watchdog uptime in 5s buckets + restart-log length
          - count of trained_agent users

        ANY caller depending on this for cache invalidation MUST also
        ensure their write site bumps `updated_at` (the SQLAlchemy
        `onupdate=func.now()` does this for AgentGoal/CodingGoal).
        For state changes that DON'T touch these tables (e.g. expert
        agent registry mutations, daemon thread restarts), the
        EventBus → SSE bridge in core/platform/bootstrap.py pushes
        `dashboard.invalidate` so the client re-fetches without
        waiting for the ETag to differ.
        """
        parts: List[str] = []
        try:
            from sqlalchemy import func
            from .models import AgentGoal, CodingGoal, User
            parts.append(str(db.query(func.max(AgentGoal.updated_at)).scalar()))
            parts.append(str(db.query(func.max(CodingGoal.updated_at)).scalar()))
            parts.append(str(
                db.query(func.count(User.id))
                  .filter(User.user_type == 'agent').scalar()))
        except Exception:
            # Schema not migrated, in-memory test DB without tables, etc.
            # Fall through with whatever we collected; version will
            # still be stable per request, just less precise.
            parts.append('schema-unavailable')

        # Daemon restart_log length: bumps on every restart, stable
        # otherwise.  We deliberately skip uptime: it ticks every
        # second and would invalidate the cache on every poll without
        # corresponding to a state the user sees change.  Always
        # append something so the parts-list cardinality is stable
        # whether or not the watchdog has been started yet.
        restart_count = ''
        try:
            from security.node_watchdog import get_watchdog
            wd = get_watchdog()
            if wd is not None:
                restart_count = str(len(wd.get_health().get('restart_log', [])))
        except Exception:
            pass
        parts.append(restart_count)

        return hashlib.sha256('|'.join(parts).encode()).hexdigest()[:16]

    @staticmethod
    def _compute_priority(agent_entry: Dict) -> int:
        """Compute priority score for dashboard ordering.

        Priority reflects what matters most RIGHT NOW:
        - Broken things first (frozen daemons)
        - Active work next (executing/active goals)
        - Background services
        - Idle/completed last
        """
        status = agent_entry.get('status', '')
        agent_type = agent_entry.get('type', '')

        # Tier 1: Currently executing
        if status in ('executing', 'dispatching'):
            base = DashboardService.TIER_EXECUTING
        # Tier 2: Active goals
        elif status == 'active' and 'goal' in agent_type:
            base = DashboardService.TIER_ACTIVE
        # Tier 3: Stalled goals (need attention)
        elif status == 'stalled':
            base = DashboardService.TIER_STALLED
        # Tier 4: Frozen daemons (need immediate attention)
        elif agent_type == 'daemon' and status == 'frozen':
            base = DashboardService.TIER_FROZEN_DAEMON
        # Tier 5: Healthy daemons
        elif agent_type == 'daemon' and status in ('healthy', 'unknown'):
            base = DashboardService.TIER_DAEMON
        # Tier 6: Idle/available
        elif status in ('idle', 'available'):
            base = DashboardService.TIER_IDLE
        # Tier 7: Completed/paused/failed
        else:
            base = DashboardService.TIER_COMPLETED

        # Sub-sort by remaining spark budget (for goals)
        metrics = agent_entry.get('metrics', {})
        budget = metrics.get('spark_budget') or 0
        spent = metrics.get('spark_spent') or 0
        remaining = max(0, budget - spent)
        base += min(remaining, 100)

        return base

    # ─── Agent Ops Console (Phase B) ─────────────────────────────────────
    # Drill-down endpoints reuse this class so /api/social/dashboard/...
    # stays the canonical surface — no new blueprint, no new service.

    @staticmethod
    def get_agent_snapshot(db: Session, agent_id: str) -> Optional[Dict]:
        """Return the drill-down payload for ONE agent's drawer.

        Combines:
          - The AgentGoal/CodingGoal row (status, status_reason same
            logic as the list endpoint — single source of truth).
          - The goal tree from the SmartLedger associated with this
            agent (walks ledger.tasks for parent/child links).
          - The most recent dispatcher decision (model + tier + ts)
            extracted from the existing ImmutableAuditLog 'goal_dispatched'
            events.  No new metrics table.

        Returns ``None`` when agent_id is not found.  Frontend renders
        a "agent not found" empty state in that case.

        Phase D will extend this with ETA + p95 from ledger history; for
        now ``eta`` is None and the drawer hides the row.
        """
        try:
            from .models import AgentGoal
        except ImportError:
            return None

        goal = db.query(AgentGoal).filter(AgentGoal.id == str(agent_id)).first()

        # Fallback to CodingGoal — the dashboard list at _get_coding_goals
        # mixes both kinds of agent IDs into the same /agents response.
        # Without this fallback, clicking any 'coding_goal' card in the
        # admin UI returned 404 because the snapshot only knew AgentGoal.
        # Adapter wraps the CodingGoal row in a goal-shaped namespace so
        # the rest of the snapshot pipeline below (status logic, tree
        # lookup, last_dispatch query, eta computation) needs no branch.
        if not goal:
            try:
                from .models import CodingGoal
                cg = db.query(CodingGoal).filter(
                    CodingGoal.id == str(agent_id)).first()
                if cg:
                    from types import SimpleNamespace
                    goal = SimpleNamespace(
                        id=cg.id,
                        owner_id=getattr(cg, 'owner_id', None) or getattr(cg, 'created_by', None),
                        created_by=getattr(cg, 'created_by', None),
                        goal_type='coding',
                        title=cg.title,
                        description=getattr(cg, 'description', None) or getattr(cg, 'title', ''),
                        status=cg.status,
                        priority=getattr(cg, 'priority', 0) or 0,
                        spark_budget=getattr(cg, 'spark_budget', 0) or 0,
                        spark_spent=getattr(cg, 'spark_spent', 0) or 0,
                        prompt_id=getattr(cg, 'prompt_id', None),
                        last_dispatched_at=getattr(cg, 'last_dispatched_at', None) or getattr(cg, 'updated_at', None),
                    )
            except Exception:
                logger.debug("CodingGoal fallback lookup failed", exc_info=True)

        # Third fallback — daemon-type agents (id prefix 'daemon_').
        # _get_daemon_status surfaces these from security.node_watchdog
        # with synthesized IDs like `daemon_agent_daemon`,
        # `daemon_distributed_worker`, `daemon_resource_governor_proactive`.
        # Without this branch, clicking any daemon card returned 404
        # because daemons are NOT in AgentGoal or CodingGoal — they're
        # tracked in NodeWatchdog's in-memory thread registry.  Wrap the
        # watchdog row in a goal-shaped namespace so the same downstream
        # pipeline (status logic, tree lookup, last_dispatch, eta)
        # works without branching.
        if not goal and str(agent_id).startswith('daemon_'):
            try:
                from security.node_watchdog import get_watchdog
                wd = get_watchdog()
                daemon_name = str(agent_id)[len('daemon_'):]
                health = wd.get_health() if wd else {}
                info = (health.get('threads', {}) or {}).get(daemon_name) or {}
                if info or daemon_name in (
                        'gossip', 'runtime_monitor', 'sync_engine',
                        'agent_daemon', 'coding_daemon',
                        'distributed_worker', 'resource_governor_proactive',
                        'resource_governor_monitor', 'tts_warmup'):
                    from types import SimpleNamespace
                    last_iso = info.get('last_heartbeat_iso')
                    last_dt = None
                    if last_iso:
                        try:
                            last_dt = datetime.fromisoformat(last_iso.replace('Z', '+00:00'))
                            if last_dt.tzinfo:
                                last_dt = last_dt.replace(tzinfo=None)
                        except Exception:
                            last_dt = None
                    daemon_status = info.get('status', 'active')
                    goal = SimpleNamespace(
                        id=str(agent_id),
                        owner_id=None,
                        created_by='system_daemon',
                        goal_type='daemon',
                        title=daemon_name,
                        description=(
                            f'Background daemon thread `{daemon_name}` '
                            f'tracked by NodeWatchdog.  '
                            f'restart_count={info.get("restart_count", 0)}, '
                            f'heartbeat_age_s={info.get("last_heartbeat_age_s", "n/a")}.'
                        ),
                        status=daemon_status if daemon_status != 'unknown' else 'active',
                        priority=0,
                        spark_budget=0,
                        spark_spent=0,
                        prompt_id=None,
                        last_dispatched_at=last_dt,
                        config_json={'pause_reason': info.get('error')} if info.get('error') else {},
                    )
            except Exception:
                logger.debug("Daemon fallback lookup failed", exc_info=True)

        if not goal:
            return None

        now = datetime.utcnow()

        # Status + status_reason — replicates the per-row logic from
        # _get_agent_goals so the drawer header matches the card header
        # the user just clicked.  Defaults to the raw goal.status when
        # nothing more specific applies.
        real_status = goal.status
        status_reason = None
        try:
            from .dashboard_service import _get_poll_interval_seconds  # not present; inline
        except Exception:
            pass
        poll_interval = 60
        if goal.status == 'active' and goal.last_dispatched_at:
            age = (now - goal.last_dispatched_at).total_seconds()
            if age > poll_interval * 2:
                real_status = 'stalled'
                status_reason = (
                    f'No dispatch in {int(age)}s '
                    f'(expected within {poll_interval * 2}s of last tick)'
                )
        elif goal.status == 'active' and not goal.last_dispatched_at:
            real_status = 'idle'
            status_reason = 'No dispatch recorded yet — awaiting first tick'
        elif goal.status == 'paused':
            # Same truthful pause-reason as _get_agent_goals — see
            # there for context.  Reading config_json.pause_reason
            # surfaces the actual auto-pause cause from
            # coding_daemon.py:227 instead of the misleading
            # "user or system" guess.
            _cfg = getattr(goal, 'config_json', None) or {}
            _pr = _cfg.get('pause_reason') if isinstance(_cfg, dict) else None
            status_reason = _pr or 'Goal status=paused (no reason recorded)'
        elif goal.status == 'failed':
            status_reason = 'Last execution returned failure status'

        # Goal tree from the ledger.  Iterate any ledger whose agent_id
        # matches and emit one node per task.  Parent/child structure
        # surfaces via task.parent_task_id / task.subtask_ids when
        # present; otherwise a flat ordered list.
        tree_nodes: List[Dict] = []
        try:
            from integrations.agent_engine.api import _iter_ledgers
            from agent_ledger import TaskStatus
            for a_id, session_id, ledger in _iter_ledgers(agent_filter=str(agent_id)):
                for task in ledger.tasks.values():
                    try:
                        tree_nodes.append({
                            'task_id': getattr(task, 'id', None) or getattr(task, 'task_id', None),
                            'title': getattr(task, 'title', None) or getattr(task, 'description', ''),
                            'status': (
                                task.status.value if hasattr(task.status, 'value')
                                else str(task.status)
                            ),
                            'parent_task_id': getattr(task, 'parent_task_id', None),
                            'created_at': _isoformat_safe(getattr(task, 'created_at', None)),
                            'updated_at': _isoformat_safe(
                                getattr(task, 'heartbeat_at', None)
                                or getattr(task, 'updated_at', None)
                            ),
                            'blocked_reason': getattr(task, 'blocked_reason', None),
                            'session_id': session_id,
                        })
                    except Exception:
                        logger.debug("skipped malformed task in %s/%s",
                                     a_id, session_id, exc_info=True)
        except ImportError:
            logger.debug("agent_ledger / api._iter_ledgers unavailable — tree omitted")

        # Most recent dispatcher decision — read from ImmutableAuditLog
        # 'goal_dispatched' events.  This is what dispatch.py:472 writes
        # per call.  No new metrics surface.
        last_dispatch: Optional[Dict] = None
        try:
            from security.immutable_audit_log import get_audit_log
            audit = get_audit_log()
            for entry in audit.query_recent(limit=200) if hasattr(audit, 'query_recent') else []:
                if entry.get('event_type') != 'goal_dispatched':
                    continue
                if entry.get('target_id') != str(agent_id):
                    continue
                last_dispatch = {
                    'timestamp': entry.get('timestamp'),
                    'model': (entry.get('detail') or {}).get('model_config'),
                    'tier': (entry.get('detail') or {}).get('model_tier'),
                    'request_id': (entry.get('detail') or {}).get('request_id'),
                }
                break
        except Exception:
            logger.debug("audit-log dispatch lookup failed for %s",
                         agent_id, exc_info=True)

        return {
            'agent': {
                'id': str(goal.id),
                'type': f'{goal.goal_type}_goal',
                'name': goal.title,
                'description': goal.description,
                'status': real_status,
                'status_reason': status_reason,
                'priority': goal.priority,
                'spark_budget': goal.spark_budget,
                'spark_spent': goal.spark_spent,
                'owner_id': goal.owner_id,
                'prompt_id': goal.prompt_id,
                'created_by': goal.created_by,
                'last_dispatched_at': _isoformat_safe(goal.last_dispatched_at),
            },
            'tree': tree_nodes,
            'last_dispatch': last_dispatch,
            'eta': _compute_eta_from_tree(tree_nodes, now=now),
            'snapshot_ts': now.isoformat() + 'Z',
        }

    @staticmethod
    def get_agent_chat_tail(agent_id: str, since_index: int = 0,
                            limit: int = 50) -> Dict:
        """Return the latest autogen GroupChat turns for an agent's drawer.

        Reads from the in-process ``_groupchat_registry`` populated by
        ``create_recipe.py`` at GroupChat instantiation sites.  The
        registry key is ``f'{owner_id}_{prompt_id}'`` — same convention
        the rest of the lifecycle uses.

        ``since_index`` is a cursor over the messages list; the drawer
        polls with the index of the last message it rendered to get
        only new turns.  This is cheaper + more accurate than time-
        based polling against a moving messages list.

        Returns ``{messages: [...], next_index: int, registered: bool}``.
        ``registered=False`` means no GroupChat is in the cache yet —
        either /chat hasn't run for this agent in this process or 8h
        TTL evicted it.  Drawer renders "no live conversation captured".
        """
        out: Dict = {'messages': [], 'next_index': since_index,
                     'registered': False}
        try:
            from .models import AgentGoal, get_db
        except ImportError:
            return out

        # Need owner_id + prompt_id to build the user_prompt key.  One
        # quick DB lookup; the route handler that calls us already has
        # an open session but this method is convenient as a standalone.
        db = get_db()
        try:
            goal = db.query(AgentGoal).filter(
                AgentGoal.id == str(agent_id)).first()
        finally:
            db.close()
        if not goal or not goal.prompt_id:
            return out

        user_prompt = f'{goal.owner_id or goal.created_by}_{goal.prompt_id}'

        try:
            from lifecycle_hooks import get_registered_groupchat
        except ImportError:
            return out
        gc = get_registered_groupchat(user_prompt)
        if gc is None:
            return out
        out['registered'] = True

        try:
            messages = list(gc.messages)
        except Exception:
            return out

        total = len(messages)
        start = max(0, int(since_index or 0))
        # Cap tail at `limit` even on cold-fetch (since_index=0) so the
        # drawer doesn't try to render thousands of turns at once.
        if start == 0 and total > limit:
            start = total - limit

        slice_ = messages[start:start + limit]
        out['messages'] = [_serialize_chat_message(m, idx + start)
                           for idx, m in enumerate(slice_)]
        out['next_index'] = start + len(slice_)
        return out


# ─── module-level helpers (used by snapshot + chat tail) ─────────────────

def _isoformat_safe(value) -> Optional[str]:
    """Best-effort ISO formatting that tolerates None / str / datetime."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat() + ('Z' if value.tzinfo is None else '')
    return str(value)


def _serialize_chat_message(msg, idx: int) -> Dict:
    """Reduce an autogen message dict / object to the drawer's render shape.

    Autogen messages are typically ``{'role': ..., 'content': ..., 'name': ...}``
    dicts but some plugins yield richer shapes (tool calls, function
    results).  We keep this defensive — anything we can't classify
    becomes a generic ``{'index, role, content, raw}`` entry.
    """
    if isinstance(msg, dict):
        role = msg.get('role') or 'assistant'
        speaker = msg.get('name') or role
        content = msg.get('content')
        if isinstance(content, list):
            # OpenAI vision-style multipart content — flatten to text.
            content = ' '.join(
                p.get('text', '') if isinstance(p, dict) else str(p)
                for p in content
            )
        return {
            'index': idx,
            'role': role,
            'speaker': speaker,
            'content': content if isinstance(content, str) else str(content),
            'tool_calls': msg.get('tool_calls'),
        }
    return {
        'index': idx,
        'role': 'unknown',
        'speaker': getattr(msg, 'name', 'unknown'),
        'content': str(msg),
        'tool_calls': None,
    }


# ─── Agent Ops Console (Phase C) ─────────────────────────────────────────


def get_a2a_graph(root_agent_id: str, depth: int = 2) -> Dict:
    """Read the A2A delegation graph centred on ``root_agent_id``.

    Walks the existing ``a2a_context.delegations`` singleton (the same
    one ``internal_agent_communication.delegate_task`` writes to at
    line 511).  No new graph store, no new schema.

    Returns ``{nodes: [{id, label, role}], edges: [{from, to,
    delegation_id, created_at, status}]}`` — drawer renders as a
    flat 2-level tree (root + delegates).  ``depth`` is reserved for
    Phase D when transitive delegation walks are needed.

    Cache-miss / module-unavailable returns an empty graph, never
    raises — the drawer renders "no delegations recorded".
    """
    nodes: Dict[str, Dict] = {}
    edges: List[Dict] = []
    try:
        # The singleton lives at module-level inside
        # internal_agent_communication; importing surfaces it.
        from integrations.internal_comm import internal_agent_communication as _iac
        a2a_ctx = getattr(_iac, 'a2a_context', None)
        delegations = getattr(a2a_ctx, 'delegations', None) if a2a_ctx else None
    except Exception:
        delegations = None
        a2a_ctx = None

    if not delegations:
        return {'nodes': [], 'edges': [],
                'root_id': str(root_agent_id), 'depth': depth}

    # Walk: for each delegation where from_agent or to_agent == root,
    # add both endpoints as nodes and the edge between them.  Single
    # hop for v1; transitive walking deferred to Phase D.
    def _add_node(agent_id: str, role: str):
        if not agent_id:
            return
        if agent_id not in nodes:
            nodes[agent_id] = {
                'id': agent_id,
                'label': agent_id,
                'role': role,  # 'root' | 'delegator' | 'delegate'
            }

    _add_node(str(root_agent_id), 'root')
    root_str = str(root_agent_id)

    try:
        items = delegations.items() if hasattr(delegations, 'items') else []
    except Exception:
        items = []

    for delegation_id, info in items:
        try:
            from_agent = str(info.get('from_agent', ''))
            to_agent = str(info.get('to_agent', ''))
        except Exception:
            continue
        if root_str not in (from_agent, to_agent):
            continue
        if from_agent == root_str:
            _add_node(to_agent, 'delegate')
        else:
            _add_node(from_agent, 'delegator')
        edges.append({
            'from': from_agent,
            'to': to_agent,
            'delegation_id': str(delegation_id),
            'status': info.get('status', 'unknown'),
            'created_at': info.get('created_at'),
        })

    return {
        'nodes': list(nodes.values()),
        'edges': edges,
        'root_id': root_str,
        'depth': depth,
    }


def steer_agent(db, agent_id: str, verb: str, actor_id: str = 'system',
                reason: Optional[str] = None) -> Dict:
    """Apply a steering verb to an AgentGoal: pause / resume / cancel.

    Uses the existing ``AgentGoal.status`` column (values: active |
    paused | completed | archived) — no new state, no new transition.
    Each call writes one ``ImmutableAuditLog`` event with type
    ``agent_steered`` so the operator's intervention is permanently
    attributable.

    Verb mapping:
      pause  -> status='paused'
      resume -> status='active'  (only legal from 'paused')
      cancel -> status='archived' (terminal; the goal is finished)

    Returns ``{ok: bool, new_status: str, error: str|None}``.
    Never raises; bad input returns ``{ok: False, error: ...}``.
    """
    out = {'ok': False, 'new_status': None, 'error': None}
    if verb not in ('pause', 'resume', 'cancel'):
        out['error'] = f'unknown verb {verb}'
        return out

    try:
        from .models import AgentGoal
    except ImportError:
        out['error'] = 'AgentGoal model unavailable'
        return out

    goal = db.query(AgentGoal).filter(AgentGoal.id == str(agent_id)).first()
    # CodingGoal fallback — same lookup pattern as get_agent_snapshot.
    # Without this, Pause/Resume/Cancel buttons on the drawer 404 for
    # any coding-type card (about half the dashboard).
    if not goal:
        try:
            from .models import CodingGoal
            goal = db.query(CodingGoal).filter(
                CodingGoal.id == str(agent_id)).first()
        except Exception:
            pass
    if not goal:
        out['error'] = 'agent not found'
        return out

    prev_status = goal.status
    target = {'pause': 'paused', 'resume': 'active', 'cancel': 'archived'}[verb]

    if verb == 'resume' and prev_status != 'paused':
        out['error'] = f'resume requires paused, got {prev_status}'
        return out
    if verb == 'cancel' and prev_status == 'archived':
        out['error'] = 'already archived'
        return out

    try:
        goal.status = target
        db.commit()
        out['ok'] = True
        out['new_status'] = target
    except Exception as e:
        db.rollback()
        out['error'] = f'commit failed: {e}'
        return out

    try:
        from security.immutable_audit_log import get_audit_log
        get_audit_log().log_event(
            event_type='agent_steered',
            actor_id=str(actor_id or 'system'),
            action=f'{verb} ({prev_status} -> {target})',
            detail={'agent_id': str(agent_id),
                    'prev_status': prev_status,
                    'new_status': target,
                    'reason': reason},
            target_id=str(agent_id),
        )
    except Exception:
        logger.exception('agent_steered audit log write failed for %s',
                         agent_id)
    return out


# ─── Agent Ops Console (Phase D) ─────────────────────────────────────────


def _compute_eta_from_tree(tree_nodes: List[Dict],
                           now: Optional[datetime] = None) -> Optional[Dict]:
    """Derive ETA stats from the same tree the snapshot already built.

    Reuses the ledger walk — no second I/O pass.  Looks at COMPLETED
    tasks for avg + p95 duration; for any IN_PROGRESS task, reports
    elapsed seconds so the drawer can flag "over avg".

    Returns ``None`` when there are < 2 completed samples (not enough
    signal).  ``{avg_seconds, p95_seconds, elapsed_seconds, samples}``
    otherwise.  ``elapsed_seconds`` is the oldest currently-running
    task — the one most likely to be the user's pain point.
    """
    if not tree_nodes:
        return None
    now = now or datetime.utcnow()
    completed_durations: List[float] = []
    elapsed_max: Optional[float] = None
    for node in tree_nodes:
        status = (node.get('status') or '').lower()
        created = _parse_iso(node.get('created_at'))
        if not created:
            continue
        if status in ('completed', 'done', 'success'):
            updated = _parse_iso(node.get('updated_at')) or now
            try:
                d = (updated - created).total_seconds()
                if d >= 0:
                    completed_durations.append(d)
            except Exception:
                continue
        elif status in ('in_progress', 'running'):
            try:
                e = (now - created).total_seconds()
                if e >= 0 and (elapsed_max is None or e > elapsed_max):
                    elapsed_max = e
            except Exception:
                continue

    if len(completed_durations) < 2 and elapsed_max is None:
        return None

    out: Dict = {'samples': len(completed_durations)}
    if completed_durations:
        completed_durations.sort()
        avg = sum(completed_durations) / len(completed_durations)
        # p95 by nearest-rank — fine for the small N we see per agent.
        idx95 = max(0, min(len(completed_durations) - 1,
                           int(round(0.95 * (len(completed_durations) - 1)))))
        out['avg_seconds'] = int(round(avg))
        out['p95_seconds'] = int(round(completed_durations[idx95]))
    if elapsed_max is not None:
        out['elapsed_seconds'] = int(round(elapsed_max))
    return out


def _parse_iso(value) -> Optional[datetime]:
    """Tolerant ISO parser for the snapshot tree's string timestamps."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    s = value.rstrip('Z')
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def inject_instruction(db, agent_id: str, instruction: str,
                       actor_id: str = 'admin-ui') -> Dict:
    """Append an operator instruction to the live GroupChat.

    Reuses the existing ``_groupchat_registry`` (populated by
    ``create_recipe.py``) and the ``user_prompt`` key convention from
    ``get_agent_chat_tail``.  The message is appended in autogen's
    user-message shape so the next select_speaker tick picks it up
    naturally — no separate "interrupt" path.

    Every inject also writes one ``agent_steered`` audit-log entry
    (verb=``inject``) so operator interventions stay attributable.

    Returns ``{ok, message_index, error}``.  ``ok=False`` when the
    GroupChat is not registered (process restarted, TTL expired, or
    /chat never ran for this agent in this process).
    """
    out = {'ok': False, 'message_index': None, 'error': None}
    if not (instruction or '').strip():
        out['error'] = 'empty instruction'
        return out

    try:
        from .models import AgentGoal
    except ImportError:
        out['error'] = 'AgentGoal unavailable'
        return out

    goal = db.query(AgentGoal).filter(AgentGoal.id == str(agent_id)).first()
    if not goal:
        # CodingGoal fallback (same pattern as get_agent_snapshot /
        # steer_agent).  Without this the inject CTA on the drawer's
        # Conversation tab silently 400s for any coding agent.
        try:
            from .models import CodingGoal
            cg = db.query(CodingGoal).filter(
                CodingGoal.id == str(agent_id)).first()
            if cg:
                from types import SimpleNamespace
                goal = SimpleNamespace(
                    prompt_id=getattr(cg, 'prompt_id', None),
                    owner_id=getattr(cg, 'owner_id', None) or getattr(cg, 'created_by', None),
                    created_by=getattr(cg, 'created_by', None),
                )
        except Exception:
            pass
    if not goal or not getattr(goal, 'prompt_id', None):
        out['error'] = 'agent not found or has no prompt_id'
        return out

    user_prompt = f'{goal.owner_id or goal.created_by}_{goal.prompt_id}'

    try:
        from lifecycle_hooks import get_registered_groupchat
    except ImportError:
        out['error'] = 'groupchat registry unavailable'
        return out
    gc = get_registered_groupchat(user_prompt)
    if gc is None:
        out['error'] = 'no live GroupChat registered for this agent'
        return out

    try:
        messages = gc.messages
    except Exception:
        out['error'] = 'GroupChat has no messages attribute'
        return out

    msg = {
        'role': 'user',
        'name': f'OperatorInjection({actor_id})',
        'content': instruction.strip(),
    }
    try:
        messages.append(msg)
        out['message_index'] = len(messages) - 1
        out['ok'] = True
    except Exception as e:
        out['error'] = f'append failed: {e}'
        return out

    try:
        from security.immutable_audit_log import get_audit_log
        get_audit_log().log_event(
            event_type='agent_steered',
            actor_id=str(actor_id or 'admin-ui'),
            action='inject',
            detail={'agent_id': str(agent_id),
                    'message_index': out['message_index'],
                    'instruction_preview': instruction[:200]},
            target_id=str(agent_id),
        )
    except Exception:
        logger.exception('inject audit-log write failed for %s', agent_id)

    return out
