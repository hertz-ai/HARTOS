"""The health round must not starve the integrity round behind it.

_background_loop runs gossip, health and integrity IN SEQUENCE. _health_check_round
pinged every non-dead peer serially at _ping_peer's 3s timeout, so on central's
566 rows one round could take ~28 minutes against a 300s integrity interval. The
integrity round was therefore never reached: no exception, no log line, just a
sibling holding the loop.

Measured on central 2026-09-01: no integrity challenge in 29h, and the retention
sweep removed 0 rows across a fresh container while 135,869 were eligible. Calling
that same sweep by hand on that same container removed exactly 10,000 rows --
proving the sweep was fine and only the scheduling was broken.

18c3e3cb isolated the rounds against EXCEPTIONS. This is the other half:
isolation against DURATION.

Runs standalone (`python tests/unit/test_health_round_budget.py`).
"""
import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from integrations.social.peer_discovery import GossipProtocol


class _Peer:
    def __init__(self, i):
        self.id = i
        self.node_id = 'peer-%d' % i
        self.url = 'https://node%d.example.net' % i   # routable-looking
        self.status = 'active'
        # Real rows always carry a first_seen; the production code does
        # `now - (last_seen or first_seen)`, which TypeErrors when both are
        # None. The fixture must not be less realistic than the schema.
        self.last_seen = None
        self.first_seen = datetime.utcnow() - timedelta(seconds=60)


class HealthRoundBudgetTest(unittest.TestCase):
    """Drive the real loop body with a slow ping and assert it yields."""

    def _run(self, n_peers, budget, ping_cost):
        pd = GossipProtocol.__new__(GossipProtocol)
        pd._running = True
        pd.node_id = 'self'
        pd.dead_threshold = 900
        pd.stale_threshold = 300
        pd._health_cursor = 0
        pd._peer_backoff = type('B', (), {'prune_expired': lambda s: None})()
        pd._heartbeat = lambda: None

        pinged = {'n': 0}

        def slow_ping(url):
            pinged['n'] += 1
            time.sleep(ping_cost)
            return False

        pd._ping_peer = slow_ping
        os.environ['HEVOLVE_HEALTH_ROUND_BUDGET_S'] = str(budget)

        peers = [_Peer(i) for i in range(n_peers)]

        calls = {'n': 0}

        class _Q:
            def __init__(self, rows): self._rows = rows
            def filter(self, *a, **k): return self
            def all(self): return self._rows
            def first(self): return None

        class _DB:
            def query(self, *a, **k):
                # 1st query = the non-dead peer list; every later one (the
                # bounded dead-peer re-probe) returns nothing.
                calls['n'] += 1
                return _Q(peers if calls['n'] == 1 else [])
            def delete(self, *a, **k): pass
            def commit(self): pass
            def rollback(self): pass
            def close(self): pass

        with patch('integrations.social.models.get_db', return_value=_DB()), \
             patch('integrations.social.peer_discovery.is_unroutable_peer_url',
                   return_value=(False, '')):
            t0 = time.time()
            pd._health_check_round()
            elapsed = time.time() - t0
        return pinged['n'], elapsed, pd

    def test_round_yields_at_the_budget(self):
        """40 peers x 0.05s would be 2s; a 0.3s budget must cut it short."""
        n, elapsed, _ = self._run(n_peers=40, budget=0.3, ping_cost=0.05)
        self.assertLess(elapsed, 1.5,
                        'health round ran past its budget and would starve integrity')
        self.assertLess(n, 40, 'every peer was pinged despite the budget')

    def test_cursor_advances_so_coverage_rotates(self):
        """A budget cut must not re-check the same prefix forever."""
        _, _, pd = self._run(n_peers=40, budget=0.3, ping_cost=0.05)
        self.assertGreater(getattr(pd, '_health_cursor', 0), 0,
                           'cursor did not advance; the tail is never reached')

    def test_small_table_completes_untouched(self):
        """A node with few peers must still do a full pass."""
        n, elapsed, _ = self._run(n_peers=3, budget=30, ping_cost=0.01)
        self.assertEqual(n, 3)


class BudgetWiringTest(unittest.TestCase):

    def test_budget_is_env_overridable(self):
        import inspect
        src = inspect.getsource(GossipProtocol._health_check_round)
        self.assertIn('HEVOLVE_HEALTH_ROUND_BUDGET_S', src)

    def test_yield_is_logged_at_warning(self):
        import inspect
        src = inspect.getsource(GossipProtocol._health_check_round)
        idx = src.index('budget after')
        self.assertIn('logger.warning', src[max(0, idx - 300):idx])

    def test_health_failure_is_no_longer_a_debug_swallow(self):
        import inspect
        src = inspect.getsource(GossipProtocol._health_check_round)
        self.assertIn('Health check round failed', src)
        self.assertNotIn('logger.debug(f"Health check error', src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
