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
import os
import sys
import threading
import time

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_ledger.core import SmartLedger, Task, TaskType  # noqa: E402


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


class TestSubmitGoalDedup:
    def _coordinator(self, ledger):
        from unittest.mock import MagicMock
        from integrations.distributed_agent.task_coordinator import (
            DistributedTaskCoordinator)
        return DistributedTaskCoordinator(
            ledger=ledger, task_lock=MagicMock(),
            verifier=MagicMock(), baseline=MagicMock())

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
        assert elapsed < 0.5, (
            f"get_task blocked {elapsed:.2f}s during a slow save — _lock is "
            f"still held across the json.dump (lock-hygiene regression)")
