"""The App Store must not offer to install what is already on the box.

2026-07-24 real-HW: the store showed a plain "Install" for Firefox — which SHIPS
IN THE IMAGE (hart-app-catalog.json preinstall:true -> hart-apps.nix bakes it into
systemPackages). Clicking it ran a Flathub install that failed with no network and
flipped the tile to "Retry", so the very first store visit looked broken (audit
#0.3).

Root cause was a parallel list: hartMarketplace.js rendered from a HARDCODED JS
CATALOG instead of /api/apps/catalog -> app_catalog.get_catalog_view(), the ONE
canonical catalog (also the source of the NixOS preinstall set) which annotates
each entry with a LOCAL `installed` flag via shutil.which(exec) — no network.
appCard already renders a non-interactive "Installed" state from app.installed; it
simply never received the flag.

Behavioural: drives the REAL get_catalog_view / annotate_installed with an
injected `which`, and the REAL Flask route, asserting the observable payload.
app_catalog imports clean (no full-app boot).

Run (dev box, targeted):
    python -m pytest tests/unit/test_app_store_shows_installed.py -v \
        --noconftest -p no:cacheprovider
"""
import os
import shutil
import subprocess

import pytest
from flask import Flask

from integrations.agent_engine import app_catalog
from integrations.agent_engine.shell_desktop_apis import register_shell_desktop_routes

MJS = os.path.join(os.path.dirname(__file__), 'test_app_store_shows_installed.mjs')


def _by_id(apps, app_id):
    return next((a for a in apps if a.get('id') == app_id), None)


def test_catalog_marks_a_preinstalled_app_installed_when_its_exec_resolves():
    """Firefox ships in the image; with its exec on PATH the store must say
    installed (-> the card renders "Installed", not a failing Install button)."""
    resolved = {'firefox': '/run/current-system/sw/bin/firefox'}
    apps = app_catalog.annotate_installed(
        app_catalog.load_catalog(), which=lambda e: resolved.get(e))
    ff = _by_id(apps, 'org.mozilla.firefox')
    assert ff is not None, 'Firefox missing from the canonical catalog'
    assert ff['preinstall'] is True, 'Firefox should be flagged preinstall in the image'
    assert ff['installed'] is True, 'a baked-in app whose exec resolves must read installed'


def test_catalog_marks_a_missing_app_not_installed():
    """An app that is NOT on the box must stay installable — the flag is honest in
    both directions (no blanket "everything installed")."""
    apps = app_catalog.annotate_installed(
        app_catalog.load_catalog(), which=lambda e: None)   # nothing on PATH
    ff = _by_id(apps, 'org.mozilla.firefox')
    assert ff['installed'] is False


def test_installed_is_decided_locally_with_no_network():
    """The decision is a PATH lookup only — it must work with the net off, which is
    the whole point (the old path required Flathub to be reachable)."""
    calls = []

    def fake_which(exe):
        calls.append(exe)
        return '/usr/bin/' + exe if exe == 'firefox' else None

    apps = app_catalog.annotate_installed(app_catalog.load_catalog(), which=fake_which)
    assert calls, 'no PATH lookups happened — installed flag was not computed locally'
    assert _by_id(apps, 'org.mozilla.firefox')['installed'] is True


def test_route_serves_the_catalog_with_the_installed_flag():
    """End-to-end on the route the marketplace JS now calls: /api/apps/catalog
    returns apps carrying `installed`, plus the canonical category order."""
    app = Flask(__name__)
    register_shell_desktop_routes(app)
    app.testing = True
    r = app.test_client().get('/api/apps/catalog')
    assert r.status_code == 200
    data = r.get_json()
    assert data.get('apps'), 'route served an empty catalog'
    assert data.get('categories'), 'route served no category order'
    for a in data['apps']:
        assert 'installed' in a, f"entry {a.get('id')} carries no installed flag"
        # The card maps these directly (id/name/category/icon/description).
        for k in ('id', 'name', 'category', 'icon', 'description'):
            assert k in a, f"entry {a.get('id')} missing {k} the card needs"


def test_marketplace_js_adopts_the_canonical_catalog():
    """Front-end half: drive the REAL loadCatalog() from the shipped
    hartMarketplace.js against a stubbed fetch and assert it maps the backend
    payload onto the card shape carrying `installed` (and keeps the seed list when
    the catalog is empty, so the store never goes blank)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available to run the JS behavioural harness')
    r = subprocess.run([node, MJS], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, 'app-store JS harness failed:\n' + r.stdout + r.stderr
    assert 'RESULT: ALL PASS' in r.stdout, r.stdout


def test_catalog_is_a_superset_of_the_seed_list():
    """The JS seed list is only a first-paint placeholder; the canonical catalog
    must cover the headline apps so the upgrade never LOSES tiles."""
    ids = {a['id'] for a in app_catalog.load_catalog()}
    for headline in ('org.mozilla.firefox', 'org.videolan.VLC', 'org.gimp.GIMP',
                     'org.libreoffice.LibreOffice'):
        assert headline in ids, f'{headline} missing from the canonical catalog'
