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

        @app.route('/api/admin/reset', methods=['GET', 'POST'])
        def admin_reset():
            # Expose the auth provenance the middleware stamps on `g` so
            # tests can assert the JWT branch actually ran (not just that
            # the request was let through).
            from flask import g
            return jsonify({
                'ok': True,
                'auth_source': getattr(g, 'auth_source', None),
                'jwt_payload': getattr(g, 'jwt_payload', None),
            })

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

    # ── The peer mesh: a node must answer to its OWN addresses ──────────────
    # Found in a VM, not by reading: hart-peer-discovery's "Server backend
    # accessible from edge" got HTTP 400 {"error":"Invalid host"} cross-host.
    # NO nixos module sets ALLOWED_HOSTS, so every HART OS node shipped with
    # the localhost,127.0.0.1 default and rejected every peer that addressed
    # it by LAN IP. Only the cloud deploy sets it (to '*'), which is why this
    # was invisible outside the OS.

    @pytest.mark.parametrize('host', [
        '192.168.1.42',      # RFC1918 /16 — the common home LAN
        '10.0.0.7',          # RFC1918 /8
        '172.16.5.9',        # RFC1918 /12
        '169.254.10.2',      # link-local / mDNS-discovered peer
        '127.0.0.1',         # loopback
    ])
    def test_a_peer_addressing_this_node_by_private_ip_is_accepted(
            self, make_app, host):
        client, _ = make_app({
            'ALLOWED_HOSTS': 'hart.ai',      # deliberately does NOT list it
            'HEVOLVE_ENV': 'production',
        })
        resp = client.get('/status', headers={'Host': host})
        assert resp.status_code == 200, (
            f'{host} is a private address that can only mean this LAN; '
            'rejecting it breaks peer-to-peer reachability')

    @pytest.mark.parametrize('host', [
        'evil.com',          # the classic reflected-Host target
        '8.8.8.8',           # PUBLIC literal — not ours, still rejected
        '1.2.3.4',
    ])
    def test_public_addresses_and_names_are_still_rejected(self, make_app, host):
        """The injection defence must survive the peer-mesh fix."""
        client, _ = make_app({
            'ALLOWED_HOSTS': 'hart.ai',
            'HEVOLVE_ENV': 'production',
        })
        resp = client.get('/status', headers={'Host': host})
        assert resp.status_code == 400, (
            f'{host} is not this node and not this LAN — accepting it would '
            'reopen the reflected-Host hole the validation exists to close')

    def test_the_node_answers_to_its_own_hostname(self, make_app):
        import socket
        own = socket.gethostname().split('.')[0]
        client, _ = make_app({
            'ALLOWED_HOSTS': 'hart.ai',
            'HEVOLVE_ENV': 'production',
        })
        resp = client.get('/status', headers={'Host': own})
        assert resp.status_code == 200, (
            'a node that will not answer to its own hostname cannot be '
            'reached by name on the LAN')

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

    def test_wildcard_allows_any_host(self, make_app):
        # ALLOWED_HOSTS='*' is the deploy default (deploy-hartos-deepbox.yml:
        # `secrets.ALLOWED_HOSTS || '*'`). It must mean "allow all" (Django
        # ALLOWED_HOSTS semantics) — otherwise the PUBLIC /api/ota/latest poll
        # that fleet nodes hit (Host: etime.hertzai.com) is rejected 400 and OTA
        # delivery breaks.
        client, _ = make_app({
            'ALLOWED_HOSTS': '*',
            'HEVOLVE_ENV': 'production',
        })
        resp = client.get('/status', headers={'Host': 'etime.hertzai.com'})
        assert resp.status_code == 200

    def test_wildcard_in_list_allows_any_host(self, make_app):
        client, _ = make_app({
            'ALLOWED_HOSTS': 'localhost,*',
            'HEVOLVE_ENV': 'production',
        })
        resp = client.get('/status', headers={'Host': 'anything.hertzai.com'})
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


# ── Admin Gate ───────────────────────────────────────────────────
#
# /api/admin/* modifies persistent state, so it ALWAYS requires auth on
# every tier (flat / regional / central) — the tier model only relaxes
# the *user-facing* API. The only bypass is bundled desktop
# (NUNBA_BUNDLED), which is in-process single-user with no network
# exposure. Before this class there was no /api/admin route in the
# fixture and therefore NO test that admin auth is enforced — a
# regression that dropped the admin gate would have shipped green.

class TestAdminGate:
    """Test that /api/admin/* is auth-gated regardless of node tier."""

    def test_admin_no_key_flat_tier_still_gated(self, make_app):
        """The core invariant: even flat/LAN-trusted tier with NO
        HEVOLVE_API_KEY configured must reject an unauthenticated admin
        request. LAN trust is explicitly not enough for admin ops."""
        client, _ = make_app({'HEVOLVE_API_KEY': ''})  # flat is the default tier
        resp = client.get('/api/admin/reset')
        assert resp.status_code == 401, (
            'admin ops mutate persistent state; a no-key flat node must '
            'still refuse an unauthenticated admin request')
        assert 'Bearer' in resp.get_json().get('error', '')

    def test_admin_no_key_regional_tier_still_gated(self, make_app):
        """Regional tier relaxes /chat (LAN-trusted) but NOT admin."""
        client, _ = make_app({
            'HEVOLVE_API_KEY': '',
            'HEVOLVE_NODE_TIER': 'regional',
        })
        resp = client.get('/api/admin/reset')
        assert resp.status_code == 401

    def test_admin_no_key_central_tier_gated(self, make_app):
        client, _ = make_app({
            'HEVOLVE_API_KEY': '',
            'HEVOLVE_NODE_TIER': 'central',
        })
        resp = client.get('/api/admin/reset')
        assert resp.status_code == 401

    def test_admin_gate_precedes_routing(self, make_app):
        """A bare /api/admin (no matching view) must 401, not 404 —
        proving the gate runs in before_request, ahead of the router,
        so no admin subpath can slip through by simply not existing."""
        client, _ = make_app({'HEVOLVE_API_KEY': ''})
        resp = client.get('/api/admin')
        assert resp.status_code == 401

    def test_admin_valid_api_key_passes(self, make_app):
        client, _ = make_app({'HEVOLVE_API_KEY': 'admin-secret-42'})
        resp = client.get('/api/admin/reset',
                          headers={'X-API-Key': 'admin-secret-42'})
        assert resp.status_code == 200
        assert resp.get_json().get('ok') is True

    def test_admin_wrong_api_key_no_bearer_returns_401(self, make_app):
        client, _ = make_app({'HEVOLVE_API_KEY': 'admin-secret-42'})
        resp = client.get('/api/admin/reset',
                          headers={'X-API-Key': 'nope'})
        assert resp.status_code == 401

    def test_admin_nunba_bundled_bypasses_gate(self, make_app):
        """Bundled desktop is the single documented exception."""
        client, _ = make_app({
            'HEVOLVE_API_KEY': 'admin-secret-42',
            'NUNBA_BUNDLED': '1',
        })
        resp = client.get('/api/admin/reset')
        assert resp.status_code == 200

    def test_admin_prefix_lookalike_not_gated(self, make_app):
        """`/api/administrators` shares a string prefix with `/api/admin`
        but is a DIFFERENT path segment — it must NOT be swept into the
        admin gate. With no matching route it should 404 (public,
        gate passes → router), never 401 (wrongly gated). Locks the
        exact-segment matching in _path_matches_any."""
        client, _ = make_app({'HEVOLVE_API_KEY': ''})
        resp = client.get('/api/administrators')
        assert resp.status_code == 404, (
            'segment-precise matching regressed: a lookalike path is being '
            'treated as an admin path')


# ── Bearer JWT Verification ──────────────────────────────────────
#
# The admin/network gates accept a Bearer token ONLY after decode_jwt()
# actually verifies it — a prefix-only "startswith('Bearer ')" check
# would let `Bearer garbage` walk straight through the admin gate. These
# tests pin that decode_jwt is genuinely invoked and its verdict honored,
# in both the mocked (deterministic) and real end-to-end forms.

class TestBearerJWTVerification:
    """Test the Bearer-JWT branch of the admin/network auth gate."""

    def test_valid_jwt_passes_admin_gate_and_stamps_g(self, make_app):
        client, _ = make_app({'HEVOLVE_API_KEY': ''})
        fake_payload = {'user_id': 7, 'scope': 'local'}
        with patch('integrations.social.auth.decode_jwt',
                   return_value=fake_payload) as mock_decode:
            resp = client.post('/api/admin/reset',
                               headers={'Authorization': 'Bearer good.jwt.token'})
        assert resp.status_code == 200
        # Proves the decode path ran on the EXACT token (not a prefix check).
        mock_decode.assert_called_once_with('good.jwt.token')
        body = resp.get_json()
        assert body.get('auth_source') == 'jwt'
        assert body.get('jwt_payload') == fake_payload

    def test_garbage_bearer_rejected_on_admin_gate(self, make_app):
        """THE regression guard: decode_jwt returns {} for a bad token,
        and the gate must 401. If someone reverts to a prefix-only Bearer
        check, decode_jwt would not be consulted and this would 200."""
        client, _ = make_app({'HEVOLVE_API_KEY': ''})
        with patch('integrations.social.auth.decode_jwt',
                   return_value={}) as mock_decode:
            resp = client.post('/api/admin/reset',
                               headers={'Authorization': 'Bearer garbage'})
        assert resp.status_code == 401
        mock_decode.assert_called_once_with('garbage')
        assert 'Invalid or expired' in resp.get_json().get('error', '')

    def test_decode_jwt_raising_is_treated_as_invalid(self, make_app):
        """A crashing decoder must fail closed (401), never fail open."""
        client, _ = make_app({'HEVOLVE_API_KEY': ''})
        with patch('integrations.social.auth.decode_jwt',
                   side_effect=RuntimeError('boom')):
            resp = client.post('/api/admin/reset',
                               headers={'Authorization': 'Bearer x.y.z'})
        assert resp.status_code == 401

    def test_empty_bearer_token_rejected(self, make_app):
        """`Authorization: Bearer ` (empty token) must not pass."""
        client, _ = make_app({'HEVOLVE_API_KEY': ''})
        with patch('integrations.social.auth.decode_jwt',
                   return_value={}) as mock_decode:
            resp = client.post('/api/admin/reset',
                               headers={'Authorization': 'Bearer '})
        assert resp.status_code == 401
        # The empty string after "Bearer " is what gets handed to the decoder.
        mock_decode.assert_called_once_with('')

    def test_real_decode_jwt_rejects_garbage_end_to_end(self, make_app):
        """No mock: the REAL decode_jwt chain must reject a bogus token on
        the admin gate. Exercises the actual import + call the middleware
        performs, so the mocked tests can't drift from reality."""
        client, _ = make_app({'HEVOLVE_API_KEY': ''})
        resp = client.post('/api/admin/reset',
                           headers={'Authorization': 'Bearer not.a.real.jwt'})
        assert resp.status_code == 401

    def test_api_key_deploy_still_accepts_valid_jwt(self, make_app):
        """When HEVOLVE_API_KEY is set, a request with NO X-API-Key but a
        valid Bearer JWT still authenticates (admin UI / k8s probe case)."""
        client, _ = make_app({'HEVOLVE_API_KEY': 'admin-secret-42'})
        with patch('integrations.social.auth.decode_jwt',
                   return_value={'user_id': 1}):
            resp = client.get('/api/admin/reset',
                              headers={'Authorization': 'Bearer valid.token'})
        assert resp.status_code == 200
        assert resp.get_json().get('auth_source') == 'jwt'

    def test_wrong_api_key_but_valid_jwt_falls_through_to_pass(self, make_app):
        """A wrong X-API-Key must not veto an otherwise-valid Bearer JWT —
        the gate falls through from the key check to the JWT check."""
        client, _ = make_app({'HEVOLVE_API_KEY': 'admin-secret-42'})
        with patch('integrations.social.auth.decode_jwt',
                   return_value={'user_id': 1}):
            resp = client.get('/api/admin/reset',
                              headers={'X-API-Key': 'wrong',
                                       'Authorization': 'Bearer valid.token'})
        assert resp.status_code == 200

    def test_network_gate_central_tier_valid_jwt_passes(self, make_app):
        """The same decode path guards the user-facing network gate on
        central tier."""
        client, _ = make_app({
            'HEVOLVE_API_KEY': '',
            'HEVOLVE_NODE_TIER': 'central',
        })
        with patch('integrations.social.auth.decode_jwt',
                   return_value={'user_id': 3}) as mock_decode:
            resp = client.post('/chat',
                               json={'prompt': 'hi'},
                               headers={'Authorization': 'Bearer good.token'})
        assert resp.status_code == 200
        mock_decode.assert_called_once_with('good.token')

    def test_network_gate_central_tier_garbage_jwt_rejected(self, make_app):
        client, _ = make_app({
            'HEVOLVE_API_KEY': '',
            'HEVOLVE_NODE_TIER': 'central',
        })
        with patch('integrations.social.auth.decode_jwt', return_value={}):
            resp = client.post('/chat',
                               json={'prompt': 'hi'},
                               headers={'Authorization': 'Bearer garbage'})
        assert resp.status_code == 401
