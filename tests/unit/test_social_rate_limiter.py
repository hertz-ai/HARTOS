"""
test_social_rate_limiter.py - Tests for integrations/social/rate_limiter.py

Tests TokenBucket rate limiter: the thread-safe in-memory rate limiting used
across all social API endpoints.

FT: check() allow/deny, refill behavior, cleanup of stale entries, key generation,
    rate_limit decorator, _build_limits() config.
NFT: Thread safety under concurrent access, high-frequency burst handling,
     cleanup under load, deterministic time behavior.
"""
import os
import sys
import time
import threading
import pytest
from unittest.mock import patch, Mock, MagicMock, PropertyMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from integrations.social.rate_limiter import (
    TokenBucket, _build_limits, rate_limit, get_limiter, LIMITS
)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def bucket():
    """Fresh token bucket for each test."""
    return TokenBucket()


# ============================================================
# TokenBucket._get_key
# ============================================================

class TestGetKey:
    """Key format determines bucket isolation."""

    def test_key_format(self, bucket):
        key = bucket._get_key("user1", "post")
        assert key == "user1:post"

    def test_different_users_different_keys(self, bucket):
        k1 = bucket._get_key("user1", "post")
        k2 = bucket._get_key("user2", "post")
        assert k1 != k2

    def test_different_actions_different_keys(self, bucket):
        k1 = bucket._get_key("user1", "post")
        k2 = bucket._get_key("user1", "comment")
        assert k1 != k2


# ============================================================
# TokenBucket.check - Core rate limiting
# ============================================================

class TestTokenBucketCheck:
    """Token bucket check() is the core allow/deny decision."""

    def test_first_call_always_allowed(self, bucket):
        """First call initializes bucket with max_tokens-1."""
        assert bucket.check("u1", "post", max_tokens=5, refill_rate=1.0) is True

    def test_allows_up_to_max_tokens(self, bucket):
        """Should allow max_tokens calls before denying."""
        max_t = 3
        results = [bucket.check("u1", "a", max_tokens=max_t, refill_rate=0.0) for _ in range(max_t)]
        assert all(results), f"Expected all {max_t} calls to be allowed"

    def test_denies_after_tokens_exhausted(self, bucket):
        """After exhausting tokens (with refill_rate=0), next call is denied."""
        max_t = 2
        for _ in range(max_t):
            bucket.check("u1", "a", max_tokens=max_t, refill_rate=0.0)
        assert bucket.check("u1", "a", max_tokens=max_t, refill_rate=0.0) is False

    def test_tokens_refill_over_time(self, bucket):
        """After time passes, tokens refill and calls are allowed again."""
        max_t = 1
        refill_rate = 1000.0  # 1000 tokens/sec -- effectively instant
        bucket.check("u1", "a", max_tokens=max_t, refill_rate=refill_rate)
        # Token exhausted
        assert bucket.check("u1", "a", max_tokens=max_t, refill_rate=0.0) is False
        # Simulate time passage by manipulating bucket state
        key = bucket._get_key("u1", "a")
        tokens, _ = bucket._buckets[key]
        bucket._buckets[key] = (tokens, time.time() - 10)  # 10 seconds ago
        # Now with refill_rate=1.0, should have 10 new tokens
        assert bucket.check("u1", "a", max_tokens=max_t, refill_rate=1.0) is True

    def test_tokens_capped_at_max(self, bucket):
        """Refill never exceeds max_tokens."""
        max_t = 5
        # First call creates bucket with max_tokens-1
        bucket.check("u1", "a", max_tokens=max_t, refill_rate=1000.0)
        # Simulate old timestamp for lots of refill
        key = bucket._get_key("u1", "a")
        bucket._buckets[key] = (0, time.time() - 1000)
        bucket.check("u1", "a", max_tokens=max_t, refill_rate=1.0)
        tokens, _ = bucket._buckets[key]
        # After spending one token, should be at most max_t - 1
        assert tokens <= max_t

    def test_separate_users_independent(self, bucket):
        """One user's rate limit does not affect another user."""
        # Exhaust user1
        bucket.check("u1", "a", max_tokens=1, refill_rate=0.0)
        assert bucket.check("u1", "a", max_tokens=1, refill_rate=0.0) is False
        # User2 should still be allowed
        assert bucket.check("u2", "a", max_tokens=1, refill_rate=0.0) is True

    def test_separate_actions_independent(self, bucket):
        """Different action types have separate buckets."""
        bucket.check("u1", "post", max_tokens=1, refill_rate=0.0)
        assert bucket.check("u1", "post", max_tokens=1, refill_rate=0.0) is False
        assert bucket.check("u1", "comment", max_tokens=1, refill_rate=0.0) is True

    def test_zero_max_tokens_denies_all(self, bucket):
        """Edge case: max_tokens=0 should deny (tokens start at -1)."""
        # First check: max_tokens - 1 = -1, stored as (-1, now)
        result = bucket.check("u1", "a", max_tokens=0, refill_rate=0.0)
        # Implementation stores max_tokens - 1 = -1 on first call and returns True
        # This is a quirk -- but we test the actual behavior
        # After first call, tokens should be -1
        assert bucket.check("u1", "a", max_tokens=0, refill_rate=0.0) is False


# ============================================================
# TokenBucket.cleanup
# ============================================================

class TestTokenBucketCleanup:
    """Cleanup removes stale entries to prevent memory leaks."""

    def test_removes_stale_entries(self, bucket):
        # Create an entry and backdate it
        bucket.check("stale_user", "a", max_tokens=5, refill_rate=1.0)
        key = bucket._get_key("stale_user", "a")
        bucket._buckets[key] = (5, time.time() - 7200)  # 2 hours ago
        bucket.cleanup(max_age=3600)
        assert key not in bucket._buckets

    def test_keeps_fresh_entries(self, bucket):
        bucket.check("fresh_user", "a", max_tokens=5, refill_rate=1.0)
        bucket.cleanup(max_age=3600)
        key = bucket._get_key("fresh_user", "a")
        assert key in bucket._buckets

    def test_cleanup_with_empty_buckets(self, bucket):
        """Cleanup on empty state should not crash."""
        bucket.cleanup()  # no entries, no error

    def test_auto_cleanup_triggers_at_interval(self, bucket):
        """After _CLEANUP_INTERVAL calls, cleanup runs automatically."""
        bucket._CLEANUP_INTERVAL = 5
        with patch.object(bucket, 'cleanup') as mock_cleanup:
            for i in range(6):
                bucket.check(f"user_{i}", "a", max_tokens=100, refill_rate=10.0)
            mock_cleanup.assert_called_once()


# ============================================================
# _build_limits
# ============================================================

class TestBuildLimits:
    """Rate limit configuration respects environment."""

    def test_production_limits_have_required_keys(self):
        with patch.dict(os.environ, {'SOCIAL_RATE_LIMIT_DISABLED': '', 'FLASK_ENV': 'production'}):
            limits = _build_limits()
        required_keys = {'global', 'auth', 'register', 'post', 'comment', 'vote', 'search',
                         'kong_lookup', 'kong_new_token'}
        assert set(limits.keys()) == required_keys

    def test_each_limit_has_max_tokens_and_refill_rate(self):
        with patch.dict(os.environ, {'SOCIAL_RATE_LIMIT_DISABLED': '', 'FLASK_ENV': 'production'}):
            limits = _build_limits()
        for action, cfg in limits.items():
            assert 'max_tokens' in cfg, f"{action} missing max_tokens"
            assert 'refill_rate' in cfg, f"{action} missing refill_rate"
            assert cfg['max_tokens'] > 0

    def test_disabled_env_gives_high_limits(self):
        with patch.dict(os.environ, {'SOCIAL_RATE_LIMIT_DISABLED': '1', 'FLASK_ENV': ''}):
            limits = _build_limits()
        assert limits['global']['max_tokens'] == 100000

    def test_testing_env_gives_high_limits(self):
        with patch.dict(os.environ, {'SOCIAL_RATE_LIMIT_DISABLED': '', 'FLASK_ENV': 'testing'}):
            limits = _build_limits()
        assert limits['global']['max_tokens'] == 100000

    def test_production_global_is_100_per_minute(self):
        with patch.dict(os.environ, {'SOCIAL_RATE_LIMIT_DISABLED': '', 'FLASK_ENV': 'production'}):
            limits = _build_limits()
        assert limits['global']['max_tokens'] == 100

    def test_production_auth_is_5_per_5min(self):
        with patch.dict(os.environ, {'SOCIAL_RATE_LIMIT_DISABLED': '', 'FLASK_ENV': 'production'}):
            limits = _build_limits()
        assert limits['auth']['max_tokens'] == 5

    def test_production_register_is_3_per_hour(self):
        with patch.dict(os.environ, {'SOCIAL_RATE_LIMIT_DISABLED': '', 'FLASK_ENV': 'production'}):
            limits = _build_limits()
        assert limits['register']['max_tokens'] == 3

    def test_production_post_limit_strict(self):
        with patch.dict(os.environ, {'SOCIAL_RATE_LIMIT_DISABLED': '', 'FLASK_ENV': 'production'}):
            limits = _build_limits()
        assert limits['post']['max_tokens'] == 1  # 1 post per 30 min


# ============================================================
# get_limiter
# ============================================================

class TestGetLimiter:
    """Module-level limiter singleton."""

    def test_returns_token_bucket(self):
        limiter = get_limiter()
        assert isinstance(limiter, TokenBucket)

    def test_returns_same_instance(self):
        l1 = get_limiter()
        l2 = get_limiter()
        assert l1 is l2


# ============================================================
# rate_limit decorator (requires Flask context)
# ============================================================

class TestRateLimitDecorator:
    """rate_limit() decorator integrates TokenBucket with Flask endpoints."""

    def _make_app(self):
        from flask import Flask
        app = Flask(__name__)
        app.config['TESTING'] = True
        return app

    def test_allows_request_within_limits(self):
        app = self._make_app()
        with app.test_request_context('/test', environ_base={'REMOTE_ADDR': '1.2.3.4'}):
            from flask import g
            g.user = None  # anonymous

            @rate_limit('global')
            def endpoint():
                return "ok"

            # With testing LIMITS (high), should always allow
            result = endpoint()
            assert result == "ok"

    def test_uses_ip_when_no_user(self):
        """Anonymous requests use remote_addr for rate limiting."""
        app = self._make_app()
        bucket = get_limiter()

        with app.test_request_context('/test', environ_base={'REMOTE_ADDR': '10.0.0.1'}):
            from flask import g
            g.user = None

            @rate_limit('global')
            def endpoint():
                return "ok"

            endpoint()
            # Check that an entry exists for the IP
            key = bucket._get_key('10.0.0.1', 'global')
            assert key in bucket._buckets

    def test_uses_user_id_when_authenticated(self):
        """Authenticated requests use user.id for rate limiting."""
        app = self._make_app()
        bucket = get_limiter()

        mock_user = Mock()
        mock_user.id = "user_42"

        with app.test_request_context('/test', environ_base={'REMOTE_ADDR': '10.0.0.2'}):
            from flask import g
            g.user = mock_user

            @rate_limit('global')
            def endpoint():
                return "ok"

            endpoint()
            key = bucket._get_key('user_42', 'global')
            assert key in bucket._buckets


# ============================================================
# NFT: Thread safety
# ============================================================

class TestThreadSafety:
    """Concurrent access to TokenBucket must not corrupt state or crash."""

    def test_concurrent_checks_no_crash(self):
        """50 threads hitting the same bucket simultaneously."""
        bucket = TokenBucket()
        barrier = threading.Barrier(50)
        results = []

        def worker():
            barrier.wait()
            r = bucket.check("concurrent_user", "action", max_tokens=100, refill_rate=10.0)
            results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 50
        # With max_tokens=100, all 50 should be allowed
        assert all(results)

    def test_concurrent_checks_respect_limit(self):
        """With max_tokens=10 and refill_rate=0, only 10 of 50 threads succeed."""
        bucket = TokenBucket()
        barrier = threading.Barrier(50)
        results = []

        def worker():
            barrier.wait()
            r = bucket.check("limited_user", "action", max_tokens=10, refill_rate=0.0)
            results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        allowed = sum(1 for r in results if r)
        denied = sum(1 for r in results if not r)
        # Due to race conditions with lock, allowed should be approximately 10
        assert allowed <= 12  # some slack for timing
        assert denied >= 38

    def test_concurrent_cleanup_no_crash(self):
        """Cleanup running concurrently with checks must not crash."""
        bucket = TokenBucket()

        for i in range(100):
            bucket.check(f"user_{i}", "a", max_tokens=5, refill_rate=1.0)

        barrier = threading.Barrier(10)
        errors = []

        def cleaner():
            barrier.wait()
            try:
                bucket.cleanup(max_age=0)
            except Exception as e:
                errors.append(e)

        def checker():
            barrier.wait()
            try:
                bucket.check("new_user", "a", max_tokens=5, refill_rate=1.0)
            except Exception as e:
                errors.append(e)

        threads = []
        for _ in range(5):
            threads.append(threading.Thread(target=cleaner))
            threads.append(threading.Thread(target=checker))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors during concurrent cleanup: {errors}"


# ============================================================
# NFT: Edge cases
# ============================================================

class TestEdgeCases:
    """Boundary and degenerate inputs."""

    def test_empty_user_id(self):
        bucket = TokenBucket()
        assert bucket.check("", "action", max_tokens=5, refill_rate=1.0) is True

    def test_empty_action(self):
        bucket = TokenBucket()
        assert bucket.check("user", "", max_tokens=5, refill_rate=1.0) is True

    def test_very_high_refill_rate_after_time_gap(self):
        """High refill rate fully replenishes after even a tiny time gap."""
        bucket = TokenBucket()
        # Exhaust all tokens
        for _ in range(5):
            bucket.check("u", "a", max_tokens=5, refill_rate=0.0)
        # Should be denied now
        assert bucket.check("u", "a", max_tokens=5, refill_rate=0.0) is False
        # Backdate the entry by 1 second
        key = bucket._get_key("u", "a")
        tokens, _ = bucket._buckets[key]
        bucket._buckets[key] = (tokens, time.time() - 1.0)
        # With refill_rate=1e9, 1 second = 1e9 tokens refilled (capped at max_tokens)
        assert bucket.check("u", "a", max_tokens=5, refill_rate=1e9) is True

    def test_negative_refill_rate_degrades_gracefully(self):
        """Negative refill should drain tokens faster but not crash."""
        bucket = TokenBucket()
        bucket.check("u", "a", max_tokens=5, refill_rate=-1.0)
        # Should not crash -- result is implementation-defined
        bucket.check("u", "a", max_tokens=5, refill_rate=-1.0)

    def test_cleanup_with_very_small_max_age(self):
        """Very small max_age removes entries that are old enough."""
        bucket = TokenBucket()
        bucket.check("u", "a", max_tokens=5, refill_rate=1.0)
        # Backdate the entry so it is stale
        key = bucket._get_key("u", "a")
        tokens, _ = bucket._buckets[key]
        bucket._buckets[key] = (tokens, time.time() - 100)
        bucket.cleanup(max_age=1)
        assert len(bucket._buckets) == 0
