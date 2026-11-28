"""HevolveAI stays inside its share of the machine.

Owner, 2026-11-27: "hevolveai shd not pull the entire system down", then "20% of
free VRAM ,  30% of overall available CPU at max , 20% of RAM and SSD storage
streaming when needed as part of the elastic compute desgin we already have".

Live, 2026-11-27 23:30-23:50 IST: the owner's HevolveAI child (PID 10808)
held 14.4 GB of commit, 36% of the box's 39.7 GB, with 3.1 GB resident in 223
threads.  Free commit on the box was 0.8-1.8 GB, and every test slot was
blocked.  By reading integrations/agent_engine/hevolveai_supervisor.py:
  * the child's Job Object set no memory limit at all;
  * its CPU cap was read as "a fraction of one core" while Windows applies
    CpuRate to the whole machine, so idle 0.50 let the child take half of all
    16 processors, and the cap was set only on a governor mode change (none
    until then);
  * the child's numeric pools (OpenMP / OpenBLAS / MKL / numexpr) start one
    worker per processor for every calling thread: ~55 MB of commit each
    (cc12b886, measured on this box: 59 threads 3.9 GB, 10 threads 1.2 GB).
Now: one worker per pool in the child's env, the RAM share as the Job's memory
ceiling, every CPU cap held to the CPU share from the spawn on, and the three
shares handed to the child, all from core.resource_governor, the one place
they live.

Real: _Supervisor._build_env, _JobHandle._set_limits, _JobHandle.set_cpu_rate,
_child_cpu_fraction, _hevolveai_share, _Supervisor._on_started.  Faked: kernel32
(the calls are recorded, nothing reaches Windows) and the total-RAM reading.
"""
import ctypes
import os
import sys
import types
from unittest.mock import patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.resource_governor as rg  # noqa: E402
from integrations.agent_engine import hevolveai_supervisor as hs  # noqa: E402

POOLS = ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
         'NUMEXPR_NUM_THREADS')


def _fake_self():
    return types.SimpleNamespace(api_url='http://127.0.0.1:8000', port=8000,
                                 pythonpath='')


def _env():
    with patch('core.port_registry.get_local_llm_url', return_value=None):
        return hs._Supervisor._build_env(_fake_self())


class _Kernel32:
    """Records each SetInformationJobObject call: (class, the struct)."""

    def __init__(self):
        self.calls = []

    def SetInformationJobObject(self, handle, info_class, ref, size):
        self.calls.append((info_class, ref._obj))
        return True


def _job():
    job = hs._JobHandle()
    job.handle = 1
    job._kernel32 = _Kernel32()
    return job


# ---- the child's env -------------------------------------------------------

def test_the_child_runs_one_worker_per_numeric_pool():
    with patch.dict(os.environ, {}, clear=False):
        for v in POOLS:
            os.environ.pop(v, None)
        env = _env()
    assert {v: env[v] for v in POOLS} == {v: '1' for v in POOLS}


def test_an_operators_pool_size_is_kept_and_said(caplog):
    with patch.dict(os.environ, {'OMP_NUM_THREADS': '4'}, clear=False):
        with caplog.at_level('INFO', logger=hs.logger.name):
            env = _env()
    assert env['OMP_NUM_THREADS'] == '4'
    assert 'OMP_NUM_THREADS=4' in caplog.text


def test_the_child_is_told_its_share_of_the_machine():
    env = _env()
    assert float(env['HEVOLVE_BUDGET_CPU_FRACTION']) == rg.HEVOLVEAI_CPU_FRACTION
    assert float(env['HEVOLVE_BUDGET_RAM_FRACTION']) == rg.HEVOLVEAI_RAM_FRACTION
    assert (float(env['HEVOLVE_BUDGET_FREE_VRAM_FRACTION'])
            == rg.HEVOLVEAI_FREE_VRAM_FRACTION)


def test_the_child_sees_no_gpu_whatever_this_process_inherited():
    """HevolveAI runs on the CPU by design; only HEVOLVE_ALLOW_CUDA=1 opens
    the GPU.  An inherited CUDA_VISIBLE_DEVICES (the governor writes and
    clears it in this process) does not."""
    for inherited in ('0', '', None):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('HEVOLVE_ALLOW_CUDA', None)
            os.environ.pop('CUDA_VISIBLE_DEVICES', None)
            if inherited is not None:
                os.environ['CUDA_VISIBLE_DEVICES'] = inherited
            env = _env()
        assert env['CUDA_VISIBLE_DEVICES'] == '', inherited


def test_the_switch_gives_the_gpu_and_keeps_a_real_device():
    with patch.dict(os.environ, {'HEVOLVE_ALLOW_CUDA': '1',
                                 'CUDA_VISIBLE_DEVICES': '0'}, clear=False):
        assert _env()['CUDA_VISIBLE_DEVICES'] == '0'
    with patch.dict(os.environ, {'HEVOLVE_ALLOW_CUDA': '1',
                                 'CUDA_VISIBLE_DEVICES': ''}, clear=False):
        assert 'CUDA_VISIBLE_DEVICES' not in _env(), \
            'an inherited empty fence still hid the GPU the switch gave'


def test_the_owners_shares():
    assert (rg.HEVOLVEAI_CPU_FRACTION, rg.HEVOLVEAI_RAM_FRACTION,
            rg.HEVOLVEAI_FREE_VRAM_FRACTION) == (0.30, 0.20, 0.20)


def test_the_shares_are_read_from_the_one_place_they_live():
    with patch.object(rg, 'HEVOLVEAI_CPU_FRACTION', 0.25), \
            patch.object(rg, 'HEVOLVEAI_RAM_FRACTION', 0.15):
        env = _env()
        assert env['HEVOLVE_BUDGET_CPU_FRACTION'] == '0.25'
        assert env['HEVOLVE_BUDGET_RAM_FRACTION'] == '0.15'
        assert hs._child_cpu_fraction('idle') == 0.25


# ---- CPU --------------------------------------------------------------------

def test_no_mode_lets_the_child_past_its_cpu_share():
    assert hs._child_cpu_fraction('idle') == pytest.approx(0.30)   # was 0.50
    assert hs._child_cpu_fraction('active') == pytest.approx(0.05)
    assert hs._child_cpu_fraction('sleep') == pytest.approx(0.01)
    assert hs._child_cpu_fraction('IDLE') == pytest.approx(0.30)
    assert hs._child_cpu_fraction('nonsense') is None


def test_the_cpu_cap_is_written_as_a_share_of_the_whole_machine():
    job = _job()
    assert job.set_cpu_rate(0.30) is True
    (_, rate), = job._kernel32.calls
    assert rate.Value == 3000          # cycles per 10,000 of all processors


def test_a_governor_idle_mode_applies_the_share_not_half_the_machine():
    job = _job()
    with patch.object(hs, '_JOB', job):
        hs._Supervisor._on_governor_mode_changed(
            types.SimpleNamespace(), 'resource.mode_changed', {'new_mode': 'idle'})
    (_, rate), = job._kernel32.calls
    assert rate.Value == 3000


@pytest.mark.parametrize('mode, cycles', [
    ('active', 500), ('sleep', 100), ('idle', 3000), (None, 3000)])
def test_a_child_starts_at_the_cap_of_the_mode_the_governor_is_in(
        monkeypatch, mode, cycles):
    """Review of cb74bf00b (rv_share F2): every spawn set 30%, so a child
    that restarted while the person was at the desk ran at six times its
    5% until the next mode change.  None: the mode cannot be read."""
    import core.resource_governor as rg

    def governor():
        if mode is None:
            raise RuntimeError('no governor')
        return types.SimpleNamespace(get_mode=lambda: mode)
    monkeypatch.setattr(rg, 'get_governor', governor)
    job = _job()
    job.assign = lambda h: True
    monkeypatch.setattr(hs, '_JOB', job)
    monkeypatch.setattr(hs.sys, 'platform', 'win32')
    fake = types.SimpleNamespace(_current_pid=None,
                                 _register_with_governor=lambda pid: None)
    hs._Supervisor._on_started(fake, types.SimpleNamespace(pid=42, _handle=7))
    (_, rate), = job._kernel32.calls
    assert rate.Value == cycles


def test_the_child_is_capped_from_the_spawn_on(monkeypatch):
    import core.resource_governor as rg
    monkeypatch.setattr(rg, 'get_governor',
                        lambda: types.SimpleNamespace(get_mode=lambda: 'idle'))
    job = _job()
    assigned = []
    job.assign = lambda h: assigned.append(h) or True
    monkeypatch.setattr(hs, '_JOB', job)
    monkeypatch.setattr(hs.sys, 'platform', 'win32')
    fake = types.SimpleNamespace(_current_pid=None,
                                 _register_with_governor=lambda pid: None)
    hs._Supervisor._on_started(fake, types.SimpleNamespace(pid=42, _handle=7))
    assert assigned == [7]
    (_, rate), = job._kernel32.calls
    assert rate.Value == 3000


# ---- RAM --------------------------------------------------------------------

def test_the_job_holds_the_child_to_its_ram_share():
    job = _job()
    with patch.object(rg.ResourceEnforcer, '_get_total_ram_gb',
                      staticmethod(lambda: 40.0)):
        assert job._set_limits() is True
    (_, info), = job._kernel32.calls
    flags = info.BasicLimitInformation.LimitFlags
    assert flags & hs._JOB_OBJECT_LIMIT_JOB_MEMORY
    assert flags & hs._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert flags & hs._JOB_OBJECT_LIMIT_BREAKAWAY_OK
    assert info.JobMemoryLimit == int(40.0 * 0.20 * 1024 ** 3)
    assert job.memory_limit_bytes == info.JobMemoryLimit


def test_without_a_ram_reading_the_job_still_kills_on_close():
    job = _job()
    with patch.object(hs, '_hevolveai_ram_limit_bytes',
                      side_effect=RuntimeError('no reading')):
        assert job._set_limits() is True
    (_, info), = job._kernel32.calls
    flags = info.BasicLimitInformation.LimitFlags
    assert flags & hs._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert not flags & hs._JOB_OBJECT_LIMIT_JOB_MEMORY
    assert info.JobMemoryLimit == 0


# ---- a child that runs out of its share (review hartos-91) -------------------
# A child whose steady need is above its share would die at the ceiling and be
# restarted every backoff, forever, unseen: the uptime breakers only count
# deaths inside 60 s.  Each such exit is named, and three in a row take the
# supervisor's long cooldown, said once.

def _exiting_supervisor():
    return types.SimpleNamespace(
        _current_pid=None, _unregister_from_governor=lambda pid: None,
        _ran_out_of_memory=False, _consecutive_memory_exits=0,
        _memory_cooldown_trips=0,
        _consecutive_fast_fails=0, _consecutive_unhealthy=0,
        last_error=None, extra_wait_once=0.0)


def test_a_child_that_says_it_ran_out_of_memory_is_named_at_its_exit(caplog):
    sup = _exiting_supervisor()
    hs._Supervisor._format_stdout_line(sup, 'MemoryError: Unable to allocate 2.1 GiB')
    with caplog.at_level('WARNING', logger=hs.logger.name):
        assert hs._Supervisor._on_child_exit(sup, 1, 3600.0) is False
    assert 'ended at its memory share' in caplog.text
    assert sup._consecutive_memory_exits == 1
    assert sup.extra_wait_once == 0.0
    assert sup._ran_out_of_memory is False      # read once, per exit


def test_a_native_no_memory_exit_is_named_without_a_traceback(caplog):
    sup = _exiting_supervisor()
    with caplog.at_level('WARNING', logger=hs.logger.name):
        hs._Supervisor._on_child_exit(sup, 0xC0000017, 900.0)
    assert sup._consecutive_memory_exits == 1


def test_three_exits_at_the_share_in_a_row_take_the_long_cooldown(caplog, monkeypatch):
    monkeypatch.setenv('HEVOLVE_SUPERVISOR_BREAKER_COOLDOWN_S', '1800')
    sup = _exiting_supervisor()
    with caplog.at_level('WARNING', logger=hs.logger.name):
        for _ in range(3):
            hs._Supervisor._format_stdout_line(sup, 'RuntimeError: not enough memory')
            hs._Supervisor._on_child_exit(sup, 1, 600.0)
    assert sup.extra_wait_once == 1800.0
    assert sup._consecutive_memory_exits == 0
    assert caplog.text.count('cooling down') == 1


def test_an_ordinary_exit_starts_the_count_again():
    sup = _exiting_supervisor()
    hs._Supervisor._format_stdout_line(sup, 'MemoryError')
    hs._Supervisor._on_child_exit(sup, 1, 600.0)
    hs._Supervisor._on_child_exit(sup, 0, 3600.0)
    assert sup._consecutive_memory_exits == 0
    assert sup.extra_wait_once == 0.0


def test_the_memory_cooldown_doubles_while_the_child_never_fits(monkeypatch):
    """Review of cb74bf00b (rv_share F3): the waits were 1 s, 1 s, then a
    flat 1800 s, repeating for ever.  Each cooldown taken again with no
    ordinary exit between is twice the last, up to one try a day."""
    monkeypatch.setenv('HEVOLVE_SUPERVISOR_BREAKER_COOLDOWN_S', '1800')
    sup = _exiting_supervisor()
    waits = []
    for _ in range(8):
        for _ in range(hs._MEMORY_EXIT_LIMIT):
            sup._ran_out_of_memory = True
            hs._Supervisor._on_child_exit(sup, 1, 3600.0)
        waits.append(sup.extra_wait_once)
    assert waits[:4] == [1800.0, 3600.0, 7200.0, 14400.0]
    assert waits == sorted(waits) and waits[-1] == hs._MEMORY_COOLDOWN_MAX_S


def test_an_ordinary_exit_starts_the_cooldown_small_again(monkeypatch):
    monkeypatch.setenv('HEVOLVE_SUPERVISOR_BREAKER_COOLDOWN_S', '1800')
    sup = _exiting_supervisor()

    def trip():
        for _ in range(hs._MEMORY_EXIT_LIMIT):
            sup._ran_out_of_memory = True
            hs._Supervisor._on_child_exit(sup, 1, 3600.0)
        return sup.extra_wait_once
    assert trip() == 1800.0 and trip() == 3600.0
    hs._Supervisor._on_child_exit(sup, 0, 3600.0)        # an ordinary exit
    assert trip() == 1800.0


@pytest.mark.parametrize('line', [
    'OSError: [WinError 1455] The paging file is too small for this operation',
    'The paging file is too small for this operation to complete'])
def test_a_commit_limit_line_is_a_memory_exit(line):
    sup = _exiting_supervisor()
    hs._Supervisor._format_stdout_line(sup, line)
    hs._Supervisor._on_child_exit(sup, 1, 3600.0)
    assert sup._consecutive_memory_exits == 1


def test_a_native_commit_limit_exit_code_is_a_memory_exit():
    sup = _exiting_supervisor()
    hs._Supervisor._on_child_exit(sup, 0xC000012D, 3600.0)
    assert sup._consecutive_memory_exits == 1
