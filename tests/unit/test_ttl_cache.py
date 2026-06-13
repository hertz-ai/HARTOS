"""ttl_cached — a HARD-TTL function-result cache for expensive *discovery*
(filesystem path resolution, config reads, proxy lookups) that the background
service loops re-do every tick.

Why a new primitive (not core.session_cache.TTLCache): session_cache extends an
entry's TTL on every read (touch-on-read), which is correct for "keep a live
session warm" but WRONG here — a cached discovery result must HARD-expire after
ttl regardless of how often it's read, so a stale value (a llama-binary upgrade,
a config edit, a changed proxy) self-heals on the next miss. Caching live state
is explicitly out of scope; this is only for values that are stable between
ticks and tolerate a bounded staleness window.

The 2026-06-13 sluggishness dig: model_lifecycle / superadmin-report / llm_status
each re-ran expensive probes (LlamaConfig load, getproxies registry read,
find_llama_server FS walk) every tick, none gated, all overlapping -> CPU pegged.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.ttl_cache import ttl_cached  # noqa: E402


class _Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, dt): self.t += dt


class TestTtlCached:
    def test_caches_within_ttl(self):
        clk = _Clock()
        calls = {'n': 0}

        @ttl_cached(ttl_seconds=10, _clock=clk)
        def probe():
            calls['n'] += 1
            return 'path'

        assert probe() == 'path'
        assert probe() == 'path'
        assert probe() == 'path'
        assert calls['n'] == 1  # computed once, served from cache

    def test_hard_expires_after_ttl_even_with_constant_reads(self):
        clk = _Clock()
        calls = {'n': 0}

        @ttl_cached(ttl_seconds=10, _clock=clk)
        def probe():
            calls['n'] += 1
            return calls['n']

        assert probe() == 1
        clk.advance(5); assert probe() == 1   # still within ttl
        clk.advance(6); assert probe() == 2   # 11s elapsed -> hard expire -> recompute
        # reads do NOT extend the entry (no touch-on-read): keep reading, still expires
        clk.advance(11); assert probe() == 3

    def test_distinct_args_cached_separately(self):
        clk = _Clock()
        calls = {'n': 0}

        @ttl_cached(ttl_seconds=10, _clock=clk)
        def probe(x, y=0):
            calls['n'] += 1
            return (x, y)

        assert probe(1) == (1, 0)
        assert probe(2) == (2, 0)
        assert probe(1, y=9) == (1, 9)
        assert calls['n'] == 3
        assert probe(1) == (1, 0)  # first key still cached
        assert calls['n'] == 3

    def test_cache_clear_forces_recompute(self):
        clk = _Clock()
        calls = {'n': 0}

        @ttl_cached(ttl_seconds=1000, _clock=clk)
        def probe():
            calls['n'] += 1
            return calls['n']

        assert probe() == 1
        assert probe() == 1
        probe.cache_clear()           # e.g. invalidate after a llama-binary upgrade
        assert probe() == 2

    def test_exception_not_cached(self):
        clk = _Clock()
        calls = {'n': 0}

        @ttl_cached(ttl_seconds=1000, _clock=clk)
        def probe():
            calls['n'] += 1
            if calls['n'] < 2:
                raise RuntimeError('transient')
            return 'ok'

        try:
            probe()
            assert False, "should have raised"
        except RuntimeError:
            pass
        assert probe() == 'ok'        # retried (failure was not cached)
        assert calls['n'] == 2
