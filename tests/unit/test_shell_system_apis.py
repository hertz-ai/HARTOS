"""
Tests for integrations.agent_engine.shell_system_apis — System management APIs.

Covers: task/process manager, storage manager, startup apps,
bluetooth management, print manager, media indexer.
"""

import json
import os
import tempfile
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
        client = _make_system_app()
        r = client.post('/api/shell/webcam/capture',
                        data=json.dumps({'device': '/dev/video0'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 500)
        data = json.loads(r.data)
        self.assertIn('error', data)


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
        mock_run.return_value = MagicMock(returncode=1, stderr='scanimage: no SANE devices found')
        client = _make_system_app()
        r = client.post('/api/shell/scanner/scan',
                        data=json.dumps({'format': 'png'}),
                        content_type='application/json')
        self.assertEqual(r.status_code, 500)


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


if __name__ == '__main__':
    unittest.main()
