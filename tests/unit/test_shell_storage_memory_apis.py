"""
Tests for the #157 Disk Utility + Memory backend in shell_system_apis.py.

Behavioural (Gate 5): each test registers the REAL routes on a Flask test client
and drives them with the subprocess / FS boundary mocked at the module seam
(_run / _run_async_bounded / the safety helpers / shutil.which). Every assertion
checks an OBSERVABLE response (status code, JSON), the destructive-op confirm +
protected-disk gates, and the degrade paths - never source text.

Covered capabilities:
  * devices       -> lsblk -J parsed into a flat list
  * health        -> live smartctl readout, snapshot fallback
  * capabilities  -> per-FS op availability from shutil.which
  * fsck          -> check default; repair refused on mounted / system disk
  * defrag        -> ext4/btrfs/xfs only; flash FS refused
  * trim          -> fstrim
  * format        -> confirm required; mounted / system disk refused; 7-FS set
  * resize        -> confirm required; xfs shrink unsupported
  * memory        -> ram/swap/zram/oomd surface
"""

import json
import unittest
from unittest.mock import patch

import integrations.agent_engine.shell_system_apis as mod


class _CP:
    """Minimal CompletedProcess stand-in."""
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _client():
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    mod.register_shell_system_routes(app)
    return app.test_client()


# ═══════════════════════════════════════════════════════════════
# Read-only surfaces
# ═══════════════════════════════════════════════════════════════

class TestDevices(unittest.TestCase):
    def test_lsblk_parsed(self):
        lsblk_json = json.dumps({"blockdevices": [
            {"name": "sda", "path": "/dev/sda", "type": "disk", "size": 500107862016,
             "rota": True, "model": "Test SSD ", "mountpoint": None, "fstype": None,
             "children": [
                 {"name": "sda1", "path": "/dev/sda1", "type": "part", "size": 512,
                  "rota": True, "model": None, "mountpoint": "/", "fstype": "ext4"}]},
        ]})
        with patch.object(mod, '_run', return_value=_CP(0, lsblk_json)):
            r = _client().get('/api/shell/storage/devices')
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        names = [d['name'] for d in data['devices']]
        self.assertEqual(names, ['sda', 'sda1'])
        self.assertEqual(data['devices'][0]['model'], 'Test SSD')  # stripped
        self.assertEqual(data['devices'][1]['fstype'], 'ext4')

    def test_lsblk_failure_is_empty(self):
        with patch.object(mod, '_run', return_value=None):
            r = _client().get('/api/shell/storage/devices')
        self.assertEqual(json.loads(r.data)['devices'], [])


class TestHealth(unittest.TestCase):
    def test_live_smart_readout(self):
        lsblk_json = json.dumps({"blockdevices": [
            {"name": "sda", "path": "/dev/sda", "type": "disk", "size": 1,
             "rota": False, "model": "SSD"}]})
        smart_json = json.dumps({
            "smart_status": {"passed": True},
            "temperature": {"current": 37},
            "power_on_time": {"hours": 1234}})

        def fake_run(cmd, timeout=10, **kw):
            if cmd[0] == 'lsblk':
                return _CP(0, lsblk_json)
            if cmd[0] == 'smartctl':
                return _CP(0, smart_json)
            return None

        with patch.object(mod, '_run', side_effect=fake_run):
            r = _client().get('/api/shell/storage/health')
        data = json.loads(r.data)
        self.assertEqual(data['source'], 'live')
        self.assertEqual(data['devices'][0]['smart'], 'passed')
        self.assertEqual(data['devices'][0]['temperature_c'], 37)
        self.assertEqual(data['devices'][0]['power_on_hours'], 1234)

    def test_snapshot_fallback(self):
        """No live disks -> fall back to the hart-disk-health boot snapshot."""
        snap = {'ok': True, 'devices': [{'name': 'sdb', 'smart': 'passed'}]}
        with patch.object(mod, '_disk_health_live', return_value=[]), \
             patch.object(mod, '_read_disk_health_snapshot', return_value=snap):
            r = _client().get('/api/shell/storage/health')
        data = json.loads(r.data)
        self.assertEqual(data['source'], 'snapshot')
        self.assertEqual(data['devices'][0]['name'], 'sdb')


class TestCapabilities(unittest.TestCase):
    def test_reports_present_tools(self):
        def which(tool):
            return '/usr/bin/' + tool if tool in ('mkfs.ext4', 'e4defrag', 'xfs_growfs') else None
        with patch.object(mod.shutil, 'which', side_effect=which):
            r = _client().get('/api/shell/storage/capabilities')
        data = json.loads(r.data)
        self.assertIn('ext4', data['supported_filesystems'])
        self.assertIn('f2fs', data['supported_filesystems'])
        self.assertTrue(data['filesystems']['ext4']['format'])
        self.assertTrue(data['filesystems']['ext4']['defrag'])
        self.assertTrue(data['filesystems']['xfs']['resize'])  # xfs_growfs present
        self.assertFalse(data['filesystems']['ntfs']['format'])  # mkfs.ntfs absent


# ═══════════════════════════════════════════════════════════════
# fsck (check default, repair gated)
# ═══════════════════════════════════════════════════════════════

class TestFsck(unittest.TestCase):
    def test_invalid_device(self):
        r = _client().post('/api/shell/storage/fsck',
                           json={'device': 'not-a-dev'})
        self.assertEqual(r.status_code, 400)

    def test_repair_refused_on_mounted(self):
        with patch.object(mod, '_is_mounted', return_value=True):
            r = _client().post('/api/shell/storage/fsck',
                               json={'device': '/dev/sdb1', 'fstype': 'ext4', 'repair': True})
        self.assertEqual(r.status_code, 409)

    def test_repair_refused_on_system_disk(self):
        with patch.object(mod, '_is_mounted', return_value=False), \
             patch.object(mod, '_is_protected_device', return_value=True):
            r = _client().post('/api/shell/storage/fsck',
                               json={'device': '/dev/sda1', 'fstype': 'ext4', 'repair': True})
        self.assertEqual(r.status_code, 403)

    def test_check_success(self):
        with patch.object(mod, '_is_mounted', return_value=False), \
             patch.object(mod.shutil, 'which', return_value='/usr/bin/e2fsck'), \
             patch.object(mod, '_run_async_bounded', return_value=(True, _CP(0, "clean"))):
            r = _client().post('/api/shell/storage/fsck',
                               json={'device': '/dev/sdb1', 'fstype': 'ext4'})
        data = json.loads(r.data)
        self.assertTrue(data['ok'])
        self.assertEqual(data['mode'], 'check')

    def test_unsupported_fstype(self):
        with patch.object(mod, '_is_mounted', return_value=False):
            r = _client().post('/api/shell/storage/fsck',
                               json={'device': '/dev/sdb1', 'fstype': 'zfs'})
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════
# defrag (ext4/btrfs/xfs only)
# ═══════════════════════════════════════════════════════════════

class TestDefrag(unittest.TestCase):
    def test_flash_fs_refused(self):
        with patch('os.path.isdir', return_value=True), \
             patch.object(mod, '_path_fstype', return_value='f2fs'):
            r = _client().post('/api/shell/storage/defrag', json={'mount': '/data'})
        self.assertEqual(r.status_code, 400)

    def test_ext4_defrag_success(self):
        with patch('os.path.isdir', return_value=True), \
             patch.object(mod.shutil, 'which', return_value='/usr/bin/e4defrag'), \
             patch.object(mod, '_run_async_bounded', return_value=(True, _CP(0, "done"))):
            r = _client().post('/api/shell/storage/defrag',
                               json={'mount': '/home', 'fstype': 'ext4'})
        data = json.loads(r.data)
        self.assertTrue(data['ok'])
        self.assertEqual(data['fstype'], 'ext4')


# ═══════════════════════════════════════════════════════════════
# trim
# ═══════════════════════════════════════════════════════════════

class TestTrim(unittest.TestCase):
    def test_trim_success(self):
        with patch('os.path.isdir', return_value=True), \
             patch.object(mod, '_run', return_value=_CP(0, "/: 1 GiB trimmed")):
            r = _client().post('/api/shell/storage/trim', json={'mount': '/'})
        data = json.loads(r.data)
        self.assertTrue(data['ok'])

    def test_trim_no_tool(self):
        with patch('os.path.isdir', return_value=True), \
             patch.object(mod, '_run', return_value=None):
            r = _client().post('/api/shell/storage/trim', json={'mount': '/'})
        self.assertEqual(r.status_code, 500)


# ═══════════════════════════════════════════════════════════════
# format (DESTRUCTIVE: confirm + protected + mounted gates)
# ═══════════════════════════════════════════════════════════════

class TestFormat(unittest.TestCase):
    def test_requires_confirm(self):
        r = _client().post('/api/shell/storage/format',
                           json={'device': '/dev/sdb', 'fstype': 'ext4'})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(json.loads(r.data).get('requires_confirm'))

    def test_unsupported_fstype(self):
        r = _client().post('/api/shell/storage/format',
                           json={'device': '/dev/sdb', 'fstype': 'reiserfs', 'confirm': True})
        self.assertEqual(r.status_code, 400)

    def test_refuses_system_disk(self):
        with patch.object(mod, '_is_protected_device', return_value=True):
            r = _client().post('/api/shell/storage/format',
                               json={'device': '/dev/sda', 'fstype': 'ext4', 'confirm': True})
        self.assertEqual(r.status_code, 403)

    def test_refuses_mounted(self):
        with patch.object(mod, '_is_protected_device', return_value=False), \
             patch.object(mod, '_is_mounted', return_value=True):
            r = _client().post('/api/shell/storage/format',
                               json={'device': '/dev/sdb', 'fstype': 'ext4', 'confirm': True})
        self.assertEqual(r.status_code, 409)

    def test_format_success_all_fs(self):
        """Every one of the 7 supported filesystems formats through the same gate."""
        for fs in ['ext4', 'btrfs', 'xfs', 'vfat', 'exfat', 'ntfs', 'f2fs']:
            with patch.object(mod, '_is_protected_device', return_value=False), \
                 patch.object(mod, '_is_mounted', return_value=False), \
                 patch.object(mod.shutil, 'which', return_value='/usr/bin/mkfs'), \
                 patch.object(mod, '_run_async_bounded', return_value=(True, _CP(0, "ok"))):
                r = _client().post('/api/shell/storage/format',
                                   json={'device': '/dev/sdb', 'fstype': fs,
                                         'label': 'DATA', 'confirm': True})
            data = json.loads(r.data)
            self.assertTrue(data['ok'], f'{fs} format should succeed')
            self.assertEqual(data['fstype'], fs)


# ═══════════════════════════════════════════════════════════════
# resize (DESTRUCTIVE: confirm; xfs shrink unsupported)
# ═══════════════════════════════════════════════════════════════

class TestResize(unittest.TestCase):
    def test_requires_confirm(self):
        r = _client().post('/api/shell/storage/resize',
                           json={'device': '/dev/sdb1', 'fstype': 'ext4', 'size': '10G'})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(json.loads(r.data).get('requires_confirm'))

    def test_xfs_shrink_unsupported(self):
        with patch.object(mod, '_is_protected_device', return_value=False):
            r = _client().post('/api/shell/storage/resize',
                               json={'device': '/dev/sdb1', 'fstype': 'xfs',
                                     'size': '5G', 'grow': False, 'confirm': True})
        self.assertEqual(r.status_code, 400)

    def test_grow_success(self):
        with patch.object(mod, '_is_protected_device', return_value=False), \
             patch.object(mod, '_is_mounted', return_value=False), \
             patch.object(mod.shutil, 'which', return_value='/usr/bin/resize2fs'), \
             patch.object(mod, '_run_async_bounded', return_value=(True, _CP(0, "grown"))):
            r = _client().post('/api/shell/storage/resize',
                               json={'device': '/dev/sdb1', 'fstype': 'ext4',
                                     'grow': True, 'confirm': True})
        self.assertTrue(json.loads(r.data)['ok'])


# ═══════════════════════════════════════════════════════════════
# memory
# ═══════════════════════════════════════════════════════════════

class TestMemory(unittest.TestCase):
    def test_memory_surface(self):
        def fake_run(cmd, timeout=10, **kw):
            if cmd[0] == 'zramctl':
                return _CP(0, "zram0 zstd 8G 1.2G")
            if cmd[0] == 'systemctl':
                return _CP(0, "active")
            return None
        with patch.object(mod, '_run', side_effect=fake_run):
            r = _client().get('/api/shell/memory')
        data = json.loads(r.data)
        self.assertIn('ram', data)
        self.assertIn('swap', data)
        self.assertEqual(data['zram'][0]['algorithm'], 'zstd')
        self.assertTrue(data['oomd']['active'])


if __name__ == "__main__":
    unittest.main()
