"""
Config Client — Thin wrapper for PlatformConfig.

Usage:
    from hart_sdk import config

    value = config.get('theme.mode', 'dark')
    config.set('theme.mode', 'light')
    config.on_change('theme.mode', handle_theme_change)
"""

from typing import Any, Callable, Optional


class ConfigClient:
    """Singleton config client for HART OS PlatformConfig."""

    def _get_config(self):
        """Get PlatformConfig from ServiceRegistry."""
        try:
            from core.platform.registry import get_registry
            registry = get_registry()
            if registry.has('config'):
                return registry.get('config')
        except ImportError:
            pass
        return None

    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value.

        Falls back to default if config unavailable or key not found.
        """
        cfg = self._get_config()
        if cfg is None:
            return default
        try:
            return cfg.get(key, default)
        except Exception:
            return default

    def set(self, key: str, value: Any) -> bool:
        """Set a configuration value.

        Returns True if set, False if unavailable.
        """
        cfg = self._get_config()
        if cfg is None:
            return False
        try:
            cfg.set(key, value)
            return True
        except Exception:
            return False

    def on_change(self, key: str, callback: Callable) -> bool:
        """Subscribe to config changes for a key.

        Returns True if subscribed, False if unavailable.
        """
        cfg = self._get_config()
        if cfg is None:
            return False
        try:
            cfg.on_change(key, callback)
            return True
        except Exception:
            return False


# Singleton
config = ConfigClient()
