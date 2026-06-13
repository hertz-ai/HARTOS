"""Hard-TTL function-result cache for expensive *discovery* work.

``ttl_cached`` memoizes a function's return per-arguments for ``ttl_seconds``,
then HARD-expires it (regardless of how often it was read) so a stale result
self-heals on the next call. Use it ONLY for values that are stable between
background-loop ticks and tolerate a bounded staleness window:

  - filesystem path resolution (``find_llama_server``)
  - config-file reads
  - proxy/registry lookups (``getproxies``)

NEVER use it for live state (CPU%, "is the server up" probes, task availability)
— those must be read fresh every time. That is the "do not cache what might be
needed live" rule, in code form.

Background — the 2026-06-13 sluggishness dig: model_lifecycle, superadmin-report
and /llm_status each re-ran an expensive probe every tick (LlamaConfig load,
getproxies registry read, find_llama_server FS walk), none cached, all
overlapping → CPU pegged. This is the shared primitive that removes that class
without going stale (TTL) and without caching anything live.

Distinct from ``core.session_cache.TTLCache``, which extends an entry's TTL on
every read (touch-on-read) — correct for keeping a live session warm, wrong here
where a discovery result must expire on a wall-clock schedule so upgrades/edits
are picked up. Apply it to a MODULE-LEVEL helper keyed by the real inputs, not a
method keyed by ``self`` (a re-instantiated object would never hit the cache).
"""
import functools
import threading
import time
from typing import Callable

__all__ = ["ttl_cached"]


def ttl_cached(ttl_seconds: float, maxsize: int = 256,
               _clock: Callable[[], float] = time.monotonic):
    """Memoize ``fn``'s return per-args for ``ttl_seconds`` with HARD expiry
    (no touch-on-read). Thread-safe. Exceptions are NOT cached, so a transient
    failure is retried on the next call. The wrapped function gains
    ``.cache_clear()`` to force a refresh after a known mutation (e.g. a
    llama-binary upgrade or a config save).

    ``_clock`` is injectable for deterministic tests; never pass it in prod.
    """
    def decorator(fn):
        cache = {}  # key -> (value, expiry)
        lock = threading.Lock()

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = _clock()
            with lock:
                hit = cache.get(key)
                if hit is not None and hit[1] > now:
                    return hit[0]
            # Compute OUTSIDE the lock so a slow probe never blocks other keys.
            # Discovery is idempotent: a rare double-compute on a cold key is
            # harmless, and not holding the lock across the slow call is what
            # keeps this from becoming its own bottleneck.
            value = fn(*args, **kwargs)
            with lock:
                cache[key] = (value, _clock() + ttl_seconds)
                if len(cache) > maxsize:
                    # Cheap bound (evict nearest-expiry), not a true LRU — these
                    # caches hold a handful of keys, not thousands.
                    oldest = min(cache, key=lambda k: cache[k][1])
                    if oldest != key:
                        del cache[oldest]
            return value

        def cache_clear():
            with lock:
                cache.clear()
        wrapper.cache_clear = cache_clear
        return wrapper
    return decorator
