"""
HevolveSocial - Coding Agent Daemon

Background thread that finds active goals, detects idle agents,
and dispatches work through the existing /chat pipeline.
No separate task tracking — SmartLedger and ActionState handle that.

Now also periodically syncs coding benchmark deltas via FederatedAggregator
for hive-wide tool routing intelligence (torrent-like, never interrupts user).
"""
import os
import time
import logging
import threading
from datetime import datetime

logger = logging.getLogger('hevolve_social')


class CodingAgentDaemon:
    """Background daemon: active goals + idle agents → /chat dispatch."""

    def __init__(self):
        self._interval = int(os.environ.get('HEVOLVE_CODING_POLL_INTERVAL', '30'))
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        # Interruptible-sleep primitive. `_running` alone cannot WAKE a
        # sleeping worker — it is only polled between sleeps — so stop()
        # had to wait out the full interval. See _wd_sleep.
        self._stop_event = threading.Event()
        self._tick_count = 0

    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='coding_daemon')
        self._thread.start()
        logger.info(f"Coding daemon started (interval={self._interval}s)")

    def stop(self):
        with self._lock:
            self._running = False
        # Wake the worker NOW rather than letting it finish its nap.
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _wd_heartbeat(self):
        """Send heartbeat to watchdog between potentially blocking operations."""
        try:
            from security.node_watchdog import get_watchdog
            wd = get_watchdog()
            if wd:
                wd.heartbeat('coding_daemon')
        except Exception:
            pass

    def _wd_sleep(self, seconds: float) -> None:
        """Sleep while keeping the coding_daemon heartbeat fresh.

        Delegates to ``NodeWatchdog.sleep_with_heartbeat`` — same
        single primitive the agent_daemon uses, so a long sleep
        (e.g. during platform-affordability back-off) can't age the
        heartbeat past the 300s frozen threshold. See the helper
        docstring for the 2026-04-11 incident context.
        """
        try:
            from security.node_watchdog import get_watchdog
            wd = get_watchdog()
            if wd is not None:
                wd.sleep_with_heartbeat(
                    'coding_daemon', seconds,
                    stop_check=lambda: not self._running,
                )
                return
        except Exception:
            pass
        # FALLBACK — must keep the interruptibility the watchdog path
        # provides via stop_check. A bare time.sleep() does not: it
        # cannot be woken, so stop() blocked for the WHOLE interval
        # (join(timeout=10) then expired in full — measured 10.00s and
        # 10.01s in test_start_stop / test_double_start, the giveaway
        # constant). That hits any node where get_watchdog() returns
        # None or raises, i.e. exactly the degraded case. Event.wait()
        # sleeps the same duration but returns immediately on stop().
        self._stop_event.wait(seconds)

    def _loop(self):
        # Boot grace period: let user chat have exclusive LLM access
        import os as _os
        _boot_grace = int(_os.environ.get('HEVOLVE_DAEMON_BOOT_DELAY', '300'))
        if _boot_grace > 0:
            self._wd_sleep(_boot_grace)

        while self._running:
            self._wd_sleep(self._interval)
            if not self._running:
                break
            self._wd_heartbeat()
            try:
                self._tick()
            except Exception as e:
                import traceback
                logger.error(f"Coding daemon tick error: {e}\n{traceback.format_exc()}")

    def _tick(self):
        """Find active coding goals, find idle agents, dispatch via /chat.

        Queries the unified AgentGoal table filtered by CODING_GOAL_TYPES.
        This daemon handles coding-related goals with idle-agent detection
        and benchmark sync; agent_daemon skips these types.
        """
        from integrations.social.models import get_db, AgentGoal
        from .idle_detection import IdleDetectionService
        from integrations.agent_engine.goal_manager import GoalManager, CODING_GOAL_TYPES
        from .task_distributor import dispatch_to_chat

        self._tick_count += 1

        # USER-YIELD GATE — single canonical primitive that every
        # background daemon consults before burning CPU/GIL/LLM/GPU.
        # ``should_yield_to_user()`` covers BOTH user-activity (chat in
        # last 10 min OR live CREATE pipeline) AND system-pressure
        # (model_lifecycle.get_system_pressure().throttle_factor < 0.1)
        # in one call.  agent_daemon._tick, agent_daemon._proactive_hive_tick,
        # and hive_benchmark_prover._continuous_loop already consult it
        # — coding_daemon was the only background loop missing it,
        # which is why py-spy showed full autogen turns running on the
        # daemon thread while the user was actively chatting.  No new
        # throttle, no parallel resource-aware system — just plug into
        # the existing gate.
        try:
            from integrations.agent_engine.dispatch import should_yield_to_user
            if should_yield_to_user():
                return
        except Exception:
            pass  # gate import unavailable — fall through (fail-open)

        # BUDGET GATE: platform affordability check before dispatching coding tasks
        try:
            from integrations.agent_engine.budget_gate import check_platform_affordability
            can_afford, details = check_platform_affordability()
            if not can_afford:
                logger.warning(f"Coding daemon paused — platform not affordable: {details}")
                return
        except ImportError:
            pass

        db = get_db()
        try:
            # ORDER BY: never-dispatched goals (NULL last_dispatched_at)
            # jump to the front, then by oldest dispatch.  Without this,
            # SQLite returns rows in insert/rowid order — a handful of
            # already-dispatched goals (whose 30s cooldown expires at
            # the same cadence as the daemon's tick interval) saturate
            # the limited idle-agent slots every tick, and never-yet-
            # dispatched goals at the back of the queue (e.g. the 49
            # self_heal goals observed live 2026-05-07) starve forever.
            #
            # SQLAlchemy emits `ORDER BY ... NULLS FIRST` for the
            # `nulls_first()` modifier; SQLite supports the syntax
            # natively since 3.30 (Python 3.11 ships 3.49+).  For
            # backends without the modifier, the .nullsfirst() call
            # is a no-op and rows still order by last_dispatched_at
            # asc (NULLs land first or last depending on the SQL
            # dialect — both orderings prevent the starvation pattern
            # because never-dispatched goals are clearly distinguishable
            # from the ones that just dispatched 30s ago).
            from sqlalchemy import asc
            goals = db.query(AgentGoal).filter(
                AgentGoal.status == 'active',
                AgentGoal.goal_type.in_(CODING_GOAL_TYPES),
            ).order_by(
                asc(AgentGoal.last_dispatched_at).nulls_first(),
            ).all()
            if not goals:
                return

            # Use ``get_idle_agent_personas`` — same canonical gate the
            # agent_daemon uses (agent_daemon.py:555-563).  CODING_GOAL_TYPES
            # are LOCAL maintenance goals (self_heal of THIS node's broken
            # venvs, autoresearch on THIS repo, code_evolution against THIS
            # codebase) — they fix this node, never need human-consent
            # routing.  Do NOT use ``get_idle_opted_in_agents``: that's the
            # distributed-compute privacy gate for peer-share workloads,
            # which is gated downstream by ``dispatch_goal_distributed``
            # already.  Mismatch silently returned [] on installs where no
            # human had opted-in → daemon stalled with self_heal goals
            # piling up (live-evidence 2026-05-07: 42 self_heal goals,
            # 0 with last_dispatched_at populated).  Same root cause +
            # same fix as the agent_daemon's 2026-05-01 switch.
            idle_agents = IdleDetectionService.get_idle_agent_personas(db)
            if not idle_agents:
                return

            dispatched = 0
            agent_idx = 0
            used_agents = set()
            max_concurrent = int(os.environ.get('HEVOLVE_CODING_MAX_CONCURRENT', '10'))
            # Headroom ceiling so the coding swarm leaves cores for the user +
            # UI (2026-06-13).  Shared policy, see dispatch.max_autonomous_concurrency.
            from integrations.agent_engine.dispatch import max_autonomous_concurrency
            max_concurrent = max_autonomous_concurrency(max_concurrent)
            now = datetime.utcnow()

            for goal in goals:
                if dispatched >= max_concurrent:
                    break

                # Skip recently dispatched goals (30s cooldown)
                if goal.last_dispatched_at:
                    age = (now - goal.last_dispatched_at).total_seconds()
                    if age < self._interval:
                        continue

                # Find next available agent
                while agent_idx < len(idle_agents):
                    if idle_agents[agent_idx]['user_id'] not in used_agents:
                        break
                    agent_idx += 1
                if agent_idx >= len(idle_agents):
                    break
                agent = idle_agents[agent_idx]
                used_agents.add(agent['user_id'])
                prompt = GoalManager.build_prompt(goal.to_dict())
                if prompt is None:
                    continue

                goal.last_dispatched_at = now
                result = dispatch_to_chat(prompt, str(agent['user_id']), goal.id,
                                          goal_type=goal.goal_type or 'coding')

                if result is None:
                    # Dispatch failed — track for backoff
                    fails = (goal.config_json or {}).get('_dispatch_failures', 0) + 1
                    cfg = goal.config_json or {}
                    cfg['_dispatch_failures'] = fails
                    goal.config_json = cfg
                    if fails >= 5:
                        goal.status = 'paused'
                        cfg['pause_reason'] = f'Auto-paused: {fails} consecutive dispatch failures'
                        goal.config_json = cfg
                        logger.warning(f"Coding goal {goal.id} AUTO-PAUSED after {fails} failures")
                else:
                    # Success — clear failure count
                    cfg = goal.config_json or {}
                    cfg.pop('_dispatch_failures', None)
                    goal.config_json = cfg

                agent_idx += 1
                dispatched += 1
                self._wd_heartbeat()

            if dispatched > 0:
                logger.info(f"Coding daemon: dispatched {dispatched} goal(s) to idle agents")
            db.commit()
        except Exception as e:
            db.rollback()
            logger.debug(f"Coding daemon error: {e}")
        finally:
            db.close()

        # Every 10 ticks (~5 min): sync benchmark deltas to hive
        # Torrent-like: only during idle windows, never interrupts user
        if self._tick_count % 10 == 0:
            self._sync_benchmark_deltas()

    def _sync_benchmark_deltas(self):
        """Export coding benchmark deltas for hive learning.

        Runs in the daemon thread (low priority, non-blocking).
        FederatedAggregator picks up the delta on its next tick.
        """
        try:
            from .benchmark_tracker import get_benchmark_tracker
            tracker = get_benchmark_tracker()
            delta = tracker.export_learning_delta()
            if delta:
                logger.debug(f"Coding benchmark delta exported: "
                             f"{len(delta.get('coding_benchmarks', {}))} task types")
        except Exception as e:
            logger.debug(f"Benchmark delta sync skipped: {e}")


# Module-level singleton
coding_daemon = CodingAgentDaemon()
