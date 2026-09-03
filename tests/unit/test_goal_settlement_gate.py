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

from datetime import datetime, timedelta  # noqa: E402
from unittest.mock import patch  # noqa: E402

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


def test_the_parallel_branch_reaches_the_gate():
    """The SAME defect, one branch earlier — measured on central 2026-09-03.

    _try_parallel_dispatch ran BEFORE the last_dispatched_at stamp and ended in
    `continue`, so a goal whose ledger has parallel subtasks skipped the stamp,
    the spark snapshot and the settlement gate on every tick. Central's three
    hive goals fanned out every 30s for two days while their rows still read
    last_dispatched_at=2026-09-01 21:05 and spark frozen — a flywheel that
    looked stopped precisely because it was spinning without accounting.

    Pinned structurally, like its speculative twin: the parallel attempt must
    sit AFTER the stamp, and nothing may `continue` between it and the gate."""
    import inspect
    import integrations.agent_engine.agent_daemon as ad
    src = inspect.getsource(ad.AgentDaemon._tick) if hasattr(ad, 'AgentDaemon') \
        else open(ad.__file__, encoding='utf-8').read()

    stamp = src.index('goal.last_dispatched_at = datetime.utcnow()')
    attempt = src.index('self._try_parallel_dispatch(')
    assert attempt > stamp, (
        '_try_parallel_dispatch runs before the last_dispatched_at stamp, so a '
        'parallel goal is dispatched without ever being recorded as dispatched')

    settle = src.index('_settle_dispatched_goal(db, goal, goal_key)')
    assert settle > attempt, 'the settlement gate no longer follows the parallel branch'
    between = src[attempt:settle]
    assert 'parallel-handoff' in between, \
        'the handoff sentinel is gone — the fall-through fix was reverted'
    assert '\n                    continue' not in between, \
        'a continue crept back between the parallel handoff and settlement'


def test_a_parallel_handoff_does_not_also_dispatch_the_goal_prompt():
    """Fanning out subtasks and then ALSO dispatching the parent prompt would
    double-spend the goal. The direct dispatch must be gated on the shared
    handoff flag, not on the speculative flag alone."""
    import inspect
    import integrations.agent_engine.agent_daemon as ad
    src = inspect.getsource(ad.AgentDaemon._tick) if hasattr(ad, 'AgentDaemon') \
        else open(ad.__file__, encoding='utf-8').read()
    assert 'if not handed_off:' in src, \
        'the direct-dispatch guard no longer covers every handoff style'
    assert 'if not speculated:' not in src, \
        'the guard still reads the speculation-only flag, so a parallel ' \
        'handoff falls through and dispatches the parent prompt as well'


# ===================================================================
# Grounding: spark is a proxy, and it was satisfied before the work
# ===================================================================
#
# Measured on central 2026-09-02.  Two proxies both went true before the goal's
# work existed:
#   - spark_spent is a LIFETIME counter and the gate compared it to zero, so a
#     goal that ever spent spark passed forever (a4734e50 carried 101 from the
#     previous day).
#   - the distributed branch returns the submitted goal id the instant
#     submit_goal returns, so the gate ran before any work existed: a4734e50
#     was completed at 12:39:36, its task claimed at 12:43:46, finished ~12:47.


class _Coordinator:
    """Seam-fake for the distributed coordinator's own accounting."""

    def __init__(self, progress):
        self._progress = progress

    def get_goal_progress(self, goal_id):
        return self._progress


def _with_coordinator(progress):
    """Patch the coordinator the gate imports from .dispatch."""
    coord = _Coordinator(progress) if progress is not None else None
    return patch('integrations.agent_engine.dispatch._get_distributed_coordinator',
                 return_value=coord)


def test_lifetime_spark_alone_no_longer_completes_a_goal():
    """THE regression: 101 spark from yesterday, nothing spent this dispatch."""
    g = Goal(spark=101, cfg={'spark_at_dispatch': 101})
    _settle_dispatched_goal(Db(), g, 'g1')
    assert g.status == 'active'
    assert g.config_json['noop_dispatch_count'] == 1,         'a dispatch that spent nothing new is a noop, whatever the lifetime total'


def test_new_spend_completes_when_there_is_no_ledger_entry():
    """Purely local dispatch: no coordinator opinion, spend is the evidence."""
    g = Goal(spark=141, cfg={'spark_at_dispatch': 101})
    with _with_coordinator(None):
        _settle_dispatched_goal(Db(), g, 'g1')
    assert g.status == 'completed'
    assert g.config_json['completion_grounding'] == 'local_dispatch_spend'


def test_outstanding_ledger_tasks_block_completion_without_a_noop_strike():
    g = Goal(spark=141, cfg={'spark_at_dispatch': 101})
    with _with_coordinator({'total_tasks': 1, 'completed': 0}):
        _settle_dispatched_goal(Db(), g, 'g1')
    assert g.status == 'active', 'work in flight must not be reported complete'
    assert 'awaiting_verification_since' in g.config_json
    assert 'noop_dispatch_count' not in g.config_json,         'in-flight work must not collect noop strikes or it auto-pauses in 5 ticks'


def test_all_ledger_tasks_done_completes_and_records_the_grounding():
    g = Goal(spark=141, cfg={'spark_at_dispatch': 101,
                             'awaiting_verification_since': '2026-09-02T12:39:36'})
    with _with_coordinator({'total_tasks': 2, 'completed': 2}):
        _settle_dispatched_goal(Db(), g, 'g1')
    assert g.status == 'completed'
    assert g.config_json['completion_grounding'] == 'ledger_tasks_complete'
    assert 'awaiting_verification_since' not in g.config_json,         'completion must clear the waiting trail'


def test_waiting_forever_is_bounded_by_a_pause_with_its_own_reason():
    stale = (datetime.utcnow() - timedelta(seconds=3600)).isoformat()
    g = Goal(spark=141, cfg={'spark_at_dispatch': 101,
                             'awaiting_verification_since': stale})
    with _with_coordinator({'total_tasks': 1, 'completed': 0}):
        _settle_dispatched_goal(Db(), g, 'g1')
    assert g.status == 'paused'
    assert 'ledger never reported every task done' in g.config_json['pause_reason']


def test_an_unreadable_wait_timestamp_keeps_waiting_rather_than_pausing():
    """A clock or format problem must never be why a goal gets paused."""
    g = Goal(spark=141, cfg={'spark_at_dispatch': 101,
                             'awaiting_verification_since': 'not-a-timestamp'})
    with _with_coordinator({'total_tasks': 1, 'completed': 0}):
        _settle_dispatched_goal(Db(), g, 'g1')
    assert g.status == 'active'


def test_a_broken_coordinator_does_not_complete_or_block_the_goal():
    """No opinion from the ledger degrades to the spend test, not to a stall."""
    class Exploding:
        def get_goal_progress(self, goal_id):
            raise RuntimeError('redis gone')

    g = Goal(spark=141, cfg={'spark_at_dispatch': 101})
    with patch('integrations.agent_engine.dispatch._get_distributed_coordinator',
               return_value=Exploding()):
        _settle_dispatched_goal(Db(), g, 'g1')
    assert g.status == 'completed'
    assert g.config_json['completion_grounding'] == 'local_dispatch_spend'


def test_a_goal_dispatched_before_this_code_shipped_still_settles():
    """No spark_at_dispatch key: degrade to the old lifetime test, never block."""
    g = Goal(spark=7)
    with _with_coordinator(None):
        _settle_dispatched_goal(Db(), g, 'g1')
    assert g.status == 'completed'


def test_continuous_goals_are_still_never_auto_completed_even_when_grounded():
    g = Goal(spark=141, cfg={'continuous': True, 'spark_at_dispatch': 101})
    with _with_coordinator({'total_tasks': 1, 'completed': 1}):
        _settle_dispatched_goal(Db(), g, 'g1')
    assert g.status == 'active'
