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

# Track which tasks we've already sent HITL notifications for (avoid spam)
_hitl_notified: set = set()


def _get_blocked_hitl_tasks(ledger, goal_id):
    """Get tasks blocked with APPROVAL_REQUIRED under a goal."""
    try:
        parent = ledger.get_task(goal_id)
        if not parent:
            return []
        blocked = []
        for child_id in (parent.child_task_ids or []):
            task = ledger.get_task(child_id)
            if task and str(task.blocked_reason) == 'APPROVAL_REQUIRED':
                blocked.append(task)
        return blocked
    except Exception:
        return []


def _send_hitl_notification(db, goal, task):
    """Send a one-time HITL notification for an approval-blocked task."""
    notif_key = f"{goal.id}:{task.id}"
    if notif_key in _hitl_notified:
        return
    _hitl_notified.add(notif_key)

    try:
        from integrations.social.services import NotificationService
        from integrations.social.realtime import on_notification
        owner_id = goal.created_by or goal.owner_id
        if not owner_id:
            return
        desc_preview = (task.description or '')[:100]
        notif = NotificationService.create(
            db, str(owner_id), 'approval_required',
            target_type='thought_experiment',
            target_id=str(task.id),
            message=f'Agent needs your review: {desc_preview}',
        )
        on_notification(str(owner_id), notif.to_dict() if hasattr(notif, 'to_dict') else {
            'type': 'approval_required', 'message': f'Agent needs your review: {desc_preview}',
        })
        logger.info(f"HITL notification sent for goal={goal.id} task={task.id}")
    except Exception as e:
        logger.debug(f"HITL notification failed: {e}")


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

            # Assign excess idle agents as exception watchers
            try:
                from .exception_watcher import ExceptionWatcher
                watcher = ExceptionWatcher.get_instance()
                if len(idle_agents) > len(goals):
                    excess = idle_agents[len(goals):]
                    for agent in excess:
                        watcher.assign_watcher(str(agent['user_id']), agent['username'])
                if watcher.has_watchers():
                    watcher.process_exceptions(db)
            except Exception as e:
                logger.debug(f"Exception watcher: {e}")

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

            # ── HITL: notify owners of APPROVAL_REQUIRED tasks ──
            try:
                from agent_ledger.core import SmartLedger
                ledger = SmartLedger()
                for goal in goals:
                    if goal.goal_type != 'thought_experiment':
                        continue
                    ledger_tasks = _get_blocked_hitl_tasks(ledger, goal.id)
                    for task in ledger_tasks:
                        _send_hitl_notification(db, goal, task)
            except Exception as e:
                logger.debug(f"HITL notification check: {e}")

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

                # Intelligence milestone: auto-file patent when threshold reached
                try:
                    from .ip_service import IPService
                    milestone = IPService.check_intelligence_milestone(db)
                    if milestone.get('triggered', False):
                        from integrations.social.models import AgentGoal
                        active_filing = db.query(AgentGoal).filter(
                            AgentGoal.status == 'active',
                            AgentGoal.goal_type == 'ip_protection',
                        ).all()
                        has_filing = any(
                            (g.config_json or {}).get('mode') == 'file'
                            for g in active_filing
                        )
                        if not has_filing:
                            from .goal_manager import GoalManager
                            GoalManager.create_goal(
                                db,
                                goal_type='ip_protection',
                                title='Auto-File Provisional Patent: Critical Intelligence Reached',
                                description=(
                                    f'Intelligence milestone triggered: '
                                    f'{milestone["consecutive_verified"]} consecutive verified days, '
                                    f'moat catch-up: {milestone["moat_catch_up"]}. '
                                    f'Use draft_patent_claims then draft_provisional_patent.'
                                ),
                                config={'mode': 'file', 'auto_triggered': True,
                                        'milestone': milestone},
                                spark_budget=500,
                                created_by='intelligence_milestone',
                            )
                            logger.info("Intelligence milestone reached — auto-patent goal created")
                except Exception as e:
                    logger.debug(f"Intelligence milestone check: {e}")

                # Self-healing: create fix goals for recurring exceptions
                try:
                    from .self_healing_dispatcher import SelfHealingDispatcher
                    healer = SelfHealingDispatcher.get_instance()
                    fix_count = healer.check_and_dispatch(db)
                    if fix_count > 0:
                        logger.info(f"Self-healing: created {fix_count} fix goal(s)")
                except Exception as e:
                    logger.debug(f"Self-healing check: {e}")

            # Baseline intelligence check: re-snapshot when world model stats shift
            if self._tick_count % (self._remediate_every * 2) == 0:
                try:
                    from .agent_baseline_service import (
                        AgentBaselineService, capture_baseline_async)
                    from integrations.social.models import AgentGoal
                    active_goals = db.query(AgentGoal).filter(
                        AgentGoal.status.in_(['active', 'completed'])).all()
                    checked = set()
                    for goal in active_goals:
                        key = f'{goal.prompt_id}_{goal.flow_id or 0}'
                        if key in checked or not goal.prompt_id:
                            continue
                        checked.add(key)
                        result = AgentBaselineService.validate_against_baseline(
                            str(goal.prompt_id), goal.flow_id or 0)
                        if result and not result.get('passed', True):
                            capture_baseline_async(
                                prompt_id=str(goal.prompt_id),
                                flow_id=goal.flow_id or 0,
                                trigger='intelligence_change')
                            logger.info(
                                f"Intelligence change detected for {key}: "
                                f"{result.get('regressions', [])}")
                except Exception as e:
                    logger.debug(f"Baseline intelligence check: {e}")

            # Federation: aggregate learning deltas across peers every 2nd tick
            if self._tick_count % 2 == 0:
                try:
                    from .federated_aggregator import get_federated_aggregator
                    fed = get_federated_aggregator()
                    fed_result = fed.tick()
                    if fed_result.get('aggregated'):
                        logger.info(
                            f"Federation: epoch={fed_result.get('epoch')}, "
                            f"convergence={fed_result.get('convergence', 0):.3f}")
                except Exception as e:
                    logger.debug(f"Federation tick: {e}")

            # Monthly API quota reset
            if self._tick_count == 1 or self._tick_count % (self._remediate_every * 10) == 0:
                try:
                    from .commercial_api import CommercialAPIService
                    reset_count = CommercialAPIService.reset_monthly_quotas(db)
                    if reset_count > 0:
                        logger.info(f"Reset monthly API quotas for {reset_count} keys")
                except Exception:
                    pass

            db.commit()
        except Exception as e:
            db.rollback()
            logger.debug(f"Agent daemon error: {e}")
        finally:
            db.close()


# Module-level singleton
agent_daemon = AgentDaemon()
