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

from unittest.mock import MagicMock

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


# ── heartbeat POST (pooled_post primary + requests fallback) ─────────────────
def _fake_http_pool(monkeypatch, post_fn=None, get_fn=None):
    """Inject a fake core.http_pool exposing only the pooled_* callables the
    test needs.  Omitting one makes `from core.http_pool import <that>` raise
    ImportError, exercising the requests fallback for that verb."""
    import sys
    import types
    m = types.ModuleType("core.http_pool")
    if post_fn is not None:
        m.pooled_post = post_fn
    if get_fn is not None:
        m.pooled_get = get_fn
    monkeypatch.setitem(sys.modules, "core.http_pool", m)


def _fake_guardrails(monkeypatch, result, record=None):
    """Inject a fake security.hive_guardrails whose HiveCircuitBreaker.halt_network
    returns `result` (mirroring a good vs. forged signature verdict) and records
    the (reason, signature) it was called with.  The REAL breaker does the actual
    master-PUBLIC-key verification; here we mock the boundary to test the caller."""
    import sys
    import types
    m = types.ModuleType("security.hive_guardrails")

    class HiveCircuitBreaker:
        @staticmethod
        def halt_network(reason=None, signature=None):
            if record is not None:
                record.append((reason, signature))
            return result

    m.HiveCircuitBreaker = HiveCircuitBreaker
    monkeypatch.setitem(sys.modules, "security.hive_guardrails", m)


class TestPostHeartbeat:
    def test_no_url_returns_false(self):
        # unconfigured -> _url() is '' -> no POST attempted
        assert CentralOrchestratorClient()._post_heartbeat() is False

    def test_2xx_is_success_and_clears_error(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://c")
        _fake_http_pool(monkeypatch, lambda *a, **k: MagicMock(status_code=200))
        c = CentralOrchestratorClient()
        assert c._post_heartbeat() is True
        assert c._last_heartbeat_error is None
        assert c._last_heartbeat_ts > 0

    def test_non_2xx_records_http_error(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://c")
        _fake_http_pool(monkeypatch, lambda *a, **k: MagicMock(status_code=503))
        c = CentralOrchestratorClient()
        assert c._post_heartbeat() is False
        assert c._last_heartbeat_error == "HTTP 503"

    def test_none_response_records_error(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://c")
        _fake_http_pool(monkeypatch, lambda *a, **k: None)
        c = CentralOrchestratorClient()
        assert c._post_heartbeat() is False
        assert c._last_heartbeat_error == "no response"

    def test_exception_is_caught_and_recorded(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://c")

        def _boom(*a, **k):
            raise RuntimeError("net down")

        _fake_http_pool(monkeypatch, _boom)
        c = CentralOrchestratorClient()
        assert c._post_heartbeat() is False
        assert "net down" in (c._last_heartbeat_error or "")

    def test_importerror_falls_back_to_requests(self, monkeypatch):
        import sys
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://c")
        # No core.http_pool -> the `from core.http_pool import pooled_post`
        # raises ImportError -> the requests fallback path runs.
        monkeypatch.setitem(sys.modules, "core.http_pool", None)
        import requests
        monkeypatch.setattr(requests, "post",
                            lambda *a, **k: MagicMock(status_code=204))
        assert CentralOrchestratorClient()._post_heartbeat() is True


class TestPostHeartbeatRequestsFallback:
    def test_2xx_success(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post",
                            lambda *a, **k: MagicMock(status_code=200))
        c = CentralOrchestratorClient()
        assert c._post_heartbeat_requests("http://c/hb", {}) is True
        assert c._last_heartbeat_error is None

    def test_non_2xx_records_error(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post",
                            lambda *a, **k: MagicMock(status_code=500))
        c = CentralOrchestratorClient()
        assert c._post_heartbeat_requests("http://c/hb", {}) is False
        assert c._last_heartbeat_error == "HTTP 500"

    def test_exception_records_error(self, monkeypatch):
        import requests

        def _boom(*a, **k):
            raise requests.RequestException("x")

        monkeypatch.setattr(requests, "post", _boom)
        c = CentralOrchestratorClient()
        assert c._post_heartbeat_requests("http://c/hb", {}) is False
        assert "x" in (c._last_heartbeat_error or "")


# ── _check_halt (poll routing) ──────────────────────────────────────────────
def _halt_resp(status=200, body=None):
    r = MagicMock(status_code=status)
    if body is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = body
    return r


class TestCheckHalt:
    def test_no_url_is_a_noop(self):
        # unconfigured -> _url() '' -> no poll, no state touched
        c = CentralOrchestratorClient()
        assert c._check_halt() is None
        assert c._last_halt_poll_ts == 0.0
        assert c._last_halt_poll_error is None

    def test_404_means_no_halt_and_clears_error(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://c")
        _fake_http_pool(monkeypatch, get_fn=lambda *a, **k: _halt_resp(status=404))
        c = CentralOrchestratorClient()
        applied = []
        monkeypatch.setattr(c, "_apply_halt", lambda **k: applied.append(k))
        c._check_halt()
        assert c._last_halt_poll_error is None
        assert c._last_halt_poll_ts > 0
        assert applied == []                       # 404 never applies a halt

    def test_non_200_records_http_error(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://c")
        _fake_http_pool(monkeypatch, get_fn=lambda *a, **k: _halt_resp(status=503))
        c = CentralOrchestratorClient()
        c._check_halt()
        assert c._last_halt_poll_error == "HTTP 503"

    def test_none_response_records_error(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://c")
        _fake_http_pool(monkeypatch, get_fn=lambda *a, **k: None)
        c = CentralOrchestratorClient()
        c._check_halt()
        assert c._last_halt_poll_error == "no response"

    def test_invalid_json_records_error(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://c")
        _fake_http_pool(monkeypatch, get_fn=lambda *a, **k: _halt_resp(status=200))
        c = CentralOrchestratorClient()
        c._check_halt()
        assert c._last_halt_poll_error == "invalid JSON"

    def test_body_without_halt_flag_clears_error(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://c")
        _fake_http_pool(monkeypatch,
                        get_fn=lambda *a, **k: _halt_resp(body={"halt": False}))
        c = CentralOrchestratorClient()
        applied = []
        monkeypatch.setattr(c, "_apply_halt", lambda **k: applied.append(k))
        c._check_halt()
        assert c._last_halt_poll_error is None
        assert applied == []

    def test_halt_WITHOUT_signature_is_refused_and_never_applied(self, monkeypatch):
        # SECURITY: an unsigned halt must be logged+ignored, NOT trip the breaker.
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://c")
        _fake_http_pool(
            monkeypatch,
            get_fn=lambda *a, **k: _halt_resp(body={"halt": True, "reason": "x"}))
        c = CentralOrchestratorClient()
        applied = []
        monkeypatch.setattr(c, "_apply_halt", lambda **k: applied.append(k))
        c._check_halt()
        assert c._last_halt_poll_error == "halt without signature"
        assert applied == []                       # the critical invariant

    def test_signed_halt_is_routed_to_apply_with_reason_and_signature(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://c")
        _fake_http_pool(
            monkeypatch,
            get_fn=lambda *a, **k: _halt_resp(
                body={"halt": True, "reason": "central down", "signature": "SIG"}))
        c = CentralOrchestratorClient()
        applied = []
        monkeypatch.setattr(c, "_apply_halt", lambda **k: applied.append(k))
        c._check_halt()
        assert applied == [{"reason": "central down", "signature": "SIG"}]
        assert c._last_halt_poll_error is None

    def test_importerror_falls_back_to_requests(self, monkeypatch):
        import sys
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://c")
        monkeypatch.setitem(sys.modules, "core.http_pool", None)
        import requests
        monkeypatch.setattr(requests, "get",
                            lambda *a, **k: _halt_resp(status=404))
        c = CentralOrchestratorClient()
        c._check_halt()
        assert c._last_halt_poll_error is None      # 404 via the requests fallback

    def test_pool_exception_is_caught_and_recorded(self, monkeypatch):
        monkeypatch.setenv(ENV_CENTRAL_URL, "http://c")

        def _boom(*a, **k):
            raise RuntimeError("poll down")

        _fake_http_pool(monkeypatch, get_fn=_boom)
        c = CentralOrchestratorClient()
        c._check_halt()
        assert "poll down" in (c._last_halt_poll_error or "")


# ── _get_halt_requests (fallback GET) ───────────────────────────────────────
class TestGetHaltRequests:
    def test_returns_response_on_success(self, monkeypatch):
        import requests
        sentinel = _halt_resp(status=404)
        monkeypatch.setattr(requests, "get", lambda *a, **k: sentinel)
        assert CentralOrchestratorClient()._get_halt_requests("http://c/halt") is sentinel

    def test_exception_returns_none_and_records_error(self, monkeypatch):
        import requests

        def _boom(*a, **k):
            raise requests.RequestException("dns")

        monkeypatch.setattr(requests, "get", _boom)
        c = CentralOrchestratorClient()
        assert c._get_halt_requests("http://c/halt") is None
        assert "dns" in (c._last_halt_poll_error or "")


# ── _apply_halt (guardrail boundary; forged/unsigned MUST NOT apply) ─────────
class TestApplyHalt:
    def test_valid_signature_trips_breaker_and_marks_applied(self, monkeypatch):
        record = []
        _fake_guardrails(monkeypatch, result=True, record=record)
        c = CentralOrchestratorClient()
        c._apply_halt(reason="down", signature="GOODSIG")
        assert c._halt_applied is True
        # the breaker sees the reason namespaced + the raw signature to verify
        assert record == [("central:down", "GOODSIG")]

    def test_rejected_signature_does_NOT_mark_applied(self, monkeypatch):
        # SECURITY: breaker returns False (signature verification failed) ->
        # a forged halt must leave the hive un-halted.
        _fake_guardrails(monkeypatch, result=False)
        c = CentralOrchestratorClient()
        c._apply_halt(reason="forged", signature="BADSIG")
        assert c._halt_applied is False

    def test_guardrails_unavailable_is_swallowed_and_not_applied(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "security.hive_guardrails", None)
        c = CentralOrchestratorClient()
        c._apply_halt(reason="x", signature="s")   # ImportError path, no raise
        assert c._halt_applied is False

    def test_breaker_exception_is_swallowed_and_not_applied(self, monkeypatch):
        import sys
        import types
        m = types.ModuleType("security.hive_guardrails")

        class HiveCircuitBreaker:
            @staticmethod
            def halt_network(reason=None, signature=None):
                raise RuntimeError("breaker boom")

        m.HiveCircuitBreaker = HiveCircuitBreaker
        monkeypatch.setitem(sys.modules, "security.hive_guardrails", m)
        c = CentralOrchestratorClient()
        c._apply_halt(reason="x", signature="s")   # Exception path, no raise
        assert c._halt_applied is False


# ── module-level delegation helpers ─────────────────────────────────────────
class TestModuleHelpers:
    def test_start_delegates_and_is_false_when_unconfigured(self):
        assert coc.start() is False               # clean env -> singleton unconfigured

    def test_stop_is_a_safe_noop(self):
        assert coc.stop() is None                 # never raises even if not running

    def test_get_status_delegates_to_singleton(self):
        st = coc.get_status()
        assert isinstance(st, dict) and "configured" in st


# ── _loop (bounded drive; backoff + never-die guarantee) ────────────────────
class _StopAfter:
    """Drop-in for the threading.Event stop flag: lets _loop run exactly `n`
    iterations, then reports set.  wait() records but never really sleeps, so
    the loop test is deterministic and fast."""

    def __init__(self, n):
        self._left = n
        self.waits = 0

    def is_set(self):
        if self._left <= 0:
            return True
        self._left -= 1
        return False

    def wait(self, timeout=None):
        self.waits += 1
        return True


class TestLoop:
    def _drive(self, monkeypatch, hb_result, iterations=1):
        c = CentralOrchestratorClient()
        c._stop_event = _StopAfter(iterations)
        hb, halt = [], []
        monkeypatch.setattr(c, "_post_heartbeat",
                            lambda: (hb.append(1), hb_result)[1])
        monkeypatch.setattr(c, "_check_halt", lambda: halt.append(1))
        c._loop()
        return c, hb, halt

    def test_one_iteration_posts_heartbeat_and_polls_halt(self, monkeypatch):
        c, hb, halt = self._drive(monkeypatch, hb_result=True)
        assert hb == [1] and halt == [1]     # both fire on the first tick
        assert c._stop_event.waits == 1      # slept once, then stopped

    def test_success_resets_backoff(self, monkeypatch):
        c = CentralOrchestratorClient()
        c._stop_event = _StopAfter(1)
        c._backoff = 80                      # a non-initial value
        monkeypatch.setattr(c, "_post_heartbeat", lambda: True)
        monkeypatch.setattr(c, "_check_halt", lambda: None)
        c._loop()
        assert c._backoff == coc._INITIAL_BACKOFF   # reset on success

    def test_failure_doubles_backoff(self, monkeypatch):
        c = CentralOrchestratorClient()
        c._stop_event = _StopAfter(1)
        c._backoff = coc._INITIAL_BACKOFF
        monkeypatch.setattr(c, "_post_heartbeat", lambda: False)
        monkeypatch.setattr(c, "_check_halt", lambda: None)
        c._loop()
        assert c._backoff == coc._INITIAL_BACKOFF * 2

    def test_failure_backoff_is_capped_at_max(self, monkeypatch):
        c = CentralOrchestratorClient()
        c._stop_event = _StopAfter(1)
        c._backoff = coc._MAX_BACKOFF - 1    # doubling would overshoot
        monkeypatch.setattr(c, "_post_heartbeat", lambda: False)
        monkeypatch.setattr(c, "_check_halt", lambda: None)
        c._loop()
        assert c._backoff == coc._MAX_BACKOFF

    def test_exception_in_iteration_never_escapes_the_loop(self, monkeypatch):
        c = CentralOrchestratorClient()
        c._stop_event = _StopAfter(1)

        def _boom():
            raise RuntimeError("hb down")

        monkeypatch.setattr(c, "_post_heartbeat", _boom)
        monkeypatch.setattr(c, "_check_halt", lambda: None)
        c._loop()                            # must return, not raise
