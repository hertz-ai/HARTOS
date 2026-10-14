"""integrations.reel_studio.jobs: one reel at a time on this desktop, and an
honest word when the desktop restarted in the middle of one.

* start_job() hands out a job, or None when a reel is already being made on
  this machine (any user, any process): rendering is one ffmpeg at a time and
  the GPU is shared with the language models.
* finish_job() records how the job ended and frees the machine.
* sweep_interrupted() finds jobs a dead process left 'running', marks them
  'interrupted' and tells each user once, on her own chat topic and never
  through central's crossbar (decision U4(d)).
* Records live under get_data_dir()/reel_studio/jobs/ and are written
  atomically by this module alone.

Tests use a temporary data root and the real publish_agent_message, with the
HARTOS publisher (publish_async) replaced by a recorder: the boundary.
"""
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from integrations.reel_studio import contracts as C


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    from tests.conftest import refuse_all_network
    refuse_all_network(monkeypatch)
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    from core import platform_paths
    platform_paths.reset_cache()
    from integrations.reel_studio import jobs as J
    monkeypatch.setattr(J, '_SWEPT', False)
    monkeypatch.setattr(J, '_ACTIVE', set())
    yield J
    platform_paths.reset_cache()


@pytest.fixture
def pushes(monkeypatch):
    """Every publish_async call: (topic, payload, flags).  The HARTOS
    publisher is the boundary; publish_agent_message is the real one."""
    sent = []

    def publish_async(topic, message, timeout=2.0, skip_crossbar=False):
        sent.append((topic, message, {'skip_crossbar': skip_crossbar}))

    monkeypatch.setitem(sys.modules, 'hart_intelligence',
                        SimpleNamespace(publish_async=publish_async))
    return sent


def _dead_pid():
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait(timeout=60)
    return proc.pid


def _record_path(J, reel_id):
    return os.path.join(J.jobs_dir(), reel_id + '.json')


# ── single flight ─────────────────────────────────────────────────────────

def test_a_job_is_recorded_running_by_this_process(jobs):
    job = jobs.start_job('10077', 42, 'req-1')
    assert job is not None
    assert job.status == 'running' and job.pid == os.getpid()
    assert job.user_id == '10077' and job.prompt_id == '42' and job.request_id == 'req-1'
    with open(_record_path(jobs, job.reel_id), encoding='utf-8') as f:
        on_disk = json.load(f)
    assert on_disk['status'] == 'running' and on_disk['reel_id'] == job.reel_id
    assert jobs.reel_job(job.reel_id).to_dict() == job.to_dict()


def test_a_second_job_waits_for_the_first_whoever_asks(jobs):
    first = jobs.start_job('10077', '42', 'a')
    assert first is not None
    assert jobs.start_job('10077', '42', 'b') is None        # same user
    assert jobs.start_job('20088', '7', 'c') is None         # another user: one per machine
    jobs.finish_job(first.reel_id, 'ok')
    again = jobs.start_job('20088', '7', 'c')
    assert again is not None and again.reel_id != first.reel_id


def test_racing_starts_hand_out_one_job(jobs):
    got, gate = [], threading.Barrier(8)

    def go(i):
        gate.wait()
        got.append(jobs.start_job('u%d' % i, '1', None))
    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sum(j is not None for j in got) == 1


def test_finish_records_the_end(jobs):
    job = jobs.start_job('10077', '42', 'r')
    jobs.finish_job(job.reel_id, 'qa_failed', 'fps')
    done = jobs.reel_job(job.reel_id)
    assert done.status == 'qa_failed' and done.detail == 'fps'
    assert done.finished_at is not None and done.finished_at >= done.started_at
    assert jobs.active_job() is None


def test_finish_takes_only_a_reel_status(jobs):
    job = jobs.start_job('10077', '42', 'r')
    with pytest.raises(ValueError):
        jobs.finish_job(job.reel_id, 'done')
    for status in C.REEL_STATUSES:
        assert status in jobs.END_STATUSES or status == 'busy'


def test_finishing_an_unknown_job_frees_nothing_it_does_not_hold(jobs):
    job = jobs.start_job('10077', '42', 'r')
    jobs.finish_job('0' * 32, 'ok')
    assert jobs.start_job('10077', '42', 'again') is None
    jobs.finish_job(job.reel_id, 'ok')


# ── a desktop that restarted mid-reel ─────────────────────────────────────

def _orphan(J, pid, *, user='10077', prompt='42', request='req-9', reel='a' * 32,
            started=None):
    """A 'running' record (and the machine claim) left by a process that died."""
    started = time.time() - 30 if started is None else started
    os.makedirs(J.jobs_dir(), exist_ok=True)
    record = {'schema': J.JOB_SCHEMA, 'reel_id': reel, 'user_id': user, 'prompt_id': prompt,
              'request_id': request, 'status': 'running', 'pid': pid, 'started_at': started,
              'finished_at': None, 'detail': ''}
    with open(_record_path(J, reel), 'w', encoding='utf-8') as f:
        json.dump(record, f)
    with open(os.path.join(J.jobs_dir(), J.CLAIM_NAME), 'w', encoding='utf-8') as f:
        json.dump({'reel_id': reel, 'pid': pid, 'started_at': started}, f)
    return reel


def test_a_dead_process_job_is_interrupted_and_she_is_told_once(jobs, pushes):
    reel = _orphan(jobs, _dead_pid())
    swept = jobs.sweep_interrupted()
    assert [j.reel_id for j in swept] == [reel]
    assert jobs.reel_job(reel).status == 'interrupted'
    assert len(pushes) == 1
    topic, payload, flags = pushes[0]
    assert topic.endswith('.10077')
    assert payload['text'] == [jobs.INTERRUPTED_NOTICE]
    assert payload['request_id'] == 'req-9' and payload['prompt_id'] == '42'
    assert flags == {'skip_crossbar': True}
    # A second sweep finds nothing and tells nobody again.
    assert jobs.sweep_interrupted() == []
    assert len(pushes) == 1
    # The machine is free again.
    assert jobs.start_job('10077', '42', 'retry') is not None


def test_another_live_process_job_is_left_running(jobs, pushes):
    # The parent of this test process stands in for a CLI making a reel.
    reel = _orphan(jobs, os.getppid())
    assert jobs.sweep_interrupted() == []
    assert jobs.reel_job(reel).status == 'running' and pushes == []
    assert jobs.start_job('20088', '7', 'x') is None


def test_this_process_own_running_job_is_left_running(jobs, pushes):
    job = jobs.start_job('10077', '42', 'r')
    assert jobs.sweep_interrupted() == []
    assert jobs.reel_job(job.reel_id).status == 'running' and pushes == []


def test_a_record_with_this_pid_this_process_never_started_is_interrupted(jobs, pushes):
    # The app restarted and the OS gave it the pid the old one had.
    reel = _orphan(jobs, os.getpid())
    assert [j.reel_id for j in jobs.sweep_interrupted()] == [reel]
    assert len(pushes) == 1


def test_a_job_running_past_its_bound_is_interrupted_even_if_the_pid_lives(jobs, pushes):
    # A pid can be reused by an unrelated process; past MAX_JOB_S no reel is still working.
    reel = _orphan(jobs, os.getppid(), started=time.time() - jobs.MAX_JOB_S - 60)
    swept = jobs.sweep_interrupted()
    assert [j.reel_id for j in swept] == [reel] and len(pushes) == 1


def test_start_takes_over_a_dead_claim(jobs, pushes):
    reel = _orphan(jobs, _dead_pid())
    job = jobs.start_job('20088', '7', 'mine')
    assert job is not None
    assert jobs.reel_job(reel).status == 'interrupted'
    assert len(pushes) == 1                      # the dead job's user is still told


def test_sweep_once_runs_once_per_process(jobs, pushes):
    _orphan(jobs, _dead_pid())
    assert len(jobs.sweep_once()) == 1
    _orphan(jobs, _dead_pid(), reel='b' * 32)
    assert jobs.sweep_once() == []


def test_sweeping_an_empty_machine_creates_nothing(jobs):
    assert jobs.sweep_interrupted() == []
    assert not os.path.exists(jobs.jobs_dir())


def test_a_damaged_record_is_skipped_not_fatal(jobs, pushes):
    os.makedirs(jobs.jobs_dir(), exist_ok=True)
    with open(_record_path(jobs, 'c' * 32), 'w', encoding='utf-8') as f:
        f.write('{not json')
    assert jobs.sweep_interrupted() == []
    assert jobs.start_job('10077', '42', 'r') is not None


# ── the record on disk ────────────────────────────────────────────────────

def test_a_failed_write_leaves_the_previous_record_whole(jobs, monkeypatch):
    job = jobs.start_job('10077', '42', 'r')
    path = _record_path(jobs, job.reel_id)
    with open(path, encoding='utf-8') as f:
        before = f.read()

    def refuse(src, dst):
        raise OSError(28, 'No space left on device')
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, 'replace', refuse)       # the disk fills at the atomic swap
        with pytest.raises(OSError):
            jobs.finish_job(job.reel_id, 'ok')
    with open(path, encoding='utf-8') as f:
        assert f.read() == before
    leftovers = [n for n in os.listdir(jobs.jobs_dir()) if n.endswith('.tmp')]
    assert leftovers == []
    # The machine is not left held by a job that has ended.
    assert jobs.start_job('20088', '7', 'next') is not None


def test_old_finished_records_are_pruned_by_the_sweep(jobs):
    job = jobs.start_job('10077', '42', 'r')
    jobs.finish_job(job.reel_id, 'ok')
    old = time.time() - jobs.RECORD_KEEP_S - 60
    os.utime(_record_path(jobs, job.reel_id), (old, old))
    jobs.sweep_interrupted()
    assert jobs.reel_job(job.reel_id) is None
