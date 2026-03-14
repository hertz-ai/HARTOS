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
    create_service,
    create_route,
    enable_plugin,
    enable_plugins,
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
