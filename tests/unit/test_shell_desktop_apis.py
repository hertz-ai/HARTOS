"""
Tests for integrations.agent_engine.shell_desktop_apis — Desktop experience APIs.

Covers: default apps, font manager, sound manager, clipboard manager,
date/time/timezone, wallpaper, input methods, night light, workspaces.
"""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock


def _make_desktop_app():
    """Create a Flask test app with all desktop routes."""
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    # Reset clipboard state between tests
    import integrations.agent_engine.shell_desktop_apis as mod
    mod._clipboard_history.clear()
    mod._clipboard_counter = 0
    from integrations.agent_engine.shell_desktop_apis import register_shell_desktop_routes
    register_shell_desktop_routes(app)
    return app.test_client()


# ═══════════════════════════════════════════════════════════════
# Default Apps
# ═══════════════════════════════════════════════════════════════

class TestDefaultApps(unittest.TestCase):

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_list_defaults(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='firefox.desktop\n')
        client = _make_desktop_app()
        r = client.get('/api/shell/default-apps')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('defaults', data)
        self.assertIn('categories', data)

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_set_default(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_desktop_app()
        r = client.post('/api/shell/default-apps/set',
                        data=json.dumps({'mime_type': 'text/html', 'app': 'chromium.desktop'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['set'])

    def test_set_default_missing_fields(self):
        client = _make_desktop_app()
        r = client.post('/api/shell/default-apps/set',
                        data=json.dumps({'mime_type': 'text/html'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_candidates_no_mime(self):
        client = _make_desktop_app()
        r = client.get('/api/shell/default-apps/candidates')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_set_category_browser(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_desktop_app()
        r = client.post('/api/shell/default-apps/set-category',
                        data=json.dumps({'category': 'browser', 'app': 'firefox.desktop'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)

    def test_set_category_invalid(self):
        client = _make_desktop_app()
        r = client.post('/api/shell/default-apps/set-category',
                        data=json.dumps({'category': 'invalid', 'app': 'x.desktop'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════
# Font Manager
# ═══════════════════════════════════════════════════════════════

class TestFontManager(unittest.TestCase):

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_list_fonts(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='JetBrains Mono|Regular|/usr/share/fonts/JBM.ttf|TrueType\n'
                   'Inter|Regular|/usr/share/fonts/Inter.ttf|TrueType\n')
        client = _make_desktop_app()
        r = client.get('/api/shell/fonts')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertGreater(data['count'], 0)
        self.assertIn('categories', data)

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_list_fonts_search(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='JetBrains Mono|Regular|/x.ttf|TrueType\nInter|Regular|/y.ttf|TrueType\n')
        client = _make_desktop_app()
        r = client.get('/api/shell/fonts?search=jet')
        data = json.loads(r.data)
        self.assertEqual(data['count'], 1)

    def test_font_preview(self):
        client = _make_desktop_app()
        r = client.get('/api/shell/fonts/preview?family=Inter&size=24')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('css', data)
        self.assertIn('Inter', data['css'])

    def test_font_preview_missing_family(self):
        client = _make_desktop_app()
        r = client.get('/api/shell/fonts/preview')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_install_font(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        with tempfile.NamedTemporaryFile(suffix='.ttf', delete=False) as f:
            f.write(b'\x00\x01\x00\x00')
            path = f.name
        try:
            client = _make_desktop_app()
            r = client.post('/api/shell/fonts/install',
                            data=json.dumps({'path': path}),
                            content_type='application/json')
            self.assertEqual(r.status_code, 200)
            data = json.loads(r.data)
            self.assertTrue(data['installed'])
        finally:
            os.unlink(path)

    def test_install_invalid_format(self):
        with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as f:
            f.write(b'not a font')
            path = f.name
        try:
            client = _make_desktop_app()
            r = client.post('/api/shell/fonts/install',
                            data=json.dumps({'path': path}),
                            content_type='application/json')
            self.assertEqual(r.status_code, 400)
        finally:
            os.unlink(path)

    def test_remove_font_missing(self):
        client = _make_desktop_app()
        r = client.post('/api/shell/fonts/remove',
                        data=json.dumps({'family': 'NonexistentFont'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 404)


# ═══════════════════════════════════════════════════════════════
# Sound Manager
# ═══════════════════════════════════════════════════════════════

class TestSoundManager(unittest.TestCase):

    def test_list_themes(self):
        client = _make_desktop_app()
        r = client.get('/api/shell/sounds/themes')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('themes', data)
        self.assertIn('active', data)

    def test_list_events(self):
        client = _make_desktop_app()
        r = client.get('/api/shell/sounds/events')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('events', data)
        self.assertGreater(len(data['events']), 5)

    @patch('integrations.agent_engine.shell_desktop_apis._save_json')
    @patch('integrations.agent_engine.shell_desktop_apis._load_json', return_value={})
    def test_set_theme(self, _load, _save):
        client = _make_desktop_app()
        r = client.post('/api/shell/sounds/set-theme',
                        data=json.dumps({'theme': 'hart-default'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['set'])

    @patch('integrations.agent_engine.shell_desktop_apis._save_json')
    @patch('integrations.agent_engine.shell_desktop_apis._load_json', return_value={})
    def test_toggle_sounds(self, _load, _save):
        client = _make_desktop_app()
        r = client.post('/api/shell/sounds/toggle',
                        data=json.dumps({'enabled': False}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertFalse(data['enabled'])

    def test_play_missing_event(self):
        client = _make_desktop_app()
        r = client.post('/api/shell/sounds/play',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('shutil.which', return_value='/usr/bin/pw-play')
    @patch('integrations.agent_engine.shell_desktop_apis._run')
    @patch('integrations.agent_engine.shell_desktop_apis._load_json')
    def test_play_event_with_override(self, mock_load, mock_run, _which):
        mock_load.side_effect = lambda p, d=None: {'bell': '/tmp/bell.oga'} if 'override' in p else d or {}
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_desktop_app()
        r = client.post('/api/shell/sounds/play',
                        data=json.dumps({'event': 'bell'}),
                        content_type='application/json')
        # May be 200 or 404 depending on file existence — test it doesn't crash
        self.assertIn(r.status_code, (200, 404))


# ═══════════════════════════════════════════════════════════════
# Clipboard Manager
# ═══════════════════════════════════════════════════════════════

class TestClipboardManager(unittest.TestCase):

    def test_history_empty(self):
        client = _make_desktop_app()
        r = client.get('/api/shell/clipboard/history')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['count'], 0)

    @patch('integrations.agent_engine.shell_desktop_apis._is_wayland', return_value=False)
    @patch('subprocess.run')
    def test_copy_then_history(self, mock_run, _wl):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_desktop_app()
        r = client.post('/api/shell/clipboard/copy',
                        data=json.dumps({'content': 'hello world'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['copied'])
        r2 = client.get('/api/shell/clipboard/history')
        data2 = json.loads(r2.data)
        self.assertEqual(data2['count'], 1)
        self.assertEqual(data2['entries'][0]['content'], 'hello world')

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_current(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='clipboard text')
        client = _make_desktop_app()
        r = client.get('/api/shell/clipboard/current')
        data = json.loads(r.data)
        self.assertEqual(data['content'], 'clipboard text')

    @patch('integrations.agent_engine.shell_desktop_apis._is_wayland', return_value=False)
    @patch('subprocess.run')
    def test_pin_entry(self, mock_run, _wl):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_desktop_app()
        client.post('/api/shell/clipboard/copy',
                     data=json.dumps({'content': 'pin me'}),
                     content_type='application/json')
        r = client.post('/api/shell/clipboard/pin',
                        data=json.dumps({'id': 1}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['pinned'])

    def test_pin_nonexistent(self):
        client = _make_desktop_app()
        r = client.post('/api/shell/clipboard/pin',
                        data=json.dumps({'id': 999}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 404)

    @patch('integrations.agent_engine.shell_desktop_apis._is_wayland', return_value=False)
    @patch('subprocess.run')
    def test_clear_all_preserves_pinned(self, mock_run, _wl):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_desktop_app()
        client.post('/api/shell/clipboard/copy',
                     data=json.dumps({'content': 'a'}), content_type='application/json')
        client.post('/api/shell/clipboard/copy',
                     data=json.dumps({'content': 'b'}), content_type='application/json')
        client.post('/api/shell/clipboard/pin',
                     data=json.dumps({'id': 1}), content_type='application/json')
        r = client.post('/api/shell/clipboard/clear',
                        data=json.dumps({'all': True}), content_type='application/json')
        data = json.loads(r.data)
        self.assertEqual(data['cleared'], 1)  # only non-pinned cleared

    def test_copy_empty_content(self):
        client = _make_desktop_app()
        r = client.post('/api/shell/clipboard/copy',
                        data=json.dumps({'content': ''}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════
# Date/Time/Timezone
# ═══════════════════════════════════════════════════════════════

class TestDateTime(unittest.TestCase):

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_get_datetime(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='Timezone=US/Eastern\nNTP=yes\nNTPSynchronized=yes\n')
        client = _make_desktop_app()
        r = client.get('/api/shell/datetime')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('timezone', data)
        self.assertIn('datetime', data)
        self.assertIn('clock_format', data)

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_list_timezones(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='US/Eastern\nUS/Pacific\nEurope/London\n')
        client = _make_desktop_app()
        r = client.get('/api/shell/datetime/timezones')
        data = json.loads(r.data)
        self.assertGreater(data['count'], 0)

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_set_timezone(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_desktop_app()
        r = client.post('/api/shell/datetime/set-timezone',
                        data=json.dumps({'timezone': 'US/Pacific'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['set'])

    def test_set_timezone_invalid(self):
        client = _make_desktop_app()
        r = client.post('/api/shell/datetime/set-timezone',
                        data=json.dumps({'timezone': 'invalid'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_set_ntp(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_desktop_app()
        r = client.post('/api/shell/datetime/set-ntp',
                        data=json.dumps({'enabled': True}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['ntp_enabled'])

    @patch('integrations.agent_engine.shell_desktop_apis._save_json')
    def test_set_format(self, _save):
        client = _make_desktop_app()
        r = client.post('/api/shell/datetime/set-format',
                        data=json.dumps({'format': '12h'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['set'])
        self.assertEqual(data['clock_format'], '12h')

    def test_set_format_invalid(self):
        client = _make_desktop_app()
        r = client.post('/api/shell/datetime/set-format',
                        data=json.dumps({'format': 'invalid'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════
# Wallpaper
# ═══════════════════════════════════════════════════════════════

class TestWallpaper(unittest.TestCase):

    def test_get_wallpaper(self):
        client = _make_desktop_app()
        r = client.get('/api/shell/wallpaper')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('mode', data)

    def test_collection_empty(self):
        client = _make_desktop_app()
        r = client.get('/api/shell/wallpaper/collection?directory=/nonexistent')
        data = json.loads(r.data)
        self.assertEqual(data['count'], 0)

    def test_collection_with_images(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ['a.png', 'b.jpg', 'c.txt']:
                with open(os.path.join(d, name), 'w') as f:
                    f.write('data')
            client = _make_desktop_app()
            r = client.get(f'/api/shell/wallpaper/collection?directory={d}')
            data = json.loads(r.data)
            self.assertEqual(data['count'], 2)  # .png and .jpg only

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    @patch('integrations.agent_engine.shell_desktop_apis._save_json')
    @patch('integrations.agent_engine.shell_desktop_apis._load_json', return_value={})
    def test_set_wallpaper(self, _load, _save, _run):
        _run.return_value = MagicMock(returncode=0)
        client = _make_desktop_app()
        r = client.post('/api/shell/wallpaper/set',
                        data=json.dumps({'path': '/usr/share/bg.png', 'mode': 'fill'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['set'])

    def test_set_wallpaper_no_path(self):
        client = _make_desktop_app()
        r = client.post('/api/shell/wallpaper/set',
                        data=json.dumps({'path': ''}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.agent_engine.shell_desktop_apis._save_json')
    @patch('integrations.agent_engine.shell_desktop_apis._load_json', return_value={})
    def test_set_lock(self, _load, _save):
        client = _make_desktop_app()
        r = client.post('/api/shell/wallpaper/set-lock',
                        data=json.dumps({'path': '/usr/share/lock.png'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['set'])

    @patch('integrations.agent_engine.shell_desktop_apis._save_json')
    @patch('integrations.agent_engine.shell_desktop_apis._load_json', return_value={})
    def test_slideshow(self, _load, _save):
        client = _make_desktop_app()
        r = client.post('/api/shell/wallpaper/slideshow',
                        data=json.dumps({'enabled': True, 'interval_minutes': 15}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['slideshow']['enabled'])


# ═══════════════════════════════════════════════════════════════
# Input Methods
# ═══════════════════════════════════════════════════════════════

class TestInputMethods(unittest.TestCase):

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_get_layouts(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='layout:     us,de\noptions:    compose:ralt\n')
        client = _make_desktop_app()
        r = client.get('/api/shell/input-methods')
        data = json.loads(r.data)
        self.assertEqual(data['active'], 'us')
        self.assertEqual(len(data['layouts']), 2)

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_switch_layout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_desktop_app()
        r = client.post('/api/shell/input-methods/switch',
                        data=json.dumps({'layout': 'de'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['switched'])

    def test_switch_empty(self):
        client = _make_desktop_app()
        r = client.post('/api/shell/input-methods/switch',
                        data=json.dumps({'layout': ''}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_add_layout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='layout:     us\n')
        client = _make_desktop_app()
        r = client.post('/api/shell/input-methods/add',
                        data=json.dumps({'layout': 'fr'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['added'])

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_compose_key(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_desktop_app()
        r = client.post('/api/shell/input-methods/compose-key',
                        data=json.dumps({'key': 'ralt'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['set'])


# ═══════════════════════════════════════════════════════════════
# Night Light
# ═══════════════════════════════════════════════════════════════

class TestNightLight(unittest.TestCase):

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_get_status(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)  # not running
        client = _make_desktop_app()
        r = client.get('/api/shell/nightlight')
        data = json.loads(r.data)
        self.assertFalse(data['active'])
        self.assertIn('temperature', data)

    @patch('subprocess.Popen')
    @patch('shutil.which', return_value='/usr/bin/gammastep')
    @patch('integrations.agent_engine.shell_desktop_apis._run')
    @patch('integrations.agent_engine.shell_desktop_apis._save_json')
    @patch('integrations.agent_engine.shell_desktop_apis._load_json', return_value={'temperature': 4500})
    def test_toggle_on(self, _load, _save, _run, _which, _popen):
        _run.return_value = MagicMock(returncode=0)
        client = _make_desktop_app()
        r = client.post('/api/shell/nightlight/toggle',
                        data=json.dumps({'enabled': True}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['enabled'])

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    @patch('integrations.agent_engine.shell_desktop_apis._save_json')
    @patch('integrations.agent_engine.shell_desktop_apis._load_json', return_value={})
    def test_toggle_off(self, _load, _save, _run):
        _run.return_value = MagicMock(returncode=0)
        client = _make_desktop_app()
        r = client.post('/api/shell/nightlight/toggle',
                        data=json.dumps({'enabled': False}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertFalse(data['enabled'])

    def test_temperature_out_of_range(self):
        client = _make_desktop_app()
        r = client.post('/api/shell/nightlight/temperature',
                        data=json.dumps({'temperature': 999}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.agent_engine.shell_desktop_apis._save_json')
    @patch('integrations.agent_engine.shell_desktop_apis._load_json', return_value={})
    def test_schedule(self, _load, _save):
        client = _make_desktop_app()
        r = client.post('/api/shell/nightlight/schedule',
                        data=json.dumps({'mode': 'manual', 'start': '21:00', 'end': '07:00'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['set'])

    def test_schedule_invalid_mode(self):
        client = _make_desktop_app()
        r = client.post('/api/shell/nightlight/schedule',
                        data=json.dumps({'mode': 'invalid'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════
# Workspaces
# ═══════════════════════════════════════════════════════════════

class TestWorkspaces(unittest.TestCase):

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    @patch.dict(os.environ, {'SWAYSOCK': '/run/user/1000/sway-ipc.sock'})
    def test_list_workspaces(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([{'num': 1, 'name': 'Main', 'focused': True, 'visible': True}]))
        client = _make_desktop_app()
        r = client.get('/api/shell/workspaces')
        data = json.loads(r.data)
        self.assertEqual(data['compositor'], 'sway')
        self.assertGreater(len(data['workspaces']), 0)

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    @patch('integrations.agent_engine.shell_desktop_apis._is_wayland', return_value=False)
    @patch.dict(os.environ, {}, clear=False)
    def test_list_fallback(self, _wl, mock_run):
        # Remove sway/hyprland env vars so compositor falls through to x11
        os.environ.pop('SWAYSOCK', None)
        os.environ.pop('HYPRLAND_INSTANCE_SIGNATURE', None)
        mock_run.return_value = None  # no wmctrl
        client = _make_desktop_app()
        r = client.get('/api/shell/workspaces')
        data = json.loads(r.data)
        self.assertEqual(len(data['workspaces']), 1)  # default Main

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    @patch.dict(os.environ, {'SWAYSOCK': '/run/user/1000/sway-ipc.sock'})
    def test_create_workspace(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_desktop_app()
        r = client.post('/api/shell/workspaces/create',
                        data=json.dumps({'name': 'Dev'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['created'])

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    @patch.dict(os.environ, {'SWAYSOCK': '/run/user/1000/sway-ipc.sock'})
    def test_switch_workspace(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_desktop_app()
        r = client.post('/api/shell/workspaces/switch',
                        data=json.dumps({'id': 2, 'name': '2'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['switched'])

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    @patch.dict(os.environ, {'SWAYSOCK': '/run/user/1000/sway-ipc.sock'})
    def test_snap_left(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_desktop_app()
        r = client.post('/api/shell/workspaces/snap',
                        data=json.dumps({'position': 'left-half'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['snapped'])

    def test_snap_invalid_position(self):
        client = _make_desktop_app()
        r = client.post('/api/shell/workspaces/snap',
                        data=json.dumps({'position': 'invalid'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════
# Multi-Monitor Management
# ═══════════════════════════════════════════════════════════════

class TestShellMultiMonitor(unittest.TestCase):

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    @patch('integrations.agent_engine.shell_desktop_apis._is_wayland', return_value=True)
    def test_displays_list_wayland(self, _wl, mock_run):
        sway_output = json.dumps([{
            'name': 'HDMI-A-1', 'model': 'Monitor',
            'rect': {'width': 1920, 'height': 1080, 'x': 0, 'y': 0},
            'scale': 1.0, 'active': True,
        }])
        mock_run.return_value = MagicMock(returncode=0, stdout=sway_output)
        client = _make_desktop_app()
        r = client.get('/api/shell/displays')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(len(data['displays']), 1)
        self.assertEqual(data['displays'][0]['name'], 'HDMI-A-1')
        self.assertEqual(data['displays'][0]['resolution'], '1920x1080')
        self.assertEqual(data['compositor'], 'wayland')

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    @patch('integrations.agent_engine.shell_desktop_apis._is_wayland', return_value=False)
    def test_displays_list_x11_fallback(self, _wl, mock_run):
        xrandr_output = 'HDMI-1 connected primary 1920x1080+0+0'
        mock_run.return_value = MagicMock(returncode=0, stdout=xrandr_output)
        client = _make_desktop_app()
        r = client.get('/api/shell/displays')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(len(data['displays']), 1)
        self.assertEqual(data['displays'][0]['name'], 'HDMI-1')
        self.assertTrue(data['displays'][0]['primary'])
        self.assertEqual(data['compositor'], 'x11')

    @patch('integrations.agent_engine.shell_desktop_apis._is_wayland', return_value=True)
    def test_displays_arrange_missing_display(self, _wl):
        client = _make_desktop_app()
        r = client.put('/api/shell/displays/arrange',
                       data=json.dumps({'resolution': '1920x1080'}),
                       content_type='application/json')
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════
# HiDPI Scaling
# ═══════════════════════════════════════════════════════════════

class TestShellHiDPI(unittest.TestCase):

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    @patch('integrations.agent_engine.shell_desktop_apis._is_wayland', return_value=True)
    def test_get_scale_wayland(self, _wl, mock_run):
        sway_output = json.dumps([{'scale': 2.0, 'name': 'eDP-1'}])
        mock_run.return_value = MagicMock(returncode=0, stdout=sway_output)
        client = _make_desktop_app()
        r = client.get('/api/shell/display/scale')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['scale'], 2.0)

    @patch('integrations.agent_engine.shell_desktop_apis._is_wayland', return_value=False)
    @patch.dict(os.environ, {'GDK_SCALE': '2'})
    def test_get_scale_x11(self, _wl):
        client = _make_desktop_app()
        r = client.get('/api/shell/display/scale')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['scale'], 2.0)

    @patch('integrations.agent_engine.shell_desktop_apis._is_wayland', return_value=False)
    def test_put_scale_x11(self, _wl):
        client = _make_desktop_app()
        r = client.put('/api/shell/display/scale',
                       data=json.dumps({'scale': 2}),
                       content_type='application/json')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['scale'], 2)
        self.assertEqual(os.environ.get('GDK_SCALE'), '2')


# ═══════════════════════════════════════════════════════════════
# Per-App Volume Control
# ═══════════════════════════════════════════════════════════════

class TestShellPerAppVolume(unittest.TestCase):

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_list_audio_apps(self, mock_run):
        pw_output = (
            'id 42,\n'
            '    type PipeWire:Interface:Node\n'
            '    application.name = "Firefox"\n'
            '    node.name = "firefox"\n'
            'id 43,\n'
            '    type PipeWire:Interface:Node\n'
            '    application.name = "Spotify"\n'
            '    node.name = "spotify"\n'
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=pw_output)
        client = _make_desktop_app()
        r = client.get('/api/shell/audio/apps')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('apps', data)
        self.assertGreater(len(data['apps']), 0)

    @patch('integrations.agent_engine.shell_desktop_apis._run')
    def test_set_volume_valid(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_desktop_app()
        r = client.put('/api/shell/audio/apps/42/volume',
                       data=json.dumps({'volume': 0.5}),
                       content_type='application/json')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['volume'], 0.5)

    def test_set_volume_invalid_range(self):
        client = _make_desktop_app()
        r = client.put('/api/shell/audio/apps/42/volume',
                       data=json.dumps({'volume': 5.0}),
                       content_type='application/json')
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════
# RTL Layout Support
# ═══════════════════════════════════════════════════════════════

class TestShellRTL(unittest.TestCase):

    @patch.dict(os.environ, {'LANG': 'ar_EG.UTF-8'})
    def test_rtl_for_arabic(self):
        client = _make_desktop_app()
        r = client.get('/api/shell/rtl/status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['rtl'])
        self.assertEqual(data['css_direction'], 'rtl')

    @patch.dict(os.environ, {'LANG': 'en_US.UTF-8'})
    def test_ltr_for_english(self):
        client = _make_desktop_app()
        r = client.get('/api/shell/rtl/status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertFalse(data['rtl'])
        self.assertEqual(data['css_direction'], 'ltr')


if __name__ == '__main__':
    unittest.main()
