"""
Tests for AI Capability Intents.

Covers: AICapability dataclass, ResolvedCapability, CapabilityRouter,
AppManifest.ai_capabilities field, AppRegistry.list_by_capability().
"""

import os
import sys
import unittest
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.platform.ai_capabilities import (
    AICapability,
    AICapabilityType,
    ResolvedCapability,
    CapabilityRouter,
)
from core.platform.app_manifest import AppManifest, AppType
from core.platform.app_registry import AppRegistry


# ─── AICapability Dataclass ──────────────────────────────────────

class TestAICapability(unittest.TestCase):

    def test_creation_defaults(self):
        cap = AICapability(type='llm')
        self.assertEqual(cap.type, 'llm')
        self.assertTrue(cap.required)
        self.assertFalse(cap.local_only)
        self.assertEqual(cap.min_accuracy, 0.0)
        self.assertEqual(cap.max_latency_ms, 0.0)
        self.assertEqual(cap.max_cost_spark, 0.0)
        self.assertEqual(cap.options, {})

    def test_creation_custom(self):
        cap = AICapability(
            type='tts', required=False, local_only=True,
            min_accuracy=0.8, max_latency_ms=2000,
            options={'voice': 'alba'}
        )
        self.assertEqual(cap.type, 'tts')
        self.assertFalse(cap.required)
        self.assertTrue(cap.local_only)
        self.assertEqual(cap.options['voice'], 'alba')

    def test_to_dict(self):
        cap = AICapability(type='vision', min_accuracy=0.5)
        d = cap.to_dict()
        self.assertEqual(d['type'], 'vision')
        self.assertEqual(d['min_accuracy'], 0.5)
        self.assertTrue(d['required'])
        self.assertIn('options', d)

    def test_from_dict(self):
        data = {'type': 'stt', 'required': False, 'local_only': True}
        cap = AICapability.from_dict(data)
        self.assertEqual(cap.type, 'stt')
        self.assertFalse(cap.required)
        self.assertTrue(cap.local_only)

    def test_from_dict_ignores_unknown_keys(self):
        data = {'type': 'llm', 'unknown_field': 42}
        cap = AICapability.from_dict(data)
        self.assertEqual(cap.type, 'llm')

    def test_roundtrip(self):
        original = AICapability(type='code', min_accuracy=0.9, max_cost_spark=5.0)
        restored = AICapability.from_dict(original.to_dict())
        self.assertEqual(original.type, restored.type)
        self.assertEqual(original.min_accuracy, restored.min_accuracy)
        self.assertEqual(original.max_cost_spark, restored.max_cost_spark)


class TestAICapabilityType(unittest.TestCase):

    def test_all_types_defined(self):
        types = [t.value for t in AICapabilityType]
        self.assertIn('llm', types)
        self.assertIn('vision', types)
        self.assertIn('tts', types)
        self.assertIn('stt', types)
        self.assertIn('image_gen', types)
        self.assertIn('embedding', types)
        self.assertIn('code', types)

    def test_from_string(self):
        self.assertEqual(AICapabilityType('llm'), AICapabilityType.LLM)
        self.assertEqual(AICapabilityType('tts'), AICapabilityType.TTS)


# ─── ResolvedCapability ─────────────────────────────────────────

class TestResolvedCapability(unittest.TestCase):

    def test_creation(self):
        r = ResolvedCapability(
            capability_type='llm', model_id='qwen3.5-4b',
            backend='local', is_local=True,
            estimated_latency_ms=100, estimated_cost_spark=0,
            available=True,
        )
        self.assertTrue(r.available)
        self.assertTrue(r.is_local)
        self.assertEqual(r.reason, '')

    def test_unavailable_with_reason(self):
        r = ResolvedCapability(
            capability_type='vision', model_id='', backend='',
            is_local=False, estimated_latency_ms=0,
            estimated_cost_spark=0, available=False,
            reason='no vision model installed',
        )
        self.assertFalse(r.available)
        self.assertIn('vision', r.reason)

    def test_to_dict(self):
        r = ResolvedCapability(
            capability_type='tts', model_id='pocket-tts',
            backend='local_tts', is_local=True,
            estimated_latency_ms=200, estimated_cost_spark=0,
            available=True,
        )
        d = r.to_dict()
        self.assertEqual(d['capability_type'], 'tts')
        self.assertEqual(d['model_id'], 'pocket-tts')
        self.assertTrue(d['available'])


# ─── CapabilityRouter ────────────────────────────────────────────

class TestCapabilityRouter(unittest.TestCase):

    def _make_model(self, model_id='test-model', is_local=True,
                    cost=0.0, latency=100, accuracy=0.8):
        m = Mock()
        m.model_id = model_id
        m.is_local = is_local
        m.cost_per_1k_tokens = cost
        m.avg_latency_ms = latency
        m.accuracy_score = accuracy
        return m

    def test_resolve_no_registry(self):
        router = CapabilityRouter(model_registry=None)
        result = router.resolve(AICapability(type='llm'))
        self.assertFalse(result.available)
        self.assertIn('no model registry', result.reason)

    def test_resolve_local_model(self):
        model = self._make_model('local-llm', is_local=True, cost=0)
        registry = Mock()
        registry.get_model_by_policy.return_value = model
        router = CapabilityRouter(model_registry=registry)

        result = router.resolve(AICapability(type='llm'))
        self.assertTrue(result.available)
        self.assertEqual(result.model_id, 'local-llm')
        self.assertTrue(result.is_local)
        self.assertEqual(result.backend, 'local')

    def test_resolve_cloud_model(self):
        model = self._make_model('gpt-4', is_local=False, cost=6.0)
        registry = Mock()
        registry.get_model_by_policy.return_value = model
        router = CapabilityRouter(model_registry=registry)

        result = router.resolve(AICapability(type='llm', min_accuracy=0.9))
        self.assertTrue(result.available)
        self.assertEqual(result.backend, 'cloud')
        self.assertFalse(result.is_local)

    def test_resolve_no_matching_model(self):
        registry = Mock()
        registry.get_model_by_policy.return_value = None
        router = CapabilityRouter(model_registry=registry)

        result = router.resolve(AICapability(type='vision'))
        self.assertFalse(result.available)
        self.assertIn('no vision model', result.reason)

    def test_local_only_constraint(self):
        registry = Mock()
        registry.get_model_by_policy.return_value = None
        router = CapabilityRouter(model_registry=registry)

        router.resolve(AICapability(type='llm', local_only=True))
        registry.get_model_by_policy.assert_called_with(
            policy='local_only', min_accuracy=0.0)

    def test_zero_cost_forces_local_only(self):
        registry = Mock()
        registry.get_model_by_policy.return_value = None
        router = CapabilityRouter(model_registry=registry)

        router.resolve(AICapability(type='llm', max_cost_spark=0))
        registry.get_model_by_policy.assert_called_with(
            policy='local_only', min_accuracy=0.0)

    def test_high_accuracy_uses_any_policy(self):
        registry = Mock()
        registry.get_model_by_policy.return_value = None
        router = CapabilityRouter(model_registry=registry)

        router.resolve(AICapability(type='llm', min_accuracy=0.8,
                                     max_cost_spark=10))
        registry.get_model_by_policy.assert_called_with(
            policy='any', min_accuracy=0.8)

    def test_latency_constraint_rejects(self):
        model = self._make_model(latency=5000)
        registry = Mock()
        registry.get_model_by_policy.return_value = model
        router = CapabilityRouter(model_registry=registry)

        result = router.resolve(AICapability(type='llm', max_latency_ms=1000))
        self.assertFalse(result.available)
        self.assertIn('latency', result.reason)

    def test_cost_constraint_rejects(self):
        model = self._make_model(cost=10.0)
        registry = Mock()
        registry.get_model_by_policy.return_value = model
        router = CapabilityRouter(model_registry=registry)

        result = router.resolve(AICapability(type='llm', max_cost_spark=5.0))
        self.assertFalse(result.available)
        self.assertIn('cost', result.reason)

    def test_resolve_all(self):
        model = self._make_model()
        registry = Mock()
        registry.get_model_by_policy.return_value = model
        router = CapabilityRouter(model_registry=registry)

        caps = [AICapability(type='llm'), AICapability(type='tts')]
        results = router.resolve_all(caps)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.available for r in results))

    def test_can_satisfy_all_required(self):
        model = self._make_model()
        registry = Mock()
        registry.get_model_by_policy.return_value = model
        router = CapabilityRouter(model_registry=registry)

        caps = [AICapability(type='llm'), AICapability(type='tts')]
        self.assertTrue(router.can_satisfy(caps))

    def test_can_satisfy_missing_required(self):
        registry = Mock()
        registry.get_model_by_policy.return_value = None
        router = CapabilityRouter(model_registry=registry)

        caps = [AICapability(type='llm', required=True)]
        self.assertFalse(router.can_satisfy(caps))

    def test_can_satisfy_ignores_optional(self):
        registry = Mock()
        registry.get_model_by_policy.return_value = None
        router = CapabilityRouter(model_registry=registry)

        caps = [AICapability(type='llm', required=False)]
        self.assertTrue(router.can_satisfy(caps))

    def test_can_satisfy_empty_list(self):
        router = CapabilityRouter()
        self.assertTrue(router.can_satisfy([]))

    def test_vram_check_does_not_block(self):
        model = self._make_model(is_local=True)
        registry = Mock()
        registry.get_model_by_policy.return_value = model
        vm = Mock()
        vm.detect_gpu.return_value = {'free_mb': 100}
        router = CapabilityRouter(model_registry=registry, vram_manager=vm)

        result = router.resolve(AICapability(type='llm'))
        self.assertTrue(result.available)  # Low VRAM warns but doesn't block

    def test_registry_exception_handled(self):
        registry = Mock()
        registry.get_model_by_policy.side_effect = RuntimeError("boom")
        router = CapabilityRouter(model_registry=registry)

        result = router.resolve(AICapability(type='llm'))
        self.assertFalse(result.available)

    def test_health(self):
        router = CapabilityRouter(model_registry=Mock(), vram_manager=Mock())
        h = router.health()
        self.assertEqual(h['status'], 'ok')
        self.assertTrue(h['has_model_registry'])
        self.assertTrue(h['has_vram_manager'])

    def test_health_no_deps(self):
        router = CapabilityRouter()
        h = router.health()
        self.assertFalse(h['has_model_registry'])
        self.assertFalse(h['has_vram_manager'])


# ─── AppManifest.ai_capabilities ─────────────────────────────────

class TestAppManifestAICapabilities(unittest.TestCase):

    def test_default_empty(self):
        m = AppManifest(id='test', name='Test', version='1.0', type='extension')
        self.assertEqual(m.ai_capabilities, [])

    def test_with_capabilities(self):
        caps = [
            AICapability(type='llm').to_dict(),
            AICapability(type='tts', required=False).to_dict(),
        ]
        m = AppManifest(id='ai_app', name='AI App', version='1.0',
                        type='extension', ai_capabilities=caps)
        self.assertEqual(len(m.ai_capabilities), 2)
        self.assertEqual(m.ai_capabilities[0]['type'], 'llm')

    def test_to_dict_includes_capabilities(self):
        caps = [AICapability(type='vision').to_dict()]
        m = AppManifest(id='v', name='V', version='1', type='agent',
                        ai_capabilities=caps)
        d = m.to_dict()
        self.assertIn('ai_capabilities', d)
        self.assertEqual(len(d['ai_capabilities']), 1)

    def test_from_dict_with_capabilities(self):
        data = {
            'id': 'x', 'name': 'X', 'version': '1', 'type': 'service',
            'ai_capabilities': [{'type': 'stt'}],
        }
        m = AppManifest.from_dict(data)
        self.assertEqual(len(m.ai_capabilities), 1)

    def test_from_dict_without_capabilities(self):
        data = {'id': 'y', 'name': 'Y', 'version': '1', 'type': 'channel'}
        m = AppManifest.from_dict(data)
        self.assertEqual(m.ai_capabilities, [])

    def test_backward_compat_panel_manifest(self):
        m = AppManifest.from_panel_manifest('feed', {
            'title': 'Feed', 'icon': 'rss', 'route': '/social'
        })
        self.assertEqual(m.ai_capabilities, [])


# ─── AppRegistry.list_by_capability ──────────────────────────────

class TestAppRegistryListByCapability(unittest.TestCase):

    def test_finds_matching_apps(self):
        reg = AppRegistry()
        reg.register(AppManifest(
            id='translator', name='Translator', version='1.0.0',
            type='extension', entry={'module': 'ext.translator'},
            ai_capabilities=[{'type': 'llm'}, {'type': 'tts'}]
        ))
        reg.register(AppManifest(
            id='player', name='Player', version='1.0.0',
            type='extension', entry={'module': 'ext.player'},
        ))

        llm_apps = reg.list_by_capability('llm')
        self.assertEqual(len(llm_apps), 1)
        self.assertEqual(llm_apps[0].id, 'translator')

    def test_returns_empty_for_no_match(self):
        reg = AppRegistry()
        reg.register(AppManifest(
            id='basic', name='Basic', version='1.0.0',
            type='extension', entry={'module': 'ext.basic'},
        ))
        self.assertEqual(reg.list_by_capability('vision'), [])

    def test_multiple_apps_same_capability(self):
        reg = AppRegistry()
        reg.register(AppManifest(
            id='a1', name='A1', version='1.0.0', type='extension',
            entry={'module': 'ext.a1'},
            ai_capabilities=[{'type': 'tts'}]
        ))
        reg.register(AppManifest(
            id='a2', name='A2', version='1.0.0', type='extension',
            entry={'module': 'ext.a2'},
            ai_capabilities=[{'type': 'tts'}, {'type': 'stt'}]
        ))
        self.assertEqual(len(reg.list_by_capability('tts')), 2)


# ─── Event Emission ──────────────────────────────────────────────

class TestCapabilityEvents(unittest.TestCase):

    @patch('core.platform.events.emit_event')
    def test_resolve_emits_event(self, mock_emit):
        from core.platform.ai_capabilities import CapabilityRouter, AICapability
        model = Mock(model_id='test', is_local=True, cost_per_1k_tokens=0,
                     avg_latency_ms=100, accuracy_score=0.8)
        registry = Mock()
        registry.get_model_by_policy.return_value = model
        router = CapabilityRouter(model_registry=registry)

        router.resolve(AICapability(type='llm'))
        # Should have emitted capability.resolved
        mock_emit.assert_called()
        call_args = mock_emit.call_args
        self.assertEqual(call_args[0][0], 'capability.resolved')

    @patch('core.platform.events.emit_event')
    def test_unavailable_emits_event(self, mock_emit):
        from core.platform.ai_capabilities import CapabilityRouter, AICapability
        registry = Mock()
        registry.get_model_by_policy.return_value = None
        router = CapabilityRouter(model_registry=registry)

        router.resolve(AICapability(type='vision'))
        mock_emit.assert_called()
        call_args = mock_emit.call_args
        self.assertEqual(call_args[0][0], 'capability.unavailable')


# ─── Bootstrap Registration ──────────────────────────────────────

class TestBootstrapCapabilityRouter(unittest.TestCase):

    def test_bootstrap_registers_router(self):
        from core.platform.registry import get_registry, reset_registry
        reset_registry()
        from core.platform.bootstrap import bootstrap_platform
        registry = bootstrap_platform()
        self.assertTrue(registry.has('capability_router'))
        router = registry.get('capability_router')
        self.assertIsInstance(router, CapabilityRouter)
        reset_registry()

    def test_router_health_after_bootstrap(self):
        from core.platform.registry import get_registry, reset_registry
        reset_registry()
        from core.platform.bootstrap import bootstrap_platform
        registry = bootstrap_platform()
        router = registry.get('capability_router')
        h = router.health()
        self.assertEqual(h['status'], 'ok')
        reset_registry()


if __name__ == '__main__':
    unittest.main()
