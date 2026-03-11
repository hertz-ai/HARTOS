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

# Lock protecting module-level mutable state accessed from daemon thread + API threads
_module_lock = threading.Lock()

# Track which tasks we've already sent HITL notifications for (avoid spam)
_hitl_notified: set = set()

# Goals that are budget-blocked are immediately paused (no retries).
# Previously we retried 3 times, but that just re-dispatches a blocked goal
# every 30 seconds with no chance of success (budget doesn't change between ticks).
_budget_blocked_goals: set = set()

# Track dispatch failures per goal for exponential backoff.
# Maps goal_id → {'failures': int, 'skip_until': float}
_dispatch_backoff: dict = {}


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
    with _module_lock:
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
        # Cache SmartLedger instances by (agent_id, session_id) to avoid
        # re-creating them every tick (which spams "starting fresh" logs).
        self._ledger_cache: dict = {}

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

    def _try_parallel_dispatch(self, goal, idle_agents, dispatched, max_concurrent):
        """Check if a goal has parallel subtasks and dispatch them concurrently.

        Returns number of tasks dispatched (0 if no parallel tasks found).
        """
        try:
            ledger = self._get_goal_ledger(goal)
            if not ledger:
                return 0

            parallel_tasks = ledger.get_parallel_executable_tasks()
            if not parallel_tasks:
                return 0

            from .parallel_dispatch import dispatch_parallel_tasks
            from .dispatch import dispatch_goal

            batch_count = min(
                len(parallel_tasks),
                len(idle_agents) - dispatched,
                max_concurrent - dispatched,
            )
            if batch_count <= 0:
                return 0

            def _dispatch_task(task):
                """Dispatch a single parallel subtask via /chat."""
                goal_id = str(goal.id) if hasattr(goal, 'id') else ''
                goal_type = goal.goal_type if hasattr(goal, 'goal_type') else 'marketing'
                user_id = str(goal.user_id) if hasattr(goal, 'user_id') else 'system'
                result = dispatch_goal(
                    task.description, user_id, goal_id, goal_type)
                return {'success': result is not None, 'response': result}

            result = dispatch_parallel_tasks(
                ledger, _dispatch_task, max_concurrent=batch_count)

            count = result['completed'] + result['failed']
            if count > 0:
                logger.info(
                    f"Parallel dispatch for goal {goal.id}: "
                    f"{result['completed']} completed, {result['failed']} failed")
            return count

        except Exception as e:
            logger.debug(f"Parallel dispatch check failed: {e}")
            return 0

    def _get_goal_ledger(self, goal):
        """Get a SmartLedger for a goal's task graph (if one exists).

        Returns None when no ledger exists or it has only 1 task (no parallelism).
        Ledger instances are cached on self._ledger_cache to avoid re-creating
        (and re-logging "starting fresh") on every daemon tick.
        """
        try:
            from agent_ledger import SmartLedger
            goal_id = str(goal.id) if hasattr(goal, 'id') else ''
            user_id = str(goal.user_id) if hasattr(goal, 'user_id') else 'system'

            cache_key = (user_id, goal_id)
            ledger = self._ledger_cache.get(cache_key)
            if ledger is None:
                ledger = SmartLedger(agent_id=user_id, session_id=str(goal_id))
                self._ledger_cache[cache_key] = ledger

            # Use the ledger's resolved dir (handles bundled/read-only fallback)
            ledger_path = str(ledger.ledger_file)
            if os.path.isfile(ledger_path):
                ledger.load(ledger_path)
                if len(ledger.tasks) > 1:
                    return ledger
        except Exception:
            pass
        return None

    def _wd_heartbeat(self):
        """Send heartbeat to watchdog between potentially blocking operations."""
        try:
            from security.node_watchdog import get_watchdog
            wd = get_watchdog()
            if wd:
                wd.heartbeat('agent_daemon')
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

        # RESOURCE GATE: throttle dispatch when system is under pressure
        # Prevents machine slowness while Nunba/HARTOS is running
        try:
            from integrations.service_tools.model_lifecycle import (
                get_model_lifecycle_manager)
            _pressure = get_model_lifecycle_manager().get_system_pressure()
            _throttle = _pressure.get('throttle_factor', 1.0)
            if _throttle < 0.1:
                logger.debug(
                    "Agent daemon: system under heavy pressure "
                    f"(throttle={_throttle:.2f}), skipping dispatch")
                return
        except Exception:
            _throttle = 1.0

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

            self._wd_heartbeat()

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
            # Reduce concurrency proportional to system pressure
            max_concurrent = max(1, int(max_concurrent * _throttle))

            for goal in goals:
                if dispatched >= len(idle_agents) or dispatched >= max_concurrent:
                    break

                # ── PARALLEL DISPATCH: check for goals with parallel subtasks ──
                parallel_dispatched = self._try_parallel_dispatch(
                    goal, idle_agents, dispatched, max_concurrent)
                if parallel_dispatched > 0:
                    dispatched += parallel_dispatched
                    continue

                # Find next unused agent (don't skip goal if current agent is taken)
                agent = None
                while dispatched < len(idle_agents) and dispatched < max_concurrent:
                    candidate = idle_agents[dispatched]
                    if candidate['user_id'] not in used_agents:
                        agent = candidate
                        break
                    dispatched += 1
                if agent is None:
                    break  # No more available agents
                used_agents.add(agent['user_id'])

                # Load product if marketing goal
                product_dict = None
                if goal.product_id:
                    product = db.query(Product).filter_by(id=goal.product_id).first()
                    if product:
                        product_dict = product.to_dict()

                # Build prompt using registered builder (guardrail: togetherness rewrite)
                prompt = GoalManager.build_prompt(goal.to_dict(), product_dict)
                if prompt is None:
                    logger.warning(f"Goal {goal.id}: build_prompt returned None (guardrails unavailable?), skipping")
                    continue

                # BUDGET PRE-CHECK: read-only check before dispatch.
                # The actual atomic budget reservation happens inside dispatch_goal()
                # via pre_dispatch_budget_gate(). This is just a quick reject to
                # avoid wasting time on goals that are clearly over budget.
                try:
                    from .budget_gate import estimate_llm_cost_spark, _resolve_model_name
                    goal_key = str(goal.id)

                    # Skip goals already known to be budget-blocked (avoids
                    # re-checking every 30s when nothing has changed).
                    with _module_lock:
                        if goal_key in _budget_blocked_goals:
                            continue

                    # Read-only check: compare remaining budget vs estimated cost
                    # without reserving (no spark_spent increment)
                    budget = goal.spark_budget or 0
                    spent = goal.spark_spent or 0
                    estimated = estimate_llm_cost_spark(prompt, _resolve_model_name())
                    bg_allowed = (budget - spent) >= estimated
                    bg_reason = f'insufficient_budget ({budget - spent} < {estimated})' if not bg_allowed else ''
                    if not bg_allowed:
                        # Immediately pause — budget won't change between daemon
                        # ticks, so retrying is wasteful.
                        goal.status = 'paused'
                        cfg = goal.config_json or {}
                        cfg['pause_reason'] = (
                            f'Auto-paused: budget gate blocked. '
                            f'Reason: {bg_reason}')
                        cfg['paused_at'] = datetime.utcnow().isoformat()
                        goal.config_json = cfg
                        with _module_lock:
                            _budget_blocked_goals.add(goal_key)
                        logger.info(
                            f"Goal {goal.id} AUTO-PAUSED by budget gate: "
                            f"{bg_reason}")
                        continue
                    else:
                        # Budget passed — clear from blocked set if it was
                        # previously blocked (e.g. goal was resumed with
                        # more budget).
                        with _module_lock:
                            _budget_blocked_goals.discard(goal_key)
                except ImportError:
                    pass

                # GUARDRAIL: full pre-dispatch gate
                try:
                    from security.hive_guardrails import GuardrailEnforcer
                    allowed, reason, prompt = GuardrailEnforcer.before_dispatch(
                        prompt, goal.to_dict())
                    if not allowed:
                        logger.warning(f"Goal {goal.id} blocked by guardrail: {reason}")
                        continue
                except ImportError:
                    logger.warning("hive_guardrails not available — dispatch proceeds without guardrail pre-check")

                # Store prompt_id on goal for REUSE tracking
                prompt_id = f"{goal.goal_type}_{goal.id[:8]}"
                if not goal.prompt_id:
                    goal.prompt_id = prompt_id

                # BACKOFF: skip goals that have failed repeatedly
                goal_key = str(goal.id)
                with _module_lock:
                    backoff_info = _dispatch_backoff.get(goal_key)
                if backoff_info and time.time() < backoff_info.get('skip_until', 0):
                    logger.debug(
                        f"Skipping goal {goal_key}: backoff "
                        f"({backoff_info['failures']} failures, "
                        f"resume in {backoff_info['skip_until'] - time.time():.0f}s)")
                    continue

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
                            # Success — clear backoff
                            with _module_lock:
                                _dispatch_backoff.pop(goal_key, None)
                            continue
                    except ImportError:
                        pass

                result = dispatch_goal(prompt, str(agent['user_id']), goal.id, goal.goal_type)
                dispatched += 1
                self._wd_heartbeat()

                # Track failures for exponential backoff
                if result is None:
                    with _module_lock:
                        info = _dispatch_backoff.get(goal_key, {'failures': 0})
                        info['failures'] = info.get('failures', 0) + 1
                        # Exponential backoff: 60s, 120s, 240s, 480s, max 900s (15 min)
                        delay = min(60 * (2 ** (info['failures'] - 1)), 900)
                        info['skip_until'] = time.time() + delay
                        _dispatch_backoff[goal_key] = info
                        failure_count = info['failures']
                    if failure_count >= 5:
                        # Auto-pause after 5 consecutive failures
                        goal.status = 'paused'
                        cfg = goal.config_json or {}
                        cfg['pause_reason'] = (
                            f'Auto-paused: {failure_count} consecutive '
                            f'dispatch failures')
                        cfg['paused_at'] = datetime.utcnow().isoformat()
                        goal.config_json = cfg
                        logger.warning(
                            f"Goal {goal_key} AUTO-PAUSED after "
                            f"{failure_count} dispatch failures")
                else:
                    # Success — clear backoff
                    with _module_lock:
                        _dispatch_backoff.pop(goal_key, None)

                    # COMPLETION: non-continuous goals complete after successful dispatch
                    cfg = goal.config_json or {}
                    if not cfg.get('continuous', False):
                        goal.status = 'completed'
                        cfg['completed_at'] = datetime.utcnow().isoformat()
                        goal.config_json = cfg
                        logger.info(f"Goal {goal_key} COMPLETED (one-shot dispatch succeeded)")

            # ── HITL: notify owners of APPROVAL_REQUIRED tasks ──
            try:
                for goal in goals:
                    if goal.goal_type != 'thought_experiment':
                        continue
                    ledger = self._get_goal_ledger(goal)
                    if not ledger:
                        continue
                    ledger_tasks = _get_blocked_hitl_tasks(ledger, goal.id)
                    for task in ledger_tasks:
                        _send_hitl_notification(db, goal, task)
            except Exception as e:
                logger.debug(f"HITL notification check: {e}")

            if dispatched > 0:
                logger.info(f"Agent daemon: dispatched {dispatched} goal(s) to idle agents")

            # Content gen monitor: check stuck games every 5 ticks (~2.5 min)
            if self._tick_count % 5 == 0:
                try:
                    from .content_gen_tracker import ContentGenTracker
                    stuck = ContentGenTracker.get_stuck_games(db)
                    for game in stuck:
                        game_id = game.get('game_id', '')
                        result = ContentGenTracker.attempt_unblock(db, game_id)
                        if result.get('success'):
                            logger.info(
                                f"Content gen unblocked {game_id}: "
                                f"{result.get('action_taken')}")
                        # Record progress snapshot for delta tracking
                        ContentGenTracker.record_progress_snapshot(db, game_id)
                except Exception as e:
                    logger.debug(f"Content gen monitor: {e}")

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

            # Revenue → trading funding: every 5th remediation cycle
            if self._tick_count % (self._remediate_every * 5) == 0:
                try:
                    from .revenue_aggregator import get_revenue_aggregator
                    rev = get_revenue_aggregator()
                    fund_result = rev.check_and_fund_trading(db)
                    if fund_result.get('funded'):
                        logger.info(
                            f"Revenue aggregator: funded trading with "
                            f"{fund_result['amount']} Spark")
                except Exception as e:
                    logger.debug(f"Revenue funding check: {e}")

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

            self._wd_heartbeat()

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
                self._wd_heartbeat()

            # Monthly API quota reset
            if self._tick_count == 1 or self._tick_count % (self._remediate_every * 10) == 0:
                try:
                    from .commercial_api import CommercialAPIService
                    reset_count = CommercialAPIService.reset_monthly_quotas(db)
                    if reset_count > 0:
                        logger.info(f"Reset monthly API quotas for {reset_count} keys")
                except Exception:
                    pass

            # CLEANUP: prune stale entries from module-level dicts every 100 ticks
            if self._tick_count % 100 == 0:
                active_goal_ids = {str(g.id) for g in goals}
                with _module_lock:
                    stale_backoff = [k for k in _dispatch_backoff if k not in active_goal_ids]
                    for k in stale_backoff:
                        del _dispatch_backoff[k]
                    stale_budget = _budget_blocked_goals - active_goal_ids
                    _budget_blocked_goals -= stale_budget
                    # _hitl_notified: keep entries to avoid re-notifying on restart,
                    # but cap size to prevent unbounded growth
                    if len(_hitl_notified) > 10000:
                        _hitl_notified.clear()
                if stale_backoff or stale_budget:
                    logger.debug(f"Pruned {len(stale_backoff)} backoff + {len(stale_budget)} budget-blocked stale entries")
                # Evict completed/archived goals from ledger cache
                stale_cache = [k for k in self._ledger_cache if k[1] not in active_goal_ids]
                for k in stale_cache:
                    del self._ledger_cache[k]

            db.commit()

            # INSTRUCTION QUEUE: drain queued instructions on idle ticks
            # When agents are idle and goals are dispatched, check if any
            # user has pending queued instructions that can be batch-executed
            if dispatched < len(idle_agents):
                try:
                    from .instruction_queue import get_all_pending
                    from .dispatch import drain_instruction_queue
                    pending = get_all_pending()
                    for uid in list(pending.keys())[:3]:  # Max 3 users per tick
                        drain_instruction_queue(uid)
                except Exception as eq:
                    logger.debug(f"Instruction queue drain: {eq}")

        except Exception as e:
            db.rollback()
            # Clear module-level dicts to stay in sync with rolled-back DB state.
            # Without this, goals marked as budget-blocked or backed-off in memory
            # would be skipped even though their DB status was rolled back.
            with _module_lock:
                _budget_blocked_goals.clear()
                _dispatch_backoff.clear()
            logger.warning(f"Agent daemon tick error (state reset): {e}")
        finally:
            db.close()


# Module-level singleton
agent_daemon = AgentDaemon()
