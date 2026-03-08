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

logger = logging.getLogger('hevolve_social')


class CodingAgentDaemon:
    """Background daemon: active goals + idle agents → /chat dispatch."""

    def __init__(self):
        self._interval = int(os.environ.get('HEVOLVE_CODING_POLL_INTERVAL', '30'))
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._tick_count = 0

    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(f"Coding daemon started (interval={self._interval}s)")

    def stop(self):
        with self._lock:
            self._running = False
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

    def _loop(self):
        while self._running:
            time.sleep(self._interval)
            if not self._running:
                break
            self._wd_heartbeat()
            try:
                self._tick()
            except Exception as e:
                logger.debug(f"Coding daemon tick error: {e}")

    def _tick(self):
        """Find active goals, find idle agents, dispatch via /chat."""
        from integrations.social.models import get_db, CodingGoal
        from .idle_detection import IdleDetectionService
        from .goal_manager import CodingGoalManager
        from .task_distributor import dispatch_to_chat

        self._tick_count += 1

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
            goals = db.query(CodingGoal).filter_by(status='active').all()
            if not goals:
                return

            idle_agents = IdleDetectionService.get_idle_opted_in_agents(db)
            if not idle_agents:
                return

            dispatched = 0
            used_agents = set()
            max_concurrent = int(os.environ.get('HEVOLVE_CODING_MAX_CONCURRENT', '10'))
            for goal in goals:
                if dispatched >= len(idle_agents) or dispatched >= max_concurrent:
                    break
                agent = idle_agents[dispatched]
                if agent['user_id'] in used_agents:
                    dispatched += 1
                    continue
                used_agents.add(agent['user_id'])
                prompt = CodingGoalManager.build_prompt(goal.to_dict())
                dispatch_to_chat(prompt, str(agent['user_id']), goal.id)
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
