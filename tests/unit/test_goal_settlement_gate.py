"""Every dispatched goal is settled by ONE gate — no dispatch style can skip it.

DEFECT (b), measured fleet-wide 2026-09-02 (Fetch-and-pull: central 94c0fd94,
desktop d47f4205; the same signature on .69): agent_daemon's speculative branch
ended in `continue`, skipping the completion gate for EVERY speculatively
dispatched goal. With speculation enabled — central and the appliance both —
no goal could ever complete, accrue a noop count, or auto-pause. Central sat at
~90 dispatches in 45 minutes with zero recipes, zero prompts, zero completions:
dispatching forever, accounting for nothing.

The gate is now extracted as _settle_dispatched_goal: DB-backed (re-reads the
goal, judges only spark_spent), so synchronous and speculative handoffs are
settled identically. These tests drive the REAL function with seam-fake goal
objects and assert the observable transitions:

    spark_spent > 0      -> completed (with completed_at, noop counter cleared)
    spark_spent == 0     -> noop counter increments; 5th noop auto-pauses
    continuous           -> never auto-completes, regardless of spark

Run:
  pytest tests/unit/test_goal_settlement_gate.py -v
"""

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from integrations.agent_engine.agent_daemon import _settle_dispatched_goal  # noqa: E402


class Goal:
    def __init__(self, spark=0, cfg=None, status='active'):
        self.spark_spent = spark
        self.config_json = cfg or {}
        self.status = status


class Db:
    """refresh is a no-op seam — the gate must still judge attribute state."""

    def refresh(self, goal):
        pass


def test_real_spark_completes_the_goal():
    g = Goal(spark=7, cfg={'noop_dispatch_count': 3})
    _settle_dispatched_goal(Db(), g, 'g1')
    assert g.status == 'completed'
    assert 'completed_at' in g.config_json
    assert 'noop_dispatch_count' not in g.config_json, \
        'completion must clear the noop trail'


def test_zero_spark_counts_a_noop_not_a_completion():
    g = Goal(spark=0)
    _settle_dispatched_goal(Db(), g, 'g1')
    assert g.status == 'active', 'zero work must not complete a goal'
    assert g.config_json['noop_dispatch_count'] == 1


def test_the_fifth_consecutive_noop_auto_pauses():
    """The protection central needed: 90 dispatches with zero work should have
    been 5 dispatches and a pause with a reason."""
    g = Goal(spark=0)
    db = Db()
    for _ in range(5):
        _settle_dispatched_goal(db, g, 'g1')
    assert g.status == 'paused'
    assert 'pause_reason' in g.config_json
    assert g.config_json['noop_dispatch_count'] == 5


def test_a_continuous_goal_never_auto_completes():
    g = Goal(spark=100, cfg={'continuous': True})
    _settle_dispatched_goal(Db(), g, 'g1')
    assert g.status == 'active', 'continuous goals are never auto-completed'


def test_refresh_failure_still_settles_from_attributes():
    class BrokenDb:
        def refresh(self, goal):
            raise RuntimeError('db gone')

    g = Goal(spark=5)
    _settle_dispatched_goal(BrokenDb(), g, 'g1')
    assert g.status == 'completed', \
        'a refresh failure must degrade to the attribute read, not skip the gate'


def test_the_speculative_branch_reaches_the_gate():
    """The defect itself, pinned structurally: the dispatch loop must contain
    NO `continue` between a speculative handoff and the settlement call. We
    assert on the compiled control flow: within _tick's source, the
    speculative-handoff sentinel exists and the old bare `continue` after
    dispatch_speculative is gone."""
    import inspect
    import integrations.agent_engine.agent_daemon as ad
    src = inspect.getsource(ad.AgentDaemon._tick) if hasattr(ad, 'AgentDaemon') \
        else open(ad.__file__, encoding='utf-8').read()
    i = src.index('dispatch_speculative(')
    window = src[i:i + 1200]
    assert 'speculative-handoff' in window, \
        'the handoff sentinel is gone — the fall-through fix was reverted'
    before_settle = window.split('_settle_dispatched_goal')[0] \
        if '_settle_dispatched_goal' in window else window
    assert '\n                            continue' not in before_settle, \
        'a continue crept back between speculation and settlement (defect (b))'
