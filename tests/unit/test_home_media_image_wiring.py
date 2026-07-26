"""W10 - wire the local semantic media index into the Netflix HOME.

Two halves, both pure WIRING of already-coded pieces (no new module / transport):

1. test_home_image_hydration_js - drives the REAL hartHome.js through its public
   surface (window.HartHome.compose) on a DOM shim and asserts the OBSERVABLE
   card-image behaviour (test_home_media_image_wiring.mjs):
     * an imageless card searches /api/media/search?q=<title>&limit=3,
     * a local IMAGE hit loads a lazy <img> from the EXISTING shell thumbnail
       route and drops the placeholder glyph,
     * a miss / a video-only hit keeps the brand gradient + glyph (never blank),
     * a producer photo (card.image) is used verbatim with no search, and a
       remote photo (card.image_url) is routed through the same-origin
       fetch-once ImageCache (/api/media/image).
   Skips cleanly if node is absent.

2. test_media_routes_mounted_on_shell_app - proves the registration wiring: the
   REAL shell app factory (_create_flask_app) now mounts the media-index routes
   on the SAME origin that serves hartHome.js, so the in-WebView loopback fetch
   reaches them with no token. This guards the #18 route-drop class (a future
   edit that drops register_media_routes fails here, not silently in the field).

Local note: this box OOM-kills the full pytest import chain; the .mjs runs
standalone (`node tests/unit/test_home_media_image_wiring.mjs`) and the route
check builds the Flask app through a temp data dir. Committed for CI.
"""
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MJS = os.path.join(os.path.dirname(__file__), 'test_home_media_image_wiring.mjs')


def _liquid_ui():
    try:
        from integrations.agent_engine.liquid_ui_service import LiquidUIService
    except Exception as e:  # heavy deps absent in a minimal runner -> skip
        pytest.skip('LiquidUIService not importable here: ' + str(e))
    return LiquidUIService


def test_home_image_hydration_js():
    """Drive the REAL hartHome.js card-image hydration (Node + DOM shim)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, 'home media-image harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


def test_media_routes_mounted_on_shell_app():
    """register_media_routes is wired into the shell app factory (W10).

    Build the REAL shell Flask app and prove the media-index routes are mounted
    AND reachable from loopback (the in-WebView fetch path), the same origin that
    serves hartHome.js. A loopback GET /api/media/search returns the search
    contract; a non-loopback request without a shell token is refused (the routes
    never become an open proxy)."""
    svc = _liquid_ui()()
    svc._data_dir = tempfile.mkdtemp()          # never touch real shell state
    app = svc._create_flask_app()

    rules = {r.rule for r in app.url_map.iter_rules()}
    for path in ('/api/media/search', '/api/media/image', '/api/media/index/status'):
        assert path in rules, path + ' not mounted (register_media_routes wiring dropped)'

    client = app.test_client()

    # Loopback (default test-client REMOTE_ADDR is 127.0.0.1) is allowed and the
    # search contract answers, proving the route is live end to end (not just in
    # the map). An empty index simply returns zero results - still a 200.
    ok = client.get('/api/media/search?q=beach&limit=1')
    assert ok.status_code == 200, ok.status_code
    body = ok.get_json()
    assert body['query'] == 'beach'
    assert 'results' in body and isinstance(body['results'], list)

    # A non-loopback caller with no shell token is refused: the local photo
    # catalog must never be readable off-box (loopback-gated, not an open proxy).
    denied = client.get('/api/media/search?q=beach',
                        environ_overrides={'REMOTE_ADDR': '10.0.0.9'})
    assert denied.status_code == 403, denied.status_code


if __name__ == '__main__':
    # Inline runner (pytest OOMs on this box): execute every test_* and report.
    fns = [v for k, v in sorted(globals().items())
           if k.startswith('test_') and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print('  OK  ', fn.__name__)
        except Exception as e:
            failed += 1
            print(' FAIL ', fn.__name__, '->', repr(e))
    print('RESULT:', 'ALL PASS' if not failed else (str(failed) + ' FAILED'))
    sys.exit(1 if failed else 0)
