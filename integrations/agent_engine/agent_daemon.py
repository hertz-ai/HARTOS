"""
Unified Agent Goal Engine - Background Daemon

Finds active goals of ANY type, detects idle agents, and dispatches
work through /chat. Replaces separate per-type daemons.

Uses prompt builder registry to build the right prompt per goal type.
Dispatches with autonomous=True so agents self-configure.
"""
import os
import time
import logging
import threading
from datetime import datetime

logger = logging.getLogger('hevolve_social')


class AgentDaemon:
    """Background daemon: active goals (any type) + idle agents → /chat dispatch."""

    def __init__(self):
        self._interval = int(os.environ.get('HEVOLVE_AGENT_POLL_INTERVAL', '30'))
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._tick_count = 0
        self._remediate_every = int(os.environ.get(
            'HEVOLVE_REMEDIATE_INTERVAL_TICKS', '10'))

    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(f"Agent daemon started (interval={self._interval}s)")

    def stop(self):
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self):
        while self._running:
            time.sleep(self._interval)
            if not self._running:
                break
            # Heartbeat to watchdog
            try:
                from security.node_watchdog import get_watchdog
                wd = get_watchdog()
                if wd:
                    wd.heartbeat('agent_daemon')
            except Exception:
                pass
            try:
                self._tick()
            except Exception as e:
                logger.debug(f"Agent daemon tick error: {e}")

    def _tick(self):
        """Find active goals, find idle agents, dispatch via /chat.

        GUARDRAILS enforced at every layer:
        - HiveCircuitBreaker.is_halted() → full stop
        - GuardrailEnforcer.before_dispatch() → per-prompt gate
        - Speculative dispatch when HEVOLVE_SPECULATIVE_ENABLED=true
        """
        self._tick_count += 1

        # GUARDRAIL: circuit breaker — deterministic stop
        try:
            from security.hive_guardrails import HiveCircuitBreaker
            if HiveCircuitBreaker.is_halted():
                return
        except ImportError:
            pass

        from integrations.social.models import get_db, AgentGoal, Product
        from integrations.coding_agent.idle_detection import IdleDetectionService
        from .goal_manager import GoalManager
        from .dispatch import dispatch_goal

        speculative_enabled = os.environ.get(
            'HEVOLVE_SPECULATIVE_ENABLED', 'false').lower() == 'true'

        db = get_db()
        try:
            # DETERMINISTIC STOP: no goals = no action = system is inert
            goals = db.query(AgentGoal).filter_by(status='active').all()
            if not goals:
                return

            idle_agents = IdleDetectionService.get_idle_opted_in_agents(db)
            if not idle_agents:
                return

            dispatched = 0
            used_agents = set()
            max_concurrent = int(os.environ.get('HEVOLVE_AGENT_MAX_CONCURRENT', '10'))

            for goal in goals:
                if dispatched >= len(idle_agents) or dispatched >= max_concurrent:
                    break

                agent = idle_agents[dispatched]
                if agent['user_id'] in used_agents:
                    dispatched += 1
                    continue
                used_agents.add(agent['user_id'])

                # Load product if marketing goal
                product_dict = None
                if goal.product_id:
                    product = db.query(Product).filter_by(id=goal.product_id).first()
                    if product:
                        product_dict = product.to_dict()

                # Build prompt using registered builder (guardrail: togetherness rewrite)
                prompt = GoalManager.build_prompt(goal.to_dict(), product_dict)

                # GUARDRAIL: full pre-dispatch gate
                try:
                    from security.hive_guardrails import GuardrailEnforcer
                    allowed, reason, prompt = GuardrailEnforcer.before_dispatch(
                        prompt, goal.to_dict())
                    if not allowed:
                        logger.warning(f"Goal {goal.id} blocked by guardrail: {reason}")
                        continue
                except ImportError:
                    pass

                # Store prompt_id on goal for REUSE tracking
                prompt_id = f"{goal.goal_type}_{goal.id[:8]}"
                if not goal.prompt_id:
                    goal.prompt_id = prompt_id

                goal.last_dispatched_at = datetime.utcnow()

                # Speculative dispatch if enabled and budget allows
                if speculative_enabled:
                    try:
                        from .speculative_dispatcher import get_speculative_dispatcher
                        dispatcher = get_speculative_dispatcher()
                        if dispatcher.should_speculate(
                                str(agent['user_id']), prompt_id, prompt, goal.to_dict()):
                            dispatcher.dispatch_speculative(
                                prompt, str(agent['user_id']), prompt_id,
                                goal.id, goal.goal_type)
                            dispatched += 1
                            continue
                    except ImportError:
                        pass

                dispatch_goal(prompt, str(agent['user_id']), goal.id, goal.goal_type)
                dispatched += 1

            if dispatched > 0:
                logger.info(f"Agent daemon: dispatched {dispatched} goal(s) to idle agents")

            # Periodic auto-remediation: scan loopholes every Nth tick
            if self._tick_count % self._remediate_every == 0:
                try:
                    from .goal_seeding import auto_remediate_loopholes
                    rem_count = auto_remediate_loopholes(db)
                    if rem_count > 0:
                        logger.info(f"Auto-remediation: created {rem_count} goal(s)")
                except Exception as e:
                    logger.debug(f"Auto-remediation check failed: {e}")

            db.commit()
        except Exception as e:
            db.rollback()
            logger.debug(f"Agent daemon error: {e}")
        finally:
            db.close()


# Module-level singleton
agent_daemon = AgentDaemon()
