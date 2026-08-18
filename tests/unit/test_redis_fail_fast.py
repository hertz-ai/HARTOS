"""Redis availability probes must fail fast, and must not re-adopt the kwarg
that stopped working.

redis-py >= 6.0 ignores retry_on_timeout and retries timeouts by default, so
four call sites asking for "fail fast, no retries" silently began paying ~14s
per probe.  Measured on redis-py 7.1.0 against a localhost port that drops
rather than refuses:

    retry_on_timeout=False   13.78s
    retry=None                2.00s

core.http_pool already documents the intended policy for HTTP -- "localhost:
0 retries (dead local services should fail instantly, not block 15s)".
core.redis_client applies it to Redis.

The AST test below is the mechanical guard, in the same spirit as
tests/test_lang_constants.py: a reviewer will not catch a re-added
retry_on_timeout, and the deprecation warning is easy to miss because the call
still succeeds.
"""
import ast
import os
import time

import pytest

from core import redis_client


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


# Sites that intend "fail fast" and must route through core.redis_client.
# security/rate_limiter_redis.py is deliberately EXCLUDED: it sets
# retry_on_timeout=True on purpose, wanting resilience over speed.
FAIL_FAST_SITES = [
    os.path.join('integrations', 'distributed_agent', 'api.py'),
    os.path.join('integrations', 'distributed_agent', 'coordinator_backends.py'),
    os.path.join('integrations', 'social', 'api_tracker.py'),
    os.path.join('integrations', 'agent_lightning', 'store.py'),
]


def test_probe_client_fails_fast_against_a_dead_port():
    """A dead local Redis must be reported absent in seconds, not ~14s."""
    c = redis_client.probe_client(host='localhost', port=6379)
    if c is None:
        pytest.skip("redis package not installed")
    t0 = time.monotonic()
    try:
        c.ping()
    except Exception:
        pass
    elapsed = time.monotonic() - t0
    assert elapsed < 6.0, (
        f"probe took {elapsed:.1f}s -- retries are still enabled; "
        f"retry=None is what disables them on redis-py >= 6.0")


def test_probe_client_returns_none_without_redis(monkeypatch):
    """No redis package must mean 'feature unavailable', not a crash.

    Preserves the behaviour every original call site had via its own
    try/except ImportError.
    """
    import builtins
    real_import = builtins.__import__

    def _no_redis(name, *a, **kw):
        if name == 'redis':
            raise ImportError("no redis")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, '__import__', _no_redis)
    assert redis_client.probe_client() is None


def test_probe_client_honours_env_host_port(monkeypatch):
    """Three of the four original sites read REDIS_HOST/REDIS_PORT by hand."""
    monkeypatch.setenv('REDIS_HOST', 'redis.example.invalid')
    monkeypatch.setenv('REDIS_PORT', '6380')
    c = redis_client.probe_client()
    if c is None:
        pytest.skip("redis package not installed")
    kw = c.get_connection_kwargs()
    assert kw['host'] == 'redis.example.invalid'
    assert kw['port'] == 6380


def test_probe_client_disables_retries():
    """Pin the mechanism, not just the timing.

    The timing test above can pass on a machine where localhost REFUSES
    (milliseconds) even with retries enabled, which would hide a regression.
    """
    c = redis_client.probe_client()
    if c is None:
        pytest.skip("redis package not installed")
    retry = c.get_retry()
    assert retry is None or getattr(retry, '_retries', None) == 0, (
        f"expected no retry policy, got {retry!r}")


@pytest.mark.parametrize('rel', FAIL_FAST_SITES)
def test_no_site_passes_the_ignored_retry_on_timeout_kwarg(rel):
    """AST guard: retry_on_timeout=False is a no-op and must not come back.

    It reads as "no retries" and does the opposite, which is why this needs a
    mechanical check rather than review.
    """
    path = os.path.join(_repo_root(), rel)
    if not os.path.exists(path):
        pytest.skip(f"{rel} not present")
    with open(path, encoding='utf-8') as fh:
        tree = ast.parse(fh.read(), filename=path)

    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == 'retry_on_timeout':
                    offenders.append(getattr(node, 'lineno', '?'))

    assert not offenders, (
        f"{rel} passes retry_on_timeout at line(s) {offenders}; redis-py >= 6.0 "
        f"ignores it. Use core.redis_client.probe_client() instead.")


@pytest.mark.parametrize('rel', FAIL_FAST_SITES)
def test_fail_fast_sites_do_not_build_their_own_client(rel):
    """No parallel path: these sites must not hand-roll redis.Redis(...).

    Each previously constructed its own client with slightly different
    timeouts, which is how one site's fix left the other three broken.
    """
    path = os.path.join(_repo_root(), rel)
    if not os.path.exists(path):
        pytest.skip(f"{rel} not present")
    with open(path, encoding='utf-8') as fh:
        tree = ast.parse(fh.read(), filename=path)

    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Attribute) and f.attr in ('Redis', 'from_url')
                    and isinstance(f.value, ast.Name) and f.value.id == 'redis'):
                offenders.append(getattr(node, 'lineno', '?'))

    assert not offenders, (
        f"{rel} constructs redis.Redis/from_url directly at line(s) {offenders}; "
        f"use core.redis_client.probe_client() so the retry policy stays in one "
        f"place.")
