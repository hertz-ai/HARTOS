"""The coding daemon runs one worker, and its goals pass through the one
completion gate.

Measured on the owner's desktop 2026-10-10:
- two live threads named coding_daemon (py-spy dump of the Nunba process),
  and "Coding daemon: dispatched 2 goal(s)" 341 times in 107 minutes, every
  ~19 s against a 30 s tick.  start() spawned a new worker whenever
  _running was False, also while the previous worker was still alive inside
  a long turn (stop() waits ten seconds; the watchdog then restarts).
  AgentDaemon.start has refused that since 2026-08-17.
- the two active self-heal goals were dispatched every 30 s from 09-15 to
  10-10 and never settled: this daemon never called
  agent_daemon._settle_dispatched_goal, the gate whose docstring says every
  dispatched goal must pass through it, so a coding goal could neither
  complete nor stop.

The failure counter's test is a pin, not a fail-first test: it passes on the
old code too (config_json is a MutableDict column, so the in-place count was
always written).  It holds the count on the row now that the gate, which
reloads the committed config, runs on the same tick.

Behavioural: the real CodingAgentDaemon (start, _tick), the real
_settle_dispatched_goal, and for the counter a real SQLite file read back in
a new session.  Replaced: the idle personas, the yield and affordability
gates, the host-core ceiling, the coordinator lookup and the /chat dispatch.

    python -m pytest tests/unit/test_coding_daemon_settles_its_goals.py -q
"""
import os
import sys
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from integrations.coding_agent.coding_daemon import CodingAgentDaemon  # noqa: E402

_FIX_CONFIG = {
    'mode': 'self_heal',
    'pattern_key': 'RuntimeError::tts.probe::probe',
    'exc_type': 'RuntimeError',
    'source_module': 'tts.probe',
    'source_function': 'probe',
    'occurrence_count': 3,
    'sample_traceback': 'Traceback (most recent call last): ...',
}


class _Goal:
    """An AgentGoal row as _tick and the gate read it."""

    def __init__(self, gid='fix', spark=0):
        self.id = gid
        self.goal_type = 'self_heal'
        self.title = f'goal {gid}'
        self.description = ''
        self.status = 'active'
        self.priority = 0
        self.last_dispatched_at = None
        self.config_json = dict(_FIX_CONFIG)
        self.spark_spent = spark

    def to_dict(self):
        d = {'id': self.id, 'goal_type': self.goal_type, 'title': self.title,
             'description': self.description, 'status': self.status,
             'priority': self.priority, 'last_dispatched_at': None}
        d.update(self.config_json)
        return d


def _tick(goal, dispatch, db=None):
    """One real _tick over ``goal``, 30 s after its last dispatch."""
    goal.last_dispatched_at = None
    if db is None:
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value \
            .all.return_value = [goal]
    with patch('integrations.social.models.get_db', return_value=db), \
            patch('integrations.coding_agent.idle_detection.'
                  'IdleDetectionService.get_idle_agent_personas',
                  return_value=[{'user_id': 'agent-1', 'username': 'a1'}]), \
            patch('integrations.agent_engine.dispatch.should_yield_to_user',
                  return_value=False), \
            patch('integrations.agent_engine.budget_gate.'
                  'check_platform_affordability', return_value=(True, {})), \
            patch('integrations.agent_engine.dispatch.'
                  'max_autonomous_concurrency', side_effect=lambda cap: cap), \
            patch('integrations.agent_engine.dispatch.'
                  '_get_distributed_coordinator', return_value=None), \
            patch('integrations.agent_engine.dispatch.is_transient_deferral',
                  return_value=False), \
            patch('integrations.coding_agent.task_distributor.'
                  'dispatch_to_chat', dispatch):
        CodingAgentDaemon()._tick()


# ── one worker ───────────────────────────────────────────────────────────

def test_start_re_arms_a_live_worker_instead_of_spawning_a_second():
    """FAILS BEFORE THE FIX: a second worker was spawned beside the first."""
    d = CodingAgentDaemon()
    release = threading.Event()
    started = []

    def _worker():                    # a worker inside a long turn
        started.append(threading.current_thread())
        release.wait(10)

    try:
        with patch.object(d, '_loop', side_effect=_worker):
            d.start()
            for _ in range(50):
                if started:
                    break
                time.sleep(0.02)
            # What stop() leaves when its ten-second join gives up: the flag
            # is down, the worker is still alive.
            with d._lock:
                d._running = False
            d.start()                 # the watchdog's restart
            time.sleep(0.2)
            assert len(started) == 1, (
                f'{len(started)} workers started: a second one ran beside '
                f'the first')
            assert d._running is True, 'the live worker was not re-armed'
            assert d._thread is started[0]
    finally:
        release.set()
        d._running = False


def test_start_after_the_worker_has_gone_spawns_a_new_one():
    d = CodingAgentDaemon()
    calls = []
    with patch.object(d, '_loop', side_effect=lambda: calls.append(1)):
        d.start()
        d._thread.join(2)
        with d._lock:
            d._running = False
        d.start()
        d._thread.join(2)
    d._running = False
    assert len(calls) == 2


# ── the one completion gate ──────────────────────────────────────────────

def test_a_coding_goal_whose_turns_do_no_work_is_paused():
    """FAILS BEFORE THE FIX: dispatched forever, never settled."""
    goal = _Goal()
    dispatch = MagicMock(return_value='Looked at the log; nothing to change.')
    for _ in range(5):
        _tick(goal, dispatch)
    assert dispatch.call_count == 5
    assert goal.status == 'paused'
    assert '0 spark' in goal.config_json['pause_reason']


def test_a_coding_goal_whose_turn_charged_spark_completes():
    """FAILS BEFORE THE FIX: a turn that did the work left the goal active,
    to be dispatched again 30 s later."""
    goal = _Goal(spark=4)

    def _turn(*a, **k):
        goal.spark_spent += 3         # charge_goal_work_completed, mid-turn
        return 'Patched tts.probe; the probe passes.'

    _tick(goal, MagicMock(side_effect=_turn))
    assert goal.status == 'completed'
    assert goal.config_json['completion_grounding'] == 'local_dispatch_spend'


def test_a_coding_goal_handed_to_the_hive_waits_for_its_run():
    """dispatch_goal gave the goal to the coordinator (it records the route
    and returns the goal id, as dispatch_goal does on that branch) and the
    task is still queued: the goal waits, it does not collect noop strikes."""
    from integrations.agent_engine import dispatch as d
    goal = _Goal()

    def _handed_off(prompt, user_id, goal_id, goal_type=None):
        d._record_route(goal_id, 'hive')
        return str(goal_id)

    queued = MagicMock()
    queued.get_goal_progress.return_value = {'total_tasks': 1, 'completed': 0}
    with patch('integrations.agent_engine.dispatch.'
               '_get_distributed_coordinator', return_value=queued):
        for _ in range(5):
            goal.last_dispatched_at = None
            db = MagicMock()
            db.query.return_value.filter.return_value.order_by.return_value \
                .all.return_value = [goal]
            with patch('integrations.social.models.get_db', return_value=db), \
                    patch('integrations.coding_agent.idle_detection.'
                          'IdleDetectionService.get_idle_agent_personas',
                          return_value=[{'user_id': 'agent-1',
                                         'username': 'a1'}]), \
                    patch('integrations.agent_engine.dispatch.'
                          'should_yield_to_user', return_value=False), \
                    patch('integrations.agent_engine.budget_gate.'
                          'check_platform_affordability',
                          return_value=(True, {})), \
                    patch('integrations.agent_engine.dispatch.'
                          'max_autonomous_concurrency',
                          side_effect=lambda cap: cap), \
                    patch('integrations.coding_agent.task_distributor.'
                          'dispatch_to_chat', side_effect=_handed_off):
                CodingAgentDaemon()._tick()
    assert goal.status == 'active', goal.config_json.get('pause_reason')
    assert 'noop_dispatch_count' not in goal.config_json
    assert 'awaiting_verification_since' in goal.config_json


def test_a_held_turn_is_neither_settled_nor_counted():
    goal = _Goal()
    with patch('integrations.coding_agent.coding_daemon._held_for_later',
               return_value=True):
        _tick(goal, MagicMock(return_value=None))
    assert goal.status == 'active'
    assert 'noop_dispatch_count' not in goal.config_json
    assert '_dispatch_failures' not in goal.config_json


# ── the failure counter reaches the row ─────────────────────────────────

sqlalchemy = pytest.importorskip('sqlalchemy')


@pytest.fixture
def db_path(monkeypatch):
    d = tempfile.mkdtemp()
    p = os.path.join(d, 'hevolve_database.db')
    monkeypatch.setenv('HEVOLVE_DB_PATH', p)
    monkeypatch.setenv('HEVOLVE_DB_URL', 'sqlite:///' + p.replace('\\', '/'))
    from integrations.social import models
    for attr in ('_engine', '_ENGINE', '_engine_cache', '_SessionLocal'):
        if hasattr(models, attr):
            monkeypatch.setattr(models, attr, None, raising=False)
    return p


def test_a_failed_dispatch_is_counted_on_the_row(db_path):
    """The count a failed dispatch adds reaches the row (read back in a new
    session), so five failures still pause the goal."""
    from integrations.social.migrations import run_migrations
    from integrations.social.models import AgentGoal, get_db

    run_migrations()
    db = get_db()
    row = AgentGoal(id='fix-row', goal_type='self_heal', title='Self-heal',
                    status='active')
    row.config_json = dict(_FIX_CONFIG)
    row.spark_budget = 200
    row.spark_spent = 0
    db.add(row)
    db.commit()
    db.close()

    with patch('integrations.coding_agent.idle_detection.'
               'IdleDetectionService.get_idle_agent_personas',
               return_value=[{'user_id': 'agent-1', 'username': 'a1'}]), \
            patch('integrations.agent_engine.dispatch.should_yield_to_user',
                  return_value=False), \
            patch('integrations.agent_engine.budget_gate.'
                  'check_platform_affordability', return_value=(True, {})), \
            patch('integrations.agent_engine.dispatch.'
                  'max_autonomous_concurrency', side_effect=lambda cap: cap), \
            patch('integrations.agent_engine.dispatch.is_transient_deferral',
                  return_value=False), \
            patch('integrations.coding_agent.task_distributor.'
                  'dispatch_to_chat', MagicMock(return_value=None)):
        CodingAgentDaemon()._tick()

    db2 = get_db()
    back = db2.query(AgentGoal).filter_by(id='fix-row').first()
    assert (back.config_json or {}).get('_dispatch_failures') == 1, (
        f'the failure count did not reach the row: {back.config_json}')
    assert back.last_dispatched_at is not None
    db2.close()


def test_a_dispatch_that_ran_clears_the_count_and_is_settled_on_the_row(db_path):
    """A turn ran after two failures: the count goes and the gate's verdict
    (a noop strike, no spark) is what the row holds.  Cleared before the
    gate, the count came back with the committed config the gate reloads."""
    from integrations.social.migrations import run_migrations
    from integrations.social.models import AgentGoal, get_db

    run_migrations()
    db = get_db()
    row = AgentGoal(id='fix-ran', goal_type='self_heal', title='Self-heal',
                    status='active')
    row.config_json = dict(_FIX_CONFIG, _dispatch_failures=2)
    row.spark_budget = 200
    row.spark_spent = 0
    db.add(row)
    db.commit()
    db.close()

    with patch('integrations.coding_agent.idle_detection.'
               'IdleDetectionService.get_idle_agent_personas',
               return_value=[{'user_id': 'agent-1', 'username': 'a1'}]), \
            patch('integrations.agent_engine.dispatch.should_yield_to_user',
                  return_value=False), \
            patch('integrations.agent_engine.budget_gate.'
                  'check_platform_affordability', return_value=(True, {})), \
            patch('integrations.agent_engine.dispatch.'
                  'max_autonomous_concurrency', side_effect=lambda cap: cap), \
            patch('integrations.agent_engine.dispatch.'
                  '_get_distributed_coordinator', return_value=None), \
            patch('integrations.coding_agent.task_distributor.'
                  'dispatch_to_chat',
                  MagicMock(return_value='Read the log; no change made.')):
        CodingAgentDaemon()._tick()

    db2 = get_db()
    back = db2.query(AgentGoal).filter_by(id='fix-ran').first()
    cfg = back.config_json or {}
    assert '_dispatch_failures' not in cfg, cfg
    assert cfg.get('noop_dispatch_count') == 1, cfg
    assert back.status == 'active'
    db2.close()
