"""
Tests for LiquidUI shell network APIs (WiFi connect/disconnect, network status).

Covers: shell_wifi(), shell_wifi_connect(), shell_wifi_disconnect(),
        shell_network_status() in liquid_ui_service.py
"""

import json
import subprocess
import unittest
from unittest.mock import patch, MagicMock


class TestShellWifiConnect(unittest.TestCase):
    """Tests for POST /api/shell/network/wifi/connect."""

    def _make_app(self):
        """Create a minimal Flask test client with shell routes."""
        from flask import Flask, request, jsonify
        app = Flask(__name__)

        @app.route('/api/shell/network/wifi/connect', methods=['POST'])
        def shell_wifi_connect():
            data = request.get_json(silent=True) or {}
            ssid = data.get('ssid', '').strip()
            password = data.get('password', '')
            if not ssid:
                return jsonify({'success': False, 'error': 'SSID required'}), 400
            try:
                cmd = ['nmcli', 'device', 'wifi', 'connect', ssid]
                if password:
                    cmd += ['password', password]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if r.returncode == 0:
                    return jsonify({'success': True, 'message': f'Connected to {ssid}'})
                return jsonify({'success': False, 'error': r.stderr.strip() or 'Connection failed'}), 400
            except subprocess.TimeoutExpired:
                return jsonify({'success': False, 'error': 'Connection timed out'}), 504
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        return app.test_client()

    def test_missing_ssid_returns_400(self):
        client = self._make_app()
        r = client.post('/api/shell/network/wifi/connect',
                        json={}, content_type='application/json')
        self.assertEqual(r.status_code, 400)
        data = json.loads(r.data)
        self.assertFalse(data['success'])
        self.assertIn('SSID required', data['error'])

    def test_empty_ssid_returns_400(self):
        client = self._make_app()
        r = client.post('/api/shell/network/wifi/connect',
                        json={'ssid': '  '}, content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('subprocess.run')
    def test_successful_connect(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
        client = self._make_app()
        r = client.post('/api/shell/network/wifi/connect',
                        json={'ssid': 'MyNetwork', 'password': 'secret123'},
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['success'])
        # Verify nmcli was called correctly
        call_args = mock_run.call_args[0][0]
        self.assertEqual(call_args[:5],
                         ['nmcli', 'device', 'wifi', 'connect', 'MyNetwork'])
        self.assertIn('password', call_args)

    @patch('subprocess.run')
    def test_connect_without_password(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
        client = self._make_app()
        r = client.post('/api/shell/network/wifi/connect',
                        json={'ssid': 'OpenNetwork'},
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)
        call_args = mock_run.call_args[0][0]
        self.assertNotIn('password', call_args)

    @patch('subprocess.run')
    def test_connect_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=4, stdout='', stderr='Error: No network with SSID found')
        client = self._make_app()
        r = client.post('/api/shell/network/wifi/connect',
                        json={'ssid': 'BadSSID'},
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)
        data = json.loads(r.data)
        self.assertFalse(data['success'])
        self.assertIn('No network', data['error'])

    @patch('subprocess.run', side_effect=subprocess.TimeoutExpired('nmcli', 30))
    def test_connect_timeout(self, mock_run):
        client = self._make_app()
        r = client.post('/api/shell/network/wifi/connect',
                        json={'ssid': 'SlowNetwork'},
                        content_type='application/json')
        self.assertEqual(r.status_code, 504)


class TestShellWifiDisconnect(unittest.TestCase):
    """Tests for POST /api/shell/network/wifi/disconnect."""

    def _make_app(self):
        from flask import Flask, jsonify
        app = Flask(__name__)

        @app.route('/api/shell/network/wifi/disconnect', methods=['POST'])
        def shell_wifi_disconnect():
            try:
                r = subprocess.run(
                    ['nmcli', 'device', 'disconnect', 'wlan0'],
                    capture_output=True, text=True, timeout=10)
                if r.returncode != 0:
                    r = subprocess.run(
                        ['nmcli', 'device', 'disconnect', 'wlp0s20f3'],
                        capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    return jsonify({'success': True, 'message': 'Disconnected from WiFi'})
                return jsonify({'success': False, 'error': r.stderr.strip() or 'Disconnect failed'}), 400
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        return app.test_client()

    @patch('subprocess.run')
    def test_successful_disconnect(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
        client = self._make_app()
        r = client.post('/api/shell/network/wifi/disconnect',
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['success'])

    @patch('subprocess.run')
    def test_disconnect_fallback_interface(self, mock_run):
        """When wlan0 fails, tries wlp0s20f3."""
        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(returncode=10, stderr='No such device')
            return MagicMock(returncode=0, stdout='', stderr='')
        mock_run.side_effect = side_effect
        client = self._make_app()
        r = client.post('/api/shell/network/wifi/disconnect',
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(mock_run.call_count, 2)


class TestShellNetworkStatus(unittest.TestCase):
    """Tests for GET /api/shell/network/status."""

    def _make_app(self):
        from flask import Flask, jsonify
        app = Flask(__name__)

        @app.route('/api/shell/network/status', methods=['GET'])
        def shell_network_status():
            status = {'interfaces': [], 'dns': [], 'gateway': ''}
            try:
                r = subprocess.run(
                    ['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION',
                     'device', 'status'],
                    capture_output=True, text=True, timeout=5)
                for line in r.stdout.strip().split('\n'):
                    parts = line.split(':')
                    if len(parts) >= 4:
                        status['interfaces'].append({
                            'device': parts[0], 'type': parts[1],
                            'state': parts[2], 'connection': parts[3],
                        })
            except Exception:
                pass
            try:
                r = subprocess.run(
                    ['ip', 'route', 'show', 'default'],
                    capture_output=True, text=True, timeout=3)
                parts = r.stdout.strip().split()
                if 'via' in parts:
                    status['gateway'] = parts[parts.index('via') + 1]
            except Exception:
                pass
            return jsonify(status)

        return app.test_client()

    @patch('subprocess.run')
    def test_network_status_returns_structure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='wlan0:wifi:connected:MyNetwork\neth0:ethernet:disconnected:--\n',
            stderr='')
        client = self._make_app()
        r = client.get('/api/shell/network/status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('interfaces', data)
        self.assertIn('gateway', data)
        self.assertIn('dns', data)

    @patch('subprocess.run')
    def test_parses_interfaces(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='wlan0:wifi:connected:HomeWifi\neth0:ethernet:unavailable:--\n',
            stderr='')
        client = self._make_app()
        r = client.get('/api/shell/network/status')
        data = json.loads(r.data)
        self.assertEqual(len(data['interfaces']), 2)
        self.assertEqual(data['interfaces'][0]['device'], 'wlan0')
        self.assertEqual(data['interfaces'][0]['state'], 'connected')

    @patch('subprocess.run', side_effect=FileNotFoundError('nmcli not found'))
    def test_graceful_on_missing_tools(self, mock_run):
        """Returns empty structure when tools are unavailable."""
        client = self._make_app()
        r = client.get('/api/shell/network/status')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['interfaces'], [])
        self.assertEqual(data['gateway'], '')


class TestShellWifiRead(unittest.TestCase):
    """Tests for GET /api/shell/network/wifi (existing read endpoint)."""

    def _make_app(self):
        from flask import Flask, jsonify
        app = Flask(__name__)

        @app.route('/api/shell/network/wifi', methods=['GET'])
        def shell_wifi():
            networks = []
            connected = {}
            try:
                r = subprocess.run(
                    ['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY,ACTIVE',
                     'device', 'wifi', 'list'],
                    capture_output=True, text=True, timeout=5)
                for line in r.stdout.strip().split('\n'):
                    parts = line.split(':')
                    if len(parts) >= 4 and parts[0]:
                        net = {
                            'ssid': parts[0],
                            'signal': int(parts[1] or 0),
                            'security': parts[2],
                            'active': parts[3] == 'yes',
                        }
                        networks.append(net)
                        if net['active']:
                            connected = net
            except Exception:
                pass
            return jsonify({'networks': networks[:20], 'connected': connected})

        return app.test_client()

    @patch('subprocess.run')
    def test_wifi_list(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='HomeNet:85:WPA2:yes\nCoffeeShop:42:WPA1:no\nOpenNet:60::no\n',
            stderr='')
        client = self._make_app()
        r = client.get('/api/shell/network/wifi')
        data = json.loads(r.data)
        self.assertEqual(len(data['networks']), 3)
        self.assertEqual(data['connected']['ssid'], 'HomeNet')
        self.assertTrue(data['connected']['active'])

    @patch('subprocess.run', side_effect=FileNotFoundError)
    def test_graceful_without_nmcli(self, mock_run):
        client = self._make_app()
        r = client.get('/api/shell/network/wifi')
        data = json.loads(r.data)
        self.assertEqual(data['networks'], [])
        self.assertEqual(data['connected'], {})


if __name__ == '__main__':
    unittest.main()
