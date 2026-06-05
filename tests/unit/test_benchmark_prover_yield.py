"""Hive benchmark prover — the only auto-running validator — must not skip a
whole 6h cycle just because the user is momentarily active.

Bug (#87): on should_yield_to_user() it slept the FULL _LOOP_INTERVAL_SECONDS
(6h), so on a machine in use it effectively never ran → "nothing is live
validated." Fix: re-check after _YIELD_RECHECK_SECONDS (~120s) so it resumes
within minutes of the user going idle, via the shared _interruptible_sleep.

Behavioural: exercises the real _interruptible_sleep + asserts the invariant.
No grep tests.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_yield_recheck_is_far_shorter_than_the_6h_cycle():
    from integrations.agent_engine import hive_benchmark_prover as m
    # The whole bug was sleeping the full 6h cycle on yield. The re-check must be
    # short enough to resume within minutes of the user going idle.
    assert m._YIELD_RECHECK_SECONDS < m._LOOP_INTERVAL_SECONDS
    assert m._YIELD_RECHECK_SECONDS <= 300


def test_interruptible_sleep_sleeps_n_then_stops_early(monkeypatch):
    from integrations.agent_engine import hive_benchmark_prover as m
    prover = object.__new__(m.HiveBenchmarkProver)  # skip __init__ (no threads)
    n = {'count': 0}
    monkeypatch.setattr(m.time, 'sleep',
                        lambda s: n.__setitem__('count', n['count'] + 1))

    prover._loop_running = True
    prover._interruptible_sleep(5)
    assert n['count'] == 5            # slept the requested number of 1s steps

    # Returns early the instant the loop is asked to stop (clean shutdown).
    n['count'] = 0

    def _stop_after_two(_s):
        n['count'] += 1
        if n['count'] >= 2:
            prover._loop_running = False

    monkeypatch.setattr(m.time, 'sleep', _stop_after_two)
    prover._loop_running = True
    prover._interruptible_sleep(1000)
    assert n['count'] == 2            # did NOT sleep 1000 — stopped early
