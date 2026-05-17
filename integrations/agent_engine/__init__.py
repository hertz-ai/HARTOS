"""
Unified Agent Goal Engine

Generic framework for autonomous agent goals: marketing, coding, analytics, etc.
Adding a new agent type = register a prompt builder + tool tags.

Enabled via HEVOLVE_AGENT_ENGINE_ENABLED=true (default: false).
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

    if os.environ.get('HEVOLVE_AGENT_ENGINE_ENABLED', 'false').lower() != 'true':
        logger.info("Agent engine disabled (HEVOLVE_AGENT_ENGINE_ENABLED != true)")
        return

    # Register API blueprint
    try:
        bp = get_engine_blueprint()
        app.register_blueprint(bp)
        logger.info("Agent engine endpoints registered")
    except Exception as e:
        logger.warning(f"Agent engine blueprint registration failed: {e}")
        return

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

    # Register commercial API blueprint
    try:
        from .commercial_api import commercial_api_bp
        app.register_blueprint(commercial_api_bp)
        logger.info("Commercial API endpoints registered")
    except Exception as e:
        logger.debug(f"Commercial API blueprint skipped: {e}")

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

    # Heavy imports (`agent_baseline_service` → `helper` from
    # hart_intelligence top-level; `agent_daemon` → many tools chain) run
    # in a deferred thread.  Reason: when called from Nunba's
    # `_deferred_social_init`, the `hartos-init` thread is concurrently
    # running `from hart_intelligence import app`, which acquires the
    # top-level `hart_intelligence` package import lock.  If this thread
    # tries to import `helper` (a module inside hart_intelligence) at the
    # same instant, CPython's per-module lock serialises and one of the
    # two threads blocks indefinitely (hartos-init holds the package
    # init in progress; we wait for the same lock to do `from helper
    # import PROMPTS_DIR`).  Smoking gun: server.log stack at
    # `_find_and_load → _lock_unlock_module → acquire` from inside
    # `init_agent_engine` line 119.
    #
    # Solution: spawn a small worker thread that polls
    # `routes.hartos_backend_adapter._hartos_initialized` (set True by
    # the hartos-init thread when its `from hart_intelligence import app`
    # finishes, success OR failure), and only THEN do the heavy imports.
    # The Flask app + blueprints + bootstrap above have already been
    # registered synchronously, so the admin dashboard endpoint is live
    # immediately; the daemon and AgentBaselineAdapter come online a few
    # seconds later, exactly like the existing `_deferred_social_init`
    # pattern.
    def _finish_init_deferred():
        import time as _t
        # Wait up to 10 minutes for hartos-init to settle.  If it never
        # settles (Tier-1 retry budget exhausted, network down, etc.),
        # we still proceed because by then the import lock contention is
        # gone — the hartos-init thread has either succeeded or given up.
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

        # Register AgentBaselineAdapter with benchmark registry
        try:
            from .benchmark_registry import get_benchmark_registry
            from .agent_baseline_service import AgentBaselineAdapter
            registry = get_benchmark_registry()
            registry.register_benchmark(AgentBaselineAdapter())
            logger.info("AgentBaselineAdapter registered with benchmark registry")
        except Exception as e:
            logger.debug(f"AgentBaselineAdapter registration skipped: {e}")

        # Start background daemon
        try:
            from .agent_daemon import agent_daemon
            agent_daemon.start()
            logger.info("Agent engine daemon started")
        except Exception as e:
            logger.debug(f"Agent engine daemon start skipped: {e}")

        # Start distributed worker loop (claims tasks from shared Redis)
        try:
            from integrations.distributed_agent.worker_loop import worker_loop
            worker_loop.start()
        except Exception as e:
            logger.debug(f"Distributed worker loop start skipped: {e}")

    import threading as _t_mod
    _t_mod.Thread(target=_finish_init_deferred, daemon=True,
                  name='agent-engine-finish').start()
    logger.info("Agent engine deferred-finish thread spawned (waits for hartos-init)")
