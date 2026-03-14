"""
Kong API Gateway onboarding for the Mindstory SDK.

Programmatically registers the hevolve-completions service, routes, and
plugins via the Kong Admin API.  Every operation is idempotent: objects are
created on first run and patched on subsequent runs.

Usage:
    python -m integrations.gateway.kong_onboard
    python -m integrations.gateway.kong_onboard --kong-url http://kong:8001 --upstream http://ai:8000
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, Optional

import requests

# ---------------------------------------------------------------------------
# Defaults aligned with KONG_GATEWAY.md
# ---------------------------------------------------------------------------
DEFAULT_KONG_ADMIN_URL = "http://localhost:8001"
DEFAULT_UPSTREAM_URL = "http://localhost:8000"
SERVICE_NAME = "hevolve-completions"
ROUTE_NAME = "completions-route"

ROUTE_PATHS = [
    "/v1/chat/completions",
    "/v1/corrections",
    "/v1/stats",
    "/health",
]

# Plugin configurations from KONG_GATEWAY.md
PLUGINS: list[Dict[str, Any]] = [
    {
        "name": "key-auth",
        "config": {
            "key_names": ["Authorization", "apikey"],
            "key_in_header": True,
            "key_in_query": False,
            "hide_credentials": True,
        },
    },
    {
        "name": "rate-limiting",
        "config": {
            "minute": 60,
            "hour": 1000,
            "day": 10000,
            "policy": "redis",
            "redis_host": "localhost",
            "redis_port": 6379,
            "fault_tolerant": True,
            "hide_client_headers": False,
        },
    },
    {
        "name": "cors",
        "config": {
            "origins": ["*"],
            "methods": ["GET", "POST", "OPTIONS"],
            "headers": ["Content-Type", "Authorization"],
            "credentials": True,
            "max_age": 3600,
        },
    },
    {
        "name": "request-size-limiting",
        "config": {
            "allowed_payload_size": 10,  # MB — base64 images
        },
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    """Print a timestamped status line."""
    print(f"[kong-onboard] {msg}")


def _put_or_post(
    session: requests.Session,
    url: str,
    payload: Dict[str, Any],
    resource_label: str,
) -> Dict[str, Any]:
    """PUT to *url* (idempotent upsert).  Falls back to POST if the Admin
    API version does not support PUT-to-create.

    Returns the JSON body of the successful response.
    Raises ``requests.HTTPError`` on unrecoverable failure.
    """
    resp = session.put(url, json=payload)
    if resp.status_code in (200, 201):
        verb = "updated" if resp.status_code == 200 else "created"
        _log(f"  {resource_label}: {verb}")
        return resp.json()

    # Some older Kong builds reject PUT-to-create; fall back to POST.
    if resp.status_code in (404, 405):
        # Derive the collection URL by stripping the last path segment.
        collection_url = url.rsplit("/", 1)[0]
        resp2 = session.post(collection_url, json=payload)
        if resp2.status_code == 409:
            # Already exists — treat as success (idempotent).
            _log(f"  {resource_label}: already exists (no change)")
            return resp2.json() if resp2.text else {}
        resp2.raise_for_status()
        _log(f"  {resource_label}: created (POST fallback)")
        return resp2.json()

    if resp.status_code == 409:
        _log(f"  {resource_label}: already exists (no change)")
        return resp.json() if resp.text else {}

    resp.raise_for_status()
    return {}  # unreachable, but keeps mypy happy


# ---------------------------------------------------------------------------
# Core onboarding steps
# ---------------------------------------------------------------------------

def create_service(
    session: requests.Session,
    kong_url: str,
    upstream_url: str,
) -> Dict[str, Any]:
    """Create or update the ``hevolve-completions`` service."""
    _log("Step 1/4 — Service")
    payload = {
        "name": SERVICE_NAME,
        "url": upstream_url,
        "retries": 3,
        "connect_timeout": 10000,
        "write_timeout": 60000,
        "read_timeout": 60000,
    }
    url = f"{kong_url}/services/{SERVICE_NAME}"
    return _put_or_post(session, url, payload, f"service '{SERVICE_NAME}'")


def create_route(
    session: requests.Session,
    kong_url: str,
) -> Dict[str, Any]:
    """Create or update the completions route on the service."""
    _log("Step 2/4 — Route")
    payload = {
        "name": ROUTE_NAME,
        "paths": ROUTE_PATHS,
        "methods": ["POST", "GET"],
        "protocols": ["https"],
        "strip_path": False,
    }
    url = f"{kong_url}/services/{SERVICE_NAME}/routes/{ROUTE_NAME}"
    return _put_or_post(session, url, payload, f"route '{ROUTE_NAME}'")


def enable_plugin(
    session: requests.Session,
    kong_url: str,
    plugin_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Enable (or update) a single plugin on the service.

    Uses PUT to ``/services/{name}/plugins/{plugin_name}`` for idempotency.
    """
    plugin_name = plugin_cfg["name"]
    payload = {
        "name": plugin_name,
        "config": plugin_cfg["config"],
        "enabled": True,
    }
    url = f"{kong_url}/services/{SERVICE_NAME}/plugins"

    # Try to find existing plugin first for idempotent update
    try:
        existing = session.get(url)
        if existing.status_code == 200:
            data = existing.json().get("data", [])
            for p in data:
                if p.get("name") == plugin_name:
                    # Update existing plugin
                    plugin_id = p["id"]
                    resp = session.patch(
                        f"{kong_url}/plugins/{plugin_id}",
                        json=payload,
                    )
                    if resp.status_code in (200, 201):
                        _log(f"  plugin '{plugin_name}': updated")
                        return resp.json()
    except Exception:
        pass

    # Create new
    resp = session.post(url, json=payload)
    if resp.status_code == 409:
        _log(f"  plugin '{plugin_name}': already exists (no change)")
        return resp.json() if resp.text else {}
    resp.raise_for_status()
    _log(f"  plugin '{plugin_name}': created")
    return resp.json()


def enable_plugins(
    session: requests.Session,
    kong_url: str,
) -> list[Dict[str, Any]]:
    """Enable all required plugins on the service."""
    _log("Step 3/4 — Plugins")
    results = []
    for plugin_cfg in PLUGINS:
        result = enable_plugin(session, kong_url, plugin_cfg)
        results.append(result)
    return results


def verify(session: requests.Session, kong_url: str) -> bool:
    """Quick verification: fetch the service back from Kong."""
    _log("Step 4/4 — Verify")
    try:
        resp = session.get(f"{kong_url}/services/{SERVICE_NAME}")
        if resp.status_code == 200:
            svc = resp.json()
            _log(f"  service id={svc.get('id', '?')}, host={svc.get('host', '?')}")
            return True
        _log(f"  verification failed: HTTP {resp.status_code}")
        return False
    except requests.ConnectionError:
        _log("  verification failed: Kong unreachable")
        return False


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def onboard(
    kong_url: str = DEFAULT_KONG_ADMIN_URL,
    upstream_url: str = DEFAULT_UPSTREAM_URL,
    session: Optional[requests.Session] = None,
) -> bool:
    """Run all onboarding steps.  Returns ``True`` on success."""
    if session is None:
        session = requests.Session()

    _log(f"Kong Admin API : {kong_url}")
    _log(f"Upstream target: {upstream_url}")
    _log("")

    try:
        # Query existing state first
        _log("Querying existing Kong configuration...")
        try:
            existing_svc = session.get(f"{kong_url}/services/{SERVICE_NAME}")
            if existing_svc.status_code == 200:
                svc = existing_svc.json()
                _log(f"  Found service '{SERVICE_NAME}' → {svc.get('host', '?')}:{svc.get('port', '?')}")
            else:
                _log(f"  No existing service '{SERVICE_NAME}' — will create")

            existing_routes = session.get(f"{kong_url}/services/{SERVICE_NAME}/routes")
            if existing_routes.status_code == 200:
                routes = existing_routes.json().get("data", [])
                for r in routes:
                    _log(f"  Found route '{r.get('name', '?')}' paths={r.get('paths', [])}")
            existing_plugins = session.get(f"{kong_url}/services/{SERVICE_NAME}/plugins")
            if existing_plugins.status_code == 200:
                plugins = existing_plugins.json().get("data", [])
                for p in plugins:
                    _log(f"  Found plugin '{p.get('name', '?')}' enabled={p.get('enabled', '?')}")
        except requests.ConnectionError:
            _log("  Kong not reachable — will attempt creation")
        _log("")

        create_service(session, kong_url, upstream_url)
        create_route(session, kong_url)
        enable_plugins(session, kong_url)
        ok = verify(session, kong_url)
    except requests.ConnectionError:
        _log("ERROR: Cannot reach Kong Admin API — is Kong running?")
        return False
    except requests.HTTPError as exc:
        _log(f"ERROR: Kong returned an error: {exc}")
        return False

    if ok:
        _log("")
        _log("Onboarding complete.  Mindstory SDK routes are live.")
    return ok


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Onboard the Mindstory SDK into Kong API Gateway",
    )
    parser.add_argument(
        "--kong-url",
        default=DEFAULT_KONG_ADMIN_URL,
        help=f"Kong Admin API base URL (default: {DEFAULT_KONG_ADMIN_URL})",
    )
    parser.add_argument(
        "--upstream",
        default=DEFAULT_UPSTREAM_URL,
        help=f"HevolveAI upstream URL (default: {DEFAULT_UPSTREAM_URL})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ok = onboard(kong_url=args.kong_url, upstream_url=args.upstream)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
