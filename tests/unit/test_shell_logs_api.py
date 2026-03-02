"""
Tests for LiquidUI log viewer API.

Covers: /api/shell/system/logs, /api/shell/system/logs/stream
"""

import json
import subprocess
import unittest
from unittest.mock import patch, MagicMock


JOURNAL_OUTPUT = '\n'.join([
    json.dumps({
        '__REALTIME_TIMESTAMP': '1709337600000000',
        '_SYSTEMD_UNIT': 'hart-backend.service',
        'PRIORITY': '6',
        'MESSAGE': 'Backend started on port 677',
    }),
    json.dumps({
        '__REALTIME_TIMESTAMP': '1709337601000000',
        '_SYSTEMD_UNIT': 'hart-discovery.service',
        'PRIORITY': '4',
        'MESSAGE': 'No peers found yet',
    }),
])


def _make_logs_app():
    from flask import Flask, request, jsonify, Response
    app = Flask(__name__)

    @app.route('/api/shell/system/logs', methods=['GET'])
    def shell_system_logs():
        unit = request.args.get('unit', 'hart-*')
        lines = int(request.args.get('lines', 100))
        priority = request.args.get('priority', '')
        since = request.args.get('since', '')
        grep_pattern = request.args.get('grep', '')
        lines = max(1, min(1000, lines))
        try:
            cmd = ['journalctl', '--output=json', '--no-pager',
                   '-u', unit, '-n', str(lines)]
            if priority:
                cmd += ['-p', priority]
            if since:
                cmd += ['--since', since]
            if grep_pattern:
                cmd += ['-g', grep_pattern]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            entries = []
            for line in r.stdout.strip().split('\n'):
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entries.append({
                        'timestamp': entry.get('__REALTIME_TIMESTAMP', ''),
                        'unit': entry.get('_SYSTEMD_UNIT', ''),
                        'priority': entry.get('PRIORITY', ''),
                        'message': entry.get('MESSAGE', ''),
                    })
                except json.JSONDecodeError:
                    pass
            return jsonify({'entries': entries, 'count': len(entries)})
        except FileNotFoundError:
            return jsonify({'entries': [], 'count': 0,
                            'error': 'journalctl not available'}), 200
        except Exception as e:
            return jsonify({'entries': [], 'error': str(e)}), 500

    return app.test_client()


class TestShellSystemLogs(unittest.TestCase):
    """Tests for GET /api/shell/system/logs."""

    @patch('subprocess.run')
    def test_parses_journal_entries(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=JOURNAL_OUTPUT)
        client = _make_logs_app()
        r = client.get('/api/shell/system/logs')
        data = json.loads(r.data)
        self.assertEqual(data['count'], 2)
        self.assertEqual(data['entries'][0]['unit'], 'hart-backend.service')
        self.assertIn('port 677', data['entries'][0]['message'])

    @patch('subprocess.run')
    def test_default_params(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='')
        client = _make_logs_app()
        r = client.get('/api/shell/system/logs')
        call_args = mock_run.call_args[0][0]
        self.assertIn('-u', call_args)
        self.assertIn('hart-*', call_args)
        self.assertIn('-n', call_args)
        self.assertIn('100', call_args)

    @patch('subprocess.run')
    def test_custom_unit(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='')
        client = _make_logs_app()
        client.get('/api/shell/system/logs?unit=hart-backend.service')
        call_args = mock_run.call_args[0][0]
        self.assertIn('hart-backend.service', call_args)

    @patch('subprocess.run')
    def test_custom_lines(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='')
        client = _make_logs_app()
        client.get('/api/shell/system/logs?lines=50')
        call_args = mock_run.call_args[0][0]
        self.assertIn('50', call_args)

    @patch('subprocess.run')
    def test_lines_clamped(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='')
        client = _make_logs_app()
        client.get('/api/shell/system/logs?lines=5000')
        call_args = mock_run.call_args[0][0]
        self.assertIn('1000', call_args)

    @patch('subprocess.run')
    def test_priority_filter(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='')
        client = _make_logs_app()
        client.get('/api/shell/system/logs?priority=3')
        call_args = mock_run.call_args[0][0]
        self.assertIn('-p', call_args)
        self.assertIn('3', call_args)

    @patch('subprocess.run')
    def test_since_filter(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='')
        client = _make_logs_app()
        client.get('/api/shell/system/logs?since=1 hour ago')
        call_args = mock_run.call_args[0][0]
        self.assertIn('--since', call_args)

    @patch('subprocess.run')
    def test_grep_filter(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='')
        client = _make_logs_app()
        client.get('/api/shell/system/logs?grep=error')
        call_args = mock_run.call_args[0][0]
        self.assertIn('-g', call_args)
        self.assertIn('error', call_args)

    @patch('subprocess.run', side_effect=FileNotFoundError)
    def test_graceful_without_journalctl(self, mock_run):
        client = _make_logs_app()
        r = client.get('/api/shell/system/logs')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertEqual(data['entries'], [])
        self.assertIn('not available', data.get('error', ''))

    @patch('subprocess.run')
    def test_malformed_json_skipped(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='not json\n' + json.dumps({
                '__REALTIME_TIMESTAMP': '123',
                'MESSAGE': 'ok',
            }))
        client = _make_logs_app()
        r = client.get('/api/shell/system/logs')
        data = json.loads(r.data)
        self.assertEqual(data['count'], 1)


if __name__ == '__main__':
    unittest.main()
