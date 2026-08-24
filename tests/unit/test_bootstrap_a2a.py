"""hartos_bootstrap._init_a2a_server — the bundle's /a2a registration.

Regression: until 2026-08-22 the A2A routes were registered ONLY by
``hart_intelligence_entry`` module-level code, which the bundled build never
imports.  A bundled Nunba therefore served the SPA catch-all (200 text/html)
for every ``/a2a/*`` path, and peers' directory fetches died in
``peer_reuse.discover_peer_agent`` with ``Expecting value: line 1 column 1``
— an HTML 200 sails past its ``status_code != 200`` guard.  Measured live on
the LAN 2026-08-22: localhost:5000/a2a/agents returned the SPA shell while
the node advertised agents nobody could fetch.

These tests drive the REAL bootstrap step against a consumer-style app (SPA
catch-all included) exactly as ``_run_bootstrap`` does — inside the
setup-lock-bypass window.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from flask import Flask

import hartos_bootstrap as hb


def _consumer_style_app():
    """A Flask app shaped like Nunba's: an SPA catch-all owns every path."""
    app = Flask(f'consumer_{id(object())}')

    @app.route('/', defaults={'p': ''})
    @app.route('/<path:p>')
    def spa(p):
        return '<!doctype html><html>SPA</html>'

    return app


@pytest.fixture
def a2a_singleton_reset():
    """Isolate the module-global A2A server across tests."""
    import integrations.google_a2a.google_a2a_integration as gi
    saved = gi._a2a_server
    gi._a2a_server = None
    yield gi
    gi._a2a_server = saved


def _run_step(app, cfg=None):
    """Invoke the step the way _run_bootstrap does: inside the bypass window.

    Outside it, Flask 3 refuses route registration after the first request —
    the exact reason the bypass exists (step 1 of the bootstrap contract).
    """
    bypass = hb._enable_setup_lock_bypass(app)
    try:
        hb._init_a2a_server(app, cfg or {})
    finally:
        if bypass:
            hb._disable_setup_lock_bypass(app)


def test_spa_swallows_a2a_without_the_step(a2a_singleton_reset):
    """The bug this step fixes: /a2a/agents is the SPA shell, HTTP 200."""
    app = _consumer_style_app()
    r = app.test_client().get('/a2a/agents')
    assert r.status_code == 200
    assert b'SPA' in r.data
    assert not r.content_type.startswith('application/json')


def test_step_registers_json_directory_over_the_catch_all(a2a_singleton_reset):
    app = _consumer_style_app()
    c = app.test_client()
    # a request has been handled, as in the live app — the step must still work
    c.get('/a2a/agents')

    _run_step(app)

    r = c.get('/a2a/agents')
    assert r.content_type.startswith('application/json'), (
        'peers must get the JSON directory, not the SPA shell: %r' % r.content_type)
    body = r.get_json()
    assert isinstance(body.get('agents'), list)


def test_second_call_is_a_clean_noop(a2a_singleton_reset):
    """Repeat bootstrap must not raise Flask's duplicate-endpoint error."""
    app = _consumer_style_app()
    _run_step(app)
    rules_before = sorted(str(r) for r in app.url_map.iter_rules())
    _run_step(app)
    assert sorted(str(r) for r in app.url_map.iter_rules()) == rules_before


def test_preexisting_a2a_rule_is_respected(a2a_singleton_reset):
    """An app that already carries /a2a rules (the standalone entry path)
    is left untouched even when the server singleton is unset."""
    app = Flask('preexisting')

    @app.route('/a2a/agents')
    def existing():
        return 'preexisting'

    n_before = len(list(app.url_map.iter_rules()))
    _run_step(app)
    assert len(list(app.url_map.iter_rules())) == n_before


def test_singleton_on_this_app_short_circuits(a2a_singleton_reset):
    """A server already bound to THIS app means nothing to do."""
    gi = a2a_singleton_reset
    app = _consumer_style_app()
    _run_step(app)                      # registers + binds singleton to app
    rules_before = sorted(str(r) for r in app.url_map.iter_rules())
    _run_step(app)
    assert sorted(str(r) for r in app.url_map.iter_rules()) == rules_before


def test_singleton_on_another_app_does_not_block_the_served_app(a2a_singleton_reset):
    """The race that re-broke /a2a on 2026-08-22: hart_intelligence_entry's
    module-level init registered on ITS OWN internal app first (which no port
    serves in bundled mode) and set the singleton.  The step must still
    register on the app that is actually served."""
    gi = a2a_singleton_reset
    other = Flask('hie_internal')
    bypass = hb._enable_setup_lock_bypass(other)
    try:
        hb._init_a2a_server(other, {})   # simulates HIE winning the race
    finally:
        if bypass:
            hb._disable_setup_lock_bypass(other)
    assert gi._a2a_server is not None
    assert gi._a2a_server.app is other

    served = _consumer_style_app()
    _run_step(served)
    r = served.test_client().get('/a2a/agents')
    assert r.content_type.startswith('application/json'), (
        'the served app must carry the directory even when another app '
        'grabbed the singleton first: %r' % r.content_type)
    assert gi._a2a_server.app is served


def test_cfg_base_url_override_wins(a2a_singleton_reset):
    gi = a2a_singleton_reset
    app = _consumer_style_app()
    _run_step(app, {'a2a_base_url': 'https://node.example:7443'})
    assert gi._a2a_server is not None
    assert gi._a2a_server.base_url == 'https://node.example:7443'
