"""
Cache Service — Unified in-memory + optional disk cache for HART OS.

Provides a thread-safe, namespace-aware cache with TTL expiry and LRU
eviction.  Every subsystem (TTS voices, model configs, resonance profiles,
compute policies) can share one cache instance instead of rolling its own
dict-with-TTL.

Design decisions:
- OrderedDict for O(1) LRU eviction (move_to_end on access)
- Namespace keys: 'tts:voice:alba' — clear('tts') wipes all tts:* keys
- Thread-safe via threading.Lock (same pattern as EventBus / PlatformConfig)
- Hit/miss counters for observability
- Optional diskcache persistence (try/except ImportError — it's an optional dep)
- Sentinel _MISSING distinguishes None values from cache misses
- Lifecycle protocol (start/stop/health) for ServiceRegistry integration

Generalizes TTL cache patterns from:
- compute_config.py (30s TTL dict)
- diskcache usage in aider_core/
- model_registry.py (in-memory model lookups)

Usage:
    from core.platform.cache import get_cache

    cache = get_cache()
    cache.set('tts:voice:alba', voice_data, ttl=600)
    voice = cache.get('tts:voice:alba')
    cache.clear('tts')  # wipe all tts:* keys
    cache.set_persistent('model:config:phi', config)  # memory + disk
"""

import collections
import logging
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger('hevolve.platform')

# ═══════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════

DEFAULT_MAX_SIZE = 4096
DEFAULT_TTL = 300  # seconds

# Sentinel for distinguishing None values from cache misses
_MISSING = object()


# ═══════════════════════════════════════════════════════════════
# Cache Entry (internal)
# ═══════════════════════════════════════════════════════════════

class _CacheEntry:
    """Internal record for a cached value with expiry metadata."""

    __slots__ = ('value', 'expires_at')

    def __init__(self, value: Any, expires_at: Optional[float]):
        self.value = value
        self.expires_at = expires_at


# ═══════════════════════════════════════════════════════════════
# Cache Service
# ═══════════════════════════════════════════════════════════════

class CacheService:
    """Thread-safe in-memory LRU cache with TTL, namespaces, and optional disk.

    Implements the Lifecycle protocol (start/stop/health) for
    ServiceRegistry integration.
    """

    def __init__(self, max_size: int = DEFAULT_MAX_SIZE,
                 default_ttl: float = DEFAULT_TTL):
        """Initialize the cache.

        Args:
            max_size: Maximum number of entries. When exceeded the least
                      recently used entry is evicted.  0 means unlimited.
            default_ttl: Default time-to-live in seconds. 0 means no expiry.
        """
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._data: collections.OrderedDict[str, _CacheEntry] = (
            collections.OrderedDict()
        )
        self._lock = threading.Lock()
        self._started = False

        # Stats
        self._hit_count = 0
        self._miss_count = 0

        # Optional disk backend
        self._disk: Any = None
        self._init_disk()

    # ── Lifecycle Protocol ────────────────────────────────────

    def start(self) -> None:
        """Start the cache service."""
        self._started = True
        logger.debug("CacheService started (max_size=%d, ttl=%ds)",
                      self._max_size, self._default_ttl)

    def stop(self) -> None:
        """Stop and clear the cache."""
        with self._lock:
            self._data.clear()
            self._started = False
        if self._disk is not None:
            try:
                self._disk.close()
            except Exception:
                pass
            self._disk = None
        logger.debug("CacheService stopped")

    def health(self) -> dict:
        """Return health status for ServiceRegistry."""
        with self._lock:
            size = len(self._data)
        return {
            'status': 'ok' if self._started else 'stopped',
            'size': size,
            'max_size': self._max_size,
            'hit_count': self._hit_count,
            'miss_count': self._miss_count,
            'hit_rate': self.hit_rate(),
            'disk_available': self._disk is not None,
        }

    # ── Core API ──────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """Retrieve a value from the cache.

        Args:
            key: Cache key (e.g., 'tts:voice:alba').
            default: Value to return on miss. Defaults to None.

        Returns:
            Cached value, or *default* if key is missing or expired.
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._miss_count += 1
                return default

            # Check TTL expiry
            if entry.expires_at is not None and time.monotonic() > entry.expires_at:
                # Expired — evict
                del self._data[key]
                self._miss_count += 1
                return default

            # LRU: move to end (most recently used)
            self._data.move_to_end(key)
            self._hit_count += 1
            return entry.value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Store a value in the cache.

        Args:
            key: Cache key.
            value: Any value (including None — distinguished via sentinel).
            ttl: Time-to-live in seconds. None uses the default TTL.
                 0 means no expiry.
        """
        if ttl is None:
            ttl = self._default_ttl
        expires_at = (time.monotonic() + ttl) if ttl > 0 else None

        with self._lock:
            # If key exists, update in place (and move to end)
            if key in self._data:
                self._data[key] = _CacheEntry(value, expires_at)
                self._data.move_to_end(key)
            else:
                # Evict LRU if at capacity
                if self._max_size > 0 and len(self._data) >= self._max_size:
                    self._data.popitem(last=False)  # pop oldest
                self._data[key] = _CacheEntry(value, expires_at)

    def delete(self, key: str) -> bool:
        """Remove a key from the cache.

        Args:
            key: Cache key.

        Returns:
            True if the key existed and was removed, False otherwise.
        """
        with self._lock:
            if key in self._data:
                del self._data[key]
                return True
            return False

    def has(self, key: str) -> bool:
        """Check if a key exists and is not expired.

        Does NOT count as a hit or miss, and does NOT affect LRU order.
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return False
            if entry.expires_at is not None and time.monotonic() > entry.expires_at:
                del self._data[key]
                return False
            return True

    def clear(self, namespace: Optional[str] = None) -> int:
        """Clear cache entries.

        Args:
            namespace: If provided, only keys starting with 'namespace:'
                       are removed.  If None, all keys are cleared.

        Returns:
            Number of entries removed.
        """
        with self._lock:
            if namespace is None:
                count = len(self._data)
                self._data.clear()
                return count

            prefix = namespace + ':'
            keys_to_delete = [k for k in self._data if k.startswith(prefix)]
            for k in keys_to_delete:
                del self._data[k]
            return len(keys_to_delete)

    # ── Disk Persistence ──────────────────────────────────────

    def set_persistent(self, key: str, value: Any,
                       ttl: Optional[float] = None) -> None:
        """Store a value in both memory and disk cache.

        Falls back to memory-only if diskcache is unavailable.

        Args:
            key: Cache key.
            value: Any picklable value.
            ttl: Time-to-live in seconds. None uses default TTL.
                 0 means no expiry.
        """
        self.set(key, value, ttl=ttl)
        if self._disk is not None:
            try:
                expire = ttl if ttl is not None else self._default_ttl
                # diskcache.set() expire=None means no expiry;
                # expire=0 would expire immediately, so map our 0 to None
                disk_expire = expire if expire > 0 else None
                self._disk.set(key, value, expire=disk_expire)
            except Exception as e:
                logger.debug("Disk cache write failed for '%s': %s", key, e)

    def get_persistent(self, key: str, default: Any = None) -> Any:
        """Retrieve a value, falling back to disk if not in memory.

        On a disk hit, the value is promoted back into memory.

        Args:
            key: Cache key.
            default: Fallback value.

        Returns:
            Cached value or *default*.
        """
        # Try memory first
        result = self.get(key, _MISSING)
        if result is not _MISSING:
            return result

        # Try disk
        if self._disk is not None:
            try:
                disk_val = self._disk.get(key, _MISSING)
                if disk_val is not _MISSING:
                    # Promote to memory
                    self.set(key, disk_val)
                    return disk_val
            except Exception as e:
                logger.debug("Disk cache read failed for '%s': %s", key, e)

        return default

    # ── Stats ─────────────────────────────────────────────────

    @property
    def hit_count(self) -> int:
        """Total cache hits since creation or last reset."""
        return self._hit_count

    @property
    def miss_count(self) -> int:
        """Total cache misses since creation or last reset."""
        return self._miss_count

    def hit_rate(self) -> float:
        """Hit rate as a float between 0.0 and 1.0.

        Returns 0.0 if no gets have been performed.
        """
        total = self._hit_count + self._miss_count
        if total == 0:
            return 0.0
        return self._hit_count / total

    def stats(self) -> Dict[str, Any]:
        """Return a stats dictionary."""
        with self._lock:
            size = len(self._data)
        return {
            'size': size,
            'max_size': self._max_size,
            'hit_count': self._hit_count,
            'miss_count': self._miss_count,
            'hit_rate': self.hit_rate(),
            'disk_available': self._disk is not None,
        }

    def reset_stats(self) -> None:
        """Reset hit/miss counters to zero."""
        self._hit_count = 0
        self._miss_count = 0

    # ── Internal ──────────────────────────────────────────────

    def _init_disk(self) -> None:
        """Try to initialize diskcache backend. No-op if unavailable."""
        try:
            import diskcache
            import os
            import tempfile
            cache_dir = os.environ.get(
                'HART_CACHE_DIR',
                os.path.join(tempfile.gettempdir(), 'hartos_cache'),
            )
            self._disk = diskcache.Cache(cache_dir)
            logger.debug("Disk cache initialized at %s", cache_dir)
        except ImportError:
            self._disk = None
            logger.debug("diskcache not installed — disk persistence unavailable")
        except Exception as e:
            self._disk = None
            logger.debug("Disk cache init failed: %s", e)


# ═══════════════════════════════════════════════════════════════
# Module-level convenience — safe access without circular imports
# ═══════════════════════════════════════════════════════════════

def get_cache() -> Optional[CacheService]:
    """Get the platform CacheService (if bootstrapped).

    Safe to call from anywhere — returns None if the platform
    hasn't been bootstrapped yet.

    Usage:
        cache = get_cache()
        if cache:
            cache.set('my:key', value)
    """
    try:
        from core.platform.registry import get_registry
        registry = get_registry()
        if not registry.has('cache'):
            return None
        return registry.get('cache')
    except Exception:
        return None
