"""Every coordinator write is one durable transaction: the ledger holds the new
state or the old one, never a mix, and no cross-host lock outlives a claim the
ledger does not hold.

Handoff 2026-09-20, item 1.  The coordinator batches its mutations with
defer_save=True and commits with ONE save(); until now four sites never read
that save's verdict -- orphan recovery, the single claim (which also saved
twice per claim), the parallel claim and hold_task -- so a rejected write
(ENOSPC, a read-only install dir, a backend outage) left the in-memory ledger
ahead of the durable one while the worker's lock was held and, on Redis, its
heartbeat kept renewing it.

Behavioural: the real SmartLedger on the real InMemoryBackend with save()
refused at the backend boundary (the seam JSONBackend reports ENOSPC through),
the real InMemoryTaskLock and the real coordinator.  Each test asserts the
task object, the durable copy and the lock, then lifts the refusal and proves
the same call succeeds.  submit_result's rejection is already pinned by
test_distributed_bridge.py; the ledger's own defer/undefer/completion rollback
by test_ledger_backend_atomic.py.

    python -m pytest tests/unit/test_coordinator_persistence_transactions.py -q
"""
import copy
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for _p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_ledger.backends import InMemoryBackend  # noqa: E402
from agent_ledger.core import (  # noqa: E402
    ExecutionMode, SmartLedger, Task, TaskStatus, TaskType)
from integrations.distributed_agent import task_coordinator as tc  # noqa: E402
from integrations.distributed_agent.coordinator_backends import (  # noqa: E402
    InMemoryTaskLock)

CHILD = 'g_task_0'


class _ReloadFaithfulBackend(InMemoryBackend):
    """InMemoryBackend keeps a SHALLOW copy of the saved dict, so its record
    aliases each live task's context dict and lists and follows in-memory
    edits the save never accepted.  A JSON file on disk would not.  Store
    what a reload would see, so ``_durable`` reads the durable copy."""

    def save(self, key, data):
        return super().save(key, copy.deepcopy(data))


@pytest.fixture
def coord(tmp_path):
    led = SmartLedger('coord', 'txn', str(tmp_path),
                      backend=_ReloadFaithfulBackend())
    co = tc.DistributedTaskCoordinator(
        ledger=led, task_lock=InMemoryTaskLock(),
        verifier=MagicMock(), baseline=MagicMock())
    return led, co


def _refuse_saves(led):
    return patch.object(led.backend, 'save', return_value=False)


def _durable(led, task_id):
    data = led.backend.load(led.ledger_key) or {}
    return (data.get('tasks') or {}).get(task_id)


def _submit(co, goal_id='g', children=(CHILD,), capabilities=(), **context):
    return co.submit_goal(
        'obj',
        [{'task_id': c, 'description': 'd', 'capabilities': list(capabilities)}
         for c in children],
        dict(context), goal_id=goal_id)


def _backdate_claim(task):
    task.context['claimed_at'] = (
        datetime.now() - timedelta(seconds=tc._ORPHAN_AFTER_S + 60)).isoformat()


# ── submission ───────────────────────────────────────────────────────────────

def test_submission_leaves_nothing_behind_when_the_save_is_refused(coord):
    led, co = coord
    with _refuse_saves(led), pytest.raises(RuntimeError, match='persist'):
        _submit(co, children=('a', 'b'))
    assert led.tasks == {} and led.task_order == []
    assert _durable(led, 'g') is None
    co._baseline.create_snapshot.assert_not_called()

    assert _submit(co, children=('a', 'b')) == 'g'
    assert set(led.tasks) == {'g', 'a', 'b'}
    assert _durable(led, 'a')['status'] == 'pending'
    co._baseline.create_snapshot.assert_called_once()


def test_a_child_id_that_collides_with_an_existing_task_refuses_the_goal_and_keeps_that_task(coord):
    led, co = coord
    keep = Task('keep', 'unrelated work', TaskType.PRE_ASSIGNED)
    keep.context['marker'] = 'mine'
    assert led.add_task(keep)

    with pytest.raises(RuntimeError, match='Could not stage'):
        _submit(co, children=('a', 'keep'))
    assert set(led.tasks) == {'keep'} and led.task_order == ['keep']
    assert led.tasks['keep'] is keep and keep.context['marker'] == 'mine'
    assert keep.status == TaskStatus.PENDING
    assert _durable(led, 'a') is None and _durable(led, 'g') is None


def test_a_duplicate_child_id_inside_one_goal_is_refused_whole(coord):
    led, co = coord
    with pytest.raises(RuntimeError, match='Could not stage'):
        _submit(co, children=('a', 'a'))
    assert led.tasks == {} and led.task_order == []


# ── re-dispatch repairs: heal, reopen, release ───────────────────────────────

def test_healing_is_undone_when_the_save_is_refused(coord):
    led, co = coord
    _submit(co, capabilities=('hive_growth',), goal_type='hive_growth')
    child = led.get_task(CHILD)
    assert child.context['capabilities_required'] == ['hive_growth']

    with _refuse_saves(led):
        _submit(co, capabilities=('hive_growth',), goal_type='hive_growth')
    assert child.context['capabilities_required'] == ['hive_growth']
    assert _durable(led, CHILD)['context']['capabilities_required'] == ['hive_growth']

    _submit(co, capabilities=('hive_growth',), goal_type='hive_growth')
    assert child.context['capabilities_required'] == []
    assert _durable(led, CHILD)['context']['capabilities_required'] == []


def test_a_continuous_reopen_is_undone_when_the_save_is_refused(coord):
    led, co = coord
    _submit(co, continuous=True)
    assert co.claim_next_task('w').task_id == CHILD
    co.submit_result(CHILD, 'w', 'done')
    child = led.get_task(CHILD)
    assert child.status == TaskStatus.COMPLETED
    receipt = child.context['result_hash']

    with _refuse_saves(led):
        _submit(co, continuous=True)
    assert child.status == TaskStatus.COMPLETED
    assert child.result == 'done' and child.context['result_hash'] == receipt
    assert 'runs' not in child.context
    assert _durable(led, CHILD)['status'] == 'completed'

    _submit(co, continuous=True)
    assert child.status == TaskStatus.PENDING
    assert child.context['last_result_hash'] == receipt
    assert child.context['runs'] == 1
    assert _durable(led, CHILD)['status'] == 'pending'


def test_a_held_task_release_is_undone_when_the_save_is_refused(coord):
    led, co = coord
    _submit(co)
    co.claim_next_task('w')
    assert co.hold_task(CHILD, 'w', 'Paused for help')
    child = led.get_task(CHILD)

    with _refuse_saves(led):
        _submit(co)
    assert child.status == TaskStatus.BLOCKED
    assert child.blocked_reason == 'input_required'
    assert child.error_message == 'Paused for help'
    assert 'runs' not in child.context
    assert _durable(led, CHILD)['status'] == 'blocked'

    _submit(co)
    assert child.status == TaskStatus.PENDING
    assert child.blocked_reason is None and child.context['runs'] == 1
    assert _durable(led, CHILD)['status'] == 'pending'


# ── orphan recovery ──────────────────────────────────────────────────────────

def _orphan(coord):
    led, co = coord
    _submit(co)
    child = co.claim_next_task('dead')
    co._lock.release_task(CHILD, 'dead')   # the worker died: lock gone
    _backdate_claim(child)
    led.save()
    return led, co, child


def test_orphan_recovery_is_undone_when_the_save_is_refused(coord):
    led, co, child = _orphan(coord)
    with _refuse_saves(led):
        assert co.claim_next_task('w2') is None
    assert child.status == TaskStatus.IN_PROGRESS
    assert child.context['claimed_by'] == 'dead'
    assert child.blocked_reason is None and child.error_message is None
    assert not co._lock.is_task_locked(CHILD)
    assert _durable(led, CHILD)['status'] == 'in_progress'

    assert co.claim_next_task('w2').task_id == CHILD
    assert child.context['claimed_by'] == 'w2'
    assert [h['status'] for h in child.state_history[-3:]] == [
        'blocked', 'pending', 'in_progress']
    assert _durable(led, CHILD)['context']['claimed_by'] == 'w2'


def test_orphan_recovery_is_undone_when_its_second_transition_is_refused(coord):
    """The BLOCKED hop must not survive in memory when PENDING is refused."""
    led, co, child = _orphan(coord)
    real = led.update_task_status

    def refuse_pending(task_id, status, *args, **kwargs):
        if (status == TaskStatus.PENDING
                and 'orphan recovery' in (kwargs.get('reason') or '')):
            return False
        return real(task_id, status, *args, **kwargs)

    with patch.object(led, 'update_task_status', side_effect=refuse_pending), \
            patch.object(led.backend, 'save', wraps=led.backend.save) as saves:
        assert co.claim_next_task('w2') is None
    assert child.status == TaskStatus.IN_PROGRESS
    assert child.blocked_reason is None and child.error_message is None
    assert child.context['claimed_by'] == 'dead'
    assert child.state_history[-1]['status'] == 'in_progress'
    assert saves.call_count == 0, 'nothing to persist, nothing was written'
    assert not co._lock.is_task_locked(CHILD)


# ── the single claim ─────────────────────────────────────────────────────────

def test_a_claim_the_ledger_cannot_record_releases_the_lock(coord):
    led, co = coord
    _submit(co)
    child = led.get_task(CHILD)
    unrelated = Task('unrelated', 'other', TaskType.PRE_ASSIGNED)
    assert led.add_task(unrelated)

    with _refuse_saves(led), \
            patch.object(led.backend, 'save', wraps=led.backend.save) as saves, \
            patch.object(co, '_snapshot_tasks', wraps=co._snapshot_tasks) as snaps:
        assert co.claim_next_task('w') is None
    assert saves.call_count == 1, \
        'persistence is ledger-wide: one refused write ends the tick'
    assert {t.task_id for call in snaps.call_args_list for t in call.args[0]} \
        == {CHILD}, 'only the task being claimed is snapshotted'
    assert child.status == TaskStatus.PENDING
    assert 'claimed_by' not in child.context and child.started_at is None
    assert child.state_history[-1]['status'] == 'pending'
    assert not co._lock.is_task_locked(CHILD)
    assert not co._lock.is_task_locked('unrelated'), \
        'a task the tick never claimed was locked'
    assert _durable(led, CHILD)['status'] == 'pending'

    assert co.claim_next_task('w').task_id == CHILD
    assert co._lock.get_task_owner(CHILD) == 'w'
    assert _durable(led, CHILD)['status'] == 'in_progress'
    assert _durable(led, CHILD)['context']['claimed_by'] == 'w'


def test_a_claim_is_one_durable_write(coord):
    led, co = coord
    _submit(co)
    with patch.object(led.backend, 'save', wraps=led.backend.save) as saves:
        assert co.claim_next_task('w').task_id == CHILD
    assert saves.call_count == 1, 'a claim used to cost two full-ledger writes'


def test_a_claim_the_transition_refuses_releases_its_lock_and_moves_on(coord):
    led, co = coord
    _submit(co, children=('first', 'second'))
    first = led.get_task('first')
    first._validate_transition = lambda status: False   # the ledger says no

    got = co.claim_next_task('w')
    assert got is not None and got.task_id == 'second'
    assert first.status == TaskStatus.PENDING and 'claimed_by' not in first.context
    assert not co._lock.is_task_locked('first')
    assert co._lock.get_task_owner('second') == 'w'


# ── the parallel batch ───────────────────────────────────────────────────────

def _parallel(led, *ids):
    tasks = []
    for tid in ids:
        task = Task(tid, 'p', TaskType.AUTONOMOUS,
                    execution_mode=ExecutionMode.PARALLEL)
        assert led.add_task(task)
        tasks.append(task)
    return tasks


def test_a_parallel_batch_the_ledger_cannot_record_releases_every_lock(coord):
    led, co = coord
    a, b = _parallel(led, 'pa', 'pb')
    with _refuse_saves(led):
        assert co.claim_parallel_batch('w', max_tasks=4) == []
    for task in (a, b):
        assert task.status == TaskStatus.PENDING
        assert 'claimed_by' not in task.context
        assert not co._lock.is_task_locked(task.task_id)
        assert _durable(led, task.task_id)['status'] == 'pending'

    with patch.object(led.backend, 'save', wraps=led.backend.save) as saves:
        claimed = co.claim_parallel_batch('w', max_tasks=4)
    assert {t.task_id for t in claimed} == {'pa', 'pb'}
    assert saves.call_count == 1
    assert _durable(led, 'pb')['context']['claimed_by'] == 'w'


def test_a_parallel_batch_is_refused_whole_when_one_task_cannot_start(coord):
    """PENDING -> IN_PROGRESS is always valid, so a refusal means the task
    changed under the batch; the batch is all-or-nothing and writes nothing."""
    led, co = coord
    a, b = _parallel(led, 'pa', 'pb')
    b._validate_transition = lambda status: False   # refused after 'pa' staged

    with patch.object(led.backend, 'save', wraps=led.backend.save) as saves:
        assert co.claim_parallel_batch('w', max_tasks=4) == []
    assert saves.call_count == 0
    for task in (a, b):
        assert task.status == TaskStatus.PENDING
        assert 'claimed_by' not in task.context and task.started_at is None
        assert not co._lock.is_task_locked(task.task_id)


# ── hold and defer ───────────────────────────────────────────────────────────

def test_a_hold_the_ledger_cannot_record_keeps_the_claim_stamp_and_releases_the_lock(coord):
    led, co = coord
    _submit(co)
    child = co.claim_next_task('w')
    stamp = child.context['claimed_at']

    with _refuse_saves(led):
        assert co.hold_task(CHILD, 'w', 'Paused for help') is False
    assert child.status == TaskStatus.IN_PROGRESS
    assert child.blocked_reason is None and child.error_message is None
    assert child.context['claimed_by'] == 'w'
    assert child.context['claimed_at'] == stamp, \
        'orphan recovery paces the retry from this stamp'
    assert not co._lock.is_task_locked(CHILD)
    assert _durable(led, CHILD)['status'] == 'in_progress'

    assert co.hold_task(CHILD, 'w', 'Paused for help') is True
    assert child.status == TaskStatus.BLOCKED
    assert child.blocked_reason == 'input_required'
    assert _durable(led, CHILD)['status'] == 'blocked'


def test_a_hold_of_an_unknown_task_is_false_and_writes_nothing(coord):
    led, co = coord
    with patch.object(led.backend, 'save', wraps=led.backend.save) as saves:
        assert co.hold_task('ghost', 'w', 'x') is False
    assert saves.call_count == 0


def test_a_deferral_the_ledger_cannot_record_keeps_the_worker_attribution(coord):
    led, co = coord
    _submit(co)
    child = co.claim_next_task('w')
    stamp = child.context['claimed_at']

    with _refuse_saves(led):
        assert co.defer_task(CHILD, 'w', 'llm busy') is False
    assert child.status == TaskStatus.IN_PROGRESS
    assert child.context['claimed_by'] == 'w'
    assert child.context['claimed_at'] == stamp
    assert child.deferred_until is None and child.deferred_reason is None
    assert not co._lock.is_task_locked(CHILD)
    assert _durable(led, CHILD)['status'] == 'in_progress'

    assert co.defer_task(CHILD, 'w', 'llm busy') is True
    assert child.status == TaskStatus.DEFERRED
    assert 'claimed_by' not in child.context and 'claimed_at' not in child.context
    assert child.deferred_reason == 'temporary worker deferral: llm busy'
    assert child.blocked_reason is None, \
        'DEFERRED has its own reason; a BlockedReason would outlive the deferral'
    assert _durable(led, CHILD)['status'] == 'deferred'

    child.deferred_until = '2000-01-01T00:00:00'
    assert co.claim_next_task('w2').task_id == CHILD
    assert child.status == TaskStatus.IN_PROGRESS and child.blocked_reason is None
    assert _durable(led, CHILD)['context']['claimed_by'] == 'w2'


# ── the HTTP routes: the one caller that did not guard the raises ────────────

@pytest.fixture
def client(coord):
    """/api/distributed/* behind an identity require_auth, the real
    coordinator from ``coord`` behind _get_coordinator.

    @require_auth is applied at import, so the SOURCE decorator is replaced
    and the module reloaded (the pattern of test_api_agent_engine_ledger),
    then both are restored so later suites see the real gate.  The
    blueprint's before_request probes Redis; a dead client short-circuits
    it the way test_delegation_subscriber_cooldown drives that path.
    """
    import importlib
    from flask import Flask, g
    import integrations.social.auth as auth_mod
    import integrations.distributed_agent.api as dapi

    real_require_auth = auth_mod.require_auth
    auth_mod.require_auth = lambda f: f
    importlib.reload(dapi)
    try:
        led, co = coord
        app = Flask(__name__)
        app.config['TESTING'] = True
        app.register_blueprint(dapi.distributed_agent_bp)

        @app.before_request
        def _stub_user():
            class _User:
                id = 'agent-1'
                is_admin = True
            g.user = _User()

        def _dead_redis():
            raise ConnectionError('redis down')

        with patch.object(dapi, '_get_coordinator', return_value=co), \
                patch.object(dapi, '_get_redis_client', _dead_redis):
            yield app.test_client(), led, co
    finally:
        auth_mod.require_auth = real_require_auth
        importlib.reload(dapi)


def _goal_body(*children, **context):
    return {'objective': 'o', 'context': context,
            'tasks': [{'task_id': c, 'description': 'd'} for c in children]}


def test_the_goals_route_still_submits_a_clean_goal(client):
    c, led, co = client
    with patch('integrations.distributed_agent.coordinator_backends.'
               'GossipTaskBridge.announce_goal', return_value=0):
        resp = c.post('/api/distributed/goals', json=_goal_body('a'))
    assert resp.status_code == 200 and resp.get_json()['success'] is True
    assert led.get_task('a').status == TaskStatus.PENDING


def test_the_goals_route_answers_409_for_a_child_id_the_ledger_already_holds(client):
    c, led, co = client
    keep = Task('keep', 'x', TaskType.PRE_ASSIGNED)
    assert led.add_task(keep)
    resp = c.post('/api/distributed/goals', json=_goal_body('a', 'keep'))
    assert resp.status_code == 409
    body = resp.get_json()
    assert body['success'] is False and 'Could not stage' in body['error']
    assert set(led.tasks) == {'keep'} and led.tasks['keep'] is keep


def test_the_goals_route_answers_503_when_the_ledger_cannot_persist(client):
    c, led, co = client
    with _refuse_saves(led):
        resp = c.post('/api/distributed/goals', json=_goal_body('a'))
    assert resp.status_code == 503
    assert 'persist' in resp.get_json()['error']
    assert led.tasks == {}


def test_the_goals_route_answers_400_for_a_hop_past_the_topology(client):
    from core.constants import HIVE_DEPTH
    c, led, co = client
    resp = c.post('/api/distributed/goals', json=_goal_body('a', hop=HIVE_DEPTH))
    assert resp.status_code == 400
    assert 'HIVE_DEPTH' in resp.get_json()['error']
    assert led.tasks == {}


def test_the_submit_route_answers_503_when_the_completion_cannot_persist(client):
    c, led, co = client
    _submit(co)
    child = co.claim_next_task('agent-1')
    with _refuse_saves(led):
        resp = c.post(f'/api/distributed/tasks/{CHILD}/submit', json={'result': 'r'})
    assert resp.status_code == 503
    assert resp.get_json()['success'] is False
    assert child.status == TaskStatus.IN_PROGRESS and child.result is None
    assert not co._lock.is_task_locked(CHILD), 'the claim must be released for retry'
    assert _durable(led, CHILD)['status'] == 'in_progress'

    resp = c.post(f'/api/distributed/tasks/{CHILD}/submit', json={'result': 'r'})
    assert resp.status_code == 200 and resp.get_json()['status'] == 'completed'
    assert child.status == TaskStatus.COMPLETED
