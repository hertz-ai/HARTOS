"""The zombie reaper opens only the ledgers that hold a task in progress.

Measured 2026-10-20 on the desktop: the reaper's first tick after boot
built every ledger in agent_data (2,489 ledgers, 14,294 tasks, 86 s) and
found no task in progress.  Only an IN_PROGRESS task can be a zombie, so a
tick now opens the ledgers whose saved state holds one (the marks
SmartLedger.save keeps) plus any ledger this process holds open with one in
memory, and reaps exactly what the whole-folder walk reaped.

These tests run the real reap_once, _iter_ledgers, SmartLedger and
JSONBackend on a temporary agent_data folder.  The audit log is the one
stand-in, so nothing reaches the real one.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
AGENT_LEDGER_SRC = os.path.join(PROJECT_ROOT, 'agent-ledger-opensource')
if AGENT_LEDGER_SRC not in sys.path:
    sys.path.insert(0, AGENT_LEDGER_SRC)

from tests.unit.module_swap import swap_modules  # noqa: E402

AGENT = '6c2dc0fc-7c93-4fe0-973e-f7466ff63f29'
IDLE_LEDGERS = 30


@pytest.fixture
def agent_data(tmp_path, monkeypatch):
    """A temporary agent_data folder, an empty ledger cache, and a record
    of which ledgers the walk opened."""
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    from core import platform_paths
    platform_paths.reset_cache()
    data_dir = tmp_path / 'data' / 'agent_data'
    data_dir.mkdir(parents=True, exist_ok=True)
    from core.platform_paths import get_agent_data_dir
    assert os.path.samefile(get_agent_data_dir(), data_dir)

    import agent_ledger.factory as factory
    factory.clear_ledger_cache()
    opened = []
    real_get_or_create = factory.get_or_create_ledger

    def recording_get_or_create(agent_id, session_id, *args, **kwargs):
        opened.append(session_id)
        return real_get_or_create(agent_id, session_id, *args, **kwargs)

    monkeypatch.setattr(factory, 'get_or_create_ledger', recording_get_or_create)
    fake_audit = SimpleNamespace(events=[])
    fake_audit.log_event = lambda **kw: fake_audit.events.append(kw)
    with swap_modules({'security.immutable_audit_log':
                       SimpleNamespace(get_audit_log=lambda: fake_audit)}):
        yield SimpleNamespace(dir=data_dir, opened=opened, audit=fake_audit)
    factory.clear_ledger_cache()
    platform_paths.reset_cache()


def _hours_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _save_ledger(data_dir, session, statuses, age_hours=3.0):
    """Save a real ledger with one task per status, last touched age_hours ago.
    Built directly, as hartos.helper_ledger builds the live ones: the factory
    cache does not hold it."""
    from agent_ledger.backends import JSONBackend
    from agent_ledger.core import SmartLedger, Task, TaskType
    ledger = SmartLedger(AGENT, session, ledger_dir=str(data_dir),
                         backend=JSONBackend(storage_dir=str(data_dir)))
    for i, status in enumerate(statuses, 1):
        task = Task(f'action_{i}', f'step {i}', TaskType.PRE_ASSIGNED)
        if status in ('in_progress', 'completed'):
            assert task.start()
        if status == 'completed':
            assert task.complete()
        task.updated_at = _hours_ago(age_hours)
        ledger.tasks[task.task_id] = task
    assert ledger.save() is True
    return ledger


def _saved_status(data_dir, session, task_id):
    path = data_dir / f'ledger_{AGENT}_{session}.json'
    return json.loads(path.read_text(encoding='utf-8'))['tasks'][task_id]['status']


def _save_idle_ledgers(data_dir):
    for i in range(IDLE_LEDGERS):
        _save_ledger(data_dir, f'idle{i:02d}',
                     ['pending', 'completed'] if i % 2 else ['pending'])


def _drop_marks(data_dir):
    """Make the folder look as an older version left it: ledgers, no marks.
    The mark folder is flat; shutil.rmtree is avoided because it is broken in
    this venv (os._walk_symlinks_as_files, venv 3.12.3 on a 3.12 stdlib)."""
    marks = data_dir / '.in_progress'
    if marks.is_dir():
        for name in os.listdir(marks):
            os.remove(marks / name)
        os.rmdir(marks)


def _reap():
    from integrations.agent_engine.zombie_reaper import reap_once
    return reap_once()


def test_a_tick_opens_only_the_ledger_with_a_task_in_progress(agent_data):
    _save_idle_ledgers(agent_data.dir)
    _save_ledger(agent_data.dir, 'busy', ['in_progress', 'pending'])

    out = _reap()

    assert out['reaped'] == 1 and out['errors'] == 0, out
    assert agent_data.opened == ['busy'], (
        f'the tick opened {len(agent_data.opened)} ledgers; only the one with '
        f'a task in progress can hold a zombie')
    assert _saved_status(agent_data.dir, 'busy', 'action_1') == 'failed'
    assert _saved_status(agent_data.dir, 'busy', 'action_2') == 'pending'
    assert [e['target_id'] for e in agent_data.audit.events] == ['action_1']

    # The reap's own save cleared the mark: the next tick opens nothing.
    agent_data.opened.clear()
    out = _reap()
    assert out['reaped'] == 0 and agent_data.opened == []


def test_ledgers_saved_before_marks_existed_are_found(agent_data):
    """An upgrade meets ledgers that no save ever marked; the first tick
    indexes them from the raw files and reaps what the old walk reaped."""
    _save_idle_ledgers(agent_data.dir)
    _save_ledger(agent_data.dir, 'busy', ['in_progress'])
    _drop_marks(agent_data.dir)

    out = _reap()

    assert out['reaped'] == 1 and agent_data.opened == ['busy'], (out, agent_data.opened)
    assert _saved_status(agent_data.dir, 'busy', 'action_1') == 'failed'


def test_a_task_running_only_in_memory_is_still_reaped(agent_data):
    """A ledger this process holds open is examined as it stands in memory,
    as the whole-folder walk examined it (it was handed back from the
    cache), even when its running task was never saved."""
    from agent_ledger.factory import get_or_create_ledger
    _save_idle_ledgers(agent_data.dir)
    _save_ledger(agent_data.dir, 'open', ['pending'])
    ledger = get_or_create_ledger(AGENT, 'open', storage_dir=str(agent_data.dir),
                                  prefer_redis=False)
    task = ledger.tasks['action_1']
    assert task.start()
    task.updated_at = _hours_ago(3)
    agent_data.opened.clear()

    out = _reap()

    assert out['reaped'] == 1 and agent_data.opened == ['open'], (out, agent_data.opened)
    assert task.status.value == 'failed'
    assert _saved_status(agent_data.dir, 'open', 'action_1') == 'failed'


def test_a_young_running_task_is_left_alone(agent_data):
    _save_ledger(agent_data.dir, 'busy', ['in_progress'], age_hours=0.5)

    out = _reap()

    assert out['reaped'] == 0 and agent_data.opened == ['busy']
    assert _saved_status(agent_data.dir, 'busy', 'action_1') == 'in_progress'


def test_a_mark_whose_ledger_file_is_gone_is_dropped(agent_data):
    from agent_ledger.core import SmartLedger
    _save_ledger(agent_data.dir, 'busy', ['in_progress'])
    assert SmartLedger.keys_in_progress(str(agent_data.dir)) == [f'ledger_{AGENT}_busy']

    os.remove(agent_data.dir / f'ledger_{AGENT}_busy.json')

    assert SmartLedger.keys_in_progress(str(agent_data.dir)) == []
    assert not (agent_data.dir / '.in_progress' / f'ledger_{AGENT}_busy').exists()


def test_a_mark_that_cannot_be_written_does_not_fail_the_save(agent_data, monkeypatch):
    """Callers roll a status change back when save() fails, so the mark is
    never allowed to fail it."""
    from agent_ledger.backends import JSONBackend
    from agent_ledger.core import TaskStatus

    def refuse(self, key, on):
        raise PermissionError('mark folder is read-only')

    ledger = _save_ledger(agent_data.dir, 'busy', ['pending'])
    monkeypatch.setattr(JSONBackend, 'set_mark', refuse)

    assert ledger.update_task_status('action_1', TaskStatus.IN_PROGRESS) is True
    assert ledger.tasks['action_1'].status == TaskStatus.IN_PROGRESS
    assert _saved_status(agent_data.dir, 'busy', 'action_1') == 'in_progress'


def test_an_index_that_cannot_be_recorded_still_answers(agent_data, monkeypatch):
    """A folder the index cannot be written to still gets the scan's answer,
    and is scanned again next time rather than trusted half-built."""
    from agent_ledger.backends import JSONBackend
    from agent_ledger.core import SmartLedger
    _save_idle_ledgers(agent_data.dir)
    _save_ledger(agent_data.dir, 'busy', ['in_progress'])
    _drop_marks(agent_data.dir)

    def refuse(self):
        raise PermissionError('mark folder is read-only')

    monkeypatch.setattr(JSONBackend, 'complete_mark_index', refuse)

    assert SmartLedger.keys_in_progress(str(agent_data.dir)) == [f'ledger_{AGENT}_busy']
    assert not (agent_data.dir / '.in_progress' / '.complete').exists()
