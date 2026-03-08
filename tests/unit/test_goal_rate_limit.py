"""
Tests for goal rate limiting via security/rate_limiter_redis.py.

Run: pytest tests/unit/test_goal_rate_limit.py -v --noconftest
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from security.rate_limiter_redis import RedisRateLimiter


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


if __name__ == '__main__':
    unittest.main()
