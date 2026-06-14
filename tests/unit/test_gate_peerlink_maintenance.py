"""PeerLinkManager._maintenance_loop must yield to a live user / hot box.

Bug: the maintenance loop ran _prune_idle_links() + _attempt_reconnects()
every ~30s regardless of whether the user was actively using the machine,
stealing CPU from a foreground request.

Fix: at the TOP of each iteration it consults the ONE canonical gate
(core.foreground.should_yield_to_user). When the gate says yield, it SKIPS
the heavy work this tick and defers via the loop's OWN sleep primitive
(NOT a bare continue — that would busy-spin, the exact bug being removed),
re-checking after one cadence interval. When the gate is clear, the heavy
work runs as before.

Behavioural test (no grep): the network/db/IO boundaries are mocked
(the two heavy methods are replaced by spies; time.sleep is patched so no
real sleeping happens), THIS module's should_yield_to_user is monkeypatched,
and exactly one loop iteration is driven. Asserts:
  - yield True  -> heavy work NOT called, but the loop still slept (no spin)
  - yield False -> heavy work IS attempted
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.peer_link import link_manager as m  # noqa: E402


def _make_manager(monkeypatch):
    """Build a PeerLinkManager without touching its real I/O boundaries.

    object.__new__ skips __init__ (no tier lookup / no threads). We wire only
    the fields _maintenance_loop reads, and replace the two heavy methods
    (the network/db side-effects) with counting spies.
    """
    mgr = object.__new__(m.PeerLinkManager)
    mgr._running = True

    calls = {'prune': 0, 'reconnect': 0, 'sleep': 0}
    mgr._prune_idle_links = lambda: calls.__setitem__('prune', calls['prune'] + 1)
    mgr._attempt_reconnects = lambda: calls.__setitem__('reconnect', calls['reconnect'] + 1)

    # Drive EXACTLY one iteration: the first time.sleep flips _running False so
    # the `while self._running` loop exits after one tick. Counting the calls
    # also proves the defer path slept (no bare-continue busy-spin) and that the
    # small-increment shutdown check short-circuits the rest of the sleep.
    def _sleep(_secs):
        calls['sleep'] += 1
        mgr._running = False

    monkeypatch.setattr(m.time, 'sleep', _sleep)
    return mgr, calls


def test_maintenance_loop_skips_heavy_work_when_yielding(monkeypatch):
    monkeypatch.setattr(m, 'should_yield_to_user', lambda: True)
    mgr, calls = _make_manager(monkeypatch)

    mgr._maintenance_loop()

    # Heavy work skipped this tick...
    assert calls['prune'] == 0
    assert calls['reconnect'] == 0
    # ...but the loop STILL slept before re-checking — i.e. it deferred via the
    # cadence sleep rather than busy-spinning on a bare `continue`.
    assert calls['sleep'] >= 1


def test_maintenance_loop_runs_heavy_work_when_not_yielding(monkeypatch):
    monkeypatch.setattr(m, 'should_yield_to_user', lambda: False)
    mgr, calls = _make_manager(monkeypatch)

    mgr._maintenance_loop()

    # Gate clear -> the heavy maintenance work is attempted exactly once.
    assert calls['prune'] == 1
    assert calls['reconnect'] == 1
    # And the loop still honoured its cadence sleep afterwards.
    assert calls['sleep'] >= 1
