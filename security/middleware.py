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
                "style-src 'self' 'unsafe-inline'; "
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
            "CORS_ORIGINS not configured — no cross-origin requests allowed. "
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

    @app.before_request
    def validate_host():
        if os.environ.get('HEVOLVE_ENV') == 'development':
            return

        host = request.host.split(':')[0]
        if host not in allowed_hosts:
            logger.warning(f"Rejected request with invalid Host: {host}")
            return jsonify({'error': 'Invalid host'}), 400


def _apply_api_auth(app: Flask):
    """API key authentication for core endpoints."""

    PROTECTED_PATHS = ('/chat', '/time_agent', '/visual_agent', '/add_history', '/prompts', '/zeroshot', '/response_ack')
    EXEMPT_PREFIXES = ('/status', '/a2a/', '/api/social/', '/.well-known/')

    @app.before_request
    def check_api_auth():
        # Only protect specific paths
        if not any(request.path == p or request.path.startswith(p + '/')
                    for p in PROTECTED_PATHS):
            return

        # Check for exempt paths
        if any(request.path.startswith(p) for p in EXEMPT_PREFIXES):
            return

        # Check API key
        api_key = request.headers.get('X-API-Key')
        if not api_key:
            return jsonify({'error': 'X-API-Key header required'}), 401

        try:
            from security.secrets_manager import get_secret
            expected_key = get_secret('HEVOLVE_API_KEY')
        except Exception:
            expected_key = os.environ.get('HEVOLVE_API_KEY', '')

        if not expected_key:
            logger.warning("HEVOLVE_API_KEY not configured - rejecting request")
            return jsonify({'error': 'Server API key not configured'}), 500

        if not _constant_time_compare(api_key, expected_key):
            logger.warning(f"Invalid API key for {request.path}")
            return jsonify({'error': 'Invalid API key'}), 401


def _constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())
