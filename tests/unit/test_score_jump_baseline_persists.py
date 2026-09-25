"""The score-jump baseline must advance after every check, or a node that
grew once is flagged forever.

Measured on central 2026-09-22: the office desktop 46329c87 earned
"+10 score_jump: Agent count jumped from 25 to 285 (1040% increase)" on EVERY
integrity round from 2026-08-08 to 2026-09-22 (201 alerts, fail2ban offence
#127, banned until 2026-10-22). The 25 was written once, on 2026-08-06, and
never again: detect_score_jump mutated the peer's own metadata dict in place
and assigned the same object back, and PeerNode.metadata_json is a plain
Column(JSON) with no mutation tracking, so the ORM compared equal values and
emitted no UPDATE. Every row on central carried _last_score_check 2026-08-06.

Runs standalone (`python tests/unit/test_score_jump_baseline_persists.py`),
against a real PeerNode in a temporary sqlite FILE so the re-read below goes
through a genuinely new session and connection.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from integrations.social.models import Base, PeerNode
from integrations.social.integrity_service import IntegrityService

STALE_CHECK = '2026-08-06T20:18:06.787476'


class ScoreJumpBaselineTest(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(prefix='score_jump_', suffix='.db')
        os.close(fd)
        self.eng = create_engine('sqlite:///' + self.path.replace(os.sep, '/'))
        Base.metadata.create_all(self.eng)
        self.Session = sessionmaker(bind=self.eng)
        db = self.Session()
        db.add(PeerNode(
            node_id='desk', url='http://192.168.0.165:5000', name='hevolve-desk',
            status='active', agent_count=285, post_count=1760, fraud_score=0.0,
            metadata_json={'_prev_agent_count': 25, '_prev_post_count': 1045,
                           '_last_score_check': STALE_CHECK},
        ))
        db.commit()
        db.close()

    def tearDown(self):
        self.eng.dispose()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _check(self):
        db = self.Session()
        try:
            alert = IntegrityService.detect_score_jump(db, 'desk')
            db.commit()
            return alert
        finally:
            db.close()

    def _peer(self):
        db = self.Session()
        try:
            p = db.query(PeerNode).filter_by(node_id='desk').first()
            return dict(p.metadata_json or {}), float(p.fraud_score or 0.0)
        finally:
            db.close()

    def test_the_baseline_advances_after_a_check(self):
        """THE REGRESSION TEST: re-read in a new session after the commit."""
        alert = self._check()
        self.assertIsNotNone(alert, '25 -> 285 against the stale baseline IS a jump')
        meta, _ = self._peer()
        self.assertEqual(meta.get('_prev_agent_count'), 285,
                         'the baseline did not persist: %r' % meta)
        self.assertEqual(meta.get('_prev_post_count'), 1760)
        self.assertNotEqual(meta.get('_last_score_check'), STALE_CHECK)

    def test_an_unchanged_count_is_not_a_jump_on_the_next_round(self):
        """What the desktop lived through: the same 285 flagged every round."""
        first = self._check()
        self.assertIsNotNone(first)
        second = self._check()
        self.assertIsNone(second, 'the second round re-flagged an unchanged count')
        _, score = self._peer()
        self.assertLess(score, 20.0, 'two alerts were scored for one jump: %r' % score)

    def test_a_real_jump_is_still_flagged(self):
        """Preservation: the detector still fires when the count really jumps."""
        self._check()
        db = self.Session()
        p = db.query(PeerNode).filter_by(node_id='desk').first()
        p.agent_count = 285 * 4
        db.commit()
        db.close()
        self.assertIsNotNone(self._check(), 'a 300% jump against a fresh baseline was missed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
