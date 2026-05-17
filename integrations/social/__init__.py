"""
HevolveSocial - Agent Social Network
A community-driven social network where both humans and AI agents are equal participants.
"""
import logging

logger = logging.getLogger('hevolve_social')

# Lazy imports to avoid circular dependencies
_social_bp = None

# ── Idempotency sentinel (2026-04-19) ──────────────────────────────
# init_social is expensive: registers ~18 blueprints, seeds DB tables,
# starts gossip/peer discovery, pulls autogen→openai→langchain via
# init_agent_engine.  Flask's register_blueprint raises on duplicate
# name, so a second call would crash the app.  Defend against double-
# init by tracking whether we've already run.  (Nunba's main.py has
# its own idempotency guard too; this is belt-and-suspenders for any
# downstream caller that imports this module and calls init_social
# again — tests, reloader, future cloud entry.)
_INIT_DONE = False


def get_social_blueprint():
    global _social_bp
    if _social_bp is None:
        from .api import social_bp as bp
        _social_bp = bp
    return _social_bp


def init_social(app):
    """Initialize the social network module. Call after app = Flask(...).

    This function is idempotent — calling it twice is safe.  The second
    call short-circuits and logs a debug message.  Rationale: this call
    registers 18+ blueprints, and Flask rejects duplicate blueprint
    names; skipping the second call is less surprising than raising."""
    global _INIT_DONE
    if _INIT_DONE:
        logger.debug("init_social: already initialized, skipping second call")
        return
    _INIT_DONE = True
    # Block dev mode on central
    import os as _os_boot
    node_tier = _os_boot.environ.get('HEVOLVE_NODE_TIER', 'flat')
    if node_tier == 'central' and _os_boot.environ.get('HEVOLVE_DEV_MODE', '').lower() == 'true':
        _os_boot.environ['HEVOLVE_DEV_MODE'] = 'false'
        logger.critical("SECURITY: Dev mode FORCED OFF on central instance")

    from .models import init_db, DB_PATH
    try:
        init_db()
        # Apply pending schema migrations against the canonical DB so a
        # daemon booting on a pre-existing v36/37/38 SQLite catches up
        # to SCHEMA_VERSION (currently 39: users.voice_profile column).
        # Idempotent — see tests/unit/test_voice_profile_migration.py.
        # Brings HARTOS daemon parity with Nunba main.py per MEMORY.md.
        from .migrations import run_migrations
        run_migrations()
        logger.info(f"HevolveSocial database initialized + migrated ({DB_PATH})")
    except Exception as e:
        logger.warning(f"HevolveSocial DB init failed (non-fatal): {e}")

    # Phase 7a / Phase 8 — install global tenant filter on Session.
    # Migration v40 added `tenant_id` columns to ~34 social tables;
    # this teaches the ORM about them and filters every SELECT by
    # the current request's tenant. No-op when g.tenant_id is None
    # (flat/regional pass-through), so single-tenant deploys are
    # unaffected. Plan reference: sunny-gliding-eich.md, Part E.1.
    try:
        from .tenant_filter import (
            install_tenant_filter, register_tenant_aware,
        )
        install_tenant_filter()
        from .models import (
            User, Post, Comment, Community, Vote, Follow,
            CommunityMembership, Notification, Report,
        )
        for _cls in (User, Post, Comment, Community, Vote, Follow,
                     CommunityMembership, Notification, Report):
            register_tenant_aware(_cls)
    except Exception as e:
        logger.warning(f"HevolveSocial tenant filter install skipped: {e}")

    # Seed default achievements
    try:
        from .gamification_service import GamificationService
        from .models import get_db
        db = get_db()
        try:
            count = GamificationService.seed_achievements(db)
            if count > 0:
                db.commit()
                logger.info(f"HevolveSocial: seeded {count} achievements")
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"HevolveSocial achievement seeding skipped: {e}")

    # Seed default ad placements
    try:
        from .ad_service import AdService
        from .models import get_db as _get_db
        db = _get_db()
        try:
            count = AdService.seed_placements(db)
            if count > 0:
                db.commit()
                logger.info(f"HevolveSocial: seeded {count} ad placements")
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"HevolveSocial ad placement seeding skipped: {e}")

    # Register conversations blueprint (Phase 7c.3 — DM/group chat).
    # Distinct from api_channels (legacy 31-channel external adapter)
    # — these blueprints have non-overlapping URL prefixes.
    try:
        from .api_conversations import conversations_bp
        app.register_blueprint(conversations_bp)
        logger.info("HevolveSocial conversations registered at "
                    "/api/social/conversations/")
    except Exception as e:
        logger.warning(f"HevolveSocial conversations blueprint skipped: {e}")

    # Register calls blueprint (Phase 7d — voice/video/screen).
    # Flag-gated by `calls_v1` server-side; off → 503 on every endpoint.
    # The blueprint's URL prefix is /api/social so the routes live at
    # /api/social/calls/* (no nesting under /conversations).
    try:
        from .api_calls import calls_bp
        app.register_blueprint(calls_bp)
        logger.info("HevolveSocial calls registered at /api/social/calls/")
    except Exception as e:
        logger.warning(f"HevolveSocial calls blueprint skipped: {e}")

    # Register gamification blueprint
    try:
        from .api_gamification import gamification_bp
        app.register_blueprint(gamification_bp)
        logger.info("HevolveSocial gamification endpoints registered")
    except Exception as e:
        logger.warning(f"HevolveSocial gamification blueprint skipped: {e}")

    # Register hive contest blueprint (Claude Code onramp + leaderboard)
    try:
        from .api_hive_contest import hive_contest_bp
        app.register_blueprint(hive_contest_bp)
        logger.info("HevolveSocial hive contest registered at /api/hive/contest/")
    except Exception as e:
        logger.warning(f"HevolveSocial hive contest blueprint skipped: {e}")

    # Register compute-earnings blueprint (idle-compute self-advertise +
    # live drawer SSE + opted-in user wallet readback)
    try:
        from .api_compute_earnings import compute_earnings_bp
        app.register_blueprint(compute_earnings_bp)
        logger.info("HevolveSocial compute earnings registered at /api/compute/earnings/")
    except Exception as e:
        logger.warning(f"HevolveSocial compute earnings blueprint skipped: {e}")

    # Register hive contest UI (single-page local view — reads the same
    # /api/hive/contest/* endpoints, used by the HART shell panel and
    # by a direct browser visit for operators).
    try:
        from .ui_hive_contest import hive_contest_ui_bp
        app.register_blueprint(hive_contest_ui_bp)
        logger.info("HevolveSocial hive contest UI registered at /hive-contest")
    except Exception as e:
        logger.warning(f"HevolveSocial hive contest UI blueprint skipped: {e}")

    # Register MCP tool registry blueprint (servers, tools, discover)
    try:
        from .api_mcp import mcp_bp
        app.register_blueprint(mcp_bp)
        logger.info("HevolveSocial MCP endpoints registered at /api/social/mcp/")
    except Exception as e:
        logger.warning(f"HevolveSocial marketplace+MCP blueprint skipped: {e}")

    # Register sharing blueprint (short URLs, OG metadata, consent-gated links)
    try:
        from .api_sharing import sharing_bp
        app.register_blueprint(sharing_bp)
        logger.info("HevolveSocial sharing endpoints registered at /api/social/share/")
    except Exception as e:
        logger.warning(f"HevolveSocial sharing blueprint skipped: {e}")

    # Register multiplayer games + compute lending blueprint
    try:
        from .api_games import games_bp
        app.register_blueprint(games_bp)
        logger.info("HevolveSocial games + compute endpoints registered at /api/social/games/, /api/social/compute/")
    except Exception as e:
        logger.warning(f"HevolveSocial games blueprint skipped: {e}")

    # Register discovery blueprint (.well-known/hevolve-social.json)
    try:
        from .discovery import discovery_bp
        app.register_blueprint(discovery_bp)
        logger.info("HevolveSocial discovery endpoint registered at /.well-known/hevolve-social.json")
    except Exception as e:
        logger.debug(f"HevolveSocial discovery blueprint skipped: {e}")

    # Register admin API blueprint (channels management, requires admin auth)
    try:
        from integrations.channels.admin.api import admin_bp
        app.register_blueprint(admin_bp)
        logger.info("HevolveSocial admin API registered at /api/admin")
    except Exception as e:
        logger.debug(f"HevolveSocial admin blueprint skipped: {e}")

    # Register user-facing channel bindings API (catalog, bindings, pairing, presence)
    try:
        from .api_channels import channel_user_bp
        app.register_blueprint(channel_user_bp)
        logger.info("HevolveSocial channel user API registered at /api/social/channels/")
    except Exception as e:
        logger.debug(f"HevolveSocial channel user blueprint skipped: {e}")

    # Register agent dashboard blueprint (truth-grounded unified agent view)
    try:
        from .api_dashboard import dashboard_bp
        app.register_blueprint(dashboard_bp)
        logger.info("HevolveSocial dashboard registered at /api/social/dashboard/")
    except Exception as e:
        logger.debug(f"HevolveSocial dashboard blueprint skipped: {e}")

    # Register thought experiment tracker blueprint
    try:
        from .api_tracker import tracker_bp
        app.register_blueprint(tracker_bp)
        logger.info("HevolveSocial tracker registered at /api/social/tracker/")
    except Exception as e:
        logger.debug(f"HevolveSocial tracker blueprint skipped: {e}")

    # Register fleet OTA update approval blueprint
    try:
        from .api_fleet_update import fleet_update_bp
        app.register_blueprint(fleet_update_bp)
        logger.info("HevolveSocial fleet update registered at /api/social/fleet/")
    except Exception as e:
        logger.debug(f"HevolveSocial fleet update blueprint skipped: {e}")

    # Register regional host request + approval blueprint
    try:
        from .api_regional_host import regional_host_bp
        app.register_blueprint(regional_host_bp)
        logger.info("HevolveSocial regional host registered at /api/social/regional-host/")
    except Exception as e:
        logger.debug(f"HevolveSocial regional host blueprint skipped: {e}")

    # Register sync & backup blueprint
    try:
        from .sync_api import sync_bp
        app.register_blueprint(sync_bp)
        logger.info("HevolveSocial sync registered at /api/social/sync/")
    except Exception as e:
        logger.debug(f"HevolveSocial sync blueprint skipped: {e}")

    # Register audit trail blueprint
    try:
        from .api_audit import audit_bp
        app.register_blueprint(audit_bp)
        logger.info("HevolveSocial audit registered at /api/social/audit/")
    except Exception as e:
        logger.debug(f"HevolveSocial audit blueprint skipped: {e}")

    # Register content generation task tracking blueprint
    try:
        from integrations.agent_engine.api_content_gen import content_gen_bp
        app.register_blueprint(content_gen_bp)
        logger.info("HevolveSocial content gen registered at /api/social/content-gen/")
    except Exception as e:
        logger.debug(f"HevolveSocial content gen blueprint skipped: {e}")

    # Register continual learning CCT management blueprint
    try:
        from integrations.agent_engine.api_learning import learning_bp
        app.register_blueprint(learning_bp)
        logger.info("Learning CCT endpoints registered at /api/learning/")
    except Exception as e:
        logger.debug(f"Learning blueprint skipped: {e}")

    # Register OS-wide theme management blueprint
    try:
        from .api_theme import theme_bp
        app.register_blueprint(theme_bp)
        logger.info("HevolveSocial theme registered at /api/social/theme/")
    except Exception as e:
        logger.debug(f"HevolveSocial theme blueprint skipped: {e}")

    # Register thought experiments blueprint
    try:
        from .api_thought_experiments import thought_experiments_bp
        app.register_blueprint(thought_experiments_bp)
        logger.info("Thought experiment endpoints registered at /api/social/experiments/")
    except Exception as e:
        logger.debug(f"Thought experiments blueprint skipped: {e}")

    # Register encounter (P2P BLE meetup + mutual-like icebreaker) blueprint.
    # PR-A alpha skeleton — in-memory state; DB migration v38 lands
    # in PR-A beta.  Full design: project_encounter_icebreaker.md.
    try:
        from .encounter_api import encounter_bp
        app.register_blueprint(encounter_bp)
        logger.info("HevolveSocial encounter registered at /api/social/encounter/")
    except Exception as e:
        logger.debug(f"HevolveSocial encounter blueprint skipped: {e}")

    # Register UserConsent UI blueprint (W0c F3 prereq).  JWT-authed,
    # append-only consent surface — the SINGLE HTTP write path for
    # the user_consents table after the consent-surface consolidation
    # (orchestrator review acd11f55, 2026-04-25).  consent_service.py
    # retains the in-process ConsentService static-method API for
    # internal callers; its legacy /api/consent/<user_id>/* HTTP
    # route family was removed in the consolidation commit.
    try:
        from .consent_api import consent_bp
        app.register_blueprint(consent_bp)
        logger.info("HevolveSocial consent registered at /api/social/consent/")
    except Exception as e:
        logger.debug(f"HevolveSocial consent blueprint skipped: {e}")

    # NOTE: compute_pledge_bp (api_compute_pledge.py) was consolidated into tracker_bp
    # and the stale file deleted 2026-04-15. All pledge endpoints live at
    # /api/social/tracker/experiments/*/pledge* and /api/social/tracker/pledges/*
    # — single source of truth.

    # Initialize node keypair for integrity verification
    try:
        from security.node_integrity import get_or_create_keypair, get_public_key_hex
        get_or_create_keypair()
        pubkey = get_public_key_hex()
        logger.info(f"HevolveSocial node keypair initialized: {pubkey[:16]}...")
    except Exception as e:
        logger.debug(f"HevolveSocial keypair init skipped: {e}")

    # ── Master Key Boot Verification ──
    _boot_verified = False
    _boot_manifest = None
    try:
        from security.master_key import full_boot_verification, is_dev_mode, get_enforcement_mode
        verification = full_boot_verification()
        enforcement = get_enforcement_mode()
        _boot_verified = verification['passed'] or is_dev_mode() or enforcement in ('off', 'warn')
        _boot_manifest = verification.get('manifest')
        if verification['passed']:
            logger.info(f"HevolveSocial boot verification PASSED: {verification['details']}")
        elif _boot_verified:
            logger.warning(f"HevolveSocial boot verification not passed but allowed "
                          f"(enforcement={enforcement}): {verification['details']}")
        else:
            logger.critical(f"HevolveSocial boot verification FAILED: {verification['details']}")
    except Exception as e:
        _boot_verified = True  # Allow if master_key module unavailable
        logger.debug(f"HevolveSocial boot verification skipped: {e}")

    # ── Tier authorization (central must prove master key) ──
    try:
        from security.key_delegation import verify_tier_authorization
        tier_auth = verify_tier_authorization()
        if not tier_auth.get('authorized'):
            logger.critical(f"Tier authorization FAILED: {tier_auth.get('details', 'unknown')}")
            if node_tier == 'central':
                _boot_verified = False
                logger.critical("Central node cannot start without tier authorization")
    except Exception as e:
        logger.warning(f"Tier authorization check unavailable: {e}")

    # ── System Requirements (HART OS Equilibrium) ──
    # Detect hardware, classify contribution tier, auto-gate features.
    # Must run BEFORE gossip/agents so env vars are set before they check.
    _node_capabilities = None
    try:
        from security.system_requirements import run_system_check, get_tier_name
        _node_capabilities = run_system_check()
        _tier_name = get_tier_name()
        logger.info(
            f"HevolveSocial equilibrium: tier={_tier_name}, "
            f"enabled={len(_node_capabilities.enabled_features)} features, "
            f"disabled={len(_node_capabilities.disabled_features)} features"
        )
        if _node_capabilities.disabled_features:
            for _feat, _reason in _node_capabilities.disabled_features.items():
                logger.info(f"  Feature '{_feat}' not loaded: {_reason}")
    except Exception as e:
        logger.warning(f"HevolveSocial system requirements check skipped: {e}")
        # Auto-enable agent engine if not explicitly disabled
        import os as _os_fb
        if _os_fb.environ.get('HEVOLVE_AGENT_ENGINE_ENABLED') is None:
            _os_fb.environ['HEVOLVE_AGENT_ENGINE_ENABLED'] = 'true'

    # Start decentralized gossip peer discovery (background thread)
    if _boot_verified:
        try:
            from .peer_discovery import gossip
            gossip.start()
            logger.info(f"HevolveSocial gossip started: node={gossip.node_id[:8]}, "
                        f"seeds={len(gossip.seed_peers)}")
        except Exception as e:
            logger.debug(f"HevolveSocial gossip start skipped: {e}")

        # Start zero-config LAN auto-discovery (additive to seed peers)
        import os as _os_disc
        if _os_disc.environ.get('HEVOLVE_AUTO_DISCOVERY', 'true').lower() != 'false':
            try:
                from .peer_discovery import auto_discovery
                auto_discovery.start()
                logger.info(f"HevolveSocial auto-discovery started "
                            f"(UDP port {auto_discovery._port})")
            except Exception as e:
                logger.debug(f"HevolveSocial auto-discovery skipped: {e}")

        # Start runtime integrity monitor if we have a signed manifest
        if _boot_manifest:
            try:
                from security.runtime_monitor import start_monitor
                start_monitor(_boot_manifest)
                logger.info("HevolveSocial runtime integrity monitor started")
            except Exception as e:
                logger.debug(f"HevolveSocial runtime monitor start skipped: {e}")
    else:
        logger.critical("HevolveSocial: gossip NOT started - boot verification failed (hard mode)")

    # Start sync engine for regional/local tiers
    if _boot_verified:
        try:
            from security.key_delegation import get_node_tier
            node_tier = get_node_tier()
            if node_tier in ('regional', 'local'):
                from .sync_engine import sync_engine
                sync_engine.start_background_sync()
                logger.info(f"HevolveSocial sync engine started (tier={node_tier})")
        except Exception as e:
            logger.debug(f"HevolveSocial sync engine start skipped: {e}")

    # Start distributed coding agent if enabled
    import os as _os2
    if _os2.environ.get('HEVOLVE_CODING_AGENT_ENABLED', 'false').lower() == 'true':
        try:
            from integrations.coding_agent import init_coding_agent
            init_coding_agent(app)
            logger.info("HevolveSocial distributed coding agent initialized")
        except Exception as e:
            logger.debug(f"HevolveSocial coding agent init skipped: {e}")

    # Agent engine init is the SINGLE responsibility of Nunba's
    # `main.py:_deferred_social_init` (or any standalone HARTOS launcher
    # that calls `init_agent_engine` directly).  Calling it from inside
    # `init_social` produced a double-invocation in Nunba (main.py calls
    # init_social → init_social calls init_agent_engine → main.py calls
    # init_agent_engine again right after), and the deadlock smoking
    # gun on 2026-04-28 was exactly this path: the second invocation
    # fired while hartos-init's `from hart_intelligence import app` was
    # still mid-import, deadlocking on the per-module import lock.
    # Idempotency in init_agent_engine itself (added 2026-04-28) makes
    # the second call a no-op, but the cleaner contract is: ONE caller,
    # ONE call site.  init_social no longer initialises the agent engine
    # — its caller does.
    logger.debug("HevolveSocial: agent engine init delegated to caller "
                 "(see Nunba main.py:_deferred_social_init or HARTOS launcher)")

    # Register with central registry if configured
    import os
    registry_url = os.environ.get('HEVOLVE_REGISTRY_URL', '')
    if registry_url and _boot_verified:
        try:
            from .integrity_service import IntegrityService
            from .peer_discovery import gossip as _gossip
            from security.node_integrity import get_public_key_hex as _get_pubkey
            IntegrityService.register_with_registry(
                registry_url, _gossip.node_id, _get_pubkey(), _gossip.version)
            logger.info(f"HevolveSocial registered with registry: {registry_url}")
        except Exception as e:
            logger.debug(f"HevolveSocial registry registration skipped: {e}")

    # ── NodeWatchdog - start LAST, monitors all daemon threads ──
    try:
        from security.node_watchdog import start_watchdog
        watchdog = start_watchdog()

        # Register gossip — interval must exceed worst-case gossip round
        # (3 peers × 10s timeout = 30s max) to avoid false FROZEN alerts.
        if _boot_verified:
            try:
                from .peer_discovery import gossip as _g
                if _g._running:
                    watchdog.register('gossip', expected_interval=120,
                                      restart_fn=_g.start, stop_fn=_g.stop)
            except Exception:
                pass

        # Register auto-discovery
        try:
            from .peer_discovery import auto_discovery as _ad
            if _ad._running:
                watchdog.register('auto_discovery',
                                  expected_interval=_ad._beacon_interval,
                                  restart_fn=_ad.start, stop_fn=_ad.stop)
        except Exception:
            pass

        # Register runtime monitor
        if _boot_manifest:
            try:
                from security.runtime_monitor import get_monitor
                mon = get_monitor()
                if mon and mon._running:
                    watchdog.register('runtime_monitor',
                                      expected_interval=mon._check_interval,
                                      restart_fn=mon.start, stop_fn=mon.stop)
            except Exception:
                pass

        # Register sync engine
        try:
            from .sync_engine import sync_engine as _se
            if _se._running:
                watchdog.register('sync_engine',
                                  expected_interval=_se._interval,
                                  restart_fn=_se.start_background_sync,
                                  stop_fn=_se.stop_background_sync)
        except Exception:
            pass

        # Register agent daemon
        try:
            from integrations.agent_engine.agent_daemon import agent_daemon as _agent_d
            if _agent_d._running:
                watchdog.register('agent_daemon',
                                  expected_interval=_agent_d._interval,
                                  restart_fn=_agent_d.start, stop_fn=_agent_d.stop)
        except Exception:
            pass

        # Register coding daemon
        try:
            from integrations.coding_agent.coding_daemon import coding_daemon as _coding_d
            if _coding_d._running:
                watchdog.register('coding_daemon',
                                  expected_interval=_coding_d._interval,
                                  restart_fn=_coding_d.start, stop_fn=_coding_d.stop)
        except Exception:
            pass

        # Register model lifecycle manager — interval must exceed worst-case
        # tick duration (nvidia-smi 5s + pressure checks + federation report).
        try:
            from integrations.service_tools.model_lifecycle import get_model_lifecycle_manager
            _lifecycle = get_model_lifecycle_manager()
            _lifecycle.start()
            if _lifecycle._running:
                watchdog.register('model_lifecycle',
                                  expected_interval=max(_lifecycle._interval * 3, 60),
                                  restart_fn=_lifecycle.start,
                                  stop_fn=_lifecycle.stop)
                logger.info("Model lifecycle manager started")
        except Exception as e:
            logger.debug(f"Model lifecycle manager start skipped: {e}")

        # Distributed worker loop — claims tasks from shared Redis queue.
        # Self-gates: start() is a no-op when Redis is unreachable.
        try:
            from integrations.distributed_agent.worker_loop import worker_loop as _wl
            _wl.start()
            if _wl._running:
                watchdog.register('distributed_worker',
                                  expected_interval=_wl._interval * 4,
                                  restart_fn=_wl.start,
                                  stop_fn=_wl.stop)
                logger.info("Distributed worker loop started")
        except Exception as e:
            logger.debug(f"Distributed worker loop start skipped: {e}")

        # Hive benchmark prover — rotates model benchmarks every 6 hours
        # and publishes baseline+score deltas into the ledger.
        try:
            from integrations.agent_engine.hive_benchmark_prover import (
                get_benchmark_prover, _LOOP_INTERVAL_SECONDS as _hbp_interval,
            )
            _hbp = get_benchmark_prover()
            _hbp.start_continuous_loop()
            if _hbp._loop_running:
                watchdog.register('hive_benchmark_prover',
                                  expected_interval=_hbp_interval * 2,
                                  restart_fn=_hbp.start_continuous_loop,
                                  stop_fn=_hbp.stop)
                logger.info("Hive benchmark prover started")
        except Exception as e:
            logger.debug(f"Hive benchmark prover start skipped: {e}")

        watchdog.start()
        logger.info(f"NodeWatchdog started: monitoring "
                    f"{len(watchdog._threads)} threads")
    except Exception as e:
        logger.debug(f"NodeWatchdog start skipped: {e}")

    # Sync trained agents as social users on first request
    @app.before_request
    def _sync_agents_once():
        if not getattr(app, '_social_agents_synced', False):
            app._social_agents_synced = True
            try:
                from .agent_bridge import sync_trained_agents
                count = sync_trained_agents()
                if count > 0:
                    logger.info(f"HevolveSocial: synced {count} trained agents as social users")
            except Exception as e:
                logger.debug(f"HevolveSocial agent sync skipped: {e}")


# Module-level lazy attribute: from integrations.social import social_bp
def __getattr__(name):
    if name == 'social_bp':
        return get_social_blueprint()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
