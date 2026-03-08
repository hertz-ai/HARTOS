"""
Tests for integrations.agent_engine.shell_os_apis — Extended OS shell APIs.

Covers: notifications, file manager, terminal, user accounts, setup wizard,
backup restore, power, i18n, accessibility, screenshot, devices, upgrades.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock


def _make_os_app():
    """Create a Flask test app with all shell OS routes."""
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    from integrations.agent_engine.shell_os_apis import register_shell_os_routes
    register_shell_os_routes(app)
    return app.test_client()


# ═══════════════════════════════════════════════════════════════
# Notifications
# ═══════════════════════════════════════════════════════════════

class TestShellNotifications(unittest.TestCase):
    """Tests for /api/shell/notifications/*."""

    def test_list_empty(self):
        client = _make_os_app()
        r = client.get('/api/shell/notifications')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('notifications', data)

    def test_send_notification(self):
        client = _make_os_app()
        r = client.post('/api/shell/notifications/send',
                        json={'title': 'Test', 'body': 'Hello'})
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['sent'])
        self.assertIn('notification', data)
        self.assertEqual(data['notification']['title'], 'Test')

    def test_send_then_list(self):
        client = _make_os_app()
        client.post('/api/shell/notifications/send',
                    json={'title': 'A', 'body': 'B'})
        r = client.get('/api/shell/notifications')
        data = json.loads(r.data)
        self.assertGreater(len(data['notifications']), 0)

    def test_mark_read(self):
        client = _make_os_app()
        r = client.post('/api/shell/notifications/send',
                        json={'title': 'X', 'body': 'Y'})
        nid = json.loads(r.data)['notification']['id']
        r = client.post('/api/shell/notifications/read',
                        json={'ids': [nid]})
        data = json.loads(r.data)
        self.assertEqual(data['marked'], 1)

    def test_mark_all_read(self):
        client = _make_os_app()
        client.post('/api/shell/notifications/send',
                    json={'title': 'A', 'body': ''})
        client.post('/api/shell/notifications/send',
                    json={'title': 'B', 'body': ''})
        r = client.post('/api/shell/notifications/read',
                        json={'all': True})
        data = json.loads(r.data)
        self.assertGreaterEqual(data['marked'], 2)

    def test_notification_has_urgency(self):
        client = _make_os_app()
        r = client.post('/api/shell/notifications/send',
                        json={'title': 'T', 'body': 'B', 'urgency': 'critical'})
        data = json.loads(r.data)
        self.assertEqual(data['notification']['urgency'], 'critical')


# ═══════════════════════════════════════════════════════════════
# File Manager
# ═══════════════════════════════════════════════════════════════

class TestShellFileManager(unittest.TestCase):
    """Tests for /api/shell/files/*."""

    def test_browse_home(self):
        client = _make_os_app()
        r = client.get('/api/shell/files/browse')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('entries', data)
        self.assertIn('path', data)
        self.assertIn('parent', data)
        self.assertIsInstance(data['entries'], list)

    def test_browse_with_path(self):
        client = _make_os_app()
        r = client.get('/api/shell/files/browse?path=' + tempfile.gettempdir())
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('entries', data)

    def test_browse_invalid_path(self):
        client = _make_os_app()
        r = client.get('/api/shell/files/browse?path=/nonexistent_dir_xyz')
        # Path outside allowed roots (home + /tmp) returns 403
        self.assertIn(r.status_code, (400, 403))

    def test_mkdir_and_delete(self):
        client = _make_os_app()
        test_dir = os.path.join(tempfile.gettempdir(), 'hart_test_mkdir')
        try:
            r = client.post('/api/shell/files/mkdir', json={'path': test_dir})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(os.path.isdir(test_dir))

            with patch('integrations.agent_engine.shell_os_apis._classify_destructive',
                        return_value=True):
                r = client.post('/api/shell/files/delete', json={'path': test_dir})
            self.assertEqual(r.status_code, 200)
        finally:
            if os.path.isdir(test_dir):
                os.rmdir(test_dir)

    def test_mkdir_no_path(self):
        client = _make_os_app()
        r = client.post('/api/shell/files/mkdir', json={'path': ''})
        self.assertEqual(r.status_code, 400)

    def test_move_file(self):
        client = _make_os_app()
        src = os.path.join(tempfile.gettempdir(), 'hart_test_src.txt')
        dst = os.path.join(tempfile.gettempdir(), 'hart_test_dst.txt')
        try:
            with open(src, 'w') as f:
                f.write('test')
            r = client.post('/api/shell/files/move',
                            json={'source': src, 'destination': dst})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(os.path.isfile(dst))
            self.assertFalse(os.path.isfile(src))
        finally:
            for p in (src, dst):
                if os.path.isfile(p):
                    os.remove(p)

    def test_copy_file(self):
        client = _make_os_app()
        src = os.path.join(tempfile.gettempdir(), 'hart_test_copy_src.txt')
        dst = os.path.join(tempfile.gettempdir(), 'hart_test_copy_dst.txt')
        try:
            with open(src, 'w') as f:
                f.write('copy test')
            r = client.post('/api/shell/files/copy',
                            json={'source': src, 'destination': dst})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(os.path.isfile(src))
            self.assertTrue(os.path.isfile(dst))
        finally:
            for p in (src, dst):
                if os.path.isfile(p):
                    os.remove(p)

    def test_file_info(self):
        client = _make_os_app()
        src = os.path.join(tempfile.gettempdir(), 'hart_test_info.txt')
        try:
            with open(src, 'w') as f:
                f.write('info test')
            r = client.get(f'/api/shell/files/info?path={src}')
            self.assertEqual(r.status_code, 200)
            data = json.loads(r.data)
            self.assertEqual(data['name'], 'hart_test_info.txt')
            self.assertIn('size', data)
            self.assertIn('modified', data)
        finally:
            if os.path.isfile(src):
                os.remove(src)

    def test_file_info_not_found(self):
        client = _make_os_app()
        r = client.get('/api/shell/files/info?path=/nonexistent_file.xyz')
        self.assertEqual(r.status_code, 404)

    def test_entries_sorted_dirs_first(self):
        client = _make_os_app()
        test_dir = os.path.join(tempfile.gettempdir(), 'hart_test_sort')
        try:
            os.makedirs(os.path.join(test_dir, 'subdir'), exist_ok=True)
            with open(os.path.join(test_dir, 'afile.txt'), 'w') as f:
                f.write('x')
            r = client.get(f'/api/shell/files/browse?path={test_dir}')
            data = json.loads(r.data)
            entries = data['entries']
            if len(entries) >= 2:
                # Dirs should come before files
                dirs = [e for e in entries if e['is_dir']]
                files = [e for e in entries if not e['is_dir']]
                if dirs and files:
                    dir_idx = entries.index(dirs[0])
                    file_idx = entries.index(files[0])
                    self.assertLess(dir_idx, file_idx)
        finally:
            import shutil
            if os.path.isdir(test_dir):
                shutil.rmtree(test_dir)

    def test_delete_not_found(self):
        client = _make_os_app()
        r = client.post('/api/shell/files/delete',
                        json={'path': '/nonexistent_xyz_file'})
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════
# Terminal
# ═══════════════════════════════════════════════════════════════

class TestShellTerminal(unittest.TestCase):
    """Tests for /api/shell/terminal/*."""

    def test_exec_command(self):
        client = _make_os_app()
        r = client.post('/api/shell/terminal/exec',
                        json={'command': 'echo hello'})
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('hello', data['stdout'])
        self.assertEqual(data['returncode'], 0)

    def test_exec_no_command(self):
        client = _make_os_app()
        r = client.post('/api/shell/terminal/exec', json={'command': ''})
        self.assertEqual(r.status_code, 400)

    def test_exec_blocked_command(self):
        client = _make_os_app()
        r = client.post('/api/shell/terminal/exec',
                        json={'command': 'rm -rf /'})
        self.assertEqual(r.status_code, 403)

    def test_exec_with_cwd(self):
        client = _make_os_app()
        cwd = tempfile.gettempdir()
        r = client.post('/api/shell/terminal/exec',
                        json={'command': 'echo ok', 'cwd': cwd})
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('ok', data['stdout'])

    def test_sessions_empty(self):
        client = _make_os_app()
        r = client.get('/api/shell/terminal/sessions')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('sessions', data)

    def test_exec_timeout(self):
        client = _make_os_app()
        r = client.post('/api/shell/terminal/exec',
                        json={'command': 'sleep 60', 'timeout': 1})
        self.assertEqual(r.status_code, 408)


# ═══════════════════════════════════════════════════════════════
# User Accounts
# ═══════════════════════════════════════════════════════════════

class TestShellUsers(unittest.TestCase):
    """Tests for /api/shell/users/*."""

    def test_list_users(self):
        client = _make_os_app()
        r = client.get('/api/shell/users')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('users', data)
        self.assertIsInstance(data['users'], list)
        self.assertGreater(len(data['users']), 0)

    def test_user_has_fields(self):
        client = _make_os_app()
        r = client.get('/api/shell/users')
        data = json.loads(r.data)
        user = data['users'][0]
        self.assertIn('username', user)
        self.assertIn('home', user)

    def test_create_no_username(self):
        client = _make_os_app()
        r = client.post('/api/shell/users/create',
                        json={'username': ''})
        self.assertEqual(r.status_code, 400)

    def test_create_invalid_username(self):
        client = _make_os_app()
        r = client.post('/api/shell/users/create',
                        json={'username': 'a'})  # too short
        self.assertEqual(r.status_code, 400)

    def test_delete_protected_user(self):
        client = _make_os_app()
        r = client.post('/api/shell/users/delete',
                        json={'username': 'root'})
        self.assertEqual(r.status_code, 403)

    def test_delete_hart_user_blocked(self):
        client = _make_os_app()
        r = client.post('/api/shell/users/delete',
                        json={'username': 'hart'})
        self.assertEqual(r.status_code, 403)


# ═══════════════════════════════════════════════════════════════
# First-Time Setup Wizard
# ═══════════════════════════════════════════════════════════════

class TestShellSetupWizard(unittest.TestCase):
    """Tests for /api/shell/setup/*."""

    def test_status(self):
        client = _make_os_app()
        r = client.get('/api/shell/setup/status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('steps', data)
        self.assertIn('wizard_completed', data)
        self.assertEqual(len(data['steps']), 5)

    def test_step_names(self):
        client = _make_os_app()
        r = client.get('/api/shell/setup/status')
        data = json.loads(r.data)
        step_ids = [s['id'] for s in data['steps']]
        self.assertIn('welcome', step_ids)
        self.assertIn('network', step_ids)
        self.assertIn('account', step_ids)
        self.assertIn('ai_models', step_ids)
        self.assertIn('privacy', step_ids)

    def test_complete_step(self):
        client = _make_os_app()
        with patch.dict(os.environ, {'HEVOLVE_DATA_DIR': tempfile.gettempdir()}):
            r = client.post('/api/shell/setup/step',
                            json={'step': 'welcome', 'data': {'accepted': True}})
            self.assertEqual(r.status_code, 200)
            data = json.loads(r.data)
            self.assertEqual(data['step'], 'welcome')

    def test_complete_all_steps(self):
        client = _make_os_app()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {'HEVOLVE_DATA_DIR': tmpdir}):
                for step in ['welcome', 'network', 'account', 'ai_models', 'privacy']:
                    r = client.post('/api/shell/setup/step',
                                    json={'step': step, 'data': {}})
                    self.assertEqual(r.status_code, 200)
                data = json.loads(r.data)
                self.assertTrue(data['completed'])


# ═══════════════════════════════════════════════════════════════
# Power Management
# ═══════════════════════════════════════════════════════════════

class TestShellPower(unittest.TestCase):
    """Tests for /api/shell/power/*."""

    def test_profiles(self):
        client = _make_os_app()
        r = client.get('/api/shell/power/profiles')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('profiles', data)
        self.assertIn('active', data)
        self.assertIn('performance', data['profiles'])
        self.assertIn('balanced', data['profiles'])
        self.assertIn('powersave', data['profiles'])

    def test_set_invalid_profile(self):
        client = _make_os_app()
        r = client.post('/api/shell/power/set',
                        json={'profile': 'turbo'})
        self.assertEqual(r.status_code, 400)

    def test_action_invalid(self):
        client = _make_os_app()
        with patch('integrations.agent_engine.shell_os_apis._classify_destructive',
                    return_value=True):
            r = client.post('/api/shell/power/action',
                            json={'action': 'destroy'})
        self.assertEqual(r.status_code, 400)

    def test_checkpoint(self):
        client = _make_os_app()
        r = client.post('/api/shell/power/checkpoint')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['checkpointed'])

    def test_resume(self):
        client = _make_os_app()
        r = client.post('/api/shell/power/resume')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['resumed'])


# ═══════════════════════════════════════════════════════════════
# i18n
# ═══════════════════════════════════════════════════════════════

class TestShellI18n(unittest.TestCase):
    """Tests for /api/shell/i18n/*."""

    def test_locales(self):
        client = _make_os_app()
        r = client.get('/api/shell/i18n/locales')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('locales', data)
        self.assertIn('current', data)
        codes = [l['code'] for l in data['locales']]
        self.assertIn('en', codes)
        self.assertIn('es', codes)
        self.assertIn('ja', codes)

    def test_rtl_locale(self):
        client = _make_os_app()
        r = client.get('/api/shell/i18n/locales')
        data = json.loads(r.data)
        ar = [l for l in data['locales'] if l['code'] == 'ar'][0]
        self.assertTrue(ar['rtl'])

    def test_set_locale(self):
        client = _make_os_app()
        r = client.post('/api/shell/i18n/set', json={'locale': 'ja'})
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['set'])
        self.assertEqual(data['locale'], 'ja')

    def test_strings_empty(self):
        client = _make_os_app()
        r = client.get('/api/shell/i18n/strings')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('strings', data)
        self.assertIn('locale', data)

    def test_strings_with_file(self):
        client = _make_os_app()
        with tempfile.TemporaryDirectory() as tmpdir:
            locale_file = os.path.join(tmpdir, 'test.json')
            with open(locale_file, 'w') as f:
                json.dump({'hello': 'Hola', 'goodbye': 'Adiós'}, f)
            with patch.dict(os.environ, {'HART_LOCALE_DIR': tmpdir}):
                r = client.get('/api/shell/i18n/strings?locale=test')
                data = json.loads(r.data)
                self.assertEqual(data['strings'].get('hello'), 'Hola')
                self.assertEqual(data['count'], 2)


# ═══════════════════════════════════════════════════════════════
# Accessibility
# ═══════════════════════════════════════════════════════════════

class TestShellAccessibility(unittest.TestCase):
    """Tests for /api/shell/accessibility."""

    def test_get_defaults(self):
        client = _make_os_app()
        r = client.get('/api/shell/accessibility')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['font_scale'], 1.0)
        self.assertFalse(data['high_contrast'])
        self.assertFalse(data['reduced_motion'])

    def test_set_font_scale(self):
        client = _make_os_app()
        r = client.put('/api/shell/accessibility',
                       json={'font_scale': 1.5})
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['font_scale'], 1.5)

    def test_set_high_contrast(self):
        client = _make_os_app()
        r = client.put('/api/shell/accessibility',
                       json={'high_contrast': True, 'large_cursor': True})
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['high_contrast'])
        self.assertTrue(data['large_cursor'])


# ═══════════════════════════════════════════════════════════════
# Screenshot
# ═══════════════════════════════════════════════════════════════

class TestShellScreenshot(unittest.TestCase):
    """Tests for /api/shell/screenshot."""

    @patch('subprocess.run')
    def test_screenshot_with_grim(self, mock_run):
        """Test screenshot with grim (Wayland)."""
        client = _make_os_app()

        def side_effect(cmd, **kwargs):
            # Simulate grim creating a file
            if cmd[0] == 'grim':
                with open(cmd[1], 'w') as f:
                    f.write('PNG')
            m = MagicMock()
            m.returncode = 0
            return m

        mock_run.side_effect = side_effect
        with tempfile.TemporaryDirectory() as tmpdir:
            r = client.post('/api/shell/screenshot',
                            json={'output_dir': tmpdir})
            self.assertEqual(r.status_code, 200)
            data = json.loads(r.data)
            self.assertTrue(data['captured'])
            self.assertIn('path', data)

    def test_screenshot_no_tool(self):
        """Test screenshot when no tool available."""
        client = _make_os_app()
        with patch('subprocess.run', side_effect=FileNotFoundError):
            with patch.dict('sys.modules', {'mss': None}):
                r = client.post('/api/shell/screenshot')
                # May succeed via mss or fail gracefully
                self.assertIn(r.status_code, (200, 501))


# ═══════════════════════════════════════════════════════════════
# Recording
# ═══════════════════════════════════════════════════════════════

class TestShellRecording(unittest.TestCase):
    """Tests for /api/shell/recording/*."""

    def test_stop_no_pid(self):
        client = _make_os_app()
        r = client.post('/api/shell/recording/stop', json={})
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════
# Devices (Compute Mesh)
# ═══════════════════════════════════════════════════════════════

class TestShellDevices(unittest.TestCase):
    """Tests for /api/shell/devices/*."""

    def test_list_devices(self):
        client = _make_os_app()
        r = client.get('/api/shell/devices')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('peers', data)

    def test_pair_no_address(self):
        client = _make_os_app()
        r = client.post('/api/shell/devices/pair', json={'address': ''})
        self.assertEqual(r.status_code, 400)

    def test_unpair_no_id(self):
        client = _make_os_app()
        r = client.post('/api/shell/devices/unpair', json={'device_id': ''})
        self.assertEqual(r.status_code, 400)

    def test_unpair_not_found(self):
        client = _make_os_app()
        r = client.post('/api/shell/devices/unpair',
                        json={'device_id': 'nonexistent'})
        self.assertEqual(r.status_code, 404)


# ═══════════════════════════════════════════════════════════════
# Upgrades API
# ═══════════════════════════════════════════════════════════════

class TestShellUpgrades(unittest.TestCase):
    """Tests for /api/upgrades/*."""

    def test_status(self):
        client = _make_os_app()
        r = client.get('/api/upgrades/status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('stage', data)

    def test_start_no_version(self):
        client = _make_os_app()
        r = client.post('/api/upgrades/start', json={'version': ''})
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════
# Backup Restore
# ═══════════════════════════════════════════════════════════════

class TestShellBackup(unittest.TestCase):
    """Tests for /api/shell/backup/*."""

    def test_list_backups(self):
        client = _make_os_app()
        r = client.get('/api/shell/backup/list')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('backups', data)

    def test_restore_missing_fields(self):
        client = _make_os_app()
        r = client.post('/api/shell/backup/restore', json={})
        self.assertEqual(r.status_code, 400)

    def test_restore_missing_passphrase(self):
        client = _make_os_app()
        r = client.post('/api/shell/backup/restore',
                        json={'user_id': 1})
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════
# Battery / Lid
# ═══════════════════════════════════════════════════════════════

class TestShellBattery(unittest.TestCase):
    """Tests for /api/shell/battery and /api/shell/power/lid."""

    @patch('integrations.agent_engine.shell_os_apis.os.path.isdir', return_value=False)
    def test_battery_status_no_battery(self, _mock_isdir):
        client = _make_os_app()
        r = client.get('/api/shell/battery')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertFalse(data['has_battery'])

    def test_battery_status_with_battery(self):
        """Battery endpoint returns level and charging state when battery present."""
        client = _make_os_app()
        bat_base = '/sys/class/power_supply'
        # Use os.path.join so keys match Windows backslash convention
        file_contents = {
            os.path.join(bat_base, 'BAT0', 'type'): 'Battery\n',
            os.path.join(bat_base, 'BAT0', 'capacity'): '75\n',
            os.path.join(bat_base, 'BAT0', 'status'): 'Charging\n',
        }
        _real_isdir = os.path.isdir
        _real_isfile = os.path.isfile
        _real_listdir = os.listdir
        _real_open = open

        def mock_isdir(p):
            if 'power_supply' in str(p):
                return p == bat_base
            return _real_isdir(p)

        def mock_isfile(p):
            if 'power_supply' in str(p):
                return p in file_contents
            return _real_isfile(p)

        def mock_listdir(p):
            if p == bat_base:
                return ['BAT0']
            return _real_listdir(p)

        def smart_open(path, *args, **kwargs):
            if path in file_contents:
                from io import StringIO
                return StringIO(file_contents[path])
            return _real_open(path, *args, **kwargs)

        with patch('os.path.isdir', side_effect=mock_isdir):
            with patch('os.path.isfile', side_effect=mock_isfile):
                with patch('os.listdir', side_effect=mock_listdir):
                    with patch('builtins.open', side_effect=smart_open):
                        r = client.get('/api/shell/battery')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['has_battery'])
        self.assertEqual(data['level'], 75)
        self.assertEqual(data['charging'], 'Charging')

    def test_battery_ac_power(self):
        """Battery endpoint returns ac_power field when AC adapter present."""
        client = _make_os_app()
        bat_base = '/sys/class/power_supply'
        file_contents = {
            os.path.join(bat_base, 'BAT0', 'type'): 'Battery\n',
            os.path.join(bat_base, 'BAT0', 'capacity'): '90\n',
            os.path.join(bat_base, 'BAT0', 'status'): 'Full\n',
            os.path.join(bat_base, 'AC0', 'online'): '1\n',
        }
        _real_isdir = os.path.isdir
        _real_isfile = os.path.isfile
        _real_listdir = os.listdir
        _real_open = open

        def mock_isdir(p):
            if 'power_supply' in str(p):
                return p == bat_base
            return _real_isdir(p)

        def mock_isfile(p):
            if 'power_supply' in str(p):
                return p in file_contents
            return _real_isfile(p)

        def mock_listdir(p):
            if p == bat_base:
                return ['BAT0']
            return _real_listdir(p)

        def smart_open(path, *args, **kwargs):
            if path in file_contents:
                from io import StringIO
                return StringIO(file_contents[path])
            return _real_open(path, *args, **kwargs)

        with patch('os.path.isdir', side_effect=mock_isdir):
            with patch('os.path.isfile', side_effect=mock_isfile):
                with patch('os.listdir', side_effect=mock_listdir):
                    with patch('builtins.open', side_effect=smart_open):
                        r = client.get('/api/shell/battery')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data.get('ac_power'))

    def test_lid_get_default(self):
        client = _make_os_app()
        r = client.get('/api/shell/power/lid')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['action'], 'suspend')
        self.assertIn('valid_actions', data)

    def test_lid_put_valid(self):
        client = _make_os_app()
        r = client.put('/api/shell/power/lid', json={'action': 'hibernate'})
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['action'], 'hibernate')

    def test_lid_put_invalid(self):
        client = _make_os_app()
        r = client.put('/api/shell/power/lid', json={'action': 'explode'})
        self.assertEqual(r.status_code, 400)
        data = json.loads(r.data)
        self.assertIn('error', data)


# ═══════════════════════════════════════════════════════════════
# WiFi Management
# ═══════════════════════════════════════════════════════════════

class TestShellWiFi(unittest.TestCase):
    """Tests for /api/shell/wifi/* routes."""

    @patch('integrations.agent_engine.shell_os_apis.subprocess')
    def test_wifi_scan_returns_networks(self, mock_sub):
        client = _make_os_app()
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = 'MyNet:85:WPA2:AA:BB:CC\nOpenNet:60::DD:EE:FF\n'
        proc.stderr = ''
        mock_sub.run.return_value = proc
        mock_sub.TimeoutExpired = Exception
        r = client.get('/api/shell/wifi/scan')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(len(data['networks']), 2)
        self.assertEqual(data['networks'][0]['ssid'], 'MyNet')
        self.assertEqual(data['networks'][0]['signal'], 85)
        self.assertEqual(data['networks'][0]['security'], 'WPA2')

    @patch('integrations.agent_engine.shell_os_apis.subprocess')
    def test_wifi_scan_empty(self, mock_sub):
        client = _make_os_app()
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ''
        proc.stderr = ''
        mock_sub.run.return_value = proc
        mock_sub.TimeoutExpired = Exception
        r = client.get('/api/shell/wifi/scan')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['networks'], [])

    @patch('integrations.agent_engine.shell_os_apis.subprocess')
    def test_wifi_scan_nmcli_not_found(self, mock_sub):
        client = _make_os_app()
        mock_sub.run.side_effect = FileNotFoundError
        mock_sub.TimeoutExpired = Exception
        r = client.get('/api/shell/wifi/scan')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['networks'], [])
        self.assertEqual(data['error'], 'nmcli not available')

    @patch('integrations.agent_engine.shell_os_apis.subprocess')
    def test_wifi_connect_success(self, mock_sub):
        client = _make_os_app()
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = 'successfully activated'
        proc.stderr = ''
        mock_sub.run.return_value = proc
        mock_sub.TimeoutExpired = Exception
        r = client.post('/api/shell/wifi/connect',
                        json={'ssid': 'MyNet', 'password': '1234'})
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['status'], 'connected')
        self.assertEqual(data['ssid'], 'MyNet')

    @patch('integrations.agent_engine.shell_os_apis.subprocess')
    def test_wifi_connect_missing_ssid(self, mock_sub):
        client = _make_os_app()
        r = client.post('/api/shell/wifi/connect', json={})
        self.assertEqual(r.status_code, 400)
        data = json.loads(r.data)
        self.assertIn('error', data)

    @patch('integrations.agent_engine.shell_os_apis.subprocess')
    def test_wifi_status(self, mock_sub):
        client = _make_os_app()
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = 'MyNet:802-11-wireless:wlan0:activated\n'
        proc.stderr = ''
        mock_sub.run.return_value = proc
        r = client.get('/api/shell/wifi/status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['connected'])
        self.assertEqual(data['connection']['name'], 'MyNet')
        self.assertEqual(data['connection']['device'], 'wlan0')


# ═══════════════════════════════════════════════════════════════
# VPN Management
# ═══════════════════════════════════════════════════════════════

class TestShellVPN(unittest.TestCase):
    """Tests for /api/shell/vpn/* routes."""

    @patch('integrations.agent_engine.shell_os_apis.subprocess')
    def test_vpn_list(self, mock_sub):
        client = _make_os_app()
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = 'MyVPN:vpn:activated\nWork:vpn:deactivated\n'
        proc.stderr = ''
        mock_sub.run.return_value = proc
        r = client.get('/api/shell/vpn/list')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(len(data['vpns']), 2)
        self.assertEqual(data['vpns'][0]['name'], 'MyVPN')

    @patch('integrations.agent_engine.shell_os_apis.subprocess')
    def test_vpn_connect_success(self, mock_sub):
        client = _make_os_app()
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = 'Connection successfully activated'
        proc.stderr = ''
        mock_sub.run.return_value = proc
        mock_sub.TimeoutExpired = Exception
        r = client.post('/api/shell/vpn/connect', json={'name': 'MyVPN'})
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['status'], 'connected')
        self.assertEqual(data['name'], 'MyVPN')

    @patch('integrations.agent_engine.shell_os_apis.subprocess')
    def test_vpn_connect_missing_name(self, mock_sub):
        client = _make_os_app()
        r = client.post('/api/shell/vpn/connect', json={})
        self.assertEqual(r.status_code, 400)
        data = json.loads(r.data)
        self.assertIn('error', data)

    @patch('integrations.agent_engine.shell_os_apis.subprocess')
    def test_vpn_disconnect(self, mock_sub):
        client = _make_os_app()
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = 'Connection successfully deactivated'
        proc.stderr = ''
        mock_sub.run.return_value = proc
        r = client.post('/api/shell/vpn/disconnect', json={'name': 'MyVPN'})
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['status'], 'disconnected')

    @patch('integrations.agent_engine.shell_os_apis.subprocess')
    @patch('integrations.agent_engine.shell_os_apis.os.path.isfile', return_value=True)
    def test_vpn_import_wireguard(self, _mock_isfile, mock_sub):
        client = _make_os_app()
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = 'Connection imported'
        proc.stderr = ''
        mock_sub.run.return_value = proc
        r = client.post('/api/shell/vpn/import',
                        json={'path': '/tmp/test.conf'})
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['status'], 'imported')


# ═══════════════════════════════════════════════════════════════
# Trash / Recycle Bin
# ═══════════════════════════════════════════════════════════════

class TestShellTrash(unittest.TestCase):
    """Tests for /api/shell/trash routes using real temp dirs."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix='hart_trash_test_')
        self._trash_root = os.path.join(self._tmpdir, '.local', 'share', 'Trash')
        self._files_dir = os.path.join(self._trash_root, 'files')
        self._info_dir = os.path.join(self._trash_root, 'info')

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _patch_trash_dir(self):
        """Return a patch that redirects _trash_dir() to our temp trash root."""
        return patch('integrations.agent_engine.shell_os_apis.os.path.expanduser',
                     return_value=self._tmpdir)

    def test_trash_list_empty(self):
        client = _make_os_app()
        with self._patch_trash_dir():
            r = client.get('/api/shell/trash')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['items'], [])
        self.assertEqual(data['total'], 0)

    def test_trash_file(self):
        client = _make_os_app()
        # Create a source file to trash
        src_file = os.path.join(self._tmpdir, 'myfile.txt')
        with open(src_file, 'w') as f:
            f.write('hello')
        with self._patch_trash_dir():
            r = client.post('/api/shell/trash', json={'path': src_file})
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['status'], 'trashed')
        # Verify file moved to trash files dir
        self.assertTrue(os.path.isdir(self._files_dir))
        trashed_files = os.listdir(self._files_dir)
        self.assertGreater(len(trashed_files), 0)
        # Verify .trashinfo was created
        self.assertTrue(os.path.isdir(self._info_dir))
        info_files = [f for f in os.listdir(self._info_dir) if f.endswith('.trashinfo')]
        self.assertGreater(len(info_files), 0)

    def test_trash_restore(self):
        client = _make_os_app()
        # Pre-populate trash
        os.makedirs(self._files_dir, exist_ok=True)
        os.makedirs(self._info_dir, exist_ok=True)
        restore_dest = os.path.join(self._tmpdir, 'restored.txt')
        with open(os.path.join(self._files_dir, 'restored.txt'), 'w') as f:
            f.write('content')
        info_content = (
            "[Trash Info]\n"
            f"Path={restore_dest}\n"
            "DeletionDate=2026-03-05T12:00:00\n"
        )
        with open(os.path.join(self._info_dir, 'restored.txt.trashinfo'), 'w') as f:
            f.write(info_content)
        with self._patch_trash_dir():
            r = client.post('/api/shell/trash/restore', json={'name': 'restored.txt'})
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['status'], 'restored')
        self.assertTrue(os.path.isfile(restore_dest))

    def test_trash_empty(self):
        client = _make_os_app()
        # Pre-populate trash with files
        os.makedirs(self._files_dir, exist_ok=True)
        os.makedirs(self._info_dir, exist_ok=True)
        with open(os.path.join(self._files_dir, 'a.txt'), 'w') as f:
            f.write('a')
        with open(os.path.join(self._files_dir, 'b.txt'), 'w') as f:
            f.write('b')
        with open(os.path.join(self._info_dir, 'a.txt.trashinfo'), 'w') as f:
            f.write('[Trash Info]\nPath=/tmp/a.txt\n')
        with self._patch_trash_dir():
            r = client.post('/api/shell/trash/empty')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['status'], 'emptied')
        self.assertGreater(data['removed'], 0)
        # Verify dirs are empty
        self.assertEqual(os.listdir(self._files_dir), [])

    def test_trash_missing_path(self):
        client = _make_os_app()
        r = client.post('/api/shell/trash', json={'path': '/nonexistent_xyz'})
        self.assertEqual(r.status_code, 400)
        data = json.loads(r.data)
        self.assertIn('error', data)

    def test_trash_list_with_items(self):
        client = _make_os_app()
        # Pre-populate info dir with .trashinfo files
        os.makedirs(self._info_dir, exist_ok=True)
        for name in ['doc.txt', 'pic.png']:
            info_content = (
                "[Trash Info]\n"
                f"Path=/home/user/{name}\n"
                "DeletionDate=2026-03-05T10:00:00\n"
            )
            with open(os.path.join(self._info_dir, f'{name}.trashinfo'), 'w') as f:
                f.write(info_content)
        with self._patch_trash_dir():
            r = client.get('/api/shell/trash')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['total'], 2)
        names = [item['name'] for item in data['items']]
        self.assertIn('doc.txt', names)
        self.assertIn('pic.png', names)


# ═══════════════════════════════════════════════════════════════
# Notes App
# ═══════════════════════════════════════════════════════════════

def _make_notes_app(notes_dir):
    """Create a Flask test app with _NOTES_DIR pointing to notes_dir.

    _NOTES_DIR is a closure variable captured at route-registration time,
    so we must redirect the path computation *before* registering routes.
    """
    from flask import Flask
    import integrations.agent_engine.shell_os_apis as mod

    real_join = os.path.join
    # The _NOTES_DIR computation is:
    #   os.path.join(os.path.dirname(os.path.dirname(
    #       os.path.dirname(os.path.abspath(__file__)))), 'agent_data', 'notes')
    # We intercept join calls that end with ('agent_data', 'notes') to redirect.
    def patched_join(*args):
        result = real_join(*args)
        if len(args) >= 3 and args[-2] == 'agent_data' and args[-1] == 'notes':
            return notes_dir
        return result

    app = Flask(__name__)
    app.config['TESTING'] = True
    with patch('os.path.join', side_effect=patched_join):
        mod.register_shell_os_routes(app)
    return app.test_client()


class TestShellNotes(unittest.TestCase):
    """Tests for /api/shell/notes routes."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix='hart_notes_test_')

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_notes_save_and_list(self):
        client = _make_notes_app(self._tmpdir)
        r = client.post('/api/shell/notes',
                        json={'title': 'Test Note', 'content': 'Hello world'})
        self.assertEqual(r.status_code, 201)
        data = json.loads(r.data)
        self.assertEqual(data['status'], 'saved')
        # List and verify
        r = client.get('/api/shell/notes')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertGreater(len(data['notes']), 0)
        titles = [n['title'] for n in data['notes']]
        self.assertIn('Test Note', titles)

    def test_notes_delete(self):
        client = _make_notes_app(self._tmpdir)
        # Create a note first
        r = client.post('/api/shell/notes',
                        json={'title': 'ToDelete', 'content': 'Bye'})
        self.assertEqual(r.status_code, 201)
        note_id = json.loads(r.data)['id']
        # Delete it
        r = client.delete(f'/api/shell/notes/{note_id}')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['status'], 'deleted')

    def test_notes_save_missing_content(self):
        client = _make_os_app()
        r = client.post('/api/shell/notes', json={'title': 'T'})
        self.assertEqual(r.status_code, 400)
        data = json.loads(r.data)
        self.assertIn('error', data)


# ═══════════════════════════════════════════════════════════════
# Open With
# ═══════════════════════════════════════════════════════════════

class TestShellOpenWith(unittest.TestCase):
    """Tests for /api/shell/open-with."""

    def test_open_with_missing_path(self):
        client = _make_os_app()
        r = client.post('/api/shell/open-with', json={})
        self.assertEqual(r.status_code, 400)
        data = json.loads(r.data)
        self.assertIn('error', data)

    def test_open_with_file_not_found(self):
        client = _make_os_app()
        r = client.post('/api/shell/open-with',
                        json={'path': '/nonexistent_file_xyz.txt'})
        self.assertEqual(r.status_code, 404)
        data = json.loads(r.data)
        self.assertIn('error', data)

    def test_open_with_outside_sandbox(self):
        client = _make_os_app()
        # Create a temp file outside allowed roots then simulate with a path
        # that resolves outside ~ and /tmp
        with patch('integrations.agent_engine.shell_os_apis.os.path.isfile', return_value=True):
            with patch('integrations.agent_engine.shell_os_apis.os.path.realpath',
                       return_value='/etc/shadow'):
                with patch('integrations.agent_engine.shell_os_apis.os.path.expanduser',
                           return_value='/home/testuser'):
                    r = client.post('/api/shell/open-with',
                                    json={'path': '/etc/shadow'})
        self.assertEqual(r.status_code, 403)
        data = json.loads(r.data)
        self.assertIn('error', data)


if __name__ == '__main__':
    unittest.main()
