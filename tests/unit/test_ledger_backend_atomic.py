"""#140: JSONBackend.save is atomic — a failed write preserves the prior file.

The agent-ledger lib is a standalone open-source package (it can't import
HARTOS's core.file_cache.atomic_json_write — different package boundary), so it
carries its OWN tmp + fsync + os.replace atomic write (backends.py:111). That
load-bearing persistence (every coordinator/agent ledger) had no test for the
FAILURE boundary: when the tmp write fails (e.g. ENOSPC disk-full), the EXISTING
ledger file must survive intact (os.replace never runs), save() returns False
(not raises), and no .tmp is left behind.

Behavioral: real JSONBackend, mock the json.dump boundary, assert observable
disk state. Distinct from tests/unit/test_atomic_json.py (that covers
core.file_cache, the HARTOS-side atomic writer — not this backend).
"""
import os
import sys
from unittest.mock import patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for _p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_ledger.backends import JSONBackend  # noqa: E402
from agent_ledger.backends import InMemoryBackend  # noqa: E402
from agent_ledger.core import SmartLedger, Task, TaskStatus, TaskType  # noqa: E402


class FailingSaveBackend(InMemoryBackend):
    def __init__(self):
        super().__init__()
        self.reject = False

    def save(self, key, data):
        if self.reject:
            return False
        return super().save(key, data)


def test_save_roundtrips(tmp_path):
    b = JSONBackend(str(tmp_path))
    assert b.save('led', {'tasks': {'t1': 1}}) is True
    assert b.load('led') == {'tasks': {'t1': 1}}


def test_failed_write_preserves_existing_file_and_returns_false(tmp_path):
    b = JSONBackend(str(tmp_path))
    assert b.save('led', {'v': 1}) is True  # seed the durable copy
    # tmp write fails mid-save (disk full) — original must survive untouched.
    with patch('json.dump', side_effect=OSError(28, 'No space left on device')):
        ok = b.save('led', {'v': 2})
    assert ok is False                       # failure reported, never raised
    assert b.load('led') == {'v': 1}         # atomic: os.replace never ran
    assert [f for f in os.listdir(str(tmp_path))
            if f.endswith('.tmp')] == []     # tmp cleaned up


def test_failed_write_on_fresh_key_leaves_no_partial_file(tmp_path):
    b = JSONBackend(str(tmp_path))
    with patch('json.dump', side_effect=OSError(28, 'No space left on device')):
        ok = b.save('fresh', {'v': 1})
    assert ok is False
    assert b.load('fresh') is None           # no partial/corrupt file surfaced
    assert [f for f in os.listdir(str(tmp_path))
            if f.endswith('.tmp')] == []


def test_corrupt_load_reports_the_exact_file(tmp_path, capsys):
    """A load failure must identify which of thousands of ledgers is bad."""
    b = JSONBackend(str(tmp_path))
    corrupt = tmp_path / 'broken.json'
    corrupt.write_text('{"unterminated":', encoding='utf-8')

    assert b.load('broken') is None
    message = capsys.readouterr().out
    assert '[JSONBackend] Load error for' in message
    assert str(corrupt) in message


def test_active_deferral_is_one_durable_edge_and_rolls_back_on_save_failure(
        tmp_path):
    backend = FailingSaveBackend()
    ledger = SmartLedger('agent', 'deferral', str(tmp_path), backend=backend)
    task = Task('t1', 'work', TaskType.PRE_ASSIGNED)
    assert ledger.add_task(task)
    assert ledger.update_task_status('t1', TaskStatus.IN_PROGRESS)

    backend.reject = True
    history_before = list(task.state_history)
    assert ledger.defer_task('t1', 'temporary', until='2099-01-01') is False
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.state_history == history_before
    assert task.deferred_until is None

    backend.reject = False
    assert ledger.defer_task('t1', 'temporary', until='2099-01-01') is True
    assert task.status == TaskStatus.DEFERRED
    assert task.state_history[-1]['previous_status'] == 'in_progress'
    assert task.state_history[-1]['status'] == 'deferred'


def test_undefer_rolls_back_when_pending_state_cannot_be_saved(tmp_path):
    backend = FailingSaveBackend()
    ledger = SmartLedger('agent', 'undefer', str(tmp_path), backend=backend)
    task = Task('t1', 'work', TaskType.PRE_ASSIGNED)
    assert ledger.add_task(task)
    assert ledger.defer_task('t1', 'later', until='2000-01-01')

    backend.reject = True
    history_before = list(task.state_history)
    assert ledger.undefer_task('t1', 'due') is False
    assert task.status == TaskStatus.DEFERRED
    assert task.state_history == history_before


def test_completion_does_not_publish_or_survive_when_save_fails(tmp_path):
    backend = FailingSaveBackend()
    ledger = SmartLedger('agent', 'completion', str(tmp_path), backend=backend)
    task = Task('t1', 'work', TaskType.PRE_ASSIGNED)
    assert ledger.add_task(task)
    assert ledger.update_task_status('t1', TaskStatus.IN_PROGRESS)

    backend.reject = True
    assert ledger.update_task_status(
        't1', TaskStatus.COMPLETED, result='claimed result') is False

    assert task.status == TaskStatus.IN_PROGRESS
    assert task.result is None
    assert not any(event.get('type') == 'task_completed'
                   for event in ledger.events)
    durable = backend.load(ledger.ledger_key)
    assert durable['tasks']['t1']['status'] == TaskStatus.IN_PROGRESS.value


def test_complete_task_uses_the_same_atomic_completion_path(tmp_path):
    backend = FailingSaveBackend()
    ledger = SmartLedger('agent', 'complete_method', str(tmp_path),
                         backend=backend)
    task = Task('t1', 'work', TaskType.PRE_ASSIGNED)
    assert ledger.add_task(task)
    assert ledger.update_task_status('t1', TaskStatus.IN_PROGRESS)

    backend.reject = True
    assert ledger.complete_task('t1', result={'receipt': 'r1'}) is False
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.result is None
    assert 'result_hash' not in task.context
    assert not any(event.get('type') == 'task_completed'
                   for event in ledger.events)


def test_complete_delegation_rolls_back_result_when_save_fails(tmp_path):
    backend = FailingSaveBackend()
    ledger = SmartLedger('agent', 'delegation_method', str(tmp_path),
                         backend=backend)
    task = Task('t1', 'work', TaskType.PRE_ASSIGNED)
    assert ledger.add_task(task)
    assert ledger.update_task_status('t1', TaskStatus.IN_PROGRESS)
    assert ledger.delegate_task('t1', 'worker')

    backend.reject = True
    assert ledger.complete_delegation('t1', result='claimed') is False
    assert task.status == TaskStatus.DELEGATED
    assert task.result is None
    assert task.delegation_result is None
    assert not any(event.get('type') == 'task_completed'
                   for event in ledger.events)


def test_completion_rollback_snapshot_does_not_copy_unrelated_tasks(tmp_path):
    class MustNotBeCopied:
        def __deepcopy__(self, memo):
            raise AssertionError('unrelated task was copied')

    backend = FailingSaveBackend()
    ledger = SmartLedger('agent', 'bounded_completion_snapshot', str(tmp_path),
                         backend=backend)
    source = Task('source', 'source work', TaskType.PRE_ASSIGNED)
    unrelated = Task('unrelated', 'other work', TaskType.PRE_ASSIGNED)
    assert ledger.add_task(source)
    assert ledger.add_task(unrelated)
    assert ledger.update_task_status('source', TaskStatus.IN_PROGRESS)
    unrelated.context['copy_guard'] = MustNotBeCopied()

    assert ledger.complete_task('source', result='done') is True
    assert source.status == TaskStatus.COMPLETED


def test_batched_completion_is_refused_before_mutating_or_publishing(tmp_path):
    ledger = SmartLedger('agent', 'no_batched_completion', str(tmp_path),
                         backend=FailingSaveBackend())
    task = Task('t1', 'work', TaskType.PRE_ASSIGNED)
    assert ledger.add_task(task)
    assert ledger.update_task_status('t1', TaskStatus.IN_PROGRESS)
    before = task.to_dict()

    assert ledger.update_task_status(
        't1', TaskStatus.COMPLETED, result='not durable',
        defer_save=True) is False
    assert task.to_dict() == before
    assert not any(event.get('type') == 'task_completed'
                   for event in ledger.events)
