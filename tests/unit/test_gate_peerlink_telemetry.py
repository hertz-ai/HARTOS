"""peerlink-telemetry yield gate: CentralConnection._telemetry_loop used to run
its heavy work (_try_connect + _publish_telemetry) every interval regardless of
whether a live user / hot box needed the machine. It now consults the ONE
canonical gate ``core.foreground.should_yield_to_user`` (imported into this
module) at the top of each iteration and SKIPS the tick when it returns True —
while still sleeping one full interval before re-checking (no busy-spin), and
never bypassing ``stop()``.

Behavioural, not grep: builds the real CentralConnection, mocks every I/O
boundary (network publish, connect), drives exactly ONE real loop iteration with
the sleep primitive patched (so the loop exits after one tick and never sleeps
for real), and asserts on whether the heavy work was actually invoked.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.peer_link.telemetry as tel  # noqa: E402


def _build_conn(monkeypatch):
    """A CentralConnection with all I/O boundaries mocked and the sleep
    primitive replaced by a one-shot that stops the loop after a single
    iteration (so ``_telemetry_loop`` runs exactly once, with no real sleep)."""
    conn = tel.CentralConnection()
    conn._running = True
    conn._node_id = 'test-node'

    sleep_calls = {'n': 0}

    def fake_sleep(seconds):
        # Record the back-off, then stop the loop so it runs exactly one tick.
        sleep_calls['n'] += 1
        sleep_calls['last_seconds'] = seconds
        conn._running = False

    # Replace the loop's OWN sleep primitive — no real time passes, and one
    # call ends the loop.
    monkeypatch.setattr(conn, '_sleep_interval', fake_sleep)

    # _try_connect would reach for crossbar_server / message_bus; stub it to
    # simply mark connected so the non-yield path reaches _publish_telemetry.
    def fake_connect():
        conn._connected = True
    monkeypatch.setattr(conn, '_try_connect', fake_connect)

    # The heavy work we are gating — observe whether it is invoked.
    publish_calls = {'n': 0}

    def fake_publish():
        publish_calls['n'] += 1
    monkeypatch.setattr(conn, '_publish_telemetry', fake_publish)

    # Control drain is a no-op poll; stub so it never touches the network.
    monkeypatch.setattr(conn, '_check_control_messages', lambda: None)

    return conn, publish_calls, sleep_calls


def test_skips_heavy_work_when_yielding(monkeypatch):
    """User active / box hot -> gate True -> _publish_telemetry NOT called,
    but the loop STILL sleeps one interval (no bare-continue busy-spin)."""
    monkeypatch.setattr(tel, 'should_yield_to_user', lambda: True)
    conn, publish_calls, sleep_calls = _build_conn(monkeypatch)

    conn._telemetry_loop()  # runs exactly one iteration (fake_sleep stops it)

    assert publish_calls['n'] == 0, (
        "heavy work _publish_telemetry() ran while yielding to the user")
    # The defer MUST back off via the loop's sleep primitive — proves it is not
    # a busy-spinning bare `continue`.
    assert sleep_calls['n'] == 1, (
        "yield path did not sleep -> busy-spin (the bug being removed)")
    assert sleep_calls['last_seconds'] == conn._telemetry_interval, (
        "yield back-off did not preserve the loop's interval cadence")


def test_runs_heavy_work_when_not_yielding(monkeypatch):
    """User idle -> gate False -> the tick runs and _publish_telemetry IS
    attempted (cadence preserved: still one interval sleep afterwards)."""
    monkeypatch.setattr(tel, 'should_yield_to_user', lambda: False)
    conn, publish_calls, sleep_calls = _build_conn(monkeypatch)

    conn._telemetry_loop()  # runs exactly one iteration

    assert publish_calls['n'] == 1, (
        "heavy work _publish_telemetry() was NOT attempted on the idle path")
    assert sleep_calls['n'] == 1, "normal cadence sleep missing after a tick"


def test_stop_still_honoured_under_yield(monkeypatch):
    """stop() semantics survive the gate: if _running is already False the loop
    body never executes — neither sleep nor publish — even with gate True."""
    monkeypatch.setattr(tel, 'should_yield_to_user', lambda: True)
    conn, publish_calls, sleep_calls = _build_conn(monkeypatch)
    conn._running = False  # simulate stop() before the loop body

    conn._telemetry_loop()

    assert sleep_calls['n'] == 0
    assert publish_calls['n'] == 0
