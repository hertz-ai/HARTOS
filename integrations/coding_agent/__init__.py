"""
HevolveSocial - Distributed Coding Agent

Orchestrates idle agents across the 3-tier hierarchy to collaboratively
code in a target repository towards a common goal. Uses the existing
CREATE/REUSE agent pipeline for all LLM work.

Enabled via HEVOLVE_CODING_AGENT_ENABLED=true (default: false).
"""
import os
import logging

logger = logging.getLogger('hevolve_social')

_coding_bp = None


def get_coding_blueprint():
    global _coding_bp
    if _coding_bp is None:
        from .api import coding_agent_bp as bp
        _coding_bp = bp
    return _coding_bp


def init_coding_agent(app):
    """Initialize the distributed coding agent module."""
    if os.environ.get('HEVOLVE_CODING_AGENT_ENABLED', 'false').lower() != 'true':
        logger.debug("Distributed coding agent disabled (HEVOLVE_CODING_AGENT_ENABLED != true)")
        return

    # Register API blueprint
    try:
        bp = get_coding_blueprint()
        app.register_blueprint(bp)
        logger.info("Distributed coding agent endpoints registered")
    except Exception as e:
        logger.warning(f"Coding agent blueprint registration failed: {e}")
        return

    # Start background daemon
    try:
        from .coding_daemon import coding_daemon
        coding_daemon.start()
        logger.info("Distributed coding agent daemon started")
    except Exception as e:
        logger.debug(f"Coding agent daemon start skipped: {e}")
