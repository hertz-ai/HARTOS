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


def _flow_recipe_exists(prompt_id) -> bool:
    """Does the goal's flow-0 recipe artifact exist on disk?

    Recipes are written ONLY by after_all_actions_terminated() and the
    filename is keyed by dispatch.prompt_id_for_goal's hash, so it cannot
    belong to a different goal. Used by the CREATE/REUSE classifier to route
    dispatches. NOTE: recipe existence proves a procedure RAN once — it is
    NOT goal completion (steward decision 2026-06-10: completion is measured
    in spark transacted; do not wire this into the completion gate).
    """
    try:
        from core.platform_paths import get_recipe_prompts_dir
        _dir = get_recipe_prompts_dir()
    except Exception:
        _dir = 'prompts'
    try:
        return os.path.exists(os.path.join(_dir, f'{prompt_id}_0_recipe.json'))
    except Exception:
        return False


def _goal_identity(goal):
    """Map an AgentGoal row to the cross-node identity dict the peer-reuse
    client keys on. SINGLE source for both the reactive discovery sweep
    (_try_peer_recipe_reuse) and the proactive advert consult
    (_consult_recipe_advert), so the two legs can never key differently.
    """
    cfg = goal.config_json or {}
    return {
        'goal_id': str(goal.id),
        'goal_slug': cfg.get('bootstrap_slug') or '',
        'goal_type': goal.goal_type or '',
        'goal_title': goal.title or '',
        'goal_description': goal.description or '',
        'owner_id': str(goal.owner_id or goal.created_by or ''),
    }


def _consult_recipe_advert(goal, local_prompt_id):
    """PROACTIVE advert-layer consult for a goal with no LOCAL recipe.

    If an admitted peer already gossip-announced that it banked this
    goal's recipe, pull it DIRECTLY from the advertised peer, skipping
    the O(peers) discovery sweep. Returns 'pulled' (recipe banked, classify
    as REUSE) on a fresh advert hit, or None on miss/stale so the caller
    falls through to the reactive _try_peer_recipe_reuse floor (UNCHANGED).
    Never raises: any failure is logged and degrades to None.
    """
    try:
        from integrations.google_a2a.peer_reuse import consume_advert
        return consume_advert(_goal_identity(goal), local_prompt_id)
    except Exception as e:
        logger.info(
            f"Recipe-advert consult failed for goal "
            f"{getattr(goal, 'id', '?')}: {e}")
        return None


def _try_peer_recipe_reuse(goal, local_prompt_id, deadline):
    """Bounded A2A peer-reuse attempt for a goal with no LOCAL recipe.

    Maps the AgentGoal row to the cross-node identity dict and delegates
    to the canonical outbound client (integrations.google_a2a.peer_reuse).
    Returns 'pulled' (recipe banked locally, classify as REUSE),
    'invoked' (executed remotely this tick, outcome recorded), or None
    (fall through to CREATE exactly as before). Never raises: any
    failure is logged and degrades to None so the tick cannot crash.
    """
    try:
        from integrations.google_a2a.peer_reuse import try_peer_recipe_reuse
        return try_peer_recipe_reuse(
            _goal_identity(goal), local_prompt_id, deadline=deadline)
    except Exception as e:
        logger.info(
            f"Peer-reuse attempt failed for goal "
            f"{getattr(goal, 'id', '?')}: {e}")
        return None


def _idle_only_blocked(cfg) -> bool:
    """True when a goal flagged ``config.idle_only=True`` must be skipped
    because the machine is not idle right now.

    Reuses the ONE existing idle detector — ``ResourceGovernor.get_mode()``
    (core/resource_governor.py, MODE_IDLE = user away past
    IDLE_THRESHOLD_SECONDS and no external load) — no parallel scheduler.
    The daemon-wide ``should_yield_to_user()`` gate already backs the whole
    tick off while the user is active, but its STARVATION OVERRIDE forces a
    tick through after sustained yielding; this per-goal check keeps
    idle_only goals (e.g. paper_explanation) out of those forced ticks too.

    Fail-CLOSED on any error: idle_only means PROVEN idle — if the governor
    can't be consulted, the goal waits for the next tick.  Goals without
    the flag are never blocked here.
    """
    if not (cfg or {}).get('idle_only', False):
        return False
    try:
        from core.resource_governor import get_governor, MODE_IDLE
        return get_governor().get_mode() != MODE_IDLE
    except Exception:
        return True


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



def _seconds_since_iso(iso_str):
    """Age in seconds of an ISO timestamp, or None if it cannot be read.

    None means "unknown", and every caller treats unknown as "keep waiting"
    rather than as an expiry — a clock or format problem must never be the
    reason a goal gets paused.
    """
    try:
        stamp = datetime.fromisoformat(str(iso_str).replace('Z', ''))
    except (TypeError, ValueError):
        return None
    return (datetime.utcnow() - stamp).total_seconds()


def _goal_ledger_grounding(goal_id):
    """Did the distributed ledger finish every task for this goal?

    Returns True (all tasks done), False (tasks outstanding), or None when
    this goal is not in the coordinator at all — a purely local dispatch,
    which has no ledger to ground against and falls back to the spark test.

    Read-only: `get_goal_progress` is the coordinator's OWN accounting
    (per-task status + result_hash), so the rule is not re-derived here.
    Note what this is NOT: `coordinator.verify_result()` re-hashes a stored
    result against its stored hash, which detects record tampering, not
    wrong work, so it is deliberately not used as the grounding signal.
    """
    try:
        from .dispatch import _get_distributed_coordinator
        coordinator = _get_distributed_coordinator()
        if not coordinator:
            return None
        progress = coordinator.get_goal_progress(str(goal_id))
        if not isinstance(progress, dict) or progress.get('error'):
            return None
        total = int(progress.get('total_tasks') or 0)
        if total <= 0:
            return None
        return int(progress.get('completed') or 0) >= total
    except Exception as e:
        # Unreachable coordinator must not silently complete a goal, but it
        # must not block one either: None = "no opinion", spark decides.
        logger.debug(f"Ledger grounding unavailable for goal {goal_id}: {e}")
        return None


def _settle_dispatched_goal(db, goal, goal_key):
    """The ONE completion gate every dispatched goal must pass through.

    Dispatch-style-independent by construction: it re-reads the goal from
    the DB, so synchronous (dispatch_goal) and speculative
    (dispatch_speculative) handoffs are settled identically.

    A goal completes only when BOTH hold:
      1. this dispatch cost something (spark_spent > spark_at_dispatch), and
      2. the work is grounded — every ledger task for the goal is done.

    Two proxies used to stand in for that and both were satisfied before the
    work happened (measured on central 2026-09-02):
      - `spark_spent > 0` reads a LIFETIME counter, so a goal that ever spent
        spark passed forever.  Goal a4734e50 carried 101 spark from the day
        before.
      - the distributed branch (dispatch.py:737-752) returns the submitted
        goal id the instant `submit_goal` returns, so the gate ran before any
        work existed.  a4734e50 was marked completed at 12:39:36; its task was
        claimed at 12:43:46 and finished around 12:47 — declared done four
        minutes before the work started.

    Three outcomes, not two.  Making the gate stricter without the middle one
    would send every in-flight goal into the 5-strike noop pause within 2.5
    minutes, which is worse than the bug it fixes:
      completed             — spark spent this dispatch AND ledger grounded
      awaiting_verification — spark spent, work genuinely in flight; no noop
                              strike, bounded by HEVOLVE_GOAL_VERIFY_TIMEOUT_S
      noop                  — no new spark at all; unchanged 5-strike pause
    Continuous goals still never auto-complete.
    """
    try:
        db.refresh(goal)
    except Exception:
        pass  # refresh failure → fall through to attribute read
    cfg = goal.config_json or {}
    is_continuous = cfg.get('continuous', False)
    spark_spent = goal.spark_spent or 0
    # Per-dispatch, not lifetime.  Absent key (a goal dispatched before this
    # code shipped) degrades to the old `> 0` test rather than blocking.
    spark_at_dispatch = cfg.get('spark_at_dispatch')
    if spark_at_dispatch is None:
        spark_this_dispatch = spark_spent
    else:
        spark_this_dispatch = spark_spent - int(spark_at_dispatch or 0)
    # Completion = SPARK: the goal's purpose is spark
    # transacted on real work (steward decision 2026-06-10 —
    # a flow-recipe artifact proves a procedure RAN once, not
    # that the goal's economic outcome happened, so recipe
    # existence must NOT complete a goal). On all-local boxes
    # spark_spent stays 0 today because estimate_llm_cost_
    # spark prices local work at 0 — the fix is metering
    # local work into spark, not weakening this gate.
    if is_continuous:
        # Continuous goals never auto-complete; cooldown gate
        # higher up already prevents re-dispatch storms.
        pass
    elif spark_this_dispatch > 0:
        # Work cost something this time round. Now: did it LAND?
        # goal_key IS str(goal.id) at the only call site, and it is the
        # same string dispatch_goal_distributed submits as the coordinator
        # goal id.  Using it keeps this gate judging its own arguments
        # rather than reaching into the ORM object.
        grounded = _goal_ledger_grounding(goal_key)
        if grounded is False:
            # In flight. Not a noop — do not touch the noop counter, and do
            # not re-spin the goal — but do not sit here silently forever
            # either: the pause below is the bound.
            since = cfg.get('awaiting_verification_since')
            if not since:
                since = datetime.utcnow().isoformat()
                cfg['awaiting_verification_since'] = since
            cfg['last_verification_check'] = datetime.utcnow().isoformat()
            waited = _seconds_since_iso(since)
            timeout_s = int(os.environ.get(
                'HEVOLVE_GOAL_VERIFY_TIMEOUT_S', '1800'))
            if waited is not None and waited > timeout_s:
                goal.status = 'paused'
                cfg['pause_reason'] = (
                    f'Auto-paused: work was dispatched and spark was spent, '
                    f'but the ledger never reported every task done after '
                    f'{int(waited)}s.  Check the distributed worker on this '
                    f'node before resuming.')
                cfg['paused_at'] = datetime.utcnow().isoformat()
                logger.warning(
                    f"Goal {goal_key} AUTO-PAUSED after {int(waited)}s "
                    f"awaiting ledger grounding")
            else:
                logger.info(
                    f"Goal {goal_key} awaiting verification "
                    f"(spark_this_dispatch={spark_this_dispatch}, "
                    f"ledger tasks outstanding)")
            goal.config_json = cfg
        else:
            # grounded is True (every ledger task done) or None (no ledger
            # entry for this goal — a purely local dispatch, where spend on
            # this dispatch is the only evidence that exists).
            goal.status = 'completed'
            cfg['completed_at'] = datetime.utcnow().isoformat()
            cfg['completion_grounding'] = (
                'ledger_tasks_complete' if grounded else 'local_dispatch_spend')
            cfg.pop('noop_dispatch_count', None)
            cfg.pop('awaiting_verification_since', None)
            goal.config_json = cfg
            logger.info(
                f"Goal {goal_key} COMPLETED "
                f"(spark_this_dispatch={spark_this_dispatch}, "
                f"grounding={cfg['completion_grounding']})")
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
                f"Goal {goal_key} dispatched but this dispatch spent no "
                f"spark (lifetime={spark_spent}, noop #{noop_count})")
        goal.config_json = cfg

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
        # ── starvation override ──────────────────────────────────────
        # When the canonical ``should_yield_to_user()`` gate returns True
        # tick after tick (sustained system pressure: ResourceGovernor in
        # ACTIVE mode → throttle=0.05, or IDLE mode with cpu>0.8 →
        # throttle=0.2; either is below the gate's 0.3 threshold), every
        # active goal stalls indefinitely and the self-heal pile-up grows
        # exponentially.
        #
        # Production incident (2026-05-20, 140+ stalled goals, oldest
        # since 2026-05-12): system CPU sustained ~90 % from a different
        # daemon (compute_optimizer process_iter walk) → governor stayed
        # in ACTIVE 60 % of the time → agent_daemon yielded every tick →
        # zero dispatches in 8 days → ``tts.probe`` + ``subprocess.tool_load``
        # error emitters each created a fresh self_heal goal every minute
        # → 70+ duplicate self_heal entries piled up.
        #
        # Fix: track the wall-clock of the last successful tick (one that
        # at least *queried* goals — not necessarily one that dispatched,
        # so an idle system with no active goals doesn't trigger the
        # override).  When the gate has been blocking longer than
        # ``HEVOLVE_AGENT_STARVATION_S`` (default 600 s = 10 min), bypass
        # ``should_yield_to_user()`` for ONE tick and log a WARNING so
        # the operator sees the bypass.  Bounded blast radius — single
        # tick of relief, then back to honoring the gate.  Matches the
        # watchdog's ``mark_in_llm_call`` escalation pattern.
        self._last_tick_completed_at = time.monotonic()
        # 120s default — under sustained pressure the flywheel forces a
        # tick every 2 min instead of every 10.  Combined with the
        # override raising max_concurrent past the throttled floor, this
        # gives ~30-60 growth-goal dispatches/hour even when the
        # ResourceGovernor is in ACTIVE mode.  Tune up if it crowds the
        # user-facing chat path; tune down if the flywheel is still slow.
        self._starvation_s = int(os.environ.get(
            'HEVOLVE_AGENT_STARVATION_S', '120'))
        # ── Agentic-home re-compose cadence ──────────────────────────
        # The home re-composes itself on a slow idle cadence (default 5 min)
        # so the local LLM is only spent when there is something to refresh,
        # never on every 30s tick.  ``0`` here means "compose on the first
        # idle tick" so the surface upgrades from the offline skeleton soon
        # after the boot grace ends.  Tunable via HART_HOME_COMPOSE_INTERVAL_S.
        self._home_compose_interval_s = int(os.environ.get(
            'HART_HOME_COMPOSE_INTERVAL_S', '300'))
        self._next_home_compose_at = 0.0

    def start(self):
        with self._lock:
            if self._running:
                return
            # Never replace a worker that is still alive.  This used to
            # reassign self._thread unconditionally, so a predecessor that
            # was still running became an untracked orphan: it kept
            # executing while _thread pointed at the new one, and the
            # supervisor's is_alive() check -- which only ever inspects the
            # CURRENT _thread -- could not see it.  Measured in one process
            # 2026-08-17, successive thread dumps: 5, 6, 7, 8, 9, 10, 11, 12
            # threads named agent_daemon, +1 per supervisor tick.
            #
            # Re-arm the live worker instead of spawning beside it: its
            # `while self._running` reads True again and it continues.  If it
            # had already left the loop it dies, and the supervisor's next
            # tick sees a dead _thread with _running=True and takes the
            # zombie-recovery branch (integrations/agent_engine/__init__.py),
            # which spawns cleanly.  One tracked worker is diagnosable; N
            # orphans are not.
            if self._thread is not None and self._thread.is_alive():
                self._running = True
                logger.warning(
                    "agent daemon start(): worker %s still alive with "
                    "_running=False -- re-arming it instead of spawning a "
                    "second (orphan-leak guard)", self._thread.name)
                return
            self._running = True
            self._consecutive_failures = 0
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='agent_daemon')
        self._thread.start()
        logger.info(f"Agent daemon started (interval={self._interval}s)")

    def run_forever(self):
        """Start the daemon and BLOCK — the foreground entrypoint for systemd.

        start() spawns the worker thread and returns immediately; a systemd
        ExecStart (nixos/modules/hart-agent.nix calls AgentDaemon().run_forever())
        needs the process to stay alive, so we start then join the worker. If the
        worker thread ever exits this returns and systemd's Restart=on-failure
        relaunches the unit. Without this method the unit crashed on boot with
        AttributeError: 'AgentDaemon' object has no attribute 'run_forever'.
        """
        self.start()
        if self._thread is not None:
            self._thread.join()

    def stop(self):
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=10)

    def _advance_thought_experiments(self):
        """Drive the experiment lifecycle off its own stored deadlines.

        Never raises into the daemon loop: a lifecycle hiccup must not stop
        goal dispatch, same contract as the proactive/home ticks above.
        """
        try:
            from integrations.social.models import db_session
            from integrations.social.thought_experiment_service import (
                ThoughtExperimentService)
            with db_session() as db:
                ThoughtExperimentService.advance_due_experiments(db)
        except Exception as exc:
            logger.debug("Thought-experiment lifecycle tick skipped: %s", exc)

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
        docstring for the full incident context.

        2026-08-06 fix: get_watchdog() used to be checked ONCE, at the
        top of this call. agent_daemon.start() can fire before the
        watchdog singleton exists and this thread gets registered
        (integrations/social/__init__.py registers it later in boot) --
        so the very first call, the long HEVOLVE_DAEMON_BOOT_DELAY
        boot-grace sleep, hit get_watchdog() is None and committed to
        the ENTIRE sleep (hours, when boot-delay is set high) in a bare
        time.sleep() with zero heartbeats. By the time registration
        happened moments later, the thread already looked stale, and
        the watchdog force-restarted it every ~5 minutes for the rest
        of the process's life -- defeating HEVOLVE_DAEMON_BOOT_DELAY
        entirely. Now re-checks get_watchdog() every chunk instead of
        once (also picks up self._running going False sooner, though a
        full interruptible stop() still needs its own fix separately).
        """
        end = time.monotonic() + seconds
        while self._running:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            chunk = min(10.0, remaining)
            try:
                from security.node_watchdog import get_watchdog
                wd = get_watchdog()
                if wd is not None:
                    wd.heartbeat('agent_daemon')
            except Exception:
                pass
            time.sleep(chunk)

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

    def _resume_state_once(self) -> None:
        """Rehydrate in-memory caches from existing save/restore on
        restart.  Generic resume — uses ONLY existing CRUDs.  No new
        schema, no new tables, no new persistence layer.  Idempotent:
        noop on second call.

        Three existing save/restore surfaces are warmed in order:

        1. ``SmartLedger`` (from ``agent_ledger`` package) — persists
           per-(user, goal) task-graph state to JSON at
           ``agent_data/ledger_<user>_<goal>.json``.  Restore happens via
           ``self._get_goal_ledger(goal)`` which already calls
           ``SmartLedger.load(ledger_path)`` (line 222).  Calling it
           here warms ``self._ledger_cache`` so the first dispatch
           inherits the full prior task graph instead of starting
           fresh.  Tasks already in COMPLETED stay completed; tasks in
           IN_PROGRESS resume from their saved checkpoint.

        2. ``task_ledger.TaskLedger`` (morphable agent) — per-user state
           machine slot keyed by ``(user_id, conversation_id)``.  Pure
           in-memory today, but rebuilding the slot at boot keeps
           thread coherence (next turn lands in the existing slot
           rather than spawning a parallel ledger).

        3. ``outreach_crm_tools.check_pending_followups_daemon()`` —
           CRM follow-up scheduler with state in DB rows.  One call
           flushes any sequences whose due-date elapsed while Nunba
           was down.

        What is NOT resumed here (rebuilt lazily on demand, by design):
        - AutoGen ``GroupChat.messages`` — rebuilt from the Message
          table when the next turn arrives.
        - Speculative dispatcher ``active`` map — per-request, no
          mid-flight resume needed (any in-flight request was already
          aborted by process exit).

        Called once per daemon-thread lifetime, after the boot grace
        period and before the first tick.
        """
        if getattr(self, '_resume_done', False):
            return
        self._resume_done = True

        try:
            from integrations.social.models import get_db, AgentGoal
            from integrations.social import task_ledger
        except Exception as e:
            logger.warning(
                "Agent daemon: resume_state_once skipped — required imports "
                "unavailable (%s)", e)
            return

        smart_ledgers_warmed = 0
        task_ledgers_warmed = 0
        try:
            db = get_db()
            try:
                # All active goals — each carries the user_id and goal_id
                # the existing SmartLedger save/restore is keyed by.
                goals = (
                    db.query(AgentGoal)
                      .filter(AgentGoal.status == 'active')
                      .all()
                )
                for goal in goals:
                    # 1. SmartLedger restore via the existing
                    # _get_goal_ledger() — internally calls
                    # SmartLedger.load() if the JSON file exists.
                    try:
                        ledger = self._get_goal_ledger(goal)
                        if ledger is not None:
                            smart_ledgers_warmed += 1
                    except Exception as inner:
                        logger.debug(
                            "resume_state_once: SmartLedger restore failed "
                            "for goal %s: %s", getattr(goal, 'id', '?'), inner)

                    # 2. TaskLedger morphable-agent slot rebuild.  Both
                    # user_id and created_by appear in the schema; prefer
                    # user_id when present.
                    user_id = (
                        getattr(goal, 'user_id', None)
                        or getattr(goal, 'created_by', None)
                    )
                    if not user_id:
                        continue
                    conv_id = (
                        str(goal.prompt_id) if getattr(goal, 'prompt_id', None)
                        else 'nunba'
                    )
                    try:
                        task_ledger.get_or_create(
                            user_id=str(user_id),
                            conversation_id=conv_id,
                        )
                        task_ledgers_warmed += 1
                    except Exception as inner:
                        logger.debug(
                            "resume_state_once: TaskLedger rebuild failed "
                            "for (%s,%s): %s", user_id, conv_id, inner)
            finally:
                db.close()
        except Exception as e:
            logger.warning(
                "Agent daemon: resume_state_once goal-scan failed: %s", e)

        followups_fired = 0
        try:
            from .outreach_crm_tools import check_pending_followups_daemon
            result = check_pending_followups_daemon()
            if isinstance(result, dict):
                followups_fired = int(result.get('processed', 0))
        except Exception as e:
            logger.debug(
                "resume_state_once: pending-followup flush skipped: %s", e)

        logger.info(
            "Agent daemon: resume_state_once — warmed %d SmartLedger(s), "
            "%d TaskLedger slot(s), flushed %d pending follow-up(s)",
            smart_ledgers_warmed, task_ledgers_warmed, followups_fired)

    def _loop(self):
        # Boot grace period: let user chat have exclusive LLM access for 60s.
        # Without this, daemon goals consume all llama-server slots immediately
        # and user /chat requests timeout (the 2026-04-12 slot starvation bug).
        _boot_grace = int(os.environ.get('HEVOLVE_DAEMON_BOOT_DELAY', '300'))
        if _boot_grace > 0:
            logger.info(f"Agent daemon: boot grace period {_boot_grace}s — "
                        f"user chat gets priority. Set HEVOLVE_DAEMON_BOOT_DELAY=0 to disable.")
            self._wd_sleep(_boot_grace)

        # Rehydrate caches from existing CRUDs BEFORE the first tick so
        # the first dispatch sees the same in-memory state a long-running
        # process would have built up.  Single point of resume — covers
        # every conversation slot via task_ledger.get_or_create.
        self._resume_state_once()

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

            # Agentic HOME re-compose (the home composes itself live).  Same
            # fire-and-forget pattern + yield gate as the proactive tick: when
            # the user is actively at the box we yield the local LLM to them
            # (the shell's own client-side refresh keeps the home fresh from the
            # data endpoints meanwhile); when the box is idle the LLM composes a
            # contextual {hero, rows} and pushes it through the EXISTING
            # compose_home feed - so the home is alive whether the user is
            # present or away.  The kill-switch + rate-cap are enforced inside
            # agent_ui_update; this only PACES the producer.
            self._spawn_home_compose_async()

            # Thought-experiment lifecycle.  create_experiment stamps
            # voting_opens_at / voting_closes_at / evaluation_deadline on every
            # row and nothing ever read them back, so every experiment stayed
            # at 'proposed' forever: cast_vote refuses anything outside
            # ('discussing','voting') and auto_evolve gathers only
            # ('voting','evaluating'), so the loop had no votable candidates
            # and no dispatchable ones.  Cheap bounded DB work, so it runs
            # inline rather than in a thread — and deliberately OUTSIDE _tick,
            # which returns early when there are no active goals.  The
            # lifecycle must not depend on whether a goal happens to exist.
            self._advance_thought_experiments()

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

    def _spawn_home_compose_async(self) -> None:
        """Compose + push the agentic home in a daemon thread when the box is
        idle, never blocking the goal-dispatch loop.

        Reuses the autonomous daemon (no new loop) + the existing compose_home
        feed (no new transport).  Three gates keep it cheap + polite:
          1. cadence  - at most once per HART_HOME_COMPOSE_INTERVAL_S (5 min);
          2. yield    - skip while the user is active (should_yield_to_user),
             so the LLM compose never steals the slot from a live chat; the
             shell's own client-side refresh keeps the home fresh meanwhile;
          3. single-flight - skip if the prior compose thread is still running.
        The kill-switch + per-agent rate cap are enforced authoritatively
        inside agent_ui_update (run_home_compose -> compose_home), so this
        method adds no new gate.  Fully guarded - a compose fault never touches
        the daemon loop."""
        now = time.time()
        if now < self._next_home_compose_at:
            return
        prior = getattr(self, '_home_compose_thread', None)
        if prior is not None and prior.is_alive():
            return
        # Yield to a live user: the home stays fresh client-side while busy;
        # the LLM compose is reserved for idle.  Reuse the ONE canonical gate.
        try:
            from .dispatch import should_yield_to_user
            if should_yield_to_user():
                return
        except Exception:
            pass
        # Schedule the next attempt now (even if this one fails) so a flapping
        # compose can't busy-loop the LLM.
        self._next_home_compose_at = now + max(60, self._home_compose_interval_s)

        def _runner():
            try:
                from integrations.agent_engine.liquid_ui_service import (
                    run_home_compose)
                run_home_compose(reason='idle_tick')
            except Exception as e:
                logger.debug(f"Home compose tick error: {e}")

        t = threading.Thread(
            target=_runner, daemon=True, name='home_compose')
        t.start()
        self._home_compose_thread = t

    def _spawn_federation_tick_async(self) -> None:
        """Run the federation epoch in a daemon thread, off the dispatch loop.

        Same shape as _spawn_home_compose_async above, single-flight guard
        included, because federation has exactly the failure mode that guard
        exists for.  get_federated_aggregator() is a singleton holding 7 locks,
        and tick() holds them across peer broadcast, embedding sync and
        resonance sync.

        Run inline, one stuck epoch stops _tick from returning.  2026-08-17:
        nine agent_daemon threads were all parked at `fed.tick()`, 10,023
        ledger tasks never dispatched, and the hive session stayed
        disconnected.  The try/except that wrapped the inline call did not
        help -- a hang is not an exception.

        agent_daemon.py:756-763 records the same failure from 2026-04-29
        (WorldModelBridge.record_interaction).  That fix moved the proactive
        tick off the loop; federation stayed inline and reproduced it.

        tick()'s return value only fed a log line, so nothing downstream needs
        it in-band.  The log still happens, from the worker thread.
        Covered by tests/unit/test_federation_tick_does_not_block_dispatch.py.
        """
        prior = getattr(self, '_federation_thread', None)
        if prior is not None and prior.is_alive():
            return

        def _runner():
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

        t = threading.Thread(
            target=_runner, daemon=True, name='federation_tick')
        t.start()
        self._federation_thread = t

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
        from .dispatch import (dispatch_goal, should_yield_to_user,
                               max_autonomous_concurrency)

        # Single canonical yield gate — user activity + system pressure.
        # See dispatch.should_yield_to_user() docstring for the contract.
        #
        # Starvation override: if the gate has been blocking for longer
        # than ``self._starvation_s`` (default 120 s under sustained
        # pressure → flywheel ticks every 2 min instead of stalling
        # indefinitely), force one tick so the GROWTH-flywheel seed
        # goals (Capital Distributor, Compute Recruiter, Open Source
        # Evangelist, App Marketplace Auto-Promoter, …) actually fire.
        _override_active = False
        if should_yield_to_user():
            from .dispatch import get_last_yield_reason
            _yreason = get_last_yield_reason()
            # monotonic: a wall-clock jump (#24) must not make _starved_for go
            # negative and perpetually yield -> flywheel stall (false-healthy #6)
            _starved_for = time.monotonic() - self._last_tick_completed_at
            if _starved_for < self._starvation_s:
                logger.debug(
                    "Agent daemon: yielding (reason=%s)", _yreason)
                return
            # B1: never override while a user request is in flight RIGHT NOW.
            # The starvation override is for IDLE starvation (user away, governor
            # self-throttling) — NOT for stealing the shared model from a live
            # chat turn.  foreground_request is the most direct "serve me now".
            try:
                from core.foreground import foreground_active
                if foreground_active() or _yreason == 'foreground_request':
                    logger.debug(
                        "Agent daemon: foreground request in flight — yielding "
                        "(starvation override suppressed)")
                    return
            except Exception:
                pass
            _override_active = True
            logger.warning(
                "Agent daemon: STARVATION OVERRIDE — yield gate has blocked "
                "on '%s' for %.0fs (>%ds threshold); forcing one tick to "
                "drain queue.  Set HEVOLVE_AGENT_STARVATION_S to tune.",
                _yreason, _starved_for, self._starvation_s)
        # _throttle is consumed lower down for soft scaling decisions —
        # re-read after the hard yield gate so we still have a value.
        # Under starvation override we ignore the throttle multiplier on
        # ``max_concurrent`` (force-tick should drain a meaningful batch,
        # not the throttle-reduced floor of 1) but keep the soft signal
        # for downstream consumers that haven't opted into the override.
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

            # Use ``get_idle_agent_personas`` — agent-type users (Echo,
            # Quest, Contest Curator, …) eligible for goal dispatch.
            # Do NOT use ``get_idle_opted_in_agents`` here: that filter
            # is the human-consent privacy gate for distributed compute
            # sharing, not the agent-persona gate.  Mismatch silently
            # returned [] on installs where no human had opted in →
            # daemon stalled with seeded personas never dispatching
            # (root-cause logged 2026-05-01).
            idle_agents = IdleDetectionService.get_idle_agent_personas(db)
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
            # Under normal tick: scale concurrency by system pressure.
            # Under starvation override: drain a batch (up to the env
            # cap) instead of being further restricted to the throttled
            # floor of 1 — the whole point of the override is to make
            # the flywheel actually move on these ticks.
            if not _override_active:
                max_concurrent = max(1, int(max_concurrent * _throttle))
            # Headroom ceiling — applies even under starvation override so a
            # force-tick can't drain a wide batch that pegs every core + starves
            # the UI (2026-06-13).  Shared policy, see dispatch.py.
            max_concurrent = max_autonomous_concurrency(max_concurrent)

            # Minimum interval between dispatches for continuous goals.
            # Without this, a continuous goal (e.g. autoresearch coordinator)
            # gets re-dispatched every 30s tick even if the previous dispatch
            # is still running — causing repeated identical actions.
            _CONTINUOUS_COOLDOWN_S = 300  # 5 minutes

            # Split goals into two queues:
            #   - CREATE queue: goals without recipes (need LLM planning, 1 at a time)
            #   - REUSE pool: goals with recipes (cheap replay, round-robin)
            # Resolve the recipe path with the SAME deployment-aware resolver the
            # pipeline SAVES with (helper.PROMPTS_DIR) and the REUSE read uses
            # (cache_loaders) — get_recipe_prompts_dir.  Using get_prompts_dir()
            # here was correct ONLY in the frozen/bundled build (data dir); in
            # Docker/dev the pipeline saves to the code-relative /app|repo/prompts,
            # so this check looked in a DIFFERENT folder and misclassified every
            # reuse-able goal into the CREATE queue.  Single-source
            # dispatch.prompt_id_for_goal hash, not a duplicate md5 (DRY).
            from .dispatch import prompt_id_for_goal
            _create_queue = []
            _reuse_pool = []
            # A2A peer-reuse leg: ONE shared monotonic deadline caps the
            # whole peer sweep this tick (peer_leg_budget_s, default 10s),
            # so a large CREATE backlog can never stall the tick on
            # network I/O. Lazily armed on the first recipe-less goal.
            _peer_deadline = None
            for goal in goals:
                # Shared recipe-existence check (_flow_recipe_exists) — the
                # SAME signal the completion gate uses, so classifier and
                # gate can never drift apart.
                _pid = prompt_id_for_goal(str(goal.id))
                if _flow_recipe_exists(_pid):
                    _reuse_pool.append(goal)
                    continue
                # No LOCAL recipe: before falling into CREATE, ask the
                # hive over the A2A surface. A peer that already banked
                # this goal's recipe ships the bytes (pull -> local
                # REUSE), or executes it remotely as a fallback. All
                # failure modes degrade to CREATE exactly as before.
                if _peer_deadline is None:
                    try:
                        from integrations.google_a2a.peer_reuse import (
                            peer_leg_budget_s)
                        _peer_deadline = time.monotonic() + peer_leg_budget_s()
                    except Exception as e:
                        logger.debug(f"peer_reuse budget unavailable: {e}")
                        _peer_deadline = time.monotonic() + 10.0
                # PROACTIVE advert layer first: a peer already gossip-
                # announced that it banked this goal's recipe, so pull from
                # the advertised peer DIRECTLY (one bounded pull, skips the
                # O(peers) discovery sweep). Miss/stale -> reactive floor
                # below, UNCHANGED, still capped by the shared deadline.
                _peer = _consult_recipe_advert(goal, _pid)
                if _peer is None:
                    _peer = _try_peer_recipe_reuse(goal, _pid, _peer_deadline)
                if _peer == 'pulled':
                    _reuse_pool.append(goal)
                elif _peer == 'invoked':
                    # Work happened remotely this tick; outcome already
                    # recorded through the WorldModelBridge. Skip local
                    # dispatch for this goal until the next tick.
                    continue
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

                cfg = goal.config_json or {}

                # IDLE-ONLY gate: goals flagged idle_only run ONLY while the
                # ResourceGovernor reports MODE_IDLE (see _idle_only_blocked).
                if _idle_only_blocked(cfg):
                    logger.debug(
                        f"Goal {goal.id}: idle_only and machine not idle, "
                        f"skipping")
                    continue

                # Skip continuous goals dispatched recently — let the previous
                # dispatch complete before re-dispatching.
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
                    # build_prompt returns None when EITHER:
                    #   (a) the registered builder declined the goal (e.g.,
                    #       robot goal on a non-robot host) — the builder
                    #       logs its own reason at INFO; OR
                    #   (b) hive_guardrails import failed in 'hard' mode —
                    #       build_prompt logs CRITICAL itself.
                    # In both cases the meaningful signal is upstream; this
                    # is just dispatch-loop bookkeeping.  DEBUG avoids the
                    # 28-per-8h chronic WARNING noise observed in production
                    # (seeded robot goals on hardware-less hosts) and the
                    # historical misattribution to "guardrails unavailable".
                    logger.debug(f"Goal {goal.id}: build_prompt returned None, skipping")
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
                        #
                        # The mutation itself lives in budget_gate, which is
                        # also reached from pre_dispatch_budget_gate.  It used
                        # to be written out inline here, so a goal blocked by
                        # THAT path was logged and left 'active' forever, while
                        # one blocked here was paused: two behaviours for one
                        # decision.  The pause also now honours never_pause,
                        # which this inline copy ignored.
                        from .budget_gate import apply_budget_pause
                        apply_budget_pause(goal, bg_reason)
                        with _module_lock:
                            _budget_blocked_goals.add(goal_key)
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

                # Store prompt_id on goal for REUSE tracking — use the SINGLE
                # SOURCE hash (dispatch.prompt_id_for_goal), NOT a duplicate md5.
                # The CREATE/REUSE split above (and dispatch_goal + the steering
                # bridge) all locate the recipe via this same hash; a second copy
                # of the formula here would silently drift the day it changes,
                # breaking REUSE tracking + bridge GroupChat lookup.  The helper is
                # already numeric (adapter/chat handler reject non-integer ids).
                from .dispatch import prompt_id_for_goal
                prompt_id = prompt_id_for_goal(str(goal.id))
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

                # Snapshot spark BEFORE the handoff so the completion gate can
                # ask "did THIS dispatch cost anything" instead of "has this
                # goal ever cost anything".  goal.spark_spent is a lifetime
                # counter on the row: goal a4734e50 carried spark_spent=101
                # from 2026-09-01 and therefore passed the gate on every
                # subsequent tick without doing any new work.  Written to
                # config_json (no schema change) and consumed by
                # _settle_dispatched_goal below.
                _cfg_pre = goal.config_json or {}
                _cfg_pre['spark_at_dispatch'] = goal.spark_spent or 0
                goal.config_json = _cfg_pre

                # Speculative dispatch if enabled and budget allows.
                #
                # NO `continue` HERE ANY MORE — that word was defect (b),
                # measured fleet-wide 2026-09-02 (Fetch-and-pull, central
                # 94c0fd94 + desktop d47f4205; same signature on .69): with
                # speculation enabled EVERY goal took this branch and skipped
                # the completion gate below, so no goal could ever reach
                # `completed`, accrue a noop count, or auto-pause. Central sat
                # at ~90 dispatches/45min with zero recipes, zero prompts,
                # zero completions. The gate (_settle_dispatched_goal) is
                # DB-backed and dispatch-style-independent, so the SAME gate,
                # noop counter and auto-pause now judge speculative handoffs.
                result = None
                speculated = False
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
                            speculated = True
                            # Handed off — same truth as dispatch_goal's
                            # non-None. Truthy so the failure/backoff arm
                            # (synchronous failures only; speculation reports
                            # none synchronously) is skipped and the
                            # completion gate below runs.
                            result = 'speculative-handoff'
                    except ImportError:
                        pass

                if not speculated:
                    result = dispatch_goal(prompt, str(agent['user_id']), goal.id, goal.goal_type)
                    dispatched += 1
                    self._wd_heartbeat()

                # Track failures for exponential backoff
                if result is None:
                    # dispatch_goal returns None for TRANSIENT defers too (user
                    # actively chatting / Tier-2 breaker open), not just real
                    # failures.  Counting those toward the 5-strike AUTO-PAUSE
                    # below would pause a healthy goal just because the user was
                    # using the machine — the "goals stuck / 0 progress" bug.
                    # Reuse the SAME canonical checks dispatch_goal defers on
                    # (single source — never drifts) and skip without penalty.
                    try:
                        from .dispatch import is_transient_deferral
                        _transient = is_transient_deferral()
                    except Exception:
                        _transient = False
                    if _transient:
                        logger.debug(
                            f"Goal {goal_key}: transient defer (user active / "
                            f"breaker open) — no backoff, no auto-pause")
                        continue
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
                    # Real completion gate — extracted to
                    # _settle_dispatched_goal so it is testable without
                    # driving the 500-line _tick, and so no dispatch style can
                    # skip it again (defect (b) was a `continue` doing exactly
                    # that).
                    _settle_dispatched_goal(db, goal, goal_key)
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

            # Mark this tick as completed (passed the yield gate AND
            # queried goals/agents).  Drives the starvation override in
            # _tick() so persistent yield doesn't stall the queue.  We
            # update this on EVERY tick that gets past the yield gate
            # — even ticks that dispatched 0 — because reaching this
            # point means the gate let work through; the absence of
            # dispatches is a different signal (no idle agents, no
            # active goals, all goals in cooldown) and is unrelated to
            # the user-pressure stall the override is guarding against.
            self._last_tick_completed_at = time.monotonic()

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

                # Flow-recipe reconciler (#85): recover flow recipes for goals
                # whose actions all completed (each {pid}_{flow}_{i}.json saved)
                # but whose flow-level recipe was never written because the
                # local model never emitted the final status:done — so the
                # CREATE/REUSE split keeps re-CREATEing them forever. Pure
                # on-disk assembly, never fabricates a partial flow; flips
                # genuinely-complete orphans from CREATE-churn to REUSE.
                try:
                    from core.flow_recipe_reconcile import (
                        reconcile_orphaned_flow_recipes)
                    rec = reconcile_orphaned_flow_recipes()
                    if rec:
                        logger.info(
                            f"Flow-recipe reconciler: recovered {len(rec)} "
                            f"orphaned flow recipe(s) -> REUSE: {rec}")
                except Exception as e:
                    logger.debug(f"Flow-recipe reconciler: {e}")

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

            # Baseline intelligence check + recipe self-optimizer (Gate 4, #88).
            # The measure half already ran here (validate_against_baseline) but
            # only re-snapshotted on a regression — it never ACTED.  Close the
            # loop: on a corroborated regression, retire the recipe so the
            # existing CREATE/REUSE split re-CREATEs it (with experience hints);
            # then accept the re-CREATE or roll back to the proven-better one.
            if self._tick_count % (self._remediate_every * 2) == 0:
                try:
                    from .agent_baseline_service import (
                        AgentBaselineService, capture_baseline_async)
                    from integrations.social.models import AgentGoal
                    from core import flow_recipe_optimizer as _opt
                    _optimizer_on = os.environ.get(
                        'HEVOLVE_RECIPE_OPTIMIZER', '1') not in ('0', 'false', 'no')
                    active_goals = db.query(AgentGoal).filter(
                        AgentGoal.status.in_(['active', 'completed'])).all()
                    checked = set()
                    for goal in active_goals:
                        _flow = goal.flow_id or 0
                        _pid = str(goal.prompt_id) if goal.prompt_id else ''
                        key = f'{_pid}_{_flow}'
                        if key in checked or not _pid:
                            continue
                        checked.add(key)

                        def _latest_reward():
                            try:
                                snap = AgentBaselineService.get_latest_snapshot(
                                    _pid, _flow) or {}
                                return float((snap.get('lightning_metrics') or {}
                                             ).get('avg_reward') or 0.0)
                            except Exception:
                                return 0.0

                        # ── Resolve any in-flight re-optimization first ──
                        if _optimizer_on and _opt.has_pending_optimization(_pid, _flow):
                            if _opt.recipe_exists(_pid, _flow):  # daemon has re-CREATEd it
                                if _latest_reward() >= (_opt.archived_reward(_pid, _flow) or 0.0):
                                    _opt.accept_reoptimization(_pid, _flow)
                                    logger.info(f"Recipe optimizer: accepted re-optimized {key}")
                                else:
                                    _opt.rollback_recipe(_pid, _flow)
                                    logger.warning(f"Recipe optimizer: rolled back {key} "
                                                   f"(re-CREATE regressed vs retired recipe)")
                            continue  # never stack a new archive while one is pending

                        # ── Measure -> (corroborated) ACT ──
                        result = AgentBaselineService.validate_against_baseline(_pid, _flow)
                        if result and not result.get('passed', True):
                            capture_baseline_async(
                                prompt_id=_pid, flow_id=_flow,
                                trigger='intelligence_change')
                            logger.info(
                                f"Intelligence change detected for {key}: "
                                f"{result.get('regressions', [])}")
                            # Retire ONLY on a second, independent signal (declining
                            # reward trend) + an active goal that will re-dispatch.
                            _trend = AgentBaselineService.compute_trend(_pid, _flow)
                            if (_optimizer_on and goal.status == 'active'
                                    and _trend.get('trend') == 'declining'
                                    and _trend.get('snapshot_count', 0) >= 2):
                                if _opt.archive_recipe_for_reoptimization(
                                        _pid, _flow, _latest_reward()):
                                    logger.info(f"Recipe optimizer: retired underperforming "
                                                f"{key} for re-CREATE (reward-declining + regressed)")
                except Exception as e:
                    logger.debug(f"Baseline intelligence + optimizer check: {e}")

            self._wd_heartbeat()

            # Federation: aggregate learning deltas across peers every 2nd tick.
            # Runs off-thread — see _spawn_federation_tick_async for why.
            if self._tick_count % 2 == 0:
                self._spawn_federation_tick_async()
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
