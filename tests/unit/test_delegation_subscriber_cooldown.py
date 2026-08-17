"""The delegation-subscriber bootstrap must not re-probe a dead Redis on every
request.

before_request runs ahead of @require_auth, so an expensive bootstrap is
reachable unauthenticated.  Measured on the live node 2026-08-16:
/api/distributed/* returned 401 in 13.8-15.0s; unauthenticated control routes
on the same app answered in ~20ms.

Two faults caused it, and both are covered here:

1. _ensure_delegation_subscriber() set _delegation_sub_started = True only on
   full success.  Failure paths returned False without recording the attempt,
   so the `if not _delegation_sub_started` guard re-fired on every request.

2. It holds _delegation_sub_lock across the probe, so concurrent callers
   serialise behind each 15s timeout -- N requests tie up N threads.  A
   cooldown alone doesn't fix that; the first attempt in each window still
   blocks everyone.  before_request must not block on the lock.

The 15s itself (context, not asserted here): redis-py >= 6.0 ignores
retry_on_timeout=False, so a 1s socket timeout becomes ~15s of retries against
a localhost:6379 that drops rather than refuses.  Separate defect, 3 sites.
"""
import threading
import time

import pytest

from integrations.distributed_agent import api as dapi


@pytest.fixture(autouse=True)
def _reset_subscriber_state():
    """Each test starts from a clean, never-bootstrapped state."""
    dapi._delegation_sub_started = False
    dapi._delegation_sub_last_attempt = 0.0
    yield
    dapi._delegation_sub_started = False
    dapi._delegation_sub_last_attempt = 0.0


def test_dead_redis_is_probed_once_not_every_call(monkeypatch):
    """THE DoS FIX: a dead Redis must be probed once, then short-circuit.

    Before the fix this counter incremented on every single call, which is
    exactly what made each /api/distributed/* request cost a full Redis
    timeout.
    """
    probes = []

    def _slow_dead_redis():
        probes.append(time.monotonic())
        raise ConnectionError("redis down")

    monkeypatch.setattr(dapi, '_get_redis_client', _slow_dead_redis)

    for _ in range(25):
        assert dapi._ensure_delegation_subscriber() is False

    assert len(probes) == 1, (
        f"dead Redis was probed {len(probes)}x across 25 calls -- the cooldown "
        f"is not latching, so every request pays a Redis timeout (the DoS)"
    )


def test_failure_does_not_claim_subscriber_is_active(monkeypatch):
    """The cooldown must NOT be implemented by setting _delegation_sub_started.

    That flag is REPORTED to clients as 'delegation_subscriber_active'
    (api.py:432).  Latching it on failure would make the endpoint lie.
    """
    monkeypatch.setattr(
        dapi, '_get_redis_client',
        lambda: (_ for _ in ()).throw(ConnectionError("redis down")))

    dapi._ensure_delegation_subscriber()

    assert dapi._delegation_sub_started is False, (
        "_delegation_sub_started must stay False when the subscriber failed to "
        "start -- it is surfaced as 'delegation_subscriber_active'")


def test_cooldown_expires_so_a_late_redis_is_still_picked_up(monkeypatch):
    """Recovery: the cooldown is a backoff, not a permanent disable.

    Redis may legitimately appear after the app boots.  A sticky 'never retry'
    would strand the subscriber forever.
    """
    probes = []

    def _dead():
        probes.append(1)
        raise ConnectionError("redis down")

    monkeypatch.setattr(dapi, '_get_redis_client', _dead)
    monkeypatch.setattr(dapi, '_DELEGATION_RETRY_COOLDOWN_S', 0.05)

    dapi._ensure_delegation_subscriber()
    assert len(probes) == 1

    dapi._ensure_delegation_subscriber()
    assert len(probes) == 1, "still inside cooldown -- must not re-probe"

    time.sleep(0.08)
    dapi._ensure_delegation_subscriber()
    assert len(probes) == 2, "cooldown expired -- a late Redis must get a retry"


def test_before_request_never_blocks_on_the_bootstrap_lock():
    """THREAD-EXHAUSTION FIX: before_request must not queue behind the probe.

    Cooldown alone is insufficient.  On the first attempt (and after each
    cooldown expiry) one thread holds the lock for the full Redis timeout; if
    before_request blocks, every concurrent unauthenticated request ties up a
    worker thread for that whole window.
    """
    dapi._delegation_sub_lock.acquire()
    try:
        done = threading.Event()

        def _call():
            dapi._bp_before_request_ensure_subscriber()
            done.set()

        threading.Thread(target=_call, daemon=True).start()

        assert done.wait(timeout=2.0), (
            "before_request blocked while the bootstrap lock was held -- "
            "concurrent unauthenticated requests would exhaust the thread pool")
    finally:
        dapi._delegation_sub_lock.release()


def test_success_still_latches_and_short_circuits(monkeypatch):
    """Zero-regression: the happy path must be unchanged.

    On success the flag latches, the public field reads True, and no further
    probing happens.
    """
    probes = []

    class _LiveRedis:
        def ping(self):
            return True

    def _live():
        probes.append(1)
        return _LiveRedis()

    class _FakePubSub:
        CHANNEL_DELEGATION = 'delegation'

        def __init__(self, client, agent_id=None):
            pass

        def subscribe(self, channels, handler):
            return True

    monkeypatch.setattr(dapi, '_get_redis_client', _live)
    import agent_ledger.pubsub as _ps
    monkeypatch.setattr(_ps, 'LedgerPubSub', _FakePubSub)

    assert dapi._ensure_delegation_subscriber() is True
    assert dapi._delegation_sub_started is True

    for _ in range(10):
        assert dapi._ensure_delegation_subscriber() is True
    assert len(probes) == 1, "a started subscriber must never re-probe Redis"
