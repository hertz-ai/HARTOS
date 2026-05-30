"""Behavioural tests for the ResourceGovernor external-CPU self-defeat fix
(task #60, 2026-05-30).

ROOT CAUSE: the governor read TOTAL system CPU for its load-based backoff.
When the user stepped away but HARTOS's OWN flywheel/LLM work pushed CPU
past 0.85, the monitor flipped to ACTIVE mode (throttle 0.05) — or in IDLE
mode the throttle scaled to 0.2 — and ``should_yield_to_user()``'s reason
#3 (``throttle < 0.3``) blocked every daemon tick.  The flywheel halted the
instant it started using the CPU it exists to contribute.

FIX: attribute CPU to HARTOS's own process tree vs everything else, and use
EXTERNAL cpu (total - own) for the backoff decision + the IDLE throttle
scaling.  These tests pin the new behaviour without spinning the monitor
thread or touching real load.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.resource_governor import (  # noqa: E402
    ResourceGovernor, MODE_ACTIVE, MODE_IDLE, MODE_SLEEP,
    LOAD_BACKOFF_CPU, LOAD_BACKOFF_MEM,
)


def _gov() -> ResourceGovernor:
    """Construct a governor WITHOUT starting its monitor/proactive threads."""
    return ResourceGovernor(idle_threshold_seconds=120)


# ── _target_mode_for: the pure decision (this is the fix's core) ──────────

def test_idle_user_own_work_only_stays_idle():
    """User away + the only CPU load is HARTOS's own tree (ext_cpu low) →
    IDLE so the flywheel may run.  This is the behaviour 18e59c0 failed to
    deliver — the whole point of the fix."""
    g = _gov()
    mode = g._target_mode_for(user_idle=True, ext_cpu=0.10, mem=0.5,
                              battery_level=1.0, on_battery=False)
    assert mode == MODE_IDLE


def test_idle_user_external_app_load_goes_active():
    """User away but ANOTHER app (render/compile) loads the box past the
    threshold → ACTIVE (politeness preserved)."""
    g = _gov()
    mode = g._target_mode_for(user_idle=True, ext_cpu=LOAD_BACKOFF_CPU + 0.05,
                              mem=0.5, battery_level=1.0, on_battery=False)
    assert mode == MODE_ACTIVE


def test_active_user_always_yields():
    """User at the keyboard → ACTIVE regardless of how low external CPU is."""
    g = _gov()
    mode = g._target_mode_for(user_idle=False, ext_cpu=0.0, mem=0.0,
                              battery_level=1.0, on_battery=False)
    assert mode == MODE_ACTIVE


def test_high_memory_pressure_goes_active_even_when_idle():
    """Memory pressure is a real user-system risk regardless of source →
    still backs off (mem kept as total, not attributed)."""
    g = _gov()
    mode = g._target_mode_for(user_idle=True, ext_cpu=0.10,
                              mem=LOAD_BACKOFF_MEM + 0.02,
                              battery_level=1.0, on_battery=False)
    assert mode == MODE_ACTIVE


def test_critical_battery_sleeps():
    g = _gov()
    mode = g._target_mode_for(user_idle=True, ext_cpu=0.10, mem=0.1,
                              battery_level=0.10, on_battery=True)
    assert mode == MODE_SLEEP


# ── _refresh_cpu_attribution: external = total - own ──────────────────────

def test_refresh_attribution_external_is_total_minus_own(monkeypatch):
    g = _gov()
    monkeypatch.setattr(g, '_get_cpu_usage', lambda: 0.90)
    monkeypatch.setattr(g, '_get_own_cpu_usage', lambda: 0.70)
    g._refresh_cpu_attribution()
    assert g._cached_total_cpu == 0.90
    assert g._cached_own_cpu == 0.70
    assert abs(g._cached_external_cpu - 0.20) < 1e-9


def test_refresh_attribution_never_negative(monkeypatch):
    """Own can momentarily exceed total during a sampling skew — external
    must clamp at 0, not go negative."""
    g = _gov()
    monkeypatch.setattr(g, '_get_cpu_usage', lambda: 0.30)
    monkeypatch.setattr(g, '_get_own_cpu_usage', lambda: 0.45)
    g._refresh_cpu_attribution()
    assert g._cached_external_cpu == 0.0


# ── _calculate_throttle (IDLE): scales on EXTERNAL, not own ───────────────

def test_idle_throttle_does_not_collapse_on_own_work(monkeypatch):
    """The bug: own work (total high, external low) dropped the throttle
    below the 0.3 gate, halting the daemon.  After the fix the throttle
    stays full because external is low."""
    g = _gov()
    g._mode = MODE_IDLE
    g._cached_total_cpu = 0.95       # box looks busy...
    g._cached_external_cpu = 0.10    # ...but it's HARTOS's own work
    monkeypatch.setattr(g, '_get_memory_pressure', lambda: 0.4)
    monkeypatch.setattr(g, '_get_battery_status', lambda: (1.0, False))
    t = g.get_throttle()
    assert t >= 0.3, f"own work collapsed own throttle below the gate: {t}"
    assert t == 1.0


def test_idle_throttle_scales_down_on_external_load(monkeypatch):
    """When ANOTHER app loads the box, the throttle DOES scale down (the
    daemon yields) — politeness preserved."""
    g = _gov()
    g._mode = MODE_IDLE
    g._cached_total_cpu = 0.95
    g._cached_external_cpu = 0.90     # external app is busy
    monkeypatch.setattr(g, '_get_memory_pressure', lambda: 0.4)
    monkeypatch.setattr(g, '_get_battery_status', lambda: (1.0, False))
    t = g.get_throttle()
    assert t < 0.3                   # ext > 0.8 → throttle 0.2 → yields


def test_external_cpu_for_throttle_falls_back_to_total_before_first_tick(monkeypatch):
    """Before the monitor has run once (cache==0), throttle scaling must
    fall back to total CPU rather than optimistically assuming 0."""
    g = _gov()
    g._cached_total_cpu = 0.0        # attribution never ran
    monkeypatch.setattr(g, '_get_cpu_usage', lambda: 0.5)
    assert g._external_cpu_for_throttle() == 0.5


# ── _get_own_cpu_usage: arithmetic (sum of per-core % / core count) ───────

def test_own_cpu_usage_normalizes_by_core_count(monkeypatch):
    import core.resource_governor as rg

    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid
        def cpu_percent(self, _=None):
            return 200.0           # two cores' worth
        def children(self, recursive=False):
            return []

    class _FakePsutil:
        def cpu_count(self):
            return 4
        def Process(self, pid):
            return _FakeProc(pid)

    g = _gov()
    g._own_proc_cache = {}          # force fresh handle creation
    g._managed_subprocesses = {}
    monkeypatch.setattr(rg, '_try_import_psutil', lambda: _FakePsutil())
    frac = g._get_own_cpu_usage()
    # 200% of one core / 4 cores = 0.5 of total capacity
    assert abs(frac - 0.5) < 1e-9


def test_own_cpu_usage_zero_without_psutil(monkeypatch):
    """No psutil → own attributed as 0 → external == total → conservative
    (backs off exactly as the pre-fix code did)."""
    import core.resource_governor as rg
    g = _gov()
    monkeypatch.setattr(rg, '_try_import_psutil', lambda: None)
    assert g._get_own_cpu_usage() == 0.0


def test_get_stats_exposes_cpu_attribution():
    g = _gov()
    g._cached_total_cpu = 0.8
    g._cached_own_cpu = 0.6
    g._cached_external_cpu = 0.2
    stats = g.get_stats()
    assert stats['cpu_total'] == 0.8
    assert stats['cpu_own'] == 0.6
    assert stats['cpu_external'] == 0.2
