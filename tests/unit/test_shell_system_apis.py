"""
Tests for integrations.agent_engine.shell_system_apis — System management APIs.

Covers: task/process manager, storage manager, startup apps,
bluetooth management, print manager, media indexer.
"""

import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock


def _make_system_app():
    """Create a Flask test app with all system routes."""
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    # Reset in-memory state between tests
    import integrations.agent_engine.shell_system_apis as mod
    with mod._bt_lock:
        mod._bt_discovered.clear()
    with mod._media_lock:
        mod._media_index.update({
            'photos': [], 'music': [], 'videos': [],
            'last_scan': 0, 'scan_dirs': [],
        })
    from integrations.agent_engine.shell_system_apis import register_shell_system_routes
    register_shell_system_routes(app)
    return app.test_client()


# ═══════════════════════════════════════════════════════════════
# Task / Process Manager
# ═══════════════════════════════════════════════════════════════

class TestTaskManager(unittest.TestCase):

    @patch('integrations.agent_engine.shell_system_apis.psutil', create=True)
    def test_list_processes(self, mock_psutil):
        proc = MagicMock()
        proc.info = {
            'pid': 42, 'name': 'python', 'username': 'hart',
            'cpu_percent': 12.5, 'memory_percent': 3.2,
            'memory_info': MagicMock(rss=100 * 1048576),
            'status': 'running', 'nice': 0, 'num_threads': 4,
            'create_time': 1700000000, 'cmdline': ['python', 'app.py'],
        }
        # Patch psutil import inside the route
        with patch.dict('sys.modules', {'psutil': mock_psutil}):
            mock_psutil.process_iter.return_value = [proc]
            mock_psutil.NoSuchProcess = Exception
            mock_psutil.AccessDenied = Exception
            client = _make_system_app()
            r = client.get('/api/shell/tasks/processes')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['processes'][0]['pid'], 42)
        self.assertAlmostEqual(data['processes'][0]['cpu_percent'], 12.5, places=1)

    @patch.dict('sys.modules', {'psutil': None})
    def test_processes_no_psutil(self):
        client = _make_system_app()
        r = client.get('/api/shell/tasks/processes')
        data = json.loads(r.data)
        self.assertIn('error', data)

    def test_kill_missing_pid(self):
        client = _make_system_app()
        r = client.post('/api/shell/tasks/kill',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_kill_pid_1(self):
        client = _make_system_app()
        r = client.post('/api/shell/tasks/kill',
                        data=json.dumps({'pid': 1}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 403)

    @patch('os.kill')
    def test_kill_success(self, mock_kill):
        mock_kill.return_value = None
        # Patch psutil to bypass protected name check
        mock_psutil = MagicMock()
        mock_psutil.Process.return_value.name.return_value = 'myapp'
        with patch.dict('sys.modules', {'psutil': mock_psutil}):
            client = _make_system_app()
            r = client.post('/api/shell/tasks/kill',
                            data=json.dumps({'pid': 999, 'signal': 'SIGTERM'}),
                            content_type='application/json')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['killed'])

    @patch('os.kill', side_effect=ProcessLookupError)
    def test_kill_not_found(self, mock_kill):
        client = _make_system_app()
        r = client.post('/api/shell/tasks/kill',
                        data=json.dumps({'pid': 99999}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 404)

    def test_priority_missing_pid(self):
        client = _make_system_app()
        r = client.post('/api/shell/tasks/priority',
                        data=json.dumps({'nice': 5}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_resources(self):
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.return_value = 25.0
        mock_psutil.cpu_count.return_value = 8
        mock_psutil.cpu_freq.return_value = MagicMock(current=3600)
        mem = MagicMock(total=16 * 1073741824, used=8 * 1073741824, percent=50.0)
        mock_psutil.virtual_memory.return_value = mem
        swap = MagicMock(total=4 * 1073741824, used=1 * 1073741824)
        mock_psutil.swap_memory.return_value = swap
        dio = MagicMock(read_bytes=1000000, write_bytes=2000000)
        mock_psutil.disk_io_counters.return_value = dio
        nio = MagicMock(bytes_sent=500000, bytes_recv=1500000)
        mock_psutil.net_io_counters.return_value = nio
        with patch.dict('sys.modules', {'psutil': mock_psutil}):
            client = _make_system_app()
            r = client.get('/api/shell/tasks/resources')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['cpu']['count'], 8)
        self.assertEqual(data['ram']['total_gb'], 16.0)


# ═══════════════════════════════════════════════════════════════
# Storage Manager
# ═══════════════════════════════════════════════════════════════

class TestStorageManager(unittest.TestCase):

    def test_storage_partitions(self):
        mock_psutil = MagicMock()
        part = MagicMock(device='/dev/sda1', mountpoint='/', fstype='ext4')
        mock_psutil.disk_partitions.return_value = [part]
        usage = MagicMock(total=500 * 1073741824, used=200 * 1073741824,
                          free=300 * 1073741824, percent=40.0)
        mock_psutil.disk_usage.return_value = usage
        with patch.dict('sys.modules', {'psutil': mock_psutil}):
            client = _make_system_app()
            r = client.get('/api/shell/storage')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(len(data['partitions']), 1)
        self.assertEqual(data['partitions'][0]['device'], '/dev/sda1')
        self.assertEqual(data['total_gb'], 500.0)

    def test_storage_usage_invalid_path(self):
        client = _make_system_app()
        r = client.get('/api/shell/storage/usage?path=/nonexistent/path/xyz')
        self.assertEqual(r.status_code, 400)

    def test_storage_usage_valid(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'test.txt'), 'w') as f:
                f.write('x' * 1024)
            client = _make_system_app()
            r = client.get(f'/api/shell/storage/usage?path={d}')
            self.assertEqual(r.status_code, 200)
            data = json.loads(r.data)
            self.assertEqual(data['path'], d)
            self.assertGreater(len(data['children']), 0)

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_storage_cleanup(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='50\t/home/user/.cache')
        client = _make_system_app()
        r = client.get('/api/shell/storage/cleanup')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('reclaimable', data)
        self.assertIn('total_reclaimable_mb', data)

    def test_clean_no_categories(self):
        client = _make_system_app()
        r = client.post('/api/shell/storage/clean',
                        data=json.dumps({'categories': []}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_clean_cache(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='100\t/home/.cache')
        client = _make_system_app()
        r = client.post('/api/shell/storage/clean',
                        data=json.dumps({'categories': ['cache']}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['cleaned'])

    def test_smart_no_device(self):
        client = _make_system_app()
        r = client.get('/api/shell/storage/smart')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_smart_success(self, mock_run):
        smart_data = {
            'smart_status': {'passed': True},
            'temperature': {'current': 35},
            'power_on_time': {'hours': 1234},
            'model_name': 'Samsung 970',
            'serial_number': 'XYZ123',
            'firmware_version': '1.0',
        }
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(smart_data))
        client = _make_system_app()
        r = client.get('/api/shell/storage/smart?device=/dev/nvme0n1')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['healthy'])
        self.assertEqual(data['temperature_c'], 35)


# ═══════════════════════════════════════════════════════════════
# Startup Apps
# ═══════════════════════════════════════════════════════════════

class TestStartupApps(unittest.TestCase):

    def test_list_startup(self):
        client = _make_system_app()
        r = client.get('/api/shell/startup')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('entries', data)
        self.assertIn('count', data)

    def test_add_startup(self):
        with tempfile.TemporaryDirectory() as d:
            with patch('os.path.expanduser', return_value=os.path.join(d, '.config/autostart')):
                client = _make_system_app()
                r = client.post('/api/shell/startup/add',
                                data=json.dumps({'name': 'MyApp', 'exec': '/usr/bin/myapp'}),
                                content_type='application/json')
                self.assertEqual(r.status_code, 200)
                data = json.loads(r.data)
                self.assertTrue(data['added'])
                self.assertIn('myapp', data['file'])

    def test_add_startup_missing_fields(self):
        client = _make_system_app()
        r = client.post('/api/shell/startup/add',
                        data=json.dumps({'name': 'NoExec'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_toggle_missing_file(self):
        client = _make_system_app()
        r = client.post('/api/shell/startup/toggle',
                        data=json.dumps({'file': ''}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_toggle_nonexistent_file(self):
        client = _make_system_app()
        r = client.post('/api/shell/startup/toggle',
                        data=json.dumps({'file': '/nonexistent/app.desktop', 'enabled': True}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 404)

    def test_remove_system_blocked(self):
        client = _make_system_app()
        r = client.post('/api/shell/startup/remove',
                        data=json.dumps({'file': '/etc/xdg/autostart/system.desktop'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 403)

    def test_remove_nonexistent(self):
        client = _make_system_app()
        r = client.post('/api/shell/startup/remove',
                        data=json.dumps({'file': '/home/user/.config/autostart/nope.desktop'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 404)


# ═══════════════════════════════════════════════════════════════
# Bluetooth Management
# ═══════════════════════════════════════════════════════════════

class TestBluetooth(unittest.TestCase):

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_status(self, mock_run):
        def run_side_effect(cmd, **kw):
            if 'show' in cmd:
                return MagicMock(returncode=0, stdout=(
                    'Controller AA:BB:CC:DD:EE:FF MyPC\n'
                    '\tPowered: yes\n\tDiscoverable: no\n\tPairable: yes\n'
                    '\tName: MyPC\n'))
            if 'devices' in cmd:
                return MagicMock(returncode=0, stdout='Device 11:22:33:44:55:66 AirPods\n')
            if 'info' in cmd:
                return MagicMock(returncode=0, stdout=(
                    '\tConnected: yes\n\tTrusted: yes\n\tIcon: audio-headphones\n'))
            return MagicMock(returncode=0, stdout='')
        mock_run.side_effect = run_side_effect
        client = _make_system_app()
        r = client.get('/api/shell/bluetooth/status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['powered'])
        self.assertEqual(len(data['devices']), 1)
        self.assertEqual(data['devices'][0]['name'], 'AirPods')
        self.assertTrue(data['devices'][0]['connected'])

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_scan(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='')
        client = _make_system_app()
        r = client.post('/api/shell/bluetooth/scan',
                        data=json.dumps({'duration': 5}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['scanning'])
        self.assertEqual(data['duration'], 5)

    def test_discovered_empty(self):
        client = _make_system_app()
        r = client.get('/api/shell/bluetooth/discovered')
        data = json.loads(r.data)
        self.assertEqual(data['count'], 0)

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_pair(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_system_app()
        r = client.post('/api/shell/bluetooth/pair',
                        data=json.dumps({'mac': '11:22:33:44:55:66'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['paired'])

    def test_pair_missing_mac(self):
        client = _make_system_app()
        r = client.post('/api/shell/bluetooth/pair',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_connect(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_system_app()
        r = client.post('/api/shell/bluetooth/connect',
                        data=json.dumps({'mac': '11:22:33:44:55:66'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['connected'])

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_disconnect(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_system_app()
        r = client.post('/api/shell/bluetooth/disconnect',
                        data=json.dumps({'mac': '11:22:33:44:55:66'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['disconnected'])

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_trust(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_system_app()
        r = client.post('/api/shell/bluetooth/trust',
                        data=json.dumps({'mac': 'AA:BB:CC:DD:EE:FF', 'trusted': True}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['trusted'])

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_remove_device(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_system_app()
        r = client.post('/api/shell/bluetooth/remove',
                        data=json.dumps({'mac': 'AA:BB:CC:DD:EE:FF'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['removed'])

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_power_off(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_system_app()
        r = client.post('/api/shell/bluetooth/power',
                        data=json.dumps({'powered': False}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertFalse(data['powered'])


# ═══════════════════════════════════════════════════════════════
# Print Manager
# ═══════════════════════════════════════════════════════════════

class TestPrintManager(unittest.TestCase):

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_list_printers(self, mock_run):
        def run_side_effect(cmd, **kw):
            if '-p' in cmd and '-d' in cmd:
                return MagicMock(returncode=0, stdout=(
                    'printer HP-LaserJet is idle.\n'
                    'system default destination: HP-LaserJet\n'))
            if '-v' in cmd:
                return MagicMock(returncode=0, stdout=(
                    'device for HP-LaserJet: ipp://192.168.1.10/ipp/print\n'))
            return MagicMock(returncode=0, stdout='')
        mock_run.side_effect = run_side_effect
        client = _make_system_app()
        r = client.get('/api/shell/printers')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['cups_running'])
        self.assertEqual(len(data['printers']), 1)
        self.assertTrue(data['printers'][0]['default'])
        self.assertEqual(data['default'], 'HP-LaserJet')

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_printers_cups_not_running(self, mock_run):
        mock_run.return_value = None
        client = _make_system_app()
        r = client.get('/api/shell/printers')
        data = json.loads(r.data)
        self.assertFalse(data['cups_running'])

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_printer_jobs(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='HP-12 hart 1024 pending\n')
        client = _make_system_app()
        r = client.get('/api/shell/printers/jobs')
        data = json.loads(r.data)
        self.assertEqual(data['count'], 1)

    def test_add_printer_missing(self):
        client = _make_system_app()
        r = client.post('/api/shell/printers/add',
                        data=json.dumps({'uri': 'ipp://x'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_add_printer(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr='')
        client = _make_system_app()
        r = client.post('/api/shell/printers/add',
                        data=json.dumps({'uri': 'ipp://192.168.1.10', 'name': 'Office'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['added'])

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_remove_printer(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_system_app()
        r = client.post('/api/shell/printers/remove',
                        data=json.dumps({'name': 'Office'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['removed'])

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_set_default(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_system_app()
        r = client.post('/api/shell/printers/set-default',
                        data=json.dumps({'name': 'Office'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['set'])

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_cancel_job(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_system_app()
        r = client.post('/api/shell/printers/cancel',
                        data=json.dumps({'job_id': 'HP-12'}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['cancelled'])

    def test_cancel_missing_id(self):
        client = _make_system_app()
        r = client.post('/api/shell/printers/cancel',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════
# Media Indexer
# ═══════════════════════════════════════════════════════════════

class TestMediaIndexer(unittest.TestCase):

    def test_status_not_scanned(self):
        client = _make_system_app()
        r = client.get('/api/shell/media/status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertFalse(data['indexed'])
        self.assertEqual(data['counts']['photos'], 0)

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_scan_starts(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='[]')
        client = _make_system_app()
        r = client.post('/api/shell/media/scan',
                        data=json.dumps({'directories': ['/tmp']}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['scanning'])
        self.assertEqual(data['directories'], ['/tmp'])

    def test_scan_default_dirs(self):
        client = _make_system_app()
        r = client.post('/api/shell/media/scan',
                        data=json.dumps({}),
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['scanning'])
        self.assertEqual(len(data['directories']), 3)  # Pictures, Videos, Music

    def test_photos_empty(self):
        client = _make_system_app()
        r = client.get('/api/shell/media/photos')
        data = json.loads(r.data)
        self.assertEqual(data['total'], 0)

    def test_photos_pagination(self):
        import integrations.agent_engine.shell_system_apis as mod
        client = _make_system_app()
        # Set state AFTER app creation (which resets state)
        with mod._media_lock:
            mod._media_index['photos'] = [
                {'path': f'/p/{i}.jpg', 'name': f'{i}.jpg', 'size': 1000, 'modified': i}
                for i in range(120)
            ]
        # Page 1 (default 50 per page)
        r = client.get('/api/shell/media/photos?page=1')
        data = json.loads(r.data)
        self.assertEqual(len(data['photos']), 50)
        self.assertEqual(data['total'], 120)
        # Page 3 (items 100-119)
        r2 = client.get('/api/shell/media/photos?page=3')
        data2 = json.loads(r2.data)
        self.assertEqual(len(data2['photos']), 20)

    def test_music_filter_artist(self):
        import integrations.agent_engine.shell_system_apis as mod
        client = _make_system_app()
        # Set state AFTER app creation (which resets state)
        with mod._media_lock:
            mod._media_index['music'] = [
                {'path': '/m/1.mp3', 'name': '1.mp3', 'size': 5000, 'modified': 1,
                 'artist': 'Bach', 'album': 'Cello Suites', 'title': 'Suite 1'},
                {'path': '/m/2.mp3', 'name': '2.mp3', 'size': 5000, 'modified': 2,
                 'artist': 'Mozart', 'album': 'Requiem', 'title': 'Lacrimosa'},
            ]
        r = client.get('/api/shell/media/music?artist=bach')
        data = json.loads(r.data)
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['tracks'][0]['artist'], 'Bach')

    def test_videos_empty(self):
        client = _make_system_app()
        r = client.get('/api/shell/media/videos')
        data = json.loads(r.data)
        self.assertEqual(data['total'], 0)


# ═══════════════════════════════════════════════════════════════
# Webcam / Camera
# ═══════════════════════════════════════════════════════════════

class TestShellWebcam(unittest.TestCase):

    @patch('glob.glob', return_value=[])
    def test_webcam_list_no_devices(self, _glob):
        client = _make_system_app()
        r = client.get('/api/shell/webcam/list')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(len(data['devices']), 0)

    @patch('integrations.agent_engine.shell_system_apis._run')
    @patch('glob.glob', return_value=['/dev/video0'])
    def test_webcam_list_with_device(self, _glob, mock_run):
        v4l2_output = (
            'Driver name   : uvcvideo\n'
            'Card type     : HD Webcam\n'
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=v4l2_output)
        client = _make_system_app()
        r = client.get('/api/shell/webcam/list')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(len(data['devices']), 1)
        self.assertEqual(data['devices'][0]['device'], '/dev/video0')
        self.assertEqual(data['devices'][0]['name'], 'HD Webcam')

    @patch('integrations.agent_engine.shell_system_apis._run', return_value=None)
    def test_webcam_capture_ffmpeg_not_found(self, mock_run):
        """A missing capture tool is 503 UNAVAILABLE, not 500 SERVER ERROR.

        Was asserting 500 and had been red since 2b2be57f, which changed the
        handler on purpose ("a missing/failed capture tool is an UNAVAILABLE
        peripheral") without updating this test. The distinction is not
        cosmetic: 500 tells the shell the backend is broken and trips retry
        /error UI, while 503 tells it this node simply has no ffmpeg — a
        permanent, honest degrade the panel should render as such.
        """
        client = _make_system_app()
        r = client.post('/api/shell/webcam/capture',
                        data=json.dumps({'device': '/dev/video0'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 503)
        data = json.loads(r.data)
        self.assertIn('error', data)
        self.assertFalse(data.get('ok', False))
        # Names the absent tool, so the panel can say WHAT is missing.
        self.assertIn('ffmpeg', data['error'])


# ═══════════════════════════════════════════════════════════════
# Scanner
# ═══════════════════════════════════════════════════════════════

class TestShellScanner(unittest.TestCase):

    @patch('integrations.agent_engine.shell_system_apis._run', return_value=None)
    def test_scanner_list_empty(self, mock_run):
        client = _make_system_app()
        r = client.get('/api/shell/scanner/list')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(len(data['scanners']), 0)

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_scanner_list_with_scanner(self, mock_run):
        scanimage_output = "device `hpaio:/net/HP_LaserJet?ip=192.168.1.10' is a Hewlett-Packard HP_LaserJet all-in-one"
        mock_run.return_value = MagicMock(returncode=0, stdout=scanimage_output)
        client = _make_system_app()
        r = client.get('/api/shell/scanner/list')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertGreater(len(data['scanners']), 0)
        self.assertIn('raw', data['scanners'][0])

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_scanner_scan_error(self, mock_run):
        """A scanner that reports no devices is 503 UNAVAILABLE (see the
        webcam sibling above for why this is not 500).

        Also asserts the tool's own stderr reaches the caller — "no SANE
        devices found" is the actionable half; a bare status code leaves
        the user with nothing to act on.
        """
        mock_run.return_value = MagicMock(returncode=1, stderr='scanimage: no SANE devices found')
        client = _make_system_app()
        r = client.post('/api/shell/scanner/scan',
                        data=json.dumps({'format': 'png'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 503)
        data = json.loads(r.data)
        self.assertIn('no SANE devices found', data['error'])


# ═══════════════════════════════════════════════════════════════
# Protected Names
# ═══════════════════════════════════════════════════════════════

class TestProtectedNames(unittest.TestCase):

    def test_docker_in_protected_names(self):
        """Verify 'dockerd' is in the _PROTECTED_NAMES set."""
        import inspect
        from integrations.agent_engine.shell_system_apis import register_shell_system_routes
        source = inspect.getsource(register_shell_system_routes)
        self.assertIn("'dockerd'", source)

    def test_k8s_in_protected_names(self):
        """Verify Kubernetes-related names are in _PROTECTED_NAMES."""
        import inspect
        from integrations.agent_engine.shell_system_apis import register_shell_system_routes
        source = inspect.getsource(register_shell_system_routes)
        for name in ('kubelet', 'etcd', 'containerd'):
            self.assertIn(f"'{name}'", source, f"{name} not found in _PROTECTED_NAMES")


# ═══════════════════════════════════════════════════════════════
# Bluetooth Timeout
# ═══════════════════════════════════════════════════════════════

class TestBluetoothTimeout(unittest.TestCase):

    def test_bluetooth_scan_has_timeout(self):
        """Verify the bluetooth scan subprocess call includes a timeout parameter."""
        import inspect
        from integrations.agent_engine.shell_system_apis import register_shell_system_routes
        source = inspect.getsource(register_shell_system_routes)
        # Find the _do_scan section and verify timeout is passed to _run
        scan_idx = source.find('def _do_scan')
        self.assertGreater(scan_idx, -1, '_do_scan function not found')
        scan_section = source[scan_idx:scan_idx + 300]
        self.assertIn('timeout=', scan_section)

    def test_bluetooth_background_thread_exists(self):
        """Verify a background scan thread function exists in the bluetooth scan route."""
        import inspect
        from integrations.agent_engine.shell_system_apis import register_shell_system_routes
        source = inspect.getsource(register_shell_system_routes)
        self.assertIn('def _do_scan', source)
        self.assertIn('Thread(target=_do_scan', source)


# ═══════════════════════════════════════════════════════════════
# Media Player (P1 Daily Driver)
# ═══════════════════════════════════════════════════════════════

class TestMediaPlayer(unittest.TestCase):
    """Tests for media player control endpoints."""

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_player_status_nothing_playing(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        client = _make_system_app()
        r = client.get('/api/shell/media/player-status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertFalse(data['playing'])

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_play_no_path(self, mock_run):
        client = _make_system_app()
        r = client.post('/api/shell/media/play',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_play_file_not_found(self, mock_run):
        client = _make_system_app()
        r = client.post('/api/shell/media/play',
                        data=json.dumps({'path': '/nonexistent/file.mp3'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 404)

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_stop_nothing_playing(self, mock_run):
        client = _make_system_app()
        r = client.post('/api/shell/media/stop',
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertFalse(data['stopped'])


# ═══════════════════════════════════════════════════════════════
# Battery / Power Monitoring (Feature 1)
# ═══════════════════════════════════════════════════════════════

class TestBatteryMonitor(unittest.TestCase):

    def test_battery_status_endpoint(self):
        client = _make_system_app()
        r = client.get('/api/shell/battery')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('present', data)
        self.assertIn('status', data)
        self.assertIn('health', data)

    def test_battery_info_structure(self):
        """_battery_info returns all expected fields."""
        import inspect
        from integrations.agent_engine.shell_system_apis import register_shell_system_routes
        source = inspect.getsource(register_shell_system_routes)
        self.assertIn('def _battery_info', source)
        self.assertIn('capacity', source)
        self.assertIn('voltage_v', source)
        self.assertIn('power_w', source)
        self.assertIn('temperature_c', source)

    def test_battery_profile_endpoint(self):
        client = _make_system_app()
        r = client.get('/api/shell/battery/profile')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('current_profile', data)
        self.assertIn('available', data)

    def test_battery_set_profile_no_body(self):
        client = _make_system_app()
        r = client.post('/api/shell/battery/profile',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════
# WiFi Management (Feature 2)
# ═══════════════════════════════════════════════════════════════

class TestWiFiManagement(unittest.TestCase):

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_wifi_status(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='enabled\n')
        client = _make_system_app()
        r = client.get('/api/shell/wifi/status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('enabled', data)
        self.assertIn('connected', data)

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_wifi_networks(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='HomeNet:85:WPA2:5 GHz\nCafe:42:WPA:2.4 GHz\n')
        client = _make_system_app()
        r = client.get('/api/shell/wifi/networks')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('networks', data)
        self.assertIn('count', data)

    def test_wifi_connect_no_ssid(self):
        client = _make_system_app()
        r = client.post('/api/shell/wifi/connect',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_wifi_connect_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='')
        client = _make_system_app()
        r = client.post('/api/shell/wifi/connect',
                        data=json.dumps({'ssid': 'TestNet', 'password': 'pass123'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['connected'])

    def test_wifi_forget_no_ssid(self):
        client = _make_system_app()
        r = client.post('/api/shell/wifi/forget',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_wifi_saved(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='HomeNet:802-11-wireless:yes\nWorkNet:802-11-wireless:no\n')
        client = _make_system_app()
        r = client.get('/api/shell/wifi/saved')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('connections', data)

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_wifi_toggle(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='')
        client = _make_system_app()
        r = client.post('/api/shell/wifi/toggle',
                        data=json.dumps({'enable': False}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)


# ═══════════════════════════════════════════════════════════════
# WiFi non-blocking contract (the click-to-freeze fix)
# ═══════════════════════════════════════════════════════════════

class TestWiFiNonBlocking(unittest.TestCase):
    """The wifi routes must never pin the small (1-2 thread) shell pool — that is
    what froze the UI when the user clicked Wi-Fi on a software-rendered box. The
    scan path must NOT sleep on the request thread, and the connect must be bounded
    so a slow nmcli cannot block; both still preserve their result behaviour.
    """

    @patch('integrations.agent_engine.shell_system_apis.time.sleep')
    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_wifi_networks_rescan_never_sleeps(self, mock_run, mock_sleep):
        # A rescan request used to time.sleep(2) on the request thread. It must not
        # any longer — yet it still returns the cached networks (behaviour kept).
        mock_run.return_value = MagicMock(
            returncode=0, stdout='HomeNet:85:WPA2:5 GHz\nCafe:42:WPA:2.4 GHz\n')
        client = _make_system_app()
        r = client.get('/api/shell/wifi/networks?rescan=true')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['count'], 2)                       # still returns networks
        self.assertEqual(data['networks'][0]['ssid'], 'HomeNet')  # sorted by signal
        mock_sleep.assert_not_called()                            # never blocks the thread

    def test_run_async_bounded_returns_fast_result(self):
        # When the command settles within the wait, the caller gets (True, result).
        import integrations.agent_engine.shell_system_apis as mod
        with patch.object(mod, '_run', return_value=MagicMock(returncode=0)):
            finished, r = mod._run_async_bounded(['nmcli', 'x'], run_timeout=5, wait=2)
        self.assertTrue(finished)
        self.assertIsNotNone(r)
        self.assertEqual(r.returncode, 0)

    def test_run_async_bounded_bounds_a_slow_command(self):
        # A command slower than the caller's wait returns (False, None) PROMPTLY
        # (request thread freed) while the worker still runs to completion so the
        # side-effect lands out-of-band.
        import integrations.agent_engine.shell_system_apis as mod
        ran_to_completion = threading.Event()

        def slow_run(cmd, timeout=10, **kw):
            time.sleep(0.4)            # exceeds the 0.1s caller bound below
            ran_to_completion.set()
            return MagicMock(returncode=0)

        t0 = time.monotonic()
        with patch.object(mod, '_run', side_effect=slow_run):
            finished, r = mod._run_async_bounded(['nmcli', 'x'], run_timeout=5, wait=0.1)
            elapsed = time.monotonic() - t0
            self.assertFalse(finished)                # bounded out
            self.assertIsNone(r)
            self.assertLess(elapsed, 0.35)            # did NOT wait the full 0.4s
            self.assertTrue(ran_to_completion.wait(2))  # worker finished out-of-band

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_wifi_connect_fast_join_returns_real_result(self, mock_run):
        # A fast connect returns the REAL joined result (200), not a masked status.
        mock_run.return_value = MagicMock(returncode=0, stdout='')
        client = _make_system_app()
        r = client.post('/api/shell/wifi/connect',
                        data=json.dumps({'ssid': 'TestNet', 'password': 'pass123'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['connected'])
        self.assertEqual(data['ssid'], 'TestNet')

    def test_wifi_connect_slow_returns_connecting_not_masked_success(self):
        # When the connect outlives the bounded wait, the route returns a structured
        # 'connecting' (202) — never a faked success — and the request thread is free.
        import integrations.agent_engine.shell_system_apis as mod
        with patch.object(mod, '_run_async_bounded', return_value=(False, None)):
            client = _make_system_app()
            r = client.post('/api/shell/wifi/connect',
                            data=json.dumps({'ssid': 'SlowNet', 'password': 'x'}),
                            content_type='application/json')
        self.assertEqual(r.status_code, 202)
        data = json.loads(r.data)
        self.assertTrue(data['connecting'])
        self.assertFalse(data['connected'])
        self.assertEqual(data['ssid'], 'SlowNet')


# ═══════════════════════════════════════════════════════════════
# VPN Client (Feature 3)
# ═══════════════════════════════════════════════════════════════

class TestVPNClient(unittest.TestCase):

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_vpn_list(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='Work VPN:vpn:no\nHome WG:wireguard:yes\n')
        client = _make_system_app()
        r = client.get('/api/shell/vpn/list')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('connections', data)

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_vpn_status(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='')
        client = _make_system_app()
        r = client.get('/api/shell/vpn/status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('connected', data)

    def test_vpn_connect_no_name(self):
        client = _make_system_app()
        r = client.post('/api/shell/vpn/connect',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_vpn_disconnect_no_name(self):
        client = _make_system_app()
        r = client.post('/api/shell/vpn/disconnect',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_vpn_import_no_config(self):
        client = _make_system_app()
        r = client.post('/api/shell/vpn/import',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_vpn_import_file_not_found(self):
        client = _make_system_app()
        r = client.post('/api/shell/vpn/import',
                        data=json.dumps({'config_path': '/nonexistent/vpn.ovpn'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 404)


# ═══════════════════════════════════════════════════════════════
# Trash / Recycle Bin (Feature 5)
# ═══════════════════════════════════════════════════════════════

class TestTrashBin(unittest.TestCase):

    def test_trash_list_empty(self):
        client = _make_system_app()
        with patch('os.path.isdir', return_value=False):
            r = client.get('/api/shell/trash')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['total_items'], 0)
        self.assertIn('items', data)

    def test_trash_move_no_path(self):
        client = _make_system_app()
        r = client.post('/api/shell/trash/move',
                        data=json.dumps({}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_trash_move_not_found(self):
        client = _make_system_app()
        r = client.post('/api/shell/trash/move',
                        data=json.dumps({'path': '/nonexistent/file.txt'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 404)

    def test_trash_restore_empty(self):
        client = _make_system_app()
        with patch('os.path.isdir', return_value=False):
            r = client.post('/api/shell/trash/restore',
                            data=json.dumps({'id': 'nonexistent'}),
                            content_type='application/json')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['restored_count'], 0)

    def test_trash_xdg_dir(self):
        """_trash_dir follows XDG spec."""
        import inspect
        from integrations.agent_engine.shell_system_apis import register_shell_system_routes
        source = inspect.getsource(register_shell_system_routes)
        self.assertIn('.local/share/Trash', source)
        self.assertIn('.trashinfo', source)

    def test_trash_freedesktop_format(self):
        """Trash info files follow freedesktop.org Trash specification."""
        import inspect
        from integrations.agent_engine.shell_system_apis import register_shell_system_routes
        source = inspect.getsource(register_shell_system_routes)
        self.assertIn('[Trash Info]', source)
        self.assertIn('Path=', source)
        self.assertIn('DeletionDate=', source)


# ═══════════════════════════════════════════════════════════════
# Screen Rotation (P2 Competitive Parity)
# ═══════════════════════════════════════════════════════════════

class TestScreenRotation(unittest.TestCase):
    """Tests for /api/shell/display/rotation and /api/shell/display/auto-rotate."""

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_get_rotation_swaymsg(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([{'name': 'eDP-1', 'transform': 'normal', 'active': True}]))
        client = _make_system_app()
        r = client.get('/api/shell/display/rotation')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('outputs', data)
        self.assertEqual(len(data['outputs']), 1)

    def test_set_rotation_no_output(self):
        client = _make_system_app()
        r = client.post('/api/shell/display/rotation',
                        data=json.dumps({'transform': '90'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_set_rotation_invalid_transform(self):
        client = _make_system_app()
        r = client.post('/api/shell/display/rotation',
                        data=json.dumps({'output': 'eDP-1', 'transform': 'upside_down'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('integrations.agent_engine.shell_system_apis._run')
    def test_auto_rotate_status(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_system_app()
        r = client.get('/api/shell/display/auto-rotate')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('available', data)


if __name__ == '__main__':
    unittest.main()


class TestShellAntivirus(unittest.TestCase):
    """ClamAV agent surface — closing a declarative-only parity gap.

    hart-security.nix had run clamd + freshclam for a long time with NO
    agent-visible surface: the OS scanned, and nothing could ask it what it
    found. These routes are the read/scan half (enable/disable stays
    declarative on purpose — turning the scanner OFF from an unauthenticated
    local API is a security decision, not a convenience).
    """

    def test_status_reports_running_daemon(self):
        with patch('integrations.agent_engine.shell_system_apis._run') as run:
            run.return_value = MagicMock(returncode=0, stdout='active\n', stderr='')
            r = _make_system_app().get('/api/shell/antivirus/status')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(json.loads(r.data)['running'])

    def test_status_reports_stopped_daemon(self):
        with patch('integrations.agent_engine.shell_system_apis._run') as run:
            run.return_value = MagicMock(returncode=3, stdout='inactive\n', stderr='')
            r = _make_system_app().get('/api/shell/antivirus/status')
        self.assertFalse(json.loads(r.data)['running'])

    def test_status_survives_missing_systemctl(self):
        """Dev boxes and containers have no systemctl — must degrade, not 500."""
        with patch('integrations.agent_engine.shell_system_apis._run',
                   return_value=None):
            r = _make_system_app().get('/api/shell/antivirus/status')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(json.loads(r.data)['running'])

    def test_stale_signatures_are_flagged(self):
        """A live daemon with an ancient DB looks healthy and catches nothing.

        This is THE field worth having: `signatures_stale` is derived once
        here rather than left for every caller to recompute from a raw age.
        """
        import time as _t
        with tempfile.TemporaryDirectory() as d:
            old = os.path.join(d, 'daily.cvd')
            with open(old, 'w') as fh:
                fh.write('x')
            os.utime(old, (_t.time() - 30 * 86400, _t.time() - 30 * 86400))
            with patch('integrations.agent_engine.shell_system_apis.os.listdir',
                       return_value=['daily.cvd']), \
                 patch('integrations.agent_engine.shell_system_apis.os.path.getmtime',
                       return_value=_t.time() - 30 * 86400), \
                 patch('integrations.agent_engine.shell_system_apis._run',
                       return_value=MagicMock(returncode=0, stdout='active\n')):
                r = _make_system_app().get('/api/shell/antivirus/status')
        data = json.loads(r.data)
        self.assertTrue(data['signatures_present'])
        self.assertTrue(data['signatures_stale'])
        self.assertGreater(data['signature_age_days'], 7)

    def test_scan_requires_a_path(self):
        r = _make_system_app().post('/api/shell/antivirus/scan',
                                    data=json.dumps({}),
                                    content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_scan_rejects_a_nonexistent_path(self):
        r = _make_system_app().post(
            '/api/shell/antivirus/scan',
            data=json.dumps({'path': '/definitely/not/here/9f3a'}),
            content_type='application/json')
        self.assertEqual(r.status_code, 404)

    def test_scan_reports_clean(self):
        with tempfile.TemporaryDirectory() as d:
            with patch('integrations.agent_engine.shell_system_apis._run_async_bounded',
                       return_value=(True, MagicMock(returncode=0, stdout='', stderr=''))):
                r = _make_system_app().post(
                    '/api/shell/antivirus/scan',
                    data=json.dumps({'path': d}),
                    content_type='application/json')
        data = json.loads(r.data)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(data['clean'])
        self.assertEqual(data['infected_count'], 0)

    def test_scan_reports_infected_without_erroring(self):
        """clamdscan exits 1 for 'infected'. That is a SUCCESSFUL scan with a
        finding — collapsing it into an error would hide the detection."""
        out = '/tmp/x/evil.bin: Eicar-Test-Signature FOUND\n'
        with tempfile.TemporaryDirectory() as d:
            with patch('integrations.agent_engine.shell_system_apis._run_async_bounded',
                       return_value=(True, MagicMock(returncode=1, stdout=out, stderr=''))):
                r = _make_system_app().post(
                    '/api/shell/antivirus/scan',
                    data=json.dumps({'path': d}),
                    content_type='application/json')
        data = json.loads(r.data)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(data['ok'])
        self.assertFalse(data['clean'])
        self.assertEqual(data['infected_count'], 1)
        self.assertIn('Eicar-Test-Signature', data['infected'][0])

    def test_long_scan_returns_202_instead_of_hanging(self):
        """A big tree scans for minutes. The request must NOT hold a pool
        thread — that is the click-to-freeze class this module documents."""
        with tempfile.TemporaryDirectory() as d:
            with patch('integrations.agent_engine.shell_system_apis._run_async_bounded',
                       return_value=(False, None)):
                r = _make_system_app().post(
                    '/api/shell/antivirus/scan',
                    data=json.dumps({'path': d}),
                    content_type='application/json')
        self.assertEqual(r.status_code, 202)
        self.assertFalse(json.loads(r.data)['finished'])

    def test_scan_503_when_clamdscan_absent(self):
        with tempfile.TemporaryDirectory() as d:
            with patch('integrations.agent_engine.shell_system_apis._run_async_bounded',
                       return_value=(True, None)):
                r = _make_system_app().post(
                    '/api/shell/antivirus/scan',
                    data=json.dumps({'path': d}),
                    content_type='application/json')
        self.assertEqual(r.status_code, 503)


class TestShellEncryption(unittest.TestCase):
    """LUKS status — the last declarative-only parity row.

    hart-luks.nix configures encryption at INSTALL time and nothing could
    report it afterwards. Read-only by design: encryption cannot be turned
    on at runtime, so an "enable" route could only lie or kick off a
    destructive re-encrypt.
    """

    LSBLK_ENCRYPTED = (
        'NAME="nvme0n1" TYPE="disk" FSTYPE="" MOUNTPOINT=""\n'
        'NAME="nvme0n1p2" TYPE="part" FSTYPE="crypto_LUKS" MOUNTPOINT=""\n'
        'NAME="cryptroot" TYPE="crypt" FSTYPE="ext4" MOUNTPOINT="/"\n'
    )
    LSBLK_PLAIN = (
        'NAME="sda" TYPE="disk" FSTYPE="" MOUNTPOINT=""\n'
        'NAME="sda1" TYPE="part" FSTYPE="ext4" MOUNTPOINT="/"\n'
    )

    def _get(self, stdout):
        with patch('integrations.agent_engine.shell_system_apis._run',
                   return_value=MagicMock(returncode=0, stdout=stdout, stderr='')):
            return _make_system_app().get('/api/shell/encryption/status')

    def test_detects_encrypted_root(self):
        data = json.loads(self._get(self.LSBLK_ENCRYPTED).data)
        self.assertTrue(data['root_encrypted'])
        self.assertEqual(data['encrypted_device_count'], 2)

    def test_plain_disk_is_not_reported_encrypted(self):
        data = json.loads(self._get(self.LSBLK_PLAIN).data)
        self.assertFalse(data['root_encrypted'])
        self.assertEqual(data['encrypted_device_count'], 0)

    def test_encrypted_data_volume_does_not_imply_encrypted_root(self):
        """The half-configured state worth catching.

        A LUKS volume mounted at /data with a PLAINTEXT root reads as
        "encrypted" to a user and protects far less than they think, so
        root_encrypted must stay False while the device still appears.
        """
        out = ('NAME="sda1" TYPE="part" FSTYPE="ext4" MOUNTPOINT="/"\n'
               'NAME="sdb1" TYPE="part" FSTYPE="crypto_LUKS" MOUNTPOINT=""\n'
               'NAME="cryptdata" TYPE="crypt" FSTYPE="ext4" MOUNTPOINT="/data"\n')
        data = json.loads(self._get(out).data)
        self.assertFalse(data['root_encrypted'])
        self.assertEqual(data['encrypted_device_count'], 2)

    def test_reports_runtime_toggle_unsupported(self):
        """Say it explicitly so no caller hunts for a toggle that cannot exist."""
        data = json.loads(self._get(self.LSBLK_ENCRYPTED).data)
        self.assertFalse(data['runtime_toggle_supported'])

    def test_missing_lsblk_degrades_to_503(self):
        with patch('integrations.agent_engine.shell_system_apis._run',
                   return_value=None):
            r = _make_system_app().get('/api/shell/encryption/status')
        self.assertEqual(r.status_code, 503)
        self.assertFalse(json.loads(r.data)['available'])

    def test_malformed_lsblk_line_does_not_crash(self):
        data = json.loads(self._get('garbage\n\nNAME="x" TYPE="crypt" FSTYPE="ext4" MOUNTPOINT="/"\n').data)
        self.assertTrue(data['root_encrypted'])


class TestKillGuardFailsClosed(unittest.TestCase):
    """The protected-process guard must FAIL CLOSED, not open.

    Found by the #31 degraded-mode review, ranked by blast radius: an agent
    drives /api/shell/tasks/kill unattended, so a guard that silently stops
    guarding is the worst shape a check can take.

    The original code was:

        try:
            import psutil
            proc = psutil.Process(pid)
            if proc.name() in _PROTECTED_NAMES:
                return ..., 403
        except Exception:
            pass                    # <-- falls through to os.kill()

    So whenever psutil was ABSENT, or Process(pid) raised (AccessDenied on a
    root-owned process is the common one), the protection check was skipped
    ENTIRELY and the kill proceeded. The system looked protected and was not.

    An unverifiable guard is not a passed guard.
    """

    def test_kill_is_refused_when_the_guard_cannot_run(self):
        """psutil unavailable => REFUSE, never 'kill anyway'."""
        killed = []
        with patch('integrations.agent_engine.shell_system_apis.os.kill',
                   side_effect=lambda *a: killed.append(a)), \
             patch.dict('sys.modules', {'psutil': None}):
            # psutil=None makes `import psutil` raise ImportError inside the route.
            r = _make_system_app().post(
                '/api/shell/tasks/kill',
                data=json.dumps({'pid': 424242}),
                content_type='application/json')
        self.assertNotEqual(r.status_code, 200, (
            "kill succeeded while the protected-process guard could not run — "
            "the guard failed OPEN"))
        self.assertEqual(killed, [], (
            "os.kill was CALLED despite the guard being unverifiable; a "
            "protected process could have been killed"))

    def test_kill_is_refused_when_process_lookup_raises(self):
        """psutil present but Process() raises => still REFUSE.

        AccessDenied on a root-owned process is the ordinary case, and it is
        precisely when the target is most likely to be protected.
        """
        import psutil as _ps
        killed = []
        with patch('integrations.agent_engine.shell_system_apis.os.kill',
                   side_effect=lambda *a: killed.append(a)), \
             patch.object(_ps, 'Process', side_effect=_ps.AccessDenied(424242)):
            r = _make_system_app().post(
                '/api/shell/tasks/kill',
                data=json.dumps({'pid': 424242}),
                content_type='application/json')
        self.assertNotEqual(r.status_code, 200,
                            "kill succeeded despite an unverifiable guard")
        self.assertEqual(killed, [], "os.kill was called with the guard bypassed")

    def test_a_normal_kill_still_works(self):
        """Failing closed must not break the ordinary path — a guard that
        blocks everything gets removed, which is worse than one that leaks."""
        import psutil as _ps
        fake = MagicMock()
        fake.name.return_value = 'some-user-process'
        killed = []
        with patch('integrations.agent_engine.shell_system_apis.os.kill',
                   side_effect=lambda *a: killed.append(a)), \
             patch.object(_ps, 'Process', return_value=fake):
            r = _make_system_app().post(
                '/api/shell/tasks/kill',
                data=json.dumps({'pid': 424242}),
                content_type='application/json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(killed, "the ordinary kill path stopped working")
