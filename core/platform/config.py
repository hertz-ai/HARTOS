"""
Platform Config — Unified 3-layer configuration resolution.

Resolves settings with precedence: environment variables > DB row > defaults.
Generalizes the compute_config.py pattern for use across all OS subsystems.

Design decisions:
- Namespace-based: each subsystem defines its own config class
- TTL cache (30s default) — same pattern as compute_config.py
- Change notifications: on_change(key, callback)
- Typed: each key has a converter (int, float, str, bool, json)
- Falls back gracefully when DB unavailable
- Thread-safe via threading.Lock

Usage:
    class DisplayConfig(PlatformConfig):
        _namespace = 'display'
        _defaults = {'scale': 1.0, 'brightness': 1.0}
        _env_map = {'scale': ('HART_DISPLAY_SCALE', float)}

    display = DisplayConfig()
    scale = display.get('scale')        # env > DB > default
    display.set('scale', 1.5)           # persists + notifies
    display.on_change('scale', fn)      # subscribe to changes
"""

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve.platform')


# ═══════════════════════════════════════════════════════════════
# Type Converters
# ═══════════════════════════════════════════════════════════════

def _convert_bool(val: Any) -> bool:
    """Convert string/int/bool to bool."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ('true', '1', 'yes', 'on')
    return bool(val)


def _convert_json(val: Any) -> Any:
    """Convert JSON string to Python object."""
    if isinstance(val, str):
        return json.loads(val)
    return val


CONVERTERS = {
    int: int,
    float: float,
    str: str,
    bool: _convert_bool,
    'json': _convert_json,
}


# ═══════════════════════════════════════════════════════════════
# Platform Config
# ═══════════════════════════════════════════════════════════════

class PlatformConfig:
    """3-layer config resolution: env vars > DB > defaults.

    Subclass and set _namespace, _defaults, _env_map to create
    a config namespace for any subsystem.

    Class attributes (set by subclasses):
        _namespace: str - Config namespace (e.g., 'display', 'audio')
        _defaults: dict - Default values for all keys
        _env_map: dict - Maps key → (ENV_VAR_NAME, converter)
        _cache_ttl: int - Cache TTL in seconds (default 30)
    """

    _namespace: str = ''
    _defaults: Dict[str, Any] = {}
    _env_map: Dict[str, Tuple[str, type]] = {}
    _cache_ttl: int = 30

    def __init__(self, db_loader: Optional[Callable] = None):
        """Initialize config.

        Args:
            db_loader: Optional callable(namespace, key) -> value or None.
                       Used for layer 2 (DB) resolution. If None, DB layer
                       is skipped (env + defaults only).
        """
        self._db_loader = db_loader
        self._db_saver: Optional[Callable] = None
        self._cache: Dict[str, Any] = {}
        self._cache_ts: float = 0.0
        self._overrides: Dict[str, Any] = {}  # in-memory set() values
        self._listeners: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()

    def set_db_saver(self, saver: Callable) -> None:
        """Set a callable(namespace, key, value) for persisting to DB."""
        self._db_saver = saver

    def get(self, key: str, default: Any = None) -> Any:
        """Resolve a config value. Precedence: env > override > DB > defaults.

        Args:
            key: Config key name.
            default: Fallback if key not found anywhere (overrides _defaults).

        Returns:
            Resolved value, type-converted if env_map specifies a converter.
        """
        # Layer 1: Environment variable (always highest priority)
        if key in self._env_map:
            env_name, converter = self._env_map[key]
            env_val = os.environ.get(env_name)
            if env_val is not None:
                try:
                    conv = CONVERTERS.get(converter, converter)
                    return conv(env_val)
                except (ValueError, TypeError, json.JSONDecodeError):
                    logger.warning("Bad env value for %s=%s", env_name, env_val)

        # Layer 2: In-memory override (from set())
        with self._lock:
            if key in self._overrides:
                return self._overrides[key]

        # Layer 3: DB (cached with TTL)
        db_val = self._get_from_db(key)
        if db_val is not None:
            return db_val

        # Layer 4: Defaults
        if key in self._defaults:
            return self._defaults[key]

        return default

    def get_all(self) -> Dict[str, Any]:
        """Return all config values (resolved)."""
        result = {}
        for key in self._defaults:
            result[key] = self.get(key)
        return result

    def set(self, key: str, value: Any) -> None:
        """Set a config value. Persists to DB if saver is configured.

        Also triggers change notifications for registered listeners.
        """
        old_val = self.get(key)

        with self._lock:
            self._overrides[key] = value

        # Persist to DB
        if self._db_saver:
            try:
                self._db_saver(self._namespace, key, value)
            except Exception as e:
                logger.warning("DB save failed for %s.%s: %s",
                               self._namespace, key, e)

        # Invalidate cache
        self._invalidate_cache()

        # Notify listeners
        if old_val != value:
            self._notify(key, old_val, value)

    def reset(self, key: str) -> None:
        """Reset a key to its default value."""
        with self._lock:
            self._overrides.pop(key, None)
        self._invalidate_cache()
        default_val = self._defaults.get(key)
        self._notify(key, None, default_val)

    def reset_all(self) -> None:
        """Reset all overrides."""
        with self._lock:
            self._overrides.clear()
        self._invalidate_cache()

    # ── Change Notifications ──────────────────────────────────

    def on_change(self, key: str, callback: Callable) -> None:
        """Subscribe to changes for a specific key.

        Callback receives (key, old_value, new_value).
        """
        with self._lock:
            if key not in self._listeners:
                self._listeners[key] = []
            self._listeners[key].append(callback)

    def off_change(self, key: str, callback: Callable) -> None:
        """Unsubscribe from changes."""
        with self._lock:
            if key in self._listeners:
                try:
                    self._listeners[key].remove(callback)
                except ValueError:
                    pass

    # ── Internal ──────────────────────────────────────────────

    def _get_from_db(self, key: str) -> Optional[Any]:
        """Load from DB with TTL cache."""
        if not self._db_loader:
            return None

        now = time.time()
        with self._lock:
            if now - self._cache_ts < self._cache_ttl and key in self._cache:
                return self._cache[key]

        try:
            val = self._db_loader(self._namespace, key)
            if val is not None:
                with self._lock:
                    self._cache[key] = val
                    self._cache_ts = now
                return val
        except Exception as e:
            logger.debug("DB load failed for %s.%s: %s",
                         self._namespace, key, e)
        return None

    def _invalidate_cache(self) -> None:
        """Clear the TTL cache."""
        with self._lock:
            self._cache.clear()
            self._cache_ts = 0.0

    def _notify(self, key: str, old_val: Any, new_val: Any) -> None:
        """Dispatch change notifications to listeners."""
        with self._lock:
            listeners = list(self._listeners.get(key, []))
        for cb in listeners:
            try:
                cb(key, old_val, new_val)
            except Exception as e:
                logger.warning("Config listener error for %s.%s: %s",
                               self._namespace, key, e)

    # ── Settings Export / Import (for sync) ─────────────────

    def export_settings(self) -> Dict[str, Any]:
        """Export all config values as a JSON-serializable dict.

        Used for cross-device settings sync via federation.
        """
        return {
            'namespace': self._namespace,
            'values': self.get_all(),
            'exported_at': time.time(),
        }

    def import_settings(self, data: Dict[str, Any]) -> int:
        """Import settings from an exported dict.

        Returns the number of keys updated.
        """
        values = data.get('values', {})
        count = 0
        for key, val in values.items():
            if key in self._defaults:
                self.set(key, val)
                count += 1
        return count

    @property
    def namespace(self) -> str:
        """Return the config namespace."""
        return self._namespace

    def __repr__(self) -> str:
        return f"PlatformConfig(namespace={self._namespace!r})"
