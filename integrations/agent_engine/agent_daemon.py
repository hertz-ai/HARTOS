"""
Unified Agent Goal Engine - Background Daemon

Finds active goals, detects idle agents, and dispatches work through /chat.
Skips CODING_GOAL_TYPES — those are handled by coding_daemon which adds
idle-agent detection and benchmark sync for the coding backends.

Uses prompt builder registry to build the right prompt per goal type.
Dispatches with autonomous=True so agents self-configure.
"""
import os
import time
import random
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
        # Exponential backoff on consecutive tick failures
        self._consecutive_failures = 0
        self._BACKOFF_MAX = 300  # 5 minute cap
        # Proactive hive tick state
        self._next_hive_explore_time = time.time() + random.randint(300, 1800)
        self._base_interval = self._interval  # remember original for optimizer

    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
            self._consecutive_failures = 0
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='agent_daemon')
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

    def _wd_sleep(self, seconds: float) -> None:
        """Sleep for ``seconds`` while keeping the watchdog heartbeat fresh.

        Delegates to the canonical ``NodeWatchdog.sleep_with_heartbeat``
        helper so a 480s exponential-backoff sleep can't silently age
        the heartbeat past the 300s frozen threshold. See the helper's
        docstring for the full incident context. When the watchdog
        isn't available (test mode, early boot) this falls back to a
        plain ``time.sleep`` so behavior is unchanged.
        """
        try:
            from security.node_watchdog import get_watchdog
            wd = get_watchdog()
            if wd is not None:
                wd.sleep_with_heartbeat(
                    'agent_daemon', seconds,
                    stop_check=lambda: not self._running,
                )
                return
        except Exception:
            pass
        time.sleep(seconds)

    def _proactive_hive_tick(self):
        """Proactive hive daemon tick — exploration, self-promotion, and compute optimization.

        Runs alongside the main tick on a random 5-30 minute interval.
        All imports are lazy (try/except) so missing modules never crash the daemon.

        Feeds dense attribution chain to WorldModelBridge via agent_attribution.
        """
        # Single canonical daemon yield gate (user activity + system
        # pressure).  Without this the proactive tick saturates GIL with
        # speculative dispatches (DB churn + LLM HTTP) while the user is
        # actively chatting — py-spy showed 8s+ sampling lag.
        from .dispatch import should_yield_to_user
        if should_yield_to_user():
            return

        now = time.time()

        # Attribution: track this tick as a long-horizon action
        action_id = None
        try:
            from integrations.agent_engine.agent_attribution import (
                begin_action, record_step, complete_action,
            )
            # Periodically clean expired actions (once per hour)
            if int(now) % 3600 < 30:
                from integrations.agent_engine.agent_attribution import get_attribution
                get_attribution().cleanup_expired()
            action_id = begin_action(
                agent_id='agent_daemon',
                action_type='proactive_hive_tick',
                expected_outcome={'status': 'ok'},
                acceptance_criteria=[
                    'benchmark_prover_reachable',
                    'optimizer_health_score > 0',
                ],
            )
        except Exception:
            pass  # Attribution is best-effort — never break the tick

        # ── 1. Random-interval hive exploration ──
        if now >= self._next_hive_explore_time:
            # Schedule next exploration (5-30 minutes from now)
            self._next_hive_explore_time = now + random.randint(300, 1800)
            if action_id:
                try:
                    record_step(action_id, 'hive_exploration_scheduled',
                                state={'next_in_s': self._next_hive_explore_time - now},
                                decision='explore_now', confidence=0.9)
                except Exception:
                    pass

            # Check benchmark prover for active benchmark needs
            benchmark_needs = None
            try:
                from integrations.agent_engine.hive_benchmark_prover import get_prover
                prover = get_prover()
                benchmark_needs = prover.get_status()
                logger.info(
                    f"Proactive hive: benchmark prover checked "
                    f"(loop_running={benchmark_needs.get('loop_running', False)})")
            except ImportError:
                logger.debug("Proactive hive: hive_benchmark_prover not available")
            except Exception as e:
                logger.debug(f"Proactive hive: benchmark prover check failed: {e}")

            # Check hive task protocol for unassigned tasks
            pending_tasks = []
            try:
                from integrations.coding_agent.hive_task_protocol import get_dispatcher
                dispatcher = get_dispatcher()
                pending_tasks = dispatcher.get_pending_tasks()
                if pending_tasks:
                    logger.info(
                        f"Proactive hive: found {len(pending_tasks)} pending "
                        f"hive tasks for dispatch")
            except ImportError:
                logger.debug("Proactive hive: hive_task_protocol not available")
            except Exception as e:
                logger.debug(f"Proactive hive: task protocol check failed: {e}")

            # If idle and tasks exist, auto-dispatch to local Claude hive session
            if pending_tasks:
                try:
                    from integrations.coding_agent.claude_hive_session import get_blueprint
                    for task in pending_tasks[:3]:  # Max 3 tasks per exploration
                        task_desc = getattr(task, 'description', '') or str(task)
                        logger.info(
                            f"Proactive hive: auto-dispatching task to local "
                            f"hive session: {task_desc[:100]}")
                except ImportError:
                    logger.debug("Proactive hive: claude_hive_session not available")
                except Exception as e:
                    logger.debug(f"Proactive hive: hive session dispatch failed: {e}")

        # ── 2. Self-promotion on benchmark results ──
        try:
            from integrations.agent_engine.hive_benchmark_prover import get_prover
            prover = get_prover()
            history = prover.get_benchmark_history(limit=1)
            if history and isinstance(history, list):
                latest = history[0]
                score = latest.get('score', 0)
                benchmark = latest.get('benchmark', 'unknown')
                node_count = latest.get('node_count', 1)
                message = (
                    f"Hive benchmark result: {benchmark} — "
                    f"score={score:.2f} across {node_count} nodes"
                )

                # Post to all connected channels via signal bridge
                try:
                    from integrations.channels.hive_signal_bridge import get_signal_bridge
                    bridge = get_signal_bridge()
                    bridge.broadcast_signal({
                        'type': 'benchmark_result',
                        'benchmark': benchmark,
                        'score': score,
                        'node_count': node_count,
                        'message': message,
                    })
                    logger.info(f"Proactive hive: broadcast benchmark result to channels")
                except ImportError:
                    logger.debug("Proactive hive: hive_signal_bridge not available")
                except Exception as e:
                    logger.debug(f"Proactive hive: signal broadcast failed: {e}")

                # Emit EventBus event for LiquidUI dashboard
                try:
                    from core.platform.events import emit_event
                    emit_event('hive.benchmark.completed', {
                        'benchmark': benchmark,
                        'score': score,
                        'node_count': node_count,
                        'message': message,
                    })
                except ImportError:
                    pass
                except Exception as e:
                    logger.debug(f"Proactive hive: EventBus emit failed: {e}")
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"Proactive hive: self-promotion check failed: {e}")

        # ── 3. Compute optimizer integration ──
        try:
            from core.compute_optimizer import get_optimizer
            optimizer = get_optimizer()
            health = optimizer.get_health_score()

            # Derive load level from health score: <0.3 = high load, >0.7 = idle
            if health < 0.3:
                load_level = 'high'
            elif health > 0.7:
                load_level = 'idle'
            else:
                load_level = 'normal'

            if load_level == 'high':
                # System under load: lengthen tick interval
                new_interval = min(self._base_interval * 3, self._BACKOFF_MAX)
                if self._interval != new_interval:
                    self._interval = new_interval
                    logger.info(
                        f"Proactive hive: system under load, "
                        f"lengthened tick interval to {new_interval}s")
            elif load_level == 'idle':
                # System idle: shorten tick interval, accept more tasks
                new_interval = max(self._base_interval // 2, 10)
                if self._interval != new_interval:
                    self._interval = new_interval
                    logger.info(
                        f"Proactive hive: system idle, "
                        f"shortened tick interval to {new_interval}s")
            else:
                # Normal: restore base interval
                if self._interval != self._base_interval:
                    self._interval = self._base_interval
                    logger.debug(
                        f"Proactive hive: load normal, "
                        f"restored tick interval to {self._base_interval}s")
        except ImportError:
            pass  # compute_optimizer not available yet
        except Exception as e:
            logger.debug(f"Proactive hive: compute optimizer check failed: {e}")

        # ── Attribution: complete action with outcome summary ──
        if action_id:
            try:
                complete_action(action_id, outcome={
                    'status': 'ok',
                    'interval_seconds': self._interval,
                    'completed_at': time.time(),
                })
            except Exception:
                pass

    def _loop(self):
        # Boot grace period: let user chat have exclusive LLM access for 60s.
        # Without this, daemon goals consume all llama-server slots immediately
        # and user /chat requests timeout (the 2026-04-12 slot starvation bug).
        _boot_grace = int(os.environ.get('HEVOLVE_DAEMON_BOOT_DELAY', '300'))
        if _boot_grace > 0:
            logger.info(f"Agent daemon: boot grace period {_boot_grace}s — "
                        f"user chat gets priority. Set HEVOLVE_DAEMON_BOOT_DELAY=0 to disable.")
            self._wd_sleep(_boot_grace)

        while self._running:
            # Exponential backoff: sleep longer on consecutive failures.
            # Uses sleep_with_heartbeat so the backoff can't age the
            # watchdog heartbeat past the frozen threshold — a 480s
            # backoff used to trigger a restart cascade because the
            # 300s threshold hit mid-sleep (2026-04-11 incident).
            if self._consecutive_failures > 0:
                backoff = min(
                    self._interval * (2 ** self._consecutive_failures),
                    self._BACKOFF_MAX)
                self._wd_sleep(backoff)
            else:
                self._wd_sleep(self._interval)
            if not self._running:
                break
            self._wd_heartbeat()

            # Proactive hive tick — exploration, self-promotion, compute
            # optimization.  RUN ASYNC: a stuck call inside the proactive
            # path (WorldModelBridge.record_interaction was the culprit
            # 2026-04-29 — daemon restarted 9 times by watchdog without
            # ever reaching _tick() because complete_action blocked) must
            # NOT block the goal-dispatch tick.  Fire-and-forget in a
            # daemon thread; watchdog heartbeat stays fresh because
            # _loop continues immediately to _tick.
            self._spawn_proactive_hive_tick_async()

            try:
                self._tick()
                self._consecutive_failures = 0
            except Exception as e:
                self._consecutive_failures += 1
                import traceback
                logger.error(f"Agent daemon tick error (backoff={self._consecutive_failures}): "
                             f"{e}\n{traceback.format_exc()}")

    def _spawn_proactive_hive_tick_async(self) -> None:
        """Run _proactive_hive_tick in a daemon thread, never blocking _loop.

        If the prior iteration's thread is still alive (sub-call stuck on
        sync I/O), we SKIP this iteration rather than pile up threads.
        That makes the proactive cadence "best-effort" while keeping the
        main goal-dispatch tick on its 30s rhythm regardless.

        Watchdog heartbeat is owned by _loop, not the proactive thread —
        so a stuck sub-call no longer ages the daemon's heartbeat past
        the 300s frozen threshold.
        """
        prior = getattr(self, '_proactive_thread', None)
        if prior is not None and prior.is_alive():
            logger.warning(
                "proactive_hive_tick from prior iteration still running — "
                "skipping this cycle (likely a sub-call is blocked on I/O)")
            return

        def _runner():
            try:
                self._proactive_hive_tick()
            except Exception as e:
                logger.debug(f"Proactive hive tick error: {e}")

        t = threading.Thread(
            target=_runner, daemon=True, name='proactive_hive_tick')
        t.start()
        self._proactive_thread = t

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
        from .goal_manager import GoalManager, CODING_GOAL_TYPES
        from .dispatch import dispatch_goal, should_yield_to_user

        # Single canonical yield gate — user activity + system pressure.
        # See dispatch.should_yield_to_user() docstring for the contract.
        if should_yield_to_user():
            logger.debug("Agent daemon: yielding (user active or system pressure)")
            return
        # _throttle is consumed lower down for soft scaling decisions —
        # re-read after the hard yield gate so we still have a value.
        try:
            from integrations.service_tools.model_lifecycle import (
                get_model_lifecycle_manager)
            _throttle = get_model_lifecycle_manager().get_system_pressure().get(
                'throttle_factor', 1.0)
        except Exception:
            _throttle = 1.0

        speculative_enabled = os.environ.get(
            'HEVOLVE_SPECULATIVE_ENABLED', 'false').lower() == 'true'

        db = get_db()
        try:
            # DETERMINISTIC STOP: no goals = no action = system is inert
            # Skip CODING_GOAL_TYPES — coding_daemon handles those with
            # idle-agent detection + benchmark sync for backend routing.
            goals = db.query(AgentGoal).filter(
                AgentGoal.status == 'active',
                ~AgentGoal.goal_type.in_(CODING_GOAL_TYPES),
            ).all()
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

            # Minimum interval between dispatches for continuous goals.
            # Without this, a continuous goal (e.g. autoresearch coordinator)
            # gets re-dispatched every 30s tick even if the previous dispatch
            # is still running — causing repeated identical actions.
            _CONTINUOUS_COOLDOWN_S = 300  # 5 minutes

            # Split goals into two queues:
            #   - CREATE queue: goals without recipes (need LLM planning, 1 at a time)
            #   - REUSE pool: goals with recipes (cheap replay, round-robin)
            import hashlib as _hlib
            _create_queue = []
            _reuse_pool = []
            for goal in goals:
                _gh = int(_hlib.md5(str(goal.id).encode()).hexdigest()[:10], 16) % 100_000_000_000
                _pid = str(max(1, _gh))
                _recipe_path = os.path.join('prompts', f'{_pid}_0_recipe.json')
                if os.path.exists(_recipe_path):
                    _reuse_pool.append(goal)
                else:
                    _create_queue.append(goal)

            # REUSE goals round-robin (cheap, can cycle through many per tick)
            # CREATE goals sequential (1 at a time, rotated so each gets a turn)
            if _create_queue:
                _cr_offset = self._tick_count % len(_create_queue)
                _create_queue = _create_queue[_cr_offset:] + _create_queue[:_cr_offset]
            if _reuse_pool:
                _re_offset = self._tick_count % len(_reuse_pool)
                _reuse_pool = _reuse_pool[_re_offset:] + _reuse_pool[:_re_offset]

            # Prioritize: 1 CREATE first (if any), then fill remaining slots with REUSE
            goals = _create_queue[:1] + _reuse_pool + _create_queue[1:]

            logger.debug(f"Goal split: {len(_create_queue)} need CREATE, "
                         f"{len(_reuse_pool)} have recipes (REUSE)")

            for goal in goals:
                if dispatched >= len(idle_agents) or dispatched >= max_concurrent:
                    break

                # Skip continuous goals dispatched recently — let the previous
                # dispatch complete before re-dispatching.
                cfg = goal.config_json or {}
                if cfg.get('continuous', False) and goal.last_dispatched_at:
                    elapsed = (datetime.utcnow() - goal.last_dispatched_at).total_seconds()
                    if elapsed < _CONTINUOUS_COOLDOWN_S:
                        logger.debug(
                            f"Continuous goal {goal.id} dispatched {elapsed:.0f}s ago "
                            f"(cooldown={_CONTINUOUS_COOLDOWN_S}s), skipping")
                        continue

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
                    estimated = estimate_llm_cost_spark(prompt, _resolve_model_name('gpt-4o'))
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
                # Must be NUMERIC — the adapter and /chat handler reject non-integer prompt_ids.
                # Use deterministic hash of goal.id so same goal always gets same prompt_id.
                import hashlib
                _gh = int(hashlib.md5(str(goal.id).encode()).hexdigest()[:10], 16) % 100_000_000_000
                prompt_id = str(max(1, _gh))
                if not goal.prompt_id or not str(goal.prompt_id).isdigit():
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

                    # COMPLETION: dispatch returning non-None means the chat was
                    # handed off, NOT that the agent actually did the work — the
                    # work runs async after dispatch_goal returns.  Auto-marking
                    # `completed` on dispatch produced the dashboard lie where
                    # ~280 goals showed 'Completed' with 0/N spark earned.
                    #
                    # Real completion gate: refresh from DB and require
                    # spark_spent > 0 (some real tool/LLM cost was incurred).
                    # Goals dispatched but doing zero work get a 'noop'
                    # counter; after 5 consecutive noops the goal is
                    # auto-paused so the daemon stops re-spinning it.
                    try:
                        db.refresh(goal)
                    except Exception:
                        pass  # refresh failure → fall through to attribute read
                    cfg = goal.config_json or {}
                    is_continuous = cfg.get('continuous', False)
                    spark_spent = goal.spark_spent or 0
                    if is_continuous:
                        # Continuous goals never auto-complete; cooldown gate
                        # higher up already prevents re-dispatch storms.
                        pass
                    elif spark_spent > 0:
                        goal.status = 'completed'
                        cfg['completed_at'] = datetime.utcnow().isoformat()
                        cfg.pop('noop_dispatch_count', None)
                        goal.config_json = cfg
                        logger.info(
                            f"Goal {goal_key} COMPLETED "
                            f"(spark_spent={spark_spent})")
                    else:
                        # Dispatched but zero real work — track and back off.
                        noop_count = int(cfg.get('noop_dispatch_count', 0)) + 1
                        cfg['noop_dispatch_count'] = noop_count
                        cfg['last_noop_dispatch'] = datetime.utcnow().isoformat()
                        if noop_count >= 5:
                            goal.status = 'paused'
                            cfg['pause_reason'] = (
                                f'Auto-paused: {noop_count} consecutive '
                                f'dispatches produced 0 spark — work is not '
                                f'reaching tool execution.  Investigate '
                                f'agent prompt or tool registration.')
                            cfg['paused_at'] = datetime.utcnow().isoformat()
                            logger.warning(
                                f"Goal {goal_key} AUTO-PAUSED after "
                                f"{noop_count} noop dispatches")
                        else:
                            logger.info(
                                f"Goal {goal_key} dispatched but "
                                f"spark_spent=0 (noop #{noop_count})")
                        goal.config_json = cfg

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

                # Outreach follow-up checker: send due follow-ups every 5 ticks
                try:
                    from .outreach_crm_tools import check_pending_followups_daemon
                    followup_result = check_pending_followups_daemon()
                    sent = followup_result.get('sent', 0)
                    if sent > 0:
                        logger.info(f"Outreach follow-ups: sent {sent} follow-up email(s)")
                except ImportError:
                    pass  # outreach_crm_tools not available
                except Exception as e:
                    logger.debug(f"Outreach follow-up check: {e}")

                # Journey engine tick: stage transitions, A/B analysis, channel routing
                try:
                    from .journey_engine import journey_daemon_tick
                    journey_result = journey_daemon_tick()
                    if journey_result.get('transitions', 0) > 0 or journey_result.get('actions', 0) > 0:
                        logger.info(f"Journey engine: {journey_result.get('transitions', 0)} transitions, "
                                    f"{journey_result.get('actions', 0)} actions")
                except ImportError:
                    pass  # journey_engine not available
                except Exception as e:
                    logger.debug(f"Journey engine tick: {e}")

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

            # Marketplace payment reconciliation: retry failed Spark transfers.
            # Rows with status='pending' older than 15 minutes are re-attempted
            # through the canonical ResonanceService gateway. Prior to this
            # hook, a failed Spark transfer during install_app() was silently
            # dropped with no retry path — creators lost revenue on
            # transient DB errors.
            if self._tick_count % self._remediate_every == 0:
                try:
                    from .app_marketplace import get_marketplace
                    mp = get_marketplace()
                    counters = mp.reconcile_pending_payments()
                    if counters.get('retried', 0) > 0 or counters.get('failed', 0) > 0:
                        logger.info(
                            f"Marketplace reconcile: "
                            f"retried={counters.get('retried', 0)}, "
                            f"settled={counters.get('settled', 0)}, "
                            f"failed={counters.get('failed', 0)}")
                except ImportError:
                    pass
                except Exception as e:
                    logger.debug(f"Marketplace reconcile failed: {e}")

            # CLEANUP: prune stale entries from module-level dicts every 100 ticks
            if self._tick_count % 100 == 0:
                active_goal_ids = {str(g.id) for g in goals}
                with _module_lock:
                    stale_backoff = [k for k in _dispatch_backoff if k not in active_goal_ids]
                    for k in stale_backoff:
                        del _dispatch_backoff[k]
                    stale_budget = _budget_blocked_goals - active_goal_ids
                    _budget_blocked_goals.difference_update(stale_budget)
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
