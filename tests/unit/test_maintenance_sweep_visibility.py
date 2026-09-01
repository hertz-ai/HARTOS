"""A maintenance sweep that fails must say so at a level production can see.

The hevolve_social logger runs at WARNING in production. Both integrity-round
sweeps reported failure at logger.debug, so a sweep throwing on every round was
indistinguishable from one with nothing to do -- and their SUCCESS lines are
INFO, equally invisible. The only observable was a row count that did not move.

Measured on central 2026-09-01: the retention sweep ran twice
(300,092 -> 290,092 -> 280,092), then stopped with 135,869 rows still eligible
and nothing in the logs either way. I wrote that blind spot into peer_discovery
the same day I warned other agents that an absent INFO log proves nothing.

Runs standalone (`python tests/unit/test_maintenance_sweep_visibility.py`).
"""
import inspect
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'


def _round_source():
    from integrations.social import peer_discovery
    src = inspect.getsource(peer_discovery)
    start = src.index('def _integrity_round')
    end = src.index('active_peers = db.query', start)
    return src[start:end]


class SweepFailureVisibilityTest(unittest.TestCase):

    def test_retention_sweep_failure_is_warning(self):
        blk = _round_source()
        self.assertIn('Integrity round retention sweep failed', blk)
        idx = blk.index('Integrity round retention sweep failed')
        window = blk[max(0, idx - 200):idx + 80]
        self.assertIn('logger.warning', window,
                      'retention sweep failure is invisible in production')

    def test_decay_sweep_failure_is_warning(self):
        blk = _round_source()
        idx = blk.index('Integrity round decay sweep failed')
        window = blk[max(0, idx - 200):idx + 80]
        self.assertIn('logger.warning', window,
                      'decay sweep failure is invisible in production')

    def test_no_debug_swallow_left_in_the_round_preamble(self):
        """Any remaining logger.debug on a failure path here is the bug."""
        blk = _round_source()
        for m in re.finditer(r'logger\.debug\(([^)]{0,120})', blk):
            self.assertNotIn('failed', m.group(1).lower(),
                             'a failure path still logs at debug: ' + m.group(1))

    def test_gossip_loop_round_failures_are_warning(self):
        """The sibling guard from 18c3e3cb must stay at WARNING too."""
        from integrations.social.peer_discovery import GossipProtocol
        src = inspect.getsource(GossipProtocol._run_round)
        self.assertIn('logger.warning', src)
        self.assertNotIn('logger.debug', src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
