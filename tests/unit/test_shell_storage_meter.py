"""The disk/storage meter counts only real writable storage, not the squashfs.

2026-07-24 audit (docs/audit/ux_degrading_design_choices_2026-07-24.md #0.4):
/api/shell/storage aggregated EVERY psutil partition with no filter, so on the live
ISO the read-only squashfs Nix store (/nix/.ro-store, always 100% full by nature)
and the ISO mount were counted -- the Storage panel alarmed at ~100% used for no
real reason (the "Disk 100%" the steward saw). The fix skips read-only + squashfs /
iso9660 / overlay / ramfs mounts.

Behavioural: registers the REAL route on a bare Flask app, mocks psutil at the
boundary, and asserts the served JSON excludes the image mounts and reports the
writable root's real usage. shell_system_apis imports clean (no full-app boot).

Run (dev box, targeted):
    python -m pytest tests/unit/test_shell_storage_meter.py -v \
        --noconftest -p no:cacheprovider
"""
from unittest.mock import patch

import pytest
from flask import Flask

from integrations.agent_engine.shell_system_apis import register_shell_system_routes

_GB = 1073741824


class _Part:
    def __init__(self, device, mount, fstype, opts):
        self.device = device
        self.mountpoint = mount
        self.fstype = fstype
        self.opts = opts


class _Usage:
    def __init__(self, total, used, free, percent):
        self.total = total
        self.used = used
        self.free = free
        self.percent = percent


@pytest.fixture
def client():
    app = Flask(__name__)
    register_shell_system_routes(app)
    app.testing = True
    return app.test_client()


def test_storage_excludes_squashfs_iso_and_readonly(client):
    parts = [
        _Part('/dev/sda1', '/', 'ext4', 'rw,relatime'),                   # real writable
        _Part('/dev/loop0', '/nix/.ro-store', 'squashfs', 'ro,relatime'),  # always 100%
        _Part('/dev/sr0', '/iso', 'iso9660', 'ro'),                        # iso, 100%
        _Part('/dev/sdb1', '/data', 'ext4', 'ro,relatime'),               # ro data disk
        _Part('overlay', '/nix/store', 'overlay', 'rw,relatime'),          # overlay pseudo
    ]
    usages = {
        '/': _Usage(100 * _GB, 40 * _GB, 60 * _GB, 40.0),
        '/nix/.ro-store': _Usage(6 * _GB, 6 * _GB, 0, 100.0),
        '/iso': _Usage(6 * _GB, 6 * _GB, 0, 100.0),
        '/data': _Usage(50 * _GB, 50 * _GB, 0, 100.0),
        '/nix/store': _Usage(6 * _GB, 6 * _GB, 0, 100.0),
    }
    with patch('psutil.disk_partitions', return_value=parts), \
         patch('psutil.disk_usage', side_effect=lambda m: usages[m]):
        r = client.get('/api/shell/storage')

    data = r.get_json()
    mounts = {p['mount'] for p in data['partitions']}
    assert mounts == {'/'}, f"image/ro mounts not excluded: {mounts}"
    # The aggregate reflects ONLY the writable ext4 root (40% of 100 GB), NOT the
    # squashfs 100% that produced the false "Disk 100%".
    assert data['total_gb'] == 100.0
    assert data['used_gb'] == 40.0
    assert data['overall_percent'] == 40.0


def test_all_writable_partitions_still_counted(client):
    parts = [
        _Part('/dev/sda1', '/', 'ext4', 'rw,relatime'),
        _Part('/dev/sda2', '/home', 'ext4', 'rw,relatime'),
    ]
    usages = {
        '/': _Usage(100 * _GB, 50 * _GB, 50 * _GB, 50.0),
        '/home': _Usage(100 * _GB, 10 * _GB, 90 * _GB, 10.0),
    }
    with patch('psutil.disk_partitions', return_value=parts), \
         patch('psutil.disk_usage', side_effect=lambda m: usages[m]):
        data = client.get('/api/shell/storage').get_json()
    assert {p['mount'] for p in data['partitions']} == {'/', '/home'}
    assert data['total_gb'] == 200.0
    assert data['overall_percent'] == 30.0  # 60GB used / 200GB
