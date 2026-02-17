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


def init_agent_engine(app):
    """Initialize the unified agent goal engine."""
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

    # Bootstrap "Hyve Platform" product for self-marketing (idempotent)
    product_id = None
    try:
        from integrations.social.models import get_db, Product, User
        db = get_db()
        existing = db.query(Product).filter_by(is_platform_product=True).first()
        if not existing:
            product = Product(
                name='Hyve Platform',
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
            logger.info("Bootstrapped Hyve Platform product for self-marketing")
        else:
            product_id = str(existing.id)

        # Bootstrap system agent for goal execution (idempotent)
        sys_agent = db.query(User).filter_by(username='hevolve_system_agent').first()
        if not sys_agent:
            sys_agent = User(
                username='hevolve_system_agent',
                display_name='Hyve System Agent',
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
