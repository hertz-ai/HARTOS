"""
Challenge history retention sweep
=================================
integrity_challenges was append-only: every gossip round wrote a row per peer
per challenge type and nothing ever deleted them.  Measured on central
2026-09-01 it held 300,092 rows / 386 MB of table plus ~60 MB of indexes, 72%
of a 618 MB database, which is what broke the SQLite write path (WAL
checkpointer could not finish, writers blocked past busy_timeout, goal status
updates never persisted).

These tests pin the retention contract: terminal non-forensic rows expire,
forensic rows never do, and the sweep stays bounded so it cannot become the
lock contention it exists to remove.

Runs standalone (`python tests/unit/test_challenge_retention.py`) because
pytest collection hangs in this tree.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from integrations.social.models import Base, IntegrityChallenge
from integrations.social import integrity_service as I
from integrations.social.integrity_service import IntegrityService


def _mk(db, status, age_days, n=1):
    made = datetime.utcnow() - timedelta(days=age_days)
    for i in range(n):
        db.add(IntegrityChallenge(
            id=f'{status}-{age_days}-{i}-{os.urandom(4).hex()}',
            challenger_node_id='challenger',
            target_node_id='target',
            challenge_type='stats_probe',
            challenge_nonce='n',
            status=status,
            created_at=made,
        ))
    db.commit()


class ChallengeRetentionTest(unittest.TestCase):

    def setUp(self):
        self.eng = create_engine('sqlite://', echo=False,
                                 connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.eng)
        self.db = sessionmaker(bind=self.eng)()

    def tearDown(self):
        self.db.close()

    def _count(self, status=None):
        q = self.db.query(IntegrityChallenge)
        if status:
            q = q.filter(IntegrityChallenge.status == status)
        return q.count()

    # ── what must be deleted ───────────────────────────────────────────
    def test_old_passed_is_deleted(self):
        _mk(self.db, 'passed', age_days=30, n=5)
        r = IntegrityService.prune_challenge_history(self.db)
        self.assertEqual(r['deleted_passed'], 5)
        self.assertEqual(self._count('passed'), 0)

    def test_old_timeout_is_deleted(self):
        _mk(self.db, 'timeout', age_days=45, n=3)
        r = IntegrityService.prune_challenge_history(self.db)
        self.assertEqual(r['deleted_timeout'], 3)
        self.assertEqual(self._count('timeout'), 0)

    # ── what must survive ──────────────────────────────────────────────
    def test_recent_passed_survives(self):
        _mk(self.db, 'passed', age_days=1, n=4)
        r = IntegrityService.prune_challenge_history(self.db)
        self.assertEqual(r['deleted_total'], 0)
        self.assertEqual(self._count('passed'), 4)

    def test_timeout_uses_its_own_longer_window(self):
        """A 20d timeout outlives the 14d 'passed' cutoff on purpose."""
        _mk(self.db, 'timeout', age_days=20, n=2)
        IntegrityService.prune_challenge_history(self.db)
        self.assertEqual(self._count('timeout'), 2)

    def test_failed_is_never_deleted(self):
        """Evidence of an actual integrity violation. Forensic, keep forever."""
        _mk(self.db, 'failed', age_days=365, n=3)
        r = IntegrityService.prune_challenge_history(self.db)
        self.assertEqual(r['deleted_total'], 0)
        self.assertEqual(self._count('failed'), 3)

    def test_pending_is_never_deleted(self):
        """May still be in flight; the timeout path owns its transition."""
        _mk(self.db, 'pending', age_days=365, n=2)
        IntegrityService.prune_challenge_history(self.db)
        self.assertEqual(self._count('pending'), 2)

    # ── boundedness: the sweep must not become the lock it is fixing ───
    def test_sweep_is_bounded_and_reports_backlog(self):
        original = I.CHALLENGE_PRUNE_MAX_PER_ROUND
        I.CHALLENGE_PRUNE_MAX_PER_ROUND = 10
        try:
            _mk(self.db, 'passed', age_days=30, n=25)
            r = IntegrityService.prune_challenge_history(self.db)
            self.assertEqual(r['deleted_total'], 10)
            self.assertTrue(r['more_remaining'])
            self.assertEqual(self._count('passed'), 15)
        finally:
            I.CHALLENGE_PRUNE_MAX_PER_ROUND = original

    def test_backlog_drains_across_rounds(self):
        original = I.CHALLENGE_PRUNE_MAX_PER_ROUND
        I.CHALLENGE_PRUNE_MAX_PER_ROUND = 10
        try:
            _mk(self.db, 'passed', age_days=30, n=25)
            for _ in range(3):
                IntegrityService.prune_challenge_history(self.db)
            self.assertEqual(self._count('passed'), 0)
        finally:
            I.CHALLENGE_PRUNE_MAX_PER_ROUND = original

    def test_empty_table_is_a_noop(self):
        r = IntegrityService.prune_challenge_history(self.db)
        self.assertEqual(r['deleted_total'], 0)
        self.assertFalse(r['more_remaining'])

    def test_mixed_table_deletes_only_eligible_rows(self):
        _mk(self.db, 'passed', age_days=30, n=6)
        _mk(self.db, 'passed', age_days=2, n=2)
        _mk(self.db, 'timeout', age_days=40, n=3)
        _mk(self.db, 'timeout', age_days=20, n=1)
        _mk(self.db, 'failed', age_days=99, n=2)
        _mk(self.db, 'pending', age_days=99, n=1)
        r = IntegrityService.prune_challenge_history(self.db)
        self.assertEqual(r['deleted_passed'], 6)
        self.assertEqual(r['deleted_timeout'], 3)
        self.assertEqual(self._count(), 6)  # 2 recent passed + 1 timeout + 2 failed + 1 pending


class RetentionWiringTest(unittest.TestCase):
    """The 2026-08-07 lesson: a maintenance sweep with no scheduled caller
    never runs.  apply_fraud_score_decay sat uncalled for months.  Pin the
    caller so this one cannot regress the same way."""

    def test_integrity_round_calls_the_sweep(self):
        import inspect
        from integrations.social import peer_discovery
        src = inspect.getsource(peer_discovery)
        self.assertIn('prune_challenge_history', src,
                      "retention sweep lost its only scheduled caller")

    def test_sweep_runs_beside_the_decay_sweep(self):
        import inspect
        from integrations.social import peer_discovery
        src = inspect.getsource(peer_discovery)
        self.assertLess(src.index('apply_fraud_score_decay'),
                        src.index('prune_challenge_history'),
                        "retention must run after decay, in the same round")


if __name__ == '__main__':
    unittest.main(verbosity=2)
