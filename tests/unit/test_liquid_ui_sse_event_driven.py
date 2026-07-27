"""The /api/notifications/stream SSE producer is EVENT-DRIVEN, not a 2s poll.

2026-07-24 audit (docs/internal/ux_degrading_design_choices_2026-07-24.md #2.1): the
producer used ``while True: time.sleep(2)``, so every A2UI card / notification /
desktop-compose (the "Liquid UI is the heart" path) landed up to 2s late and every
open stream re-scanned all agents every 2s even when idle. The fix wakes the
producer on a dedicated ``threading.Condition`` the instant ``agent_ui_update``
stores a component.

Behavioural: constructs the REAL LiquidUIService, waits on the SAME condition the
real SSE generator blocks on, calls the REAL ``agent_ui_update``, and asserts the
wait is woken PROMPTLY (well under the old 2s poll) AND the component is stored
with the ``_ts`` cursor the producer keys on. No mocks of the code under test.

Run (dev box, targeted):
    python -m pytest tests/unit/test_liquid_ui_sse_event_driven.py -v \
        --noconftest -p no:cacheprovider
"""
import threading
import time

import pytest

from integrations.agent_engine.liquid_ui_service import LiquidUIService


@pytest.fixture
def svc():
    return LiquidUIService()


def test_agent_ui_update_wakes_the_sse_condition_promptly(svc):
    """A push must wake a stream blocked on the CV in well under the old 2s poll."""
    # Warm the lazy singletons agent_ui_update touches on its FIRST call (the audit
    # log's hash-chain load, the guardrail import) so the TIMED push measures the
    # push->notify latency, not one-time process init.
    svc.agent_ui_update('warmup', {'type': 'notification', 'title': 'w', 'message': 'w'})

    woke_at = {}
    started = threading.Event()

    def producer():
        # Mimic the SSE producer's wait on the SAME CV the real generator uses,
        # with a safety timeout far larger than the old poll so a prompt wake is
        # unambiguously the notify firing (not the timeout).
        with svc._ui_event_cv:
            started.set()
            svc._ui_event_cv.wait(timeout=10.0)
        woke_at['t'] = time.monotonic()

    t = threading.Thread(target=producer, daemon=True)
    t.start()
    assert started.wait(2.0), "producer thread never started"
    time.sleep(0.1)  # ensure the producer is inside wait() (CV lock released)

    t0 = time.monotonic()
    ok = svc.agent_ui_update(
        'agent-1', {'type': 'notification', 'title': 'x', 'message': 'hello'})
    assert ok is True, "agent_ui_update was refused (a guard tripped)"

    t.join(3.0)
    assert 't' in woke_at, "the SSE condition was never woken by a push"
    latency = woke_at['t'] - t0
    assert latency < 0.5, (
        f"push -> SSE wake took {latency:.3f}s; not event-driven (old poll was 2s)")


def test_push_is_stored_with_a_cursor_timestamp_for_the_producer(svc):
    """The producer emits components whose ``_ts`` is newer than its cursor, so a
    push that stored no ``_ts`` would be invisible. Prove the real path stamps it."""
    before = time.time()
    assert svc.agent_ui_update(
        'agent-2', {'type': 'notification', 'title': 'x', 'message': 'y'}) is True
    comps = svc._agent_components.get('agent-2') or []
    assert comps, "component was not stored for the SSE stream"
    ts = comps[-1].get('_ts', 0)
    assert ts >= before, "the _ts cursor was not stamped; the producer would never emit it"


def test_no_push_means_the_condition_waits_out_its_timeout(svc):
    """Negative control: with NO push the wait must block to its timeout (no
    busy-spin, no spurious early return) — ``Condition.wait`` returns False on
    timeout, True only on a real notify."""
    t0 = time.monotonic()
    with svc._ui_event_cv:
        woken = svc._ui_event_cv.wait(timeout=0.3)
    elapsed = time.monotonic() - t0
    assert woken is False, "CV reported notified with no push"
    assert elapsed >= 0.28, f"wait returned too early ({elapsed:.3f}s) — busy-spin?"
