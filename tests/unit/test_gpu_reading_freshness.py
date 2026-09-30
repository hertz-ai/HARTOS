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
        # The ledger the memo is checked against; the cached reading was
        # taken at its current revision (nothing loaded or unloaded since).
        self.mgr._allocations = vm._AllocationLedger()
        self.mgr._gpu_info_seq = self.mgr._allocations.changed_seq
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


class TestALoadOrUnloadInvalidatesTheMemo(_Manager):
    """The same class of failure from the other side, 2026-09-16 (live app
    PID 26452): llama-server came up and llama_config booked
    ``_allocations['llm'] = 2.84`` after its health check passed, but a
    reading taken BEFORE the load (7.5 GB free) stayed inside the 120 s TTL
    and was what every non-LLM selector sized itself against.

    A load or unload is the transition; the allocation ledger is the
    manager's own record of it -- llama_config writes the row directly,
    notify_loaded/allocate() book tools, both release on unload.  So a
    memo older than the ledger's last change is re-probed ONCE, and a
    memo newer than it is kept.  detect_gpu() and refresh_gpu_info()
    both honour it; the TTL still bounds the steady state.
    """

    def _book_llm(self):
        self.mgr._allocations['llm'] = 2.84   # llama_config, health OK

    def test_a_memo_older_than_the_last_allocation_change_is_reprobed(self):
        self._at(5)                            # fresh by TTL standards
        self._book_llm()
        with patch.object(self.mgr, '_probe_gpu', self._fresh):
            got = self.mgr.detect_gpu()
        self.assertEqual(len(self.probes), 1,
                         'the pre-load reading was served after the LLM '
                         'booked its VRAM')
        self.assertEqual(got['free_gb'], 7.56)

    def test_the_reprobe_happens_once_not_on_every_read(self):
        self._at(5)
        self._book_llm()
        with patch.object(self.mgr, '_probe_gpu', self._fresh):
            self.mgr.detect_gpu()
            self.mgr.detect_gpu()
            self.mgr.get_free_vram()
        self.assertEqual(len(self.probes), 1)

    def test_a_memo_newer_than_the_last_change_is_kept(self):
        self._book_llm()
        with patch.object(self.mgr, '_probe_gpu', self._fresh):
            self.mgr.detect_gpu()              # the one re-probe
            self.probes.clear()
            self.mgr.detect_gpu()
            self.mgr.get_free_vram()
        self.assertEqual(self.probes, [])

    def test_releasing_a_row_invalidates_too(self):
        self._book_llm()
        with patch.object(self.mgr, '_probe_gpu', self._fresh):
            self.mgr.detect_gpu()
            self.probes.clear()
            self.mgr._allocations.pop('llm', 0)   # llama_config on stop
            self.mgr.detect_gpu()
        self.assertEqual(len(self.probes), 1)

    def test_popping_a_row_that_was_never_booked_changes_nothing(self):
        self._at(5)
        self.mgr._allocations.pop('llm', 0)
        with patch.object(self.mgr, '_probe_gpu', self._fresh):
            self.mgr.detect_gpu()
        self.assertEqual(self.probes, [])

    def test_refresh_gpu_info_inside_the_ttl_honours_the_ledger(self):
        self._at(5)
        self._book_llm()
        with patch.object(self.mgr, '_probe_gpu', self._fresh):
            got = self.mgr.refresh_gpu_info()
        self.assertEqual(len(self.probes), 1)
        self.assertEqual(got['free_gb'], 7.56)

    def test_a_change_that_lands_during_the_probe_invalidates_its_result(self):
        """The revision is read before the probe, so a booking that lands
        while nvidia-smi runs is newer than the reading it produced."""
        self._at(5)
        self.mgr._allocations['tts_f5'] = 1.3  # something to re-probe for

        def _probe_with_booking():
            self._book_llm()                    # lands mid-probe
            return self._fresh()

        with patch.object(self.mgr, '_probe_gpu', _probe_with_booking):
            self.mgr.detect_gpu()
        with patch.object(self.mgr, '_probe_gpu', self._fresh):
            self.mgr.detect_gpu()
        self.assertEqual(len(self.probes), 2)

    def test_the_ledger_is_still_a_plain_mapping_for_its_readers(self):
        """get_allocations()/get_allocations_display() copy it with dict()."""
        self._book_llm()
        self.assertEqual(dict(self.mgr._allocations), {'llm': 2.84})
        self.assertIn('llm', self.mgr._allocations)
        self.assertEqual(sum(self.mgr._allocations.values()), 2.84)


if __name__ == '__main__':
    unittest.main()
