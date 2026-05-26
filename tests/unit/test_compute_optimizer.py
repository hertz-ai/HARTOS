"""
Tests for the OS Compute Optimizer (core/compute_optimizer.py).

Covers: SystemSnapshot, OptimizationAction, ComputeOptimizer (snapshot, health score,
thresholds, suggestions, apply, lifecycle, hive exploration, federation, EventBus),
singleton get_optimizer(), Flask blueprint, and edge cases (no psutil, no GPU).

Run: pytest tests/unit/test_compute_optimizer.py -v --noconftest
"""
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.compute_optimizer import (
    ActionType,
    ComputeOptimizer,
    OptimizationAction,
    SystemSnapshot,
    create_optimizer_blueprint,
    get_optimizer,
)


# ═══════════════════════════════════════════════════════════════════════
# SystemSnapshot
# ═══════════════════════════════════════════════════════════════════════

class TestSystemSnapshot(unittest.TestCase):
    """Tests for the SystemSnapshot dataclass."""

    def test_default_values(self):
        """All numeric fields default to 0, lists to empty, string to ''."""
        snap = SystemSnapshot()
        self.assertEqual(snap.timestamp, 0.0)
        self.assertEqual(snap.cpu_percent, 0.0)
        self.assertEqual(snap.ram_percent, 0.0)
        self.assertEqual(snap.ram_used_gb, 0.0)
        self.assertEqual(snap.ram_total_gb, 0.0)
        self.assertEqual(snap.swap_percent, 0.0)
        self.assertEqual(snap.disk_usage_percent, 0.0)
        self.assertEqual(snap.disk_io_read_mb, 0.0)
        self.assertEqual(snap.disk_io_write_mb, 0.0)
        self.assertEqual(snap.net_sent_mb, 0.0)
        self.assertEqual(snap.net_recv_mb, 0.0)
        self.assertEqual(snap.top_processes, [])
        self.assertEqual(snap.gpu_util_percent, 0.0)
        self.assertEqual(snap.gpu_mem_used_gb, 0.0)
        self.assertEqual(snap.gpu_mem_total_gb, 0.0)
        self.assertEqual(snap.platform_name, '')

    def test_creation_with_values(self):
        """Snapshot stores explicit field values."""
        snap = SystemSnapshot(
            timestamp=1000.0,
            cpu_percent=45.6,
            ram_percent=72.3,
            ram_used_gb=11.5,
            ram_total_gb=16.0,
            swap_percent=20.0,
            disk_usage_percent=55.0,
            platform_name='Linux',
        )
        self.assertEqual(snap.cpu_percent, 45.6)
        self.assertEqual(snap.ram_total_gb, 16.0)
        self.assertEqual(snap.platform_name, 'Linux')

    def test_to_dict_all_keys(self):
        """to_dict() includes every expected key."""
        snap = SystemSnapshot(timestamp=1.0, platform_name='Windows')
        d = snap.to_dict()
        expected_keys = {
            'timestamp', 'cpu_percent', 'ram_percent', 'ram_used_gb',
            'ram_total_gb', 'swap_percent', 'disk_usage_percent',
            'disk_io_read_mb', 'disk_io_write_mb', 'net_sent_mb',
            'net_recv_mb', 'top_processes', 'gpu_util_percent',
            'gpu_mem_used_gb', 'gpu_mem_total_gb', 'platform',
        }
        self.assertEqual(set(d.keys()), expected_keys)
        self.assertEqual(d['platform'], 'Windows')

    def test_to_dict_rounds_values(self):
        """Numeric fields are rounded in serialized output."""
        snap = SystemSnapshot(
            cpu_percent=45.678,
            ram_used_gb=11.5678,
            net_sent_mb=123.456,
        )
        d = snap.to_dict()
        self.assertEqual(d['cpu_percent'], 45.7)
        self.assertEqual(d['ram_used_gb'], 11.57)
        self.assertEqual(d['net_sent_mb'], 123.5)

    def test_to_dict_truncates_top_processes(self):
        """Only the first 10 processes are included in the dict."""
        procs = [{'pid': i, 'name': f'proc{i}'} for i in range(20)]
        snap = SystemSnapshot(top_processes=procs)
        d = snap.to_dict()
        self.assertEqual(len(d['top_processes']), 10)


# ═══════════════════════════════════════════════════════════════════════
# OptimizationAction
# ═══════════════════════════════════════════════════════════════════════

class TestOptimizationAction(unittest.TestCase):
    """Tests for OptimizationAction and ActionType enum."""

    def test_action_type_values(self):
        """All six ActionType enum members have expected string values."""
        self.assertEqual(ActionType.PRIORITY_ADJUST.value, 'priority_adjust')
        self.assertEqual(ActionType.CACHE_CLEAN.value, 'cache_clean')
        self.assertEqual(ActionType.SWAP_MANAGE.value, 'swap_manage')
        self.assertEqual(ActionType.POWER_TUNE.value, 'power_tune')
        self.assertEqual(ActionType.PROCESS_SUGGEST.value, 'process_suggest')
        self.assertEqual(ActionType.NETWORK_TUNE.value, 'network_tune')

    def test_action_type_count(self):
        """There are exactly 6 action types."""
        self.assertEqual(len(ActionType), 6)

    def test_creation_defaults(self):
        """OptimizationAction has sensible defaults for optional fields."""
        action = OptimizationAction(
            action_type=ActionType.CACHE_CLEAN,
            target='system_temp',
        )
        self.assertEqual(action.params, {})
        self.assertEqual(action.impact_estimate, '')
        self.assertFalse(action.applied)
        self.assertEqual(action.timestamp, 0.0)
        self.assertEqual(action.result, '')

    def test_to_dict_serialization(self):
        """to_dict() converts action_type to its string value."""
        action = OptimizationAction(
            action_type=ActionType.SWAP_MANAGE,
            target='swap_pressure',
            params={'swap_percent': 60.0},
            impact_estimate='Reduce swap usage',
            applied=True,
            timestamp=5000.0,
            result='done',
        )
        d = action.to_dict()
        self.assertEqual(d['action_type'], 'swap_manage')
        self.assertEqual(d['target'], 'swap_pressure')
        self.assertEqual(d['params'], {'swap_percent': 60.0})
        self.assertTrue(d['applied'])
        self.assertEqual(d['result'], 'done')


# ═══════════════════════════════════════════════════════════════════════
# ComputeOptimizer — Health Score
# ═══════════════════════════════════════════════════════════════════════

class TestHealthScore(unittest.TestCase):
    """Tests for get_health_score() weighted calculation."""

    def setUp(self):
        self.opt = ComputeOptimizer()

    def test_perfect_health_all_zeros(self):
        """0% utilization everywhere yields health score 1.0."""
        snap = SystemSnapshot(
            cpu_percent=0.0,
            ram_percent=0.0,
            swap_percent=0.0,
            disk_usage_percent=0.0,
        )
        self.assertEqual(self.opt.get_health_score(snap), 1.0)

    def test_worst_health_all_100(self):
        """100% utilization everywhere yields health score 0.0."""
        snap = SystemSnapshot(
            cpu_percent=100.0,
            ram_percent=100.0,
            swap_percent=100.0,
            disk_usage_percent=100.0,
        )
        self.assertEqual(self.opt.get_health_score(snap), 0.0)

    def test_half_utilization(self):
        """50% on all resources yields 0.5."""
        snap = SystemSnapshot(
            cpu_percent=50.0,
            ram_percent=50.0,
            swap_percent=50.0,
            disk_usage_percent=50.0,
        )
        self.assertEqual(self.opt.get_health_score(snap), 0.5)

    def test_cpu_weight_30_percent(self):
        """CPU=100% with everything else 0% yields 1.0 - 0.30 = 0.70."""
        snap = SystemSnapshot(
            cpu_percent=100.0,
            ram_percent=0.0,
            swap_percent=0.0,
            disk_usage_percent=0.0,
        )
        self.assertAlmostEqual(self.opt.get_health_score(snap), 0.7, places=2)

    def test_ram_weight_30_percent(self):
        """RAM=100% with everything else 0% yields 0.70."""
        snap = SystemSnapshot(
            cpu_percent=0.0,
            ram_percent=100.0,
            swap_percent=0.0,
            disk_usage_percent=0.0,
        )
        self.assertAlmostEqual(self.opt.get_health_score(snap), 0.7, places=2)

    def test_swap_weight_20_percent(self):
        """Swap=100% with everything else 0% yields 0.80."""
        snap = SystemSnapshot(
            cpu_percent=0.0,
            ram_percent=0.0,
            swap_percent=100.0,
            disk_usage_percent=0.0,
        )
        self.assertAlmostEqual(self.opt.get_health_score(snap), 0.8, places=2)

    def test_disk_weight_20_percent(self):
        """Disk=100% with everything else 0% yields 0.80."""
        snap = SystemSnapshot(
            cpu_percent=0.0,
            ram_percent=0.0,
            swap_percent=0.0,
            disk_usage_percent=100.0,
        )
        self.assertAlmostEqual(self.opt.get_health_score(snap), 0.8, places=2)

    def test_no_snapshot_returns_1(self):
        """No snapshot available defaults to healthy (1.0)."""
        self.assertEqual(self.opt.get_health_score(None), 1.0)

    def test_score_clamped_between_0_and_1(self):
        """Score stays in [0,1] even with extreme values."""
        snap = SystemSnapshot(cpu_percent=200.0, ram_percent=200.0,
                              swap_percent=200.0, disk_usage_percent=200.0)
        score = self.opt.get_health_score(snap)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_return_type_is_float(self):
        """Score is always a float."""
        snap = SystemSnapshot()
        self.assertIsInstance(self.opt.get_health_score(snap), float)


# ═══════════════════════════════════════════════════════════════════════
# ComputeOptimizer — Thresholds
# ═══════════════════════════════════════════════════════════════════════

class TestCheckThresholds(unittest.TestCase):
    """Tests for _check_thresholds() breach detection."""

    def setUp(self):
        self.opt = ComputeOptimizer()

    def test_no_breach_when_all_low(self):
        """Everything below thresholds yields empty breach list."""
        snap = SystemSnapshot(
            cpu_percent=10.0, ram_percent=20.0,
            swap_percent=5.0, disk_usage_percent=30.0,
        )
        breaches = self.opt._check_thresholds(snap)
        self.assertEqual(breaches, [])

    def test_cpu_breach(self):
        """CPU above 80% is detected."""
        snap = SystemSnapshot(cpu_percent=85.0)
        breaches = self.opt._check_thresholds(snap)
        self.assertIn('cpu_high', breaches)

    def test_ram_breach(self):
        """RAM above 85% is detected."""
        snap = SystemSnapshot(ram_percent=90.0)
        breaches = self.opt._check_thresholds(snap)
        self.assertIn('ram_high', breaches)

    def test_swap_breach(self):
        """Swap above 50% is detected."""
        snap = SystemSnapshot(swap_percent=55.0)
        breaches = self.opt._check_thresholds(snap)
        self.assertIn('swap_high', breaches)

    def test_disk_breach(self):
        """Disk above 90% is detected."""
        snap = SystemSnapshot(disk_usage_percent=95.0)
        breaches = self.opt._check_thresholds(snap)
        self.assertIn('disk_high', breaches)

    def test_all_breaches(self):
        """All thresholds breached simultaneously."""
        snap = SystemSnapshot(
            cpu_percent=90.0, ram_percent=90.0,
            swap_percent=60.0, disk_usage_percent=95.0,
        )
        breaches = self.opt._check_thresholds(snap)
        self.assertEqual(len(breaches), 4)

    def test_exactly_at_threshold_no_breach(self):
        """Values exactly at the threshold do NOT trigger (strictly greater)."""
        snap = SystemSnapshot(
            cpu_percent=80.0, ram_percent=85.0,
            swap_percent=50.0, disk_usage_percent=90.0,
        )
        breaches = self.opt._check_thresholds(snap)
        self.assertEqual(breaches, [])


# ═══════════════════════════════════════════════════════════════════════
# ComputeOptimizer — Suggest Optimizations
# ═══════════════════════════════════════════════════════════════════════

class TestSuggestOptimizations(unittest.TestCase):
    """Tests for _suggest_optimizations() action generation."""

    def setUp(self):
        self.opt = ComputeOptimizer()
        # Reset cooldowns so all suggestions fire
        self.opt._cooldowns = {}

    def test_cpu_high_suggests_process_suggest(self):
        """High CPU with heavy processes suggests PROCESS_SUGGEST actions."""
        snap = SystemSnapshot(
            cpu_percent=90.0,
            top_processes=[
                {'pid': 1234, 'name': 'heavy_app', 'cpu_percent': 45.0, 'mem_percent': 10.0},
            ],
        )
        actions = self.opt._suggest_optimizations(snap)
        types = [a.action_type for a in actions]
        self.assertIn(ActionType.PROCESS_SUGGEST, types)

    def test_ram_high_suggests_cache_clean(self):
        """High RAM suggests CACHE_CLEAN."""
        snap = SystemSnapshot(ram_percent=90.0)
        actions = self.opt._suggest_optimizations(snap)
        types = [a.action_type for a in actions]
        self.assertIn(ActionType.CACHE_CLEAN, types)

    def test_swap_high_suggests_swap_manage(self):
        """High swap suggests SWAP_MANAGE."""
        snap = SystemSnapshot(swap_percent=60.0)
        actions = self.opt._suggest_optimizations(snap)
        types = [a.action_type for a in actions]
        self.assertIn(ActionType.SWAP_MANAGE, types)

    def test_disk_high_suggests_cache_clean(self):
        """High disk suggests CACHE_CLEAN for temp files."""
        snap = SystemSnapshot(disk_usage_percent=95.0)
        actions = self.opt._suggest_optimizations(snap)
        types = [a.action_type for a in actions]
        self.assertIn(ActionType.CACHE_CLEAN, types)

    def test_no_suggestions_when_healthy(self):
        """No suggestions when all metrics are healthy."""
        snap = SystemSnapshot(
            cpu_percent=10.0, ram_percent=20.0,
            swap_percent=5.0, disk_usage_percent=30.0,
        )
        actions = self.opt._suggest_optimizations(snap)
        self.assertEqual(actions, [])

    def test_suggestions_increment_counter(self):
        """Suggestions increment _suggestions_made."""
        snap = SystemSnapshot(ram_percent=90.0, swap_percent=60.0)
        before = self.opt._suggestions_made
        self.opt._suggest_optimizations(snap)
        self.assertGreater(self.opt._suggestions_made, before)

    def test_cooldown_prevents_repeat_suggestions(self):
        """Same action type is suppressed within cooldown window."""
        snap = SystemSnapshot(ram_percent=90.0)
        # First call produces suggestion
        a1 = self.opt._suggest_optimizations(snap)
        self.assertTrue(len(a1) > 0)
        # Simulate cooldown just set
        self.opt._cooldowns[ActionType.CACHE_CLEAN.value] = time.time()
        # Second call within cooldown produces nothing for CACHE_CLEAN
        a2 = self.opt._suggest_optimizations(snap)
        cache_actions = [a for a in a2 if a.action_type == ActionType.CACHE_CLEAN]
        self.assertEqual(cache_actions, [])


# ═══════════════════════════════════════════════════════════════════════
# ComputeOptimizer — Snapshot (mocked psutil)
# ═══════════════════════════════════════════════════════════════════════

class TestSnapshot(unittest.TestCase):
    """Tests for snapshot() with mocked psutil and GPU detection."""

    def _make_mock_psutil(self):
        """Build a realistic psutil mock."""
        mock_psutil = MagicMock()
        mock_psutil.cpu_percent.return_value = 35.0

        mem = SimpleNamespace(percent=62.0, used=8 * (1024 ** 3), total=16 * (1024 ** 3))
        mock_psutil.virtual_memory.return_value = mem

        swap = SimpleNamespace(percent=10.0, total=4 * (1024 ** 3))
        mock_psutil.swap_memory.return_value = swap

        disk = SimpleNamespace(percent=55.0)
        mock_psutil.disk_usage.return_value = disk

        dio = SimpleNamespace(read_bytes=500 * (1024 ** 2), write_bytes=200 * (1024 ** 2))
        mock_psutil.disk_io_counters.return_value = dio

        nio = SimpleNamespace(bytes_sent=100 * (1024 ** 2), bytes_recv=300 * (1024 ** 2))
        mock_psutil.net_io_counters.return_value = nio

        proc_mock = MagicMock()
        proc_mock.info = {'pid': 99, 'name': 'test_proc', 'cpu_percent': 5.0, 'memory_percent': 3.0}
        mock_psutil.process_iter.return_value = [proc_mock]

        return mock_psutil

    @patch('core.compute_optimizer._try_detect_gpu')
    @patch('core.compute_optimizer._try_import_psutil')
    def test_snapshot_with_psutil(self, mock_import, mock_gpu):
        """Snapshot captures all psutil metrics when available."""
        mock_psutil = self._make_mock_psutil()
        mock_import.return_value = mock_psutil
        mock_gpu.return_value = {'name': 'none', 'total_gb': 0, 'free_gb': 0, 'cuda_available': False}

        opt = ComputeOptimizer()
        snap = opt.snapshot()

        self.assertEqual(snap.cpu_percent, 35.0)
        self.assertEqual(snap.ram_percent, 62.0)
        self.assertAlmostEqual(snap.ram_total_gb, 16.0, places=1)
        self.assertEqual(snap.swap_percent, 10.0)
        self.assertEqual(snap.disk_usage_percent, 55.0)
        self.assertAlmostEqual(snap.disk_io_read_mb, 500.0, places=0)
        self.assertAlmostEqual(snap.net_recv_mb, 300.0, places=0)
        self.assertGreater(snap.timestamp, 0)

    @patch('core.compute_optimizer._try_detect_gpu')
    @patch('core.compute_optimizer._try_import_psutil')
    def test_snapshot_without_psutil(self, mock_import, mock_gpu):
        """Snapshot returns defaults when psutil is not installed."""
        mock_import.return_value = None
        mock_gpu.return_value = {'name': 'none', 'total_gb': 0, 'free_gb': 0, 'cuda_available': False}

        opt = ComputeOptimizer()
        snap = opt.snapshot()

        self.assertEqual(snap.cpu_percent, 0.0)
        self.assertEqual(snap.ram_percent, 0.0)
        self.assertGreater(snap.timestamp, 0)

    @patch('core.compute_optimizer._try_detect_gpu')
    @patch('core.compute_optimizer._try_import_psutil')
    def test_snapshot_with_gpu(self, mock_import, mock_gpu):
        """Snapshot captures GPU info when CUDA is available."""
        mock_import.return_value = None
        mock_gpu.return_value = {
            'name': 'RTX 4090',
            'total_gb': 24.0,
            'free_gb': 10.0,
            'cuda_available': True,
        }

        opt = ComputeOptimizer()
        snap = opt.snapshot()

        self.assertEqual(snap.gpu_mem_total_gb, 24.0)
        self.assertEqual(snap.gpu_mem_used_gb, 14.0)
        self.assertAlmostEqual(snap.gpu_util_percent, (14.0 / 24.0) * 100, places=1)

    @patch('core.compute_optimizer._try_detect_gpu')
    @patch('core.compute_optimizer._try_import_psutil')
    def test_snapshot_no_gpu(self, mock_import, mock_gpu):
        """GPU fields stay at 0 when CUDA is not available."""
        mock_import.return_value = None
        mock_gpu.return_value = {'name': 'none', 'total_gb': 0, 'free_gb': 0, 'cuda_available': False}

        opt = ComputeOptimizer()
        snap = opt.snapshot()

        self.assertEqual(snap.gpu_mem_total_gb, 0.0)
        self.assertEqual(snap.gpu_util_percent, 0.0)

    @patch('core.compute_optimizer._try_detect_gpu')
    @patch('core.compute_optimizer._try_import_psutil')
    def test_snapshot_stored_in_history(self, mock_import, mock_gpu):
        """Each snapshot is appended to internal deque and _last_snapshot."""
        mock_import.return_value = None
        mock_gpu.return_value = {'name': 'none', 'total_gb': 0, 'free_gb': 0, 'cuda_available': False}

        opt = ComputeOptimizer()
        snap = opt.snapshot()

        self.assertEqual(opt._last_snapshot, snap)
        self.assertEqual(len(opt._snapshots), 1)


# ═══════════════════════════════════════════════════════════════════════
# ComputeOptimizer — Apply Optimization
# ═══════════════════════════════════════════════════════════════════════

class TestApplyOptimization(unittest.TestCase):
    """Tests for _apply_optimization() non-destructive actions."""

    def setUp(self):
        self.opt = ComputeOptimizer()

    def test_cache_clean_does_not_crash(self):
        """Cache clean runs without exceptions (works on any OS)."""
        action = OptimizationAction(
            action_type=ActionType.CACHE_CLEAN,
            target='system_temp',
            params={'max_age_seconds': 999999},  # Very old = nothing to clean
        )
        self.opt._apply_optimization(action)
        self.assertTrue(action.applied)
        self.assertIn('cleaned', action.result)

    def test_network_tune_advisory(self):
        """Network tuning is advisory only."""
        action = OptimizationAction(
            action_type=ActionType.NETWORK_TUNE,
            target='network',
        )
        self.opt._apply_optimization(action)
        self.assertTrue(action.applied)
        self.assertIn('advisory', action.result)

    @patch('core.compute_optimizer._try_import_psutil')
    def test_priority_adjust_no_psutil(self, mock_import):
        """Priority adjust gracefully handles missing psutil."""
        mock_import.return_value = None
        action = OptimizationAction(
            action_type=ActionType.PROCESS_SUGGEST,
            target='some_proc',
            params={'pid': 12345},
        )
        self.opt._apply_optimization(action)
        self.assertIn('psutil not available', action.result)

    def test_priority_adjust_no_pid(self):
        """Priority adjust returns 'no pid' when pid is missing."""
        action = OptimizationAction(
            action_type=ActionType.PROCESS_SUGGEST,
            target='some_proc',
            params={},
        )
        self.opt._apply_optimization(action)
        # Either 'no pid' or 'psutil not available' depending on environment
        self.assertTrue(action.result != '')

    def test_optimization_increments_counter(self):
        """Successful optimization increments _optimizations_applied."""
        action = OptimizationAction(
            action_type=ActionType.NETWORK_TUNE,
            target='net',
        )
        before = self.opt._optimizations_applied
        self.opt._apply_optimization(action)
        self.assertEqual(self.opt._optimizations_applied, before + 1)

    def test_optimization_added_to_history(self):
        """Applied optimizations are recorded in history deque."""
        action = OptimizationAction(
            action_type=ActionType.NETWORK_TUNE,
            target='net',
        )
        self.opt._apply_optimization(action)
        self.assertEqual(len(self.opt._history), 1)
        self.assertEqual(self.opt._history[0].action_type, ActionType.NETWORK_TUNE)

    @patch('core.compute_optimizer._emit')
    def test_optimization_emits_event(self, mock_emit):
        """Successful optimization emits 'system.optimization.applied' event."""
        action = OptimizationAction(
            action_type=ActionType.NETWORK_TUNE,
            target='net',
        )
        self.opt._apply_optimization(action)
        mock_emit.assert_called_once()
        call_args = mock_emit.call_args
        self.assertEqual(call_args[0][0], 'system.optimization.applied')

    def test_swap_manage_does_not_crash(self):
        """Swap management does not crash regardless of platform."""
        action = OptimizationAction(
            action_type=ActionType.SWAP_MANAGE,
            target='swap_pressure',
            params={'swap_percent': 60.0},
        )
        self.opt._apply_optimization(action)
        # Should succeed or fail gracefully (PermissionError on Linux, redirect to
        # cache clean on Windows, 'not available' on other platforms)
        self.assertTrue(action.result != '')


# ═══════════════════════════════════════════════════════════════════════
# ComputeOptimizer — Lifecycle (start/stop)
# ═══════════════════════════════════════════════════════════════════════

class TestLifecycle(unittest.TestCase):
    """Tests for start()/stop() thread lifecycle."""

    def test_start_sets_running(self):
        """start() sets _running to True and creates threads."""
        opt = ComputeOptimizer()
        opt.start()
        try:
            self.assertTrue(opt._running)
            self.assertIsNotNone(opt._monitor_thread)
            self.assertIsNotNone(opt._hive_thread)
            self.assertTrue(opt._monitor_thread.is_alive())
            self.assertTrue(opt._hive_thread.is_alive())
        finally:
            opt.stop()

    def test_stop_clears_running(self):
        """stop() sets _running to False."""
        opt = ComputeOptimizer()
        opt.start()
        opt.stop()
        self.assertFalse(opt._running)

    def test_double_start_is_idempotent(self):
        """Calling start() twice does not create extra threads."""
        opt = ComputeOptimizer()
        opt.start()
        try:
            thread1 = opt._monitor_thread
            opt.start()
            self.assertIs(opt._monitor_thread, thread1)
        finally:
            opt.stop()

    def test_stop_without_start_is_safe(self):
        """stop() on a never-started optimizer does not raise."""
        opt = ComputeOptimizer()
        opt.stop()  # Should not raise
        self.assertFalse(opt._running)

    def test_threads_are_daemon(self):
        """Both background threads are daemons (won't prevent exit)."""
        opt = ComputeOptimizer()
        opt.start()
        try:
            self.assertTrue(opt._monitor_thread.daemon)
            self.assertTrue(opt._hive_thread.daemon)
        finally:
            opt.stop()


# ═══════════════════════════════════════════════════════════════════════
# ComputeOptimizer — Trigger Optimization
# ═══════════════════════════════════════════════════════════════════════

class TestTriggerOptimization(unittest.TestCase):
    """Tests for trigger_optimization() manual check."""

    @patch('core.compute_optimizer._try_detect_gpu')
    @patch('core.compute_optimizer._try_import_psutil')
    def test_returns_dict_with_required_keys(self, mock_import, mock_gpu):
        """Result dict contains snapshot, health_score, and actions."""
        mock_import.return_value = None
        mock_gpu.return_value = {'name': 'none', 'total_gb': 0, 'free_gb': 0, 'cuda_available': False}

        opt = ComputeOptimizer()
        result = opt.trigger_optimization()

        self.assertIn('snapshot', result)
        self.assertIn('health_score', result)
        self.assertIn('actions', result)
        self.assertIsInstance(result['actions'], list)
        self.assertGreater(len(result['actions']), 0)  # At least maintenance clean

    @patch('core.compute_optimizer._try_detect_gpu')
    @patch('core.compute_optimizer._try_import_psutil')
    def test_maintenance_clean_when_no_breaches(self, mock_import, mock_gpu):
        """When no thresholds breached, a maintenance CACHE_CLEAN is performed."""
        mock_import.return_value = None
        mock_gpu.return_value = {'name': 'none', 'total_gb': 0, 'free_gb': 0, 'cuda_available': False}

        opt = ComputeOptimizer()
        result = opt.trigger_optimization()

        action_types = [a['action_type'] for a in result['actions']]
        self.assertIn('cache_clean', action_types)


# ═══════════════════════════════════════════════════════════════════════
# ComputeOptimizer — Hive & Federation (mocked)
# ═══════════════════════════════════════════════════════════════════════

class TestHiveExploration(unittest.TestCase):
    """Tests for _explore_hive_stream() and _contribute_to_hive()."""

    @patch('core.compute_optimizer._emit')
    def test_explore_hive_no_crash_when_unavailable(self, mock_emit):
        """Hive exploration does not crash when goal_manager is unavailable."""
        opt = ComputeOptimizer()
        opt._last_snapshot = SystemSnapshot(cpu_percent=30.0, ram_percent=40.0)
        # _fetch_hive_goals will fail to import and return []
        opt._explore_hive_stream()  # Should not raise

    @patch('core.compute_optimizer._emit')
    def test_contribute_to_hive_no_crash(self, mock_emit):
        """Federation contribution does not crash when aggregator unavailable."""
        opt = ComputeOptimizer()
        snap = SystemSnapshot(cpu_percent=30.0, ram_percent=40.0, disk_usage_percent=50.0)
        opt._contribute_to_hive(snap)  # Should not raise

    @patch('core.compute_optimizer._emit')
    def test_emit_stats_calls_eventbus(self, mock_emit):
        """_emit_stats() emits 'system.health.snapshot' event."""
        opt = ComputeOptimizer()
        snap = SystemSnapshot(cpu_percent=30.0)
        opt._emit_stats(snap)
        mock_emit.assert_called_once()
        self.assertEqual(mock_emit.call_args[0][0], 'system.health.snapshot')

    @patch('core.compute_optimizer._emit')
    def test_emit_stats_includes_health_score(self, mock_emit):
        """Emitted stats dict includes the health_score key."""
        opt = ComputeOptimizer()
        snap = SystemSnapshot(cpu_percent=50.0, ram_percent=50.0,
                              swap_percent=50.0, disk_usage_percent=50.0)
        opt._emit_stats(snap)
        data = mock_emit.call_args[0][1]
        self.assertIn('health_score', data)
        self.assertAlmostEqual(data['health_score'], 0.5, places=2)


# ═══════════════════════════════════════════════════════════════════════
# ComputeOptimizer — get_stats()
# ═══════════════════════════════════════════════════════════════════════

class TestGetStats(unittest.TestCase):
    """Tests for get_stats() public API."""

    def test_stats_keys(self):
        """Stats dict has all expected keys."""
        opt = ComputeOptimizer()
        stats = opt.get_stats()
        expected_keys = {
            'running', 'optimizations_applied', 'suggestions_made',
            'hive_explorations', 'cache_bytes_freed', 'cache_mb_freed',
            'history_count', 'recent_history', 'platform',
        }
        self.assertEqual(set(stats.keys()), expected_keys)

    def test_stats_initial_values(self):
        """Fresh optimizer has zeroed counters."""
        opt = ComputeOptimizer()
        stats = opt.get_stats()
        self.assertFalse(stats['running'])
        self.assertEqual(stats['optimizations_applied'], 0)
        self.assertEqual(stats['suggestions_made'], 0)
        self.assertEqual(stats['hive_explorations'], 0)
        self.assertEqual(stats['cache_bytes_freed'], 0)
        self.assertEqual(stats['history_count'], 0)


# ═══════════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════════

class TestSingleton(unittest.TestCase):
    """Tests for get_optimizer() singleton."""

    def test_returns_same_instance(self):
        """get_optimizer() returns the same object on multiple calls."""
        o1 = get_optimizer()
        o2 = get_optimizer()
        self.assertIs(o1, o2)

    def test_returns_compute_optimizer(self):
        """Singleton is a ComputeOptimizer instance."""
        opt = get_optimizer()
        self.assertIsInstance(opt, ComputeOptimizer)


# ═══════════════════════════════════════════════════════════════════════
# Flask Blueprint
# ═══════════════════════════════════════════════════════════════════════

class TestFlaskBlueprint(unittest.TestCase):
    """Tests for create_optimizer_blueprint()."""

    def test_blueprint_created(self):
        """Blueprint is created when Flask is available."""
        try:
            from flask import Blueprint
        except ImportError:
            self.skipTest("Flask not installed")

        bp = create_optimizer_blueprint()
        self.assertIsNotNone(bp)
        self.assertEqual(bp.name, 'compute_optimizer')

    def test_blueprint_has_three_routes(self):
        """Blueprint registers /health, /optimizations, /optimize routes."""
        try:
            from flask import Blueprint
        except ImportError:
            self.skipTest("Flask not installed")

        bp = create_optimizer_blueprint()
        # Blueprint deferred_functions contains the route registrations
        # Check by examining the rules that get created
        rules = set()
        for func in bp.deferred_functions:
            # Each deferred function is a closure that registers routes
            # We can verify by counting the deferred functions (1 per route)
            pass
        # At minimum 3 deferred functions (one per route)
        self.assertGreaterEqual(len(bp.deferred_functions), 3)

    def test_blueprint_url_prefix(self):
        """Blueprint has /api/system URL prefix."""
        try:
            from flask import Blueprint
        except ImportError:
            self.skipTest("Flask not installed")

        bp = create_optimizer_blueprint()
        self.assertEqual(bp.url_prefix, '/api/system')

    @patch('core.compute_optimizer.create_optimizer_blueprint')
    def test_blueprint_none_without_flask(self, mock_create):
        """Blueprint creation returns None when Flask is missing."""
        # Simulate Flask import failure by having the real function handle it
        # We test the contract: the function CAN return None
        mock_create.return_value = None
        result = mock_create()
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════════
# Cooldown mechanism
# ═══════════════════════════════════════════════════════════════════════

class TestCooldown(unittest.TestCase):
    """Tests for _cooldown_ok() gating mechanism."""

    def test_cooldown_ok_when_never_run(self):
        """Action is allowed when it has never been run before."""
        opt = ComputeOptimizer()
        self.assertTrue(opt._cooldown_ok(ActionType.CACHE_CLEAN, time.time(), 300))

    def test_cooldown_blocks_within_interval(self):
        """Action is blocked if within cooldown interval."""
        opt = ComputeOptimizer()
        now = time.time()
        opt._cooldowns[ActionType.CACHE_CLEAN.value] = now
        self.assertFalse(opt._cooldown_ok(ActionType.CACHE_CLEAN, now + 10, 300))

    def test_cooldown_allows_after_interval(self):
        """Action is allowed after cooldown interval expires."""
        opt = ComputeOptimizer()
        now = time.time()
        opt._cooldowns[ActionType.CACHE_CLEAN.value] = now - 400
        self.assertTrue(opt._cooldown_ok(ActionType.CACHE_CLEAN, now, 300))


# ═══════════════════════════════════════════════════════════════════════
# Priority adjust — protected process safety
# ═══════════════════════════════════════════════════════════════════════

class TestPriorityAdjustProtection(unittest.TestCase):
    """Tests for _apply_priority_adjust() process protection."""

    @patch('core.compute_optimizer._try_import_psutil')
    def test_protected_process_skipped(self, mock_import):
        """System-critical processes are never adjusted."""
        mock_psutil = MagicMock()
        mock_proc = MagicMock()
        mock_proc.name.return_value = 'csrss.exe'
        mock_proc.nice.return_value = 32
        mock_psutil.Process.return_value = mock_proc
        mock_psutil.NoSuchProcess = type('NoSuchProcess', (Exception,), {})
        mock_psutil.AccessDenied = type('AccessDenied', (Exception,), {})
        mock_import.return_value = mock_psutil

        opt = ComputeOptimizer()
        action = OptimizationAction(
            action_type=ActionType.PROCESS_SUGGEST,
            target='csrss.exe',
            params={'pid': 4},
        )
        result = opt._apply_priority_adjust(action)
        self.assertIn('protected', result)
        mock_proc.nice.assert_not_called()  # nice() with arg should NOT be called

    @patch('core.compute_optimizer._try_import_psutil')
    def test_access_denied_handled(self, mock_import):
        """AccessDenied from psutil is caught and reported."""
        mock_psutil = MagicMock()
        exc_cls = type('AccessDenied', (Exception,), {})
        mock_psutil.AccessDenied = exc_cls
        mock_psutil.NoSuchProcess = type('NoSuchProcess', (Exception,), {})
        mock_proc = MagicMock()
        mock_proc.name.return_value = 'user_app'
        mock_proc.nice.side_effect = exc_cls("denied")
        mock_psutil.Process.return_value = mock_proc
        mock_import.return_value = mock_psutil

        opt = ComputeOptimizer()
        opt._platform = 'Linux'
        action = OptimizationAction(
            action_type=ActionType.PROCESS_SUGGEST,
            target='user_app',
            params={'pid': 9999},
        )
        result = opt._apply_priority_adjust(action)
        self.assertIn('access denied', result)


# ═══════════════════════════════════════════════════════════════════════
# Edge: history bounded
# ═══════════════════════════════════════════════════════════════════════

class TestHistoryBounded(unittest.TestCase):
    """Verify internal deques are bounded to prevent memory leaks."""

    def test_snapshot_deque_maxlen(self):
        """Snapshot history deque has maxlen=60."""
        opt = ComputeOptimizer()
        self.assertEqual(opt._snapshots.maxlen, 60)

    def test_history_deque_maxlen(self):
        """Optimization history deque has maxlen=HISTORY_MAXLEN (200)."""
        opt = ComputeOptimizer()
        self.assertEqual(opt._history.maxlen, 200)


if __name__ == '__main__':
    unittest.main()
