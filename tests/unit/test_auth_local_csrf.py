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


# ════════════════════════════════════════════════════════════════════
# Auth-bypass paths that never ran before: the whole remote-caller side
# of require_local_or_token and the TRUSTED_PROXY / X-Forwarded-For
# trust decision inside _is_local_request.  Every test above lands on
# the default remote_addr=127.0.0.1, so these branches were unasserted.
#
# We drive them behaviourally through the real Flask routes on the
# `app` fixture, overriding REMOTE_ADDR via the WSGI environ so the
# real request.remote_addr / request.headers flow through the real
# decorator code (no monkeypatching of _is_local_request itself).
# ════════════════════════════════════════════════════════════════════

# A stable non-loopback source address for "remote caller" tests.
REMOTE_IP = '203.0.113.9'          # TEST-NET-3, never routable
REMOTE = {'REMOTE_ADDR': REMOTE_IP}
PROXY_IP = '10.0.0.1'


# ── _is_local_request: direct remote_addr branch ───────────────────


def test_ipv6_loopback_remote_addr_accepted(app):
    """::1 is localhost too — the decorator accepts it with no token."""
    client = app.test_client()
    resp = client.post('/test/local-only',
                       environ_base={'REMOTE_ADDR': '::1'})
    assert resp.status_code == 200


def test_remote_addr_non_loopback_rejected_without_token(app):
    """A genuinely remote caller with no token configured → 401 with the
    documented JSON body.  This is the core auth-bypass guard."""
    client = app.test_client()
    resp = client.post('/test/local-only', environ_base=REMOTE)
    assert resp.status_code == 401
    body = resp.get_json()
    assert body['error'] == 'unauthorized'
    assert 'HARTOS_API_TOKEN' in body['message']


# ── require_local_or_token: remote Bearer-token branch ─────────────


def test_remote_valid_bearer_accepted_constant_time(app, monkeypatch):
    """Remote caller presenting the correct Bearer token is accepted
    (the hmac.compare_digest constant-time accept path)."""
    from core import auth_local
    monkeypatch.setattr(auth_local, 'API_TOKEN', 'secret-token-123')
    client = app.test_client()
    resp = client.post('/test/local-only', environ_base=REMOTE,
                       headers={'Authorization': 'Bearer secret-token-123'})
    assert resp.status_code == 200


def test_remote_wrong_bearer_rejected(app, monkeypatch):
    from core import auth_local
    monkeypatch.setattr(auth_local, 'API_TOKEN', 'secret-token-123')
    client = app.test_client()
    resp = client.post('/test/local-only', environ_base=REMOTE,
                       headers={'Authorization': 'Bearer wrong-token'})
    assert resp.status_code == 401
    assert resp.get_json()['error'] == 'unauthorized'


def test_remote_with_token_configured_but_no_auth_header_rejected(app,
                                                                  monkeypatch):
    from core import auth_local
    monkeypatch.setattr(auth_local, 'API_TOKEN', 'secret-token-123')
    client = app.test_client()
    resp = client.post('/test/local-only', environ_base=REMOTE)
    assert resp.status_code == 401


def test_remote_non_bearer_auth_scheme_rejected(app, monkeypatch):
    """Authorization present but not a Bearer scheme (e.g. Basic) must
    not match — startswith('Bearer ') guard."""
    from core import auth_local
    monkeypatch.setattr(auth_local, 'API_TOKEN', 'secret-token-123')
    client = app.test_client()
    resp = client.post('/test/local-only', environ_base=REMOTE,
                       headers={'Authorization': 'Basic secret-token-123'})
    assert resp.status_code == 401


def test_remote_bearer_but_no_token_configured_rejected(app):
    """When API_TOKEN is unset (default), even a well-formed Bearer
    header cannot grant remote access — the `if API_TOKEN:` guard is
    false, so we never even compare.  Guards against an empty-secret
    bypass."""
    client = app.test_client()  # API_TOKEN defaults to '' in the fixture
    resp = client.post('/test/local-only', environ_base=REMOTE,
                       headers={'Authorization': 'Bearer '})  # empty == ''
    assert resp.status_code == 401


def test_remote_empty_bearer_value_rejected(app, monkeypatch):
    """`Authorization: Bearer ` (empty token after the space) must not
    match a configured non-empty API_TOKEN."""
    from core import auth_local
    monkeypatch.setattr(auth_local, 'API_TOKEN', 'secret-token-123')
    client = app.test_client()
    resp = client.post('/test/local-only', environ_base=REMOTE,
                       headers={'Authorization': 'Bearer '})
    assert resp.status_code == 401


# ── EDGE-CASE BUG: non-ASCII Bearer token must not 500 ─────────────
#
# hmac.compare_digest(str, str) is ASCII-only; a non-ASCII candidate
# raises TypeError, which escaped the decorator as an unhandled 500
# instead of a clean 401.  A remote caller controls the Authorization
# header, so this is an attacker-reachable error-handling defect.


def test_remote_non_ascii_bearer_returns_401_not_500(app, monkeypatch):
    from core import auth_local
    monkeypatch.setattr(auth_local, 'API_TOKEN', 'secret-token-123')
    client = app.test_client()
    resp = client.post('/test/local-only', environ_base=REMOTE,
                       headers={'Authorization': 'Bearer ünïcodé'})
    assert resp.status_code == 401
    assert resp.get_json()['error'] == 'unauthorized'


def test_csrf_safe_non_ascii_bearer_does_not_500(app, monkeypatch):
    """Same non-ASCII-token defect at the second call site inside
    require_local_or_token_csrf_safe.  A local (127.0.0.1) caller with a
    configured token and a junk non-ASCII Bearer header should fall
    through to the local+CSRF path (200 here, no Origin), never 500."""
    from core import auth_local
    monkeypatch.setattr(auth_local, 'API_TOKEN', 'secret-token-123')
    client = app.test_client()  # default remote_addr = 127.0.0.1 (local)
    resp = client.post('/test/csrf-safe',
                       headers={'Authorization': 'Bearer ünïcodé'})
    assert resp.status_code != 500
    assert resp.status_code == 200


# ── _is_local_request: TRUSTED_PROXY + X-Forwarded-For decision ────


def test_trusted_proxy_forwarded_loopback_accepted(app, monkeypatch):
    """Behind a trusted reverse proxy, the real client IP arrives in
    X-Forwarded-For.  Proxy addr matches TRUSTED_PROXY and XFF is
    loopback → treat as local, accept without a token."""
    monkeypatch.setenv('TRUSTED_PROXY', PROXY_IP)
    client = app.test_client()
    resp = client.post('/test/local-only',
                       environ_base={'REMOTE_ADDR': PROXY_IP},
                       headers={'X-Forwarded-For': '127.0.0.1'})
    assert resp.status_code == 200


def test_trusted_proxy_forwarded_remote_rejected(app, monkeypatch):
    """Proxy is trusted but the forwarded client is a remote IP →
    NOT local → 401 (no token)."""
    monkeypatch.setenv('TRUSTED_PROXY', PROXY_IP)
    client = app.test_client()
    resp = client.post('/test/local-only',
                       environ_base={'REMOTE_ADDR': PROXY_IP},
                       headers={'X-Forwarded-For': '203.0.113.55'})
    assert resp.status_code == 401


def test_trusted_proxy_uses_first_forwarded_hop(app, monkeypatch):
    """XFF can be a comma list (client, proxy1, proxy2).  The original
    client is the FIRST hop; a loopback first hop is accepted even when
    later hops are non-loopback."""
    monkeypatch.setenv('TRUSTED_PROXY', PROXY_IP)
    client = app.test_client()
    resp = client.post('/test/local-only',
                       environ_base={'REMOTE_ADDR': PROXY_IP},
                       headers={'X-Forwarded-For': '127.0.0.1, 10.0.0.9'})
    assert resp.status_code == 200


def test_trusted_proxy_forwarded_ipv6_loopback_accepted(app, monkeypatch):
    monkeypatch.setenv('TRUSTED_PROXY', PROXY_IP)
    client = app.test_client()
    resp = client.post('/test/local-only',
                       environ_base={'REMOTE_ADDR': PROXY_IP},
                       headers={'X-Forwarded-For': '::1'})
    assert resp.status_code == 200


def test_trusted_proxy_forwarded_localhost_string_accepted(app, monkeypatch):
    """The literal token 'localhost' is in the accepted forwarded set."""
    monkeypatch.setenv('TRUSTED_PROXY', PROXY_IP)
    client = app.test_client()
    resp = client.post('/test/local-only',
                       environ_base={'REMOTE_ADDR': PROXY_IP},
                       headers={'X-Forwarded-For': 'localhost'})
    assert resp.status_code == 200


def test_trusted_proxy_empty_forwarded_header_rejected(app, monkeypatch):
    """Proxy matches but no XFF present → forwarded_for is '' → not in
    the loopback set → NOT local → 401.  A misconfigured proxy that
    strips XFF must fail closed, not open."""
    monkeypatch.setenv('TRUSTED_PROXY', PROXY_IP)
    client = app.test_client()
    resp = client.post('/test/local-only',
                       environ_base={'REMOTE_ADDR': PROXY_IP})
    assert resp.status_code == 401


def test_untrusted_source_xff_spoofing_ignored(app, monkeypatch):
    """SECURITY: with TRUSTED_PROXY unset (default), X-Forwarded-For is
    NOT consulted at all — a remote attacker cannot forge XFF: 127.0.0.1
    to impersonate localhost."""
    monkeypatch.delenv('TRUSTED_PROXY', raising=False)
    client = app.test_client()
    resp = client.post('/test/local-only', environ_base=REMOTE,
                       headers={'X-Forwarded-For': '127.0.0.1'})
    assert resp.status_code == 401


def test_proxy_configured_but_request_not_from_proxy_ignores_xff(app,
                                                                 monkeypatch):
    """SECURITY: TRUSTED_PROXY is set, but the request arrives directly
    from a non-proxy remote IP.  Since remote_addr != TRUSTED_PROXY, the
    XFF header is ignored and the direct remote_addr governs → 401.
    A non-proxy remote cannot spoof XFF to gain local trust."""
    monkeypatch.setenv('TRUSTED_PROXY', PROXY_IP)
    client = app.test_client()
    resp = client.post('/test/local-only', environ_base=REMOTE,
                       headers={'X-Forwarded-For': '127.0.0.1'})
    assert resp.status_code == 401


def test_trusted_proxy_remote_client_with_valid_token_accepted(app,
                                                               monkeypatch):
    """The token path is still reachable behind a proxy: a remote
    forwarded client that carries a valid Bearer token is accepted even
    though the XFF client isn't loopback."""
    monkeypatch.setenv('TRUSTED_PROXY', PROXY_IP)
    from core import auth_local
    monkeypatch.setattr(auth_local, 'API_TOKEN', 'secret-token-123')
    client = app.test_client()
    resp = client.post('/test/local-only',
                       environ_base={'REMOTE_ADDR': PROXY_IP},
                       headers={'X-Forwarded-For': '203.0.113.55',
                                'Authorization': 'Bearer secret-token-123'})
    assert resp.status_code == 200
