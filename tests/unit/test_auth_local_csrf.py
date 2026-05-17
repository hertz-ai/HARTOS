"""HARTOS — auth_local CSRF defense-in-depth (Phase 9.5).

Plan reference: hardening backlog item from sunny-gliding-eich.md.

Coverage:
  - require_local_or_token_csrf_safe accepts a same-origin browser POST
    (Origin matches request Host).
  - Rejects a cross-origin browser POST with a non-loopback Origin.
  - Accepts a non-browser client (no Origin / no Referer) — curl,
    native desktop apps, server-to-server.
  - Bearer-token caller bypasses the CSRF gate (already authenticated).
  - Loopback Origin (http://127.0.0.1:5000, http://localhost:3000) is
    always accepted regardless of Host.
  - HARTOS_TRUSTED_ORIGINS env var extends the allow-list.
  - Falls back to Referer when Origin is absent.
  - Original require_local_or_token decorator is unchanged (regression).
"""
from __future__ import annotations

import os
import sys

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def app():
    """Minimal Flask app exposing both decorators on /test routes."""
    from flask import Flask, jsonify
    from core import auth_local
    # Re-read API_TOKEN per test in case env changed.
    auth_local.API_TOKEN = os.environ.get('HARTOS_API_TOKEN', '')

    flask_app = Flask(__name__)

    @flask_app.route('/test/csrf-safe', methods=['POST'])
    @auth_local.require_local_or_token_csrf_safe
    def csrf_safe_route():
        return jsonify({'ok': True}), 200

    @flask_app.route('/test/local-only', methods=['POST'])
    @auth_local.require_local_or_token
    def local_only_route():
        return jsonify({'ok': True}), 200

    return flask_app


# ── Same-origin browser POSTs ──────────────────────────────────────


def test_same_origin_origin_header_accepted(app):
    """Browser POST from a page served by the same Host — Origin
    matches request.host_url, accept."""
    client = app.test_client()
    resp = client.post('/test/csrf-safe',
                       headers={'Origin': 'http://localhost'})
    assert resp.status_code == 200


def test_loopback_origin_127_0_0_1_accepted(app):
    client = app.test_client()
    resp = client.post('/test/csrf-safe',
                       headers={'Origin': 'http://127.0.0.1:5000'})
    assert resp.status_code == 200


def test_loopback_ipv6_origin_accepted(app):
    client = app.test_client()
    resp = client.post('/test/csrf-safe',
                       headers={'Origin': 'http://[::1]:5000'})
    assert resp.status_code == 200


# ── Cross-origin attack ────────────────────────────────────────────


def test_cross_origin_origin_header_rejected(app):
    """A page on https://attacker.example tries to POST to localhost.
    Browser sends Origin: https://attacker.example.  Reject."""
    client = app.test_client()
    resp = client.post('/test/csrf-safe',
                       headers={'Origin': 'https://attacker.example'})
    assert resp.status_code == 403
    assert resp.get_json()['error'] == 'forbidden'


def test_cross_origin_referer_only_rejected(app):
    """Older browsers / Electron paths may send only Referer.  Same
    rule applies — non-loopback Referer host rejects."""
    client = app.test_client()
    resp = client.post('/test/csrf-safe',
                       headers={'Referer': 'https://attacker.example/page'})
    assert resp.status_code == 403


def test_null_origin_rejected(app):
    """Browser sends 'Origin: null' for opaque origins (file://,
    sandboxed iframes).  Treat as untrusted."""
    client = app.test_client()
    resp = client.post('/test/csrf-safe',
                       headers={'Origin': 'null'})
    assert resp.status_code == 403


# ── Non-browser clients ────────────────────────────────────────────


def test_no_origin_no_referer_accepted(app):
    """curl / native desktop / server-to-server clients don't send
    Origin or Referer.  Browsers always send at least Origin on
    cross-origin POST — absence is a strong signal it's not a
    browser-driven attack."""
    client = app.test_client()
    resp = client.post('/test/csrf-safe')
    assert resp.status_code == 200


# ── Bearer token bypass ────────────────────────────────────────────


def test_bearer_token_bypasses_csrf_check(app, monkeypatch):
    """Authenticated callers (server-to-server with HARTOS_API_TOKEN)
    skip the CSRF gate — token possession proves it's not a
    cross-origin browser."""
    from core import auth_local
    monkeypatch.setattr(auth_local, 'API_TOKEN', 'secret-token-123')
    client = app.test_client()
    # Even with a "malicious" Origin, the bearer token bypasses CSRF.
    resp = client.post('/test/csrf-safe',
                       headers={'Origin': 'https://attacker.example',
                                'Authorization': 'Bearer secret-token-123'})
    assert resp.status_code == 200


def test_wrong_bearer_token_falls_through_to_csrf(app, monkeypatch):
    from core import auth_local
    monkeypatch.setattr(auth_local, 'API_TOKEN', 'secret-token-123')
    client = app.test_client()
    resp = client.post('/test/csrf-safe',
                       headers={'Origin': 'https://attacker.example',
                                'Authorization': 'Bearer wrong-token'})
    # Falls through to CSRF check, which rejects the non-loopback Origin.
    assert resp.status_code == 403


# ── HARTOS_TRUSTED_ORIGINS extension ───────────────────────────────


def test_trusted_origins_env_extends_allow_list(app, monkeypatch):
    monkeypatch.setenv('HARTOS_TRUSTED_ORIGINS',
                       'https://nunba.local,https://hevolve.ai')
    client = app.test_client()
    resp = client.post('/test/csrf-safe',
                       headers={'Origin': 'https://nunba.local'})
    assert resp.status_code == 200
    resp2 = client.post('/test/csrf-safe',
                       headers={'Origin': 'https://hevolve.ai'})
    assert resp2.status_code == 200


def test_trusted_origins_empty_env_does_not_open_holes(app, monkeypatch):
    monkeypatch.setenv('HARTOS_TRUSTED_ORIGINS', '')
    client = app.test_client()
    resp = client.post('/test/csrf-safe',
                       headers={'Origin': 'https://attacker.example'})
    assert resp.status_code == 403


# ── Original decorator unchanged (regression) ──────────────────────


def test_original_decorator_accepts_no_csrf_check(app):
    """require_local_or_token (the original, non-CSRF-safe variant)
    must be unchanged: a browser POST with a malicious Origin still
    passes IF remote_addr is localhost.  This is the documented
    'partial' coverage in the module docstring; CSRF defense is
    opt-in via the _csrf_safe variant."""
    client = app.test_client()
    resp = client.post('/test/local-only',
                       headers={'Origin': 'https://attacker.example'})
    # Original decorator passes — this is the documented gap that
    # the new decorator is meant to close on opt-in routes.
    assert resp.status_code == 200


# ── _origin_host parser ────────────────────────────────────────────


def test_origin_host_parser():
    from core.auth_local import _origin_host
    assert _origin_host('https://hevolve.ai/path?q=1') == 'hevolve.ai'
    assert _origin_host('http://127.0.0.1:5000') == '127.0.0.1'
    assert _origin_host('http://[::1]:5000') == '::1'
    assert _origin_host('null') == ''
    assert _origin_host('') == ''
    assert _origin_host('not a url') == ''


def test_module_imports_cleanly():
    from core import auth_local  # noqa: F401
    assert hasattr(auth_local, 'require_local_or_token_csrf_safe')
    assert hasattr(auth_local, '_is_safe_csrf_origin')
