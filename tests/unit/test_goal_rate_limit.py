"""
Tests for goal rate limiting via security/rate_limiter_redis.py.

Run: pytest tests/unit/test_goal_rate_limit.py -v --noconftest
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from flask import Flask, g

from security.rate_limiter_redis import (
    RedisRateLimiter,
    rate_limit,
    get_rate_limiter,
)


class TestGoalRateLimit(unittest.TestCase):
    """Goal creation rate limiting."""

    def setUp(self):
        self.limiter = RedisRateLimiter()
        self.limiter._redis = None  # Force in-memory mode
        self.limiter._memory_store.clear()

    def test_goal_create_limit_exists(self):
        """goal_create must be in LIMITS."""
        self.assertIn('goal_create', RedisRateLimiter.LIMITS)
        max_req, window = RedisRateLimiter.LIMITS['goal_create']
        self.assertEqual(max_req, 10)
        self.assertEqual(window, 3600)

    @patch.object(RedisRateLimiter, '_get_key', return_value='rl:goal_create:user:user_1')
    def test_under_limit_allowed(self, mock_key):
        for i in range(10):
            result = self.limiter.check('goal_create')
            self.assertTrue(result, f"Request {i+1} should be allowed")

    @patch.object(RedisRateLimiter, '_get_key', return_value='rl:goal_create:user:user_1')
    def test_over_limit_blocked(self, mock_key):
        # Exhaust the limit
        for i in range(10):
            self.limiter.check('goal_create')

        # 11th should be blocked
        result = self.limiter.check('goal_create')
        self.assertFalse(result, "11th goal should be blocked")

    def test_different_users_separate_limits(self):
        """Different user keys have independent limits."""
        # Simulate user 1 exhausting their limit
        self.limiter._memory_store.clear()
        with patch.object(self.limiter, '_get_key', return_value='rl:goal_create:user:user_1'):
            for i in range(10):
                self.limiter.check('goal_create')
            # User 1 blocked
            result = self.limiter.check('goal_create')
            self.assertFalse(result)

        # User 2 should still have quota (different key)
        with patch.object(self.limiter, '_get_key', return_value='rl:goal_create:user:user_2'):
            result = self.limiter.check('goal_create')
            self.assertTrue(result, "Different user should have separate limit")


class TestRedisFailClosed(unittest.TestCase):
    """
    A Redis outage must FAIL CLOSED: the limiter falls back to the in-memory
    limiter and keeps enforcing. It must NEVER fail OPEN (silently allow every
    request), which would bypass every rate limit.
    """

    def setUp(self):
        self.limiter = RedisRateLimiter()
        self.limiter._memory_store.clear()

    def _broken_redis(self, stage='pipeline'):
        """A redis client that is truthy but explodes on use."""
        r = MagicMock(name='broken_redis')
        if stage == 'pipeline':
            r.pipeline.side_effect = ConnectionError('redis down')
        elif stage == 'execute':
            pipe = MagicMock()
            pipe.execute.side_effect = ConnectionError('redis down')
            r.pipeline.return_value = pipe
        return r

    def test_pipeline_error_falls_back_to_memory_and_still_enforces(self):
        """redis.pipeline() raising -> memory limiter still blocks at the ceiling."""
        broken = self._broken_redis('pipeline')
        self.limiter._redis = broken  # truthy -> _check_redis path is taken
        with patch.object(self.limiter, '_get_key', return_value='rl:chat:user:u1'):
            # chat = (30, 60): first 30 allowed, 31st must be blocked by the
            # in-memory fallback -> proves it did NOT fail open.
            for i in range(30):
                self.assertTrue(
                    self.limiter.check('chat'),
                    f"request {i+1} should be allowed by memory fallback",
                )
            self.assertFalse(
                self.limiter.check('chat'),
                "31st request must be BLOCKED -> limiter failed CLOSED, not open",
            )
        # The broken redis path was actually exercised (not a happy memory path).
        self.assertTrue(broken.pipeline.called)

    def test_execute_error_also_fails_closed(self):
        """pipe.execute() raising is caught and routed to the memory fallback."""
        broken = self._broken_redis('execute')
        self.limiter._redis = broken
        with patch.object(self.limiter, '_get_key', return_value='rl:auth:ip:1.2.3.4'):
            # auth = (10, 60)
            for _ in range(10):
                self.assertTrue(self.limiter.check('auth'))
            self.assertFalse(
                self.limiter.check('auth'),
                "must fail CLOSED via memory even when pipe.execute() dies",
            )

    def test_single_redis_error_does_not_return_open_allow(self):
        """
        Direct _check_redis unit: on the very first errored call it must return
        the memory verdict (True because empty), never an unconditional allow
        that ignores the ceiling. We prove it by pre-filling memory to the cap.
        """
        broken = self._broken_redis('pipeline')
        self.limiter._redis = broken
        # Pre-fill memory for this key to its ceiling.
        import time as _t
        now = _t.time()
        self.limiter._memory_store['k'] = [now] * 10
        # max_requests=10 -> already at ceiling -> fallback must return False.
        self.assertFalse(
            self.limiter._check_redis('k', 10, 60),
            "fallback must respect the ceiling, not blanket-allow on error",
        )


class TestCheckRedisHappyPath(unittest.TestCase):
    """The Redis sliding-window branch itself (mocked redis boundary)."""

    def setUp(self):
        self.limiter = RedisRateLimiter()
        self.limiter._memory_store.clear()

    def _redis_with_count(self, count):
        r = MagicMock(name='redis')
        pipe = MagicMock()
        # results[1] is the zcard (current count); results[0] is the cleanup.
        pipe.execute.return_value = [0, count]
        r.pipeline.return_value = pipe
        return r

    def test_under_limit_allows_and_records(self):
        r = self._redis_with_count(5)
        self.limiter._redis = r
        self.assertTrue(self.limiter._check_redis('k', 10, 60))
        r.zadd.assert_called_once()          # under limit -> records the hit
        r.expire.assert_called_once_with('k', 61)  # window + 1

    def test_at_limit_blocks_without_recording(self):
        r = self._redis_with_count(10)  # zcard == max_requests
        self.limiter._redis = r
        self.assertFalse(self.limiter._check_redis('k', 10, 60))
        r.zadd.assert_not_called()  # blocked -> must NOT record a new hit
        r.expire.assert_not_called()

    def test_over_limit_blocks_without_recording(self):
        r = self._redis_with_count(15)  # already above ceiling
        self.limiter._redis = r
        self.assertFalse(self.limiter._check_redis('k', 10, 60))
        r.zadd.assert_not_called()


class TestGetKeyScoping(unittest.TestCase):
    """_get_key: per-user when g.user_id is set, else per-IP; 'unknown' IP guard."""

    def setUp(self):
        self.app = Flask(__name__)
        self.limiter = RedisRateLimiter()
        self.limiter._redis = None

    def test_per_user_key_when_user_id_present(self):
        with self.app.test_request_context('/', environ_base={'REMOTE_ADDR': '10.0.0.5'}):
            g.user_id = 'user_42'
            self.assertEqual(self.limiter._get_key('chat'), 'rl:chat:user:user_42')

    def test_integer_user_id_scopes_per_user(self):
        with self.app.test_request_context('/', environ_base={'REMOTE_ADDR': '10.0.0.5'}):
            g.user_id = 7  # DB primary-key style
            self.assertEqual(self.limiter._get_key('goal_create'),
                             'rl:goal_create:user:7')

    def test_per_ip_key_when_no_user_id(self):
        with self.app.test_request_context('/', environ_base={'REMOTE_ADDR': '10.0.0.5'}):
            # g.user_id deliberately unset -> IP scoping
            self.assertEqual(self.limiter._get_key('chat'), 'rl:chat:ip:10.0.0.5')

    def test_ip_unknown_when_remote_addr_none(self):
        with self.app.test_request_context('/'):
            with patch('security.rate_limiter_redis.request') as mreq:
                mreq.remote_addr = None
                self.assertEqual(self.limiter._get_key('chat'), 'rl:chat:ip:unknown')

    def test_user_scope_isolates_from_ip_scope(self):
        """A logged-in user and an anonymous IP on the SAME action get distinct buckets."""
        # Anonymous caller from an IP exhausts nothing that touches the user bucket.
        with self.app.test_request_context('/', environ_base={'REMOTE_ADDR': '9.9.9.9'}):
            ip_key = self.limiter._get_key('post')
        with self.app.test_request_context('/', environ_base={'REMOTE_ADDR': '9.9.9.9'}):
            g.user_id = 'alice'
            user_key = self.limiter._get_key('post')
        self.assertNotEqual(ip_key, user_key)
        self.assertEqual(ip_key, 'rl:post:ip:9.9.9.9')
        self.assertEqual(user_key, 'rl:post:user:alice')


class TestUnknownActionAndRetry(unittest.TestCase):
    """Unknown action names fall back to the 'global' ceiling, not to unlimited."""

    def setUp(self):
        self.limiter = RedisRateLimiter()
        self.limiter._redis = None
        self.limiter._memory_store.clear()

    def test_unknown_action_uses_global_ceiling(self):
        # global = (60, 60). An unknown action must still be capped at 60.
        with patch.object(self.limiter, '_get_key', return_value='rl:mystery:ip:x'):
            for _ in range(60):
                self.assertTrue(self.limiter.check('does_not_exist'))
            self.assertFalse(
                self.limiter.check('does_not_exist'),
                "unknown action must inherit the global cap, not be unlimited",
            )

    def test_retry_after_unknown_action_is_global_window(self):
        self.assertEqual(self.limiter.get_retry_after('does_not_exist'), 60)

    def test_retry_after_known_action(self):
        self.assertEqual(self.limiter.get_retry_after('goal_create'), 3600)


class TestRateLimitDecorator(unittest.TestCase):
    """End-to-end decorator behaviour: pass-through under limit, 429 over limit."""

    def setUp(self):
        self.app = Flask(__name__)
        self.limiter = get_rate_limiter()  # decorator uses the singleton
        self.limiter._redis = None
        self.limiter._memory_store.clear()

    def test_calls_wrapped_fn_when_under_limit(self):
        @rate_limit('auth')
        def handler():
            return 'ok'

        with self.app.test_request_context('/'):
            self.assertEqual(handler(), 'ok')

    def test_returns_429_with_retry_after_when_over_limit(self):
        @rate_limit('auth')  # (10, 60)
        def handler():
            return 'ok'

        with self.app.test_request_context('/'):  # stable REMOTE_ADDR 127.0.0.1
            for _ in range(10):
                self.assertEqual(handler(), 'ok')
            resp = handler()
            self.assertEqual(resp.status_code, 429)
            self.assertEqual(resp.headers['Retry-After'], '60')
            self.assertEqual(resp.get_json()['error'], 'Rate limit exceeded')
            self.assertEqual(resp.get_json()['retry_after'], 60)

    def test_decorator_fails_closed_on_redis_outage(self):
        """Even with a broken redis, the decorator must still hand out 429s."""
        broken = MagicMock()
        broken.pipeline.side_effect = ConnectionError('redis down')
        self.limiter._redis = broken
        self.limiter._memory_store.clear()

        @rate_limit('auth')
        def handler():
            return 'ok'

        with self.app.test_request_context('/'):
            for _ in range(10):
                self.assertEqual(handler(), 'ok')
            resp = handler()
            self.assertEqual(
                resp.status_code, 429,
                "decorator must fail CLOSED (429) during a redis outage",
            )


if __name__ == '__main__':
    unittest.main()
