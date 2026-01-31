"""
HevolveSocial - Rate Limiter
In-memory token bucket with Redis fallback.
"""
import time
import threading
from functools import wraps
from flask import request, g, jsonify


class TokenBucket:
    """Thread-safe in-memory token bucket rate limiter."""

    def __init__(self):
        self._buckets = {}  # key -> (tokens, last_refill)
        self._lock = threading.Lock()

    def _get_key(self, user_id: str, action: str) -> str:
        return f"{user_id}:{action}"

    def check(self, user_id: str, action: str, max_tokens: int, refill_rate: float) -> bool:
        """
        Check if action is allowed. Returns True if allowed, False if rate-limited.
        refill_rate = tokens added per second.
        """
        key = self._get_key(user_id, action)
        now = time.time()

        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = (max_tokens - 1, now)
                return True

            tokens, last_refill = self._buckets[key]
            elapsed = now - last_refill
            tokens = min(max_tokens, tokens + elapsed * refill_rate)

            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                return True
            else:
                self._buckets[key] = (tokens, now)
                return False

    def cleanup(self, max_age: float = 3600):
        """Remove stale entries older than max_age seconds."""
        now = time.time()
        with self._lock:
            stale = [k for k, (_, t) in self._buckets.items() if now - t > max_age]
            for k in stale:
                del self._buckets[k]


_limiter = TokenBucket()

# Rate limit configurations
LIMITS = {
    'global':   {'max_tokens': 100, 'refill_rate': 100 / 60},     # 100 req/min
    'post':     {'max_tokens': 1,   'refill_rate': 1 / 1800},     # 1 post/30min
    'comment':  {'max_tokens': 50,  'refill_rate': 50 / 3600},    # 50 comments/hr
    'vote':     {'max_tokens': 60,  'refill_rate': 60 / 60},      # 60 votes/min
    'search':   {'max_tokens': 30,  'refill_rate': 30 / 60},      # 30 searches/min
}


def rate_limit(action: str = 'global'):
    """Decorator: rate-limits an endpoint. Requires g.user to be set."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user_id = getattr(g, 'user', None)
            if user_id is None:
                # Use IP as fallback for unauthenticated requests
                user_id = request.remote_addr or 'anonymous'
            else:
                user_id = g.user.id

            # Check global limit first
            cfg = LIMITS.get('global', LIMITS['global'])
            if not _limiter.check(str(user_id), 'global', cfg['max_tokens'], cfg['refill_rate']):
                return jsonify({
                    'success': False,
                    'error': 'Rate limit exceeded. Try again later.'
                }), 429

            # Check action-specific limit
            if action != 'global' and action in LIMITS:
                cfg = LIMITS[action]
                if not _limiter.check(str(user_id), action, cfg['max_tokens'], cfg['refill_rate']):
                    return jsonify({
                        'success': False,
                        'error': f'Rate limit exceeded for {action}. Try again later.'
                    }), 429

            return f(*args, **kwargs)
        return decorated
    return decorator


def get_limiter() -> TokenBucket:
    return _limiter
