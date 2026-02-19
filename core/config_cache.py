"""
Cached configuration loader.

Replaces repeated `open("config.json")` calls across helper.py, create_recipe.py,
reuse_recipe.py, and langchain_gpt_api.py with a single cached load.

Before: config.json read 3+ times at module import (once per file).
After:  config.json read exactly once, cached in memory.
"""

import json
import os
import logging
import threading

logger = logging.getLogger('hevolve_core')

_config = None
_config_lock = threading.Lock()


def get_config() -> dict:
    """
    Load config.json once and cache it.
    Thread-safe singleton pattern.
    """
    global _config
    if _config is not None:
        return _config

    with _config_lock:
        # Double-check after acquiring lock
        if _config is not None:
            return _config

        # Try encrypted vault first (security module)
        try:
            from security.secrets_manager import SecretsManager
            mgr = SecretsManager()
            # If secrets manager has been migrated, use it
            if mgr._secrets:
                _config = dict(mgr._secrets)
                logger.info("Config loaded from encrypted vault")
                return _config
        except Exception:
            pass

        # Fall back to config.json (standalone) or langchain_config.json (bundled)
        _base_dir = os.path.dirname(os.path.dirname(__file__))
        # In frozen/bundled mode, config lives next to the exe as langchain_config.json
        if getattr(__import__('sys'), 'frozen', False):
            _base_dir = os.path.dirname(__import__('sys').executable)
        _config_candidates = [
            os.path.join(_base_dir, 'config.json'),
            os.path.join(_base_dir, 'langchain_config.json'),
        ]
        for _cp in _config_candidates:
            try:
                with open(_cp, 'r') as f:
                    _config = json.load(f)
                logger.info(f"Config loaded from {os.path.basename(_cp)}")
                return _config
            except FileNotFoundError:
                continue
        logger.warning("config.json not found, using environment variables only")
        _config = {}

        return _config


def get_secret(name: str, default: str = '') -> str:
    """
    Get a configuration value by name.
    Checks environment variable first, then cached config.
    """
    # Env vars take precedence
    env_val = os.environ.get(name)
    if env_val:
        return env_val

    config = get_config()
    return config.get(name, default)


def reload_config():
    """Force reload of configuration (for testing or after migration)."""
    global _config
    with _config_lock:
        _config = None
    return get_config()
