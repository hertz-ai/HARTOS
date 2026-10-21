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


def _fresh_process_view():
    """Forget what this process knows, as a restarted node would: the
    ledgers it holds open, and the folders it has read in full."""
    from agent_ledger import core
    from agent_ledger.factory import clear_ledger_cache
    clear_ledger_cache()
    getattr(core, '_FOLDERS_READ_IN_FULL', set()).clear()


def _build_index(data_dir):
    from agent_ledger.core import SmartLedger
    SmartLedger.keys_in_progress(str(data_dir))


def _raw_ledger_data(session, statuses):
    """The data a SmartLedger save writes, for a writer that sets no mark
    (an agent_ledger from before marks) to store with a plain backend save."""
    from agent_ledger.core import Task, TaskType
    tasks = {}
    for i, status in enumerate(statuses, 1):
        task = Task(f'action_{i}', f'step {i}', TaskType.PRE_ASSIGNED)
        if status == 'in_progress':
            assert task.start()
        task.updated_at = _hours_ago(3)
        tasks[task.task_id] = task.to_dict()
    return {'agent_id': AGENT, 'session_id': session, 'task_order': list(tasks),
            'tasks': tasks}


def test_a_writer_that_sets_no_mark_is_still_found(agent_data):
    """An agent_ledger from before marks (a rollback, a second process, a
    copy not yet swapped) saves a running task after the index exists: the
    tick still finds it, as the whole-folder walk did."""
    from agent_ledger.backends import JSONBackend
    _save_idle_ledgers(agent_data.dir)
    _build_index(agent_data.dir)
    JSONBackend(storage_dir=str(agent_data.dir)).save(
        f'ledger_{AGENT}_old', _raw_ledger_data('old', ['in_progress']))
    _fresh_process_view()

    out = _reap()

    assert out['reaped'] == 1 and agent_data.opened == ['old'], (out, agent_data.opened)
    assert _saved_status(agent_data.dir, 'old', 'action_1') == 'failed'


def test_a_mark_that_cannot_be_written_does_not_fail_the_save(agent_data, monkeypatch):
    """Callers roll a status change back when save() fails, so the mark is
    never allowed to fail it; and the running task it should have marked is
    still found once marks can be written again."""
    from agent_ledger.backends import JSONBackend
    from agent_ledger.core import TaskStatus
    _save_idle_ledgers(agent_data.dir)
    ledger = _save_ledger(agent_data.dir, 'busy', ['pending'])
    _build_index(agent_data.dir)
    real_set_mark = JSONBackend.set_mark

    def refuse(self, key, on):
        raise PermissionError('mark folder is read-only')

    monkeypatch.setattr(JSONBackend, 'set_mark', refuse)
    assert ledger.update_task_status('action_1', TaskStatus.IN_PROGRESS) is True
    assert ledger.tasks['action_1'].status == TaskStatus.IN_PROGRESS
    assert _saved_status(agent_data.dir, 'busy', 'action_1') == 'in_progress'
    ledger.tasks['action_1'].updated_at = _hours_ago(3)
    assert ledger.save() is True
    monkeypatch.setattr(JSONBackend, 'set_mark', real_set_mark)
    _fresh_process_view()

    out = _reap()

    assert out['reaped'] == 1 and agent_data.opened == ['busy'], (out, agent_data.opened)


def test_two_instances_of_one_ledger_cannot_hide_a_running_task(agent_data):
    """A sets the mark, B writes, A writes its running task, B clears the
    mark (two instances of one key, each with its own lock).  The file holds
    the running task and no mark; the tick still finds it."""
    import threading
    from agent_ledger.backends import JSONBackend
    from agent_ledger.core import SmartLedger
    _save_ledger(agent_data.dir, 's', ['pending', 'pending'])
    _build_index(agent_data.dir)

    def instance():
        return SmartLedger(AGENT, 's', ledger_dir=str(agent_data.dir),
                           backend=JSONBackend(storage_dir=str(agent_data.dir)))

    a, b = instance(), instance()
    assert a.tasks['action_1'].start()
    a.tasks['action_1'].updated_at = _hours_ago(3)
    assert b.tasks['action_2'].start() and b.tasks['action_2'].complete()
    b_wrote, a_wrote = threading.Event(), threading.Event()
    a_save, b_save = a.backend.save, b.backend.save

    def a_writes_after_b(key, data):
        assert b_wrote.wait(5)
        ok = a_save(key, data)
        a_wrote.set()
        return ok

    def b_clears_after_a(key, data):
        ok = b_save(key, data)
        b_wrote.set()
        assert a_wrote.wait(5)
        return ok

    a.backend.save, b.backend.save = a_writes_after_b, b_clears_after_a
    ta, tb = threading.Thread(target=a.save), threading.Thread(target=b.save)
    ta.start()
    tb.start()
    ta.join(10)
    tb.join(10)
    assert _saved_status(agent_data.dir, 's', 'action_1') == 'in_progress'
    assert not (agent_data.dir / '.in_progress' / f'ledger_{AGENT}_s').exists()
    _fresh_process_view()

    out = _reap()

    assert out['reaped'] == 1 and agent_data.opened == ['s'], (out, agent_data.opened)


def test_an_agent_ledger_without_the_index_gets_the_whole_walk(agent_data, monkeypatch):
    """A partial hot-swap can leave HARTOS on an agent_ledger that predates
    keys_in_progress: the tick walks every ledger, as before, instead of
    aborting."""
    from agent_ledger.core import SmartLedger
    _save_idle_ledgers(agent_data.dir)
    _save_ledger(agent_data.dir, 'busy', ['in_progress'])
    monkeypatch.delattr(SmartLedger, 'keys_in_progress')

    out = _reap()

    assert out['reaped'] == 1 and out['errors'] == 0, out
    assert len(agent_data.opened) == IDLE_LEDGERS + 1


def test_a_rejected_write_leaves_the_mark(agent_data, monkeypatch):
    """The mark goes on before the write, so a write that does not land
    leaves an extra mark (one ledger opened for nothing), never a missing one."""
    ledger = _save_ledger(agent_data.dir, 'busy', ['pending'])
    assert ledger.tasks['action_1'].start()
    monkeypatch.setattr(ledger.backend, 'save', lambda key, data: False)

    assert ledger.save() is False
    assert (agent_data.dir / '.in_progress' / f'ledger_{AGENT}_busy').exists()


def test_deleting_a_ledger_clears_its_mark(agent_data):
    from agent_ledger.backends import JSONBackend
    _save_ledger(agent_data.dir, 'busy', ['in_progress'])
    mark = agent_data.dir / '.in_progress' / f'ledger_{AGENT}_busy'
    assert mark.exists()

    assert JSONBackend(storage_dir=str(agent_data.dir)).delete(f'ledger_{AGENT}_busy') is True
    assert not mark.exists()


def test_a_tick_reads_only_the_files_written_since_the_last_pass(agent_data, monkeypatch):
    """After the first pass, a tick lists the folder and reads only files
    written since; an unchanged folder is not read at all."""
    import time
    from agent_ledger import core
    monkeypatch.setattr(core, '_INDEX_TIME_SLACK_S', 0.0)
    _save_idle_ledgers(agent_data.dir)
    time.sleep(0.05)
    _build_index(agent_data.dir)
    read = []
    real_read = core._saved_work_in_progress
    monkeypatch.setattr(core, '_saved_work_in_progress',
                        lambda path: (read.append(os.path.basename(path)), real_read(path))[1])

    _build_index(agent_data.dir)
    assert read == []

    time.sleep(0.05)
    _save_ledger(agent_data.dir, 'idle00', ['pending', 'pending'])
    _build_index(agent_data.dir)
    assert read == [f'ledger_{AGENT}_idle00.json']


def test_an_index_that_cannot_be_recorded_still_answers(agent_data, monkeypatch):
    """A folder the index cannot be written to still gets the pass's answer,
    and the same files are read again next time."""
    from agent_ledger.backends import JSONBackend
    from agent_ledger.core import SmartLedger
    _save_idle_ledgers(agent_data.dir)
    _save_ledger(agent_data.dir, 'busy', ['in_progress'])
    _drop_marks(agent_data.dir)

    def refuse(self, when):
        raise PermissionError('mark folder is read-only')

    monkeypatch.setattr(JSONBackend, 'stamp_mark_index', refuse)

    assert SmartLedger.keys_in_progress(str(agent_data.dir)) == [f'ledger_{AGENT}_busy']
    assert not (agent_data.dir / '.in_progress' / '.last_pass').exists()


def _pass_recorded_after(data_dir, seconds):
    """Record the index's last pass as ``seconds`` from now: a pass that ran
    after every file's last write."""
    import time
    from agent_ledger.backends import JSONBackend
    JSONBackend(storage_dir=str(data_dir)).stamp_mark_index(time.time() + seconds)


def _count_reads(monkeypatch):
    from agent_ledger import core
    read = []
    real_read = core._saved_work_in_progress
    monkeypatch.setattr(core, '_saved_work_in_progress',
                        lambda path: (read.append(os.path.basename(path)), real_read(path))[1])
    return read


def test_a_mark_cleared_after_a_later_pass_is_found_after_a_restart(agent_data):
    """Review of 1b6e34f42, R1: an instance saves its running task and marks
    it, a pass runs, another instance of the same key clears the mark more
    than the slack later, and the node restarts.  The file holds the running
    task and no mark, and was written before the last pass.  The new
    process's first tick still finds it."""
    from agent_ledger.backends import JSONBackend
    _save_idle_ledgers(agent_data.dir)
    _save_ledger(agent_data.dir, 'busy', ['in_progress'])
    _build_index(agent_data.dir)
    _pass_recorded_after(agent_data.dir, 10)
    JSONBackend(storage_dir=str(agent_data.dir)).set_mark(f'ledger_{AGENT}_busy', False)
    _fresh_process_view()

    out = _reap()

    assert out['reaped'] == 1 and agent_data.opened == ['busy'], (out, agent_data.opened)
    assert _saved_status(agent_data.dir, 'busy', 'action_1') == 'failed'


def test_a_ledger_restored_with_older_times_is_found_after_a_restart(agent_data):
    """Review of 1b6e34f42, R2: a ledger copied back over a name deleted
    seconds earlier keeps old times (NTFS hands the new file the deleted
    one's creation time, copy2 keeps the modification time), so it reads as
    written before the last pass.  A test cannot make NTFS do that; the pass
    is recorded after the file's times instead, the comparison the index
    makes.  The new process's first tick still finds the running task."""
    import time
    from agent_ledger.backends import JSONBackend
    _save_idle_ledgers(agent_data.dir)
    _build_index(agent_data.dir)
    JSONBackend(storage_dir=str(agent_data.dir)).save(
        f'ledger_{AGENT}_restored', _raw_ledger_data('restored', ['in_progress']))
    old = time.time() - 3600
    os.utime(agent_data.dir / f'ledger_{AGENT}_restored.json', (old, old))
    _pass_recorded_after(agent_data.dir, 10)
    _fresh_process_view()

    out = _reap()

    assert out['reaped'] == 1 and agent_data.opened == ['restored'], (out, agent_data.opened)


def test_each_process_reads_every_file_once(agent_data, monkeypatch):
    """The first pass in a process reads every ledger file whatever the
    index recorded; later passes in the same process read only the files
    written since."""
    import time
    from agent_ledger import core
    monkeypatch.setattr(core, '_INDEX_TIME_SLACK_S', 0.0)
    _save_idle_ledgers(agent_data.dir)
    time.sleep(0.05)
    _build_index(agent_data.dir)
    read = _count_reads(monkeypatch)

    _build_index(agent_data.dir)
    assert read == []

    _fresh_process_view()
    _build_index(agent_data.dir)
    assert len(read) == IDLE_LEDGERS

    _build_index(agent_data.dir)
    assert len(read) == IDLE_LEDGERS


def test_a_first_pass_that_cannot_be_recorded_reads_every_file_again(agent_data, monkeypatch):
    """A process whose first pass could not record the index has not read
    the folder in full as far as the next pass knows: it reads every file
    again."""
    from agent_ledger.backends import JSONBackend
    _save_idle_ledgers(agent_data.dir)
    read = _count_reads(monkeypatch)
    real_stamp = JSONBackend.stamp_mark_index

    def refuse(self, when):
        raise PermissionError('mark folder is read-only')

    monkeypatch.setattr(JSONBackend, 'stamp_mark_index', refuse)
    _build_index(agent_data.dir)
    assert len(read) == IDLE_LEDGERS
    monkeypatch.setattr(JSONBackend, 'stamp_mark_index', real_stamp)
    read.clear()

    _build_index(agent_data.dir)
    assert len(read) == IDLE_LEDGERS
