"""The integrity round must not starve the gossip and health rounds behind it.

_background_loop runs gossip, health and integrity IN SEQUENCE on one thread.
_integrity_round made one 5s GET and one 30s POST per active peer with no
duration bound, in TWO full passes: audit every peer, then challenge every
peer.  Measured on central 2026-09-22 (read-only, /app/logs/hartos_social.db):
1,149 'active' rows, 774 of them private 10.x/192.168.x addresses unroutable
from the container, so the audit pass alone took ~65 minutes before the first
challenge row could exist, and one "300s" round took ~10 hours.  With a
container replaced every 20-60 minutes the challenge pass was never reached:
newest integrity_challenges row 2026-09-21 21:50Z, zero rows across the next
13 containers, and exactly one health-round line in 45 minutes of log where a
ticking loop writes ~22.  No exception, no CRITICAL line: a sibling holding the
thread.

test_health_round_budget.py pinned this contract for the health round
(HEVOLVE_HEALTH_ROUND_BUDGET_S + _health_cursor).  This pins it for the
integrity round, plus the part specific to it: audit and challenge run per
peer in ONE pass, so challenges begin inside the first budget window instead
of after a full audit pass.

Runs standalone (`python tests/unit/test_integrity_round_budget.py`) because
pytest collection hangs in this tree.
"""
import inspect
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from integrations.social.models import Base, PeerNode, IntegrityChallenge
from integrations.social import integrity_service as I
from integrations.social import peer_discovery as P
from integrations.social.peer_discovery import GossipProtocol


class _SlowNet:
    """Stands in for pooled_get / pooled_post: waits like a connect to a
    blackholed address, records the call in order, then fails like an
    unroutable peer does (the timeout path, the one that also writes)."""

    def __init__(self, cost):
        self.cost = cost
        self.calls = []   # ('GET' | 'POST', url), in call order

    def get(self, url, **kw):
        self.calls.append(('GET', url))
        time.sleep(self.cost)
        raise requests.ConnectionError('unroutable peer')

    def post(self, url, **kw):
        self.calls.append(('POST', url))
        time.sleep(self.cost)
        raise requests.ConnectionError('unroutable peer')

    def gets(self):
        return [u for k, u in self.calls if k == 'GET']

    def posts(self):
        return [u for k, u in self.calls if k == 'POST']


class IntegrityRoundBudgetTest(unittest.TestCase):
    """Drive the real round over slow, unroutable peers and assert it yields."""

    def setUp(self):
        # The round stops itself when the local guardrail self-check fails,
        # before any network call.  security.hive_guardrails is a frozen
        # module, so the real check has to pass in the environment running
        # this test; say so plainly rather than fail on "no probe happened".
        from security.hive_guardrails import verify_guardrail_integrity
        if not verify_guardrail_integrity():
            self.skipTest("verify_guardrail_integrity() is False in this "
                          "environment; the round exits before its first probe")
        # mkdtemp + a hand-rolled remove, as test_integrity_round_lock_span
        # does: TemporaryDirectory.cleanup() trips over this tree's mixed
        # 3.12 stdlib.  File-backed so create_challenge's own commit is real.
        self.dir = tempfile.mkdtemp(prefix='integbudget-')
        self.path = os.path.join(self.dir, 'budget.db')
        self.eng = create_engine('sqlite:///' + self.path, echo=False,
                                 connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.eng)
        self.db = sessionmaker(bind=self.eng, expire_on_commit=False)()

    def tearDown(self):
        self.db.close()
        self.eng.dispose()
        for name in os.listdir(self.dir):
            try:
                os.remove(os.path.join(self.dir, name))
            except OSError:
                pass
        try:
            os.rmdir(self.dir)
        except OSError:
            pass

    def _peers(self, n):
        for i in range(1, n + 1):
            self.db.add(PeerNode(
                node_id='peer-%d' % i,
                url='https://node%d.example.net' % i,   # routable-looking
                status='active', integrity_status='unverified',
                first_seen=datetime.utcnow() - timedelta(seconds=60),
                last_seen=datetime.utcnow()))
        self.db.commit()

    def _gossip(self):
        pd = GossipProtocol.__new__(GossipProtocol)
        pd._running = True
        pd.node_id = 'self'
        pd._heartbeat = lambda: None
        pd.stop = lambda: None
        return pd

    def _run_round(self, pd, net, budget):
        # is_code_healthy is patchable; HEVOLVE_REGISTRY_URL is cleared so
        # step 5 cannot reach a real registry through the unpatched
        # integrity_service.pooled_get.
        with (patch.object(P, 'pooled_get', net.get),
              patch.object(I, 'pooled_post', net.post),
              patch('integrations.social.models.get_db', return_value=self.db),
              patch('security.runtime_monitor.is_code_healthy', return_value=True),
              patch.dict(os.environ, {
                  'HEVOLVE_REGISTRY_URL': '',
                  'HEVOLVE_INTEGRITY_ROUND_BUDGET_S': str(budget)})):
            t0 = time.time()
            pd._integrity_round()
            return time.time() - t0

    # Each peer costs its two sleeps PLUS real SQLite commits on a file-backed
    # DB (create_challenge's own commit, the fraud-score write, two per-row
    # flushes), measured ~0.3-0.4s per peer here, and the round's DB-only
    # phases (decay + retention sweeps, fraud detection and audit dominance
    # over every peer) add ~2s that the budget deliberately does not cover.
    # The budgets below are sized against that, not against the sleeps alone.

    def test_round_yields_at_the_budget(self):
        """12 peers at ~0.4s each is ~5s of network loop; a 1s budget cuts it."""
        self._peers(12)
        net = _SlowNet(0.05)
        elapsed = self._run_round(self._gossip(), net, budget=1.0)
        self.assertLess(len(net.posts()), 12,
                        'every peer was challenged despite the budget')
        self.assertLess(elapsed, 8.0,
                        'integrity round ran past its budget and would starve '
                        'the gossip and health rounds')

    def test_challenges_begin_inside_the_first_window(self):
        """The central failure: two full passes meant no challenge could exist
        until EVERY peer had been audited, and under a short container life
        that was never.  One interleaved pass challenges a peer right after
        auditing it, so the first window already produces challenge rows."""
        self._peers(12)
        net = _SlowNet(0.05)
        self._run_round(self._gossip(), net, budget=1.0)
        self.assertTrue(net.posts(),
                        'no challenge was sent inside the first budget window')
        kinds = [k for k, _ in net.calls]
        k = len(net.posts())
        # audit, challenge, audit, challenge ...: each peer is challenged
        # before the next one is audited.  Two full passes would read
        # GET x N then POST x N and fail here.
        self.assertEqual(kinds[:2 * k], ['GET', 'POST'] * k,
                         'the round is still two full passes, not one: %r' % kinds)
        self.assertEqual(
            self.db.query(IntegrityChallenge).filter_by(status='timeout').count(),
            k, 'a challenge that was sent left no committed row')

    def test_cursor_advances_and_later_rounds_reach_the_tail(self):
        """A budget cut must not re-check the same prefix forever."""
        self._peers(12)
        pd = self._gossip()
        net = _SlowNet(0.05)
        self._run_round(pd, net, budget=1.0)
        self.assertGreater(getattr(pd, '_integrity_cursor', 0), 0,
                           'cursor did not advance; the tail is never reached')
        for _ in range(12):
            if len(set(net.posts())) == 12:
                break
            self._run_round(pd, net, budget=1.0)
        self.assertEqual(len(set(net.posts())), 12,
                         'repeated budgeted rounds never covered every peer')

    def test_small_table_completes_untouched(self):
        """A node with few peers must still do a full pass, both steps."""
        self._peers(3)
        net = _SlowNet(0.01)
        self._run_round(self._gossip(), net, budget=30)
        self.assertEqual(len(net.gets()), 3)
        self.assertEqual(len(net.posts()), 3)
        self.assertEqual(self.db.query(IntegrityChallenge).count(), 3)


class BudgetWiringTest(unittest.TestCase):

    def test_budget_is_env_overridable(self):
        src = inspect.getsource(GossipProtocol._integrity_round)
        self.assertIn('HEVOLVE_INTEGRITY_ROUND_BUDGET_S', src)

    def test_yield_is_logged_at_warning(self):
        src = inspect.getsource(GossipProtocol._integrity_round)
        idx = src.index('budget after')
        self.assertIn('logger.warning', src[max(0, idx - 400):idx])


if __name__ == '__main__':
    unittest.main(verbosity=2)
