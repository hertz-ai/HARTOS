"""A GPU reading used to size a spawn must not be a memoized one.

THE LIVE FAILURE THIS ENCODES (2026-09-10, installed build):

    16:19:58,967  GPU (nvidia-smi): ... 8.0 GB total, 2.98 GB free
    16:20:16      llama-server on :8080 died (connection refused)
    16:21:55,203  Dynamic context size: 4096
                  (VRAM free=3.0GB, model=2.8GB, remaining=0.1GB)
    16:21:59,261  GPU (nvidia-smi): ... 8.0 GB total, 7.56 GB free

`detect_gpu()` is a plain memo -- `if self._gpu_info is not None: return it`,
with NO TTL check of its own.  Only `refresh_gpu_info()` carries the TTL, and
under NUNBA_BUNDLED that TTL is 120 s.  So the one decision that fixes the
server geometry for the whole process read a sample taken 117 s earlier,
WHILE THE OUTGOING SERVER STILL HELD 4.5 GB.  4.76 GB of headroom was read as
0.1 GB, the smallest context tier (4096) was chosen, and every tool-carrying
request then died -- the tool schema alone measured 8026 tokens.

The TTL alone cannot fix this: at 16:21:55 the previous sample was 117 s old,
under the 120 s TTL, so `refresh_gpu_info()` would have returned the same
stale number.  A spawn is a state transition that invalidates any cached
reading BY CONSTRUCTION, so it needs a way to say "probe now".
"""

import sys
import unittest
from unittest.mock import patch

import integrations.service_tools.vram_manager  # noqa: F401 (ensure cached)

vm = sys.modules['integrations.service_tools.vram_manager']


class _Manager(unittest.TestCase):
    """A manager pre-loaded with a cached reading whose TTL has NOT expired."""

    def setUp(self):
        self.mgr = vm.VRAMManager.__new__(vm.VRAMManager)
        self.mgr._gpu_info = {'name': 'stale', 'total_gb': 8.0,
                              'free_gb': 2.98, 'cuda_available': True}
        self.mgr._refresh_ttl = 120.0
        self.mgr._vendor_tools_absent = False
        self.probes = []

        def _fresh():
            self.probes.append(1)
            info = {'name': 'fresh', 'total_gb': 8.0, 'free_gb': 7.56,
                    'cuda_available': True}
            self.mgr._gpu_info = info
            return info

        self._fresh = _fresh

    def _at(self, age_s):
        """Pin the cache timestamp so the reading is `age_s` seconds old."""
        import time as _t
        self.mgr._gpu_info_ts = _t.monotonic() - age_s


class TestTheTtlPathIsUnchanged(_Manager):
    """force defaults off — every existing caller keeps its TTL behaviour."""

    def test_inside_the_ttl_the_cache_is_returned_and_nothing_is_probed(self):
        self._at(117)
        with patch.object(self.mgr, 'detect_gpu', self._fresh):
            got = self.mgr.refresh_gpu_info()
        self.assertEqual(self.probes, [], 'a fresh probe ran inside the TTL')
        self.assertEqual(got['free_gb'], 2.98)

    def test_past_the_ttl_it_reprobes(self):
        self._at(121)
        with patch.object(self.mgr, 'detect_gpu', self._fresh):
            got = self.mgr.refresh_gpu_info()
        self.assertEqual(len(self.probes), 1)
        self.assertEqual(got['free_gb'], 7.56)


class TestForceBypassesTheTtl(_Manager):
    """THE LIVE SHAPE: 117 s old, inside the TTL, and still wrong."""

    def test_force_reprobes_inside_the_ttl(self):
        self._at(117)
        with patch.object(self.mgr, 'detect_gpu', self._fresh):
            self.mgr.refresh_gpu_info(force=True)
        self.assertEqual(len(self.probes), 1,
                         'force=True did not re-probe: the spawn would size '
                         'itself from the dying server\'s VRAM')

    def test_force_returns_the_new_reading_not_the_cached_one(self):
        self._at(117)
        with patch.object(self.mgr, 'detect_gpu', self._fresh):
            got = self.mgr.refresh_gpu_info(force=True)
        self.assertEqual(got['free_gb'], 7.56)

    def test_force_restamps_the_ttl_clock(self):
        """Otherwise the next unforced call re-probes too, defeating the TTL."""
        self._at(117)
        import time as _t
        before = _t.monotonic()
        with patch.object(self.mgr, 'detect_gpu', self._fresh):
            self.mgr.refresh_gpu_info(force=True)
        self.assertGreaterEqual(self.mgr._gpu_info_ts, before)

    def test_force_clears_the_memo_so_detect_gpu_cannot_short_circuit(self):
        """detect_gpu returns self._gpu_info verbatim when it is set."""
        self._at(117)
        seen = {}

        def _record():
            seen['memo_at_entry'] = self.mgr._gpu_info
            return self._fresh()

        with patch.object(self.mgr, 'detect_gpu', _record):
            self.mgr.refresh_gpu_info(force=True)
        self.assertIsNone(seen['memo_at_entry'],
                          'the memo was still set when detect_gpu ran, so the '
                          'real probe would have been skipped')


if __name__ == '__main__':
    unittest.main()
