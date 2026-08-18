"""ResourceGovernor CPU rebalance (Active 50% / Idle 80%, model never throttled).

Pins the behaviour the 2026-06-03 rebalance introduced after the live finding
that chatting strangled Nunba + the model:
  - ACTIVE used to hard-cap the whole Job Object to 25% CPU / 4 cores AND set
    gpu_frac=0 (CUDA_VISIBLE_DEVICES=''), and llama-server — a CHILD of Nunba —
    inherited that cap.  The chat (and the model) crawled past the 3-min UI
    timeout.
Now: caps are single-sourced (ACTIVE_CPU_LIMIT / IDLE_CPU_LIMIT), the GPU stays
visible when active, CPU is limited by per-process affinity (not a job-wide rate
cap), and the model is pinned back to every core.

Behavioural — imports the real code, mocks the OS boundary (psutil/ctypes),
calls the real methods, asserts observable side-effects.  No grep tests.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import Mock, patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_caps_are_single_sourced_and_gpu_stays_visible_when_active():
    """update_caps derives CPU from the module constants and never hides the GPU
    in active/idle (gpu_frac>0 → no CUDA_VISIBLE_DEVICES='')."""
    from core.resource_governor import (
        ResourceEnforcer, ACTIVE_CPU_LIMIT, IDLE_CPU_LIMIT,
        MODE_ACTIVE, MODE_IDLE)
    e = ResourceEnforcer()
    e._enforced = True
    with patch.object(e, '_enforce_cpu') as cpu, \
         patch.object(e, '_enforce_ram'), \
         patch.object(e, '_enforce_gpu') as gpu:
        e.update_caps(MODE_ACTIVE)
        active_cpu, active_gpu = cpu.call_args[0][0], gpu.call_args[0][0]
        e.update_caps(MODE_IDLE)
        idle_cpu, idle_gpu = cpu.call_args[0][0], gpu.call_args[0][0]

    assert active_cpu == ACTIVE_CPU_LIMIT == 0.50   # single source, raised from 0.25
    assert idle_cpu == IDLE_CPU_LIMIT == 0.80       # raised from 0.75
    assert active_gpu > 0.0, "GPU must stay visible in ACTIVE (was 0.0 → hidden)"
    assert idle_gpu > 0.0


def test_ensure_job_windows_is_idempotent_no_recreate():
    """The Job Object is created once and reused — not recreated per mode flip
    (which on Windows accumulated to the most-restrictive cap)."""
    from core.resource_governor import ResourceEnforcer
    e = ResourceEnforcer()
    e._job_handle = 4242  # already created
    # create=True: ctypes has no `windll` attribute on Linux runners, and
    # patch() refuses to patch a missing attribute without it. The test
    # drives the Windows-only path directly, so the mock must exist on
    # every platform.
    with patch('ctypes.windll', create=True) as wd:
        e._ensure_job_windows()
        wd.kernel32.CreateJobObjectW.assert_not_called()


def test_model_is_pinned_to_every_core():
    """_unrestrict_llm_affinity gives llama-server ALL cores so the model is
    never core-starved by our orchestration affinity limit."""
    from core.resource_governor import ResourceEnforcer
    e = ResourceEnforcer()
    model_proc = Mock()
    psutil_mod = Mock()
    psutil_mod.Process.return_value = model_proc
    with patch('core.resource_governor._find_llm_pids', return_value={4242}), \
         patch('core.resource_governor._try_import_psutil', return_value=psutil_mod):
        e._unrestrict_llm_affinity(total_cores=16)
    model_proc.cpu_affinity.assert_called_once_with(list(range(16)))


def test_enforce_cpu_limits_our_process_by_affinity_and_exempts_model():
    """CPU limit = per-process affinity on OUR process (last N cores), NOT a
    job-wide rate cap; the model is exempted via _unrestrict_llm_affinity."""
    from core.resource_governor import ResourceEnforcer
    e = ResourceEnforcer()
    our_proc = Mock()
    psutil_mod = Mock()
    psutil_mod.Process.return_value = our_proc
    with patch('core.resource_governor._try_import_psutil', return_value=psutil_mod), \
         patch.object(e, '_ensure_job_windows') as job, \
         patch.object(e, '_unrestrict_llm_affinity') as exempt, \
         patch.object(e, '_enforce_cpu_linux'):
        e._enforce_cpu(0.50, usable_cores=8, total_cores=16)

    # our orchestration restricted to the last 8 of 16 cores
    our_proc.cpu_affinity.assert_called_once_with(list(range(16))[-8:])
    # the model is always exempted
    exempt.assert_called_once_with(16)
    # on Windows the job is ensured (memory ceiling) — but it is NOT a rate cap
    if sys.platform == 'win32':
        job.assert_called_once()


def test_active_mode_keeps_gpu_allowed_and_50pct():
    """Transitioning to ACTIVE now leaves the GPU available (was False) — the
    model is foreground work — and sets the 50% advisory limit."""
    from core.resource_governor import ResourceGovernor, MODE_ACTIVE, MODE_IDLE
    gov = ResourceGovernor()
    gov._mode = MODE_IDLE  # so the transition actually runs
    with patch('core.resource_governor.get_enforcer'):
        gov._transition_to(MODE_ACTIVE)
    assert gov._gpu_allowed is True
    assert gov._cpu_limit == 0.50


def test_should_allow_gpu_follows_flag_even_in_active():
    """The model (gpu) is never blocked just because the user is at the keyboard
    — should_allow('gpu') follows the gpu_allowed flag in active too."""
    from core.resource_governor import ResourceGovernor, MODE_ACTIVE
    gov = ResourceGovernor()
    gov._mode = MODE_ACTIVE
    gov._gpu_allowed = True
    assert gov.should_allow('gpu') is True
    assert gov.should_allow('cpu_heavy') is True   # bounded by affinity, allowed
    gov._gpu_allowed = False
    assert gov.should_allow('gpu') is False
    # background disk/network heavy still backs off in active (politeness)
    assert gov.should_allow('disk_heavy') is False
