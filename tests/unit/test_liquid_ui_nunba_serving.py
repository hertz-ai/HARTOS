"""The :6800 glass shell must serve the Nunba React dist NATIVELY at the origin
root — a no-basename BrowserRouter (history) SPA whose bundle refs are
origin-root absolute (``/static/js/main.*.js``, ``/static/css/…``).

Before this shim the shell fought the dist on every axis: it served under
``/app/<path>`` and set ``NUNBA_BASE='/app/#'`` (HASH), so an iframe loaded
``/app/#/social`` => the server returned ``/app/`` index.html and the
history router (which ignores the ``#`` fragment) rendered the SPA root =>
blank panel. And ``/static/*`` 404'd because the only Nunba route was
``/app/*``.

The fix (single-writer, mirrors Nunba's own ``app.py`` catch-all — no parallel
path):
  * ``GET /static/<path>``  -> the hashed bundle passthrough,
  * ``GET /<path>``         -> SPA history fallback (file-or-index.html),
  * ``GET /cors/test``      -> the dist's 12s liveness probe stub,
  * ``NUNBA_BASE=''``       -> the iframe src becomes a real history path.

These tests start the REAL Flask app from ``_create_flask_app()`` with
``NUNBA_STATIC_DIR`` pointed at a tmp build dir, then fetch the exact URLs the
dist asks the browser to load and assert the bytes — behavioural, not
source-shape. They also pin the regression surface: the explicit shell routes
(``/``, ``/health``, ``/api/shell/apps``) must keep winning over the catch-all.
"""
import json
import os

import pytest

from integrations.agent_engine.liquid_ui_service import LiquidUIService


_INDEX_HTML = (
    b'<!doctype html><html><head>'
    b'<script defer src="/static/js/main.5d05848f.js"></script>'
    b'<link href="/static/css/main.ece56ab4.css" rel="stylesheet">'
    b'</head><body><div id="root"></div>NUNBA-SPA-INDEX</body></html>'
)
_BUNDLE_JS = b'/* nunba main bundle */ console.log("hevolve");'
# A non-hashed bundle directly under static/ (the task's literal passthrough URL
# /static/app.js) — distinct bytes so the passthrough test can prove the real
# file was served, never the index.html fallback.
_APP_JS = b'/* nunba static/app.js */ console.log("APP-JS-PASSTHROUGH");'


@pytest.fixture
def nunba_dist(tmp_path):
    """A minimal but real CRA-shaped build dir: index.html at the root plus a
    hashed bundle under static/js/. Mirrors the shape of the actual Nunba
    landing-page build/ tree."""
    (tmp_path / 'index.html').write_bytes(_INDEX_HTML)
    static_js = tmp_path / 'static' / 'js'
    static_js.mkdir(parents=True)
    (static_js / 'main.5d05848f.js').write_bytes(_BUNDLE_JS)
    (tmp_path / 'static' / 'app.js').write_bytes(_APP_JS)
    # A root-relative asset the index pulls (favicon/logo/webmanifest class).
    (tmp_path / 'site.webmanifest').write_bytes(b'{"name":"HART"}')
    return tmp_path


@pytest.fixture
def served(nunba_dist, monkeypatch):
    """Real LiquidUIService + Flask test client with the Nunba dist mounted.
    ``_create_flask_app`` reads ``NUNBA_STATIC_DIR`` at call time, so the env is
    set before the app is built and torn down by monkeypatch afterwards."""
    monkeypatch.setenv('NUNBA_STATIC_DIR', str(nunba_dist))
    svc = LiquidUIService()
    app = svc._create_flask_app()
    app.testing = True
    return svc, app.test_client()


@pytest.fixture
def served_no_dist(monkeypatch):
    """The shell with NO Nunba dist mounted — to prove the shims are gated and
    the floor-lock /static 404 is preserved when NUNBA_STATIC_DIR is unset."""
    monkeypatch.delenv('NUNBA_STATIC_DIR', raising=False)
    svc = LiquidUIService()
    app = svc._create_flask_app()
    app.testing = True
    return svc, app.test_client()


# ─── Native serving (the actual fix) ─────────────────────────────────────────

def test_hashed_bundle_is_served_from_static_passthrough(served):
    """The origin-root absolute bundle ref in index.html resolves on :6800."""
    _svc, client = served
    r = client.get('/static/js/main.5d05848f.js')
    assert r.status_code == 200
    assert r.data == _BUNDLE_JS
    # The task's literal passthrough URL: /static/app.js returns the real file,
    # NOT the SPA index.html fallback.
    r2 = client.get('/static/app.js')
    assert r2.status_code == 200, (
        '/static/app.js must pass through to the dist file, got %s'
        % r2.status_code)
    assert r2.data == _APP_JS
    assert b'NUNBA-SPA-INDEX' not in r2.data


def test_history_route_falls_back_to_index_html(served):
    """A BrowserRouter path the server has no file for (``/social``) must return
    index.html so the in-browser router resolves the route itself — this is the
    exact case that came up blank under the old hash mount."""
    _svc, client = served
    r = client.get('/social')
    assert r.status_code == 200
    assert b'NUNBA-SPA-INDEX' in r.data
    # The task's literal deep-route example: /app/social has no real file, so
    # the history fallback serves index.html 200 (not a 404, not the bundle).
    r2 = client.get('/app/social')
    assert r2.status_code == 200, (
        '/app/social deep route must fall back to index.html 200, got %s'
        % r2.status_code)
    assert b'NUNBA-SPA-INDEX' in r2.data
    assert b'APP-JS-PASSTHROUGH' not in r2.data


def test_nested_history_route_falls_back_to_index_html(served):
    """Multi-segment history routes (``/social/recipes``) fall back too — the
    ``<path:path>`` converter spans slashes."""
    _svc, client = served
    r = client.get('/social/recipes')
    assert r.status_code == 200
    assert b'NUNBA-SPA-INDEX' in r.data


def test_real_root_asset_is_served_not_index(served):
    """A real file at the build root (``/site.webmanifest``) is served as-is, not
    shadowed by the index fallback (file-or-index, file wins)."""
    _svc, client = served
    r = client.get('/site.webmanifest')
    assert r.status_code == 200
    assert r.data == b'{"name":"HART"}'


def test_cors_test_probe_returns_200(served):
    """index.html's 12s stall fallback fetches ``/cors/test``; without the stub
    it 404'd and the loader showed a misleading 'Server is starting up'."""
    _svc, client = served
    r = client.get('/cors/test')
    assert r.status_code == 200
    assert r.data == b'ok'


# ─── Regression: the catch-all must shadow nothing ───────────────────────────

def test_root_is_still_the_glass_shell(served):
    """``/`` keeps the explicit shell handler — the SPA catch-all is ``<path>``
    only and never matches the empty path."""
    _svc, client = served
    r = client.get('/')
    assert r.status_code == 200
    assert b'NUNBA-SPA-INDEX' not in r.data
    assert b'<!doctype html' in r.data.lower() or b'<html' in r.data.lower()


def test_health_still_returns_json(served):
    """``/health`` (a more-specific rule) still wins over ``/<path>``."""
    _svc, client = served
    r = client.get('/health')
    assert r.status_code == 200
    body = json.loads(r.data)
    assert body.get('status') == 'ok'
    assert body.get('service') == 'liquid-ui-shell'


def test_api_shell_apps_still_served(served):
    """An ``/api/shell/*`` route still resolves to its real handler, proving the
    catch-all does not swallow the OS-management API surface."""
    _svc, client = served
    r = client.get('/api/shell/apps')
    assert r.status_code == 200
    body = json.loads(r.data)
    assert 'apps' in body


def test_favicon_still_204(served):
    """The explicit favicon route wins over the catch-all (no index.html for a
    request the shell intentionally answers empty)."""
    _svc, client = served
    assert client.get('/favicon.ico').status_code == 204


# ─── Gating: shims off when no dist is mounted ───────────────────────────────

def test_static_is_404_when_no_dist_mounted(served_no_dist):
    """Floor-lock preserved: with NUNBA_STATIC_DIR unset, ``/static/*`` is a 404
    (the shell's own assets live at the distinct /shell/static prefix)."""
    _svc, client = served_no_dist
    assert client.get('/static/js/main.5d05848f.js').status_code == 404
    # The task's literal control: /static/app.js must 404 when no dist mounted.
    assert client.get('/static/app.js').status_code == 404


def test_history_route_404_when_no_dist_mounted(served_no_dist):
    """No SPA fallback when no dist is mounted — an unknown path is a real 404,
    not an index.html that does not exist."""
    _svc, client = served_no_dist
    assert client.get('/social').status_code == 404


def test_cors_probe_is_unconditional(served_no_dist):
    """The liveness stub answers even without a dist (harmless, gated off the
    Nunba block) so a bare shell still reports itself up."""
    _svc, client = served_no_dist
    r = client.get('/cors/test')
    assert r.status_code == 200
    assert r.data == b'ok'


# ─── Source guard: the hash mount is gone ────────────────────────────────────
# The browser-side NUNBA_BASE const cannot be executed from Python (it runs in
# WebKit), so a behavioural fetch test can't observe it. This guard is NOT the
# only test for the edit — every route above is behavioural — it only pins that
# the blank-iframe hash convention ('/app/#') was removed in favour of the
# empty history mount, which is the regression the routing tests can't reach.

def test_source_guard_nunba_base_is_history_not_hash():
    svc = LiquidUIService()
    html = svc.render_desktop_shell()
    assert "NUNBA_BASE = ''" in html, "NUNBA_BASE must be the empty history mount"
    assert "'/app/#'" not in html, "the old hash mount must be gone"
    assert '/app/#' not in html, "no '/app/#' hash route anywhere in the shell"
