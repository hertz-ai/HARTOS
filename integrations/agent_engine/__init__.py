"""
Unified Agent Goal Engine

Generic framework for autonomous agent goals: marketing, coding, analytics, etc.
Adding a new agent type = register a prompt builder + tool tags.

Enabled by default (HEVOLVE_AGENT_ENGINE_ENABLED).  Set the env var to
'false' to disable for dev/test isolation.
"""
import os
import logging

logger = logging.getLogger('hevolve_social')

_engine_bp = None


def get_engine_blueprint():
    global _engine_bp
    if _engine_bp is None:
        from .api import agent_engine_bp as bp
        _engine_bp = bp
    return _engine_bp


_agent_engine_initialized = False
_agent_engine_init_lock = None  # threading.Lock() lazy-created in init


def init_agent_engine(app):
    """Initialize the unified agent goal engine.

    Idempotent: safe to call multiple times — only the first call does work.
    The previous opt-in gate (`HEVOLVE_AGENT_ENGINE_ENABLED`) was added on
    2026-04-19 to escape a 244s cold-boot stall caused by the import chain
    `agent_baseline_service` → `helper` (the hart_intelligence top-level
    `helper.py` module) racing with the Nunba `hartos-init` thread on
    `from hart_intelligence import app`.  Both threads enter
    `_find_and_load → _lock_unlock_module → acquire`; CPython serialises
    on the per-module import lock and one thread blocks indefinitely
    because the other is mid-import of a parent package.

    Fix (2026-04-28): keep the function callable unconditionally but make
    the heavy `agent_baseline_service` + `agent_daemon` imports lazy, run
    inside the function body (not at module load), and protected by an
    idempotency guard so duplicate callers (Nunba's main.py
    `_deferred_social_init` AND HARTOS's own `social/__init__.py`) cannot
    double-bootstrap.  The `helper` import inside `agent_baseline_service`
    already has a fallback path, so it is allowed to run after
    hart_intelligence is fully imported (hartos-init done) — we no longer
    race.
    """
    global _agent_engine_initialized, _agent_engine_init_lock

    if _agent_engine_init_lock is None:
        import threading as _t
        _agent_engine_init_lock = _t.Lock()

    with _agent_engine_init_lock:
        if _agent_engine_initialized:
            logger.debug("Agent engine already initialised — skipping duplicate call")
            return
        # Mark BEFORE the heavy work so a second caller racing on the lock
        # exits cleanly even if our own work is still in progress in this
        # thread (we hold the lock for the duration; the second caller
        # can't observe a half-initialised state).
        _agent_engine_initialized = True

    # Default ON.  Set HEVOLVE_AGENT_ENGINE_ENABLED=false to disable.
    # The marketing flywheel needs the daemon ticking to do anything;
    # defaulting off meant new installs sat idle forever unless the operator
    # knew to flip the flag.  Defaulting on means the seeded marketing /
    # outreach / revenue goals start executing on the 30s tick after boot.
    if os.environ.get('HEVOLVE_AGENT_ENGINE_ENABLED', 'true').lower() == 'false':
        logger.info("Agent engine disabled (HEVOLVE_AGENT_ENGINE_ENABLED=false)")
        return

    # Two-phase init:
    #   Phase 1 (synchronous, here on the calling thread): register blueprints.
    #     Their top-level imports are stdlib + Flask + SQLAlchemy + light
    #     project modules — NO heavy chain (no autogen, no helper.py,
    #     no marketing_tools/campaign_service).  Safe to do inline.
    #     MUST happen synchronously because Flask refuses
    #     register_blueprint() once it has handled its first request
    #     (smoking gun: "The setup method 'register_blueprint' can no
    #     longer be called on the application. It has already handled
    #     its first request" — seen in c62a01a boot at 13:01:51).
    #
    #   Phase 2 (deferred thread, after hartos-init signals done):
    #     DB bootstrap (Product + system agent + seed_bootstrap_goals),
    #     AgentBaselineAdapter, agent_daemon.start(), worker_loop.start().
    #     These ARE the heavy chain that caused the original deadlock
    #     (py-spy: hartos-bootstrap, TTSWarmup, ToolsWarmup all serializing
    #     on per-module import locks).  Deferred until hartos-init done
    #     ⇒ no parallel-import contention.

    # ── Phase 1: register blueprints synchronously ──────────────────────

    # Register agent_engine API blueprint
    try:
        bp = get_engine_blueprint()
        app.register_blueprint(bp)
        logger.info("Agent engine endpoints registered")
    except Exception as e:
        logger.warning(f"Agent engine blueprint registration failed: {e}")

    # Register commercial API blueprint — buyer-facing pricing + upgrade
    # endpoints (/api/v1/intelligence/*).  Top-level imports are light
    # (stdlib + flask + sqlalchemy + core.port_registry); route handlers
    # do their AP2 imports lazily inside the function bodies.
    try:
        from .commercial_api import commercial_api_bp
        app.register_blueprint(commercial_api_bp)
        logger.info("Commercial API endpoints registered")
    except Exception as e:
        logger.warning(f"Commercial API blueprint skipped: {e}")

    # Register build distribution blueprint
    try:
        from .build_distribution import build_distribution_bp
        app.register_blueprint(build_distribution_bp)
        logger.info("Build distribution endpoints registered")
    except Exception as e:
        logger.debug(f"Build distribution blueprint skipped: {e}")

    # Register regional host blueprint
    try:
        from integrations.social.api_regional_host import regional_host_bp
        app.register_blueprint(regional_host_bp)
        logger.info("Regional host endpoints registered")
    except Exception as e:
        logger.debug(f"Regional host blueprint skipped: {e}")

    # ── Phase 2: defer heavy work to background thread ──────────────────
    #
    # Why: 2026-05-14 py-spy dump of the deadlocked Nunba.exe (PID 21752)
    # showed three threads simultaneously stuck in CPython importlib:
    #   - `hartos-bootstrap` running `init_social` → calling us → triggering
    #     `from .commercial_api import commercial_api_bp` → exec_module →
    #     get_code → get_data, holding the per-module import lock for the
    #     entire commercial_api dependency graph.
    #   - `ToolsWarmup` running `<module> langchain_core.tracers.log_stream`,
    #     traversing langsmith → opentelemetry — needs to acquire the same
    #     locks held by hartos-bootstrap.
    #   - `TTSWarmup` running `_warmup_tts`, also doing exec_module.
    # All three serialize on CPython's per-module locks; Flask's request
    # handler thread (Hypercorn asyncio loop at app.py:5894) can't import
    # anything to answer /api/* requests because the locks never free.
    # Net result: Nunba.exe process alive, TCP port 5000 accepting, but
    # every request hangs.  User closes the webview because the UI never
    # gets a response from the backend.
    #
    # The 2026-04-28 "lazy imports" fix only deferred the deepest imports
    # (`agent_baseline_service`, `agent_daemon`).  The middle layer
    # (blueprint registration + bootstrap_product + seed_goals) still ran
    # on the calling thread and imported heavy modules.  Moving those to
    # the deferred thread eliminates the race entirely: by the time the
    # thread wakes (after hartos-init signals done), TTSWarmup +
    # ToolsWarmup are also long done with their imports — no contention.
    #
    # Trade-off: agent-engine endpoints (admin dashboard, commercial API)
    # come online a few seconds after Flask starts serving instead of
    # being registered synchronously.  Worth it — the previous behaviour
    # was the entire app hanging forever.
    # Spawn the daemon EARLY on its own dedicated thread — it does not
    # depend on hartos-init or product/seed bootstrap.  Earlier flow
    # waited up to 10 min for hartos-init before calling
    # `agent_daemon.start()`; on any partial Nunba boot that signal
    # never fired and the daemon silently sat dead forever (root cause
    # of the 2502-pending-tasks accumulation seen on 2026-05-19).  The
    # daemon's loop is itself lazy + idempotent, so starting it before
    # the heavy bootstrap is safe.
    def _start_daemon_with_self_heal():
        """Start agent_daemon and re-spawn if its thread dies."""
        import time as _td
        while True:
            try:
                from .agent_daemon import agent_daemon
                # If _running is True but the thread is dead, that's a
                # zombie — clear the flag so start() will re-spawn the
                # worker thread instead of returning early.
                if getattr(agent_daemon, '_running', False) and not (
                        getattr(agent_daemon, '_thread', None)
                        and agent_daemon._thread.is_alive()):
                    logger.warning(
                        "Agent daemon zombie detected (_running=True but thread dead) — "
                        "clearing flag to allow restart")
                    agent_daemon._running = False
                if not getattr(agent_daemon, '_running', False):
                    agent_daemon.start()
                    logger.info("Agent daemon started (early-spawn path)")
                # Heartbeat watchdog: every 60s, check thread is alive
                _td.sleep(60)
            except Exception as e:
                logger.warning(
                    "Agent daemon start/heartbeat failed (will retry in 60s): %s", e)
                _td.sleep(60)

    import threading as _t_early
    _t_early.Thread(
        target=_start_daemon_with_self_heal,
        daemon=True,
        name='agent-daemon-supervisor',
    ).start()
    logger.info("Agent daemon supervisor thread spawned (independent of hartos-init)")

    def _finish_init_deferred():
        import time as _t
        # Wait up to 10 minutes for hartos-init to settle.  By then any
        # parallel TTS/Tools warmup threads have also finished their
        # imports, so the per-module import lock is fully released.
        _start = _t.time()
        _max_wait = 600
        while _t.time() - _start < _max_wait:
            try:
                from routes.hartos_backend_adapter import _hartos_initialized
                if _hartos_initialized:
                    break
            except Exception:
                # Not running inside Nunba — no hartos-init thread to wait
                # for.  Proceed immediately.
                break
            _t.sleep(1)

        # Blueprints already registered synchronously in Phase 1.
        # This thread does only the HEAVY work whose imports are the
        # ones that caused the original 2026-05-14 import-lock deadlock.

        # Bootstrap "HART Platform" product for self-marketing (idempotent)
        product_id = None
        try:
            from integrations.social.models import get_db, Product, User
            db = get_db()
            existing = db.query(Product).filter_by(is_platform_product=True).first()
            if not existing:
                product = Product(
                    name='HART Platform',
                    description='Crowdsourced agentic intelligence platform — a gift from hevolve.ai',
                    tagline='Crowdsourced intelligence, human control',
                    product_url='https://hevolve.ai',
                    category='platform',
                    target_audience='Developers, businesses, and creators who want AI-powered automation',
                    unique_value_prop='96 expert agents, autonomous recipe-based execution, '
                                      'cross-session memory, multi-channel distribution',
                    keywords_json=['AI', 'agents', 'automation', 'marketing', 'chatbot',
                                   'autonomous', 'multi-agent', 'LLM'],
                    is_platform_product=True,
                )
                db.add(product)
                db.flush()
                product_id = str(product.id)
                logger.info("Bootstrapped HART Platform product for self-marketing")
            else:
                product_id = str(existing.id)

            # Bootstrap system agent for goal execution (idempotent)
            sys_agent = db.query(User).filter_by(username='hevolve_system_agent').first()
            if not sys_agent:
                sys_agent = User(
                    username='hevolve_system_agent',
                    display_name='HART System Agent',
                    user_type='agent',
                    idle_compute_opt_in=True,
                    is_admin=False,
                )
                db.add(sys_agent)
                db.flush()
                logger.info("Bootstrapped system agent for goal execution")

            # Seed bootstrap goals (idempotent)
            from .goal_seeding import seed_bootstrap_goals
            count = seed_bootstrap_goals(db, platform_product_id=product_id)
            if count > 0:
                logger.info(f"Seeded {count} bootstrap goal(s)")

            db.commit()
            db.close()
        except Exception as e:
            logger.debug(f"Platform product bootstrap skipped: {e}")

        # Register AgentBaselineAdapter with benchmark registry
        try:
            from .benchmark_registry import get_benchmark_registry
            from .agent_baseline_service import AgentBaselineAdapter
            registry = get_benchmark_registry()
            registry.register_benchmark(AgentBaselineAdapter())
            logger.info("AgentBaselineAdapter registered with benchmark registry")
        except Exception as e:
            logger.debug(f"AgentBaselineAdapter registration skipped: {e}")

        # Start background daemon.  Supervisor thread above
        # (_start_daemon_with_self_heal) usually starts it before we
        # arrive here; this is a fallback/no-op when supervisor already
        # set _running=True.  Bump from debug to warning so any future
        # regression (silent skip) is visible in logs — this exact path
        # silently failed for weeks and caused 2502 pending tasks to pile
        # up on 2026-05-19.
        try:
            from .agent_daemon import agent_daemon
            if not getattr(agent_daemon, '_running', False):
                agent_daemon.start()
                logger.info("Agent engine daemon started (deferred-path fallback)")
            else:
                logger.info("Agent engine daemon already running (supervisor started it)")
        except Exception as e:
            logger.warning(f"Agent engine daemon start skipped: {e}", exc_info=True)

        # Start distributed worker loop (claims tasks from shared Redis)
        try:
            from integrations.distributed_agent.worker_loop import worker_loop
            worker_loop.start()
        except Exception as e:
            logger.debug(f"Distributed worker loop start skipped: {e}")

        logger.info("Agent engine full init complete")

    import threading as _t_mod
    _t_mod.Thread(target=_finish_init_deferred, daemon=True,
                  name='agent-engine-finish').start()
    logger.info("Agent engine init deferred to background thread "
                "(prevents import-lock deadlock with TTSWarmup/ToolsWarmup)")
