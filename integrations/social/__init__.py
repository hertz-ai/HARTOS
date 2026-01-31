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

    # Register discovery blueprint (.well-known/hevolve-social.json)
    try:
        from .discovery import discovery_bp
        app.register_blueprint(discovery_bp)
        logger.info("HevolveSocial discovery endpoint registered at /.well-known/hevolve-social.json")
    except Exception as e:
        logger.debug(f"HevolveSocial discovery blueprint skipped: {e}")

    # Start decentralized gossip peer discovery (background thread)
    try:
        from .peer_discovery import gossip
        gossip.start()
        logger.info(f"HevolveSocial gossip started: node={gossip.node_id[:8]}, "
                    f"seeds={len(gossip.seed_peers)}")
    except Exception as e:
        logger.debug(f"HevolveSocial gossip start skipped: {e}")

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
