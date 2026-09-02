"""
Integrity round: the write lock must never span a network call
==============================================================
Measured on the installed desktop 2026-09-02 (Nunba.exe, HARTOS d47f4205):
the agent daemon logged "dispatched 2 goal(s)" every tick and then "tick
error: database is locked"; max(agent_goals.last_dispatched_at) stood still
for two hours while the process looked alive.  A BEGIN IMMEDIATE probe could
not get the write lock; py-spy showed the only open write transaction
belonged to the gossip thread, parked in IntegrityService.create_challenge ->
pooled_post -> urllib3 create_connection.

create_challenge did `db.add(challenge); db.flush()` (the INSERT takes
SQLite's single write lock) and only then POSTed to the peer with a 30s
timeout, and _integrity_round committed once after looping every active
peer: 525 rows on that desktop, most of them unroutable, so one round held
the lock for hours and integrity_interval (300s) meant the next round began
as soon as the last one ended.  374f5ab6 fixed the identical shape in the
health round (commit each row before the next probe); these tests pin the
same contract for the integrity round.

The probe is the real thing: inside the fake network call a SECOND
connection asks for the write lock, which is exactly what the daemon's
UPDATE does while the round is mid-flight.

Runs standalone (`python tests/unit/test_integrity_round_lock_span.py`)
because pytest collection hangs in this tree.
"""
import os
import sqlite3
import sys
import tempfile
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
from integrations.social.integrity_service import IntegrityService
from integrations.social.peer_discovery import GossipProtocol


class _LockProbe:
    """Stands in for pooled_get / pooled_post.  Inside the "network call" it
    tries BEGIN IMMEDIATE on a second connection with a short busy timeout
    and records whether the round was holding the write lock at that
    moment.  Then it fails like an unroutable peer does (the timeout path,
    which is the one that also writes: status='timeout' + fraud score)."""

    def __init__(self, path):
        self.path = path
        self.held = []      # one bool per network call: True = lock was held
        self.urls = []

    def __call__(self, url, **kw):
        self.urls.append(url)
        c = sqlite3.connect(self.path, timeout=0.2)
        try:
            c.execute('BEGIN IMMEDIATE')
            c.execute('ROLLBACK')
            self.held.append(False)
        except sqlite3.OperationalError:
            self.held.append(True)
        finally:
            c.close()
        raise requests.ConnectionError('unroutable peer')


class _FileDb(unittest.TestCase):
    """A file-backed SQLite, because an in-memory one is private to its
    connection and a second connection could never observe the lock."""

    def setUp(self):
        # mkdtemp + a hand-rolled remove: TemporaryDirectory.cleanup() goes
        # through shutil.rmtree, which in this tree's venv trips over a mixed
        # 3.12 stdlib (os has no _walk_symlinks_as_files).
        self.dir = tempfile.mkdtemp(prefix='lockspan-')
        self.path = os.path.join(self.dir, 'lockspan.db')
        self.eng = create_engine('sqlite:///' + self.path, echo=False,
                                 connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.eng)
        # expire_on_commit=False mirrors integrations.social.models.get_db,
        # so what create_challenge reads back after its commit is what the
        # daemon would read.
        self.db = sessionmaker(bind=self.eng, expire_on_commit=False)()
        self.probe = _LockProbe(self.path)

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

    def _peer(self, i):
        p = PeerNode(node_id='peer-%d' % i, url='http://10.1.0.%d:5000' % i,
                     status='active', integrity_status='unverified',
                     first_seen=datetime.utcnow() - timedelta(seconds=60),
                     last_seen=datetime.utcnow())
        self.db.add(p)
        return p


class CreateChallengeLockSpanTest(_FileDb):

    def test_lock_is_not_held_during_the_post(self):
        self._peer(1)
        self.db.commit()
        with patch.object(I, 'pooled_post', self.probe):
            IntegrityService.create_challenge(
                self.db, 'self', 'peer-1', 'http://10.1.0.1:5000',
                'agent_count_verify')
        self.assertEqual(len(self.probe.held), 1, "the POST never happened")
        self.assertEqual(self.probe.held, [False],
                         "create_challenge held SQLite's write lock across the "
                         "network call (the INSERT was flushed, not committed)")

    def test_challenge_row_is_durable_before_the_post(self):
        """If the process dies mid-POST the challenge must already exist:
        that is the whole point of committing before the network call."""
        self._peer(1)
        self.db.commit()
        seen = {}

        def post_then_look(url, **kw):
            other = sqlite3.connect(self.path, timeout=0.2)
            try:
                seen['rows'] = other.execute(
                    "select count(*) from integrity_challenges").fetchone()[0]
            finally:
                other.close()
            raise requests.ConnectionError('unroutable peer')

        with patch.object(I, 'pooled_post', post_then_look):
            IntegrityService.create_challenge(
                self.db, 'self', 'peer-1', 'http://10.1.0.1:5000',
                'stats_probe')
        self.assertEqual(seen.get('rows'), 1,
                         "a second connection could not see the challenge "
                         "row while the POST was in flight")

    def test_timeout_path_still_records_the_outcome(self):
        """Committing early must not lose the after-POST bookkeeping."""
        self._peer(1)
        self.db.commit()
        with patch.object(I, 'pooled_post', self.probe):
            result = IntegrityService.create_challenge(
                self.db, 'self', 'peer-1', 'http://10.1.0.1:5000',
                'code_hash_check')
        self.db.commit()
        self.assertEqual(result['status'], 'timeout')
        row = self.db.query(IntegrityChallenge).one()
        self.assertEqual(row.status, 'timeout')
        peer = self.db.query(PeerNode).filter_by(node_id='peer-1').one()
        self.assertIsNotNone(peer.last_challenge_at)
        self.assertGreater(peer.fraud_score, 0.0)


class IntegrityRoundLockSpanTest(_FileDb):
    """Drive the real round over several unroutable peers and assert that no
    network call, in either step, finds the write lock held."""

    def setUp(self):
        super().setUp()
        # The round stops itself when the local guardrail self-check fails,
        # before any network call.  security.hive_guardrails is a frozen
        # module (patching it raises "Cannot modify frozen guardrail"), so
        # the real check has to pass in the environment running this test;
        # say so plainly rather than fail on "round never reached a peer".
        from security.hive_guardrails import verify_guardrail_integrity
        if not verify_guardrail_integrity():
            self.skipTest("verify_guardrail_integrity() is False in this "
                          "environment; the round exits before its first probe")

    def _gossip(self):
        pd = GossipProtocol.__new__(GossipProtocol)
        pd._running = True
        pd.node_id = 'self'
        pd._heartbeat = lambda: None
        pd.stop = lambda: None
        return pd

    def _round_env(self):
        # is_code_healthy is patchable; HEVOLVE_REGISTRY_URL is cleared so
        # step 5 cannot reach out to a real registry through the unpatched
        # integrity_service.pooled_get.
        return [
            patch('integrations.social.models.get_db', return_value=self.db),
            patch('security.runtime_monitor.is_code_healthy', return_value=True),
            patch.dict(os.environ, {'HEVOLVE_REGISTRY_URL': ''}),
        ]

    def test_no_network_call_sees_the_lock_held(self):
        n = 3
        for i in range(1, n + 1):
            self._peer(i)
        self.db.commit()
        pd = self._gossip()
        with (patch.object(P, 'pooled_get', self.probe),
              patch.object(I, 'pooled_post', self.probe)):
            for ctx in self._round_env():
                self.enterContext(ctx)
            pd._integrity_round()
        # step 1 audits every peer (GET), step 2 challenges every peer (POST)
        self.assertEqual(len(self.probe.held), 2 * n,
                         "round did not reach every peer: %r" % self.probe.urls)
        self.assertNotIn(True, self.probe.held,
                         "a network call ran while the round held the write "
                         "lock: %r" % list(zip(self.probe.urls, self.probe.held)))

    def test_round_persists_each_peer_even_if_it_never_finishes(self):
        """A round that hangs on peer 3 must already have committed peers 1
        and 2 (the health-round lesson: per-row commits mean a hung probe
        stalls only this round, not every writer in the process)."""
        for i in range(1, 4):
            self._peer(i)
        self.db.commit()
        pd = self._gossip()
        hung = {}

        def post_probe(url, **kw):
            if url.startswith('http://10.1.0.3'):
                other = sqlite3.connect(self.path, timeout=0.2)
                try:
                    hung['committed'] = other.execute(
                        "select count(*) from integrity_challenges "
                        "where status='timeout'").fetchone()[0]
                finally:
                    other.close()
                raise RuntimeError('simulated hang, round aborted here')
            raise requests.ConnectionError('unroutable peer')

        with (patch.object(P, 'pooled_get', self.probe),
              patch.object(I, 'pooled_post', post_probe)):
            for ctx in self._round_env():
                self.enterContext(ctx)
            pd._integrity_round()
        self.assertEqual(hung.get('committed'), 2,
                         "peers 1 and 2 were not committed before peer 3's POST")


if __name__ == '__main__':
    unittest.main(verbosity=2)
