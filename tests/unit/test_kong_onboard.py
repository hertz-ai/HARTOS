"""
Tests for integrations.gateway.kong_onboard

All HTTP calls are mocked — no real Kong or network access required.
Run with:  pytest tests/unit/test_kong_onboard.py -v --noconftest
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch, call

import pytest
import requests

from integrations.gateway.kong_onboard import (
    SERVICE_NAME,
    ROUTE_NAME,
    ROUTE_PATHS,
    PLUGINS,
    DEFAULT_KONG_ADMIN_URL,
    DEFAULT_UPSTREAM_URL,
    GUEST_SERVICE_NAME,
    GUEST_ROUTE_NAME,
    GUEST_ROUTE_PATHS,
    GUEST_PLUGINS,
    create_service,
    create_route,
    enable_plugin,
    enable_plugins,
    onboard_guest_widget,
    _put_or_post,
    verify,
    onboard,
    build_parser,
    main,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(status_code: int = 200, json_data: dict | None = None, text: str = ""):
    """Return a ``MagicMock`` that quacks like ``requests.Response``."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text or json.dumps(json_data or {})
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(
            response=resp,
        )
    return resp


def _session() -> MagicMock:
    return MagicMock(spec=requests.Session)


KONG = DEFAULT_KONG_ADMIN_URL
UPSTREAM = DEFAULT_UPSTREAM_URL


# ---------------------------------------------------------------------------
# Service creation
# ---------------------------------------------------------------------------

class TestCreateService:
    def test_service_created(self):
        """PUT returns 201 → service created."""
        session = _session()
        session.put.return_value = _mock_response(201, {"id": "svc-1", "name": SERVICE_NAME})

        result = create_service(session, KONG, UPSTREAM)

        session.put.assert_called_once()
        url_arg = session.put.call_args[0][0]
        assert SERVICE_NAME in url_arg
        assert result["id"] == "svc-1"

    def test_service_updated(self):
        """PUT returns 200 → service updated (idempotent)."""
        session = _session()
        session.put.return_value = _mock_response(200, {"id": "svc-1", "name": SERVICE_NAME})

        result = create_service(session, KONG, UPSTREAM)

        assert result["name"] == SERVICE_NAME

    def test_service_already_exists_conflict(self):
        """PUT returns 409 → treated as success (already exists)."""
        session = _session()
        session.put.return_value = _mock_response(409, {"name": SERVICE_NAME})

        result = create_service(session, KONG, UPSTREAM)

        assert result.get("name") == SERVICE_NAME

    def test_service_post_fallback(self):
        """PUT returns 404 → falls back to POST to collection URL."""
        session = _session()
        session.put.return_value = _mock_response(404)
        session.post.return_value = _mock_response(201, {"id": "svc-2", "name": SERVICE_NAME})

        result = create_service(session, KONG, UPSTREAM)

        session.post.assert_called_once()
        assert result["id"] == "svc-2"


# ---------------------------------------------------------------------------
# Route creation
# ---------------------------------------------------------------------------

class TestCreateRoute:
    def test_route_created(self):
        """Route is created on first run."""
        session = _session()
        session.put.return_value = _mock_response(201, {"id": "rt-1", "name": ROUTE_NAME})

        result = create_route(session, KONG)

        url_arg = session.put.call_args[0][0]
        assert ROUTE_NAME in url_arg
        assert SERVICE_NAME in url_arg
        payload = session.put.call_args[1]["json"]
        assert set(payload["paths"]) == set(ROUTE_PATHS)
        assert result["id"] == "rt-1"

    def test_route_updated(self):
        """Route PUT returns 200 → updated."""
        session = _session()
        session.put.return_value = _mock_response(200, {"id": "rt-1", "name": ROUTE_NAME})

        result = create_route(session, KONG)
        assert result["name"] == ROUTE_NAME


# ---------------------------------------------------------------------------
# Plugin enabling
# ---------------------------------------------------------------------------

class TestEnablePlugin:
    @pytest.mark.parametrize("plugin_cfg", PLUGINS, ids=[p["name"] for p in PLUGINS])
    def test_plugin_created(self, plugin_cfg):
        """Each plugin type can be created via POST."""
        session = _session()
        session.get.return_value = _mock_response(200, {"data": []})
        session.post.return_value = _mock_response(201, {"id": "pl-1", "name": plugin_cfg["name"]})

        result = enable_plugin(session, KONG, plugin_cfg)

        assert result["name"] == plugin_cfg["name"]

    def test_plugin_updated_when_exists(self):
        """If the plugin already exists, it is PATCHed instead."""
        session = _session()
        existing_plugin = {"id": "pl-existing", "name": "key-auth"}
        session.get.return_value = _mock_response(200, {"data": [existing_plugin]})
        session.patch.return_value = _mock_response(200, {"id": "pl-existing", "name": "key-auth"})

        result = enable_plugin(session, KONG, PLUGINS[0])

        session.patch.assert_called_once()
        assert result["id"] == "pl-existing"

    def test_plugin_conflict_treated_as_success(self):
        """POST returns 409 → plugin already exists, no error."""
        session = _session()
        session.get.return_value = _mock_response(200, {"data": []})
        session.post.return_value = _mock_response(409, {"name": "rate-limiting"})

        result = enable_plugin(session, KONG, PLUGINS[1])

        assert result.get("name") == "rate-limiting"


class TestEnablePlugins:
    def test_all_plugins_enabled(self):
        """enable_plugins() calls enable_plugin() for every configured plugin."""
        session = _session()
        session.get.return_value = _mock_response(200, {"data": []})
        session.post.return_value = _mock_response(201, {"id": "pl", "name": "x"})

        results = enable_plugins(session, KONG)

        assert len(results) == len(PLUGINS)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

class TestVerify:
    def test_verify_success(self):
        session = _session()
        session.get.return_value = _mock_response(200, {"id": "svc-1", "host": "localhost"})

        assert verify(session, KONG) is True

    def test_verify_failure_http(self):
        session = _session()
        session.get.return_value = _mock_response(404)

        assert verify(session, KONG) is False

    def test_verify_failure_connection(self):
        session = _session()
        session.get.side_effect = requests.ConnectionError("refused")

        assert verify(session, KONG) is False


# ---------------------------------------------------------------------------
# Idempotency — full onboard twice
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_onboard_twice_no_error(self):
        """Calling onboard() twice succeeds both times."""
        session = _session()
        # Service + route PUT → 200 (update)
        session.put.return_value = _mock_response(200, {"id": "svc-1", "name": SERVICE_NAME})
        # Plugin listing + creation
        session.get.return_value = _mock_response(200, {"data": [], "id": "svc-1", "host": "localhost"})
        session.post.return_value = _mock_response(201, {"id": "pl", "name": "x"})

        assert onboard(KONG, UPSTREAM, session=session) is True
        assert onboard(KONG, UPSTREAM, session=session) is True


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_kong_unreachable(self):
        """onboard() returns False when Kong is not reachable."""
        session = _session()
        session.put.side_effect = requests.ConnectionError("Connection refused")

        assert onboard(KONG, UPSTREAM, session=session) is False

    def test_kong_http_error(self):
        """onboard() returns False on unexpected HTTP errors."""
        session = _session()
        bad = _mock_response(500)
        bad.raise_for_status.side_effect = requests.HTTPError(response=bad)
        # PUT for service creation returns 500 (not 404/409 — so raise)
        session.put.return_value = bad

        assert onboard(KONG, UPSTREAM, session=session) is False


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

class TestCLI:
    def test_default_args(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.kong_url == DEFAULT_KONG_ADMIN_URL
        assert args.upstream == DEFAULT_UPSTREAM_URL

    def test_custom_args(self):
        parser = build_parser()
        args = parser.parse_args([
            "--kong-url", "http://kong:8001",
            "--upstream", "http://ai:8000",
        ])
        assert args.kong_url == "http://kong:8001"
        assert args.upstream == "http://ai:8000"

    def test_main_returns_zero_on_success(self):
        with patch("integrations.gateway.kong_onboard.onboard", return_value=True):
            assert main([]) == 0

    def test_main_returns_one_on_failure(self):
        with patch("integrations.gateway.kong_onboard.onboard", return_value=False):
            assert main([]) == 1


# ---------------------------------------------------------------------------
# Payload capture helpers — assert on the config actually SENT to Kong.
#
# These are behavioural: they run the REAL onboarding functions against a
# mocked requests.Session (the only boundary) and inspect the JSON bodies the
# code pushes to the Admin API.  They deliberately do NOT read the module-level
# PLUGINS / GUEST_PLUGINS constants, so a regression that mutates a constant OR
# rewires which config reaches Kong is caught either way.
# ---------------------------------------------------------------------------

def _writes(session: MagicMock, method: str):
    """Return [(url, json_body), ...] for a mocked session verb (put/post/patch)."""
    out = []
    for c in getattr(session, method).call_args_list:
        url = c.args[0] if c.args else c.kwargs.get("url", "")
        out.append((url, c.kwargs.get("json")))
    return out


def _run_enable_plugins():
    """Run the real enable_plugins() on a fresh service and capture POSTed configs."""
    session = _session()
    session.get.return_value = _mock_response(200, {"data": []})
    session.post.return_value = _mock_response(201, {"id": "pl", "name": "x"})
    enable_plugins(session, KONG)
    by_name = {}
    for url, body in _writes(session, "post"):
        if body and "config" in body:
            by_name[body["name"]] = body["config"]
    return by_name


def _run_guest_widget():
    """Run the real onboard_guest_widget() and capture what it pushes to Kong."""
    session = _session()
    session.get.return_value = _mock_response(200, {"data": []})
    session.put.return_value = _mock_response(201, {"id": "svc"})
    session.post.return_value = _mock_response(201, {"id": "pl", "name": "x"})
    session.patch.return_value = _mock_response(200, {"id": "pl"})
    ok = onboard_guest_widget(session, KONG, UPSTREAM)
    guest_plugin_cfg = {}
    for url, body in _writes(session, "post"):
        if body and GUEST_SERVICE_NAME in url and "config" in body:
            guest_plugin_cfg[body["name"]] = body["config"]
    return session, ok, guest_plugin_cfg


# ---------------------------------------------------------------------------
# Main (paid) service — security-critical plugin config
# ---------------------------------------------------------------------------

class TestMainPluginSecurityConfig:
    """The paid completions service must ship key-auth that hides credentials
    from the upstream, keys only in the header, and never in the query string."""

    def test_key_auth_hides_credentials_from_upstream(self):
        cfg = _run_enable_plugins()["key-auth"]
        # hide_credentials=True strips the API key before proxying upstream —
        # flipping it to False leaks the caller's key to HevolveAI logs.
        assert cfg["hide_credentials"] is True
        assert cfg["key_in_header"] is True
        assert cfg["key_in_query"] is False
        assert cfg["key_names"] == ["Authorization", "apikey"]

    def test_main_rate_limit_is_fail_open_for_authed_traffic(self):
        cfg = _run_enable_plugins()["rate-limiting"]
        assert cfg["minute"] == 60
        assert cfg["hour"] == 1000
        assert cfg["day"] == 10000
        # Authenticated, paying traffic: a broken redis counter must not lock
        # out customers, so the main tier is intentionally fail-OPEN.
        assert cfg["fault_tolerant"] is True

    def test_main_request_size_limit_present(self):
        cfg = _run_enable_plugins()["request-size-limiting"]
        assert cfg["allowed_payload_size"] == 10

    def test_key_auth_actually_reaches_kong(self):
        # Regression guard: key-auth must be among the plugins pushed, not just
        # present in the constant.  If key-auth stops being onboarded, the paid
        # endpoint becomes anonymous.
        assert "key-auth" in _run_enable_plugins()


# ---------------------------------------------------------------------------
# Guest widget — public, no-key-auth, must be locked down
# ---------------------------------------------------------------------------

class TestGuestWidgetSecurityConfig:
    def test_onboard_returns_true(self):
        _session_, ok, _cfg = _run_guest_widget()
        assert ok is True

    def test_guest_rate_limit_is_fail_closed(self):
        """The public token-minting endpoint MUST fail closed — a broken counter
        must deny, never fall open to unlimited guest-token farming."""
        _s, _ok, cfg = _run_guest_widget()
        rl = cfg["rate-limiting"]
        assert rl["fault_tolerant"] is False   # fail CLOSED — the whole point
        assert rl["minute"] == 5
        assert rl["hour"] == 30
        assert rl["policy"] == "local"

    def test_guest_cors_is_origin_locked_not_wildcard(self):
        """Guest CORS must never widen to '*': the widget mints tokens, so an
        open origin lets any site drive the flow."""
        _s, _ok, cfg = _run_guest_widget()
        cors = cfg["cors"]
        assert "*" not in cors["origins"]
        assert set(cors["origins"]) == {
            "https://docs.hevolve.ai",
            "https://hevolve.ai",
            "http://localhost:8000",
        }
        # No cross-site credentials on a public, cookie-less widget.
        assert cors["credentials"] is False
        # Minimal method surface — only what the register flow needs.
        assert set(cors["methods"]) == {"POST", "OPTIONS"}

    def test_guest_has_no_key_auth(self):
        """The widget is intentionally public — no key-auth plugin — protection
        is rate-limit + CORS + short-lived server-side tokens."""
        _s, _ok, cfg = _run_guest_widget()
        assert "key-auth" not in cfg

    def test_guest_has_ip_restriction_and_tight_size_limit(self):
        _s, _ok, cfg = _run_guest_widget()
        assert "ip-restriction" in cfg
        # deny-list mode: empty allow == allow all, deny populated by abuse detection
        assert cfg["ip-restriction"]["allow"] == []
        assert cfg["request-size-limiting"]["allowed_payload_size"] == 1

    def test_guest_service_and_route_payload(self):
        session = _session()
        session.get.return_value = _mock_response(200, {"data": []})
        session.put.return_value = _mock_response(201, {"id": "svc"})
        session.post.return_value = _mock_response(201, {"id": "pl", "name": "x"})
        session.patch.return_value = _mock_response(200, {"id": "pl"})

        onboard_guest_widget(session, KONG, UPSTREAM)

        put_bodies = {b["name"]: b for _u, b in _writes(session, "put") if b}
        assert GUEST_SERVICE_NAME in put_bodies
        assert GUEST_ROUTE_NAME in put_bodies
        assert set(put_bodies[GUEST_ROUTE_NAME]["paths"]) == set(GUEST_ROUTE_PATHS)
        # guest register is cheap → single retry, http allowed for localhost dev
        assert put_bodies[GUEST_SERVICE_NAME]["retries"] == 1
        assert "http" in put_bodies[GUEST_ROUTE_NAME]["protocols"]


# ---------------------------------------------------------------------------
# BUG GUARD: guest onboarding must NOT touch the paid main service.
#
# enable_plugin() is hard-scoped to SERVICE_NAME (hevolve-completions).  Calling
# it from onboard_guest_widget() pushed the guest plugin configs (5/min,
# fault_tolerant=False, locked CORS, ip-restriction) onto the MAIN service —
# PATCHing the paid API's rate-limiting down to 5 req/min fail-closed and
# overwriting its wildcard CORS.  Guest onboarding must only ever write to the
# guest service.
# ---------------------------------------------------------------------------

class TestGuestWidgetDoesNotPolluteMainService:
    def test_guest_onboarding_writes_only_to_guest_service(self):
        session = _session()
        session.get.return_value = _mock_response(200, {"data": []})
        session.put.return_value = _mock_response(201, {"id": "svc"})
        session.post.return_value = _mock_response(201, {"id": "pl", "name": "x"})
        session.patch.return_value = _mock_response(200, {"id": "pl"})

        onboard_guest_widget(session, KONG, UPSTREAM)

        for method in ("put", "post", "patch"):
            for url, _body in _writes(session, method):
                assert SERVICE_NAME not in url, (
                    f"guest onboarding wrote to the paid main service via "
                    f"{method.upper()} {url} — this cripples the completions API"
                )

    def test_guest_rate_limit_config_never_lands_on_main_service(self):
        """Sharper form: the fail-closed 5/min guest limit must never be the
        body of a write aimed at the main completions service."""
        session = _session()
        session.get.return_value = _mock_response(200, {"data": []})
        session.put.return_value = _mock_response(201, {"id": "svc"})
        session.post.return_value = _mock_response(201, {"id": "pl", "name": "x"})
        session.patch.return_value = _mock_response(200, {"id": "pl"})

        onboard_guest_widget(session, KONG, UPSTREAM)

        for method in ("post", "patch"):
            for url, body in _writes(session, method):
                if body and body.get("name") == "rate-limiting":
                    cfg = body.get("config", {})
                    if cfg.get("minute") == 5 and cfg.get("fault_tolerant") is False:
                        assert f"/services/{SERVICE_NAME}/" not in url, (
                            "guest 5/min fail-closed rate-limit was written to "
                            f"the main service: {method.upper()} {url}"
                        )


# ---------------------------------------------------------------------------
# Additional branch coverage for the shared helpers used by both flows.
# ---------------------------------------------------------------------------

class TestPutOrPostBranches:
    def test_405_falls_back_to_post(self):
        """PUT-to-create rejected with 405 → POST fallback to collection URL."""
        session = _session()
        session.put.return_value = _mock_response(405)
        session.post.return_value = _mock_response(201, {"id": "svc-x"})

        result = _put_or_post(session, f"{KONG}/services/x", {"name": "x"}, "svc x")

        session.post.assert_called_once()
        assert session.post.call_args[0][0] == f"{KONG}/services"  # last segment stripped
        assert result["id"] == "svc-x"

    def test_post_fallback_409_is_idempotent_success(self):
        """PUT 404 then POST 409 (already exists) → treated as success, no raise."""
        session = _session()
        session.put.return_value = _mock_response(404)
        session.post.return_value = _mock_response(409, {"name": "x"})

        result = _put_or_post(session, f"{KONG}/services/x", {"name": "x"}, "svc x")
        assert result.get("name") == "x"

    def test_hard_error_raises(self):
        """A 500 with no fallback path raises HTTPError."""
        session = _session()
        session.put.return_value = _mock_response(500)

        with pytest.raises(requests.HTTPError):
            _put_or_post(session, f"{KONG}/services/x", {"name": "x"}, "svc x")


class TestEnablePluginResilience:
    def test_get_exception_falls_through_to_post(self):
        """If listing existing plugins raises, the code still POSTs the new one."""
        session = _session()
        session.get.side_effect = requests.ConnectionError("boom")
        session.post.return_value = _mock_response(201, {"id": "pl", "name": "cors"})

        result = enable_plugin(session, KONG, PLUGINS[2])  # cors
        assert result["name"] == "cors"
        session.post.assert_called_once()

    def test_patch_non_2xx_falls_through_to_post(self):
        """Existing plugin found but PATCH fails (non-2xx) → falls back to POST."""
        session = _session()
        session.get.return_value = _mock_response(
            200, {"data": [{"id": "pl-1", "name": "key-auth"}]}
        )
        session.patch.return_value = _mock_response(500)
        session.post.return_value = _mock_response(201, {"id": "pl-2", "name": "key-auth"})

        result = enable_plugin(session, KONG, PLUGINS[0])  # key-auth
        assert result["id"] == "pl-2"
        session.post.assert_called_once()


class TestGuestWidgetPluginPatchAndDegrade:
    def test_existing_guest_plugins_are_patched(self):
        """When the guest plugins already exist, onboarding PATCHes them."""
        existing = {"data": [{"id": f"g-{p['name']}", "name": p["name"]} for p in GUEST_PLUGINS]}
        session = _session()
        session.get.return_value = _mock_response(200, existing)
        session.put.return_value = _mock_response(200, {"id": "svc"})
        session.patch.return_value = _mock_response(200, {"id": "g"})

        assert onboard_guest_widget(session, KONG, UPSTREAM) is True
        assert session.patch.call_count >= len(GUEST_PLUGINS)

    def test_guest_plugin_listing_error_degrades_to_post(self):
        """If listing guest plugins raises, onboarding still POSTs them (and returns True)."""
        session = _session()
        session.get.side_effect = requests.ConnectionError("kong hiccup")
        session.put.return_value = _mock_response(201, {"id": "svc"})
        session.post.return_value = _mock_response(201, {"id": "g", "name": "x"})

        assert onboard_guest_widget(session, KONG, UPSTREAM) is True
        # every guest plugin fell through to a POST on the guest service
        guest_posts = [u for u, b in _writes(session, "post") if GUEST_SERVICE_NAME in u]
        assert len(guest_posts) >= len(GUEST_PLUGINS)
