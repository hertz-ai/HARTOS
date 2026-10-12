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
# Defaults: localhost for dev, cloud Kong via KONG_ADMIN_URL env var
# Cloud Kong admin is only accessible from inside the VM (not publicly exposed)
# Cloud proxy: https://azurekong.hertzai.com (port 443/8443)
import os as _os
DEFAULT_KONG_ADMIN_URL = _os.environ.get("KONG_ADMIN_URL", "http://localhost:8001")
DEFAULT_UPSTREAM_URL = _os.environ.get("HEVOLVE_API_URL", "http://localhost:8000")
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
    """Enable (or update) a single plugin on the Mindstory service."""
    return _upsert_plugin(session, kong_url,
                          f"/services/{SERVICE_NAME}/plugins", plugin_cfg)


def _upsert_plugin(
    session: requests.Session,
    kong_url: str,
    scope_path: str,
    plugin_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Enable (or update) one plugin under ``scope_path`` (a service's or a
    route's ``/plugins`` collection): list, PATCH the one with this name if
    it exists, else POST; a 409 is "already there".  The ONE upsert every
    onboarding block here uses."""
    plugin_name = plugin_cfg["name"]
    payload = {
        "name": plugin_name,
        "config": plugin_cfg["config"],
        "enabled": True,
    }
    url = f"{kong_url}{scope_path}"

    # Find the existing plugin first (idempotent update).  A lookup or update
    # that fails still falls through to the create, so a first-time create
    # survives an admin-API hiccup (TestEnablePluginResilience pins this),
    # but it is remembered: see the 409 branch below.
    unreached = False
    try:
        existing = session.get(url)
        if existing.status_code == 200:
            for p in existing.json().get("data", []):
                if p.get("name") == plugin_name:
                    resp = session.patch(f"{kong_url}/plugins/{p['id']}", json=payload)
                    if resp.status_code in (200, 201):
                        _log(f"  plugin '{plugin_name}': updated")
                        return resp.json()
                    _log(f"  plugin '{plugin_name}': update HTTP {resp.status_code}; "
                         "trying create")
                    unreached = True
                    break
        else:
            _log(f"  plugin '{plugin_name}': lookup HTTP {existing.status_code}; trying create")
            unreached = True
    except Exception as e:
        _log(f"  plugin '{plugin_name}': lookup failed ({e}); trying create")
        unreached = True

    # Create new
    resp = session.post(url, json=payload)
    if resp.status_code == 409:
        if unreached:
            # It exists, and neither the lookup nor the update reached it: the
            # config on the box is whatever it was.  Reported, and falsy, so
            # the caller does not call the gateway ready (review of 00d29f073:
            # this used to read "already exists (no change)" while a changed
            # cap was silently not applied).
            _log(f"  plugin '{plugin_name}': exists but could NOT be updated; NOT applied")
            return {}
        _log(f"  plugin '{plugin_name}': already exists (no change)")
        return resp.json() if resp.text else {"name": plugin_name}
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


# ---------------------------------------------------------------------------
# Guest widget service — rate-limited, CORS-locked, no key-auth
# ---------------------------------------------------------------------------

GUEST_SERVICE_NAME = "hevolve-guest-widget"
GUEST_ROUTE_NAME = "guest-register-route"

GUEST_ROUTE_PATHS = [
    "/api/social/auth/guest-register",
]

GUEST_PLUGINS: list[Dict[str, Any]] = [
    {
        "name": "rate-limiting",
        "config": {
            "minute": 5,        # 5 guest tokens per minute per IP
            "hour": 30,         # 30 per hour — enough for real users, stops farming
            "policy": "local",  # No Redis dependency for this
            "fault_tolerant": False,  # Fail closed — deny if counter broken
            "hide_client_headers": False,
        },
    },
    {
        "name": "cors",
        "config": {
            "origins": [
                "https://docs.hevolve.ai",
                "https://hevolve.ai",
                "http://localhost:8000",  # local dev
            ],
            "methods": ["POST", "OPTIONS"],
            "headers": ["Content-Type"],
            "credentials": False,
            "max_age": 3600,
        },
    },
    {
        "name": "request-size-limiting",
        "config": {
            "allowed_payload_size": 1,  # 1 MB — guest register is tiny
        },
    },
    {
        "name": "ip-restriction",
        "config": {
            "deny": [],   # Populated by abuse detection
            "allow": [],  # Empty = allow all (deny-list mode)
        },
    },
]


def onboard_guest_widget(
    session: requests.Session,
    kong_url: str,
    upstream_url: str,
) -> bool:
    """Register the guest-register endpoint in Kong with tight rate limits.

    No key-auth — the widget is public. Protection is via rate limiting,
    CORS origin lock, and short-lived tokens (server-side).
    """
    _log("")
    _log("=== Guest Widget Service ===")

    # Service
    _log("Step 1/3 — Guest service")
    svc_payload = {
        "name": GUEST_SERVICE_NAME,
        "url": upstream_url,
        "retries": 1,
        "connect_timeout": 5000,
        "write_timeout": 10000,
        "read_timeout": 10000,
    }
    _put_or_post(
        session,
        f"{kong_url}/services/{GUEST_SERVICE_NAME}",
        svc_payload,
        f"service '{GUEST_SERVICE_NAME}'",
    )

    # Route
    _log("Step 2/3 — Guest route")
    route_payload = {
        "name": GUEST_ROUTE_NAME,
        "paths": GUEST_ROUTE_PATHS,
        "methods": ["POST", "OPTIONS"],
        "protocols": ["https", "http"],
        "strip_path": False,
    }
    _put_or_post(
        session,
        f"{kong_url}/services/{GUEST_SERVICE_NAME}/routes/{GUEST_ROUTE_NAME}",
        route_payload,
        f"route '{GUEST_ROUTE_NAME}'",
    )

    # Plugins
    _log("Step 3/3 — Guest plugins")
    for plugin_cfg in GUEST_PLUGINS:
        # Apply each plugin to the GUEST service ONLY.  enable_plugin() is
        # hard-scoped to the main completions service (SERVICE_NAME), so calling
        # it here would PATCH the paid API's rate-limiting down to the guest
        # 5/min fail-closed limit and overwrite its wildcard CORS.  The block
        # below targets GUEST_SERVICE_NAME explicitly.
        plugin_name = plugin_cfg["name"]
        payload = {
            "name": plugin_name,
            "config": plugin_cfg["config"],
            "enabled": True,
        }
        # Apply to guest service specifically
        guest_url = f"{kong_url}/services/{GUEST_SERVICE_NAME}/plugins"
        try:
            existing = session.get(guest_url)
            if existing.status_code == 200:
                data = existing.json().get("data", [])
                for p in data:
                    if p.get("name") == plugin_name:
                        session.patch(f"{kong_url}/plugins/{p['id']}", json=payload)
                        break
                else:
                    session.post(guest_url, json=payload)
            else:
                session.post(guest_url, json=payload)
        except Exception:
            session.post(guest_url, json=payload)

    _log("Guest widget onboarding complete.")
    return True


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
        onboard_guest_widget(session, kong_url, upstream_url)
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

# ---------------------------------------------------------------------------
# LLM gateway: HARTOS -> Kong -> gpt3 (owner policy 2026-10-12)
# ---------------------------------------------------------------------------
#
# HARTOS calls ONE endpoint for every LLM turn (core.autogen_config).  With
# Kong in front, that endpoint is this service: Kong forwards /llm/v1/... to
# gpt3's /v1/... (the provider fan-out) and applies request caps by the two
# headers HARTOS stamps on every call (core.llm_outbound_logger._annotate_
# request): ``X-HARTOS-Lane`` (revenue | user | daemon) and ``X-HARTOS-Model``.
#
# This Kong is 2.8.5 community (measured 2026-10-12): no AI plugins, no token
# or dollar awareness.  So Kong is the fuse and HARTOS's budget gate is the
# meter.  Routes are matched on header VALUES (Kong routes match headers
# exactly), one route per cap:
#
#   llm-revenue  Lane=revenue                         no rate-limiting at all:
#                a paying conversation is never throttled (owner).
#   llm-claude   Lane in {user, daemon} + Model-Family  a fuse on Claude calls
#                = claude                               that policy already
#                                                       forbids (requests/day,
#                                                       LLM_CLAUDE_REQUESTS_PER_DAY)
#   llm-user     Lane=user                            ordinary person caps
#   llm-daemon   Lane=daemon                          background caps, tightest
#   llm-default  no header required                   a caller that stamps no
#                                                     lane is a user
#
# Two headers on llm-claude make it the most specific match for a user's or
# the daemon's Claude call; a revenue call to Claude matches only llm-revenue
# (its lane is not in the claude route's list), so Kong never throttles it.
# SAID PLAINLY: revenue-lane Claude spend has NO Kong fuse.  Its cap is the
# HARTOS day budget (budget_gate.expert_budget_allows), which withholds the
# expert past $6 and when the ledger cannot be read; this route only fuses the
# Claude calls policy forbids anyway (user and daemon lanes).
# The family comes from HARTOS's X-HARTOS-Model-Family header (one rule,
# core.autogen_config.model_family, the same the dollar meter prices by), so
# there is no second list of Claude model names here.
# Counters key on client ip per route (``limit_by: ip``): every HARTOS call on
# the box arrives from the container's address, so one counter == one lane.
# No key-auth: the service accepts only loopback and the docker bridge
# (ip-restriction), which is where HARTOS runs.  Any client the allowlist admits
# picks its own cap with a header, so before applying this on a box whose Kong
# proxy port is reachable from outside (deepbox's 8100 is NATed per
# docs/internal/FLYWHEEL_RECOVERY_BRIEF.md) check Kong's trusted_ips / real_ip
# settings, or narrow LLM_GATEWAY_ALLOWED_CLIENTS to the one container.
LLM_SERVICE_NAME = "llm-gateway"
LLM_ROUTE_PATH = "/llm"                 # stripped: /llm/v1/x -> gpt3 /v1/x
DEFAULT_LLM_UPSTREAM_URL = _os.environ.get("LLM_GATEWAY_UPSTREAM", "http://172.17.0.1:5459")
LLM_LANE_HEADER = "X-HARTOS-Lane"
LLM_MODEL_FAMILY_HEADER = "X-HARTOS-Model-Family"
LLM_READ_TIMEOUT_MS = 180000            # a 27B VL page read or a long Claude turn
LLM_BODY_LIMIT_MB = 20                  # base64 pages and screenshots
LLM_ALLOWED_CLIENTS = [p.strip() for p in _os.environ.get(
    "LLM_GATEWAY_ALLOWED_CLIENTS", "127.0.0.1,172.17.0.0/16").split(",") if p.strip()]
CAPPED_MODEL_FAMILY = "claude"
LLM_ROUTE_REVENUE = "llm-revenue"
LLM_ROUTE_CLAUDE = "llm-claude"
LLM_ROUTE_USER = "llm-user"
LLM_ROUTE_DAEMON = "llm-daemon"
LLM_ROUTE_DEFAULT = "llm-default"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(_os.environ.get(name, "") or default))
    except ValueError:
        return default


def llm_lane_routes() -> list:
    """The five routes, each ``{name, headers, limits}``; ``limits`` None means
    no rate-limiting plugin.  Caps are env-tunable so the owner can move them
    without a code change; the Claude day cap is the $6/day policy in requests
    (60 at about ten cents a call, the owner's number to tune from measured
    tokens per turn)."""
    user = {"minute": _env_int("LLM_USER_PER_MINUTE", 60),
            "hour": _env_int("LLM_USER_PER_HOUR", 600),
            "day": _env_int("LLM_USER_PER_DAY", 5000)}
    daemon = {"minute": _env_int("LLM_DAEMON_PER_MINUTE", 20),
              "hour": _env_int("LLM_DAEMON_PER_HOUR", 200),
              "day": _env_int("LLM_DAEMON_PER_DAY", 2000)}
    claude = {"hour": _env_int("LLM_CLAUDE_REQUESTS_PER_HOUR", 20),
              "day": _env_int("LLM_CLAUDE_REQUESTS_PER_DAY", 60)}
    return [
        {"name": LLM_ROUTE_REVENUE, "headers": {LLM_LANE_HEADER: ["revenue"]}, "limits": None},
        {"name": LLM_ROUTE_CLAUDE,
         "headers": {LLM_LANE_HEADER: ["user", "daemon"],
                     LLM_MODEL_FAMILY_HEADER: [CAPPED_MODEL_FAMILY]},
         "limits": claude},
        {"name": LLM_ROUTE_USER, "headers": {LLM_LANE_HEADER: ["user"]}, "limits": user},
        {"name": LLM_ROUTE_DAEMON, "headers": {LLM_LANE_HEADER: ["daemon"]}, "limits": daemon},
        {"name": LLM_ROUTE_DEFAULT, "headers": None, "limits": dict(user)},
    ]


def _rate_limit_plugin(limits: Dict[str, int]) -> Dict[str, Any]:
    cfg = {"policy": "local", "limit_by": "ip", "fault_tolerant": True,
           "hide_client_headers": False}
    cfg.update(limits)
    return {"name": "rate-limiting", "config": cfg}


def onboard_llm_gateway(
    session: requests.Session,
    kong_url: str,
    upstream_url: str = None,
) -> bool:
    """Create or update the LLM gateway service, its five lane routes and
    their plugins.  Idempotent; returns True only when the service reads back
    AND every plugin (body limit, allowlist, each lane's cap) was applied: a
    cap that could not be set must not be reported as a gateway that is
    ready."""
    upstream_url = upstream_url or DEFAULT_LLM_UPSTREAM_URL
    applied = []
    _log("")
    _log("=== LLM gateway (HARTOS -> Kong -> gpt3) ===")
    _log("Step 1/3 — Service")
    _put_or_post(session, f"{kong_url}/services/{LLM_SERVICE_NAME}", {
        "name": LLM_SERVICE_NAME,
        "url": upstream_url,
        "retries": 0,                      # never replay an LLM call
        "connect_timeout": 10000,
        "write_timeout": LLM_READ_TIMEOUT_MS,
        "read_timeout": LLM_READ_TIMEOUT_MS,
    }, f"service '{LLM_SERVICE_NAME}'")
    svc_plugins = f"/services/{LLM_SERVICE_NAME}/plugins"
    applied.append(bool(_upsert_plugin(session, kong_url, svc_plugins, {
        "name": "request-size-limiting",
        "config": {"allowed_payload_size": LLM_BODY_LIMIT_MB}})))
    applied.append(bool(_upsert_plugin(session, kong_url, svc_plugins, {
        "name": "ip-restriction",
        "config": {"allow": list(LLM_ALLOWED_CLIENTS), "deny": []}})))

    _log("Step 2/3 — Lane routes")
    for route in llm_lane_routes():
        payload = {
            "name": route["name"],
            "paths": [LLM_ROUTE_PATH],
            "methods": ["POST", "GET"],
            "protocols": ["http", "https"],
            "strip_path": True,
        }
        if route["headers"]:
            payload["headers"] = route["headers"]
        _put_or_post(session,
                     f"{kong_url}/services/{LLM_SERVICE_NAME}/routes/{route['name']}",
                     payload, f"route '{route['name']}'")
        if route["limits"]:
            applied.append(bool(_upsert_plugin(
                session, kong_url, f"/routes/{route['name']}/plugins",
                _rate_limit_plugin(route["limits"]))))

    _log("Step 3/3 — Verify")
    try:
        resp = session.get(f"{kong_url}/services/{LLM_SERVICE_NAME}")
        readable = resp.status_code == 200
    except requests.ConnectionError:
        readable = False
    ok = readable and all(applied)
    _log("  LLM gateway " + ("ready" if ok else
                             "NOT readable back" if not readable else
                             f"NOT ready: {applied.count(False)} plugin(s) not applied"))
    return ok


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
    parser.add_argument(
        "--llm",
        action="store_true",
        help="onboard ONLY the LLM gateway (HARTOS -> Kong -> gpt3) with its lane routes",
    )
    parser.add_argument(
        "--llm-upstream",
        default=DEFAULT_LLM_UPSTREAM_URL,
        help=f"gpt3 base URL the LLM gateway forwards to (default: {DEFAULT_LLM_UPSTREAM_URL})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.llm:
        try:
            ok = onboard_llm_gateway(requests.Session(), args.kong_url, args.llm_upstream)
        except requests.RequestException as exc:
            _log(f"ERROR: Kong Admin API: {exc}")
            ok = False
        return 0 if ok else 1
    ok = onboard(kong_url=args.kong_url, upstream_url=args.upstream)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
