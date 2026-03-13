"""
Tests for security/middleware.py — the outermost security boundary.

Covers: security headers, CORS, CSRF protection, host validation, API auth,
and constant-time string comparison.
"""

import os
import pytest
from unittest.mock import patch
from flask import Flask, jsonify


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def make_app():
    """Factory that creates a Flask app with security middleware applied.

    Env vars are set via os.environ directly so they persist during
    request handling (middleware reads env at request time, not just
    at registration time). Cleaned up after the test.
    """
    _saved = {}
    _added = []

    def _make(env_vars=None):
        env = {
            'CORS_ORIGINS': '',
            'ALLOWED_HOSTS': 'localhost,127.0.0.1',
            'HEVOLVE_ENV': 'production',
            'HEVOLVE_API_KEY': '',
        }
        if env_vars:
            env.update(env_vars)

        # Set env vars, saving originals for cleanup
        for k, v in env.items():
            if k in os.environ:
                _saved[k] = os.environ[k]
            else:
                _added.append(k)
            os.environ[k] = v

        app = Flask(__name__)
        app.config['TESTING'] = True

        from security.middleware import apply_security_middleware
        apply_security_middleware(app)

        @app.route('/chat', methods=['GET', 'POST'])
        def chat():
            return jsonify({'ok': True})

        @app.route('/status')
        def status():
            return jsonify({'status': 'ok'})

        @app.route('/api/social/feed')
        def social_feed():
            return jsonify({'feed': []})

        @app.route('/a2a/test/execute', methods=['POST'])
        def a2a_exec():
            return jsonify({'ok': True})

        @app.route('/.well-known/agent.json')
        def well_known():
            return jsonify({'name': 'test'})

        @app.route('/form-submit', methods=['POST'])
        def form_submit():
            return jsonify({'ok': True})

        @app.route('/prompts', methods=['GET', 'POST'])
        def prompts():
            return jsonify({'ok': True})

        return app.test_client(), app
    yield _make

    # Cleanup: restore original env vars
    for k, v in _saved.items():
        os.environ[k] = v
    for k in _added:
        os.environ.pop(k, None)


# ── Security Headers ─────────────────────────────────────────────

class TestSecurityHeaders:
    """Test that security headers are applied to all responses."""

    def test_x_frame_options(self, make_app):
        client, _ = make_app()
        resp = client.get('/status')
        assert resp.headers.get('X-Frame-Options') == 'DENY'

    def test_x_content_type_options(self, make_app):
        client, _ = make_app()
        resp = client.get('/status')
        assert resp.headers.get('X-Content-Type-Options') == 'nosniff'

    def test_x_xss_protection(self, make_app):
        client, _ = make_app()
        resp = client.get('/status')
        assert resp.headers.get('X-XSS-Protection') == '1; mode=block'

    def test_referrer_policy(self, make_app):
        client, _ = make_app()
        resp = client.get('/status')
        assert resp.headers.get('Referrer-Policy') == 'strict-origin-when-cross-origin'

    def test_permissions_policy(self, make_app):
        client, _ = make_app()
        resp = client.get('/status')
        pp = resp.headers.get('Permissions-Policy', '')
        assert 'camera=()' in pp
        assert 'microphone=()' in pp
        assert 'geolocation=()' in pp

    def test_hsts_in_production(self, make_app):
        client, _ = make_app({'HEVOLVE_ENV': 'production'})
        resp = client.get('/status')
        hsts = resp.headers.get('Strict-Transport-Security', '')
        assert 'max-age=31536000' in hsts
        assert 'includeSubDomains' in hsts

    def test_no_hsts_in_development(self, make_app):
        client, _ = make_app({'HEVOLVE_ENV': 'development'})
        resp = client.get('/status')
        assert 'Strict-Transport-Security' not in resp.headers

    def test_csp_in_production(self, make_app):
        client, _ = make_app({'HEVOLVE_ENV': 'production'})
        resp = client.get('/status')
        csp = resp.headers.get('Content-Security-Policy', '')
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp

    def test_no_csp_in_development(self, make_app):
        client, _ = make_app({'HEVOLVE_ENV': 'development'})
        resp = client.get('/status')
        assert 'Content-Security-Policy' not in resp.headers


# ── CORS ─────────────────────────────────────────────────────────

class TestCORS:
    """Test CORS origin allowlist behavior."""

    def test_allowed_origin_gets_cors_headers(self, make_app):
        client, _ = make_app({'CORS_ORIGINS': 'https://hart.ai'})
        resp = client.get('/status', headers={'Origin': 'https://hart.ai'})
        assert resp.headers.get('Access-Control-Allow-Origin') == 'https://hart.ai'
        assert 'GET' in resp.headers.get('Access-Control-Allow-Methods', '')

    def test_disallowed_origin_no_cors_headers(self, make_app):
        client, _ = make_app({'CORS_ORIGINS': 'https://hart.ai'})
        resp = client.get('/status', headers={'Origin': 'https://evil.com'})
        assert 'Access-Control-Allow-Origin' not in resp.headers

    def test_no_origin_header_no_cors(self, make_app):
        client, _ = make_app({'CORS_ORIGINS': 'https://hart.ai'})
        resp = client.get('/status')
        assert 'Access-Control-Allow-Origin' not in resp.headers

    def test_empty_cors_origins_blocks_all(self, make_app):
        client, _ = make_app({'CORS_ORIGINS': ''})
        resp = client.get('/status', headers={'Origin': 'https://hart.ai'})
        assert 'Access-Control-Allow-Origin' not in resp.headers

    def test_multiple_allowed_origins(self, make_app):
        client, _ = make_app({'CORS_ORIGINS': 'https://hart.ai,https://app.hart.ai'})
        resp1 = client.get('/status', headers={'Origin': 'https://hart.ai'})
        resp2 = client.get('/status', headers={'Origin': 'https://app.hart.ai'})
        assert resp1.headers.get('Access-Control-Allow-Origin') == 'https://hart.ai'
        assert resp2.headers.get('Access-Control-Allow-Origin') == 'https://app.hart.ai'

    def test_options_preflight_allowed_origin(self, make_app):
        client, _ = make_app({'CORS_ORIGINS': 'https://hart.ai'})
        resp = client.options('/chat', headers={'Origin': 'https://hart.ai'})
        assert resp.status_code == 200
        assert resp.headers.get('Access-Control-Allow-Origin') == 'https://hart.ai'

    def test_options_preflight_disallowed_origin(self, make_app):
        client, _ = make_app({'CORS_ORIGINS': 'https://hart.ai'})
        resp = client.options('/chat', headers={'Origin': 'https://evil.com'})
        assert resp.status_code == 200  # OPTIONS always 200
        assert 'Access-Control-Allow-Origin' not in resp.headers

    def test_cors_credentials_header(self, make_app):
        client, _ = make_app({'CORS_ORIGINS': 'https://hart.ai'})
        resp = client.get('/status', headers={'Origin': 'https://hart.ai'})
        assert resp.headers.get('Access-Control-Allow-Credentials') == 'true'


# ── CSRF Protection ──────────────────────────────────────────────

class TestCSRF:
    """Test CSRF protection for state-changing requests."""

    def test_get_requests_bypass_csrf(self, make_app):
        client, _ = make_app()
        resp = client.get('/chat')
        assert resp.status_code == 200

    def test_post_with_bearer_token_bypasses_csrf(self, make_app):
        client, _ = make_app()
        resp = client.post('/form-submit',
                           headers={'Authorization': 'Bearer test-token'},
                           content_type='text/plain')
        assert resp.status_code == 200

    def test_post_with_api_key_bypasses_csrf(self, make_app):
        client, _ = make_app()
        resp = client.post('/form-submit',
                           headers={'X-API-Key': 'some-key'},
                           content_type='text/plain')
        assert resp.status_code == 200

    def test_post_with_json_content_type_bypasses_csrf(self, make_app):
        client, _ = make_app()
        resp = client.post('/form-submit',
                           json={'data': 'test'})
        assert resp.status_code == 200

    def test_post_without_csrf_token_returns_403(self, make_app):
        client, _ = make_app()
        resp = client.post('/form-submit',
                           content_type='application/x-www-form-urlencoded',
                           data='field=value')
        assert resp.status_code == 403
        assert 'CSRF' in resp.get_json().get('error', '')

    def test_post_with_csrf_token_header_passes(self, make_app):
        client, _ = make_app()
        resp = client.post('/form-submit',
                           headers={'X-CSRF-Token': 'valid-token'},
                           content_type='application/x-www-form-urlencoded',
                           data='field=value')
        assert resp.status_code == 200

    def test_a2a_exempt_from_csrf(self, make_app):
        client, _ = make_app()
        resp = client.post('/a2a/test/execute',
                           content_type='application/x-www-form-urlencoded',
                           data='field=value')
        assert resp.status_code == 200

    def test_well_known_exempt_from_csrf(self, make_app):
        # .well-known is GET only in our routes, but CSRF exemption path is tested
        client, _ = make_app()
        resp = client.get('/.well-known/agent.json')
        assert resp.status_code == 200

    def test_status_exempt_from_csrf(self, make_app):
        # /status is GET, but verify the prefix is exempt
        client, _ = make_app()
        resp = client.get('/status')
        assert resp.status_code == 200


# ── Host Validation ──────────────────────────────────────────────

class TestHostValidation:
    """Test Host header injection prevention."""

    def test_valid_host_passes(self, make_app):
        client, _ = make_app({'ALLOWED_HOSTS': 'localhost,127.0.0.1'})
        resp = client.get('/status')  # test client uses localhost by default
        assert resp.status_code == 200

    def test_invalid_host_returns_400(self, make_app):
        client, _ = make_app({
            'ALLOWED_HOSTS': 'hart.ai',
            'HEVOLVE_ENV': 'production',
        })
        resp = client.get('/status', headers={'Host': 'evil.com'})
        assert resp.status_code == 400
        assert 'Invalid host' in resp.get_json().get('error', '')

    def test_development_mode_bypasses_host_check(self, make_app):
        client, _ = make_app({
            'ALLOWED_HOSTS': 'hart.ai',
            'HEVOLVE_ENV': 'development',
        })
        resp = client.get('/status', headers={'Host': 'anything.evil.com'})
        assert resp.status_code == 200

    def test_nunba_bundled_bypasses_host_check(self, make_app):
        client, _ = make_app({
            'ALLOWED_HOSTS': 'hart.ai',
            'HEVOLVE_ENV': 'production',
            'NUNBA_BUNDLED': '1',
        })
        resp = client.get('/status', headers={'Host': 'anything.evil.com'})
        assert resp.status_code == 200

    def test_host_with_port_stripped(self, make_app):
        client, _ = make_app({
            'ALLOWED_HOSTS': 'localhost',
            'HEVOLVE_ENV': 'production',
        })
        resp = client.get('/status', headers={'Host': 'localhost:6777'})
        assert resp.status_code == 200


# ── API Auth ─────────────────────────────────────────────────────

class TestAPIAuth:
    """Test opt-in API key authentication."""

    def test_no_api_key_configured_passes_all(self, make_app):
        """When HEVOLVE_API_KEY not set, middleware is a no-op (gateway handles auth)."""
        client, _ = make_app({'HEVOLVE_API_KEY': ''})
        resp = client.post('/chat', json={'prompt': 'test'})
        assert resp.status_code == 200

    def test_valid_api_key_passes(self, make_app):
        client, _ = make_app({'HEVOLVE_API_KEY': 'secret-key-123'})
        resp = client.post('/chat',
                           json={'prompt': 'test'},
                           headers={'X-API-Key': 'secret-key-123'})
        assert resp.status_code == 200

    def test_invalid_api_key_returns_401(self, make_app):
        client, _ = make_app({'HEVOLVE_API_KEY': 'secret-key-123'})
        resp = client.post('/chat',
                           json={'prompt': 'test'},
                           headers={'X-API-Key': 'wrong-key'})
        assert resp.status_code == 401

    def test_missing_api_key_returns_401(self, make_app):
        client, _ = make_app({'HEVOLVE_API_KEY': 'secret-key-123'})
        resp = client.post('/chat', json={'prompt': 'test'})
        assert resp.status_code == 401
        assert 'X-API-Key' in resp.get_json().get('error', '')

    def test_exempt_paths_skip_auth(self, make_app):
        client, _ = make_app({'HEVOLVE_API_KEY': 'secret-key-123'})
        # /status is exempt
        resp = client.get('/status')
        assert resp.status_code == 200
        # /api/social/ is exempt
        resp2 = client.get('/api/social/feed')
        assert resp2.status_code == 200

    def test_prompts_endpoint_requires_auth(self, make_app):
        client, _ = make_app({'HEVOLVE_API_KEY': 'secret-key-123'})
        resp = client.get('/prompts')
        assert resp.status_code == 401

    def test_nunba_bundled_bypasses_api_auth(self, make_app):
        client, _ = make_app({
            'HEVOLVE_API_KEY': 'secret-key-123',
            'NUNBA_BUNDLED': '1',
        })
        resp = client.post('/chat', json={'prompt': 'test'})
        assert resp.status_code == 200


# ── Constant-Time Compare ────────────────────────────────────────

class TestConstantTimeCompare:
    """Test the timing-safe string comparison."""

    def test_equal_strings(self):
        from security.middleware import _constant_time_compare
        assert _constant_time_compare('abc', 'abc') is True

    def test_unequal_strings(self):
        from security.middleware import _constant_time_compare
        assert _constant_time_compare('abc', 'xyz') is False

    def test_empty_strings(self):
        from security.middleware import _constant_time_compare
        assert _constant_time_compare('', '') is True

    def test_one_empty_one_not(self):
        from security.middleware import _constant_time_compare
        assert _constant_time_compare('', 'abc') is False
        assert _constant_time_compare('abc', '') is False


# ── Integration: Full Middleware Stack ────────────────────────────

class TestFullMiddlewareStack:
    """Test all middleware layers working together."""

    def test_all_headers_present_on_single_request(self, make_app):
        client, _ = make_app({'HEVOLVE_ENV': 'production'})
        resp = client.get('/status')
        assert resp.status_code == 200
        assert resp.headers.get('X-Frame-Options') == 'DENY'
        assert resp.headers.get('X-Content-Type-Options') == 'nosniff'
        assert 'Content-Security-Policy' in resp.headers
        assert 'Strict-Transport-Security' in resp.headers

    def test_cors_and_security_headers_coexist(self, make_app):
        client, _ = make_app({
            'CORS_ORIGINS': 'https://hart.ai',
            'HEVOLVE_ENV': 'production',
        })
        resp = client.get('/status', headers={'Origin': 'https://hart.ai'})
        assert resp.headers.get('Access-Control-Allow-Origin') == 'https://hart.ai'
        assert resp.headers.get('X-Frame-Options') == 'DENY'
