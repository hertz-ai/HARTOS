"""Unit tests for VRAM auto-tighten telemetry in vram_manager."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# `integrations.service_tools.__init__` rebinds `vram_manager` to the
# singleton INSTANCE, which shadows attribute access to the submodule.
# Fetch the module object directly out of sys.modules (populated by the
# import) so patch.object / attribute access resolve to the module.
import integrations.service_tools.vram_manager  # noqa: F401 (ensure cached)
import sys
vm = sys.modules['integrations.service_tools.vram_manager']


class _IsolatedManagerMixin:
    """Each test gets a fresh VRAMManager with a temp-file telemetry store."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._path = Path(self._tmpdir) / 'vram_measured.json'
        with patch.object(vm.VRAMManager, '_resolve_measured_path',
                          return_value=self._path):
            self.mgr = vm.VRAMManager()

    def _set_fake_gpu(self, free_gb: float = 10.0, total_gb: float = 16.0):
        self.mgr._gpu_info = {
            'name': 'fake', 'total_gb': total_gb, 'free_gb': free_gb,
            'cuda_available': True,
        }
        self.mgr._gpu_info_ts = 99999.0  # suppress refresh


class RecordActualUsageTest(_IsolatedManagerMixin, unittest.TestCase):
    """record_actual_usage stores, persists, and clamps."""

    def test_positive_measurement_recorded(self):
        self.mgr.record_actual_usage('tts_omnivoice', 2.1)
        self.assertEqual(
            self.mgr.get_measured_usage().get('tts_omnivoice'),
            2.1,
        )

    def test_measurement_persisted_to_disk(self):
        self.mgr.record_actual_usage('tts_f5', 1.35)
        data = json.loads(self._path.read_text(encoding='utf-8'))
        self.assertAlmostEqual(data['tts_f5'], 1.35)

    def test_zero_ignored(self):
        # Worker couldn't measure (CPU-only, Metal) — emits 0.0
        self.mgr.record_actual_usage('tts_kokoro', 0.0)
        self.assertNotIn('tts_kokoro', self.mgr.get_measured_usage())

    def test_negative_ignored(self):
        self.mgr.record_actual_usage('tts_chatterbox_turbo', -1.0)
        self.assertNotIn('tts_chatterbox_turbo', self.mgr.get_measured_usage())

    def test_absurd_value_ignored(self):
        # 100 GB can't be right on any consumer card — ignore
        self.mgr.record_actual_usage('tts_f5', 100.0)
        self.assertNotIn('tts_f5', self.mgr.get_measured_usage())

    def test_string_input_coerced(self):
        self.mgr.record_actual_usage('tts_f5', '1.9')  # type: ignore
        self.assertAlmostEqual(
            self.mgr.get_measured_usage().get('tts_f5'), 1.9,
        )

    def test_rubbish_input_ignored(self):
        self.mgr.record_actual_usage('tts_f5', 'not-a-number')  # type: ignore
        self.assertNotIn('tts_f5', self.mgr.get_measured_usage())

    def test_later_measurement_overwrites(self):
        self.mgr.record_actual_usage('tts_f5', 1.2)
        self.mgr.record_actual_usage('tts_f5', 1.5)
        self.assertEqual(self.mgr.get_measured_usage().get('tts_f5'), 1.5)


class EffectiveBudgetTest(_IsolatedManagerMixin, unittest.TestCase):
    """get_effective_budget merges VRAM_BUDGETS with measurements."""

    def test_no_measurement_falls_back_to_declared(self):
        eff = self.mgr.get_effective_budget('tts_indic_parler')
        self.assertEqual(eff, vm.VRAM_BUDGETS['tts_indic_parler'])

    def test_measurement_shrinks_model_size(self):
        # Declared 3.8 GB for chatterbox_turbo; measure 2.5 GB
        self.mgr.record_actual_usage('tts_chatterbox_turbo', 2.5)
        _min, size = self.mgr.get_effective_budget('tts_chatterbox_turbo')
        self.assertEqual(size, 2.5)

    def test_measurement_min_has_headroom_floor(self):
        # min_vram should never drop below declared minimum, even if
        # measured size is small
        self.mgr.record_actual_usage('tts_chatterbox_turbo', 1.0)
        min_vram, _size = self.mgr.get_effective_budget('tts_chatterbox_turbo')
        declared_min = vm.VRAM_BUDGETS['tts_chatterbox_turbo'][0]
        self.assertGreaterEqual(min_vram, declared_min)

    def test_unknown_tool_returns_none(self):
        self.assertIsNone(self.mgr.get_effective_budget('nonexistent'))


class CanFitUsesEffectiveBudgetTest(_IsolatedManagerMixin, unittest.TestCase):
    """can_fit/allocate use the measurement when present."""

    def test_can_fit_rejects_when_free_below_declared_min(self):
        # f5 declared min is 2.5 GB; set free to 2.0 GB
        self._set_fake_gpu(free_gb=2.0)
        self.assertFalse(self.mgr.can_fit('tts_f5'))

    def test_can_fit_accepts_when_free_above_declared_min(self):
        self._set_fake_gpu(free_gb=5.0)
        self.assertTrue(self.mgr.can_fit('tts_f5'))

    def test_allocate_records_measured_not_declared(self):
        self._set_fake_gpu(free_gb=16.0)
        self.mgr.record_actual_usage('tts_f5', 1.3)
        self.assertTrue(self.mgr.allocate('tts_f5'))
        self.assertAlmostEqual(
            self.mgr.get_allocations().get('tts_f5'), 1.3,
        )


class PersistenceRoundtripTest(_IsolatedManagerMixin, unittest.TestCase):
    """Measurements survive a VRAMManager restart."""

    def test_reload_reads_back_measurements(self):
        self.mgr.record_actual_usage('tts_omnivoice', 2.4)
        # Fresh manager reads same file
        with patch.object(vm.VRAMManager, '_resolve_measured_path',
                          return_value=self._path):
            fresh = vm.VRAMManager()
        self.assertEqual(
            fresh.get_measured_usage().get('tts_omnivoice'),
            2.4,
        )

    def test_corrupt_file_ignored_on_load(self):
        self._path.write_text('not valid json', encoding='utf-8')
        with patch.object(vm.VRAMManager, '_resolve_measured_path',
                          return_value=self._path):
            fresh = vm.VRAMManager()
        self.assertEqual(fresh.get_measured_usage(), {})


if __name__ == '__main__':
    unittest.main()
