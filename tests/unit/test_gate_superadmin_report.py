"""Behavioural guard: the superadmin report-in background loop yields the
box to a live user / hot machine.

This is NOT a grep/source-shape test.  It imports the real module, mocks
the network/disk/watchdog boundaries (so no real I/O), drives exactly one
real loop iteration via the extracted ``_loop_once`` (which calls the REAL
``should_yield_to_user`` gate, the REAL heartbeat call, and the REAL sleep
primitive), and asserts the observable side-effects:

  * yield == True  -> heavy work (report_join + drain_outbox) is SKIPPED,
                      the watchdog heartbeat STILL fires, and the loop
                      sleeps one interval (no bare-``continue`` busy-spin).
  * yield == False -> heavy work IS attempted (report_join called with the
                      node_info, drain_outbox called), heartbeat fires, and
                      the loop sleeps one interval.

Covers the regression: the loop used to run report_join + drain_outbox on
every tick regardless of user activity.
"""
import importlib
from unittest import mock

import pytest

import core.superadmin_report as sr


@pytest.fixture
def patched(monkeypatch):
    """Mock every boundary of the loop so one iteration does zero real I/O.

    Returns the bag of mocks the tests assert against.  ``time.sleep`` is
    patched on THIS module so the iteration never actually blocks, and the
    call is recorded so we can prove the defer/work paths both sleep (i.e.
    never busy-spin with a bare ``continue``)."""
    # Heavy work — the two calls the gate must skip on yield.
    m_report_join = mock.Mock(return_value=1)
    m_drain_outbox = mock.Mock(return_value=0)
    # Watchdog heartbeat — the call that MUST fire every iteration.
    m_heartbeat = mock.Mock()
    # The loop's own sleep primitive — patched so no real wall-clock wait.
    m_sleep = mock.Mock()

    monkeypatch.setattr(sr, "report_join", m_report_join)
    monkeypatch.setattr(sr, "drain_outbox", m_drain_outbox)
    monkeypatch.setattr(sr, "_heartbeat_safe", m_heartbeat)
    monkeypatch.setattr(sr.time, "sleep", m_sleep)

    return {
        "report_join": m_report_join,
        "drain_outbox": m_drain_outbox,
        "heartbeat": m_heartbeat,
        "sleep": m_sleep,
    }


def _node_info():
    """A node_info dict that satisfies the report_join precondition
    (``info.get('node_id')`` truthy)."""
    return {"node_id": "node-test-123", "name": "test-node"}


def test_yield_skips_heavy_work_but_heartbeat_still_fires(monkeypatch, patched):
    # Gate says: user is active / box is hot -> yield this tick.
    monkeypatch.setattr(sr, "should_yield_to_user", lambda: True)

    # last_report=0.0 would normally force a report (interval long elapsed),
    # so if the gate is honoured the SKIP is what proves the behaviour.
    new_last_report = sr._loop_once(_node_info, 0.0)

    # Heavy work skipped entirely.
    patched["report_join"].assert_not_called()
    patched["drain_outbox"].assert_not_called()
    # Heartbeat MUST still fire on a yielded tick (frozen-thread guard).
    patched["heartbeat"].assert_called_once()
    # Deferred ticks must SLEEP (reuse the loop's primitive) — never a bare
    # ``continue`` that would busy-spin a core.
    patched["sleep"].assert_called_once_with(sr.REPORT_OUTBOX_RETRY_SEC)
    # Cadence carried forward unchanged when we skip the report.
    assert new_last_report == 0.0


def test_no_yield_runs_heavy_work_and_heartbeat(monkeypatch, patched):
    # Gate says: idle / cool -> do the work.
    monkeypatch.setattr(sr, "should_yield_to_user", lambda: False)

    new_last_report = sr._loop_once(_node_info, 0.0)

    # Heartbeat fires on a working tick too.
    patched["heartbeat"].assert_called_once()
    # Heavy work IS attempted: report_join called with the node_info, and
    # the outbox drained.
    patched["report_join"].assert_called_once_with(_node_info())
    patched["drain_outbox"].assert_called_once()
    # Still sleeps one interval afterwards (same cadence as the yield path).
    patched["sleep"].assert_called_once_with(sr.REPORT_OUTBOX_RETRY_SEC)
    # last_report advanced past 0.0 because a report was attempted this tick.
    assert new_last_report > 0.0


def test_gate_is_the_single_foreground_source(monkeypatch, patched):
    """The loop consults the ONE canonical gate (core.foreground.
    should_yield_to_user) — not a second/parallel predicate.  Patching the
    foreground module's gate (via set_yield_gate) flips the loop's
    behaviour, proving the loop reads exactly that source."""
    import core.foreground as fg

    # Snapshot + restore the process-wide registered gate so this test does
    # not leak state into others.
    saved = fg._yield_gate
    try:
        # Register a yielding gate through the canonical inversion-of-control
        # entrypoint the real dispatch layer uses.
        fg.set_yield_gate(lambda: True)
        sr._loop_once(_node_info, 0.0)
        patched["report_join"].assert_not_called()
        patched["drain_outbox"].assert_not_called()

        # Now register a non-yielding gate -> heavy work runs.
        patched["report_join"].reset_mock()
        patched["drain_outbox"].reset_mock()
        fg.set_yield_gate(lambda: False)
        sr._loop_once(_node_info, 0.0)
        patched["report_join"].assert_called_once()
        patched["drain_outbox"].assert_called_once()
    finally:
        fg._yield_gate = saved


def test_module_imports_cleanly():
    """Sanity: reloading the module (and thus re-running the
    ``from core.foreground import should_yield_to_user`` top-level import)
    raises nothing — guards against a circular-import regression."""
    importlib.reload(sr)
    assert hasattr(sr, "_loop_once")
    assert hasattr(sr, "should_yield_to_user")
