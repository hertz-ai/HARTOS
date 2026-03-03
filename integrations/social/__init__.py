"""
HevolveSocial - Agent Social Network
A community-driven social network where both humans and AI agents are equal participants.
"""
import logging

logger = logging.getLogger('hevolve_social')

# Lazy imports to avoid circular dependencies
_social_bp = None


def get_social_blueprint():
    global _social_bp
    if _social_bp is None:
        from .api import social_bp as bp
        _social_bp = bp
    return _social_bp


def init_social(app):
    """Initialize the social network module. Call after app = Flask(...)."""
    # Block dev mode on central
    import os as _os_boot
    node_tier = _os_boot.environ.get('HEVOLVE_NODE_TIER', 'flat')
    if node_tier == 'central' and _os_boot.environ.get('HEVOLVE_DEV_MODE', '').lower() == 'true':
        _os_boot.environ['HEVOLVE_DEV_MODE'] = 'false'
        logger.critical("SECURITY: Dev mode FORCED OFF on central instance")

    from .models import init_db, DB_PATH
    try:
        init_db()
        logger.info(f"HevolveSocial database initialized ({DB_PATH})")
    except Exception as e:
        logger.warning(f"HevolveSocial DB init failed (non-fatal): {e}")

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

    # Register gamification blueprint
    try:
        from .api_gamification import gamification_bp
        app.register_blueprint(gamification_bp)
        logger.info("HevolveSocial gamification endpoints registered")
    except Exception as e:
        logger.warning(f"HevolveSocial gamification blueprint skipped: {e}")

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

    # Start unified agent engine (marketing, coding goals via unified daemon)
    try:
        from integrations.agent_engine import init_agent_engine
        init_agent_engine(app)
    except Exception as e:
        logger.warning(f"HevolveSocial agent engine init skipped: {e}")

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

        # Register gossip
        if _boot_verified:
            try:
                from .peer_discovery import gossip as _g
                if _g._running:
                    watchdog.register('gossip', expected_interval=10,
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

        # Register model lifecycle manager
        try:
            from integrations.service_tools.model_lifecycle import get_model_lifecycle_manager
            _lifecycle = get_model_lifecycle_manager()
            _lifecycle.start()
            if _lifecycle._running:
                watchdog.register('model_lifecycle',
                                  expected_interval=_lifecycle._interval,
                                  restart_fn=_lifecycle.start,
                                  stop_fn=_lifecycle.stop)
                logger.info("Model lifecycle manager started")
        except Exception as e:
            logger.debug(f"Model lifecycle manager start skipped: {e}")

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
