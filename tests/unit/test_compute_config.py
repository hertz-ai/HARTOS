"""
Tests for compute_config.py — policy resolution with env > DB > defaults.
"""
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from integrations.agent_engine.compute_config import (
    get_compute_policy, invalidate_cache, _DEFAULTS, _parse_bool,
)


class TestComputeConfigDefaults(unittest.TestCase):
    """Test default policy values when no DB or env overrides exist."""

    def setUp(self):
        invalidate_cache()

    def test_default_policy_is_local_preferred(self):
        policy = get_compute_policy()
        self.assertEqual(policy['compute_policy'], 'local_preferred')

    def test_default_hive_policy_is_local_preferred(self):
        policy = get_compute_policy()
        self.assertEqual(policy['hive_compute_policy'], 'local_preferred')

    def test_allow_metered_default_false(self):
        policy = get_compute_policy()
        self.assertFalse(policy['allow_metered_for_hive'])

    def test_default_max_hive_gpu_pct(self):
        policy = get_compute_policy()
        self.assertEqual(policy['max_hive_gpu_pct'], 50)

    def test_default_metered_daily_limit(self):
        policy = get_compute_policy()
        self.assertEqual(policy['metered_daily_limit_usd'], 0.0)

    def test_default_accept_thought_experiments(self):
        policy = get_compute_policy()
        self.assertTrue(policy['accept_thought_experiments'])

    def test_default_accept_frontier_training_false(self):
        policy = get_compute_policy()
        self.assertFalse(policy['accept_frontier_training'])

    def test_default_auto_settle_true(self):
        policy = get_compute_policy()
        self.assertTrue(policy['auto_settle'])

    def test_all_default_keys_present(self):
        """Every key in _DEFAULTS must appear in the resolved policy."""
        policy = get_compute_policy()
        for key in _DEFAULTS:
            self.assertIn(key, policy, f"Missing key: {key}")


class TestComputeConfigEnvOverride(unittest.TestCase):
    """Test that environment variables override defaults."""

    def setUp(self):
        invalidate_cache()

    @patch.dict(os.environ, {'HEVOLVE_COMPUTE_POLICY': 'local_only'})
    def test_env_compute_policy_override(self):
        invalidate_cache()
        policy = get_compute_policy()
        self.assertEqual(policy['compute_policy'], 'local_only')

    @patch.dict(os.environ, {'HEVOLVE_ALLOW_METERED_HIVE': 'true'})
    def test_env_allow_metered_override(self):
        invalidate_cache()
        policy = get_compute_policy()
        self.assertTrue(policy['allow_metered_for_hive'])

    @patch.dict(os.environ, {'HEVOLVE_MAX_HIVE_GPU_PCT': '75'})
    def test_env_max_gpu_pct_override(self):
        invalidate_cache()
        policy = get_compute_policy()
        self.assertEqual(policy['max_hive_gpu_pct'], 75)

    @patch.dict(os.environ, {'HEVOLVE_METERED_DAILY_LIMIT': '5.50'})
    def test_env_daily_limit_override(self):
        invalidate_cache()
        policy = get_compute_policy()
        self.assertAlmostEqual(policy['metered_daily_limit_usd'], 5.50)

    @patch.dict(os.environ, {'HEVOLVE_HIVE_COMPUTE_POLICY': 'any'})
    def test_env_hive_policy_override(self):
        invalidate_cache()
        policy = get_compute_policy()
        self.assertEqual(policy['hive_compute_policy'], 'any')


class TestComputeConfigCache(unittest.TestCase):
    """Test caching behavior."""

    def test_policy_cache_returns_same_object(self):
        invalidate_cache()
        p1 = get_compute_policy('test_node')
        p2 = get_compute_policy('test_node')
        self.assertEqual(p1, p2)

    def test_invalidate_cache_forces_refresh(self):
        invalidate_cache()
        p1 = get_compute_policy('node_a')
        invalidate_cache('node_a')
        p2 = get_compute_policy('node_a')
        # Both should have same values (defaults) but cache was invalidated
        self.assertEqual(p1, p2)

    def test_different_nodes_cached_separately(self):
        invalidate_cache()
        p1 = get_compute_policy('node_x')
        p2 = get_compute_policy('node_y')
        self.assertEqual(p1, p2)  # Same defaults


class TestParseBool(unittest.TestCase):
    """Test _parse_bool helper."""

    def test_true_values(self):
        for val in ('true', 'True', 'TRUE', '1', 'yes', 'Yes'):
            self.assertTrue(_parse_bool(val), f"Expected True for '{val}'")

    def test_false_values(self):
        for val in ('false', 'False', '0', 'no', '', 'anything'):
            self.assertFalse(_parse_bool(val), f"Expected False for '{val}'")


if __name__ == '__main__':
    unittest.main()
