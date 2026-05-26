"""Tests for core.prompts_backup - boot-time prompts/ snapshot +
retention.  Closes recovery gap when user wipes data dir."""

import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from core.prompts_backup import (
    snapshot_prompts, list_snapshots, _prune_old_snapshots,
    _snapshots_root, MAX_DAILY_SNAPSHOTS,
)


class TestSnapshotPrompts(unittest.TestCase):

    def setUp(self):
        self.tmp_root = tempfile.mkdtemp()
        self.prompts_dir = os.path.join(self.tmp_root, 'prompts')
        os.makedirs(self.prompts_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def _seed_prompts(self, n=3):
        for i in range(n):
            with open(os.path.join(self.prompts_dir, f'{1000+i}.json'), 'w') as f:
                f.write(f'{{"id":{1000+i}}}')

    def test_missing_prompts_dir_returns_none(self):
        result = snapshot_prompts('/no/such/dir/exists')
        self.assertIsNone(result)

    def test_empty_prompts_dir_returns_none(self):
        # No-op: don't create misleading "everything was wiped" snapshots
        result = snapshot_prompts(self.prompts_dir)
        self.assertIsNone(result)

    def test_creates_snapshot_with_files(self):
        self._seed_prompts(3)
        snap_name = snapshot_prompts(self.prompts_dir)
        self.assertIsNotNone(snap_name)
        snap_dir = os.path.join(_snapshots_root(self.prompts_dir), snap_name)
        self.assertTrue(os.path.isdir(snap_dir))
        files = sorted(os.listdir(snap_dir))
        self.assertEqual(files, ['1000.json', '1001.json', '1002.json'])

    def test_snapshot_files_match_source(self):
        self._seed_prompts(2)
        snap_name = snapshot_prompts(self.prompts_dir)
        snap_dir = os.path.join(_snapshots_root(self.prompts_dir), snap_name)
        with open(os.path.join(snap_dir, '1000.json')) as f:
            self.assertEqual(f.read(), '{"id":1000}')

    def test_only_json_files_copied(self):
        self._seed_prompts(2)
        # Plant a non-json file in source
        with open(os.path.join(self.prompts_dir, 'README.txt'), 'w') as f:
            f.write('not a recipe')
        snap_name = snapshot_prompts(self.prompts_dir)
        snap_dir = os.path.join(_snapshots_root(self.prompts_dir), snap_name)
        files = sorted(os.listdir(snap_dir))
        self.assertNotIn('README.txt', files)

    def test_idempotent_within_same_second(self):
        """Two snapshots within the same second don't double-write."""
        self._seed_prompts(1)
        with patch('core.prompts_backup.time.strftime',
                   return_value='20260504_120000'):
            first = snapshot_prompts(self.prompts_dir)
            second = snapshot_prompts(self.prompts_dir)
        self.assertEqual(first, second)
        # Only one snapshot dir should exist
        self.assertEqual(len(list_snapshots(self.prompts_dir)), 1)


class TestSnapshotTornJsonDefense(unittest.TestCase):
    """M3 in post-shipment review: snapshots must NOT capture
    half-written / corrupt JSON files, even if a concurrent writer
    leaves one mid-flight."""

    def setUp(self):
        self.tmp_root = tempfile.mkdtemp()
        self.prompts_dir = os.path.join(self.tmp_root, 'prompts')
        os.makedirs(self.prompts_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def test_torn_json_skipped_in_snapshot(self):
        # Two valid + one torn JSON file
        with open(os.path.join(self.prompts_dir, '1.json'), 'w') as f:
            f.write('{"valid":true}')
        with open(os.path.join(self.prompts_dir, '2.json'), 'w') as f:
            f.write('{"also":"valid"}')
        with open(os.path.join(self.prompts_dir, '3_torn.json'), 'w') as f:
            f.write('{"half_written":')  # invalid JSON
        snap_name = snapshot_prompts(self.prompts_dir)
        self.assertIsNotNone(snap_name)
        snap_dir = os.path.join(_snapshots_root(self.prompts_dir), snap_name)
        files = sorted(os.listdir(snap_dir))
        self.assertIn('1.json', files)
        self.assertIn('2.json', files)
        self.assertNotIn('3_torn.json', files,
            'torn-JSON file must be skipped to keep snapshot restore-safe')

    def test_only_torn_files_no_snapshot(self):
        """When the only files are torn, the snapshot should be empty
        and rmdir cleanup should fire (no misleading empty snapshot)."""
        with open(os.path.join(self.prompts_dir, 'torn.json'), 'w') as f:
            f.write('{')  # invalid
        result = snapshot_prompts(self.prompts_dir)
        self.assertIsNone(result,
            'snapshot with all-torn files should return None')


class TestRetention(unittest.TestCase):

    def setUp(self):
        self.tmp_root = tempfile.mkdtemp()
        self.prompts_dir = os.path.join(self.tmp_root, 'prompts')
        os.makedirs(self.prompts_dir)
        self._seed_prompts()

    def tearDown(self):
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def _seed_prompts(self):
        with open(os.path.join(self.prompts_dir, '1.json'), 'w') as f:
            f.write('{}')

    def test_prune_keeps_n_newest(self):
        # Manually create 10 snapshot dirs with predictable names
        root = _snapshots_root(self.prompts_dir)
        os.makedirs(root)
        for i in range(10):
            os.makedirs(os.path.join(root, f'2026010{i}_120000'))
        pruned = _prune_old_snapshots(self.prompts_dir, keep=3)
        self.assertEqual(pruned, 7)
        remaining = list_snapshots(self.prompts_dir)
        self.assertEqual(len(remaining), 3)
        # Newest 3 should remain (sorted ascending - last 3 = newest)
        self.assertEqual(remaining,
                         ['20260107_120000', '20260108_120000', '20260109_120000'])

    def test_prune_under_keep_does_nothing(self):
        root = _snapshots_root(self.prompts_dir)
        os.makedirs(root)
        for i in range(2):
            os.makedirs(os.path.join(root, f'2026010{i}_120000'))
        pruned = _prune_old_snapshots(self.prompts_dir, keep=5)
        self.assertEqual(pruned, 0)
        self.assertEqual(len(list_snapshots(self.prompts_dir)), 2)

    def test_prune_keep_zero_does_nothing(self):
        """Defensive: keep=0 would wipe everything, refuse it."""
        root = _snapshots_root(self.prompts_dir)
        os.makedirs(root)
        for i in range(3):
            os.makedirs(os.path.join(root, f'2026010{i}_120000'))
        pruned = _prune_old_snapshots(self.prompts_dir, keep=0)
        self.assertEqual(pruned, 0)


class TestListSnapshots(unittest.TestCase):

    def setUp(self):
        self.tmp_root = tempfile.mkdtemp()
        self.prompts_dir = os.path.join(self.tmp_root, 'prompts')
        os.makedirs(self.prompts_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def test_no_snapshots_returns_empty(self):
        self.assertEqual(list_snapshots(self.prompts_dir), [])

    def test_returns_sorted(self):
        root = _snapshots_root(self.prompts_dir)
        os.makedirs(root)
        # Create out of order
        for name in ['20260105_120000', '20260101_120000', '20260103_120000']:
            os.makedirs(os.path.join(root, name))
        result = list_snapshots(self.prompts_dir)
        self.assertEqual(result, sorted(result))

    def test_skips_non_snapshot_dirs(self):
        root = _snapshots_root(self.prompts_dir)
        os.makedirs(root)
        os.makedirs(os.path.join(root, 'not_a_snapshot'))  # leading non-digit
        os.makedirs(os.path.join(root, '20260101_120000'))  # valid
        result = list_snapshots(self.prompts_dir)
        self.assertEqual(result, ['20260101_120000'])


if __name__ == '__main__':
    unittest.main()
