"""A task whose worker died must become claimable again.

DEFECT, measured on central 2026-09-02: 6 tasks sat IN_PROGRESS against a
single-threaded worker that can hold at most one.  ``claim_next_task`` only
considers PENDING, so a task whose worker died was never re-claimed, its
goal never grounded, and (once the completion gate started requiring the
ledger) that goal sat in awaiting_verification until it auto-paused.

``DistributedTaskLock.reclaim_stale_tasks`` exists for this and is NOT the
fix: it deletes the Redis key and leaves the ledger untouched, so the task
stays unclaimable.  Its only caller is a test.

Two safety properties are pinned here, and both were violated by earlier
revisions of the recovery:
  1. a goal ROOT is IN_PROGRESS while its children run and nothing locks
     it — recovering on "unlocked" alone handed roots out as work units;
  2. absence of a lock is NOT evidence of death (a mocked or momentarily
     unreachable oracle reads identically), so recovery also requires the
     claim to be OLD.  Double execution is worse than a stall.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from integrations.distributed_agent import task_coordinator as tc  # noqa: E402


class _Task:
    def __init__(self, task_id, status, parent=None, children=None,
                 claimed_at=None, capabilities=None):
        self.task_id = task_id
        self.status = status
        self.parent_task_id = parent
        self.child_task_ids = children or []
        self.context = {'capabilities_required': capabilities or []}
        if claimed_at is not None:
            self.context['claimed_at'] = claimed_at
        self.description = 'd'


def _ledger(tasks):
    led = MagicMock()
    led.task_order = [t.task_id for t in tasks]
    by_id = {t.task_id: t for t in tasks}
    led.get_task.side_effect = lambda tid: by_id.get(tid)

    def _update(tid, status):
        by_id[tid].status = status
    led.update_task_status.side_effect = _update
    return led


def _coordinator(tasks, locked=False):
    co = tc.DistributedTaskCoordinator.__new__(tc.DistributedTaskCoordinator)
    co._ledger = _ledger(tasks)
    co._lock = MagicMock()
    co._lock.is_task_locked.return_value = locked
    co._lock.try_claim_task.return_value = True
    co._verifier = MagicMock()
    co._baseline = MagicMock()
    return co


def _stamp(seconds_ago):
    return (datetime.now() - timedelta(seconds=seconds_ago)).isoformat()


def test_a_stale_unlocked_task_is_recovered_and_claimable():
    """The 6 stuck tasks on central: claimed long ago, lock long gone."""
    t = _Task('g1_task_0', tc.TaskStatus.IN_PROGRESS, parent='g1_root',
              claimed_at=_stamp(tc._ORPHAN_AFTER_S + 60))
    co = _coordinator([t], locked=False)
    got = co.claim_next_task('worker_2')
    assert got is not None and got.task_id == 'g1_task_0'


def test_a_freshly_claimed_task_is_never_stolen():
    """test_no_double_claim's invariant: a live worker keeps its task even
    when the lock oracle says unlocked."""
    t = _Task('g1_task_0', tc.TaskStatus.IN_PROGRESS, parent='g1_root',
              claimed_at=_stamp(5))
    co = _coordinator([t], locked=False)
    assert co.claim_next_task('worker_2') is None


def test_a_still_locked_task_is_never_stolen():
    t = _Task('g1_task_0', tc.TaskStatus.IN_PROGRESS, parent='g1_root',
              claimed_at=_stamp(tc._ORPHAN_AFTER_S + 60))
    co = _coordinator([t], locked=True)
    assert co.claim_next_task('worker_2') is None


def test_a_task_with_no_claim_stamp_is_left_alone():
    """Cannot tell how old it is -> leave it, never guess in favour of
    stealing."""
    t = _Task('g1_task_0', tc.TaskStatus.IN_PROGRESS, parent='g1_root')
    co = _coordinator([t], locked=False)
    assert co.claim_next_task('worker_2') is None


def test_an_unparseable_claim_stamp_is_left_alone():
    t = _Task('g1_task_0', tc.TaskStatus.IN_PROGRESS, parent='g1_root',
              claimed_at='not-a-timestamp')
    co = _coordinator([t], locked=False)
    assert co.claim_next_task('worker_2') is None


def test_a_goal_root_is_never_handed_out_as_work():
    """A root is IN_PROGRESS while its children run and nothing locks it.
    An earlier revision recovered it and returned it as a work unit."""
    root = _Task('g1_root', tc.TaskStatus.IN_PROGRESS, parent=None,
                 children=['g1_task_0'],
                 claimed_at=_stamp(tc._ORPHAN_AFTER_S + 60))
    co = _coordinator([root], locked=False)
    assert co.claim_next_task('worker_2') is None


def test_recovery_clears_the_dead_owner():
    t = _Task('g1_task_0', tc.TaskStatus.IN_PROGRESS, parent='g1_root',
              claimed_at=_stamp(tc._ORPHAN_AFTER_S + 60))
    t.context['claimed_by'] = 'dead_worker'
    co = _coordinator([t], locked=False)
    got = co.claim_next_task('worker_2')
    assert got is not None
    assert t.context.get('claimed_by') == 'worker_2', \
        'the dead owner must not survive the recovery'
