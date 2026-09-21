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

    # allow_metered_for_hive is a PERMISSION, so its env pin is asymmetric: it
    # may restrict, never grant. This replaces a test that asserted
    # HEVOLVE_ALLOW_METERED_HIVE='true' GRANTS it -- that test pinned the defect.
    # Two things the grant defeated:
    #   * the DB row is the operator's own answer, and env > DB meant the pin
    #     spent their metered link after they declined it;
    #   * /api/.../compute-policy returns 403 for this field on a central node
    #     (hart_intelligence_entry.py:12897); the env layer sat above that guard.
    # Same asymmetry as copilot_enabled() (66386a45e).

    @patch.dict(os.environ, {'HEVOLVE_ALLOW_METERED_HIVE': 'true'})
    def test_env_cannot_GRANT_metered_permission(self):
        invalidate_cache()
        policy = get_compute_policy()
        self.assertFalse(
            policy['allow_metered_for_hive'],
            'an env pin granted a permission the operator did not give; '
            'HEVOLVE_ALLOW_METERED_HIVE must only be able to restrict')

    @patch.dict(os.environ, {'HEVOLVE_ALLOW_METERED_HIVE': 'false'})
    def test_env_CAN_still_restrict_metered_permission(self):
        """The headless kill-switch direction must keep working."""
        invalidate_cache()
        policy = get_compute_policy()
        self.assertFalse(policy['allow_metered_for_hive'])

    @patch.dict(os.environ, {'HEVOLVE_ALLOW_METERED_HIVE': 'true'})
    def test_a_granting_pin_does_not_disturb_capacity_keys(self):
        """Only the permission is asymmetric; capacity pins stay honoured.

        Guards the obvious over-fold: clamping every env override would have
        broken max_hive_gpu_pct / metered_daily_limit_usd, which are numbers, not
        answers about what is allowed.
        """
        invalidate_cache()
        with patch.dict(os.environ, {'HEVOLVE_MAX_HIVE_GPU_PCT': '75',
                                     'HEVOLVE_METERED_DAILY_LIMIT': '5.50'}):
            invalidate_cache()
            policy = get_compute_policy()
        self.assertEqual(policy['max_hive_gpu_pct'], 75)
        self.assertAlmostEqual(policy['metered_daily_limit_usd'], 5.50)
        self.assertFalse(policy['allow_metered_for_hive'])

    def test_the_restrict_only_set_names_permissions_not_capacity(self):
        """A capacity key must never be added to the restrict-only set.

        If someone adds max_hive_gpu_pct here, an operator raising their own GPU
        share via env silently stops working -- a config value would be treated
        as if it were a consent answer.
        """
        from integrations.agent_engine.compute_config import (
            _ENV_MAY_ONLY_RESTRICT,
        )
        capacity = {'max_hive_gpu_pct', 'offered_gpu_hours_per_day',
                    'metered_daily_limit_usd', 'min_settlement_spark'}
        self.assertEqual(
            _ENV_MAY_ONLY_RESTRICT & capacity, frozenset(),
            'a capacity number was marked restrict-only; that set is for '
            'permissions (what the machine may do), not for limits')

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
