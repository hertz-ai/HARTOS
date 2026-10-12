"""The LLM gateway Kong onboarding (integrations.gateway.kong_onboard
.onboard_llm_gateway): what it asks Kong 2.8.5 to create, asserted on the
admin-API calls, no network.

Owner policy 2026-10-12: a revenue conversation is never throttled; a
person's turn and the daemon's turn have their own request caps; a caller
that stamps no lane is treated as a user.  Kong 2.8.5 community has no AI
plugins (measured), so caps are requests per window matched on the
X-HARTOS-Lane and X-HARTOS-Model-Family headers HARTOS stamps; the family
comes from the ONE rule the dollar meter prices with, so Kong keeps no list
of Claude model names.  Revenue-lane Claude has no Kong fuse: its cap is the
HARTOS day budget (budget_gate); the Claude route here fuses only the user
and daemon Claude calls policy forbids anyway.
"""
import json
from unittest.mock import MagicMock

import pytest
import requests

from integrations.gateway import kong_onboard as ko

KONG = "http://192.168.0.9:8101"


def _resp(status=200, body=None):
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.json.return_value = body or {}
    r.text = json.dumps(body or {})
    r.raise_for_status = MagicMock()
    return r


def _session():
    s = MagicMock(spec=requests.Session)
    s.put.return_value = _resp(201, {"id": "x"})
    s.get.return_value = _resp(200, {"data": []})
    s.post.return_value = _resp(201, {"id": "pl"})
    s.patch.return_value = _resp(200, {"id": "pl"})
    return s


def _puts(session):
    return {c[0][0].replace(KONG, ""): c[1]["json"] for c in session.put.call_args_list}


def _posts(session):
    """collection path -> list of plugin payloads POSTed there."""
    out = {}
    for c in session.post.call_args_list:
        out.setdefault(c[0][0].replace(KONG, ""), []).append(c[1]["json"])
    return out


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ("LLM_CLAUDE_REQUESTS_PER_DAY", "LLM_CLAUDE_REQUESTS_PER_HOUR",
              "LLM_USER_PER_DAY", "LLM_DAEMON_PER_DAY", "LLM_USER_PER_MINUTE",
              "LLM_DAEMON_PER_MINUTE", "LLM_USER_PER_HOUR", "LLM_DAEMON_PER_HOUR"):
        monkeypatch.delenv(v, raising=False)


def test_service_forwards_to_gpt3_with_long_timeouts_and_no_retries():
    s = _session()
    assert ko.onboard_llm_gateway(s, KONG, "http://172.17.0.1:5459") is True
    svc = _puts(s)["/services/llm-gateway"]
    assert svc["url"] == "http://172.17.0.1:5459"
    assert svc["read_timeout"] == 180000 and svc["write_timeout"] == 180000
    assert svc["retries"] == 0                      # an LLM call is never replayed


def test_service_plugins_bound_body_size_and_callers():
    s = _session()
    ko.onboard_llm_gateway(s, KONG)
    plugins = {p["name"]: p["config"] for p in _posts(s)["/services/llm-gateway/plugins"]}
    assert plugins["request-size-limiting"]["allowed_payload_size"] == 20
    assert plugins["ip-restriction"]["allow"] == ["127.0.0.1", "172.17.0.0/16"]
    assert "key-auth" not in plugins


def test_five_lane_routes_strip_the_prefix_and_match_the_headers():
    s = _session()
    ko.onboard_llm_gateway(s, KONG)
    puts = _puts(s)
    routes = {k.rsplit("/", 1)[1]: v for k, v in puts.items() if "/routes/" in k}
    assert set(routes) == {"llm-revenue", "llm-claude", "llm-user", "llm-daemon", "llm-default"}
    for r in routes.values():
        assert r["paths"] == ["/llm"] and r["strip_path"] is True
        assert r["protocols"] == ["http", "https"]
    assert routes["llm-revenue"]["headers"] == {"X-HARTOS-Lane": ["revenue"]}
    assert routes["llm-user"]["headers"] == {"X-HARTOS-Lane": ["user"]}
    assert routes["llm-daemon"]["headers"] == {"X-HARTOS-Lane": ["daemon"]}
    assert "headers" not in routes["llm-default"]
    claude = routes["llm-claude"]["headers"]
    # A revenue call to Claude does not match this route (its lane is not
    # listed), so only user and daemon Claude calls are capped.
    assert claude["X-HARTOS-Lane"] == ["user", "daemon"]
    # Matched on the family HARTOS stamps, never on a list of model names: a
    # model the list would have missed (claude-sonnet-4-6 is already in the
    # repo, deploy/bootstrap_hive_economy.py) still trips the fuse.
    from core.autogen_config import model_family
    assert claude["X-HARTOS-Model-Family"] == [model_family("anthropic/claude-sonnet-4-6")] == ["claude"]
    assert "X-HARTOS-Model" not in claude


def test_revenue_route_has_no_rate_limit_and_the_others_do():
    s = _session()
    ko.onboard_llm_gateway(s, KONG)
    posts = _posts(s)
    assert "/routes/llm-revenue/plugins" not in posts          # never throttled
    for name in ("llm-claude", "llm-user", "llm-daemon", "llm-default"):
        (plugin,) = posts[f"/routes/{name}/plugins"]
        assert plugin["name"] == "rate-limiting"
        assert plugin["config"]["limit_by"] == "ip"
        assert plugin["config"]["policy"] == "local"
        assert plugin["config"]["fault_tolerant"] is True
    claude = posts["/routes/llm-claude/plugins"][0]["config"]
    assert claude["day"] == 60 and claude["hour"] == 20          # the $6/day policy, in requests
    user = posts["/routes/llm-user/plugins"][0]["config"]
    daemon = posts["/routes/llm-daemon/plugins"][0]["config"]
    assert daemon["day"] < user["day"] and daemon["hour"] < user["hour"]
    assert posts["/routes/llm-default/plugins"][0]["config"]["day"] == user["day"]


def test_caps_follow_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_CLAUDE_REQUESTS_PER_DAY", "25")
    monkeypatch.setenv("LLM_DAEMON_PER_DAY", "100")
    s = _session()
    ko.onboard_llm_gateway(s, KONG)
    posts = _posts(s)
    assert posts["/routes/llm-claude/plugins"][0]["config"]["day"] == 25
    assert posts["/routes/llm-daemon/plugins"][0]["config"]["day"] == 100


def test_a_cap_that_could_not_be_looked_up_or_updated_is_not_reported_applied():
    """A failed lookup used to fall through to a POST that 409'd as "already
    exists (no change)" while the cap on the box stayed the OLD one (review
    of 00d29f073).  The fall-through stays (a first-time create survives an
    admin-API hiccup, TestEnablePluginResilience), but a 409 after a failed
    lookup is reported NOT applied and the gateway is not called ready."""
    s = _session()

    def get(url):
        if url.endswith("/routes/llm-claude/plugins"):
            return _resp(500)
        return _resp(200, {"data": []})

    def post(url, json=None):
        if url.endswith("/routes/llm-claude/plugins"):
            return _resp(409, {"message": "unique constraint violation"})
        return _resp(201, {"id": "pl"})
    s.get.side_effect = get
    s.post.side_effect = post
    assert ko.onboard_llm_gateway(s, KONG) is False
    assert "/routes/llm-user/plugins" in _posts(s)          # the others were applied


def test_an_update_kong_refuses_falls_through_and_is_applied_only_if_created():
    existing = {"data": [{"id": "rl-1", "name": "rate-limiting"}]}
    s = _session()
    s.get.return_value = _resp(200, existing)
    s.patch.return_value = _resp(400, {"message": "schema violation"})
    s.post.return_value = _resp(409, {"message": "unique constraint violation"})
    assert ko.onboard_llm_gateway(s, KONG) is False          # exists, not updated
    s2 = _session()
    s2.get.return_value = _resp(200, existing)
    s2.patch.return_value = _resp(400, {"message": "schema violation"})
    assert ko.onboard_llm_gateway(s2, KONG) is True          # the create (201) applied it


def test_existing_plugin_is_patched_not_duplicated():
    s = _session()
    s.get.return_value = _resp(200, {"data": [{"id": "rl-1", "name": "rate-limiting"}]})
    ko.onboard_llm_gateway(s, KONG)
    patched = [c[0][0] for c in s.patch.call_args_list]
    assert patched and all(u == f"{KONG}/plugins/rl-1" for u in patched)
    posted = [p["name"] for ps in _posts(s).values() for p in ps]
    assert "rate-limiting" not in posted


def test_returns_false_when_the_service_does_not_read_back():
    s = _session()

    def get(url):
        if url.endswith("/services/llm-gateway"):
            return _resp(404)
        return _resp(200, {"data": []})
    s.get.side_effect = get
    assert ko.onboard_llm_gateway(s, KONG) is False


def test_cli_llm_flag_runs_only_the_llm_gateway(monkeypatch):
    seen = {}

    def fake(session, kong_url, upstream):
        seen["args"] = (kong_url, upstream)
        return True
    monkeypatch.setattr(ko, "onboard_llm_gateway", fake)
    monkeypatch.setattr(ko, "onboard", lambda **kw: pytest.fail("the SDK onboarding must not run"))
    assert ko.main(["--llm", "--kong-url", KONG, "--llm-upstream", "http://10.0.0.5:5459"]) == 0
    assert seen["args"] == (KONG, "http://10.0.0.5:5459")


def test_mindstory_enable_plugin_still_targets_its_own_service():
    """The upsert was folded into one helper; the Mindstory block keeps its scope."""
    s = _session()
    ko.enable_plugin(s, KONG, ko.PLUGINS[0])
    assert s.get.call_args[0][0] == f"{KONG}/services/{ko.SERVICE_NAME}/plugins"
    assert s.post.call_args[0][0] == f"{KONG}/services/{ko.SERVICE_NAME}/plugins"
