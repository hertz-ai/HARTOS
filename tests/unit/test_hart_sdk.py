"""Tests for hart_sdk — HART OS Developer SDK (WS3)."""

import sys
import os
import unittest
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


# ─── HartApp Builder ────────────────────────────────────────────────

class TestHartAppBuilder(unittest.TestCase):

    def test_basic_creation(self):
        from hart_sdk.app_builder import HartApp
        app = HartApp('my-app')
        self.assertEqual(app._id, 'my-app')
        self.assertEqual(app._name, 'My App')
        self.assertEqual(app._version, '1.0.0')

    def test_custom_name(self):
        from hart_sdk.app_builder import HartApp
        app = HartApp('translator', name='Super Translator', version='2.0.0')
        self.assertEqual(app._name, 'Super Translator')
        self.assertEqual(app._version, '2.0.0')

    def test_needs_ai_fluent(self):
        from hart_sdk.app_builder import HartApp
        app = HartApp('test')
        result = app.needs_ai('llm', min_accuracy=0.7)
        self.assertIs(result, app)  # Fluent returns self
        self.assertEqual(len(app._ai_capabilities), 1)
        self.assertEqual(app._ai_capabilities[0]['type'], 'llm')
        self.assertEqual(app._ai_capabilities[0]['min_accuracy'], 0.7)

    def test_multiple_capabilities(self):
        from hart_sdk.app_builder import HartApp
        app = HartApp('test')
        app.needs_ai('llm').needs_ai('tts', required=False).needs_ai('vision')
        self.assertEqual(len(app._ai_capabilities), 3)
        self.assertFalse(app._ai_capabilities[1]['required'])

    def test_all_builder_methods(self):
        from hart_sdk.app_builder import HartApp
        app = (HartApp('full-app')
               .needs_ai('llm')
               .permissions(['network', 'audio'])
               .group('Productivity')
               .tags(['translate'])
               .icon('translate')
               .description('A translator app')
               .entry(route='/translate')
               .size(1024, 768)
               .depends_on('core-llm')
               .platforms(['linux', 'windows']))

        self.assertEqual(app._permissions, ['network', 'audio'])
        self.assertEqual(app._group, 'Productivity')
        self.assertEqual(app._tags, ['translate'])
        self.assertEqual(app._icon, 'translate')
        self.assertEqual(app._description, 'A translator app')
        self.assertEqual(app._entry, {'route': '/translate'})
        self.assertEqual(app._default_size, (1024, 768))
        self.assertEqual(app._dependencies, ['core-llm'])
        self.assertEqual(app._platforms, ['linux', 'windows'])

    def test_manifest_returns_app_manifest(self):
        from hart_sdk.app_builder import HartApp
        from core.platform.app_manifest import AppManifest
        app = HartApp('test-app', version='1.0.0')
        app.needs_ai('llm')
        app.entry(module='ext.test')
        manifest = app.manifest()
        self.assertIsInstance(manifest, AppManifest)
        self.assertEqual(manifest.id, 'test-app')
        self.assertEqual(len(manifest.ai_capabilities), 1)

    def test_manifest_fallback_dict(self):
        from hart_sdk.app_builder import HartApp
        app = HartApp('test')
        # Simulate ImportError by patching
        with patch.dict('sys.modules', {'core.platform.app_manifest': None}):
            # Need to create a fresh HartApp since module may be cached
            app2 = HartApp.__new__(HartApp)
            app2.__init__('fallback')
            result = app2._to_dict()
            self.assertIsInstance(result, dict)
            self.assertEqual(result['id'], 'fallback')

    def test_register_with_platform(self):
        from hart_sdk.app_builder import HartApp
        from core.platform.registry import get_registry, reset_registry
        reset_registry()
        from core.platform.bootstrap import bootstrap_platform
        registry = bootstrap_platform()

        app = HartApp('sdk-test-app', version='1.0.0')
        app.entry(module='ext.sdk_test')
        result = app.register()
        self.assertTrue(result)

        apps = registry.get('apps')
        found = apps.get('sdk-test-app')
        self.assertIsNotNone(found)
        self.assertEqual(found.name, 'Sdk Test App')
        reset_registry()


# ─── AI Client ──────────────────────────────────────────────────────

class TestAIClient(unittest.TestCase):

    def test_singleton_import(self):
        from hart_sdk.ai_client import ai
        self.assertIsNotNone(ai)

    def test_infer_no_platform(self):
        from hart_sdk.ai_client import AIClient
        client = AIClient()
        with patch.dict('sys.modules', {
            'integrations.agent_engine.model_bus_service': None,
        }):
            result = client.infer('hello')
            self.assertIn('error', result)

    def test_infer_with_mock_bus(self):
        from hart_sdk.ai_client import AIClient
        client = AIClient()
        mock_bus = Mock()
        mock_bus.infer.return_value = {'response': 'world'}
        with patch.dict('sys.modules', {
            'integrations.agent_engine.model_bus_service': Mock(
                get_model_bus_service=Mock(return_value=mock_bus),
            ),
        }):
            result = client.infer('hello')
            self.assertEqual(result['response'], 'world')

    def test_capability_creates_ai_capability(self):
        from hart_sdk.ai_client import AIClient
        from core.platform.ai_capabilities import AICapability
        client = AIClient()
        cap = client.capability('llm', min_accuracy=0.8)
        self.assertIsInstance(cap, AICapability)
        self.assertEqual(cap.type, 'llm')
        self.assertEqual(cap.min_accuracy, 0.8)

    def test_list_models_no_registry(self):
        from hart_sdk.ai_client import AIClient
        client = AIClient()
        with patch.dict('sys.modules', {
            'integrations.agent_engine.model_registry': None,
        }):
            result = client.list_models()
            self.assertEqual(result, [])

    def test_can_satisfy_no_platform(self):
        from hart_sdk.ai_client import AIClient
        client = AIClient()
        # Without bootstrap, should return False
        from core.platform.registry import reset_registry
        reset_registry()
        result = client.can_satisfy([])
        self.assertFalse(result)
        reset_registry()


# ─── Event Client ───────────────────────────────────────────────────

class TestEventClient(unittest.TestCase):

    def test_singleton_import(self):
        from hart_sdk.event_client import events
        self.assertIsNotNone(events)

    @patch('core.platform.events.emit_event')
    def test_emit(self, mock_emit):
        from hart_sdk.event_client import EventClient
        client = EventClient()
        result = client.emit('test.topic', {'key': 'value'})
        self.assertTrue(result)
        mock_emit.assert_called_once_with('test.topic', {'key': 'value'})

    def test_on_with_bootstrap(self):
        from hart_sdk.event_client import EventClient
        from core.platform.registry import reset_registry
        reset_registry()
        from core.platform.bootstrap import bootstrap_platform
        bootstrap_platform()

        client = EventClient()
        callback = Mock()
        result = client.on('test.topic', callback)
        self.assertTrue(result)
        reset_registry()

    def test_off_no_bus(self):
        from hart_sdk.event_client import EventClient
        from core.platform.registry import reset_registry
        reset_registry()
        client = EventClient()
        result = client.off('test.topic', Mock())
        self.assertFalse(result)
        reset_registry()


# ─── Config Client ──────────────────────────────────────────────────

class TestConfigClient(unittest.TestCase):

    def test_singleton_import(self):
        from hart_sdk.config_client import config
        self.assertIsNotNone(config)

    def test_get_no_platform(self):
        from hart_sdk.config_client import ConfigClient
        from core.platform.registry import reset_registry
        reset_registry()
        client = ConfigClient()
        result = client.get('theme.mode', 'dark')
        self.assertEqual(result, 'dark')  # Falls back to default
        reset_registry()

    def test_set_no_platform(self):
        from hart_sdk.config_client import ConfigClient
        from core.platform.registry import reset_registry
        reset_registry()
        client = ConfigClient()
        result = client.set('theme.mode', 'light')
        self.assertFalse(result)
        reset_registry()

    def test_on_change_no_platform(self):
        from hart_sdk.config_client import ConfigClient
        from core.platform.registry import reset_registry
        reset_registry()
        client = ConfigClient()
        result = client.on_change('key', Mock())
        self.assertFalse(result)
        reset_registry()


# ─── Environment Client ────────────────────────────────────────────

class TestEnvironmentClient(unittest.TestCase):

    def test_singleton_import(self):
        from hart_sdk.environment_client import environments
        self.assertIsNotNone(environments)

    def test_create_with_bootstrap(self):
        from hart_sdk.environment_client import EnvironmentClient
        from core.platform.registry import reset_registry
        reset_registry()
        from core.platform.bootstrap import bootstrap_platform
        bootstrap_platform()

        client = EnvironmentClient()
        env = client.create('test-env', model_policy='local_only')
        self.assertIsNotNone(env)
        self.assertEqual(env.name, 'test-env')
        self.assertEqual(env.config.model_policy, 'local_only')
        reset_registry()

    def test_create_no_platform(self):
        from hart_sdk.environment_client import EnvironmentClient
        from core.platform.registry import reset_registry
        reset_registry()
        client = EnvironmentClient()
        result = client.create('test')
        self.assertIsNone(result)
        reset_registry()

    def test_list_all_no_platform(self):
        from hart_sdk.environment_client import EnvironmentClient
        from core.platform.registry import reset_registry
        reset_registry()
        client = EnvironmentClient()
        result = client.list_all()
        self.assertEqual(result, [])
        reset_registry()

    def test_destroy_no_platform(self):
        from hart_sdk.environment_client import EnvironmentClient
        from core.platform.registry import reset_registry
        reset_registry()
        client = EnvironmentClient()
        result = client.destroy('nope')
        self.assertFalse(result)
        reset_registry()


# ─── Platform Detection ────────────────────────────────────────────

class TestPlatformDetect(unittest.TestCase):

    def test_detect_platform_returns_dict(self):
        from hart_sdk.platform_detect import detect_platform
        info = detect_platform()
        self.assertIsInstance(info, dict)
        self.assertIn('arch', info)
        self.assertIn('os', info)
        self.assertIn('python_version', info)
        self.assertIn('gpu', info)
        self.assertIn('capabilities', info)

    def test_arch_normalization(self):
        from hart_sdk.platform_detect import _ARCH_MAP
        self.assertEqual(_ARCH_MAP['amd64'], 'x86_64')
        self.assertEqual(_ARCH_MAP['arm64'], 'aarch64')
        self.assertEqual(_ARCH_MAP['riscv64'], 'riscv64')

    def test_os_matches_platform(self):
        import sys
        from hart_sdk.platform_detect import detect_platform
        info = detect_platform()
        self.assertEqual(info['os'], sys.platform)

    def test_get_capabilities(self):
        from hart_sdk.platform_detect import get_capabilities
        caps = get_capabilities()
        self.assertIsInstance(caps, list)
        self.assertGreater(len(caps), 0)


# ─── Integration Flow ──────────────────────────────────────────────

class TestSDKIntegration(unittest.TestCase):

    def test_full_app_lifecycle(self):
        """Full flow: build app -> register -> query capabilities."""
        from hart_sdk.app_builder import HartApp
        from core.platform.registry import reset_registry
        reset_registry()
        from core.platform.bootstrap import bootstrap_platform
        registry = bootstrap_platform()

        # Build and register
        app = HartApp('sdk-integration-test', version='1.0.0')
        app.needs_ai('llm', min_accuracy=0.5)
        app.needs_ai('tts', required=False)
        app.permissions(['network'])
        app.group('Test')
        app.entry(module='ext.integration_test')
        self.assertTrue(app.register())

        # Verify in registry
        apps = registry.get('apps')
        found = apps.get('sdk-integration-test')
        self.assertIsNotNone(found)
        self.assertEqual(len(found.ai_capabilities), 2)

        # Query by capability
        llm_apps = apps.list_by_capability('llm')
        self.assertTrue(any(a.id == 'sdk-integration-test' for a in llm_apps))

        reset_registry()

    def test_environment_with_sdk(self):
        """Create environment through SDK, verify tool gating."""
        from hart_sdk.environment_client import EnvironmentClient
        from core.platform.registry import reset_registry
        reset_registry()
        from core.platform.bootstrap import bootstrap_platform
        bootstrap_platform()

        client = EnvironmentClient()
        env = client.create('sdk-env-test',
                             allowed_tools=['web_search'],
                             model_policy='local_only')
        self.assertIsNotNone(env)
        self.assertTrue(env.check_tool('web_search'))
        self.assertFalse(env.check_tool('delete_file'))

        # Cleanup
        client.destroy(env.env_id)
        reset_registry()

    def test_events_round_trip(self):
        """Emit and receive events through SDK."""
        from hart_sdk.event_client import EventClient
        from core.platform.registry import reset_registry
        reset_registry()
        from core.platform.bootstrap import bootstrap_platform
        bootstrap_platform()

        client = EventClient()
        received = []
        client.on('sdk.test', lambda topic, d: received.append(d))
        client.emit('sdk.test', {'value': 42})
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['value'], 42)

        reset_registry()


# ─── Graceful Degradation ──────────────────────────────────────────

class TestGracefulDegradation(unittest.TestCase):

    def test_sdk_import_works(self):
        """SDK can be imported without HART OS platform."""
        import hart_sdk
        self.assertIsNotNone(hart_sdk.__version__)

    def test_hart_app_works_standalone(self):
        """HartApp builder works without platform."""
        from hart_sdk.app_builder import HartApp
        app = HartApp('standalone', version='1.0.0')
        app.needs_ai('llm')
        app.entry(module='ext.standalone')
        # manifest() should still work (returns AppManifest or dict)
        m = app.manifest()
        self.assertIsNotNone(m)

    def test_ai_infer_returns_error_without_bus(self):
        from hart_sdk.ai_client import AIClient
        client = AIClient()
        with patch.dict('sys.modules', {
            'integrations.agent_engine.model_bus_service': None,
        }):
            result = client.infer('test')
            self.assertIn('error', result)

    def test_events_emit_returns_false_on_error(self):
        from hart_sdk.event_client import EventClient
        client = EventClient()
        with patch('core.platform.events.emit_event',
                   side_effect=Exception('boom')):
            result = client.emit('test', {})
            self.assertFalse(result)

    def test_config_returns_default_without_platform(self):
        from hart_sdk.config_client import ConfigClient
        from core.platform.registry import reset_registry
        reset_registry()
        client = ConfigClient()
        self.assertEqual(client.get('missing', 'fallback'), 'fallback')
        reset_registry()

    def test_environments_returns_none_without_platform(self):
        from hart_sdk.environment_client import EnvironmentClient
        from core.platform.registry import reset_registry
        reset_registry()
        client = EnvironmentClient()
        self.assertIsNone(client.create('test'))
        self.assertIsNone(client.get('nope'))
        self.assertFalse(client.destroy('nope'))
        self.assertEqual(client.list_all(), [])
        reset_registry()


# ─── Package Structure ──────────────────────────────────────────────

class TestPackageStructure(unittest.TestCase):

    def test_all_exports(self):
        import hart_sdk
        self.assertTrue(hasattr(hart_sdk, 'HartApp'))
        self.assertTrue(hasattr(hart_sdk, 'ai'))
        self.assertTrue(hasattr(hart_sdk, 'events'))
        self.assertTrue(hasattr(hart_sdk, 'config'))
        self.assertTrue(hasattr(hart_sdk, 'environments'))
        self.assertTrue(hasattr(hart_sdk, 'detect_platform'))

    def test_version(self):
        import hart_sdk
        self.assertEqual(hart_sdk.__version__, '0.1.0')


if __name__ == '__main__':
    unittest.main()
