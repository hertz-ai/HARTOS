"""
Tests for model_registry.py — get_local_model() and get_model_by_policy().
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from integrations.agent_engine.model_registry import (
    ModelBackend, ModelRegistry, ModelTier,
)


def _make_backend(model_id, is_local=False, accuracy=0.5,
                  latency=1000.0, cost=0.0):
    return ModelBackend(
        model_id=model_id,
        display_name=model_id,
        tier=ModelTier.FAST if is_local else ModelTier.EXPERT,
        config_list_entry={'model': model_id, 'api_key': 'test'},
        avg_latency_ms=latency,
        accuracy_score=accuracy,
        cost_per_1k_tokens=cost,
        is_local=is_local,
    )


class TestGetLocalModel(unittest.TestCase):
    """Test ModelRegistry.get_local_model()."""

    def setUp(self):
        self.reg = ModelRegistry()

    def test_returns_none_when_no_models(self):
        self.assertIsNone(self.reg.get_local_model())

    def test_returns_none_when_no_local_models(self):
        self.reg.register(_make_backend('gpt-4', is_local=False, accuracy=0.9))
        self.assertIsNone(self.reg.get_local_model())

    def test_returns_local_only(self):
        self.reg.register(_make_backend('gpt-4', is_local=False, accuracy=0.9))
        self.reg.register(_make_backend('local-llm', is_local=True, accuracy=0.6))
        result = self.reg.get_local_model()
        self.assertIsNotNone(result)
        self.assertEqual(result.model_id, 'local-llm')
        self.assertTrue(result.is_local)

    def test_returns_highest_accuracy_local(self):
        self.reg.register(_make_backend('local-a', is_local=True, accuracy=0.5))
        self.reg.register(_make_backend('local-b', is_local=True, accuracy=0.8))
        self.reg.register(_make_backend('local-c', is_local=True, accuracy=0.6))
        result = self.reg.get_local_model()
        self.assertEqual(result.model_id, 'local-b')

    def test_respects_min_accuracy(self):
        self.reg.register(_make_backend('local-low', is_local=True, accuracy=0.3))
        self.reg.register(_make_backend('local-high', is_local=True, accuracy=0.7))
        result = self.reg.get_local_model(min_accuracy=0.5)
        self.assertEqual(result.model_id, 'local-high')

    def test_returns_none_when_min_accuracy_too_high(self):
        self.reg.register(_make_backend('local-low', is_local=True, accuracy=0.3))
        result = self.reg.get_local_model(min_accuracy=0.9)
        self.assertIsNone(result)


class TestGetModelByPolicy(unittest.TestCase):
    """Test ModelRegistry.get_model_by_policy()."""

    def setUp(self):
        self.reg = ModelRegistry()
        self.reg.register(_make_backend(
            'local-qwen', is_local=True, accuracy=0.55, latency=800))
        self.reg.register(_make_backend(
            'gpt-4', is_local=False, accuracy=0.92, latency=3000, cost=2.5))
        self.reg.register(_make_backend(
            'groq-fast', is_local=False, accuracy=0.60, latency=300, cost=0.1))

    def test_local_only_returns_local(self):
        result = self.reg.get_model_by_policy('local_only', 'own')
        self.assertIsNotNone(result)
        self.assertTrue(result.is_local)
        self.assertEqual(result.model_id, 'local-qwen')

    def test_local_only_blocks_metered(self):
        """local_only policy never returns non-local models."""
        reg = ModelRegistry()
        reg.register(_make_backend('gpt-4', is_local=False, accuracy=0.9))
        result = reg.get_model_by_policy('local_only', 'own')
        self.assertIsNone(result)

    def test_local_preferred_returns_local_when_available(self):
        result = self.reg.get_model_by_policy('local_preferred', 'own')
        self.assertTrue(result.is_local)

    def test_local_preferred_falls_back_to_metered(self):
        """When no local model meets min_accuracy, falls back to fastest metered."""
        result = self.reg.get_model_by_policy(
            'local_preferred', 'own', min_accuracy=0.9)
        self.assertIsNotNone(result)
        self.assertFalse(result.is_local)

    def test_any_returns_fastest(self):
        result = self.reg.get_model_by_policy('any', 'own')
        self.assertEqual(result.model_id, 'groq-fast')  # lowest latency

    def test_hive_task_enforces_local_preferred(self):
        """Hive tasks default to local_preferred even if policy is local_only."""
        result = self.reg.get_model_by_policy('local_only', 'hive')
        # hive + non-'any' → local_preferred, which falls back
        self.assertIsNotNone(result)

    def test_hive_task_with_any_policy_allows_metered(self):
        """Only when node opts into 'any' can hive use metered."""
        result = self.reg.get_model_by_policy('any', 'hive')
        self.assertIsNotNone(result)

    def test_idle_task_same_as_hive(self):
        result = self.reg.get_model_by_policy('local_only', 'idle')
        self.assertIsNotNone(result)

    def test_own_task_respects_configured_policy(self):
        """Own tasks use the exact policy given."""
        result = self.reg.get_model_by_policy('any', 'own')
        # 'any' → fastest, which is groq-fast at 300ms
        self.assertEqual(result.model_id, 'groq-fast')


class TestThreadLocalTaskSource(unittest.TestCase):
    """Test task_source thread-local propagation."""

    def test_default_task_source_is_own(self):
        from threadlocal import thread_local_data
        self.assertEqual(thread_local_data.get_task_source(), 'own')

    def test_set_and_get_task_source(self):
        from threadlocal import thread_local_data
        thread_local_data.set_task_source('hive')
        self.assertEqual(thread_local_data.get_task_source(), 'hive')
        thread_local_data.clear_task_source()
        self.assertEqual(thread_local_data.get_task_source(), 'own')

    def test_idle_task_source(self):
        from threadlocal import thread_local_data
        thread_local_data.set_task_source('idle')
        self.assertEqual(thread_local_data.get_task_source(), 'idle')
        thread_local_data.clear_task_source()


if __name__ == '__main__':
    unittest.main()
