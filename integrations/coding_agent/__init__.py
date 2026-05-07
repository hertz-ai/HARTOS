"""
HevolveSocial - Distributed Coding Agent

Orchestrates idle agents across the 3-tier hierarchy to collaboratively
code in a target repository towards a common goal. Uses the existing
CREATE/REUSE agent pipeline for all LLM work.

ALSO drains the self_heal goal queue produced by error_advice +
SelfHealingDispatcher — these are NOT optional collaborative work,
they are the system's autonomous-fix loop for production failures.
Per the 2026-05-04 audit (15 stale self_heal goals back to
2026-04-27, zero completed), the queue piles up indefinitely when
the daemon isn't running.

Enabled via HEVOLVE_CODING_AGENT_ENABLED (default: TRUE — flipped
2026-05-07 since the daemon is the consumer for self_heal goals
that error_advice + #102 producers fill).  Safety: the daemon's
_tick() at coding_daemon.py:129-131 early-returns when
IdleDetectionService.get_idle_opted_in_agents is empty, so users
with no opted-in agents see zero side effects.  Budget gate at
line 110-117 blocks dispatches if platform isn't affordable.
Server deployments that explicitly don't want the daemon set
HEVOLVE_CODING_AGENT_ENABLED=false.
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
    if os.environ.get('HEVOLVE_CODING_AGENT_ENABLED', 'true').lower() != 'true':
        logger.info("Distributed coding agent disabled (HEVOLVE_CODING_AGENT_ENABLED=false explicitly set)")
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
