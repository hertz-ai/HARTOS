"""One failing background round must not starve the other two.

_background_loop used to run gossip, health and integrity inside a SINGLE
try/except. That had two compounding failure modes:

  1. a raising _gossip_round() skipped health AND integrity for that tick;
  2. last_gossip was assigned only AFTER the call, so a raising gossip left it
     un-advanced -- the next tick (5s later) retried gossip, raised again, and
     the other two rounds never ran again. Permanently. At logger.debug.

Measured on central 2026-09-01: newest integrity challenge 2026-08-31 04:29,
24h stale, while inbound announces were served normally. The retention sweep
that bounds integrity_challenges lives in _integrity_round, so 155,869 rows sat
past their window with the code to remove them never reached.

Runs standalone (`python tests/unit/test_gossip_round_isolation.py`).
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from integrations.social.peer_discovery import GossipProtocol


class _Loop:
    """Drives the real _background_loop for a bounded number of ticks."""

    def __init__(self, gossip_fails=False, health_fails=False,
                 integrity_fails=False, ticks=4):
        self.calls = {'gossip': 0, 'health': 0, 'integrity': 0}
        self._fail = {'gossip': gossip_fails, 'health': health_fails,
                      'integrity': integrity_fails}
        self._ticks = ticks

    def install(self, pd):
        def mk(name):
            def _round():
                self.calls[name] += 1
                if self._fail[name]:
                    raise RuntimeError(f'{name} exploded')
            return _round
        pd._gossip_round = mk('gossip')
        pd._health_check_round = mk('health')
        pd._integrity_round = mk('integrity')
        # every round due on every tick
        pd.gossip_interval = 0
        pd.health_interval = 0
        # stop after N ticks
        n = {'i': 0}
        real_sleep = time.sleep

        def fake_sleep(_s):
            n['i'] += 1
            if n['i'] >= self._ticks:
                pd._running = False
            real_sleep(0)
        return fake_sleep


def _drive(**kw):
    pd = GossipProtocol.__new__(GossipProtocol)
    pd._running = True
    loop = _Loop(**kw)
    fake_sleep = loop.install(pd)
    import integrations.social.peer_discovery as mod
    real_sleep, mod.time.sleep = mod.time.sleep, fake_sleep
    os.environ['HEVOLVE_INTEGRITY_INTERVAL'] = '0'
    try:
        pd._background_loop()
    finally:
        mod.time.sleep = real_sleep
    return loop.calls


class GossipRoundIsolationTest(unittest.TestCase):

    def test_all_rounds_run_when_healthy(self):
        calls = _drive(ticks=3)
        for name in ('gossip', 'health', 'integrity'):
            self.assertGreaterEqual(calls[name], 3, f'{name} did not run')

    # ── the regression ─────────────────────────────────────────────────
    def test_failing_gossip_does_not_starve_integrity(self):
        """The exact production failure: gossip throws, integrity must survive."""
        calls = _drive(gossip_fails=True, ticks=3)
        self.assertGreaterEqual(calls['gossip'], 3)
        self.assertGreaterEqual(calls['integrity'], 3,
                                'integrity was starved by a failing gossip round')
        self.assertGreaterEqual(calls['health'], 3,
                                'health was starved by a failing gossip round')

    def test_failing_health_does_not_starve_integrity(self):
        calls = _drive(health_fails=True, ticks=3)
        self.assertGreaterEqual(calls['integrity'], 3)
        self.assertGreaterEqual(calls['gossip'], 3)

    def test_failing_integrity_does_not_starve_gossip(self):
        calls = _drive(integrity_fails=True, ticks=3)
        self.assertGreaterEqual(calls['gossip'], 3)
        self.assertGreaterEqual(calls['health'], 3)

    def test_every_round_failing_still_ticks_all_three(self):
        calls = _drive(gossip_fails=True, health_fails=True,
                       integrity_fails=True, ticks=3)
        for name in ('gossip', 'health', 'integrity'):
            self.assertGreaterEqual(calls[name], 3, f'{name} stopped ticking')

    # ── the helper's own contract ──────────────────────────────────────
    def test_run_round_reports_outcome_and_swallows(self):
        pd = GossipProtocol.__new__(GossipProtocol)

        def boom():
            raise ValueError('nope')

        self.assertTrue(pd._run_round('ok', lambda: None))
        self.assertFalse(pd._run_round('bad', boom))  # must not propagate


if __name__ == '__main__':
    unittest.main(verbosity=2)
