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
from unittest.mock import patch

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


def test_the_audit_write_cannot_hold_the_ui_wake_behind_it(svc):
    """The audit log records the push; it does not decide how fast the UI is.

    ``agent_ui_update`` called ``immutable_audit_log.log_event`` BEFORE it stored
    the component and woke the CV, and log_event does a synchronous SQLAlchemy
    commit and connection close. So every card, every notification and every
    desktop compose waited on a durable write before the SSE producer was even
    told there was anything to send. Measured on a dev box, three consecutive
    pushes took 5.8s, 11.5s and 8.3s against this file's own 0.5s budget, which is
    why the event-driven producer above stopped measuring as event-driven.

    This pins the ordering by its CONSEQUENCE rather than by reading the source: a
    deliberately slow audit sink must not delay the wake. It also pins that the
    audit still happens, so "make it fast" can never quietly become "drop it".
    """
    svc.agent_ui_update('warm', {'type': 'notification', 'title': 'w', 'message': 'w'})

    seen = {}
    slow = threading.Event()

    class _SlowSink:
        def log_event(self, event, **kw):
            seen['event'] = event
            seen['kw'] = kw
            slow.set()
            time.sleep(1.5)          # a stalled disk, a locked db, a slow fsync

    woke_at = {}
    started = threading.Event()

    def producer():
        with svc._ui_event_cv:
            started.set()
            svc._ui_event_cv.wait(timeout=10.0)
        woke_at['t'] = time.monotonic()

    t = threading.Thread(target=producer, daemon=True)
    t.start()
    assert started.wait(2.0), "producer thread never started"
    time.sleep(0.1)

    with patch('security.immutable_audit_log.get_audit_log', return_value=_SlowSink()):
        t0 = time.monotonic()
        assert svc.agent_ui_update(
            'agent-3', {'type': 'notification', 'title': 'x', 'message': 'y'}) is True
        t.join(5.0)

    assert 't' in woke_at, "the SSE condition was never woken"
    assert woke_at['t'] - t0 < 0.5, (
        "the wake waited %.3fs on the audit sink; the audit is back on the push "
        "latency path" % (woke_at['t'] - t0))
    # ...and it really did run, with the same record it always wrote.
    assert slow.is_set(), "the audit was skipped, not merely reordered"
    assert seen['event'] == 'a2ui_push'
    assert seen['kw'].get('actor_id') == 'agent-3'
    assert seen['kw'].get('detail') == {'type': 'notification'}


def test_the_stream_head_flushes_without_waiting_for_a_heartbeat(svc):
    """Opening the stream must not cost a full heartbeat.

    THE BUG, measured on the box 2026-09-07 with a plain urllib client:

        stream open: http=200 content-type=text/event-stream in 15.011s
           15.01s  : hb
           30.01s  : hb

    urlopen returns as soon as the response HEAD arrives, so 15.011s says the
    head did not arrive until the first heartbeat did. Werkzeug does not send
    headers until the generator yields, and the producer loop OPENS with the
    15s CV wait, so on a quiet fleet every page load and every reconnect sat
    unconnected for a full heartbeat before EventSource fired onopen. Same
    channel as the wake latency fixed above, one layer earlier.

    Behavioural: pulls the FIRST chunk out of the REAL route's REAL generator
    with no push pending, which is exactly the quiet-fleet case. If the head
    flush is ever removed this blocks on the CV and the elapsed assertion
    fails rather than the test hanging.
    """
    app = svc._create_flask_app()
    with app.test_request_context('/api/notifications/stream'):
        resp = app.view_functions['notification_stream']()
        stream = iter(resp.response)
        t0 = time.time()
        try:
            first = next(stream)
        finally:
            elapsed = time.time() - t0
            resp.response.close()

    assert elapsed < 2.0, (
        "the SSE head waited %.3fs for the first chunk; the client cannot "
        "know it is connected until then" % elapsed)

    if isinstance(first, bytes):
        first = first.decode('utf-8')
    # An SSE comment: no "event:"/"data:" field, so no browser handler sees it.
    assert first.startswith(':'), (
        "the priming chunk must be an SSE comment, got %r" % first[:40])
    assert first.endswith('\n\n'), "SSE frames terminate on a blank line"
