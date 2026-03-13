"""
Redis-backed Distributed Rate Limiter
Sliding window counter with composite key (user_id + IP).
Falls back to in-memory when Redis is unavailable.
"""

import os
import time
import logging
from collections import defaultdict
from functools import wraps
from typing import Optional

from flask import request, jsonify, g

logger = logging.getLogger('hevolve_security')


class RedisRateLimiter:
    """
    Rate limiter with Redis backend and in-memory fallback.
    Uses sliding window counter algorithm.
    """

    # Default limits per action type
    LIMITS = {
        'global': (60, 60),          # 60 requests per 60 seconds
        'auth': (10, 60),            # 10 auth attempts per 60 seconds
        'search': (30, 60),          # 30 searches per 60 seconds
        'post': (10, 60),            # 10 posts per 60 seconds
        'comment': (20, 60),         # 20 comments per 60 seconds
        'vote': (60, 60),            # 60 votes per 60 seconds
        'bot_register': (5, 300),    # 5 registrations per 5 minutes
        'discover': (10, 60),        # 10 discovery calls per 60 seconds
        'chat': (30, 60),            # 30 chat requests per 60 seconds
        'goal_create': (10, 3600),   # 10 goals per user per hour
        'remote_desktop': (30, 60),  # 30 connections per 60 seconds
        'remote_desktop_auth': (5, 60),  # 5 failed auth attempts per 60 seconds
        'shell_ops': (30, 60),          # 30 shell operations per 60 seconds
        'shell_file_ops': (20, 60),     # 20 file operations per 60 seconds
        'shell_terminal': (10, 60),     # 10 terminal sessions per 60 seconds
        'shell_power': (3, 60),         # 3 power actions per 60 seconds
        'app_install': (5, 3600),       # 5 installs per hour
        'sharing': (20, 60),            # 20 shares per 60 seconds
        'gamification': (30, 60),       # 30 gamification calls per 60 seconds
        'games': (20, 60),              # 20 game operations per 60 seconds
        'mcp': (30, 60),                # 30 MCP operations per 60 seconds
        'tts': (10, 60),                # 10 TTS generations per 60 seconds
        'tts_speak': (20, 60),              # 20 TTS speak requests per 60 seconds
        'tts_clone': (5, 3600),             # 5 voice clones per hour
        'civic_sentinel': (20, 60),      # 20 civic sentinel ops per 60 seconds
        'p2p_rideshare': (30, 60),       # 30 ride ops per 60 seconds
        'p2p_marketplace': (30, 60),     # 30 marketplace ops per 60 seconds
        'p2p_grocery': (20, 60),         # 20 grocery ops per 60 seconds
        'p2p_food': (20, 60),            # 20 food delivery ops per 60 seconds
        'p2p_bills': (15, 60),           # 15 bill payments per 60 seconds
        'p2p_tickets': (15, 60),         # 15 ticket bookings per 60 seconds
        'p2p_freelance': (20, 60),       # 20 freelance ops per 60 seconds
        'p2p_tutoring': (20, 60),        # 20 tutoring ops per 60 seconds
        'p2p_services': (20, 60),        # 20 service ops per 60 seconds
        'p2p_rental': (20, 60),          # 20 rental ops per 60 seconds
        'p2p_health': (15, 60),          # 15 health ops per 60 seconds
        'p2p_logistics': (20, 60),       # 20 logistics ops per 60 seconds
        'autoresearch': (5, 3600),       # 5 autoresearch sessions per hour
        'wifi': (30, 60),                # 30 wifi operations per 60 seconds
        'vpn': (20, 60),                 # 20 vpn operations per 60 seconds
        'trash': (30, 60),               # 30 trash operations per 60 seconds
        'battery': (60, 60),             # 60 battery queries per 60 seconds
        'webcam': (10, 60),              # 10 webcam operations per 60 seconds
        'scanner': (5, 60),              # 5 scanner operations per 60 seconds
        'video_gen': (5, 300),           # 5 video generations per 5 minutes
        'keyboard': (20, 60),            # 20 keyboard operations per 60 seconds
        'app_permissions': (10, 60),     # 10 permission changes per 60 seconds
        'file_tags': (30, 60),           # 30 file tag operations per 60 seconds
        'hotspot': (10, 60),             # 10 hotspot operations per 60 seconds
        'weather': (10, 60),             # 10 weather queries per 60 seconds
        'auto_update': (3, 3600),        # 3 update runs per hour
        'dns': (5, 60),                  # 5 DNS config changes per 60 seconds
        'sso': (5, 60),                  # 5 SSO operations per 60 seconds
        'email': (10, 60),               # 10 email operations per 60 seconds
        'voice_control': (30, 60),       # 30 voice operations per 60 seconds
        'screen_rotation': (10, 60),     # 10 rotation changes per 60 seconds
    }

    def __init__(self):
        self._redis = None
        self._memory_store: dict = defaultdict(list)
        self._init_redis()

    def _init_redis(self):
        try:
            import redis
            redis_url = os.environ.get(
                'REDIS_RATE_LIMIT_URL',
                os.environ.get('REDIS_URL', 'redis://localhost:6379/1')
            )
            self._redis = redis.from_url(
                redis_url, decode_responses=True,
                socket_timeout=3, socket_connect_timeout=2,
                socket_keepalive=True, retry_on_timeout=True,
            )
            self._redis.ping()
            logger.info("Redis rate limiter connected")
        except Exception as e:
            self._redis = None
            logger.info(f"Redis unavailable, using in-memory rate limiter: {e}")

    def _get_key(self, action: str) -> str:
        """Build composite rate limit key from user_id + IP."""
        user_id = getattr(g, 'user_id', None) if hasattr(g, 'user_id') else None
        ip = request.remote_addr or 'unknown'

        if user_id:
            return f"rl:{action}:user:{user_id}"
        return f"rl:{action}:ip:{ip}"

    def check(self, action: str = 'global') -> bool:
        """
        Check if request is within rate limit.
        Returns True if allowed, False if rate limited.
        """
        max_requests, window = self.LIMITS.get(action, self.LIMITS['global'])
        key = self._get_key(action)

        if self._redis:
            return self._check_redis(key, max_requests, window)
        return self._check_memory(key, max_requests, window)

    def _check_redis(self, key: str, max_requests: int, window: int) -> bool:
        """Redis sliding window counter."""
        try:
            now = time.time()
            # Step 1: clean old entries and count current
            pipe = self._redis.pipeline()
            pipe.zremrangebyscore(key, 0, now - window)
            pipe.zcard(key)
            results = pipe.execute()

            current_count = results[1]
            if current_count >= max_requests:
                return False

            # Step 2: only record if under limit
            self._redis.zadd(key, {str(now): now})
            self._redis.expire(key, window + 1)
            return True
        except Exception as e:
            logger.warning(f"Redis rate limit check failed, falling back to memory: {e}")
            # Fail-closed: fall back to in-memory limiter, NOT open allow
            return self._check_memory(key, max_requests, window)

    def _check_memory(self, key: str, max_requests: int, window: int) -> bool:
        """In-memory sliding window (fallback)."""
        now = time.time()
        # Clean old entries
        self._memory_store[key] = [
            t for t in self._memory_store[key] if t > now - window
        ]
        if len(self._memory_store[key]) >= max_requests:
            return False
        self._memory_store[key].append(now)
        return True

    def get_retry_after(self, action: str = 'global') -> int:
        """Get seconds until rate limit resets."""
        _, window = self.LIMITS.get(action, self.LIMITS['global'])
        return window


# Singleton instance
_limiter = None


def get_rate_limiter() -> RedisRateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RedisRateLimiter()
    return _limiter


def rate_limit(action: str = 'global'):
    """
    Flask decorator for rate limiting.
    Usage: @rate_limit('search')
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            limiter = get_rate_limiter()
            if not limiter.check(action):
                retry_after = limiter.get_retry_after(action)
                response = jsonify({
                    'error': 'Rate limit exceeded',
                    'retry_after': retry_after,
                })
                response.status_code = 429
                response.headers['Retry-After'] = str(retry_after)
                return response
            return f(*args, **kwargs)
        return decorated
    return decorator
