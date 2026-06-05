"""Behavioural tests for the llama-server CPU-attribution fix that stops the
yield-gate flapping (2026-05-31).

ROOT (idle-hour: 0 goal executions): both yield-gate reasons read TOTAL CPU.
When the agent daemon runs its own LLM tick, llama-server spikes total CPU →
(a) governor goes ACTIVE / throttle<0.3 → 'governor_throttle', AND (b)
model_lifecycle._calculate_throttle_factor sees cpu>=95 → factor 0.1 →
'model_pressure'.  llama-server is NOT register_subprocess'd (HARTOS doesn't
spawn it), so the 2026-05-30 own-CPU fix didn't exclude it.

FIX: ResourceGovernor resolves llama-server by its LISTENING PORT and counts
it as own; both gates now read the governor's EXTERNAL cpu (total - own) via
the single getter get_external_cpu_fraction().  So the daemon's own LLM work
no longer trips either gate, while genuine foreign load still does.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.resource_governor as rg
from core.resource_governor import ResourceGovernor


def _gov():
    return ResourceGovernor(idle_threshold_seconds=120)


# ── Governor: llama-server resolved by port + counted as own ──────────────

def test_resolve_llm_server_pid_by_listening_port(monkeypatch):
    g = _gov()
    g._cached_llm_pid = None

    class _Conn:
        def __init__(self, port, pid, status='LISTEN'):
            self.status = status
            self.laddr = type('A', (), {'port': port})()
            self.pid = pid

    fake_ps = MagicMock()
    fake_ps.pid_exists.return_value = True
    fake_ps.net_connections.return_value = [
        _Conn(443, 999, 'ESTABLISHED'),   # not LISTEN, ignore
        _Conn(8082, 4242, 'LISTEN'),      # llama-server
    ]
    monkeypatch.setattr(rg, '_try_import_psutil', lambda: fake_ps)
    # No port_registry hits → falls back to default ports incl. 8082
    pid = g._resolve_llm_server_pid()
    assert pid == 4242, "must resolve the PID LISTENing on the llama port"
    # Cached: second call uses pid_exists fast-path, no re-scan
    fake_ps.net_connections.reset_mock()
    assert g._resolve_llm_server_pid() == 4242
    assert not fake_ps.net_connections.called, "cached pid must skip re-scan"


def test_resolve_llm_pid_recovers_when_cached_dies(monkeypatch):
    g = _gov()
    g._cached_llm_pid = 1111  # stale

    fake_ps = MagicMock()
    fake_ps.pid_exists.return_value = False  # cached pid gone

    class _Conn:
        status = 'LISTEN'
        def __init__(self, port, pid):
            self.laddr = type('A', (), {'port': port})()
            self.pid = pid
    fake_ps.net_connections.return_value = [_Conn(8080, 5555)]
    monkeypatch.setattr(rg, '_try_import_psutil', lambda: fake_ps)
    assert g._resolve_llm_server_pid() == 5555


def test_resolve_llm_pid_none_without_psutil(monkeypatch):
    g = _gov()
    monkeypatch.setattr(rg, '_try_import_psutil', lambda: None)
    assert g._resolve_llm_server_pid() is None


def test_get_external_cpu_fraction_returns_cached_or_none():
    g = _gov()
    # Before first tick → None (caller falls back to total)
    assert g.get_external_cpu_fraction() is None
    g._cached_total_cpu = 0.9
    g._cached_external_cpu = 0.2
    assert g.get_external_cpu_fraction() == 0.2


def test_own_cpu_includes_llm_server(monkeypatch):
    """The daemon's LLM (llama) CPU must count toward OWN, so external stays
    low even when llama is pinning a core."""
    g = _gov()
    g._own_proc_cache = {}
    g._managed_subprocesses = {}
    g._cached_llm_pid = None

    class _Proc:
        def __init__(self, pid, pct):
            self.pid = pid
            self._pct = pct
        def cpu_percent(self, _=None):
            return self._pct
        def children(self, recursive=False):
            return []

    procs = {os.getpid(): _Proc(os.getpid(), 0.0), 7777: _Proc(7777, 320.0)}

    fake_ps = MagicMock()
    fake_ps.cpu_count.return_value = 4
    fake_ps.Process.side_effect = lambda pid: procs[pid]
    monkeypatch.setattr(rg, '_try_import_psutil', lambda: fake_ps)
    monkeypatch.setattr(g, '_resolve_llm_server_pid', lambda: 7777)

    own = g._get_own_cpu_usage()
    # 320% (llama) + 0% (main) over 4 cores = 0.8 of capacity, attributed OWN
    assert abs(own - 0.8) < 1e-9, f"llama CPU must count as own, got {own}"


# ── model_lifecycle: throttle_factor reads governor external, not total ───

def test_model_pressure_uses_governor_external_not_total(monkeypatch):
    """With total CPU pinned by HARTOS's own llama (external low), the model
    throttle_factor must stay healthy — NOT collapse to 0.1 — so the
    'model_pressure' gate reason no longer flaps."""
    from integrations.service_tools import model_lifecycle as ml

    mgr = ml.ModelLifecycleManager.__new__(ml.ModelLifecycleManager)
    mgr._cpu_pressure_pct = 80.0

    # Governor reports LOW external cpu (HARTOS's own llama excluded)
    class _Gov:
        def get_external_cpu_fraction(self):
            return 0.15  # 15% external → no CPU throttle
    monkeypatch.setattr('core.resource_governor.get_governor', lambda: _Gov())

    factor = mgr._calculate_throttle_factor(
        cpu_on=False, ram_on=False, vram_on=False, disk_on=False)
    assert factor == 1.0, (
        f"external cpu 15% must NOT throttle; got {factor} "
        "(regression: still reading total cpu)")


def test_model_pressure_still_throttles_on_real_external_load(monkeypatch):
    """When ANOTHER app pins the box (external cpu high), throttle_factor
    still collapses — politeness preserved."""
    from integrations.service_tools import model_lifecycle as ml
    mgr = ml.ModelLifecycleManager.__new__(ml.ModelLifecycleManager)
    mgr._cpu_pressure_pct = 80.0

    class _Gov:
        def get_external_cpu_fraction(self):
            return 0.97  # foreign app pinning the box
    monkeypatch.setattr('core.resource_governor.get_governor', lambda: _Gov())

    factor = mgr._calculate_throttle_factor(
        cpu_on=True, ram_on=False, vram_on=False, disk_on=False)
    assert factor <= 0.1, f"real external load must throttle; got {factor}"
