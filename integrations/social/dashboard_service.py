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

            # Truth-grounding: detect stalled or idle
            real_status = goal.status
            if goal.status == 'active' and goal.last_dispatched_at:
                age = (now - goal.last_dispatched_at).total_seconds()
                if age > poll_interval * 2:
                    real_status = 'stalled'
            elif goal.status == 'active' and not goal.last_dispatched_at:
                real_status = 'idle'

            result.append({
                'id': str(goal.id),
                'type': f'{goal.goal_type}_goal',
                'name': goal.title,
                'status': real_status,
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
