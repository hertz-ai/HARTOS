"""Behavioural test: NON-destructive log export to a (mock) removable disk.

Proves the inverse-of-the-flasher contract: logs land in a HARTOS-logs subfolder,
the disk's pre-existing content is preserved, unmounted dest fails honestly, and
one unreadable log never aborts the rest. Real handler, real filesystem (temp
dirs), only the platform-path source mocked. Run directly (pytest OOMs the box):
    C:/Users/sathi/miniconda3/python.exe tests/unit/test_log_export.py
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from core.log_export import export_logs_to_disk, EXPORT_DIRNAME


class TestLogExport(unittest.TestCase):
    def setUp(self):
        self.usb = tempfile.mkdtemp()        # the "removable disk"
        # pre-existing user content that MUST be preserved (non-destructive)
        with open(os.path.join(self.usb, 'my_photo.jpg'), 'wb') as f:
            f.write(b'PRECIOUS')
        self.logsrc = tempfile.mkdtemp()
        for n in ('frozen_debug.log', 'app.log'):
            with open(os.path.join(self.logsrc, n), 'w') as f:
                f.write('event line\n')

    def tearDown(self):
        shutil.rmtree(self.usb, ignore_errors=True)
        shutil.rmtree(self.logsrc, ignore_errors=True)

    def test_copies_logs_into_subfolder(self):
        srcs = [os.path.join(self.logsrc, 'frozen_debug.log'),
                os.path.join(self.logsrc, 'app.log')]
        res = export_logs_to_disk(self.usb, sources=srcs)
        self.assertTrue(res['ok'])
        self.assertEqual(sorted(res['files']), ['app.log', 'frozen_debug.log'])
        self.assertTrue(os.path.isfile(
            os.path.join(self.usb, EXPORT_DIRNAME, 'frozen_debug.log')))
        self.assertGreater(res['bytes'], 0)

    def test_preserves_existing_disk_content(self):
        export_logs_to_disk(self.usb, sources=[os.path.join(self.logsrc, 'app.log')])
        keep = os.path.join(self.usb, 'my_photo.jpg')   # untouched — NON-destructive
        self.assertTrue(os.path.isfile(keep))
        with open(keep, 'rb') as f:
            self.assertEqual(f.read(), b'PRECIOUS')

    def test_unmounted_dest_is_honest_failure(self):
        res = export_logs_to_disk('/no/such/mount', sources=[])
        self.assertFalse(res['ok'])
        self.assertIn('not a mounted', res['error'])

    def test_unreadable_log_does_not_abort_others(self):
        good = os.path.join(self.logsrc, 'app.log')
        res = export_logs_to_disk(self.usb, sources=['/no/such.log', good])
        self.assertTrue(res['ok'])
        self.assertEqual(res['files'], ['app.log'])

    def test_default_sources_use_platform_dirs(self):
        with patch('core.log_export.get_log_dir', return_value=self.logsrc), \
             patch('core.log_export.get_db_dir', return_value=self.logsrc):
            res = export_logs_to_disk(self.usb)
        self.assertTrue(res['ok'])
        self.assertIn('frozen_debug.log', res['files'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
