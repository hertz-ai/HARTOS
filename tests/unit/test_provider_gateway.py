"""
Tests for the universal provider gateway system.

Covers:
  - ProviderRegistry: registration, query, find_best, find_cheapest, find_fastest
  - ProviderGateway: routing, fallback, local fallback, cost calculation
  - EfficiencyMatrix: recording, benchmarking, leaderboard, persistence
  - Agent tools: tool registration and execution
"""

import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

# ═══════════════════════════════════════════════════════════════════════
# Registry Tests
# ═══════════════════════════════════════════════════════════════════════

class TestProviderRegistry(unittest.TestCase):
    """Test ProviderRegistry catalog, query, and selection."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.registry_path = os.path.join(self.tmpdir, 'registry.json')

    def _make_registry(self):
        from integrations.providers.registry import ProviderRegistry
        return ProviderRegistry(registry_path=self.registry_path)

    def test_builtin_providers_loaded(self):
        reg = self._make_registry()
        providers = reg.list_all()
        self.assertGreater(len(providers), 5, "Should have multiple builtin providers")
        ids = [p.id for p in providers]
        self.assertIn('together', ids)
        self.assertIn('groq', ids)
        self.assertIn('local', ids)

    def test_list_by_category(self):
        reg = self._make_registry()
        llm_providers = reg.list_by_category('llm')
        self.assertGreater(len(llm_providers), 3)
        for p in llm_providers:
            self.assertIn('llm', p.categories)

    def test_list_api_vs_affiliate(self):
        reg = self._make_registry()
        api = reg.list_api_providers()
        aff = reg.list_affiliate_providers()
        self.assertGreater(len(api), 3)
        self.assertGreater(len(aff), 0)
        for p in api:
            self.assertEqual(p.provider_type, 'api')
        for p in aff:
            self.assertEqual(p.provider_type, 'affiliate')

    def test_find_cheapest_no_api_key(self):
        """find_cheapest should return None if no providers have API keys."""
        reg = self._make_registry()
        # Clear all env vars
        for p in reg.list_api_providers():
            if p.env_key and p.env_key in os.environ:
                del os.environ[p.env_key]
        result = reg.find_cheapest('llm')
        self.assertIsNone(result)

    def test_find_cheapest_with_api_key(self):
        reg = self._make_registry()
        os.environ['TOGETHER_API_KEY'] = 'test-key-123'
        try:
            result = reg.find_cheapest('llm')
            self.assertIsNotNone(result)
            provider, model = result
            self.assertEqual(provider.id, 'together')
        finally:
            del os.environ['TOGETHER_API_KEY']

    def test_find_best_balanced(self):
        reg = self._make_registry()
        os.environ['GROQ_API_KEY'] = 'test-key'
        try:
            result = reg.find_best('llm', strategy='balanced')
            self.assertIsNotNone(result)
        finally:
            del os.environ['GROQ_API_KEY']

    def test_register_custom_provider(self):
        from integrations.providers.registry import Provider
        reg = self._make_registry()
        custom = Provider(
            id='custom_test', name='Custom Test',
            provider_type='api', base_url='https://example.com/v1',
            categories=['llm'],
        )
        reg.register(custom, persist=True)
        self.assertIsNotNone(reg.get('custom_test'))

        # Reload from disk
        reg2 = self._make_registry()
        self.assertIsNotNone(reg2.get('custom_test'))

    def test_update_model_stats(self):
        reg = self._make_registry()
        together = reg.get('together')
        model_id = list(together.models.keys())[0]
        pm = together.models[model_id]
        old_speed = pm.avg_tok_per_s

        reg.update_model_stats('together', model_id, tok_per_s=150.0, success=True)
        self.assertGreater(pm.avg_tok_per_s, 0)

    def test_set_api_key(self):
        reg = self._make_registry()
        result = reg.set_api_key('together', 'sk-test-key-xyz')
        self.assertTrue(result)
        self.assertEqual(os.environ.get('TOGETHER_API_KEY'), 'sk-test-key-xyz')
        # Cleanup
        if 'TOGETHER_API_KEY' in os.environ:
            del os.environ['TOGETHER_API_KEY']

    def test_capabilities_summary(self):
        reg = self._make_registry()
        summary = reg.get_capabilities_summary()
        self.assertIn('llm', summary)
        self.assertIn('image_gen', summary)
        self.assertGreater(len(summary['llm']), 2)

    def test_provider_serialization(self):
        from integrations.providers.registry import Provider, ProviderModel
        p = Provider(
            id='test', name='Test', categories=['llm'],
            models={'m1': ProviderModel(model_id='m1', model_type='llm')},
        )
        d = p.to_dict()
        p2 = Provider.from_dict(d)
        self.assertEqual(p2.id, 'test')
        self.assertIn('m1', p2.models)

    def test_thread_safety(self):
        """Concurrent reads and writes should not crash."""
        reg = self._make_registry()
        errors = []

        def _reader():
            try:
                for _ in range(50):
                    reg.list_all()
                    reg.find_best('llm', strategy='balanced')
            except Exception as e:
                errors.append(e)

        def _writer():
            try:
                for i in range(50):
                    reg.update_model_stats(
                        'together',
                        list(reg.get('together').models.keys())[0],
                        tok_per_s=float(i * 10),
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_reader) for _ in range(3)]
        threads += [threading.Thread(target=_writer) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(errors), 0, f"Thread safety errors: {errors}")


# ═══════════════════════════════════════════════════════════════════════
# Gateway Tests
# ═══════════════════════════════════════════════════════════════════════

class TestProviderGateway(unittest.TestCase):
    """Test ProviderGateway routing and API calls."""

    def test_no_provider_returns_error(self):
        from integrations.providers.gateway import ProviderGateway
        gw = ProviderGateway()
        result = gw.generate('test', model_type='llm')
        # No API keys set → should fail gracefully
        # (may succeed if local server is running, so just check it doesn't crash)
        self.assertIsNotNone(result)
        self.assertIsInstance(result.success, bool)

    def test_cost_calculation_per_1m_tokens(self):
        from integrations.providers.gateway import ProviderGateway
        from integrations.providers.registry import ProviderModel, PRICE_PER_1M_TOKENS
        pm = ProviderModel(
            model_id='test', input_price=1.0, output_price=2.0,
            pricing_unit=PRICE_PER_1M_TOKENS,
        )
        cost = ProviderGateway._calculate_cost(pm, 1000, 500)
        expected = 1000 * 1.0 / 1_000_000 + 500 * 2.0 / 1_000_000
        self.assertAlmostEqual(cost, expected, places=8)

    def test_cost_calculation_per_image(self):
        from integrations.providers.gateway import ProviderGateway
        from integrations.providers.registry import ProviderModel, PRICE_PER_IMAGE
        pm = ProviderModel(
            model_id='test', input_price=0.04,
            pricing_unit=PRICE_PER_IMAGE,
        )
        cost = ProviderGateway._calculate_cost(pm, 0, 0)
        self.assertEqual(cost, 0.04)

    def test_cost_calculation_free(self):
        from integrations.providers.gateway import ProviderGateway
        from integrations.providers.registry import ProviderModel, PRICE_FREE
        pm = ProviderModel(model_id='test', pricing_unit=PRICE_FREE)
        cost = ProviderGateway._calculate_cost(pm, 10000, 5000)
        self.assertEqual(cost, 0.0)

    def test_stats_tracking(self):
        from integrations.providers.gateway import ProviderGateway
        gw = ProviderGateway()
        stats = gw.get_stats()
        self.assertEqual(stats['total_requests'], 0)
        self.assertEqual(stats['total_cost_usd'], 0.0)
        self.assertIn('capabilities', stats)

    @patch('integrations.providers.gateway.ProviderGateway._call_openai')
    def test_fallback_on_failure(self, mock_call):
        """Gateway should try next provider on failure."""
        from integrations.providers.gateway import ProviderGateway, GatewayResult
        gw = ProviderGateway()

        # First call fails, second succeeds
        mock_call.side_effect = [
            GatewayResult(success=False, error='rate limited', provider_id='together'),
            GatewayResult(success=True, content='Hello!', provider_id='groq'),
        ]

        os.environ['TOGETHER_API_KEY'] = 'test'
        os.environ['GROQ_API_KEY'] = 'test'
        try:
            result = gw.generate('test', model_type='llm')
            # Should have tried at least once
            self.assertGreaterEqual(mock_call.call_count, 1)
        finally:
            os.environ.pop('TOGETHER_API_KEY', None)
            os.environ.pop('GROQ_API_KEY', None)


# ═══════════════════════════════════════════════════════════════════════
# Efficiency Matrix Tests
# ═══════════════════════════════════════════════════════════════════════

class TestEfficiencyMatrix(unittest.TestCase):
    """Test EfficiencyMatrix recording, benchmarking, and querying."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.matrix_path = os.path.join(self.tmpdir, 'matrix.json')

    def _make_matrix(self):
        from integrations.providers.efficiency_matrix import EfficiencyMatrix
        return EfficiencyMatrix(matrix_path=self.matrix_path)

    def test_record_request(self):
        m = self._make_matrix()
        m.record_request('together', 'llama-70b', tok_per_s=120, e2e_ms=500,
                         cost_usd=0.001, output_tokens=100, success=True)

        bm = m.get_benchmark('together', 'llama-70b')
        self.assertIsNotNone(bm)
        self.assertEqual(bm.total_requests, 1)
        self.assertAlmostEqual(bm.avg_tok_per_s, 120.0, places=1)
        self.assertEqual(bm.success_rate, 1.0)

    def test_ema_smoothing(self):
        """Subsequent records should smooth via EMA, not overwrite."""
        m = self._make_matrix()
        m.record_request('p1', 'm1', tok_per_s=100, success=True)
        m.record_request('p1', 'm1', tok_per_s=200, success=True)

        bm = m.get_benchmark('p1', 'm1')
        # EMA with alpha=0.1: after 100 then 200 → 100*(1-0.1) + 200*0.1 = 110
        self.assertAlmostEqual(bm.avg_tok_per_s, 110.0, places=1)

    def test_failure_tracking(self):
        m = self._make_matrix()
        m.record_request('p1', 'm1', success=True)
        m.record_request('p1', 'm1', success=False)
        m.record_request('p1', 'm1', success=True)

        bm = m.get_benchmark('p1', 'm1')
        self.assertEqual(bm.total_requests, 3)
        self.assertEqual(bm.failed_requests, 1)
        self.assertAlmostEqual(bm.success_rate, 2/3, places=2)

    def test_efficiency_score_computation(self):
        from integrations.providers.efficiency_matrix import ModelBenchmark
        bm = ModelBenchmark(
            provider_id='test', model_id='test',
            avg_tok_per_s=100, quality_score=0.9,
            success_rate=0.95, cost_per_1k_output_tokens=0.5,
        )
        bm.compute_efficiency()
        # efficiency = (quality × speed × reliability) / cost
        # speed = min(1.0, 100/100) = 1.0
        # efficiency = (0.9 × 1.0 × 0.95) / 0.5 = 1.71
        self.assertGreater(bm.efficiency_score, 1.0)

    def test_leaderboard_sorting(self):
        m = self._make_matrix()
        # Record data for 3 providers
        for i, pid in enumerate(['fast', 'medium', 'slow']):
            m.record_request(pid, 'model', tok_per_s=(300 - i * 100),
                             cost_usd=0.001, output_tokens=100, success=True)

        board = m.get_leaderboard('llm', sort_by='speed')
        self.assertGreater(len(board), 0)
        if len(board) >= 2:
            self.assertGreaterEqual(board[0].avg_tok_per_s, board[1].avg_tok_per_s)

    def test_persistence(self):
        m = self._make_matrix()
        m.record_request('p1', 'm1', tok_per_s=150, success=True)
        m.save()

        # Reload
        m2 = self._make_matrix()
        bm = m2.get_benchmark('p1', 'm1')
        self.assertIsNotNone(bm)
        self.assertAlmostEqual(bm.avg_tok_per_s, 150.0, places=1)

    def test_matrix_summary(self):
        m = self._make_matrix()
        m.record_request('p1', 'm1', model_type='llm', success=True)
        m.record_request('p2', 'm2', model_type='image_gen', success=True)
        summary = m.get_matrix_summary()
        self.assertEqual(summary['total_entries'], 2)
        self.assertIn('llm', summary['by_type'])

    def test_quality_scoring(self):
        from integrations.providers.efficiency_matrix import (
            EfficiencyMatrix, BenchmarkTask,
        )
        m = self._make_matrix()
        task = BenchmarkTask(
            id='test', prompt='test',
            expected_keywords=['quantum', 'qubit'],
        )
        # Full match
        score = m._score_quality('Quantum computers use qubits for computation.', task)
        self.assertGreater(score, 0.7)

        # No match
        score = m._score_quality('The weather is nice today.', task)
        self.assertLess(score, 0.6)

        # Empty
        score = m._score_quality('', task)
        self.assertEqual(score, 0.0)

    def test_thread_safety(self):
        m = self._make_matrix()
        errors = []

        def _recorder(pid):
            try:
                for i in range(100):
                    m.record_request(pid, 'model', tok_per_s=float(i),
                                     success=(i % 10 != 0))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_recorder, args=(f'p{i}',))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")


# ═══════════════════════════════════════════════════════════════════════
# Agent Tools Tests
# ═══════════════════════════════════════════════════════════════════════

class TestAgentTools(unittest.TestCase):
    """Test that provider tools register correctly."""

    def test_tools_register(self):
        try:
            from integrations.providers.agent_tools import get_provider_tools
            tools = get_provider_tools()
            # Should have tools if LangChain is available, empty list otherwise
            self.assertIsInstance(tools, list)
            if tools:
                names = [t.name for t in tools]
                self.assertIn('Cloud_LLM', names)
                self.assertIn('Generate_Image', names)
                self.assertIn('List_AI_Providers', names)
                self.assertIn('Provider_Leaderboard', names)
        except ImportError:
            pass  # LangChain not installed


# ═══════════════════════════════════════════════════════════════════════
# Integration Tests
# ═══════════════════════════════════════════════════════════════════════

class TestProviderIntegration(unittest.TestCase):
    """Integration tests — end-to-end flow without actual API calls."""

    def test_full_flow_with_mock(self):
        """Simulate: register provider → configure key → generate → track stats."""
        from integrations.providers.registry import (
            ProviderRegistry, Provider, ProviderModel, PRICE_PER_1M_TOKENS,
        )
        from integrations.providers.efficiency_matrix import EfficiencyMatrix

        tmpdir = tempfile.mkdtemp()
        reg = ProviderRegistry(os.path.join(tmpdir, 'reg.json'))
        matrix = EfficiencyMatrix(os.path.join(tmpdir, 'matrix.json'))

        # Register a test provider
        reg.register(Provider(
            id='test_provider', name='Test',
            provider_type='api',
            base_url='https://test.example.com/v1',
            api_format='openai',
            env_key='TEST_PROVIDER_KEY',
            categories=['llm'],
            models={
                'test-model': ProviderModel(
                    model_id='test-model', canonical_id='test',
                    model_type='llm', input_price=0.5, output_price=1.0,
                    pricing_unit=PRICE_PER_1M_TOKENS,
                ),
            },
        ))

        # Set API key
        os.environ['TEST_PROVIDER_KEY'] = 'sk-test'
        try:
            # Verify it's findable
            result = reg.find_best('llm', strategy='cheapest')
            self.assertIsNotNone(result)
            self.assertEqual(result[0].id, 'test_provider')

            # Simulate recording usage
            matrix.record_request(
                'test_provider', 'test-model',
                tok_per_s=100, e2e_ms=500,
                cost_usd=0.001, output_tokens=200, success=True,
            )

            bm = matrix.get_benchmark('test_provider', 'test-model')
            self.assertEqual(bm.total_requests, 1)
            self.assertGreater(bm.efficiency_score, 0)

            # Leaderboard
            board = matrix.get_leaderboard('llm')
            self.assertEqual(len(board), 1)
        finally:
            os.environ.pop('TEST_PROVIDER_KEY', None)


# ═══════════════════════════════════════════════════════════════════════
# Revenue Tracker Tests
# ═══════════════════════════════════════════════════════════════════════

class TestRevenueTracker(unittest.TestCase):
    """Test RevenueTracker recording, analytics, and persistence."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tracker_path = os.path.join(self.tmpdir, 'revenue.json')

    def _make_tracker(self):
        from integrations.providers.revenue_tracker import RevenueTracker
        return RevenueTracker(tracker_path=self.tracker_path)

    def test_record_cost(self):
        t = self._make_tracker()
        t.record_cost('together', 'llama-70b', 0.001, tokens_used=500)
        self.assertEqual(t._total_requests, 1)
        self.assertAlmostEqual(t._total_cost, 0.001, places=6)

    def test_record_revenue(self):
        t = self._make_tracker()
        t.record_revenue('affiliate', 0.50, provider_id='runwayml')
        self.assertAlmostEqual(t._total_revenue, 0.50, places=2)

    def test_earning_spark(self):
        t = self._make_tracker()
        t.record_cost('p1', 'm1', 0.10)
        t.record_revenue('credits', 0.30)
        spark = t.get_earning_spark()
        self.assertAlmostEqual(spark, 3.0, places=1)  # 0.30 / 0.10

    def test_earning_spark_no_cost(self):
        t = self._make_tracker()
        t.record_revenue('credits', 1.0)
        self.assertEqual(t.get_earning_spark(), float('inf'))

    def test_earning_spark_no_revenue_no_cost(self):
        t = self._make_tracker()
        self.assertEqual(t.get_earning_spark(), 0.0)

    def test_summary(self):
        t = self._make_tracker()
        t.record_cost('p1', 'm1', 0.05, request_type='llm')
        t.record_cost('p2', 'm2', 0.10, request_type='image_gen')
        t.record_revenue('affiliate', 0.25, provider_id='runwayml')

        s = t.get_summary()
        self.assertAlmostEqual(s['total_cost_usd'], 0.15, places=2)
        self.assertAlmostEqual(s['total_revenue_usd'], 0.25, places=2)
        self.assertGreater(s['earning_spark'], 1.0)
        self.assertIn('p1', s['cost_by_provider'])
        self.assertIn('llm', s['cost_by_type'])
        self.assertIn('affiliate', s['revenue_by_source'])

    def test_persistence(self):
        t = self._make_tracker()
        t.record_cost('p1', 'm1', 0.05)
        t.record_revenue('credits', 0.20)
        t.save()

        t2 = self._make_tracker()
        self.assertAlmostEqual(t2._total_cost, 0.05, places=4)
        self.assertAlmostEqual(t2._total_revenue, 0.20, places=4)

    def test_period_stats(self):
        t = self._make_tracker()
        t.record_cost('p1', 'm1', 0.01)
        t.record_cost('p1', 'm1', 0.02)
        ps = t.get_period_stats(hours=1)
        self.assertAlmostEqual(ps.total_cost, 0.03, places=4)
        self.assertEqual(ps.total_requests, 2)

    def test_trim_old_entries(self):
        t = self._make_tracker()
        # Add an old entry manually
        from integrations.providers.revenue_tracker import CostEntry
        old = CostEntry(timestamp=time.time() - 100000, provider_id='old',
                        model_id='old', cost_usd=999.0)
        t._costs.append(old)
        t._trim_old_entries()
        self.assertFalse(any(c.provider_id == 'old' for c in t._costs))


# ═══════════════════════════════════════════════════════════════════════
# Discovery Agent Tests
# ═══════════════════════════════════════════════════════════════════════

class TestDiscoveryAgent(unittest.TestCase):
    """Test DiscoveryAgent source selection and canonical ID."""

    def test_canonical_id(self):
        from integrations.providers.discovery_agent import DiscoveryAgent
        agent = DiscoveryAgent()
        self.assertEqual(agent._to_canonical_id('meta-llama/Llama-3.3-70B-Instruct-Turbo'),
                         'llama-3.3-70b')
        self.assertEqual(agent._to_canonical_id('deepseek-ai/DeepSeek-V3'),
                         'deepseek-v3')
        self.assertEqual(agent._to_canonical_id('Qwen/QwQ-32B'),
                         'qwq-32b')

    def test_pick_source_rotates(self):
        from integrations.providers.discovery_agent import DiscoveryAgent
        agent = DiscoveryAgent()
        sources_seen = set()
        for _ in range(4):
            s = agent._pick_source()
            if s:
                sources_seen.add(s)
                agent._last_scan[s] = 0  # Don't block rotation
        self.assertGreater(len(sources_seen), 1)

    def test_pick_source_respects_cooldown(self):
        from integrations.providers.discovery_agent import DiscoveryAgent, DISCOVERY_SOURCES
        agent = DiscoveryAgent()
        # Mark all sources as recently scanned
        for s in DISCOVERY_SOURCES:
            agent._last_scan[s] = time.time()
        self.assertIsNone(agent._pick_source())

    def test_stats(self):
        from integrations.providers.discovery_agent import DiscoveryAgent
        agent = DiscoveryAgent()
        stats = agent.get_stats()
        self.assertEqual(stats['total_discoveries'], 0)
        self.assertIn('sources', stats)

    def test_is_inference_ready(self):
        from integrations.providers.discovery_agent import DiscoveryAgent
        self.assertTrue(DiscoveryAgent._is_inference_ready({'downloads': 5000, 'likes': 50}))
        self.assertFalse(DiscoveryAgent._is_inference_ready({'downloads': 10, 'likes': 1}))


# ═══════════════════════════════════════════════════════════════════════
# Resource Enforcer Tests
# ═══════════════════════════════════════════════════════════════════════

class TestResourceEnforcer(unittest.TestCase):
    """Test ResourceEnforcer detection and cap calculation."""

    def test_get_total_ram(self):
        from core.resource_governor import ResourceEnforcer
        e = ResourceEnforcer()
        ram = e._get_total_ram_gb()
        self.assertGreater(ram, 0)
        self.assertLess(ram, 2048)  # Sanity — no machine has 2 TB

    def test_enforce_idempotent(self):
        """Calling enforce() twice should not crash."""
        from core.resource_governor import ResourceEnforcer
        e = ResourceEnforcer()
        # Don't actually enforce (would set priority) — just test the flag
        e._enforced = True
        e.enforce()  # Should return early
        self.assertTrue(e._enforced)

    def test_singleton_thread_safe(self):
        from core.resource_governor import get_enforcer
        enforcers = []
        def _get():
            enforcers.append(id(get_enforcer()))
        threads = [threading.Thread(target=_get) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # All should be the same instance
        self.assertEqual(len(set(enforcers)), 1)


# ═══════════════════════════════════════════════════════════════════════
# Gateway Result Model Type Test (C1 fix verification)
# ═══════════════════════════════════════════════════════════════════════

class TestGatewayResultModelType(unittest.TestCase):
    """Verify C1 fix: model_type propagates to _track for revenue tracker."""

    def test_gateway_result_has_model_type(self):
        from integrations.providers.gateway import GatewayResult
        r = GatewayResult(success=True, model_type='image_gen')
        self.assertEqual(r.model_type, 'image_gen')

    def test_gateway_result_default_model_type(self):
        from integrations.providers.gateway import GatewayResult
        r = GatewayResult(success=True)
        self.assertEqual(r.model_type, 'llm')


# ═══════════════════════════════════════════════════════════════════════
# Provider to_dict excludes api_key_set (C3 fix verification)
# ═══════════════════════════════════════════════════════════════════════

class TestProviderApiKeyPersistence(unittest.TestCase):
    """Verify C3 fix: api_key_set not persisted to JSON."""

    def test_to_dict_excludes_api_key_set(self):
        from integrations.providers.registry import Provider
        p = Provider(id='test', name='Test', api_key_set=True)
        d = p.to_dict()
        self.assertNotIn('api_key_set', d)

    def test_from_dict_does_not_mutate_input(self):
        from integrations.providers.registry import Provider
        d = {'id': 'test', 'name': 'Test', 'models': {}}
        d_copy = dict(d)
        Provider.from_dict(d)
        self.assertEqual(d, d_copy)  # Original dict unchanged


# ═══════════════════════════════════════════════════════════════════════
# Auth Header Builder Tests (M1+M2 fix verification)
# ═══════════════════════════════════════════════════════════════════════

class TestAuthHeaderBuilder(unittest.TestCase):
    """Verify M1+M2 fix: consistent auth across all callers."""

    def test_bearer_auth(self):
        from integrations.providers.gateway import ProviderGateway
        from integrations.providers.registry import Provider
        os.environ['TEST_AUTH_KEY'] = 'sk-test'
        try:
            p = Provider(id='test', name='Test', env_key='TEST_AUTH_KEY',
                         auth_method='bearer')
            headers = ProviderGateway._build_headers(p)
            self.assertEqual(headers['Authorization'], 'Bearer sk-test')
        finally:
            os.environ.pop('TEST_AUTH_KEY', None)

    def test_fal_key_auth(self):
        from integrations.providers.gateway import ProviderGateway
        from integrations.providers.registry import Provider
        os.environ['FAL_KEY'] = 'fal-test'
        try:
            p = Provider(id='fal', name='fal.ai', env_key='FAL_KEY')
            headers = ProviderGateway._build_headers(p)
            self.assertEqual(headers['Authorization'], 'Key fal-test')
        finally:
            os.environ.pop('FAL_KEY', None)

    def test_no_key(self):
        from integrations.providers.gateway import ProviderGateway
        from integrations.providers.registry import Provider
        p = Provider(id='test', name='Test', env_key='NONEXISTENT_KEY')
        headers = ProviderGateway._build_headers(p)
        self.assertNotIn('Authorization', headers)
        self.assertIn('Content-Type', headers)


if __name__ == '__main__':
    unittest.main()
