"""
Platform Boot Service — Independent platform substrate initialization.

Runs independently of both LiquidUI (desktop) and langchain_gpt_api (agent
backend). Both servers call `ensure_platform()` at startup; the first call
bootstraps, subsequent calls are no-ops.

Can also run standalone:
    python -m core.platform.boot_service          # foreground
    python -m core.platform.boot_service --daemon  # background

Architecture:
    ┌──────────────────────────────┐
    │   Platform Boot Service      │  ← owns bootstrap
    │   EventBus, AppRegistry,     │
    │   Extensions, CapabilityRouter│
    ├──────────────────────────────┤
    │ LiquidUI        │ Agent API  │  ← both consume via ensure_platform()
    │ (desktop shell)  │ (port 6777)│
    └──────────────────────────────┘
"""

import logging
import threading

logger = logging.getLogger('hevolve.platform.boot')

_boot_lock = threading.Lock()
_booted = False


def ensure_platform(extensions_dir=None):
    """Ensure the platform substrate is bootstrapped. Idempotent.

    Called by both LiquidUI and langchain_gpt_api at startup.
    First call bootstraps; subsequent calls return immediately.

    Returns:
        The global ServiceRegistry, or None on failure.
    """
    global _booted
    if _booted:
        return _get_registry_safe()

    with _boot_lock:
        # Double-check after acquiring lock
        if _booted:
            return _get_registry_safe()

        try:
            from core.platform.bootstrap import bootstrap_platform
            registry = bootstrap_platform(extensions_dir)
            _booted = True
            logger.info("Platform substrate ready")
            return registry
        except Exception as e:
            logger.error("Platform bootstrap failed: %s", e)
            return None


def is_booted():
    """Check if the platform substrate has been bootstrapped."""
    return _booted


def _get_registry_safe():
    """Get the global registry without re-bootstrapping."""
    try:
        from core.platform.registry import get_registry
        return get_registry()
    except Exception:
        return None


if __name__ == '__main__':
    import argparse
    import signal
    import sys
    import time

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s')

    parser = argparse.ArgumentParser(description='HART OS Platform Boot Service')
    parser.add_argument('--daemon', action='store_true',
                        help='Run as background daemon (keep alive)')
    parser.add_argument('--extensions-dir', default=None,
                        help='Path to extensions directory')
    args = parser.parse_args()

    registry = ensure_platform(args.extensions_dir)
    if registry is None:
        logger.error("Bootstrap failed — exiting")
        sys.exit(1)

    if args.daemon:
        logger.info("Boot service running (Ctrl+C to stop)")
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        stop.wait()
    else:
        logger.info("Bootstrap complete — exiting")
