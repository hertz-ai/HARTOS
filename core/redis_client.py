"""Fail-fast Redis clients for availability probes.

Same policy core.http_pool already states for HTTP:

    localhost: 0 retries (dead local services should fail instantly,
    not block 15s)

The Redis call sites never got that treatment, and a dependency bump silently
took it away from the ones that asked for it.

redis-py >= 6.0 deprecated ``retry_on_timeout`` and IGNORES it -- the warning
reads "Call to '__init__' function with deprecated usage of input argument/s
'retry_on_timeout'. (TimeoutError is included by default.)".  Timeouts are now
retried by default, so ``retry_on_timeout=False`` no longer disables anything.

Measured on redis-py 7.1.0, pinging a localhost port that DROPS rather than
refuses (a refused port fails in milliseconds and hides this):

    retry_on_timeout=False        13.78s
    retry=Retry(NoBackoff(), 0)    2.02s
    retry=None                     2.00s

Four call sites passed the ignored kwarg with socket_connect_timeout=1 and paid
~14s per probe instead of ~1s.  One of them ran on every /api/distributed/*
request ahead of the auth check.

Use ``probe_client()`` for "is Redis there?" checks.  Anything that wants
resilience instead of speed -- security.rate_limiter_redis deliberately sets
retry_on_timeout=True -- should NOT use this module.
"""

import logging
import os
from typing import Any, Optional

logger = logging.getLogger('hevolve_core')

# Matches the tightest of the four original call sites.  A probe that cannot
# answer in a second is indistinguishable from absent for our purposes.
DEFAULT_CONNECT_TIMEOUT_S = 1
DEFAULT_SOCKET_TIMEOUT_S = 2


def probe_client(
    host: Optional[str] = None,
    port: Optional[int] = None,
    db: int = 0,
    *,
    socket_connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_S,
    socket_timeout: int = DEFAULT_SOCKET_TIMEOUT_S,
    decode_responses: bool = True,
    **kwargs: Any,
):
    """Return a Redis client that fails fast, or None if redis isn't installed.

    ``host``/``port`` default to REDIS_HOST / REDIS_PORT, then localhost:6379,
    which is what three of the four original sites did by hand.

    Returns None rather than raising on ImportError so callers keep the
    "no redis -> feature unavailable, app still works" behaviour they already
    had.  Connection failure is NOT swallowed here: the caller pings and
    handles it, same as before.

    retry=None is what actually disables retries on redis-py >= 6.0; see the
    module docstring for the measurements.
    """
    try:
        import redis
    except ImportError:
        return None

    if host is None:
        host = os.environ.get('REDIS_HOST', 'localhost')
    if port is None:
        port = int(os.environ.get('REDIS_PORT', 6379))

    return redis.Redis(
        host=host,
        port=port,
        db=db,
        decode_responses=decode_responses,
        socket_connect_timeout=socket_connect_timeout,
        socket_timeout=socket_timeout,
        retry=None,
        **kwargs,
    )
