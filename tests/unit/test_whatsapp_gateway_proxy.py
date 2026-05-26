"""Unit tests for the WhatsApp gateway proxy routes added to
integrations/social/api_channels.py (#225).

What these tests pin:
  * GET  /api/social/channels/whatsapp/qr        — proxies to the
    embedded Baileys gateway, returns the QR string + auth state.
  * POST /api/social/channels/whatsapp/pair-code — proxies the
    "Link with phone number" 8-char code path.
  * Gateway-unreachable maps to HTTP 503 with a clear error message
    (does NOT crash the wizard).
  * Account-id derivation is stable per-user so Baileys' on-disk
    creds at ~/.hevolve/whatsapp/auth/<account_id>/ survive restart.
  * WhatsApp catalog entry uses auth_method='gateway_qr' — guards the
    wizard's render-branch contract from drifting back to qr_session.

The proxy is exercised against a mocked _proxy_gateway so we don't
spawn Node or hit the network.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


class CatalogContractTests(unittest.TestCase):
    """The wizard branches on auth_method — guard the value here so a
    well-meaning refactor can't silently send WhatsApp back to the
    generic qr_session/QRPairingDisplay path."""

    def test_whatsapp_auth_method_is_gateway_qr(self):
        from integrations.channels.metadata import CHANNEL_CATALOG
        self.assertIn('whatsapp', CHANNEL_CATALOG)
        self.assertEqual(
            CHANNEL_CATALOG['whatsapp']['auth_method'],
            'gateway_qr',
            "WhatsApp must use gateway_qr (embedded Baileys gateway), not "
            "qr_session (Hevolve device pair) — see #225.",
        )

    def test_qr_session_channels_unchanged(self):
        """telegram_user / discord_user still use qr_session — the
        change must NOT have stolen their wizard branch."""
        from integrations.channels.metadata import CHANNEL_CATALOG
        self.assertEqual(CHANNEL_CATALOG['telegram_user']['auth_method'], 'qr_session')
        self.assertEqual(CHANNEL_CATALOG['discord_user']['auth_method'], 'qr_session')


class _FakeUser:
    id = 42


class _FakeDB:
    """Minimal duck-type matching what the require_auth wrapper +
    proxy routes ever touch on g.db.  The proxy routes do not query
    the DB at all, but the require_auth wrapper does commit() /
    rollback() / close() / is_active around them."""

    is_active = True

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


def _bypass_auth(monkeypatched_modules: dict) -> None:
    """Patch ``_get_user_from_token`` so any Bearer token resolves to
    a fake user.  This keeps the @require_auth wrapper exactly as
    production runs it — we just stub the credential-check seam.
    """
    from integrations.social import auth as _auth

    def _fake_get(_token):
        return _FakeUser(), _FakeDB()

    monkeypatched_modules['orig'] = _auth._get_user_from_token
    _auth._get_user_from_token = _fake_get


def _restore_auth(monkeypatched_modules: dict) -> None:
    from integrations.social import auth as _auth
    if 'orig' in monkeypatched_modules:
        _auth._get_user_from_token = monkeypatched_modules['orig']


def _build_test_app():
    """Minimal Flask app with channel_user_bp mounted.  The proxy
    routes carry @require_auth — tests pass a fake Bearer token and
    rely on the per-test _bypass_auth() shim."""
    from flask import Flask
    from integrations.social import api_channels as ac

    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test'
    app.register_blueprint(ac.channel_user_bp)
    return app


_AUTH_HEADERS = {'Authorization': 'Bearer test-token'}


class ProxyRouteTests(unittest.TestCase):

    def setUp(self):
        self.app = _build_test_app()
        self.client = self.app.test_client()
        self._auth_state = {}
        _bypass_auth(self._auth_state)

    def tearDown(self):
        _restore_auth(self._auth_state)

    def test_account_id_is_stable_per_user(self):
        """Two requests from the same logged-in user must yield the
        same Baileys account id so creds at
        ~/.hevolve/whatsapp/auth/<id>/ persist across calls."""
        from integrations.social import api_channels as ac
        seen = []

        def _capture_proxy(method, path, **_):
            seen.append(path)
            return {'qr': 'WA-QR-STR', 'authenticated': False, 'state': 'connecting'}, 200

        with mock.patch.object(ac, '_proxy_gateway', side_effect=_capture_proxy):
            r1 = self.client.get('/api/social/channels/whatsapp/qr', headers=_AUTH_HEADERS)
            r2 = self.client.get('/api/social/channels/whatsapp/qr', headers=_AUTH_HEADERS)

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        # 2 GET-qr calls → 4 proxy calls (start + status, twice).  All
        # must reference the SAME account id.
        ids = {p.split('/')[3] for p in seen if p.startswith('/api/sessions/')}
        self.assertEqual(len(ids), 1, f"account_id drifted across calls: {seen}")
        self.assertTrue(next(iter(ids)).startswith('user_'))

    def test_qr_endpoint_returns_baileys_qr_string(self):
        from integrations.social import api_channels as ac

        def _stub(method, path, **_):
            if path.endswith('/start'):
                return {'success': True}, 201
            if path.endswith('/status'):
                return {'qr': 'WHATSAPP-WEB-QR-DATA', 'authenticated': False, 'state': 'connecting'}, 200
            return {}, 404

        with mock.patch.object(ac, '_proxy_gateway', side_effect=_stub):
            r = self.client.get('/api/social/channels/whatsapp/qr', headers=_AUTH_HEADERS)

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(body['data']['qr'], 'WHATSAPP-WEB-QR-DATA')
        self.assertFalse(body['data']['authenticated'])

    def test_qr_endpoint_reports_authenticated_state(self):
        """After the user scans the QR the gateway flips authenticated
        to True and clears the QR — the proxy must surface both."""
        from integrations.social import api_channels as ac

        def _stub(method, path, **_):
            return {'qr': None, 'authenticated': True, 'state': 'connected'}, 200

        with mock.patch.object(ac, '_proxy_gateway', side_effect=_stub):
            r = self.client.get('/api/social/channels/whatsapp/qr', headers=_AUTH_HEADERS)

        body = r.get_json()['data']
        self.assertTrue(body['authenticated'])
        self.assertIsNone(body['qr'])

    def test_qr_endpoint_returns_503_when_gateway_unreachable(self):
        from integrations.social import api_channels as ac

        with mock.patch.object(ac, '_proxy_gateway', return_value=(None, 503)):
            r = self.client.get('/api/social/channels/whatsapp/qr', headers=_AUTH_HEADERS)

        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertFalse(body['success'])
        # User-facing error must mention what's wrong + how to fix it.
        self.assertIn('gateway', body['error'].lower())

    def test_pair_code_endpoint_requires_phone(self):
        r = self.client.post(
            '/api/social/channels/whatsapp/pair-code',
            data=json.dumps({}),
            content_type='application/json',
            headers=_AUTH_HEADERS,
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn('phone', r.get_json()['error'].lower())

    def test_pair_code_endpoint_returns_baileys_code(self):
        from integrations.social import api_channels as ac

        def _stub(method, path, **kwargs):
            if path.endswith('/request-pair-code'):
                self.assertEqual(kwargs.get('json', {}).get('phone'), '+91 90030 54371')
                return {'success': True, 'code': 'ABCD1234'}, 200
            return {'success': True}, 201

        with mock.patch.object(ac, '_proxy_gateway', side_effect=_stub):
            r = self.client.post(
                '/api/social/channels/whatsapp/pair-code',
                data=json.dumps({'phone': '+91 90030 54371'}),
                content_type='application/json',
                headers=_AUTH_HEADERS,
            )

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(body['data']['code'], 'ABCD1234')

    def test_pair_code_endpoint_passes_through_gateway_errors(self):
        from integrations.social import api_channels as ac

        def _stub(method, path, **_):
            if path.endswith('/request-pair-code'):
                return {'error': 'already_authenticated'}, 409
            return {'success': True}, 201

        with mock.patch.object(ac, '_proxy_gateway', side_effect=_stub):
            r = self.client.post(
                '/api/social/channels/whatsapp/pair-code',
                data=json.dumps({'phone': '+91 9003054371'}),
                content_type='application/json',
                headers=_AUTH_HEADERS,
            )

        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()['error'], 'already_authenticated')


class GatewayBaseResolutionTests(unittest.TestCase):
    """The proxy's base URL must follow WHATSAPP_API_URL when set
    (operator-managed remote WAHA) and default to the embedded gateway
    otherwise.  Same single-source-of-truth rule the adapter uses."""

    def setUp(self):
        self._saved = {
            'WHATSAPP_API_URL': os.environ.get('WHATSAPP_API_URL'),
            'WHATSAPP_GATEWAY_PORT': os.environ.get('WHATSAPP_GATEWAY_PORT'),
        }
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_is_embedded_gateway(self):
        from integrations.social.api_channels import _whatsapp_gateway_base
        self.assertEqual(_whatsapp_gateway_base(), 'http://127.0.0.1:3000')

    def test_custom_port_via_env(self):
        os.environ['WHATSAPP_GATEWAY_PORT'] = '3030'
        from integrations.social.api_channels import _whatsapp_gateway_base
        self.assertEqual(_whatsapp_gateway_base(), 'http://127.0.0.1:3030')

    def test_remote_waha_override(self):
        os.environ['WHATSAPP_API_URL'] = 'https://waha.example.com'
        from integrations.social.api_channels import _whatsapp_gateway_base
        self.assertEqual(_whatsapp_gateway_base(), 'https://waha.example.com')


if __name__ == '__main__':
    unittest.main()
