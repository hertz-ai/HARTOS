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
    - **Touch-on-read**: every successful read via `[]` / `.get()` /
      `.setdefault()` extends the entry's TTL.  This keeps actively-used
      session state alive for the full duration of an active recipe
      pipeline, while abandoned entries (no reads → no touch) still
      auto-expire to prevent the original memory-leak class.
      `__contains__` (`in` operator) is intentionally side-effect free
      to keep Python idioms predictable.

    Touch-on-read fixes the 2026-05-08 "TTS without text" incident:
    `scheduler_check[user_prompt]` raised KeyError mid-recipe because
    the 2h TTL elapsed during a long pipeline run, even though the
    recipe was being read continuously.  After this change, a recipe
    that's actively reading state stays warm; one that's been
    abandoned for 2h still gets cleaned up.
    """

    def __init__(self, ttl_seconds: int = 7200, max_size: int = 1000, name: str = 'cache', loader=None):
        self._data = OrderedDict()
        self._timestamps = {}
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._name = name
        self._lock = threading.Lock()
        self._cleanup_counter = 0
        self._loader = loader  # callable(key) → value or None

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
            if key in self._data and not self._is_expired(key):
                # Touch-on-read: extend TTL for actively-used keys.  Active
                # recipe pipelines continuously read state — without this,
                # the 2h TTL would hard-evict mid-flow and produce KeyError
                # (root cause of the 2026-05-08 TTS-without-text incident).
                # Abandoned entries (no reads) still age out via the
                # original timestamp + _cleanup_expired path, preserving
                # the memory-leak-prevention guarantee.
                self._timestamps[key] = time.monotonic()
                return self._data[key]
            # Clean up expired entry if present
            if key in self._data:
                self._remove(key)
            # Try loader before raising KeyError
            if self._loader:
                try:
                    value = self._loader(key)
                except Exception as e:
                    logger.debug(f"[{self._name}] Loader error for {key}: {e}")
                    raise KeyError(key)
                if value is not None:
                    self._data[key] = value
                    self._timestamps[key] = time.monotonic()
                    logger.debug(f"[{self._name}] Loaded {key} from persistent storage")
                    return value
            raise KeyError(key)

    def __contains__(self, key):
        with self._lock:
            if key in self._data and not self._is_expired(key):
                return True
            # Clean up expired entry if present
            if key in self._data:
                self._remove(key)
            # Try loader
            if self._loader:
                try:
                    value = self._loader(key)
                except Exception:
                    return False
                if value is not None:
                    self._data[key] = value
                    self._timestamps[key] = time.monotonic()
                    logger.debug(f"[{self._name}] Loaded {key} from persistent storage")
                    return True
            return False

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

    def setdefault(self, key, default=None):
        with self._lock:
            if key in self._data and not self._is_expired(key):
                # Touch-on-read: same rationale as __getitem__.
                # setdefault is a read-or-create operation; the read
                # path should also extend TTL.
                self._timestamps[key] = time.monotonic()
                return self._data[key]
            # Clean up expired entry if present
            if key in self._data:
                self._remove(key)
            # Try loader before using default
            if self._loader:
                try:
                    value = self._loader(key)
                except Exception:
                    value = None
                if value is not None:
                    self._data[key] = value
                    self._timestamps[key] = time.monotonic()
                    return value
            self._data[key] = default
            self._timestamps[key] = time.monotonic()
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
