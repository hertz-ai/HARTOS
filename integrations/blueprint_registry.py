"""
Blueprint Registry — Single entry point for registering all HARTOS blueprints.

Both Nunba (main.py, direct mode) and standalone (hart_intelligence_entry.py)
call register_all_blueprints(app) to get every endpoint wired.

Each registration is try/except guarded — missing dependencies are logged
and skipped. The app starts with whatever is available.
"""
import logging

logger = logging.getLogger('hevolve.blueprints')


def register_all_blueprints(app) -> dict:
    """Register all HARTOS blueprints on a Flask app.

    Returns:
        Dict with 'registered' (list of names) and 'skipped' (list of names).
    """
    registered = []
    skipped = []

    # Collect already-registered blueprint names to avoid collisions
    existing_bp_names = {bp.name for bp in app.blueprints.values()} if hasattr(app, 'blueprints') else set()

    def _try_register(name: str, import_fn):
        try:
            bp = import_fn()
            if bp is None:
                skipped.append(name)
                logger.debug("Blueprint returned None: %s", name)
                return
            # Skip if already registered (avoids duplicate name errors)
            bp_name = getattr(bp, 'name', name)
            if bp_name in existing_bp_names:
                skipped.append(name)
                logger.debug("Blueprint already registered, skipping: %s", bp_name)
                return
            app.register_blueprint(bp)
            existing_bp_names.add(bp_name)
            registered.append(name)
            logger.info("Registered blueprint: %s", name)
        except ImportError as e:
            skipped.append(name)
            logger.debug("Blueprint import failed (%s): %s", name, e)
        except Exception as e:
            skipped.append(name)
            logger.warning("Blueprint init failed (%s): %s", name, e)

    # ── Hive Session ──
    _try_register('hive_session', lambda: (
        __import__('integrations.coding_agent.claude_hive_session',
                   fromlist=['create_hive_session_blueprint'])
        .create_hive_session_blueprint()
    ))

    # ── Hive Signal Bridge ──
    _try_register('hive_signals', lambda: (
        __import__('integrations.channels.hive_signal_bridge',
                   fromlist=['create_signal_blueprint'])
        .create_signal_blueprint()
    ))

    # ── Benchmark Prover ──
    _try_register('benchmark_prover', lambda: (
        __import__('integrations.agent_engine.hive_benchmark_prover',
                   fromlist=['create_benchmark_blueprint'])
        .create_benchmark_blueprint()
    ))

    # ── Claude Code frontier endpoint (EXPERT tier) ──
    # Registered HERE rather than at either entry point, because this module is
    # the single place both of them call. hart_intelligence_entry.py:1103 also
    # registers it on its own module-level app, but Nunba's frozen entry is
    # app.py/main.py, so that registration landed on an app nobody serves: the
    # desktop logged "registered" three times from the ToolsWarmup import while
    # /api/claude/v1/models answered 404 on :5000. Adding it to main.py as well
    # would have made a THIRD hand-maintained list. _try_register already skips
    # a blueprint whose name is taken, so the duplicate entry-point registration
    # is harmless until it is removed.
    #
    # Load-bearing: model_registry.py:464 points the EXPERT ModelBackend at
    # 'http://127.0.0.1:<backend port>/api/claude/v1'. Without this mount the
    # tier registers and then 404s on every call, so a node with a working,
    # logged-in Claude Code silently never uses it.
    _try_register('claude_code', lambda: (
        __import__('integrations.providers.claude_code_endpoint',
                   fromlist=['claude_code_bp'])
        .claude_code_bp
    ))

    # ── App Marketplace ──
    _try_register('marketplace', lambda: (
        __import__('integrations.agent_engine.app_marketplace',
                   fromlist=['marketplace_bp'])
        .marketplace_bp
    ))

    # ── Robotics Hardware Bridge ──
    _try_register('robotics', lambda: (
        __import__('integrations.robotics.hardware_bridge',
                   fromlist=['create_robotics_blueprint'])
        .create_robotics_blueprint()
    ))

    # ── Robot Intelligence API ──
    # Canonical routes at /api/robotics/ai/* via robot_intelligence_bp
    # (registered directly in hart_intelligence_entry.py). The previous
    # create_intelligence_blueprint() entry was a duplicate /api/robotics/
    # intelligence/{think,robots} subset — removed 2026-04-15.

    # ── Compute Optimizer ──
    def _register_optimizer():
        mod = __import__('core.compute_optimizer',
                         fromlist=['create_optimizer_blueprint', 'get_optimizer'])
        bp = mod.create_optimizer_blueprint()
        if bp:
            # Start optimizer in background (event-driven, zero overhead when idle)
            try:
                mod.get_optimizer().start()
            except Exception:
                pass
        return bp
    _try_register('compute_optimizer', _register_optimizer)

    logger.info(
        "Blueprint registry: %d registered, %d skipped",
        len(registered), len(skipped),
    )
    return {'registered': registered, 'skipped': skipped}
