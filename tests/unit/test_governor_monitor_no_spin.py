"""ResourceGovernor monitor loop must SLEEP its interval, never busy-spin.

The 2026-06-05 "Nunba hung + no TTS" incident: _transition_to(ACTIVE) sets
_cancel_event to back the proactive thread off, but the MONITOR loop slept on
that SAME event and only cleared it in IDLE. So while the mode was ACTIVE (any
foreground request), _cancel_event stayed set, the monitor's wait() returned
instantly every tick, and the loop busy-spun the expensive
_refresh_cpu_attribution() -> psutil.Process.children() walk — pegging a core,
holding the GIL ~continuously, and starving the TTS thread (py-spy caught
ResourceGovernor-Monitor as the sole active+gil thread at 97.7% CPU).

Fix: the monitor sleeps on a DEDICATED _stop_event set only by stop().

These behavioural tests drive the REAL _monitor_loop with the mode pinned
ACTIVE (the exact bug trigger) and assert interval-cadence ticks, not a spin.
No grep tests.
"""
from __future__ import annotations

import os
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.resource_governor as rg
from core.resource_governor import ResourceGovernor, MODE_ACTIVE


def _pin_active_and_count(gov, monkeypatch):
    """Pin the loop into the bug condition (ACTIVE + cancel_event set) and make
    every per-tick op cheap, counting ticks via _refresh_cpu_attribution."""
    gov._mode = MODE_ACTIVE
    gov._cancel_event.set()                      # what _transition_to(ACTIVE) does
    # Neutralise the per-tick watchdog heartbeat: its cold `from
    # security.node_watchdog import get_watchdog` would otherwise dominate the
    # first tick's wall time and skew the cadence measurement. Not under test.
    import security.node_watchdog as _nw
    monkeypatch.setattr(_nw, 'get_watchdog', lambda: None)
    ticks = {'n': 0}
    monkeypatch.setattr(gov, '_refresh_cpu_attribution',
                        lambda: ticks.__setitem__('n', ticks['n'] + 1))
    monkeypatch.setattr(gov, '_get_memory_pressure', lambda: 0.1)
    monkeypatch.setattr(gov, '_detect_user_idle', lambda: False)
    monkeypatch.setattr(gov, '_get_battery_status', lambda: (100, False))
    monkeypatch.setattr(gov, '_target_mode_for', lambda *a, **k: MODE_ACTIVE)
    monkeypatch.setattr(gov, '_transition_to', lambda m: None)  # stay ACTIVE
    return ticks


def test_monitor_does_not_spin_in_active_mode(monkeypatch):
    monkeypatch.setattr(rg, '_MONITOR_INTERVAL_SECONDS', 0.1)
    gov = ResourceGovernor()
    ticks = _pin_active_and_count(gov, monkeypatch)

    gov._running = True
    t = threading.Thread(target=gov._monitor_loop, daemon=True)
    t.start()
    time.sleep(0.35)
    gov._running = False
    gov._stop_event.set()                        # what stop() does
    t.join(timeout=2.0)

    assert not t.is_alive(), "monitor thread did not exit after stop()"
    # ~0.35s / 0.1s interval ≈ 3-4 ticks when it SLEEPS. The bug spun hundreds.
    assert 1 <= ticks['n'] <= 15, (
        f"monitor busy-spun ({ticks['n']} ticks in 0.35s @0.1s interval) — "
        "the ACTIVE-mode _cancel_event defeated the interval sleep (the hang)")


def test_active_cancel_event_is_independent_of_stop_event():
    """The ACTIVE/SLEEP proactive cancel signal must never imply shutdown, so it
    can never defeat the monitor's interval sleep."""
    gov = ResourceGovernor()
    gov._cancel_event.set()
    assert not gov._stop_event.is_set()


def test_stop_event_wakes_monitor_promptly(monkeypatch):
    """stop() sets _stop_event so the monitor exits without waiting a full
    interval — proves shutdown stays responsive with a long interval."""
    monkeypatch.setattr(rg, '_MONITOR_INTERVAL_SECONDS', 30)   # long on purpose
    gov = ResourceGovernor()
    _pin_active_and_count(gov, monkeypatch)

    gov._running = True
    t = threading.Thread(target=gov._monitor_loop, daemon=True)
    t.start()
    time.sleep(0.2)                              # let it enter the 30s wait
    gov._running = False
    gov._stop_event.set()
    t.join(timeout=2.0)
    assert not t.is_alive(), (
        "monitor did not wake on _stop_event within 2s — shutdown would block "
        "the full interval")


def test_proactive_loop_does_not_spin_in_active_mode(monkeypatch):
    """The PROACTIVE action-stream loop (the monitor's sibling thread) must also
    sleep while the user is ACTIVE, not busy-spin. It used to wait on
    _cancel_event — SET in ACTIVE/SLEEP — so the wait returned instantly and it
    spun the loop (watchdog heartbeat + 4 _jitter() timer resets per iteration)
    on a core, exactly while the user is active. Fix: wait on _stop_event."""
    gov = ResourceGovernor()
    gov._mode = MODE_ACTIVE
    gov._cancel_event.set()                       # the ACTIVE signal (bug trigger)
    import security.node_watchdog as _nw
    monkeypatch.setattr(_nw, 'get_watchdog', lambda: None)

    # _jitter is called 4× at init and 4× per ACTIVE iteration (the timer-reset
    # branch). When the loop SLEEPS the full interval only the 4 init calls fire
    # in the test window; when it SPINS the count runs to the hundreds.
    jitter = {'n': 0}
    monkeypatch.setattr(rg, '_jitter',
                        lambda *_a: (jitter.__setitem__('n', jitter['n'] + 1), 0.0)[1])

    gov._running = True
    t = threading.Thread(target=gov._proactive_action_stream, daemon=True)
    t.start()
    time.sleep(0.4)
    gov._running = False
    gov._stop_event.set()                         # what stop() does
    t.join(timeout=2.0)

    assert not t.is_alive(), "proactive thread did not exit after stop()"
    assert jitter['n'] <= 12, (
        f"proactive loop busy-spun ({jitter['n']} _jitter calls in 0.4s) — it "
        "waited on the ACTIVE-set _cancel_event instead of _stop_event")
