"""A goal handed to the hive is judged by its hive run, not by 0 spark at
the instant of the handoff.

Measured on the owner's desktop 2026-10-10 (read-only, ~/Documents/Nunba):
- 67 of the 72 seed goals were paused.  21 carried "5 consecutive
  dispatches produced 0 spark".  With two active peer_nodes rows every
  dispatch_goal went to the coordinator, which returns the goal id the
  moment submit_goal returns, so the completion gate ran before any work
  existed and struck a noop every tick.  A charge made later, when the
  worker's flow completed, landed between two ticks, after the next tick's
  spark_at_dispatch snapshot, and was never attributed.
- Two self-heal goals were "submitted" every 30 s into a task set completed
  on 09-15.  submit_goal dedups onto the set it holds and re-opens it only
  for a continuous goal, so the submit did nothing and still came back as a
  handoff.

Behavioural: the real AgentDaemon._tick, dispatch_goal,
dispatch_goal_distributed, _settle_dispatched_goal, DistributedTaskCoordinator
(in-memory ledger and lock) and DistributedWorkerLoop._tick.  Replaced at the
boundaries only: the DB session and goal row, the idle personas, the yield,
budget and guardrail gates, the prompt builder, the peer count, the /chat
turn (local_chat_dispatch, which charges spark the way
charge_goal_work_completed does when a flow completes), the data directory,
the audit log and the world-model bridge.

    python -m pytest tests/unit/test_hive_handoff_is_settled_by_its_run.py -q
"""
import contextlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for _p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from agent_ledger.backends import InMemoryBackend  # noqa: E402
from agent_ledger.core import SmartLedger, TaskStatus  # noqa: E402
from integrations.agent_engine import agent_daemon, dispatch  # noqa: E402
from integrations.agent_engine.agent_daemon import AgentDaemon  # noqa: E402
from integrations.distributed_agent.coordinator_backends import (  # noqa: E402
    InMemoryTaskLock)
from integrations.distributed_agent.task_coordinator import (  # noqa: E402
    DistributedTaskCoordinator)
from integrations.distributed_agent.worker_loop import (  # noqa: E402
    DistributedWorkerLoop)
from tests.unit.module_swap import swap_modules  # noqa: E402

GOAL = 'goal-hive-1'
HERE = 'node-here-0a1b'


class Goal:
    """Stands in for an AgentGoal row (the attributes _tick reads)."""

    def __init__(self, gid=GOAL, cfg=None, spark=0):
        self.id = gid
        self.goal_type = 'marketing'
        self.title = 'Recruit compute'
        self.description = ''
        self.status = 'active'
        self.config_json = dict(cfg or {})
        self.spark_spent = spark
        self.spark_budget = 500
        self.last_dispatched_at = None
        self.product_id = None
        self.owner_id = None
        self.prompt_id = None
        self.flow_id = None

    def to_dict(self):
        d = {'id': self.id, 'goal_type': self.goal_type, 'title': self.title,
             'description': self.description, 'status': self.status}
        d.update(self.config_json)
        return d


class _Rows:
    """db_session() for dispatch's goal-row lookups: the one goal."""

    def __init__(self, goal):
        self._goal = goal

    def query(self, _model):
        return self

    def filter_by(self, **kw):
        return self

    def first(self):
        return self._goal


@pytest.fixture
def coord(tmp_path, monkeypatch):
    monkeypatch.setenv('HEVOLVE_DB_PATH', ':memory:')
    from core.file_cache import invalidate_file_cache
    invalidate_file_cache()
    led = SmartLedger('coord', 'hive', str(tmp_path), backend=InMemoryBackend())
    co = DistributedTaskCoordinator(ledger=led, task_lock=InMemoryTaskLock(),
                                    verifier=MagicMock(), baseline=MagicMock())
    with patch('core.platform_paths.get_agent_data_dir',
               return_value=str(tmp_path)), \
         patch('integrations.distributed_agent.requesters.this_node_id',
               return_value=HERE):
        yield co
    invalidate_file_cache()


def _boundaries(goal, coord, chat, peers=True):
    """The patches every tick here runs under."""
    @contextlib.contextmanager
    def _session(*a, **k):
        yield _Rows(goal)

    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [goal]
    return [
        patch('integrations.social.models.get_db', return_value=db),
        patch('integrations.social.models.db_session', _session),
        patch('integrations.coding_agent.idle_detection.IdleDetectionService.'
              'get_idle_agent_personas',
              return_value=[{'user_id': 'agent-1', 'username': 'a1'}]),
        patch.object(dispatch, 'should_yield_to_user', return_value=False),
        patch.object(dispatch, 'local_dispatch_provider_breaker_open',
                     return_value=''),
        patch.object(dispatch, 'local_dispatch_llm_busy', return_value=False),
        patch.object(dispatch, '_dispatch_provider_host', return_value=''),
        patch.object(agent_daemon, '_flow_recipe_exists', return_value=True),
        patch('integrations.agent_engine.goal_manager.GoalManager.build_prompt',
              return_value='Recruit compute for the hive'),
        patch('integrations.agent_engine.budget_gate.estimate_llm_cost_spark',
              return_value=0),
        patch('integrations.agent_engine.budget_gate.pre_dispatch_budget_gate',
              return_value=(True, 'OK')),
        patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
              side_effect=lambda prompt, *a, **k: (True, '', prompt)),
        patch('security.hive_guardrails.GuardrailEnforcer.after_response',
              return_value=(True, '')),
        patch('security.immutable_audit_log.get_audit_log',
              return_value=MagicMock()),
        patch('integrations.agent_engine.world_model_bridge.'
              'get_world_model_bridge', return_value=MagicMock()),
        patch.object(AgentDaemon, '_try_parallel_dispatch',
                     return_value={'completed': 0, 'failed': 0, 'deferred': 0}),
        # The real decomposition also writes a per-goal SmartLedger file to
        # the working directory; the coordinator only needs its task list.
        patch.object(dispatch, '_decompose_goal',
                     side_effect=lambda prompt, gid, gt, uid: [{
                         'task_id': f'{gid}_task_0', 'description': prompt,
                         'capabilities': []}]),
        patch.object(dispatch, '_get_distributed_coordinator',
                     return_value=coord),
        patch.object(dispatch, '_has_hive_peers', return_value=peers),
        patch.object(dispatch, 'local_chat_dispatch', chat),
        swap_modules({'routes.hartos_backend_adapter': None}),
    ]


def _run(patches, fn):
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        fn()


def daemon_tick(goal, coord, chat, peers=True):
    """One real agent-daemon tick over ``goal``."""
    agent_daemon._dispatch_backoff.pop(str(goal.id), None)
    agent_daemon._budget_blocked_goals.discard(str(goal.id))
    goal.last_dispatched_at = None
    _run(_boundaries(goal, coord, chat, peers), lambda: AgentDaemon()._tick())


def worker_tick(goal, coord, chat):
    """One real distributed-worker tick on this node."""
    loop = DistributedWorkerLoop()
    loop._node_id = HERE
    with patch.object(loop, '_get_coordinator', return_value=coord):
        _run(_boundaries(goal, coord, chat), loop._tick)


def charging_chat(goal, spark=2, reply='The compute drive is planned.'):
    """The /chat turn: its flow completes and charges the goal's spark,
    which is what charge_goal_work_completed does inside the pipeline."""
    def _turn(*a, **k):
        goal.spark_spent = (goal.spark_spent or 0) + spark
        return ('ok', reply)
    return MagicMock(side_effect=_turn)


def _child(coord, gid=GOAL):
    return coord._ledger.get_task(f'{gid}_task_0')


# ── in flight ────────────────────────────────────────────────────────────

def test_work_waiting_on_the_hive_is_not_a_noop(coord):
    """FAILS BEFORE THE FIX: five ticks while the task waits in the queue
    paused the goal with "5 dispatches produced 0 spark"."""
    goal = Goal()
    chat = charging_chat(goal)
    for _ in range(5):
        daemon_tick(goal, coord, chat)

    assert goal.status == 'active', goal.config_json.get('pause_reason')
    assert 'noop_dispatch_count' not in goal.config_json
    assert 'awaiting_verification_since' in goal.config_json
    assert _child(coord).status == TaskStatus.PENDING
    assert chat.call_count == 0, 'a handoff must not also run the turn here'


def test_a_run_the_worker_finished_completes_the_goal(coord):
    """FAILS BEFORE THE FIX: the worker ran the task and its flow charged
    spark between two ticks; the next tick's snapshot already held that
    spark, so the gate saw 0 and struck a noop instead of completing."""
    goal = Goal()
    chat = charging_chat(goal)
    daemon_tick(goal, coord, chat)              # handed to the hive
    assert chat.call_count == 0

    worker_tick(goal, coord, chat)              # this node's worker runs it
    assert _child(coord).status == TaskStatus.COMPLETED
    assert goal.spark_spent == 2

    daemon_tick(goal, coord, chat)              # the next tick settles it
    assert goal.status == 'completed', goal.config_json
    assert goal.config_json['completion_grounding'] == 'ledger_tasks_complete'
    assert 'spark_at_handoff' not in goal.config_json
    assert 'awaiting_verification_since' not in goal.config_json
    assert chat.call_count == 1, 'the finished run must not be run again here'


def test_a_hive_run_that_charged_nothing_is_a_noop(coord):
    """The run finished but its turn completed no flow: no spark.  Not
    work, so a noop, and the run's marks go so the next dispatch starts
    afresh."""
    goal = Goal()
    chat = MagicMock(return_value=('ok', 'I need more detail to continue.'))
    daemon_tick(goal, coord, chat)
    worker_tick(goal, coord, chat)
    assert _child(coord).status == TaskStatus.COMPLETED

    daemon_tick(goal, coord, chat)
    assert goal.status == 'active'
    assert goal.config_json['noop_dispatch_count'] == 1
    assert 'spark_at_handoff' not in goal.config_json
    assert 'awaiting_verification_since' not in goal.config_json


# ── a finished task set is not a handoff ─────────────────────────────────

def _finished_set(coord, gid=GOAL):
    coord.submit_goal('Recruit compute', [
        {'task_id': f'{gid}_task_0', 'description': 'Recruit compute',
         'capabilities': []}], {'prompt': 'Recruit compute'}, goal_id=gid)
    task = coord.claim_next_task('earlier-run', [])
    coord.submit_result(task.task_id, 'earlier-run', 'done in September')
    assert _child(coord, gid).status == TaskStatus.COMPLETED


def test_a_goal_whose_hive_set_is_finished_runs_here(coord):
    """FAILS BEFORE THE FIX: the self-heal shape.  The set finished long
    ago, the goal is active again, and every tick "submitted" it into the
    finished set, where nothing could run it."""
    _finished_set(coord)
    goal = Goal()
    chat = charging_chat(goal)
    daemon_tick(goal, coord, chat)

    assert chat.call_count == 1, 'the goal never ran: the finished set took it'
    assert goal.status == 'completed'
    assert goal.config_json['completion_grounding'] == 'ledger_tasks_complete'


def test_a_finished_run_still_waiting_to_be_settled_is_not_run_again(coord):
    """The other side: a run handed off and finished since is settled, not
    sent to the turn a second time."""
    _finished_set(coord)
    goal = Goal(cfg={'spark_at_handoff': 0,
                     'awaiting_verification_since': '2026-10-10T05:00:00'},
                spark=3)
    chat = charging_chat(goal)
    daemon_tick(goal, coord, chat)

    assert chat.call_count == 0
    assert goal.status == 'completed'


# ── a wait that runs out ends with the pause ─────────────────────────────

class _Progress:
    def __init__(self, progress):
        self._p = progress

    def get_goal_progress(self, goal_id):
        return self._p


class _Db:
    def refresh(self, *a, **k):
        pass

    def flush(self):
        pass


def test_a_wait_that_runs_out_pauses_and_a_resume_starts_a_fresh_wait():
    """The pause bounds the wait, and the wait's marks go with it: left in
    place, a resumed goal's first settle read the old wait as expired and
    paused it again on the spot."""
    from datetime import datetime, timedelta
    stale = (datetime.utcnow() - timedelta(seconds=3600)).isoformat()
    goal = Goal(cfg={'spark_at_dispatch': 0, 'spark_at_handoff': 0,
                     'awaiting_verification_since': stale})
    outstanding = _Progress({'total_tasks': 1, 'completed': 0})
    with patch.object(dispatch, '_get_distributed_coordinator',
                      return_value=outstanding):
        agent_daemon._settle_dispatched_goal(_Db(), goal, GOAL,
                                             handed_to_hive=True)
        assert goal.status == 'paused'
        assert 'ledger never reported every task done' in \
            goal.config_json['pause_reason']
        assert 'awaiting_verification_since' not in goal.config_json
        assert 'spark_at_handoff' not in goal.config_json

        goal.status = 'active'               # the owner resumes it
        agent_daemon._settle_dispatched_goal(_Db(), goal, GOAL,
                                             handed_to_hive=True)
    assert goal.status == 'active', goal.config_json.get('pause_reason')
    assert 'awaiting_verification_since' in goal.config_json


# ── the local route is unchanged ─────────────────────────────────────────

def test_with_no_other_node_the_turn_runs_here_and_its_spark_completes(coord):
    goal = Goal()
    chat = charging_chat(goal)
    daemon_tick(goal, coord, chat, peers=False)

    assert chat.call_count == 1
    assert goal.status == 'completed'
    assert goal.config_json['completion_grounding'] == 'local_dispatch_spend'


def test_a_local_turn_that_charged_nothing_is_still_a_noop(coord):
    goal = Goal()
    chat = MagicMock(return_value=('ok', 'Nothing to do.'))
    daemon_tick(goal, coord, chat, peers=False)

    assert goal.status == 'active'
    assert goal.config_json['noop_dispatch_count'] == 1


# ── the route is said once per change ───────────────────────────────────

def test_the_route_is_logged_when_it_changes_not_every_tick(coord, caplog):
    import logging
    goal = Goal(gid='goal-route-1')
    chat = MagicMock(return_value=('ok', 'Nothing to do.'))
    with caplog.at_level(logging.INFO, logger='hevolve_social'):
        daemon_tick(goal, coord, chat)
        daemon_tick(goal, coord, chat)
        goal.status = 'active'
        daemon_tick(goal, coord, chat, peers=False)
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith('Goal goal-route-1: dispatched')]
    assert lines == [
        'Goal goal-route-1: dispatched to the hive; was not dispatched yet',
        'Goal goal-route-1: dispatched here (no other active node); was hive',
    ], lines
