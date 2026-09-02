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
        # base_url makes it dispatchable; the selectors skip a backend
        # without an endpoint (is_dispatchable), which is what left every
        # selector test in this file returning None.
        config_list_entry={'model': model_id, 'api_key': 'test',
                           'base_url': 'http://test.invalid/v1'},
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
        from hartos.threadlocal import thread_local_data
        self.assertEqual(thread_local_data.get_task_source(), 'own')

    def test_set_and_get_task_source(self):
        from hartos.threadlocal import thread_local_data
        thread_local_data.set_task_source('hive')
        self.assertEqual(thread_local_data.get_task_source(), 'hive')
        thread_local_data.clear_task_source()
        self.assertEqual(thread_local_data.get_task_source(), 'own')

    def test_idle_task_source(self):
        from hartos.threadlocal import thread_local_data
        thread_local_data.set_task_source('idle')
        self.assertEqual(thread_local_data.get_task_source(), 'idle')
        thread_local_data.clear_task_source()


class TestConfiguredBackendRegistration(unittest.TestCase):
    """Behavioural test for _register_defaults() following the ONE configured
    LLM (core.autogen_config), #69.

    Drives the real _register_defaults() against a fresh registry with the
    env boundary patched, then asserts the observable registry state.  The
    vendor side doors (GROQ/DEEPSEEK/AZURE/ANTHROPIC/GLM/QWEN keys) used to
    register a second text LLM beside the configured one; a stray key must
    not pick a model any more.
    """

    _TRIO = ('HEVOLVE_LLM_ENDPOINT_URL', 'HEVOLVE_LLM_MODEL_NAME',
             'HEVOLVE_LLM_API_KEY')
    _CLEARED = _TRIO + (
        'HEVOLVE_NODE_TIER', 'HEVOLVE_ACTIVE_CLOUD_PROVIDER',
        'HEVOLVE_LOCAL_LLM_MODEL',
        # the removed side doors
        'GROQ_API_KEY', 'DEEPSEEK_API_KEY', 'AZURE_OPENAI_API_KEY',
        'ANTHROPIC_API_KEY', 'GLM_API_KEY', 'ZHIPUAI_API_KEY', 'QWEN_API_KEY',
        'QWEN_BASE_URL', 'QWEN_MODEL',
        # optional local backends that would compete for the draft slot
        'HEVOLVEAI_API_URL', 'HEVOLVE_VISION_LITE_ENABLED',
    )
    _LOCAL_IDS = {'qwen3.5-0.8b-draft', 'qwen3.5-4b-local', 'qwen3-vl-4b-local'}
    _VENDOR_IDS = {'groq-llama-3.1-8b', 'deepseek-v3', 'gpt-4.1-azure',
                   'claude-sonnet', 'glm-5.2', 'qwen3.8-27b'}

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self._CLEARED}
        for k in self._CLEARED:
            os.environ.pop(k, None)
        from integrations.agent_engine import model_registry as mr
        self.mr = mr
        # _register_defaults() writes into the module-global registry; give
        # it a fresh one so the process-wide state is left alone.
        self._real_registry = mr.model_registry
        mr.model_registry = ModelRegistry()

    def tearDown(self):
        self.mr.model_registry = self._real_registry
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _ids(self):
        return {m.model_id for m in self.mr.model_registry.list_models()}

    def _configure_api(self):
        os.environ['HEVOLVE_NODE_TIER'] = 'central'
        os.environ['HEVOLVE_LLM_ENDPOINT_URL'] = 'https://api.example.test/v1'
        os.environ['HEVOLVE_LLM_MODEL_NAME'] = 'Qwen/Qwen3.8-27B'
        os.environ['HEVOLVE_LLM_API_KEY'] = 'test-key'

    def test_api_kind_registers_the_configured_backend_only(self):
        self._configure_api()
        # A stray vendor key must not add a second text LLM.
        os.environ['GLM_API_KEY'] = 'stray-glm'
        os.environ['QWEN_API_KEY'] = 'stray-qwen'
        os.environ['GROQ_API_KEY'] = 'stray-groq'
        self.mr._register_defaults()
        ids = self._ids()
        self.assertIn('configured-api', ids)
        self.assertFalse(ids & self._VENDOR_IDS, ids & self._VENDOR_IDS)
        self.assertFalse(ids & self._LOCAL_IDS, ids & self._LOCAL_IDS)
        b = self.mr.model_registry.get_model('configured-api')
        entry = b.config_list_entry
        self.assertEqual(entry['model'], 'Qwen/Qwen3.8-27B')
        self.assertEqual(entry['api_key'], 'test-key')
        self.assertEqual(entry['base_url'], 'https://api.example.test/v1')
        self.assertEqual(b.tier, self.mr.ModelTier.FAST)
        # Its own LLM: dispatch runs this node's full /chat pipeline.
        self.assertTrue(b.is_local)
        self.assertTrue(b.is_dispatchable())

    def test_api_kind_the_configured_backend_serves_every_role(self):
        self._configure_api()
        self.mr._register_defaults()
        reg = self.mr.model_registry
        # claude-code registers wherever the copilot is logged in (the
        # frontier tier, EXPERT, is_local); take it out so this asserts the
        # single-backend collapse itself: draft = fast = local = expert.
        reg.unregister('claude-code')
        self.assertEqual(reg.get_draft_model().model_id, 'configured-api')
        self.assertEqual(reg.get_fast_model().model_id, 'configured-api')
        self.assertEqual(reg.get_local_model().model_id, 'configured-api')
        self.assertEqual(reg.get_expert_model().model_id, 'configured-api')
        # One backend: nothing to speculate between.
        self.assertEqual(reg.speculation_pair(), (None, None))

    def test_api_kind_prices_the_configured_model_like_the_budget_gate(self):
        from integrations.agent_engine.budget_gate import spark_per_1k
        self._configure_api()
        self.mr._register_defaults()
        b = self.mr.model_registry.get_model('configured-api')
        self.assertEqual(b.cost_per_1k_tokens, spark_per_1k('Qwen/Qwen3.8-27B'))
        os.environ['HEVOLVE_LLM_MODEL_NAME'] = 'gpt-4o'
        self.mr.model_registry = ModelRegistry()
        self.mr._register_defaults()
        b = self.mr.model_registry.get_model('configured-api')
        self.assertEqual(b.cost_per_1k_tokens, spark_per_1k('gpt-4o'))
        self.assertEqual(b.config_list_entry['model'], 'gpt-4o')

    def test_local_kind_registers_the_local_servers_and_no_vendor(self):
        # No trio, flat tier: the node's own llama-server(s).
        os.environ['GLM_API_KEY'] = 'stray-glm'
        os.environ['QWEN_API_KEY'] = 'stray-qwen'
        self.mr._register_defaults()
        ids = self._ids()
        self.assertTrue(self._LOCAL_IDS <= ids, ids)
        self.assertNotIn('configured-api', ids)
        self.assertFalse(ids & self._VENDOR_IDS, ids & self._VENDOR_IDS)
        self.assertEqual(self.mr.model_registry.get_draft_model().model_id,
                         'qwen3.5-0.8b-draft')

    def test_vault_provider_without_endpoint_stays_local(self):
        # anthropic/gemini/groq export no OpenAI-compatible endpoint: only
        # the vendor SDK ladder in get_llm() reaches them, so the registry
        # keeps the local servers rather than posting the key elsewhere.
        os.environ['HEVOLVE_ACTIVE_CLOUD_PROVIDER'] = 'anthropic'
        os.environ['HEVOLVE_LLM_API_KEY'] = 'sk-ant-test'
        os.environ['HEVOLVE_LLM_MODEL_NAME'] = 'claude-sonnet-4-20250514'
        self.mr._register_defaults()
        ids = self._ids()
        self.assertNotIn('configured-api', ids)
        self.assertTrue(self._LOCAL_IDS <= ids, ids)

    def test_vault_provider_with_endpoint_is_the_api_kind_on_a_flat_node(self):
        os.environ['HEVOLVE_ACTIVE_CLOUD_PROVIDER'] = 'groq'
        os.environ['HEVOLVE_LLM_API_KEY'] = 'gsk-test'
        os.environ['HEVOLVE_LLM_MODEL_NAME'] = 'llama-3.3-70b-versatile'
        os.environ['HEVOLVE_LLM_ENDPOINT_URL'] = 'https://api.groq.com/openai/v1'
        self.mr._register_defaults()
        ids = self._ids()
        self.assertIn('configured-api', ids)
        self.assertFalse(ids & self._LOCAL_IDS, ids & self._LOCAL_IDS)
        entry = self.mr.model_registry.get_model('configured-api').config_list_entry
        self.assertEqual(entry['base_url'], 'https://api.groq.com/openai/v1')
        self.assertEqual(entry['model'], 'llama-3.3-70b-versatile')


if __name__ == '__main__':
    unittest.main()
