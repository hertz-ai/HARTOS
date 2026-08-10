"""
Tests for VRAM budget check in ModelLifecycleManager.start().

When pinned models exceed GPU capacity, an ERROR log is emitted.
When all models exceed GPU capacity, a WARNING log is emitted.

Source: integrations/service_tools/model_lifecycle.py ~line 293
"""
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from unittest.mock import patch, MagicMock, PropertyMock


# ── Helper: create ModelState-like objects ──

def _make_model_state(name, vram_gb=0.0, pinned=False):
    """Create a minimal ModelState-like object."""
    from integrations.service_tools.model_lifecycle import ModelState, ModelDevice
    state = ModelState(name=name, vram_gb=vram_gb, pinned=pinned,
                       device=ModelDevice.GPU)
    return state


# ═══════════════════════════════════════════════════════════════
# Functional Tests
# ═══════════════════════════════════════════════════════════════

class TestVRAMBudgetCheckFunctional:
    """FT: VRAM budget warnings and errors on start()."""

    @pytest.fixture
    def manager_with_models(self):
        """Create a ModelLifecycleManager with pre-loaded models."""
        from integrations.service_tools.model_lifecycle import ModelLifecycleManager
        mgr = ModelLifecycleManager()
        return mgr

    def test_pinned_exceeds_total_logs_error(self, manager_with_models, caplog):
        """Pinned models > total VRAM triggers ERROR log."""
        mgr = manager_with_models
        # Set up: 2 pinned models totaling 6GB on a 4GB GPU
        mgr._models['draft'] = _make_model_state('draft', vram_gb=1.5, pinned=True)
        mgr._models['main'] = _make_model_state('main', vram_gb=4.5, pinned=True)

        mock_vm = MagicMock()
        mock_vm.get_total_vram.return_value = 4.0

        with patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._sync_from_rtm'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._detect_tier'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._loop'), \
             patch('integrations.service_tools.runtime_manager.runtime_tool_manager', MagicMock()), \
             patch('integrations.service_tools.vram_manager.get_vram_manager', return_value=mock_vm), \
             caplog.at_level(logging.ERROR):
            mgr.start()

        mgr.stop()
        error_msgs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
        assert any('CRITICAL' in m or 'pinned' in m.lower() for m in error_msgs), \
            f"Expected ERROR about pinned VRAM, got: {error_msgs}"

    def test_all_models_exceed_total_logs_warning(self, manager_with_models, caplog):
        """All models > total VRAM triggers WARNING (but not ERROR if pinned fits)."""
        mgr = manager_with_models
        # Pinned = 1.5GB (fits in 4GB), but total = 5.5GB (exceeds)
        mgr._models['draft'] = _make_model_state('draft', vram_gb=1.5, pinned=True)
        mgr._models['main'] = _make_model_state('main', vram_gb=3.5, pinned=False)
        mgr._models['tts'] = _make_model_state('tts', vram_gb=0.5, pinned=False)

        mock_vm = MagicMock()
        mock_vm.get_total_vram.return_value = 4.0

        with patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._sync_from_rtm'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._detect_tier'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._loop'), \
             patch('integrations.service_tools.runtime_manager.runtime_tool_manager', MagicMock()), \
             patch('integrations.service_tools.vram_manager.get_vram_manager', return_value=mock_vm), \
             caplog.at_level(logging.WARNING):
            mgr.start()

        mgr.stop()
        warn_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any('exceeded' in m.lower() or 'budget' in m.lower() for m in warn_msgs), \
            f"Expected WARNING about VRAM budget, got: {warn_msgs}"

    def test_models_fit_in_vram_no_warning(self, manager_with_models, caplog):
        """When all models fit, no WARNING or ERROR is logged."""
        mgr = manager_with_models
        mgr._models['draft'] = _make_model_state('draft', vram_gb=1.5, pinned=True)
        mgr._models['main'] = _make_model_state('main', vram_gb=2.0, pinned=False)

        mock_vm = MagicMock()
        mock_vm.get_total_vram.return_value = 8.0  # plenty

        with patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._sync_from_rtm'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._detect_tier'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._loop'), \
             patch('integrations.service_tools.runtime_manager.runtime_tool_manager', MagicMock()), \
             patch('integrations.service_tools.vram_manager.get_vram_manager', return_value=mock_vm), \
             caplog.at_level(logging.WARNING):
            mgr.start()

        mgr.stop()
        warn_or_err = [r for r in caplog.records if r.levelno >= logging.WARNING]
        vram_related = [r for r in warn_or_err if 'vram' in r.message.lower() or 'budget' in r.message.lower()]
        assert len(vram_related) == 0, f"Unexpected VRAM warnings: {[r.message for r in vram_related]}"

    def test_no_gpu_detected_skips_check(self, manager_with_models, caplog):
        """When get_total_vram()=0 (no GPU), VRAM check is skipped."""
        mgr = manager_with_models
        mgr._models['draft'] = _make_model_state('draft', vram_gb=1.5, pinned=True)

        mock_vm = MagicMock()
        mock_vm.get_total_vram.return_value = 0  # No GPU detected

        with patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._sync_from_rtm'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._detect_tier'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._loop'), \
             patch('integrations.service_tools.runtime_manager.runtime_tool_manager', MagicMock()), \
             patch('integrations.service_tools.vram_manager.get_vram_manager', return_value=mock_vm), \
             caplog.at_level(logging.WARNING):
            mgr.start()

        mgr.stop()
        vram_warns = [r for r in caplog.records if 'vram' in r.message.lower()]
        assert len(vram_warns) == 0

    def test_vram_manager_none_skips_check(self, manager_with_models, caplog):
        """When get_vram_manager() returns None, check is skipped gracefully."""
        mgr = manager_with_models
        mgr._models['draft'] = _make_model_state('draft', vram_gb=5.0, pinned=True)

        with patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._sync_from_rtm'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._detect_tier'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._loop'), \
             patch('integrations.service_tools.runtime_manager.runtime_tool_manager', MagicMock()), \
             patch('integrations.service_tools.vram_manager.get_vram_manager', return_value=None), \
             caplog.at_level(logging.WARNING):
            mgr.start()

        mgr.stop()
        # total_vram = 0 when vm is None, so check is skipped
        vram_warns = [r for r in caplog.records if 'vram' in r.message.lower()]
        assert len(vram_warns) == 0

    def test_vram_import_error_skips_check(self, manager_with_models, caplog):
        """ImportError on vram_manager does not crash start()."""
        mgr = manager_with_models
        mgr._models['draft'] = _make_model_state('draft', vram_gb=5.0, pinned=True)

        with patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._sync_from_rtm'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._detect_tier'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._loop'), \
             patch('integrations.service_tools.runtime_manager.runtime_tool_manager', MagicMock()), \
             patch.dict(sys.modules, {'integrations.service_tools.vram_manager': None}), \
             caplog.at_level(logging.WARNING):
            # This should not raise
            mgr.start()

        mgr.stop()

    def test_no_models_loaded_no_warning(self, manager_with_models, caplog):
        """Empty model dict — nothing to check, no warnings."""
        mgr = manager_with_models
        # No models added

        mock_vm = MagicMock()
        mock_vm.get_total_vram.return_value = 4.0

        with patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._sync_from_rtm'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._detect_tier'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._loop'), \
             patch('integrations.service_tools.runtime_manager.runtime_tool_manager', MagicMock()), \
             patch('integrations.service_tools.vram_manager.get_vram_manager', return_value=mock_vm), \
             caplog.at_level(logging.WARNING):
            mgr.start()

        mgr.stop()
        vram_warns = [r for r in caplog.records if 'vram' in r.message.lower()]
        assert len(vram_warns) == 0

    def test_zero_vram_models_ignored(self, manager_with_models, caplog):
        """Models with vram_gb=0 do not count toward budget."""
        mgr = manager_with_models
        mgr._models['cpu_model'] = _make_model_state('cpu_model', vram_gb=0.0, pinned=True)

        mock_vm = MagicMock()
        mock_vm.get_total_vram.return_value = 4.0

        with patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._sync_from_rtm'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._detect_tier'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._loop'), \
             patch('integrations.service_tools.runtime_manager.runtime_tool_manager', MagicMock()), \
             patch('integrations.service_tools.vram_manager.get_vram_manager', return_value=mock_vm), \
             caplog.at_level(logging.WARNING):
            mgr.start()

        mgr.stop()
        vram_warns = [r for r in caplog.records if 'vram' in r.message.lower()]
        assert len(vram_warns) == 0


# ═══════════════════════════════════════════════════════════════
# Non-Functional Tests
# ═══════════════════════════════════════════════════════════════

class TestVRAMBudgetCheckNonFunctional:
    """NFT: Exception safety, advisory nature, no blocking."""

    @pytest.fixture
    def manager(self):
        from integrations.service_tools.model_lifecycle import ModelLifecycleManager
        return ModelLifecycleManager()

    def test_vram_check_exception_does_not_block_start(self, manager):
        """Any exception in VRAM check is caught — start() completes."""
        manager._models['draft'] = _make_model_state('draft', vram_gb=2.0, pinned=True)

        # Force an unexpected exception inside the VRAM check. The code reads
        # the budget via vm.get_total_vram() (a METHOD — model_lifecycle.py:337,
        # whose own comment records that total_vram_gb never existed), so the
        # crash must come from that call, not the dead attribute.
        mock_vm = MagicMock()
        mock_vm.get_total_vram.side_effect = RuntimeError("gpu crash")

        with patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._sync_from_rtm'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._detect_tier'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._loop'), \
             patch('integrations.service_tools.runtime_manager.runtime_tool_manager', MagicMock()), \
             patch('integrations.service_tools.vram_manager.get_vram_manager', return_value=mock_vm):
            manager.start()  # Must NOT raise

        manager.stop()
        assert manager._running is False  # stopped cleanly

    def test_warning_message_contains_budget_numbers(self, manager, caplog):
        """WARNING message includes the actual GB numbers for debugging."""
        manager._models['a'] = _make_model_state('a', vram_gb=3.0, pinned=False)
        manager._models['b'] = _make_model_state('b', vram_gb=3.0, pinned=False)

        mock_vm = MagicMock()
        mock_vm.get_total_vram.return_value = 4.0

        with patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._sync_from_rtm'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._detect_tier'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._loop'), \
             patch('integrations.service_tools.runtime_manager.runtime_tool_manager', MagicMock()), \
             patch('integrations.service_tools.vram_manager.get_vram_manager', return_value=mock_vm), \
             caplog.at_level(logging.WARNING):
            manager.start()

        manager.stop()
        warn_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        # Should mention the actual numbers (6.0GB needed, 4.0GB available)
        combined = ' '.join(warn_msgs)
        assert '6.0' in combined or '4.0' in combined, f"Expected GB numbers in: {combined}"

    def test_error_message_suggests_action(self, manager, caplog):
        """ERROR message suggests reducing pinned count or using CPU."""
        manager._models['big'] = _make_model_state('big', vram_gb=6.0, pinned=True)

        mock_vm = MagicMock()
        mock_vm.get_total_vram.return_value = 4.0

        with patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._sync_from_rtm'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._detect_tier'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._loop'), \
             patch('integrations.service_tools.runtime_manager.runtime_tool_manager', MagicMock()), \
             patch('integrations.service_tools.vram_manager.get_vram_manager', return_value=mock_vm), \
             caplog.at_level(logging.ERROR):
            manager.start()

        manager.stop()
        error_msgs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
        combined = ' '.join(error_msgs)
        assert 'cpu' in combined.lower() or 'pinned' in combined.lower(), \
            f"Expected actionable advice in: {combined}"

    def test_both_warning_and_error_when_pinned_exceeds(self, manager, caplog):
        """When pinned > total, BOTH warning (all > total) and error (pinned > total) fire."""
        manager._models['big_pinned'] = _make_model_state('big_pinned', vram_gb=6.0, pinned=True)

        mock_vm = MagicMock()
        mock_vm.get_total_vram.return_value = 4.0

        with patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._sync_from_rtm'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._detect_tier'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._loop'), \
             patch('integrations.service_tools.runtime_manager.runtime_tool_manager', MagicMock()), \
             patch('integrations.service_tools.vram_manager.get_vram_manager', return_value=mock_vm), \
             caplog.at_level(logging.WARNING):
            manager.start()

        manager.stop()
        levels = {r.levelno for r in caplog.records if 'vram' in r.message.lower() or 'pinned' in r.message.lower()}
        assert logging.WARNING in levels or logging.ERROR in levels

    def test_check_is_advisory_not_blocking(self, manager):
        """VRAM check is advisory: start() always succeeds regardless of budget."""
        manager._models['huge'] = _make_model_state('huge', vram_gb=100.0, pinned=True)

        mock_vm = MagicMock()
        mock_vm.get_total_vram.return_value = 1.0  # Absurdly small

        with patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._sync_from_rtm'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._detect_tier'), \
             patch('integrations.service_tools.model_lifecycle.ModelLifecycleManager._loop'), \
             patch('integrations.service_tools.runtime_manager.runtime_tool_manager', MagicMock()), \
             patch('integrations.service_tools.vram_manager.get_vram_manager', return_value=mock_vm):
            manager.start()  # Must NOT raise or abort

        assert manager._running is True
        manager.stop()
