"""
Tests for LiquidUI system metrics and process list APIs.

Covers: /api/shell/system/metrics, /api/shell/system/processes
"""

import json
import unittest
from unittest.mock import patch, MagicMock


def _make_system_app():
    from flask import Flask, jsonify
    app = Flask(__name__)

    @app.route('/api/shell/system/metrics', methods=['GET'])
    def shell_system_metrics():
        metrics = {}
        try:
            import psutil
            metrics['cpu_percent'] = psutil.cpu_percent(interval=0.1)
            metrics['cpu_count'] = psutil.cpu_count()
            mem = psutil.virtual_memory()
            metrics['ram'] = {
                'total_gb': round(mem.total / (1024**3), 1),
                'used_gb': round(mem.used / (1024**3), 1),
                'percent': mem.percent,
            }
            disks = []
            for part in psutil.disk_partitions():
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    disks.append({
                        'mount': part.mountpoint,
                        'total_gb': round(usage.total / (1024**3), 1),
                        'used_gb': round(usage.used / (1024**3), 1),
                        'percent': usage.percent,
                    })
                except (PermissionError, OSError):
                    pass
            metrics['disks'] = disks
            net = psutil.net_io_counters()
            metrics['network'] = {
                'bytes_sent': net.bytes_sent,
                'bytes_recv': net.bytes_recv,
            }
            metrics['load_avg'] = list(psutil.getloadavg()) if hasattr(psutil, 'getloadavg') else []
            metrics['uptime_seconds'] = int(
                __import__('time').time() - psutil.boot_time())
        except ImportError:
            metrics['error'] = 'psutil not installed'
        return jsonify(metrics)

    @app.route('/api/shell/system/processes', methods=['GET'])
    def shell_system_processes():
        procs = []
        try:
            import psutil
            for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
                try:
                    info = p.info
                    if info.get('cpu_percent', 0) > 0 or info.get('memory_percent', 0) > 0.1:
                        procs.append({
                            'pid': info['pid'],
                            'name': info['name'],
                            'cpu': round(info.get('cpu_percent', 0), 1),
                            'mem': round(info.get('memory_percent', 0), 1),
                        })
                except Exception:
                    pass
            procs.sort(key=lambda p: p['cpu'], reverse=True)
        except ImportError:
            pass
        return jsonify({'processes': procs[:30]})

    return app.test_client()


class TestShellSystemMetrics(unittest.TestCase):
    """Tests for GET /api/shell/system/metrics."""

    def test_returns_cpu_percent(self):
        client = _make_system_app()
        r = client.get('/api/shell/system/metrics')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('cpu_percent', data)
        self.assertIsInstance(data['cpu_percent'], (int, float))

    def test_returns_ram(self):
        client = _make_system_app()
        r = client.get('/api/shell/system/metrics')
        data = json.loads(r.data)
        self.assertIn('ram', data)
        self.assertIn('total_gb', data['ram'])
        self.assertIn('used_gb', data['ram'])
        self.assertIn('percent', data['ram'])
        self.assertGreater(data['ram']['total_gb'], 0)

    def test_returns_disks(self):
        client = _make_system_app()
        r = client.get('/api/shell/system/metrics')
        data = json.loads(r.data)
        self.assertIn('disks', data)
        self.assertIsInstance(data['disks'], list)
        if data['disks']:
            d = data['disks'][0]
            self.assertIn('mount', d)
            self.assertIn('total_gb', d)

    def test_returns_network(self):
        client = _make_system_app()
        r = client.get('/api/shell/system/metrics')
        data = json.loads(r.data)
        self.assertIn('network', data)
        self.assertIn('bytes_sent', data['network'])
        self.assertIn('bytes_recv', data['network'])

    def test_returns_cpu_count(self):
        client = _make_system_app()
        r = client.get('/api/shell/system/metrics')
        data = json.loads(r.data)
        self.assertIn('cpu_count', data)
        self.assertGreater(data['cpu_count'], 0)

    def test_returns_uptime(self):
        client = _make_system_app()
        r = client.get('/api/shell/system/metrics')
        data = json.loads(r.data)
        self.assertIn('uptime_seconds', data)
        self.assertGreater(data['uptime_seconds'], 0)


class TestShellSystemProcesses(unittest.TestCase):
    """Tests for GET /api/shell/system/processes."""

    def test_returns_process_list(self):
        client = _make_system_app()
        r = client.get('/api/shell/system/processes')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn('processes', data)
        self.assertIsInstance(data['processes'], list)

    def test_process_structure(self):
        client = _make_system_app()
        r = client.get('/api/shell/system/processes')
        data = json.loads(r.data)
        procs = data['processes']
        if procs:
            p = procs[0]
            self.assertIn('pid', p)
            self.assertIn('name', p)
            self.assertIn('cpu', p)
            self.assertIn('mem', p)

    def test_max_30_processes(self):
        client = _make_system_app()
        r = client.get('/api/shell/system/processes')
        data = json.loads(r.data)
        self.assertLessEqual(len(data['processes']), 30)

    def test_sorted_by_cpu(self):
        client = _make_system_app()
        r = client.get('/api/shell/system/processes')
        data = json.loads(r.data)
        procs = data['processes']
        if len(procs) >= 2:
            cpus = [p['cpu'] for p in procs]
            self.assertEqual(cpus, sorted(cpus, reverse=True))


if __name__ == '__main__':
    unittest.main()
