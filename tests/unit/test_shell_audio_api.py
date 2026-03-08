"""
Tests for LiquidUI shell audio APIs (volume, mute, default sink, source volume).

Covers: shell_audio(), shell_audio_volume(), shell_audio_mute(),
        shell_audio_default(), shell_audio_source_volume()
"""

import json
import subprocess
import unittest
from unittest.mock import patch, MagicMock


def _make_audio_app():
    """Create Flask test client with audio shell routes."""
    from flask import Flask, request, jsonify
    app = Flask(__name__)

    def _parse_volume(vol_info):
        if isinstance(vol_info, dict):
            for ch in vol_info.values():
                if isinstance(ch, dict) and 'value_percent' in ch:
                    return int(ch['value_percent'].rstrip('%'))
        return 100

    @app.route('/api/shell/audio', methods=['GET'])
    def shell_audio():
        sinks = []
        sources = []
        default_sink = ''
        try:
            r = subprocess.run(['pactl', 'get-default-sink'],
                               capture_output=True, text=True, timeout=3)
            default_sink = r.stdout.strip()
        except Exception:
            pass
        try:
            r = subprocess.run(['pactl', '--format=json', 'list', 'sinks'],
                               capture_output=True, text=True, timeout=5)
            if r.stdout.strip():
                raw = json.loads(r.stdout)
                sinks = [{
                    'id': s.get('name', ''),
                    'name': s.get('description', ''),
                    'mute': s.get('mute', False),
                    'volume': _parse_volume(s.get('volume', {})),
                    'default': s.get('name', '') == default_sink,
                } for s in raw]
        except Exception:
            pass
        try:
            r = subprocess.run(['pactl', '--format=json', 'list', 'sources'],
                               capture_output=True, text=True, timeout=5)
            if r.stdout.strip():
                raw = json.loads(r.stdout)
                sources = [{
                    'id': s.get('name', ''),
                    'name': s.get('description', ''),
                    'volume': _parse_volume(s.get('volume', {})),
                } for s in raw]
        except Exception:
            pass
        return jsonify({'sinks': sinks, 'sources': sources})

    @app.route('/api/shell/audio/volume', methods=['POST'])
    def shell_audio_volume():
        data = request.get_json(silent=True) or {}
        sink_id = data.get('sink_id', '')
        volume = data.get('volume')
        if not sink_id or volume is None:
            return jsonify({'success': False, 'error': 'sink_id and volume required'}), 400
        volume = max(0, min(150, int(volume)))
        try:
            r = subprocess.run(['pactl', 'set-sink-volume', sink_id, f'{volume}%'],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return jsonify({'success': True, 'volume': volume})
            return jsonify({'success': False, 'error': r.stderr.strip()}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/shell/audio/mute', methods=['POST'])
    def shell_audio_mute():
        data = request.get_json(silent=True) or {}
        sink_id = data.get('sink_id', '')
        muted = data.get('muted', True)
        if not sink_id:
            return jsonify({'success': False, 'error': 'sink_id required'}), 400
        try:
            val = '1' if muted else '0'
            r = subprocess.run(['pactl', 'set-sink-mute', sink_id, val],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return jsonify({'success': True, 'muted': muted})
            return jsonify({'success': False, 'error': r.stderr.strip()}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/shell/audio/default', methods=['POST'])
    def shell_audio_default():
        data = request.get_json(silent=True) or {}
        sink_id = data.get('sink_id', '')
        if not sink_id:
            return jsonify({'success': False, 'error': 'sink_id required'}), 400
        try:
            r = subprocess.run(['pactl', 'set-default-sink', sink_id],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return jsonify({'success': True, 'default_sink': sink_id})
            return jsonify({'success': False, 'error': r.stderr.strip()}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/shell/audio/source/volume', methods=['POST'])
    def shell_audio_source_volume():
        data = request.get_json(silent=True) or {}
        source_id = data.get('source_id', '')
        volume = data.get('volume')
        if not source_id or volume is None:
            return jsonify({'success': False, 'error': 'source_id and volume required'}), 400
        volume = max(0, min(150, int(volume)))
        try:
            r = subprocess.run(['pactl', 'set-source-volume', source_id, f'{volume}%'],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return jsonify({'success': True, 'volume': volume})
            return jsonify({'success': False, 'error': r.stderr.strip()}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    return app.test_client()


class TestShellAudioGet(unittest.TestCase):
    """Tests for GET /api/shell/audio."""

    @patch('subprocess.run')
    def test_returns_sinks_and_sources(self, mock_run):
        def side_effect(cmd, **kw):
            if 'get-default-sink' in cmd:
                return MagicMock(returncode=0, stdout='alsa_output.pci.analog-stereo\n')
            if 'sinks' in cmd:
                return MagicMock(returncode=0, stdout=json.dumps([
                    {'name': 'alsa_output.pci.analog-stereo',
                     'description': 'Built-in Audio',
                     'mute': False, 'volume': {}}
                ]))
            if 'sources' in cmd:
                return MagicMock(returncode=0, stdout=json.dumps([
                    {'name': 'alsa_input.pci.analog-stereo',
                     'description': 'Built-in Mic', 'volume': {}}
                ]))
            return MagicMock(returncode=0, stdout='')
        mock_run.side_effect = side_effect
        client = _make_audio_app()
        r = client.get('/api/shell/audio')
        data = json.loads(r.data)
        self.assertEqual(len(data['sinks']), 1)
        self.assertEqual(data['sinks'][0]['name'], 'Built-in Audio')
        self.assertTrue(data['sinks'][0]['default'])
        self.assertEqual(len(data['sources']), 1)

    @patch('subprocess.run', side_effect=FileNotFoundError)
    def test_graceful_without_pactl(self, mock_run):
        client = _make_audio_app()
        r = client.get('/api/shell/audio')
        data = json.loads(r.data)
        self.assertEqual(data['sinks'], [])
        self.assertEqual(data['sources'], [])


class TestShellAudioVolume(unittest.TestCase):
    """Tests for POST /api/shell/audio/volume."""

    def test_missing_params(self):
        client = _make_audio_app()
        r = client.post('/api/shell/audio/volume', json={},
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('subprocess.run')
    def test_set_volume(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_audio_app()
        r = client.post('/api/shell/audio/volume',
                        json={'sink_id': 'alsa_output.pci', 'volume': 75},
                        content_type='application/json')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data['success'])
        self.assertEqual(data['volume'], 75)
        mock_run.assert_called_once_with(
            ['pactl', 'set-sink-volume', 'alsa_output.pci', '75%'],
            capture_output=True, text=True, timeout=5)

    @patch('subprocess.run')
    def test_volume_clamped_to_150(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_audio_app()
        r = client.post('/api/shell/audio/volume',
                        json={'sink_id': 'x', 'volume': 999},
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertEqual(data['volume'], 150)

    @patch('subprocess.run')
    def test_volume_clamped_to_0(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_audio_app()
        r = client.post('/api/shell/audio/volume',
                        json={'sink_id': 'x', 'volume': -50},
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertEqual(data['volume'], 0)


class TestShellAudioMute(unittest.TestCase):
    """Tests for POST /api/shell/audio/mute."""

    def test_missing_sink_id(self):
        client = _make_audio_app()
        r = client.post('/api/shell/audio/mute', json={'muted': True},
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('subprocess.run')
    def test_mute(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_audio_app()
        r = client.post('/api/shell/audio/mute',
                        json={'sink_id': 'alsa_output.pci', 'muted': True},
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['success'])
        self.assertTrue(data['muted'])
        mock_run.assert_called_once_with(
            ['pactl', 'set-sink-mute', 'alsa_output.pci', '1'],
            capture_output=True, text=True, timeout=5)

    @patch('subprocess.run')
    def test_unmute(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_audio_app()
        r = client.post('/api/shell/audio/mute',
                        json={'sink_id': 'alsa_output.pci', 'muted': False},
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertFalse(data['muted'])
        call_args = mock_run.call_args[0][0]
        self.assertEqual(call_args[-1], '0')


class TestShellAudioDefault(unittest.TestCase):
    """Tests for POST /api/shell/audio/default."""

    def test_missing_sink_id(self):
        client = _make_audio_app()
        r = client.post('/api/shell/audio/default', json={},
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('subprocess.run')
    def test_set_default(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_audio_app()
        r = client.post('/api/shell/audio/default',
                        json={'sink_id': 'alsa_output.hdmi'},
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['success'])
        self.assertEqual(data['default_sink'], 'alsa_output.hdmi')


class TestShellAudioSourceVolume(unittest.TestCase):
    """Tests for POST /api/shell/audio/source/volume."""

    def test_missing_params(self):
        client = _make_audio_app()
        r = client.post('/api/shell/audio/source/volume', json={},
                        content_type='application/json')
        self.assertEqual(r.status_code, 400)

    @patch('subprocess.run')
    def test_set_source_volume(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        client = _make_audio_app()
        r = client.post('/api/shell/audio/source/volume',
                        json={'source_id': 'alsa_input.pci', 'volume': 80},
                        content_type='application/json')
        data = json.loads(r.data)
        self.assertTrue(data['success'])
        self.assertEqual(data['volume'], 80)


if __name__ == '__main__':
    unittest.main()
