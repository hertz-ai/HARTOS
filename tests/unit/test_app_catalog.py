"""Tests for the offline-first curated app catalog (#154).

Covers:
  * the canonical catalog JSON (nixos/modules/hart-app-catalog.json) loads and is
    well-formed — preinstall entries carry a nixpkgs attr the Nix module bakes;
  * app_catalog.py serves the catalog OFFLINE with a LOCAL installed flag
    (shutil.which), proving Open-vs-Install dedup needs no network;
  * the /api/shell/apps/catalog backend route returns offline data and does NOT
    shell out / hit the network;
  * the appearance/wallpaper collection backend scans bundled dirs offline and
    degrades cleanly when none exist.

Behavioral (imports the real code, mocks only the boundary: which / subprocess /
the filesystem) — no grep/source-shape assertions.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import integrations.agent_engine.app_catalog as app_catalog


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_REAL_CATALOG = os.path.join(_REPO_ROOT, 'nixos', 'modules',
                             'hart-app-catalog.json')


def _reset_catalog_cache():
    app_catalog._cache = None
    app_catalog._cache_path_used = None


def _write_temp_catalog(apps, categories=None):
    payload = {'schemaVersion': 1, 'apps': apps}
    if categories is not None:
        payload['categories'] = categories
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(payload, f)
    return path


# ═══════════════════════════════════════════════════════════════
# The committed canonical catalog file
# ═══════════════════════════════════════════════════════════════

class TestCanonicalCatalogFile(unittest.TestCase):
    """Guards the ONE source of truth the Nix module and Python both read."""

    @classmethod
    def setUpClass(cls):
        with open(_REAL_CATALOG, 'r', encoding='utf-8') as f:
            cls.doc = json.load(f)

    def test_file_is_valid_object_with_apps_and_categories(self):
        self.assertIsInstance(self.doc, dict)
        self.assertIsInstance(self.doc.get('apps'), list)
        self.assertTrue(self.doc['apps'])
        self.assertIsInstance(self.doc.get('categories'), list)
        self.assertTrue(self.doc['categories'])

    def test_every_entry_has_required_display_keys(self):
        for a in self.doc['apps']:
            for k in ('id', 'name', 'category', 'icon', 'description'):
                self.assertTrue(a.get(k), f'{a.get("id")} missing {k}')

    def test_preinstall_entries_carry_a_nix_package_and_exec(self):
        # The Nix module bakes ONLY preinstall entries with a non-empty package;
        # without one they would be silently dropped from systemPackages.
        preinstall = [a for a in self.doc['apps'] if a.get('preinstall')]
        self.assertTrue(preinstall, 'expected some preinstall apps')
        for a in preinstall:
            self.assertTrue(a.get('package'),
                            f'preinstall {a["id"]} has no nixpkgs package')
            self.assertIsInstance(a['package'], str)
            self.assertTrue(a.get('exec'),
                            f'preinstall {a["id"]} has no exec for which() dedup')

    def test_entry_categories_are_declared(self):
        declared = set(self.doc['categories'])
        for a in self.doc['apps']:
            self.assertIn(a['category'], declared,
                          f'{a["id"]} category not in declared categories')

    def test_ids_unique(self):
        ids = [a['id'] for a in self.doc['apps']]
        self.assertEqual(len(ids), len(set(ids)), 'duplicate catalog ids')

    def test_no_em_dash_in_user_facing_strings(self):
        for a in self.doc['apps']:
            for k in ('name', 'description'):
                self.assertNotIn('—', a[k], f'em dash in {a["id"]}.{k}')


# ═══════════════════════════════════════════════════════════════
# app_catalog.py — offline loader + dedup
# ═══════════════════════════════════════════════════════════════

class TestAppCatalogLoader(unittest.TestCase):

    def setUp(self):
        _reset_catalog_cache()
        self._saved_env = os.environ.get('HART_APP_CATALOG')

    def tearDown(self):
        _reset_catalog_cache()
        if self._saved_env is None:
            os.environ.pop('HART_APP_CATALOG', None)
        else:
            os.environ['HART_APP_CATALOG'] = self._saved_env

    def test_loads_real_catalog_repo_relative(self):
        # No env override -> resolves nixos/modules/hart-app-catalog.json.
        os.environ.pop('HART_APP_CATALOG', None)
        _reset_catalog_cache()
        apps = app_catalog.load_catalog(force=True)
        self.assertTrue(apps)
        self.assertTrue(any(a['id'] == 'org.mozilla.firefox' for a in apps))

    def test_env_override_is_honoured(self):
        path = _write_temp_catalog([
            {'id': 'x.Demo', 'name': 'Demo', 'category': 'Web',
             'icon': 'public', 'description': 'demo app', 'preinstall': False,
             'exec': 'demo'}])
        try:
            os.environ['HART_APP_CATALOG'] = path
            _reset_catalog_cache()
            apps = app_catalog.load_catalog(force=True)
            self.assertEqual(len(apps), 1)
            self.assertEqual(apps[0]['id'], 'x.Demo')
        finally:
            os.remove(path)

    def test_env_miss_falls_back_to_bundled_repo_catalog(self):
        # An explicit override pointing at a missing file degrades to the bundled
        # repo catalog (degrade toward SOME catalog, never crash).
        os.environ['HART_APP_CATALOG'] = '/nonexistent/hart-app-catalog.json'
        _reset_catalog_cache()
        apps = app_catalog.load_catalog(force=True)
        self.assertTrue(apps)
        self.assertTrue(any(a['id'] == 'org.mozilla.firefox' for a in apps))

    def test_no_catalog_anywhere_degrades_to_empty_never_raises(self):
        # When NO candidate path resolves, the loader yields an empty list and
        # the view is still a well-formed envelope (the JS fallback still paints).
        with patch.object(app_catalog, '_candidate_paths',
                          return_value=iter(['/nonexistent/a.json'])):
            _reset_catalog_cache()
            apps = app_catalog.load_catalog(force=True)
            self.assertEqual(apps, [])
            view = app_catalog.get_catalog_view()
            self.assertEqual(view['apps'], [])
            self.assertEqual(view['count'], 0)

    def test_malformed_entries_are_dropped(self):
        path = _write_temp_catalog([
            {'id': 'good.App', 'name': 'Good', 'category': 'Web',
             'icon': 'public', 'description': 'ok', 'exec': 'good'},
            {'id': 'bad.App'},  # missing required keys -> dropped
            'not-a-dict',
        ])
        try:
            os.environ['HART_APP_CATALOG'] = path
            _reset_catalog_cache()
            apps = app_catalog.load_catalog(force=True)
            self.assertEqual([a['id'] for a in apps], ['good.App'])
        finally:
            os.remove(path)

    def test_annotate_installed_uses_which_no_network(self):
        path = _write_temp_catalog([
            {'id': 'a.Has', 'name': 'Has', 'category': 'Web', 'icon': 'public',
             'description': 'installed', 'exec': 'has-bin'},
            {'id': 'a.Missing', 'name': 'Missing', 'category': 'Web',
             'icon': 'public', 'description': 'not here', 'exec': 'missing-bin'},
        ])
        try:
            os.environ['HART_APP_CATALOG'] = path
            _reset_catalog_cache()
            apps = app_catalog.load_catalog(force=True)
            fake_which = lambda name: '/usr/bin/has-bin' if name == 'has-bin' else None
            annotated = app_catalog.annotate_installed(apps, which=fake_which)
            by_id = {a['id']: a for a in annotated}
            self.assertTrue(by_id['a.Has']['installed'])
            self.assertFalse(by_id['a.Missing']['installed'])
        finally:
            os.remove(path)

    def test_catalog_view_query_filters_locally(self):
        os.environ.pop('HART_APP_CATALOG', None)
        _reset_catalog_cache()
        view = app_catalog.get_catalog_view(query='firefox')
        self.assertTrue(view['apps'])
        self.assertTrue(all('firefox' in a['name'].lower()
                            or 'firefox' in a['id'].lower()
                            or 'firefox' in a['description'].lower()
                            for a in view['apps']))


# ═══════════════════════════════════════════════════════════════
# Backend routes (offline)
# ═══════════════════════════════════════════════════════════════

def _make_desktop_app():
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    from integrations.agent_engine.shell_desktop_apis import (
        register_shell_desktop_routes)
    register_shell_desktop_routes(app)
    return app.test_client()


class TestCatalogRouteOffline(unittest.TestCase):

    def setUp(self):
        _reset_catalog_cache()
        self._saved_env = os.environ.get('HART_APP_CATALOG')

    def tearDown(self):
        _reset_catalog_cache()
        if self._saved_env is None:
            os.environ.pop('HART_APP_CATALOG', None)
        else:
            os.environ['HART_APP_CATALOG'] = self._saved_env

    def test_catalog_route_serves_offline_no_subprocess(self):
        os.environ.pop('HART_APP_CATALOG', None)
        _reset_catalog_cache()
        client = _make_desktop_app()
        # If the route shelled out, this patch would raise inside it.
        with patch('subprocess.run',
                   side_effect=AssertionError('catalog route shelled out')):
            r = client.get('/api/shell/apps/catalog')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('apps', data)
        self.assertIn('categories', data)
        self.assertTrue(data['apps'])
        # Each entry carries the locally-decided installed flag.
        self.assertIn('installed', data['apps'][0])

    def test_catalog_route_legacy_prefix(self):
        os.environ.pop('HART_APP_CATALOG', None)
        _reset_catalog_cache()
        client = _make_desktop_app()
        r = client.get('/api/apps/catalog')
        self.assertEqual(r.status_code, 200)

    def test_catalog_route_query_param(self):
        os.environ.pop('HART_APP_CATALOG', None)
        _reset_catalog_cache()
        client = _make_desktop_app()
        r = client.get('/api/shell/apps/catalog?q=vlc')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['query'], 'vlc')
        self.assertTrue(any(a['id'] == 'org.videolan.VLC' for a in data['apps']))


class TestWallpaperCollectionOffline(unittest.TestCase):

    def test_collection_aggregates_bundled_dirs_offline(self):
        with tempfile.TemporaryDirectory() as d:
            # Files in the dir itself and in a subdir (gnome layout).
            open(os.path.join(d, 'one.jpg'), 'w').close()
            open(os.path.join(d, 'note.txt'), 'w').close()  # ignored ext
            sub = os.path.join(d, 'gnome')
            os.makedirs(sub)
            open(os.path.join(sub, 'two.png'), 'w').close()
            client = _make_desktop_app()
            r = client.get('/api/shell/wallpaper/collection',
                           query_string={'directory': d})
            self.assertEqual(r.status_code, 200)
            data = json.loads(r.data)
            names = sorted(img['name'] for img in data['images'])
            self.assertEqual(names, ['one', 'two'])

    def test_collection_default_dirs_offline_no_crash(self):
        # No directory param + no override -> scans NixOS-valid candidates which
        # may all be absent on the test host. Must NOT crash, returns an envelope.
        with patch.dict(os.environ, {'HART_WALLPAPER_DIR': '/nonexistent/bg'}):
            client = _make_desktop_app()
            r = client.get('/api/shell/wallpaper/collection')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('images', data)
        self.assertIsInstance(data['images'], list)


if __name__ == '__main__':
    unittest.main()
