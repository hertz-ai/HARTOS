"""Pytest driver for the shared sheet-dismissal contract (hartDismiss.js).

The behavioural assertions live in test_shell_dismiss_sheets.mjs, which drives
the REAL static modules (hartDismiss.js, hartContextMenu.js, hartConnectivity.js,
hartSenses.js) and the REAL inline toggleStartMenu sliced out of the rendered
shell, on the shared DOM shim, and asserts the OBSERVABLE outcome: whether each
sheet is still open after a blur / outside press / Escape / scroll / resize.

The Python half here pins the one thing node cannot: that the helper is actually
in the served shell (a <script defer> include that the static route serves), the
same way test_shell_lock_fouc_and_nav pins hartNav.js. Without that include every
sheet would silently fall back to "no dismissal at all" on the box.
"""
import os
import shutil
import subprocess

import pytest

MJS = os.path.join(os.path.dirname(__file__), 'test_shell_dismiss_sheets.mjs')


def test_sheets_dismiss_through_one_set_js():
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, 'sheet dismissal harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


def test_dismiss_helper_is_included_before_its_consumers_and_served():
    """hartDismiss.js must load BEFORE hartSenses.js / hartConnectivity.js and the
    dynamically injected hartContextMenu.js can arm it, and must be fetchable."""
    from integrations.agent_engine.liquid_ui_service import LiquidUIService
    svc = LiquidUIService()
    html = svc.render_desktop_shell()
    tag = 'src="/shell/static/hartDismiss.js"'
    assert tag in html, 'hartDismiss.js must be a <script defer> include in the served shell'
    at = html.index(tag)
    for consumer in ('hartDesktop.js', 'hartSenses.js', 'hartConnectivity.js'):
        assert at < html.index('src="/shell/static/%s"' % consumer), (
            'hartDismiss.js must be included before %s (deferred scripts run in '
            'document order)' % consumer)

    app = svc._create_flask_app()
    app.testing = True
    r = app.test_client().get('/shell/static/hartDismiss.js')
    assert r.status_code == 200 and b'HartDismiss' in r.data
