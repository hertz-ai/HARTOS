"""
Security Middleware for Flask
Applies security headers, CORS, CSRF protection, host validation, and API auth.
"""

import os
import logging
from functools import wraps
from flask import Flask, request, jsonify, g

logger = logging.getLogger('hevolve_security')


def apply_security_middleware(app: Flask):
    """Apply all security middleware to a Flask app."""

    _apply_security_headers(app)
    _apply_cors(app)
    _apply_csrf_protection(app)
    _apply_host_validation(app)
    _apply_api_auth(app)


def _apply_security_headers(app: Flask):
    """Add security headers to all responses."""

    @app.after_request
    def add_security_headers(response):
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Permissions-Policy'] = (
            'camera=(), microphone=(), geolocation=(), '
            'payment=(), usb=(), magnetometer=()'
        )

        # HSTS only in production
        if os.environ.get('HEVOLVE_ENV') != 'development':
            response.headers['Strict-Transport-Security'] = (
                'max-age=31536000; includeSubDomains; preload'
            )
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self'; "
                "img-src 'self' data:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'"
            )

        return response


def _apply_cors(app: Flask):
    """CORS with explicit origin allowlist.

    If CORS_ORIGINS is not set, no origins are allowed (fail-closed).
    Set CORS_ORIGINS=* for development only.
    """
    raw_origins = os.environ.get('CORS_ORIGINS', '')
    allowed_origins = set(
        o.strip() for o in raw_origins.split(',')
        if o.strip()
    )
    if not allowed_origins:
        logger.warning(
            "CORS_ORIGINS not configured - no cross-origin requests allowed. "
            "Set CORS_ORIGINS env var for production (comma-separated origins).")

    @app.after_request
    def add_cors_headers(response):
        origin = request.headers.get('Origin', '')

        if origin in allowed_origins:
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Access-Control-Allow-Methods'] = (
                'GET, POST, PUT, DELETE, PATCH, OPTIONS'
            )
            response.headers['Access-Control-Allow-Headers'] = (
                'Content-Type, Authorization, X-API-Key, X-CSRF-Token'
            )
            response.headers['Access-Control-Allow-Credentials'] = 'true'
            response.headers['Access-Control-Max-Age'] = '600'

        return response

    @app.before_request
    def handle_preflight():
        if request.method == 'OPTIONS':
            response = app.make_default_options_response()
            origin = request.headers.get('Origin', '')
            if origin in allowed_origins:
                response.headers['Access-Control-Allow-Origin'] = origin
                response.headers['Access-Control-Allow-Methods'] = (
                    'GET, POST, PUT, DELETE, PATCH, OPTIONS'
                )
                response.headers['Access-Control-Allow-Headers'] = (
                    'Content-Type, Authorization, X-API-Key, X-CSRF-Token'
                )
            return response


def _apply_csrf_protection(app: Flask):
    """CSRF protection for state-changing requests."""

    # Paths exempt from CSRF (API-only endpoints using Bearer auth)
    CSRF_EXEMPT_PREFIXES = (
        '/a2a/', '/api/social/bots/', '/status',
        '/.well-known/',
    )

    @app.before_request
    def csrf_check():
        if request.method not in ('POST', 'PUT', 'DELETE', 'PATCH'):
            return

        # Skip for exempt paths
        if any(request.path.startswith(p) for p in CSRF_EXEMPT_PREFIXES):
            return

        # Bearer token auth is inherently CSRF-safe
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            return

        # API key auth is also CSRF-safe
        if request.headers.get('X-API-Key'):
            return

        # JSON content type with Origin check provides CSRF protection
        if request.content_type and 'application/json' in request.content_type:
            return

        # For non-API requests (forms), require CSRF token
        csrf_token = request.headers.get('X-CSRF-Token')
        if not csrf_token:
            logger.warning(f"CSRF token missing for {request.method} {request.path}")
            return jsonify({'error': 'CSRF token required'}), 403


def _apply_host_validation(app: Flask):
    """Prevent Host header injection."""

    allowed_hosts = set(
        h.strip() for h in
        os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
        if h.strip()
    )
    # '*' is the Django-style wildcard: the operator opted into allow-all. The
    # deploy default is exactly this (deploy-hartos-deepbox.yml sets
    # ALLOWED_HOSTS=`secrets.ALLOWED_HOSTS || '*'`), and it is load-bearing:
    # /api/ota/latest is a PUBLIC pointer fleet nodes poll with
    # Host: <central-host>. Without wildcard support a '*' config is treated as a
    # literal host, so every real Host is rejected 400 and OTA delivery silently
    # breaks fleet-wide. Precompute once — allowed_hosts is fixed at app init.
    allow_all_hosts = '*' in allowed_hosts

    @app.before_request
    def validate_host():
        if os.environ.get('HEVOLVE_ENV') == 'development':
            return
        if os.environ.get('NUNBA_BUNDLED'):
            return
        if allow_all_hosts:
            return

        host = request.host.split(':')[0]
        if host not in allowed_hosts:
            logger.warning(f"Rejected request with invalid Host: {host}")
            return jsonify({'error': 'Invalid host'}), 400


#: Admin operations that modify persistent state. ALWAYS require auth
#: regardless of tier — even on a trusted LAN, random devices (IoT,
#: guests, kids' tablets) shouldn't be able to hit admin routes. The
#: only exception is bundled desktop mode (NUNBA_BUNDLED), which is
#: single-user in-process test_client territory with no network exposure.
#:
#: Each entry is a prefix — anything starting with it is considered an
#: admin path. Add new admin routes to this tuple; they inherit the
#: auth gate automatically.
ADMIN_PATHS = ('/api/admin',)

#: User-facing API endpoints. These are guarded only when the deployment
#: is publicly exposed (central tier). Regional deployments live on a
#: trusted LAN or behind a gateway (KONG, etc.) that handles auth; flat
#: deployments are single-user desktop and pre-trusted.
NETWORK_PROTECTED_PATHS = ('/chat', '/time_agent', '/visual_agent',
                           '/add_history', '/prompts', '/zeroshot',
                           '/response_ack')

#: Legacy alias — some tests still import PROTECTED_PATHS expecting
#: the combined tuple. Keep this as the union so older imports don't
#: break, while the split above drives the new enforcement logic.
PROTECTED_PATHS = ADMIN_PATHS + NETWORK_PROTECTED_PATHS

EXEMPT_PREFIXES = ('/status', '/a2a/', '/api/social/', '/.well-known/',
                   '/prompts/public')


def _apply_api_auth(app: Flask):
    """Tier-aware API authentication with a strict admin guard.

    Two gates run in order:

      1. ADMIN guard — /api/admin/* ALWAYS requires auth on any tier
         except bundled desktop. Admin ops modify persistent state so
         LAN trust is not enough — a compromised IoT device on the
         same network must not be able to drop agents or reconfigure
         TTS engines.
      2. NETWORK guard — /chat, /prompts, /visual_agent, etc. are
         guarded only on central tier (publicly exposed). Regional
         and flat tiers assume LAN trust or gateway-handled auth.

    When HEVOLVE_API_KEY is set, BOTH gates accept X-API-Key. When it
    is unset, BOTH gates accept only a Bearer JWT. Exempt prefixes
    (/status, /a2a/, /api/social/, /.well-known/, /prompts/public)
    bypass both gates so health probes and social-media-facing routes
    stay public.

    Deployment scenarios:
      - Behind KONG:                    KONG handles auth → no key needed,
                                        middleware enforces tier-conditional
                                        only if KONG is bypassed
      - Bundled desktop (NUNBA_BUNDLED): early return, always trusted
      - Regional LAN:                   /chat open, /api/admin gated
      - Central cloud:                  everything gated
    """

    def _path_matches_any(path: str, prefixes: tuple) -> bool:
        return any(path == p or path.startswith(p + '/') for p in prefixes)

    def _is_exempt(path: str) -> bool:
        return any(path.startswith(p) for p in EXEMPT_PREFIXES)

    def _is_admin_path(path: str) -> bool:
        return _path_matches_any(path, ADMIN_PATHS)

    def _is_network_protected(path: str) -> bool:
        return _path_matches_any(path, NETWORK_PROTECTED_PATHS)

    def _require_api_key_or_bearer(expected_key: str):
        """Return None if the request carries a valid credential, else
        a 401 jsonify response. Preference order matches the original
        middleware: X-API-Key if configured, otherwise Bearer token."""
        if expected_key:
            api_key = request.headers.get('X-API-Key')
            if api_key and _constant_time_compare(api_key, expected_key):
                return None
            # Fall through to Bearer check so API-key-configured deploys
            # still accept JWTs (useful for admin UI + k8s probes).
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            # Actually decode and verify the JWT — not just check the prefix.
            # Without this, `Bearer garbage` passes the admin gate.
            _token = auth_header[7:]
            try:
                from integrations.social.auth import decode_jwt
                jwt_payload = decode_jwt(_token)
                if jwt_payload:
                    g.auth_source = 'jwt'
                    g.jwt_payload = jwt_payload
                    return None
            except Exception:
                pass
            # Token invalid/expired — reject
            return jsonify({'error': 'Invalid or expired Bearer token'}), 401
        if expected_key:
            logger.warning(f"Invalid/missing credential for {request.path}")
            return jsonify(
                {'error': 'X-API-Key header or Bearer token required'},
            ), 401
        return jsonify(
            {'error': 'Authentication required (Bearer token)'},
        ), 401

    @app.before_request
    def check_api_auth():
        # Bundled/desktop mode: in-process test_client, always trusted.
        if os.environ.get('NUNBA_BUNDLED'):
            return

        path = request.path
        if _is_exempt(path):
            return

        # Resolve the shared credential once — both gates share it.
        try:
            from security.secrets_manager import get_secret
            expected_key = get_secret('HEVOLVE_API_KEY')
        except Exception:
            expected_key = os.environ.get('HEVOLVE_API_KEY', '')

        # Gate 1: Admin paths. ALWAYS required. Even regional LAN
        # deployments gate admin ops — the tier model is for user-facing
        # APIs, not for operations that mutate persistent state.
        if _is_admin_path(path):
            resp = _require_api_key_or_bearer(expected_key)
            if resp is not None:
                return resp
            return  # Admin path authenticated, skip gate 2

        # Gate 2: User-facing API paths. Only enforced on central tier
        # (publicly exposed) OR whenever HEVOLVE_API_KEY is explicitly set
        # (direct-exposure deployments that opt in to the key layer).
        if not _is_network_protected(path):
            return  # Not in either tuple → public

        node_tier = os.environ.get('HEVOLVE_NODE_TIER', 'flat')
        if expected_key:
            resp = _require_api_key_or_bearer(expected_key)
            if resp is not None:
                return resp
            return
        if node_tier == 'central':
            resp = _require_api_key_or_bearer(expected_key='')
            if resp is not None:
                return resp
            return
        # Non-central without API key → LAN-trusted or gateway-auth'd
        return


def _constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())
