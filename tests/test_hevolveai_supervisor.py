"""
Unit tests for the HevolveAI subprocess supervisor + its resource-governor
accounting hooks.

These are pure-Python tests: they exercise the env-resolution helpers and the
governor register/unregister bookkeeping WITHOUT spawning a real subprocess or
touching ctypes / Job Objects (the live spawn + kill-on-close path is verified
separately by an inline smoke test). No Docker, no network.

Regression coverage:
  * L3 -- HEVOLVEAI_API_URL must be DERIVED from HEVOLVEAI_PORT when not set
    explicitly, so overriding only the port keeps the bridge and the spawned
    child in agreement (a bare localhost:8000 default would diverge).
  * supervisor_should_run opt-out gating (skip env + remote-URL guard).
  * ResourceGovernor.register_subprocess / unregister_subprocess and the
    managed_subprocesses key added to get_stats() (the monitor accounting hook).
"""

import importlib

import pytest

_sup = importlib.import_module(
    "integrations.agent_engine.hevolveai_supervisor")
_gov = importlib.import_module("core.resource_governor")


# -- L3: URL derives from PORT unless explicitly set -------------------

def test_api_url_follows_port_override(monkeypatch):
    monkeypatch.setenv("HEVOLVEAI_PORT", "9000")
    monkeypatch.delenv("HEVOLVEAI_API_URL", raising=False)
    assert _sup._hevolveai_port() == 9000
    assert _sup._hevolveai_api_url() == "http://localhost:9000"


def test_explicit_api_url_wins_over_port(monkeypatch):
    monkeypatch.setenv("HEVOLVEAI_PORT", "9000")
    monkeypatch.setenv("HEVOLVEAI_API_URL", "http://localhost:8000")
    # Explicit URL is authoritative even if it disagrees with the port.
    assert _sup._hevolveai_api_url() == "http://localhost:8000"


def test_default_url_when_nothing_set(monkeypatch):
    monkeypatch.delenv("HEVOLVEAI_PORT", raising=False)
    monkeypatch.delenv("HEVOLVEAI_API_URL", raising=False)
    assert _sup._hevolveai_api_url() == "http://localhost:8000"


def test_bad_port_falls_back_to_8000(monkeypatch):
    monkeypatch.setenv("HEVOLVEAI_PORT", "not-a-number")
    assert _sup._hevolveai_port() == 8000


# -- supervisor_should_run opt-out gating ------------------------------

def test_should_run_default_true(monkeypatch):
    monkeypatch.delenv("HEVOLVE_SKIP_HEVOLVEAI_SPAWN", raising=False)
    monkeypatch.delenv("HEVOLVEAI_API_URL", raising=False)
    assert _sup.supervisor_should_run() is True


def test_should_run_skip_env(monkeypatch):
    monkeypatch.setenv("HEVOLVE_SKIP_HEVOLVEAI_SPAWN", "1")
    assert _sup.supervisor_should_run() is False


def test_should_run_false_for_remote_url(monkeypatch):
    monkeypatch.delenv("HEVOLVE_SKIP_HEVOLVEAI_SPAWN", raising=False)
    monkeypatch.setenv("HEVOLVEAI_API_URL", "http://gpu-box.internal:8000")
    # We never spawn a remote target's server locally.
    assert _sup.supervisor_should_run() is False


def test_should_run_true_for_localhost_url(monkeypatch):
    monkeypatch.delenv("HEVOLVE_SKIP_HEVOLVEAI_SPAWN", raising=False)
    monkeypatch.setenv("HEVOLVEAI_API_URL", "http://127.0.0.1:8000")
    assert _sup.supervisor_should_run() is True


# -- ResourceGovernor subprocess accounting ----------------------------

def test_governor_register_unregister_roundtrip():
    g = _gov.ResourceGovernor()
    g.register_subprocess("hevolveai", 12345)
    assert g.get_stats()["managed_subprocesses"] == {"hevolveai": 12345}
    g.unregister_subprocess("hevolveai", 12345)
    assert g.get_stats()["managed_subprocesses"] == {}


def test_governor_register_rejects_bad_input():
    g = _gov.ResourceGovernor()
    g.register_subprocess("", 100)        # empty name -> ignored
    g.register_subprocess("x", 0)         # non-positive pid -> ignored
    g.register_subprocess("y", -1)        # negative pid -> ignored
    assert g.get_stats()["managed_subprocesses"] == {}


def test_governor_stale_unregister_does_not_clobber_restart():
    """After a supervisor restart re-registers a fresh PID, a late
    unregister carrying the OLD pid must NOT evict the new one."""
    g = _gov.ResourceGovernor()
    g.register_subprocess("hevolveai", 111)   # original
    g.register_subprocess("hevolveai", 222)   # restart -> new pid
    g.unregister_subprocess("hevolveai", 111)  # old exit hook fires late
    assert g.get_stats()["managed_subprocesses"] == {"hevolveai": 222}


def test_get_stats_still_has_baseline_keys():
    """Adding managed_subprocesses must not drop the pre-existing schema
    consumers rely on (mode / throttle / cpu_limit / gpu_allowed)."""
    g = _gov.ResourceGovernor()
    stats = g.get_stats()
    for key in ("mode", "throttle", "cpu_limit", "gpu_allowed",
                "managed_subprocesses"):
        assert key in stats
