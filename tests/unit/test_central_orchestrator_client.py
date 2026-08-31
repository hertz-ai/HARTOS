"""Behavioural tests for core.central_orchestrator_client — the background
heartbeat/halt client to hevolve.ai central.

Covers the deterministic surface (env parsing, the configured/tier start gates,
stop, status, the PRIVACY-conscious heartbeat payload, URL construction, the
node-id fallback, and the module singleton). The network loop + the master-key
halt-apply are integration paths left to the VM suite; here we pin the pure logic
+ the guards that decide whether the client even runs. 0% covered before this
file. Real functions, env via monkeypatch, no source-substring checks.

    python -m pytest tests/unit/test_central_orchestrator_client.py -q --noconftest
"""
from __future__ import annotations

import pytest

from core import central_orchestrator_client as coc
from core.central_orchestrator_client import (
    CentralOrchestratorClient, ENV_CENTRAL_URL, ENV_NODE_TIER, ENV_NODE_ID,
    ENV_HEARTBEAT_INTERVAL, ENV_HEARTBEAT_PATH, _int_env,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (ENV_CENTRAL_URL, ENV_NODE_TIER, ENV_NODE_ID,
              ENV_HEARTBEAT_INTERVAL, ENV_HEARTBEAT_PATH):
        monkeypatch.delenv(k, raising=False)
    yield


# ── _int_env ────────────────────────────────────────────────────────────────
class TestIntEnv:
    def test_parses_a_valid_int(self, monkeypatch):
        monkeypatch.setenv(ENV_HEARTBEAT_INTERVAL, "45")
        assert _int_env(ENV_HEARTBEAT_INTERVAL, 60) == 45

    def test_unset_returns_default(self):
        assert _int_env(ENV_HEARTBEAT_INTERVAL, 60) == 60

    @pytest.mark.parametrize("bad", ["", "  ", "abc", "1.5"])
    def test_blank_or_nonint_returns_default(self, monkeypatch, bad):
        monkeypatch.setenv(ENV_HEARTBEAT_INTERVAL, bad)
        assert _int_env(ENV_HEARTBEAT_INTERVAL, 60) == 60


# ── is_configured ───────────────────────────────────────────────────────────
class TestIsConfigured:
    def test_true_when_central_url_set(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://central")
        assert CentralOrchestratorClient().is_configured() is True

    def test_false_when_unset(self):
        assert CentralOrchestratorClient().is_configured() is False

    def test_false_when_only_whitespace(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "   ")
        assert CentralOrchestratorClient().is_configured() is False


# ── start / stop gates ──────────────────────────────────────────────────────
class TestStartStop:
    def test_start_is_noop_when_unconfigured(self):
        assert CentralOrchestratorClient().start() is False

    def test_start_is_noop_on_central_tier(self, monkeypatch):
        # A central node must NOT heartbeat itself, even when a URL is set.
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://central")
        monkeypatch.setenv(ENV_NODE_TIER, "CENTRAL")   # case-insensitive
        assert CentralOrchestratorClient().start() is False

    def test_start_spawns_loop_when_configured_and_stops_cleanly(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://central")
        monkeypatch.setenv(ENV_NODE_TIER, "flat")
        c = CentralOrchestratorClient()
        # Replace the network loop with a stop-event wait so no real HTTP fires.
        monkeypatch.setattr(c, "_loop", lambda: c._stop_event.wait())
        assert c.start() is True
        assert c._running is True
        assert c._thread is not None and c._thread.is_alive()
        c.stop()
        assert c._running is False
        assert c._stop_event.is_set()
        c._thread.join(timeout=5)
        assert not c._thread.is_alive()

    def test_start_is_idempotent_while_running(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://central")
        c = CentralOrchestratorClient()
        monkeypatch.setattr(c, "_loop", lambda: c._stop_event.wait())
        assert c.start() is True
        assert c.start() is False  # already running — no second thread
        c.stop()
        c._thread.join(timeout=5)


# ── get_status ──────────────────────────────────────────────────────────────
class TestGetStatus:
    def test_status_reports_configuration_and_defaults(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://central")
        st = CentralOrchestratorClient().get_status()
        assert st["configured"] is True
        assert st["central_url"] == "http://central"
        assert st["running"] is False        # not started
        assert st["halt_applied"] is False
        for k in ("last_heartbeat_ts", "last_heartbeat_error",
                  "last_halt_poll_ts", "last_halt_poll_error"):
            assert k in st


# ── _build_heartbeat_payload (privacy + defaults) ───────────────────────────
class TestHeartbeatPayload:
    _ALLOWED = {"node_id", "node_tier", "timestamp", "version",
                "guardrail_hash", "halted", "benchmark_best", "world_model"}

    def test_mandatory_fields_and_defaults(self, monkeypatch):
        c = CentralOrchestratorClient()
        p = c._build_heartbeat_payload()
        assert p["version"] == 1
        assert isinstance(p["timestamp"], float)
        assert p["node_tier"] == "flat"                 # default when unset
        assert isinstance(p["node_id"], str) and p["node_id"]

    def test_env_overrides_node_id_and_tier(self, monkeypatch):
        monkeypatch.setenv(ENV_NODE_ID, "node-XYZ")
        monkeypatch.setenv(ENV_NODE_TIER, "regional")
        p = CentralOrchestratorClient()._build_heartbeat_payload()
        assert p["node_id"] == "node-XYZ"
        assert p["node_tier"] == "regional"

    def test_payload_carries_no_pii_only_allowed_keys(self):
        # The privacy contract: central sees only node identity + guardrail hash
        # + small summaries, never raw user data.
        p = CentralOrchestratorClient()._build_heartbeat_payload()
        assert set(p).issubset(self._ALLOWED), (
            f"heartbeat leaked unexpected keys: {set(p) - self._ALLOWED}")


# ── _url construction ───────────────────────────────────────────────────────
class TestUrl:
    def test_empty_base_yields_empty(self):
        assert CentralOrchestratorClient()._url(ENV_HEARTBEAT_PATH, "/hb") == ""

    def test_base_plus_default_path(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://c")
        assert CentralOrchestratorClient()._url(
            ENV_HEARTBEAT_PATH, "/heartbeat") == "http://c/heartbeat"

    def test_trailing_slash_on_base_is_normalised(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://c/")
        assert CentralOrchestratorClient()._url(
            ENV_HEARTBEAT_PATH, "/heartbeat") == "http://c/heartbeat"

    def test_custom_path_env_without_leading_slash_is_fixed(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://c")
        monkeypatch.setenv(ENV_HEARTBEAT_PATH, "beat")
        assert CentralOrchestratorClient()._url(
            ENV_HEARTBEAT_PATH, "/heartbeat") == "http://c/beat"


# ── fallback node id + singleton ────────────────────────────────────────────
class TestMisc:
    def test_fallback_node_id_is_a_nonempty_string(self):
        nid = coc._fallback_node_id()
        assert isinstance(nid, str) and nid  # a hash prefix or 'unknown-node'

    def test_get_client_is_a_singleton(self):
        assert coc.get_client() is coc.get_client()
