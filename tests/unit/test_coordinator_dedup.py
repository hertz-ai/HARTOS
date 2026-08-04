"""TaskCoordinator.submit_goal dedup + ledger save lock-hygiene (#145).

THE flywheel-deadlock root: submit_goal minted a random goal_id every call, so
re-dispatching the SAME goal created a fresh parent+children each tick → the
shared coordinator ledger grew to 9166 tasks → save()'s full json.dump under
the ledger lock wedged both daemons (py-spy-confirmed 2026-06-12).

Fix verified here:
1. submit_goal(goal_id=X) twice → ONE task set, not two (dedup).
2. save() builds the snapshot under _lock but writes under _io_lock — the slow
   serialize never holds _lock (so another thread's add_task isn't wedged).
"""
from core.constants import latency_budget
import os
import sys
import threading
import time

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_ledger.core import SmartLedger, Task, TaskStatus, TaskType  # noqa: E402


class _MemBackend:
    """Minimal ledger backend; save() can be made artificially slow."""
    def __init__(self):
        self.data = {}
        self.save_delay = 0.0
        self.save_calls = 0

    def load(self, key):
        return self.data.get(key)

    def save(self, key, data):
        self.save_calls += 1
        if self.save_delay:
            time.sleep(self.save_delay)
        self.data[key] = data

    def exists(self, key):
        return key in self.data


def _ledger():
    return SmartLedger(agent_id='coord', session_id='s', backend=_MemBackend())


def _coordinator(ledger):
    from unittest.mock import MagicMock
    from integrations.distributed_agent.task_coordinator import (
        DistributedTaskCoordinator)
    return DistributedTaskCoordinator(
        ledger=ledger, task_lock=MagicMock(),
        verifier=MagicMock(), baseline=MagicMock())


class TestSubmitGoalDedup:
    def _coordinator(self, ledger):
        return _coordinator(ledger)

    def test_redispatch_same_goal_id_does_not_duplicate_tasks(self):
        led = _ledger()
        coord = self._coordinator(led)
        tasks = [{'description': 'a'}, {'description': 'b'}]
        gid = coord.submit_goal('obj', tasks, {}, goal_id='goal-X')
        n_after_first = len(led.tasks)
        gid2 = coord.submit_goal('obj', tasks, {}, goal_id='goal-X')
        n_after_second = len(led.tasks)
        assert gid == gid2 == 'goal-X'
        assert n_after_second == n_after_first, (
            f"re-dispatch duplicated tasks: {n_after_first} -> {n_after_second}")

    def test_distinct_goals_still_create_distinct_tasks(self):
        led = _ledger()
        coord = self._coordinator(led)
        coord.submit_goal('o1', [{'description': 'a'}], {}, goal_id='g1')
        n1 = len(led.tasks)
        coord.submit_goal('o2', [{'description': 'b'}], {}, goal_id='g2')
        assert len(led.tasks) > n1  # genuinely new goal adds tasks


class TestSaveLockHygiene:
    def test_slow_save_does_not_hold_the_task_lock(self):
        """A slow save() must hold _io_lock (write serialization) but NOT _lock
        — so another thread's READ (get_task / acquiring _lock) is not wedged
        behind the json.dump. This is the exact deadlock that hung the daemons:
        save held _lock across the write, blocking add_task/get_task on _lock.
        """
        led = _ledger()
        led.add_task(Task(task_id='t0', description='seed',
                          task_type=TaskType.AUTONOMOUS))
        led.backend.save_delay = 1.5  # simulate a huge-ledger json.dump
        slow = threading.Thread(target=led.save, daemon=True)
        slow.start()
        time.sleep(0.2)  # slow save now mid-write, holding _io_lock
        # A read only needs _lock — it must NOT wait for the json.dump.
        t0 = time.time()
        got = led.get_task('t0')
        elapsed = time.time() - t0
        slow.join(timeout=5)
        assert got is not None and got.task_id == 't0'
        assert elapsed < latency_budget('coordinator_dedup_s'), (
            f"get_task blocked {elapsed:.2f}s during a slow save — _lock is "
            f"still held across the json.dump (lock-hygiene regression)")


class TestSubmitGoalBatchSave:
    """submit_goal must persist the whole goal in ONE backend.save, not one
    per task added.

    The live root cause of the 5-min sluggishness (py-spy 2026-06-13): the
    coding_daemon was caught inside json.dump during submit_goal because every
    add_task/update_task_status re-serialized the entire 1.46 MB ledger. A goal
    with K children fired K+3 full json.dumps (parent add + IN_PROGRESS update +
    K child adds + the final save) — O(ledger_size * tasks) GIL-held writes that
    starved Nunba's UI/Flask/SSE threads. Batching collapses it to 1.
    """

    def test_submit_goal_serializes_once_not_per_task(self):
        led = _ledger()
        coord = _coordinator(led)
        children = [{'description': f't{i}'} for i in range(5)]
        before = led.backend.save_calls
        coord.submit_goal('obj', children, {}, goal_id='g-batch')
        saves = led.backend.save_calls - before
        assert saves == 1, (
            f"submit_goal did {saves} full-ledger serializes for a 5-task goal "
            f"(unbatched would be K+3=8); the json.dump amplification is back")

    def test_submit_goal_still_persists_every_task(self):
        # Batching must not lose tasks: the single save at the end must contain
        # the parent + all children.
        led = _ledger()
        coord = _coordinator(led)
        children = [{'description': f't{i}'} for i in range(5)]
        coord.submit_goal('obj', children, {}, goal_id='g-keep')
        persisted = led.backend.data[led.ledger_key]['tasks']
        assert 'g-keep' in persisted  # parent
        kids = [t for t in led.tasks.values()
                if getattr(t, 'parent_task_id', None) == 'g-keep']
        assert len(kids) == 5  # every child added
        # and the goal parent is IN_PROGRESS (the deferred status update applied)
        assert led.tasks['g-keep'].status == TaskStatus.IN_PROGRESS


class TestDeferSave:
    """defer_save is opt-in: default behaviour (always persist) is unchanged so
    no existing caller regresses; defer_save=True mutates in-memory but leaves
    the disk write to the caller's explicit save()."""

    def test_add_task_defer_skips_backend_save_but_mutates_memory(self):
        led = _ledger()
        before = led.backend.save_calls
        ok = led.add_task(
            Task(task_id='d1', description='x', task_type=TaskType.AUTONOMOUS),
            defer_save=True)
        assert ok is True
        assert led.backend.save_calls == before  # no disk write
        assert led.get_task('d1') is not None     # but present in memory
        led.save()
        assert led.backend.save_calls == before + 1  # caller persists once

    def test_add_task_default_still_saves(self):
        led = _ledger()
        before = led.backend.save_calls
        led.add_task(
            Task(task_id='d2', description='x', task_type=TaskType.AUTONOMOUS))
        assert led.backend.save_calls == before + 1  # unchanged contract

    def test_update_status_defer_skips_backend_save_but_mutates_memory(self):
        led = _ledger()
        led.add_task(
            Task(task_id='d3', description='x', task_type=TaskType.AUTONOMOUS))
        before = led.backend.save_calls
        ok = led.update_task_status(
            'd3', TaskStatus.IN_PROGRESS, defer_save=True)
        assert ok is True
        assert led.backend.save_calls == before  # no disk write
        assert led.tasks['d3'].status == TaskStatus.IN_PROGRESS  # mutated

    def test_update_status_default_still_saves(self):
        led = _ledger()
        led.add_task(
            Task(task_id='d4', description='x', task_type=TaskType.AUTONOMOUS))
        before = led.backend.save_calls
        led.update_task_status('d4', TaskStatus.IN_PROGRESS)
        assert led.backend.save_calls == before + 1  # unchanged contract
