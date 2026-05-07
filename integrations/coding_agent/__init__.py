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
_tick() early-returns when there are no idle agent personas
(IdleDetectionService.get_idle_agent_personas — the same canonical
gate agent_daemon uses for local goal dispatch; previously
get_idle_opted_in_agents, which silently returned [] on installs
where no human had opted into distributed compute → daemon stalled
with self_heal goals piling up.  Live-evidence 2026-05-07: 42
goals, 0 dispatched.  Same root-cause + fix as agent_daemon's
2026-05-01 switch).  Budget gate at line 110-117 blocks dispatches
if platform isn't affordable.  Server deployments that explicitly
don't want the daemon set HEVOLVE_CODING_AGENT_ENABLED=false.
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
