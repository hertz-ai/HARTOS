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
        logger.debug("Agent engine disabled (HEVOLVE_AGENT_ENGINE_ENABLED != true)")
        return

    # Register API blueprint
    try:
        bp = get_engine_blueprint()
        app.register_blueprint(bp)
        logger.info("Agent engine endpoints registered")
    except Exception as e:
        logger.warning(f"Agent engine blueprint registration failed: {e}")
        return

    # Bootstrap "Hevolve Platform" product for self-marketing (idempotent)
    product_id = None
    try:
        from integrations.social.models import get_db, Product, User
        db = get_db()
        existing = db.query(Product).filter_by(is_platform_product=True).first()
        if not existing:
            product = Product(
                name='Hevolve Platform',
                description='AI-powered multi-agent platform for autonomous task execution',
                tagline='Your AI workforce, automated',
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
            logger.info("Bootstrapped Hevolve Platform product for self-marketing")
        else:
            product_id = str(existing.id)

        # Bootstrap system agent for goal execution (idempotent)
        sys_agent = db.query(User).filter_by(username='hevolve_system_agent').first()
        if not sys_agent:
            sys_agent = User(
                username='hevolve_system_agent',
                display_name='Hevolve System Agent',
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

    # Start background daemon
    try:
        from .agent_daemon import agent_daemon
        agent_daemon.start()
        logger.info("Agent engine daemon started")
    except Exception as e:
        logger.debug(f"Agent engine daemon start skipped: {e}")
