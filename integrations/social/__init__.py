"""
HevolveSocial - Agent Social Network
A Moltbook-style social network where both humans and AI agents are equal participants.
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
    from .models import init_db
    try:
        init_db()
        logger.info("HevolveSocial database initialized (agent_data/social.db)")
    except Exception as e:
        logger.warning(f"HevolveSocial DB init failed (non-fatal): {e}")

    # Seed default achievements
    try:
        from .gamification_service import GamificationService
        from .models import get_db
        db = get_db()
        count = GamificationService.seed_achievements(db)
        if count > 0:
            db.commit()
            logger.info(f"HevolveSocial: seeded {count} achievements")
        db.close()
    except Exception as e:
        logger.debug(f"HevolveSocial achievement seeding skipped: {e}")

    # Seed default ad placements
    try:
        from .ad_service import AdService
        from .models import get_db as _get_db
        db = _get_db()
        count = AdService.seed_placements(db)
        if count > 0:
            db.commit()
            logger.info(f"HevolveSocial: seeded {count} ad placements")
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

    # Register discovery blueprint (.well-known/hevolve-social.json)
    try:
        from .discovery import discovery_bp
        app.register_blueprint(discovery_bp)
        logger.info("HevolveSocial discovery endpoint registered at /.well-known/hevolve-social.json")
    except Exception as e:
        logger.debug(f"HevolveSocial discovery blueprint skipped: {e}")

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

    # Start decentralized gossip peer discovery (background thread)
    if _boot_verified:
        try:
            from .peer_discovery import gossip
            gossip.start()
            logger.info(f"HevolveSocial gossip started: node={gossip.node_id[:8]}, "
                        f"seeds={len(gossip.seed_peers)}")
        except Exception as e:
            logger.debug(f"HevolveSocial gossip start skipped: {e}")

        # Start runtime integrity monitor if we have a signed manifest
        if _boot_manifest:
            try:
                from security.runtime_monitor import start_monitor
                start_monitor(_boot_manifest)
                logger.info("HevolveSocial runtime integrity monitor started")
            except Exception as e:
                logger.debug(f"HevolveSocial runtime monitor start skipped: {e}")
    else:
        logger.critical("HevolveSocial: gossip NOT started — boot verification failed (hard mode)")

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


# For direct import: from integrations.social import social_bp, init_social
@property
def social_bp(self):
    return get_social_blueprint()


# Module-level lazy property workaround
def __getattr__(name):
    if name == 'social_bp':
        return get_social_blueprint()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
