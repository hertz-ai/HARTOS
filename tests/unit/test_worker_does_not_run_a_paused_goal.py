"""A hive worker never runs the work of a goal that is not running (#194).

Measured on central 2026-10-26 (container log, 6 h, read-only): 176 claims of
hive tasks whose goals were paused, and 176 "transitioned to blocked: orphan
recovery: worker claim expired".  Each run was a multi-call agent turn on the
free model that failed (#115: the 400 and the free tier's 429); the worker
released the claim (abandon_task), orphan recovery put the task back to
PENDING once the claim was _ORPHAN_AFTER_S old, and the next tick ran it
again: about 35 runs per task in 6 h, ~1,470 model calls a day, no output, and
the free tier's daily cap spent before any turn that could earn.  Pausing a
goal never reached its hive task, and the worker never asked whether the goal
still ran.

The worker now asks, after the claim and before the turn.  A goal this node
holds that is not active (paused, completed, archived) has its task held
(coordinator.hold_task, the parking a goal waiting on a person already gets),
and the goal's next dispatch, which the daemon makes only for an active goal,
brings it back (_release_held_tasks).  A goal this node does not hold (a
peer's work) runs as before.

Real DistributedTaskCoordinator, SmartLedger, in-memory lock and in-memory
social DB; only the turn itself (_execute_task) is recorded.

    python -m pytest tests/unit/test_worker_does_not_run_a_paused_goal.py -q
"""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_ledger.core import SmartLedger, TaskStatus  # noqa: E402
from integrations.social.models import (  # noqa: E402
    AgentGoal, Base, db_session, get_engine)

_NEEDS_HELP = ('Needs help: action 2 (present top options to the customer) '
               'could not be done autonomously: the verifier says it needs input')


@pytest.fixture(autouse=True)
def _worker_may_claim(monkeypatch):
    """The daemon gate is open (pinned in
    test_worker_tick_claims_nothing_it_would_defer.py), so a tick here does
    not inherit this box's pressure readings."""
    from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
    monkeypatch.setattr(DistributedWorkerLoop, '_dispatch_would_defer',
                        staticmethod(lambda: None))


@pytest.fixture
def db():
    Base.metadata.create_all(get_engine())


class _MemBackend:
    def __init__(self):
        self.data = {}

    def load(self, key):
        return self.data.get(key)

    def save(self, key, data):
        self.data[key] = data

    def exists(self, key):
        return key in self.data


def _goal(status, pause_reason=''):
    with db_session() as s:
        g = AgentGoal(goal_type='marketing', title='Grow the network',
                      description='', status=status,
                      config_json={'pause_reason': pause_reason} if pause_reason else {})
        s.add(g)
        s.flush()
        return str(g.id)


def _submit(co, goal_id):
    """What dispatch_goal_distributed submits: the goal's own id as the root."""
    co.submit_goal('obj', [{'task_id': f'{goal_id}_task_0',
                            'description': 'post the weekly digest'}],
                   {'user_id': 'h-1', 'prompt': 'obj'}, goal_id=goal_id)


def _coordinator(goal_id):
    from integrations.distributed_agent.coordinator_backends import InMemoryTaskLock
    from integrations.distributed_agent.task_coordinator import (
        DistributedTaskCoordinator)
    led = SmartLedger(agent_id='coord', session_id='s', backend=_MemBackend())
    co = DistributedTaskCoordinator(
        ledger=led, task_lock=InMemoryTaskLock(),
        verifier=MagicMock(), baseline=MagicMock())
    _submit(co, goal_id)
    return led, co


def _tick(co, result=None):
    """One worker tick against ``co``; returns the task ids a turn ran for.
    ``result`` None is the measured case: the turn failed."""
    from integrations.distributed_agent.worker_loop import DistributedWorkerLoop
    loop = DistributedWorkerLoop()
    loop._node_id = 'node-a'
    turns = []

    def _turn(task):
        turns.append(task.task_id)
        return result

    with patch.object(loop, '_get_coordinator', return_value=co), \
         patch.object(loop, '_execute_task', side_effect=_turn):
        loop._tick()
    return turns


@pytest.mark.parametrize('status', ['paused', 'completed', 'archived'])
def test_a_goal_that_is_not_running_has_its_task_held_not_run(db, status):
    gid = _goal(status, _NEEDS_HELP if status == 'paused' else '')
    led, co = _coordinator(gid)
    assert _tick(co) == []
    task = led.get_task(f'{gid}_task_0')
    assert task.status == TaskStatus.BLOCKED
    assert task.blocked_reason == 'input_required'
    assert f'the goal is {status}' in task.error_message
    assert not co._lock.is_task_locked(task.task_id), 'the claim was not released'


def test_a_paused_goals_task_is_not_run_again_an_orphan_window_later(db):
    """The measured cycle: a failed turn released the claim, orphan recovery
    returned the task once the claim was old, and the next tick ran it
    again."""
    from integrations.distributed_agent import task_coordinator as tc
    gid = _goal('paused', _NEEDS_HELP)
    led, co = _coordinator(gid)
    turns = _tick(co)
    task = led.get_task(f'{gid}_task_0')
    task.context['claimed_at'] = (
        datetime.now() - timedelta(seconds=tc._ORPHAN_AFTER_S + 60)).isoformat()
    turns += _tick(co)
    assert turns == []


def test_the_goal_resumed_and_dispatched_again_runs_its_task(db):
    gid = _goal('paused', _NEEDS_HELP)
    led, co = _coordinator(gid)
    _tick(co)
    with db_session() as s:
        s.query(AgentGoal).filter_by(id=gid).first().status = 'active'
    _submit(co, gid)          # the daemon dispatches only an active goal
    assert led.get_task(f'{gid}_task_0').status == TaskStatus.PENDING
    assert _tick(co) == [f'{gid}_task_0']


def test_an_active_goals_task_runs(db):
    gid = _goal('active')
    _led, co = _coordinator(gid)
    assert _tick(co) == [f'{gid}_task_0']


def test_a_task_of_a_goal_this_node_does_not_hold_runs(db):
    """A peer's goal has no row here: its work runs as before."""
    _led, co = _coordinator('peer-goal-7')
    assert _tick(co) == ['peer-goal-7_task_0']


def test_a_config_key_named_status_does_not_hide_the_pause(db):
    """AgentGoal.to_dict() lays config_json over the columns, so a config
    'status' would read as the goal's: the column is what decides."""
    with db_session() as s:
        g = AgentGoal(goal_type='marketing', title='Grow the network',
                      description='', status='paused',
                      config_json={'status': 'active', 'pause_reason': _NEEDS_HELP})
        s.add(g)
        s.flush()
        gid = str(g.id)
    _led, co = _coordinator(gid)
    assert _tick(co) == []


def test_a_goal_that_cannot_be_read_does_not_stop_the_work(db):
    """An unreadable goal store is no evidence the goal stopped: the task
    runs as it did before this check existed."""
    gid = _goal('active')
    _led, co = _coordinator(gid)
    with patch('integrations.social.models.db_session',
               side_effect=RuntimeError('database is locked')):
        assert _tick(co) == [f'{gid}_task_0']
