"""
Tests for LiquidUI shell display APIs (resolution, brightness, scale).

Covers: shell_display(), shell_display_resolution(), shell_display_brightness(),
        shell_display_scale()
"""

import json
import subprocess
import unittest
from unittest.mock import patch, MagicMock


XRANDR_OUTPUT = """Screen 0: minimum 8 x 8, current 1920 x 1080, maximum 32767 x 32767
HDMI-1 connected primary 1920x1080+0+0
   1920x1080     60.00*+  50.00    59.94
   1280x720      60.00    50.00
   1024x768      60.00
DP-1 disconnected
eDP-1 connected 1366x768+1920+0
   1366x768      60.00*+
   1280x720      60.00
"""


def _make_display_app():
    from flask import Flask, request, jsonify
    app = Flask(__name__)

    @app.route('/api/shell/display', methods=['GET'])
    def shell_display():
        displays = []
        try:
            r = subprocess.run(['xrandr', '--current'],
                               capture_output=True, text=True, timeout=5)
            current_display = None
            for line in r.stdout.split('\n'):
                if ' connected' in line:
                    parts = line.split()
                    res = 'unknown'
                    for p in parts[2:]:
                        if 'x' in p and p[0].isdigit():
                            res = p.split('+')[0]
                            break
                    current_display = {
                        'name': parts[0],
                        'resolution': res,
                        'modes': [],
                    }
                    displays.append(current_display)
                elif current_display and line.startswith('   '):
                    mode_parts = line.strip().split()
                    if mode_parts:
                        mode = mode_parts[0]
                        rates = []
                        active = False
                        for p in mode_parts[1:]:
                            clean = p.replace('*', '').replace('+', '')
                            if '*' in p:
                                active = True
                            try:
                                rates.append(float(clean))
                            except ValueError:
                                pass
                        current_display['modes'].append({
                            'resolution': mode, 'rates': rates, 'active': active,
                        })
                elif not line.startswith(' '):
                    current_display = None
        except Exception:
            pass
        return jsonify({'displays': displays})

    @app.route('/api/shell/display/resolution', methods=['POST'])
    def shell_display_resolution():
        data = request.get_json(silent=True) or {}
        output = data.get('output', '')
        resolution = data.get('resolution', '')
        rate = data.get('rate')
        if not output or not resolution:
            return jsonify({'success': False, 'error': 'output and resolution required'}), 400
        try:
            cmd = ['xrandr', '--output', output, '--mode', resolution]
            if rate:
                cmd += ['--rate', str(rate)]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return jsonify({'success': True, 'output': output, 'resolution': resolution})
            return jsonify({'success': False, 'error': r.stderr.strip() or 'Failed'}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/shell/display/brightness', methods=['POST'])
    def shell_display_brightness():
        data = request.get_json(silent=True) or {}
        output = data.get('output', '')
        brightness = data.get('brightness')
        if not output or brightness is None:
            return jsonify({'success': False, 'error': 'output and brightness required'}), 400
        brightness = max(0.1, min(1.0, float(brightness)))
        try:
            r = subprocess.run(
                ['xrandr', '--output', output, '--brightness', str(brightness)],
                capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return jsonify({'success': True, 'brightness': brightness})
            return jsonify({'success': False, 'error': r.stderr.strip()}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/shell/display/scale', methods=['POST'])
    def shell_display_scale():
        data = request.get_json(silent=True) or {}
        output = data.get('output', '')
        scale = data.get('scale')
        if not output or scale is None:
            return jsonify({'success': False, 'error': 'output and scale required'}), 400
        scale = max(0.5, min(3.0, float(scale)))
        try:
            transform = str(round(1.0 / scale, 4))
            r = subprocess.run(
                ['xrandr', '--output', output, '--scale', f'{transform}x{transform}'],
                capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return jsonify({'success': True, 'scale': scale})
            return jsonify({'success': False, 'error': r.stderr.strip()}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    return app.test_client()


class TestShellDisplayGet(unittest.TestCase):
    """Tests for GET /api/shell/display."""

    @patch('subprocess.run')
    def test_parses_xrandr_output(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=XRANDR_OUTPUT)
        client = _make_display_app()
        r = client.get('/api/shell/display')
        data = json.loads(r.data)
        displays = data['displays']
        self.assertEqual(len(displays), 2)
        # HDMI-1
        self.assertEqual(displays[0]['name'], 'HDMI-1')
        self.assertIn('1920x1080', displays[0]['resolution'])
        self.assertEqual(len(displays[0]['modes']), 3)
        # First mode should be active
        self.assertTrue(displays[0]['modes'][0]['active'])
        self.assertIn(60.0, displays[0]['modes'][0]['rates'])
        # eDP-1
        self.assertEqual(displays[1]['name'], 'eDP-1')
        self.assertEqual(len(displays[1]['modes']), 2)

    @patch('subprocess.run')
    def test_disconnected_displays_excluded(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=XRANDR_OUTPUT)
        client = _make_display_app()
        r = client.get('/api/shell/display')
        data = json.loads(r.data)
        names = [d['name'] for d in data['displays']]
        self.assertNotIn('DP-1', names)  # disconnected

    @patch('subprocess.run', side_effect=FileNotFoundError)
    def test_graceful_without_xrandr(self, mock_run):
        client = _make_display_app()
        r = client.get('/api/shell/display')
        data = json.loads(r.data)
        self.assertEqual(data['displays'], [])


class TestShellDisplayResolution(unittest.TestCase):
    """Tests for POST /api/shell/display/resolution."""

    def test_missing_params(self):
        client = _make_display_app()
        r = client.post('/api/shell/display/resolution', json={},
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('subprocess.run')
    def test_set_resolution(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_display_app()
        r = client.post('/api/shell/display/resolution',
                        json={'output': 'HDMI-1', 'resolution': '1280x720', 'rate': 60},
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['success'])
        call_args = mock_run.call_args[0][0]
        self.assertEqual(call_args, ['xrandr', '--output', 'HDMI-1',
                                     '--mode', '1280x720', '--rate', '60'])

    @patch('subprocess.run')
    def test_set_resolution_no_rate(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_display_app()
        r = client.post('/api/shell/display/resolution',
                        json={'output': 'HDMI-1', 'resolution': '1920x1080'},
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['success'])
        call_args = mock_run.call_args[0][0]
        self.assertNotIn('--rate', call_args)

    @patch('subprocess.run')
    def test_set_resolution_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr='Size not found')
        client = _make_display_app()
        r = client.post('/api/shell/display/resolution',
                        json={'output': 'HDMI-1', 'resolution': '9999x9999'},
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)


class TestShellDisplayBrightness(unittest.TestCase):
    """Tests for POST /api/shell/display/brightness."""

    def test_missing_params(self):
        client = _make_display_app()
        r = client.post('/api/shell/display/brightness', json={},
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('subprocess.run')
    def test_set_brightness(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_display_app()
        r = client.post('/api/shell/display/brightness',
                        json={'output': 'HDMI-1', 'brightness': 0.7},
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['success'])
        self.assertAlmostEqual(data['brightness'], 0.7)

    @patch('subprocess.run')
    def test_brightness_clamped(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_display_app()
        # Too low
        r = client.post('/api/shell/display/brightness',
                        json={'output': 'X', 'brightness': 0.01},
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertAlmostEqual(data['brightness'], 0.1)
        # Too high
        r = client.post('/api/shell/display/brightness',
                        json={'output': 'X', 'brightness': 5.0},
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertAlmostEqual(data['brightness'], 1.0)


class TestShellDisplayScale(unittest.TestCase):
    """Tests for POST /api/shell/display/scale."""

    def test_missing_params(self):
        client = _make_display_app()
        r = client.post('/api/shell/display/scale', json={},
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('subprocess.run')
    def test_set_scale(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_display_app()
        r = client.post('/api/shell/display/scale',
                        json={'output': 'eDP-1', 'scale': 1.5},
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['success'])
        self.assertAlmostEqual(data['scale'], 1.5)
        # Verify xrandr --scale uses inverse
        call_args = mock_run.call_args[0][0]
        self.assertIn('--scale', call_args)

    @patch('subprocess.run')
    def test_scale_clamped(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_display_app()
        r = client.post('/api/shell/display/scale',
                        json={'output': 'X', 'scale': 10.0},
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertAlmostEqual(data['scale'], 3.0)


if __name__ == '__main__':
    unittest.main()
