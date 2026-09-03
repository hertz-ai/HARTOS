"""AgentGoal.config_json must persist in-place mutations.

Every writer in the tree uses the same shape:

    cfg = goal.config_json or {}
    cfg['pause_reason'] = ...
    goal.config_json = cfg          # SAME dict object

On a plain JSON column SQLAlchemy sees no change and silently drops the write,
while any scalar set in the same block (goal.status) persists normally. The
result is a row that looks transitioned but carries none of the context that
explains why.

Proven on central 2026-09-01: 40 goals sat status='paused' and NOT ONE carried a
pause_reason, though all four pause paths write one -- agent_daemon's
dispatch-failure and noop paths, coding_daemon's dispatch-failure path, and
budget_gate. Reproduced in isolation: status persisted, config_json came back as
the pre-edit dict, and assigning a NEW dict persisted fine.

The silent half is worse than the missing reason: coding_daemon increments
`_dispatch_failures` through the same pattern, so the counter never advanced past
its first write and its 5-failure auto-pause has never been able to fire.

Runs standalone (`python tests/unit/test_config_json_mutation.py`).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Resolve the model the way production does. integrations.social.models prefers
# the canonical sql.models from the hevolve-database sibling repo when it is
# installed, and falls back to _models_local.py otherwise. The two share one
# declarative Base, so they CANNOT both be loaded in a process -- which is why
# this cannot simply import the fallback directly.
#
# Central runs the fallback (verified live: AgentGoal there resolves to
# /app/integrations/social/_models_local.py), so the behavioural tests below run
# there and in any environment without hevolve-database. Where the canonical
# model wins they are skipped with the reason, and the source assertion still
# guards the fallback from regressing.
import inspect

from integrations.social.models import Base, AgentGoal

_MODEL_FILE = inspect.getfile(AgentGoal)
_IS_FALLBACK = _MODEL_FILE.replace(chr(92), '/').endswith('_models_local.py')
_LOCAL_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'integrations', 'social', '_models_local.py')


class FallbackModelSourceTest(unittest.TestCase):
    """Always runs: the fallback model is what central executes."""

    def test_fallback_column_uses_mutabledict(self):
        with open(_LOCAL_SRC, encoding='utf-8') as fh:
            src = fh.read()
        self.assertIn('config_json = Column(MutableDict.as_mutable(JSON)', src,
                      'central runs _models_local.py; a bare JSON column there '
                      'silently drops every in-place config_json write')
        self.assertIn('from sqlalchemy.ext.mutable import MutableDict', src)


@unittest.skipUnless(_IS_FALLBACK,
                     'canonical sql.models is installed here, so the fallback '
                     'model cannot be loaded in the same process; these run on '
                     'central and anywhere without hevolve-database')
class ConfigJsonMutationTest(unittest.TestCase):

    def setUp(self):
        self.eng = create_engine('sqlite://', echo=False,
                                 connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.eng)
        self.S = sessionmaker(bind=self.eng)
        db = self.S()
        db.add(AgentGoal(id='g1', goal_type='coding', title='t', status='active',
                         spark_budget=10, spark_spent=10,
                         config_json={'mode': 'audit'}))
        db.commit(); db.close()

    def _cfg(self):
        db = self.S()
        try:
            return dict(self.S().query(AgentGoal).filter_by(id='g1').first().config_json or {})
        finally:
            db.close()

    def test_in_place_mutation_persists(self):
        """THE regression: same-object reassignment must still be written."""
        db = self.S()
        g = db.query(AgentGoal).filter_by(id='g1').first()
        cfg = g.config_json or {}
        cfg['pause_reason'] = 'Auto-paused: budget gate blocked'
        g.config_json = cfg
        db.commit(); db.close()
        self.assertIn('pause_reason', self._cfg())

    def test_status_and_reason_persist_together(self):
        """The production symptom was status WITHOUT reason."""
        db = self.S()
        g = db.query(AgentGoal).filter_by(id='g1').first()
        cfg = g.config_json or {}
        g.status = 'paused'
        cfg['pause_reason'] = 'Auto-paused: 5 consecutive dispatch failures'
        g.config_json = cfg
        db.commit(); db.close()
        db = self.S()
        g = db.query(AgentGoal).filter_by(id='g1').first()
        self.assertEqual(g.status, 'paused')
        self.assertIn('pause_reason', g.config_json or {})
        db.close()

    def test_counter_increments_across_sessions(self):
        """coding_daemon's _dispatch_failures backoff depends on this."""
        for expected in (1, 2, 3, 4, 5):
            db = self.S()
            g = db.query(AgentGoal).filter_by(id='g1').first()
            cfg = g.config_json or {}
            cfg['_dispatch_failures'] = cfg.get('_dispatch_failures', 0) + 1
            g.config_json = cfg
            db.commit(); db.close()
            self.assertEqual(self._cfg().get('_dispatch_failures'), expected,
                             'counter stalled — the 5-failure gate can never fire')

    def test_pop_persists(self):
        """The success path clears the counter with cfg.pop()."""
        db = self.S()
        g = db.query(AgentGoal).filter_by(id='g1').first()
        cfg = g.config_json or {}
        cfg['_dispatch_failures'] = 3
        g.config_json = cfg
        db.commit(); db.close()
        self.assertEqual(self._cfg().get('_dispatch_failures'), 3)

        db = self.S()
        g = db.query(AgentGoal).filter_by(id='g1').first()
        cfg = g.config_json or {}
        cfg.pop('_dispatch_failures', None)
        g.config_json = cfg
        db.commit(); db.close()
        self.assertNotIn('_dispatch_failures', self._cfg())

    def test_existing_keys_survive(self):
        db = self.S()
        g = db.query(AgentGoal).filter_by(id='g1').first()
        cfg = g.config_json or {}
        cfg['paused_at'] = '2026-09-01T00:00:00'
        g.config_json = cfg
        db.commit(); db.close()
        cfg = self._cfg()
        self.assertEqual(cfg.get('mode'), 'audit')
        self.assertIn('paused_at', cfg)

    def test_column_is_mutation_tracked(self):
        """Pin the mechanism so a revert to a bare JSON column fails here."""
        db = self.S()
        g = db.query(AgentGoal).filter_by(id='g1').first()
        self.assertIsInstance(g.config_json, dict)
        # MutableDict wraps the loaded value; a bare JSON column yields a plain
        # dict with no association to the parent.
        self.assertTrue(hasattr(g.config_json, '_parents'),
                        'config_json is not MutableDict-tracked; in-place '
                        'mutations will be silently dropped again')
        db.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
