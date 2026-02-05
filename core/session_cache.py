"""
TTL-based session cache for global dictionaries.

Replaces unbounded global dicts (user_agents, agent_data, etc.) with
auto-expiring caches that prevent memory leaks on long-running servers.

Before: 11+ global dicts grow unbounded, accumulating GB of garbage.
After:  Entries auto-expire after configurable TTL (default 2 hours).
"""

import time
import threading
import logging
from collections import OrderedDict

logger = logging.getLogger('hevolve_core')


class TTLCache:
    """
    Thread-safe dictionary with automatic time-to-live expiration.

    Features:
    - O(1) get/set/delete
    - Automatic cleanup of expired entries
    - Max size cap to prevent unbounded growth
    - Drop-in replacement for dict (supports [] operator, .get(), etc.)
    """

    def __init__(self, ttl_seconds: int = 7200, max_size: int = 1000, name: str = 'cache'):
        self._data = OrderedDict()
        self._timestamps = {}
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._name = name
        self._lock = threading.Lock()
        self._cleanup_counter = 0

    def __setitem__(self, key, value):
        with self._lock:
            now = time.monotonic()
            if key in self._data:
                del self._data[key]
            self._data[key] = value
            self._timestamps[key] = now

            # Evict oldest if over max size
            while len(self._data) > self._max_size:
                oldest_key, _ = self._data.popitem(last=False)
                self._timestamps.pop(oldest_key, None)
                logger.debug(f"[{self._name}] Evicted oldest entry: {oldest_key}")

            # Periodic cleanup every 100 writes
            self._cleanup_counter += 1
            if self._cleanup_counter >= 100:
                self._cleanup_counter = 0
                self._cleanup_expired(now)

    def __getitem__(self, key):
        with self._lock:
            if key not in self._data:
                raise KeyError(key)
            if self._is_expired(key):
                self._remove(key)
                raise KeyError(key)
            return self._data[key]

    def __contains__(self, key):
        with self._lock:
            if key not in self._data:
                return False
            if self._is_expired(key):
                self._remove(key)
                return False
            return True

    def __delitem__(self, key):
        with self._lock:
            self._remove(key)

    def __len__(self):
        with self._lock:
            self._cleanup_expired(time.monotonic())
            return len(self._data)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def pop(self, key, *args):
        with self._lock:
            if key in self._data:
                value = self._data.pop(key)
                self._timestamps.pop(key, None)
                return value
            if args:
                return args[0]
            raise KeyError(key)

    def keys(self):
        with self._lock:
            self._cleanup_expired(time.monotonic())
            return list(self._data.keys())

    def values(self):
        with self._lock:
            self._cleanup_expired(time.monotonic())
            return list(self._data.values())

    def items(self):
        with self._lock:
            self._cleanup_expired(time.monotonic())
            return list(self._data.items())

    def clear(self):
        with self._lock:
            self._data.clear()
            self._timestamps.clear()

    def _is_expired(self, key) -> bool:
        ts = self._timestamps.get(key)
        if ts is None:
            return True
        return (time.monotonic() - ts) > self._ttl

    def _remove(self, key):
        self._data.pop(key, None)
        self._timestamps.pop(key, None)

    def _cleanup_expired(self, now):
        expired = [k for k, ts in self._timestamps.items() if (now - ts) > self._ttl]
        for k in expired:
            self._remove(k)
        if expired:
            logger.debug(f"[{self._name}] Cleaned up {len(expired)} expired entries")

    def stats(self) -> dict:
        with self._lock:
            now = time.monotonic()
            active = sum(1 for ts in self._timestamps.values() if (now - ts) <= self._ttl)
            return {
                'name': self._name,
                'total': len(self._data),
                'active': active,
                'expired': len(self._data) - active,
                'max_size': self._max_size,
                'ttl_seconds': self._ttl,
            }
