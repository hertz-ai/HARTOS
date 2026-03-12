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


# ═══════════════════════════════════════════════════════════════
# App Store (Feature 6 - P0 OS Credibility)
# ═══════════════════════════════════════════════════════════════

class TestAppStore(unittest.TestCase):
    """Tests for app search, install, uninstall endpoints."""

    def test_search_no_query(self):
        """Search without q parameter returns 400."""
        client = _make_os_app()
        r = client.get('/api/apps/search')
        self.assertEqual(r.status_code, 400)
        data = json.loads(r.data)
        self.assertIn('error', data)

    @patch('integrations.agent_engine.shell_os_apis.AppInstaller', create=True)
    def test_search_with_query(self, _mock_cls):
        """Search with query delegates to AppInstaller."""
        client = _make_os_app()
        with patch('integrations.agent_engine.app_installer.AppInstaller') as mock_ai:
            mock_inst = MagicMock()
            mock_inst.search.return_value = [{'name': 'firefox', 'platform': 'flatpak'}]
            mock_ai.return_value = mock_inst
            r = client.get('/api/apps/search?q=firefox')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['query'], 'firefox')
        self.assertIn('results', data)
        self.assertIn('count', data)

    def test_search_empty_query(self):
        """Empty string query returns 400."""
        client = _make_os_app()
        r = client.get('/api/apps/search?q=')
        self.assertEqual(r.status_code, 400)

    def test_installed_apps(self):
        """Installed apps endpoint returns list."""
        client = _make_os_app()
        r = client.get('/api/apps/installed')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('apps', data)
        self.assertIn('count', data)
        self.assertIsInstance(data['apps'], list)

    @patch('integrations.agent_engine.shell_os_apis._require_shell_auth',
           lambda f: f)
    def test_install_no_source(self):
        """Install without source returns 400."""
        client = _make_os_app()
        r = client.post('/api/apps/install',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)
        data = json.loads(r.data)
        self.assertIn('error', data)

    @patch('integrations.agent_engine.shell_os_apis._require_shell_auth',
           lambda f: f)
    def test_uninstall_no_app_id(self):
        """Uninstall without app_id returns 400."""
        client = _make_os_app()
        r = client.post('/api/apps/uninstall',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)
        data = json.loads(r.data)
        self.assertIn('error', data)


# ═══════════════════════════════════════════════════════════════
# Cloud File Sync (P1 Daily Driver)
# ═══════════════════════════════════════════════════════════════

class TestCloudSync(unittest.TestCase):
    """Tests for cloud file sync (rclone wrapper) endpoints."""

    @patch('integrations.agent_engine.shell_os_apis.subprocess.run',
           side_effect=FileNotFoundError)
    def test_remotes_rclone_not_installed(self, mock_run):
        """When rclone not installed, returns empty list."""
        client = _make_os_app()
        r = client.get('/api/shell/cloud-sync/remotes')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['remotes'], [])
        self.assertFalse(data['rclone_available'])

    @patch('integrations.agent_engine.shell_os_apis.subprocess.run')
    def test_remotes_with_rclone(self, mock_run):
        """rclone listremotes returns configured remotes."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout='gdrive:\ns3bucket:\n')
        client = _make_os_app()
        r = client.get('/api/shell/cloud-sync/remotes')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(len(data['remotes']), 2)
        names = [rem['name'] for rem in data['remotes']]
        self.assertIn('gdrive', names)

    def test_pairs_empty(self):
        """No pairs configured returns empty list."""
        client = _make_os_app()
        r = client.get('/api/shell/cloud-sync/pairs')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIsInstance(data['pairs'], list)

    def test_add_pair_missing_fields(self):
        """Adding pair without required fields returns 400."""
        client = _make_os_app()
        r = client.post('/api/shell/cloud-sync/pairs',
                        data=json.dumps({'local_path': '/home/user/docs'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.agent_engine.shell_os_apis.subprocess.run')
    def test_sync_status(self, mock_run):
        """Sync status shows rclone availability."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout='rclone v1.65.0\n')
        client = _make_os_app()
        r = client.get('/api/shell/cloud-sync/status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['rclone_installed'])
        self.assertIn('rclone v', data['rclone_version'])

    def test_run_no_pairs(self):
        """Running sync with no pairs returns 400."""
        # Use a temp dir so no pre-existing config is loaded
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {'HOME': tmpdir, 'USERPROFILE': tmpdir}):
                from flask import Flask
                app2 = Flask(__name__)
                app2.config['TESTING'] = True
                from integrations.agent_engine.shell_os_apis import register_shell_os_routes
                register_shell_os_routes(app2)
                client = app2.test_client()
                r = client.post('/api/shell/cloud-sync/run',
                                data=json.dumps({}),
                                content_type='application/json')
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════
# App Permissions (Feature 7 - P0 OS Credibility)
# ═══════════════════════════════════════════════════════════════

class TestAppPermissions(unittest.TestCase):
    """Tests for app permission management endpoints."""

    def test_get_permissions_unknown_app(self):
        """Get permissions for unknown app returns empty list."""
        client = _make_os_app()
        with patch('integrations.agent_engine.shell_os_apis.open',
                   side_effect=FileNotFoundError):
            r = client.get('/api/apps/unknown_app_xyz/permissions')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['app_id'], 'unknown_app_xyz')
        self.assertIsInstance(data['permissions'], list)

    def test_set_permission(self):
        """Set permission for an app stores it correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            perm_file = os.path.join(tmpdir, 'app-permissions.json')
            # Patch the module-level _PERMISSIONS_FILE used inside closures
            from flask import Flask
            app = Flask(__name__)
            app.config['TESTING'] = True
            import integrations.agent_engine.shell_os_apis as mod
            # Store original and override
            orig = getattr(mod, '_PERMISSIONS_FILE', None)
            # The _PERMISSIONS_FILE is set inside register, we need to patch
            # before routes register. Use a fresh app.
            with patch.dict(os.environ, {'HOME': tmpdir, 'USERPROFILE': tmpdir}):
                app2 = Flask(__name__)
                app2.config['TESTING'] = True
                from integrations.agent_engine.shell_os_apis import register_shell_os_routes
                register_shell_os_routes(app2)
                client = app2.test_client()
                r = client.post(
                    '/api/apps/test_app/permission/camera',
                    data=json.dumps({'granted': False}),
                    content_type='application/json')
            self.assertEqual(r.status_code, 200)
            data = json.loads(r.data)
            self.assertTrue(data['updated'])
            self.assertEqual(data['type'], 'camera')
            self.assertFalse(data['granted'])

    def test_reset_permissions(self):
        """Reset removes all stored permissions for an app."""
        from flask import Flask
        with tempfile.TemporaryDirectory() as tmpdir:
            perm_file = os.path.join(tmpdir, '.config', 'hart', 'app-permissions.json')
            os.makedirs(os.path.dirname(perm_file), exist_ok=True)
            with open(perm_file, 'w') as f:
                json.dump({'test_app': {'camera': {'granted': False}}}, f)
            with patch.dict(os.environ, {'HOME': tmpdir, 'USERPROFILE': tmpdir}):
                app2 = Flask(__name__)
                app2.config['TESTING'] = True
                from integrations.agent_engine.shell_os_apis import register_shell_os_routes
                register_shell_os_routes(app2)
                client = app2.test_client()
                r = client.post('/api/apps/test_app/permissions/reset')
            self.assertEqual(r.status_code, 200)
            data = json.loads(r.data)
            self.assertTrue(data['reset'])
            self.assertEqual(data['app_id'], 'test_app')

    def test_permissions_response_shape(self):
        """Permission entries have type, granted, requested fields."""
        client = _make_os_app()
        # Create mock permissions file content
        mock_data = json.dumps({'some_app': {'camera': {'granted': False, 'updated': 1}}})
        m = MagicMock()
        m.__enter__ = MagicMock(return_value=MagicMock(
            read=MagicMock(return_value=mock_data)))
        m.__exit__ = MagicMock(return_value=False)
        with patch('builtins.open', return_value=m):
            with patch('json.load', return_value={'some_app': {'camera': {'granted': False}}}):
                r = client.get('/api/apps/some_app/permissions')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        if data['permissions']:
            perm = data['permissions'][0]
            self.assertIn('type', perm)
            self.assertIn('granted', perm)


# ═══════════════════════════════════════════════════════════════
# File Tagging (P2 Competitive Parity)
# ═══════════════════════════════════════════════════════════════

class TestFileTagging(unittest.TestCase):
    """Tests for /api/shell/files/tags and /api/shell/files/search-by-tag."""

    def test_get_tags_no_path(self):
        client = _make_os_app()
        r = client.get('/api/shell/files/tags')
        self.assertEqual(r.status_code, 400)

    def test_get_tags_nonexistent_path(self):
        client = _make_os_app()
        r = client.get('/api/shell/files/tags?path=/nonexistent/file')
        self.assertEqual(r.status_code, 400)

    def test_set_tags_no_path(self):
        client = _make_os_app()
        r = client.post('/api/shell/files/tags',
                        data=json.dumps({'tags': ['work']}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_set_tags_invalid_type(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmppath = f.name
        try:
            client = _make_os_app()
            r = client.post('/api/shell/files/tags',
                            data=json.dumps({'path': tmppath, 'tags': 'not_a_list'}),
                            content_type='application/json')
            self.assertEqual(r.status_code, 400)
        finally:
            os.unlink(tmppath)

    def test_search_by_tag_no_tag(self):
        client = _make_os_app()
        r = client.get('/api/shell/files/search-by-tag')
        self.assertEqual(r.status_code, 400)

    def test_search_by_tag_outside_home(self):
        client = _make_os_app()
        r = client.get('/api/shell/files/search-by-tag?tag=x&dir=/etc')
        self.assertEqual(r.status_code, 403)


# ═══════════════════════════════════════════════════════════════
# Hotspot (P2 Competitive Parity)
# ═══════════════════════════════════════════════════════════════

class TestHotspot(unittest.TestCase):
    """Tests for /api/shell/hotspot/*."""

    @patch('integrations.agent_engine.shell_os_apis.subprocess.run')
    def test_hotspot_status(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='')
        client = _make_os_app()
        r = client.get('/api/shell/hotspot/status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('active', data)

    @patch('integrations.agent_engine.shell_os_apis.subprocess.run')
    def test_hotspot_start(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='success')
        client = _make_os_app()
        r = client.post('/api/shell/hotspot/start',
                        data=json.dumps({'ssid': 'TestHotspot'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)

    @patch('integrations.agent_engine.shell_os_apis.subprocess.run')
    def test_hotspot_stop(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_os_app()
        r = client.post('/api/shell/hotspot/stop')
        self.assertEqual(r.status_code, 200)


# ═══════════════════════════════════════════════════════════════
# Weather (P2 Competitive Parity)
# ═══════════════════════════════════════════════════════════════

class TestWeather(unittest.TestCase):
    """Tests for /api/shell/weather."""

    @patch('urllib.request.urlopen')
    def test_weather_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps({
            'current_condition': [{
                'temp_C': '22', 'temp_F': '72', 'humidity': '50',
                'FeelsLikeC': '21', 'windspeedKmph': '10',
                'winddir16Point': 'N', 'uvIndex': '3', 'visibility': '10',
                'weatherDesc': [{'value': 'Sunny'}],
            }]
        }).encode('utf-8')
        mock_urlopen.return_value = mock_resp
        client = _make_os_app()
        r = client.get('/api/shell/weather?location=London')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['temp_c'], '22')
        self.assertEqual(data['description'], 'Sunny')

    @patch('urllib.request.urlopen', side_effect=Exception('network error'))
    def test_weather_unavailable(self, mock_urlopen):
        client = _make_os_app()
        r = client.get('/api/shell/weather')
        self.assertEqual(r.status_code, 503)


# ═══════════════════════════════════════════════════════════════
# Auto Update (P2 Competitive Parity)
# ═══════════════════════════════════════════════════════════════

class TestAutoUpdate(unittest.TestCase):
    """Tests for /api/shell/auto-update/*."""

    @patch('integrations.agent_engine.shell_os_apis.subprocess.run')
    def test_auto_update_status(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout='inactive')
        client = _make_os_app()
        r = client.get('/api/shell/auto-update/status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('flatpak_auto_update', data)
        self.assertIn('nixos_auto_upgrade', data)

    @patch('integrations.agent_engine.shell_os_apis.subprocess.run')
    def test_auto_update_run(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='Updated')
        client = _make_os_app()
        r = client.post('/api/shell/auto-update/run',
                        data=json.dumps({'target': 'flatpak'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)


# ═══════════════════════════════════════════════════════════════
# Secure DNS (P2 Competitive Parity)
# ═══════════════════════════════════════════════════════════════

class TestSecureDNS(unittest.TestCase):
    """Tests for /api/shell/dns/*."""

    @patch('integrations.agent_engine.shell_os_apis.subprocess.run')
    def test_dns_status(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='DNS Servers: 1.1.1.1\nDNS over TLS: yes\n')
        client = _make_os_app()
        r = client.get('/api/shell/dns/status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('servers', data)

    @patch('integrations.agent_engine.shell_os_apis.subprocess.run')
    def test_dns_set_cloudflare(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_os_app()
        r = client.post('/api/shell/dns/set',
                        data=json.dumps({'provider': 'cloudflare', 'dot': True}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['set'])
        self.assertEqual(data['provider'], 'cloudflare')

    def test_dns_set_unknown_provider(self):
        client = _make_os_app()
        r = client.post('/api/shell/dns/set',
                        data=json.dumps({'provider': 'unknown'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════
# SSO / Enterprise Login (P2 Competitive Parity)
# ═══════════════════════════════════════════════════════════════

class TestSSO(unittest.TestCase):
    """Tests for /api/shell/sso/*."""

    @patch('integrations.agent_engine.shell_os_apis.subprocess.run')
    def test_sso_status(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout='inactive')
        client = _make_os_app()
        r = client.get('/api/shell/sso/status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('sssd_active', data)

    def test_sso_join_missing_domain(self):
        client = _make_os_app()
        r = client.post('/api/shell/sso/join',
                        data=json.dumps({'username': 'admin'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_sso_leave_missing_domain(self):
        client = _make_os_app()
        r = client.post('/api/shell/sso/leave',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_sso_test_missing_uri(self):
        client = _make_os_app()
        r = client.post('/api/shell/sso/test',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════
# Email Launcher (P2 Competitive Parity)
# ═══════════════════════════════════════════════════════════════

class TestEmailLauncher(unittest.TestCase):
    """Tests for /api/shell/email/*."""

    @patch('integrations.agent_engine.shell_os_apis.subprocess.run')
    def test_email_status(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        client = _make_os_app()
        r = client.get('/api/shell/email/status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('installed', data)
        self.assertEqual(data['client'], 'thunderbird')

    @patch('integrations.agent_engine.shell_os_apis.subprocess.Popen',
           side_effect=FileNotFoundError)
    def test_email_launch_not_installed(self, mock_popen):
        client = _make_os_app()
        r = client.post('/api/shell/email/launch')
        self.assertEqual(r.status_code, 404)


if __name__ == '__main__':
    unittest.main()
