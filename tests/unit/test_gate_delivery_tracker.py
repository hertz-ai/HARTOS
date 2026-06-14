"""Behavioural test for the DeliveryTracker._cleanup_loop foreground yield gate.

DeliveryTracker._cleanup_loop runs a background TTL-expiry sweep every 15s.
When a genuine user request holds the foreground (box is hot), the loop must
SKIP that tick's heavy work (the TTL-expiry sweep + its notification emit +
FCM push) so it does not steal cycles from the user — while still sleeping its
own cadence (no busy-spin).

This test drives EXACTLY ONE loop iteration with all I/O boundaries mocked:
  * ``local_subscribers.time.sleep`` is replaced so the loop runs once then
    flips ``_running`` False (no real sleep, deterministic single tick).
  * ``local_subscribers.should_yield_to_user`` (this module's binding) is the
    single canonical gate under test.
  * ``core.platform.events.emit_event`` + ``core.fcm_sync.send_fcm_push`` are
    mocked — these are the observable side-effects of the heavy work.

Heavy-work signal: an already-expired pending entry. When the sweep RUNS it
pops that entry and calls ``emit_event``; when the sweep is SKIPPED the entry
survives and ``emit_event`` is never called.
"""

import time
from unittest.mock import MagicMock

import pytest

from core.peer_link import local_subscribers
from core.peer_link.local_subscribers import DeliveryTracker, _DELIVERY_TTL


def _run_one_tick(monkeypatch, *, yield_value, emit_mock, fcm_mock):
    """Drive the real _cleanup_loop for exactly one iteration.

    Seeds one already-expired pending entry, patches all boundaries, sets the
    yield gate to ``yield_value``, and patches ``time.sleep`` to stop the loop
    after the first (no-op) sleep so the single iteration's gate branch runs.
    Returns the tracker so the caller can inspect ``_pending``.
    """
    tracker = DeliveryTracker()
    tracker._running = True

    # Seed an entry whose age already exceeds the TTL so the sweep WOULD act.
    expired_key = 'req-expired'
    tracker._pending[expired_key] = {
        'topic': 'task.confirmation',
        'topic_name': 'task.confirmation',
        'timestamp': time.time() - (_DELIVERY_TTL + 100),
        'user_id': 'user-42',
    }

    # The single canonical gate (patch THIS module's binding).
    monkeypatch.setattr(local_subscribers, 'should_yield_to_user',
                        lambda: yield_value)

    # No real sleep; flip _running off so the while-loop body runs exactly once.
    def _fake_sleep(_seconds):
        tracker._running = False
    monkeypatch.setattr(local_subscribers.time, 'sleep', _fake_sleep)

    # Heavy-work side-effect boundaries (imported lazily inside the loop).
    monkeypatch.setattr('core.platform.events.emit_event', emit_mock)
    monkeypatch.setattr('core.fcm_sync.send_fcm_push', fcm_mock)

    # Run the real loop — _fake_sleep guarantees a single iteration.
    tracker._cleanup_loop()
    return tracker, expired_key


def test_cleanup_loop_skips_sweep_when_user_active(monkeypatch):
    """Gate True → the TTL-expiry sweep (heavy work) is NOT performed this tick."""
    emit_mock = MagicMock()
    fcm_mock = MagicMock()

    tracker, expired_key = _run_one_tick(
        monkeypatch, yield_value=True, emit_mock=emit_mock, fcm_mock=fcm_mock)

    # Heavy work skipped: no notification emitted, no FCM push.
    emit_mock.assert_not_called()
    fcm_mock.assert_not_called()
    # The expired entry survives — the sweep that would pop it never ran.
    assert expired_key in tracker._pending


def test_cleanup_loop_runs_sweep_when_user_idle(monkeypatch):
    """Gate False → the TTL-expiry sweep IS attempted (entry swept + emit fired)."""
    emit_mock = MagicMock()
    fcm_mock = MagicMock()

    tracker, expired_key = _run_one_tick(
        monkeypatch, yield_value=False, emit_mock=emit_mock, fcm_mock=fcm_mock)

    # Heavy work attempted: the expired entry was swept out of _pending...
    assert expired_key not in tracker._pending
    # ...and the unconfirmed-delivery notification was emitted for it.
    emit_mock.assert_called_once()
    topic_arg = emit_mock.call_args.args[0]
    assert topic_arg == 'notification.unconfirmed'
    # ...and the FCM fallback was attempted (entry carried a user_id).
    fcm_mock.assert_called_once()


def test_cleanup_loop_defer_does_not_busy_spin(monkeypatch):
    """A deferred tick still consumes the loop's own sleep cadence (no busy-spin).

    The yield branch must NOT be a bare ``continue`` that re-loops without
    sleeping. We assert that on a yielding iteration ``time.sleep`` is invoked
    with the loop's existing cadence before the gate short-circuits — i.e. the
    deferral is paced by the loop's own primitive.
    """
    sleep_calls = []
    tracker = DeliveryTracker()
    tracker._running = True

    monkeypatch.setattr(local_subscribers, 'should_yield_to_user', lambda: True)

    def _record_sleep(seconds):
        sleep_calls.append(seconds)
        tracker._running = False  # stop after one iteration
    monkeypatch.setattr(local_subscribers.time, 'sleep', _record_sleep)
    monkeypatch.setattr('core.platform.events.emit_event', MagicMock())
    monkeypatch.setattr('core.fcm_sync.send_fcm_push', MagicMock())

    tracker._cleanup_loop()

    # Exactly one paced sleep at the loop's existing 15s cadence occurred even
    # though the heavy work was deferred — proving the defer is not a busy-spin.
    assert sleep_calls == [15]


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
