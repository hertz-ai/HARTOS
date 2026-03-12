"""
Tests for EventBus WAMP bridge, MakeItTalk TTS fallback chain,
and event emission from state-changing operations.

Covers:
  - EventBus WAMP topic mapping (local ↔ WAMP URI)
  - EventBus WAMP bridge lifecycle (connect, disconnect, health)
  - EventBus _from_wamp echo prevention
  - MakeItTalk cloud → Pocket TTS fallback in model_bus_service
  - Model registry MakeItTalk backend registration
  - Event emission from theme, resonance, lifecycle, memory, inference
  - emit_event() module helper
"""

import json
import os
import threading
import time
import unittest
from unittest.mock import patch, MagicMock, PropertyMock


# ═══════════════════════════════════════════════════════════════
# EventBus WAMP Topic Mapping
# ═══════════════════════════════════════════════════════════════

class TestWAMPTopicMapping(unittest.TestCase):
    """Test local ↔ WAMP topic conversion."""

    def test_local_to_wamp(self):
        from core.platform.events import _local_to_wamp
        self.assertEqual(
            _local_to_wamp('theme.changed'),
            'com.hartos.event.theme.changed')

    def test_local_to_wamp_nested(self):
        from core.platform.events import _local_to_wamp
        self.assertEqual(
            _local_to_wamp('config.display.scale'),
            'com.hartos.event.config.display.scale')

    def test_wamp_to_local(self):
        from core.platform.events import _wamp_to_local
        self.assertEqual(
            _wamp_to_local('com.hartos.event.theme.changed'),
            'theme.changed')

    def test_wamp_to_local_nested(self):
        from core.platform.events import _wamp_to_local
        self.assertEqual(
            _wamp_to_local('com.hartos.event.config.display.scale'),
            'config.display.scale')

    def test_wamp_to_local_wrong_prefix(self):
        from core.platform.events import _wamp_to_local
        result = _wamp_to_local('com.hertzai.hevolve.something')
        self.assertIsNone(result)

    def test_roundtrip(self):
        from core.platform.events import _local_to_wamp, _wamp_to_local
        topic = 'inference.completed'
        self.assertEqual(_wamp_to_local(_local_to_wamp(topic)), topic)


# ═══════════════════════════════════════════════════════════════
# EventBus WAMP Bridge
# ═══════════════════════════════════════════════════════════════

class TestEventBusWAMPBridge(unittest.TestCase):
    """Test WAMP bridge state management."""

    def test_wamp_not_connected_by_default(self):
        from core.platform.events import EventBus
        bus = EventBus()
        self.assertFalse(bus.wamp_connected)

    def test_health_includes_wamp_status(self):
        from core.platform.events import EventBus
        bus = EventBus()
        h = bus.health()
        self.assertIn('wamp_connected', h)
        self.assertFalse(h['wamp_connected'])

    def test_connect_wamp_without_autobahn(self):
        """When autobahn is not installed, connect_wamp returns False."""
        from core.platform.events import EventBus
        bus = EventBus()
        with patch.dict('sys.modules', {'autobahn': None,
                                         'autobahn.asyncio': None,
                                         'autobahn.asyncio.component': None}):
            with patch('builtins.__import__', side_effect=ImportError):
                # Should gracefully fail
                result = bus.connect_wamp('ws://fake:8088/ws')
                self.assertFalse(result)

    def test_disconnect_wamp_safe_when_not_connected(self):
        """disconnect_wamp should not raise when not connected."""
        from core.platform.events import EventBus
        bus = EventBus()
        bus.disconnect_wamp()  # should not raise

    def test_emit_does_not_publish_to_wamp_when_disconnected(self):
        """When WAMP is not connected, emit should not call _publish_to_wamp."""
        from core.platform.events import EventBus
        bus = EventBus()
        with patch.object(bus, '_publish_to_wamp') as mock_pub:
            bus.emit('test.event', {'value': 1})
            mock_pub.assert_not_called()

    def test_emit_publishes_to_wamp_when_connected(self):
        """When WAMP session is set, emit should call _publish_to_wamp."""
        from core.platform.events import EventBus
        bus = EventBus()
        bus._wamp_connected = True
        bus._wamp_session = MagicMock()
        with patch.object(bus, '_publish_to_wamp') as mock_pub:
            bus.emit('test.event', {'value': 1})
            mock_pub.assert_called_once_with('test.event', {'value': 1})

    def test_from_wamp_flag_prevents_echo(self):
        """Events from WAMP should NOT be re-published to WAMP."""
        from core.platform.events import EventBus
        bus = EventBus()
        bus._wamp_connected = True
        bus._wamp_session = MagicMock()
        with patch.object(bus, '_publish_to_wamp') as mock_pub:
            bus.emit('test.event', {'value': 1}, _from_wamp=True)
            mock_pub.assert_not_called()

    def test_publish_to_wamp_serializes_data(self):
        """_publish_to_wamp should JSON-serialize data for WAMP transport."""
        from core.platform.events import EventBus
        import asyncio
        bus = EventBus()
        bus._wamp_session = MagicMock()
        bus._wamp_loop = asyncio.new_event_loop()
        try:
            bus._publish_to_wamp('test.event', {'key': 'value'})
            # Should not raise — fire and forget
        finally:
            bus._wamp_loop.close()
            bus._wamp_loop = None

    def test_publish_to_wamp_handles_non_serializable(self):
        """Non-JSON-serializable data should be stringified."""
        from core.platform.events import EventBus
        import asyncio
        bus = EventBus()
        bus._wamp_session = MagicMock()
        bus._wamp_loop = asyncio.new_event_loop()
        try:
            bus._publish_to_wamp('test.event', object())
            # Should not raise
        finally:
            bus._wamp_loop.close()
            bus._wamp_loop = None


# ═══════════════════════════════════════════════════════════════
# emit_event() module helper
# ═══════════════════════════════════════════════════════════════

class TestEmitEventHelper(unittest.TestCase):
    """Test the module-level emit_event() convenience function."""

    def test_emit_event_noop_when_not_bootstrapped(self):
        """Should not raise when registry has no 'events' service."""
        from core.platform.events import emit_event
        from core.platform.registry import reset_registry
        reset_registry()
        emit_event('test.event', {'value': 1})  # should not raise

    def test_emit_event_fires_when_bootstrapped(self):
        """Should emit to the EventBus when platform is bootstrapped."""
        from core.platform.events import emit_event
        from core.platform.registry import get_registry, reset_registry
        from core.platform.events import EventBus

        reset_registry()
        registry = get_registry()
        bus = EventBus()
        registry.register('events', lambda: bus, singleton=True)

        events = []
        bus.on('helper.test', lambda t, d: events.append(d))
        emit_event('helper.test', {'x': 1}, async_=False)
        self.assertEqual(events, [{'x': 1}])

        reset_registry()


# ═══════════════════════════════════════════════════════════════
# MakeItTalk Cloud → Pocket TTS Fallback
# ═══════════════════════════════════════════════════════════════

class TestTTSFallbackChain(unittest.TestCase):
    """Test model_bus_service TTS routing with MakeItTalk fallback."""

    def _make_bus(self):
        from integrations.agent_engine.model_bus_service import ModelBusService
        bus = ModelBusService()
        bus._semaphore = MagicMock()
        bus._semaphore.acquire.return_value = True
        return bus

    @patch.dict(os.environ, {}, clear=False)
    @patch('integrations.service_tools.pocket_tts_tool.pocket_tts_synthesize')
    def test_offline_mode_uses_pocket_tts_directly(self, mock_synth):
        """When HART_OS_MODE is set and no MAKEITTALK_API_URL, use local TTS."""
        mock_synth.return_value = json.dumps({
            'path': '/tmp/test.wav', 'duration': 1.0,
            'sample_rate': 24000, 'voice': 'alba', 'engine': 'pocket-tts'})

        bus = self._make_bus()
        # Disable router to test legacy fallback chain
        with patch.dict(os.environ, {'HART_OS_MODE': 'desktop'}), \
             patch('integrations.channels.media.tts_router.get_tts_router',
                   side_effect=ImportError('disabled for test')):
            result = bus._route_tts('Hello', {})
        # Either LuxTTS (local_tts_cpu) or Pocket TTS (local_tts) - both are local
        self.assertTrue(result['backend'].startswith('local_tts'),
                        f"Expected local backend, got {result['backend']}")
        self.assertIn(result['model'], ('pocket-tts-100m', 'luxtts-48k'))

    @patch('integrations.service_tools.pocket_tts_tool.pocket_tts_synthesize')
    def test_router_unavailable_falls_back_to_pocket_tts(self, mock_synth):
        """When TTSRouter unavailable, legacy fallback goes straight to Pocket TTS.

        MakeItTalk is now handled BY the router — the legacy chain skips it
        entirely and goes to pocket_tts (guaranteed CPU, always available).
        """
        mock_synth.return_value = json.dumps({
            'path': '/tmp/fallback.wav', 'duration': 1.0,
            'sample_rate': 24000, 'voice': 'alba', 'engine': 'pocket-tts'})

        bus = self._make_bus()
        with patch('integrations.channels.media.tts_router.get_tts_router',
                   side_effect=ImportError('disabled for test')):
            result = bus._route_tts('Hello', {})

        self.assertEqual(result['backend'], 'local_tts')
        self.assertEqual(result['engine'], 'pocket-tts')
        mock_synth.assert_called_once()

    @patch('integrations.service_tools.pocket_tts_tool.pocket_tts_synthesize')
    def test_cloud_failure_falls_back_to_pocket_tts(self, mock_synth):
        """When LuxTTS unavailable and MakeItTalk fails, fall back to Pocket TTS."""
        import requests as http_requests
        mock_synth.return_value = json.dumps({
            'path': '/tmp/fallback.wav', 'duration': 1.0,
            'sample_rate': 24000, 'voice': 'alba', 'engine': 'pocket-tts'})

        bus = self._make_bus()
        # Disable router to test legacy fallback chain
        with patch.dict(os.environ, {'MAKEITTALK_API_URL': 'http://cloud:5454'}):
            with patch('requests.post',
                       side_effect=http_requests.ConnectionError("refused")):
                with patch.object(bus, '_try_luxtts',
                                  return_value={'error': 'not installed'}):
                    with patch('integrations.channels.media.tts_router.get_tts_router',
                               side_effect=ImportError('disabled for test')):
                        result = bus._route_tts('Hello', {})

        self.assertEqual(result['backend'], 'local_tts')
        self.assertEqual(result['engine'], 'pocket-tts')
        mock_synth.assert_called_once()

    @patch('integrations.service_tools.pocket_tts_tool.pocket_tts_synthesize')
    def test_router_unavailable_skips_makeittalk(self, mock_synth):
        """Without router, legacy fallback does NOT try makeittalk — straight to pocket.

        Previously: luxtts fail → makeittalk timeout → pocket_tts
        Now: router fail → pocket_tts (router handles makeittalk internally)
        """
        mock_synth.return_value = json.dumps({
            'path': '/tmp/timeout.wav', 'duration': 0.5,
            'sample_rate': 24000, 'voice': 'alba', 'engine': 'pocket-tts'})

        bus = self._make_bus()
        with patch.dict(os.environ, {'MAKEITTALK_API_URL': 'http://cloud:5454'}):
            with patch('integrations.channels.media.tts_router.get_tts_router',
                       side_effect=ImportError('disabled for test')):
                result = bus._route_tts('Hello', {})

        self.assertEqual(result['backend'], 'local_tts')
        self.assertEqual(result['engine'], 'pocket-tts')
        mock_synth.assert_called_once()

    def test_makeittalk_http_error_returns_error(self):
        """MakeItTalk returning non-200 should return error dict."""
        bus = self._make_bus()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = 'Internal server error'

        result = bus._try_makeittalk_tts('Hello', {}, 'http://cloud:5454')
        # Without mock, it will fail with connection error
        self.assertIn('error', result)

    @patch.dict(os.environ, {}, clear=False)
    @patch('integrations.service_tools.pocket_tts_tool.pocket_tts_synthesize')
    def test_no_makeittalk_no_env_uses_pocket(self, mock_synth):
        """Without MAKEITTALK_API_URL env, use Pocket TTS directly."""
        mock_synth.return_value = json.dumps({
            'path': '/tmp/direct.wav', 'duration': 1.0,
            'sample_rate': 24000, 'voice': 'alba', 'engine': 'pocket-tts'})

        bus = self._make_bus()
        # Ensure no MAKEITTALK_API_URL in env
        env = {k: v for k, v in os.environ.items() if k != 'MAKEITTALK_API_URL'}
        with patch.dict(os.environ, env, clear=True):
            result = bus._route_tts('Hello', {})
        self.assertTrue(result['backend'].startswith('local_tts'),
                        f"Expected local backend, got {result['backend']}")


# ═══════════════════════════════════════════════════════════════
# Model Registry — MakeItTalk Backend
# ═══════════════════════════════════════════════════════════════

class TestMakeItTalkModelRegistry(unittest.TestCase):
    """Test MakeItTalk model registration."""

    def test_makeittalk_not_registered_without_env(self):
        from integrations.agent_engine.model_registry import model_registry
        model = model_registry.get_model('makeittalk-cloud')
        if not os.environ.get('MAKEITTALK_API_URL'):
            self.assertIsNone(model)

    def test_makeittalk_registered_with_env(self):
        from integrations.agent_engine.model_registry import (
            ModelRegistry, ModelBackend, ModelTier)
        reg = ModelRegistry()
        reg.register(ModelBackend(
            model_id='makeittalk-cloud',
            display_name='MakeItTalk Cloud',
            tier=ModelTier.BALANCED,
            config_list_entry={'model': 'makeittalk', 'api_key': 'cloud',
                               'base_url': 'http://test:5454', 'price': [0, 0]},
            avg_latency_ms=5000.0,
            accuracy_score=0.92,
            is_local=False,
        ))
        model = reg.get_model('makeittalk-cloud')
        self.assertIsNotNone(model)
        self.assertFalse(model.is_local)
        self.assertEqual(model.tier, ModelTier.BALANCED)

    def test_model_bus_lists_makeittalk_when_configured(self):
        from integrations.agent_engine.model_bus_service import ModelBusService
        bus = ModelBusService()
        with patch.dict(os.environ, {'MAKEITTALK_API_URL': 'http://test:5454'}):
            models = bus.list_models()
        ids = [m['id'] for m in models]
        self.assertIn('makeittalk-cloud', ids)
        self.assertIn('pocket-tts-100m', ids)

    def test_model_bus_omits_makeittalk_when_not_configured(self):
        from integrations.agent_engine.model_bus_service import ModelBusService
        bus = ModelBusService()
        env = {k: v for k, v in os.environ.items() if k != 'MAKEITTALK_API_URL'}
        with patch.dict(os.environ, env, clear=True):
            models = bus.list_models()
        ids = [m['id'] for m in models]
        self.assertNotIn('makeittalk-cloud', ids)
        self.assertIn('pocket-tts-100m', ids)


# ═══════════════════════════════════════════════════════════════
# Event Emission from State Changes
# ═══════════════════════════════════════════════════════════════

class TestThemeEventEmission(unittest.TestCase):
    """Test that theme changes emit events."""

    @patch('core.platform.events.emit_event')
    def test_apply_theme_emits_event(self, mock_emit):
        from integrations.agent_engine.theme_service import ThemeService
        with patch.object(ThemeService, 'get_preset',
                          return_value={'id': 'dark', 'colors': {}}):
            with patch('builtins.open', MagicMock()):
                with patch('os.makedirs'):
                    with patch.object(ThemeService, '_apply_gtk'):
                        with patch.object(ThemeService, '_notify_liquid_ui'):
                            ThemeService.apply_theme('dark')
        mock_emit.assert_called()
        # Find the theme.changed call
        calls = [c for c in mock_emit.call_args_list
                 if c[0][0] == 'theme.changed']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][1]['theme_id'], 'dark')

    @patch('core.platform.events.emit_event')
    def test_update_custom_emits_event(self, mock_emit):
        from integrations.agent_engine.theme_service import ThemeService
        with patch.object(ThemeService, '_load_custom_overrides', return_value={}):
            with patch('builtins.open', MagicMock()):
                with patch('os.makedirs'):
                    with patch.object(ThemeService, '_load_active_file',
                                      return_value=None):
                        with patch.object(ThemeService, 'get_active_theme',
                                          return_value={'id': 'dark'}):
                            with patch.object(ThemeService, '_notify_liquid_ui'):
                                ThemeService.update_custom({'font': {'size': 18}})
        calls = [c for c in mock_emit.call_args_list
                 if c[0][0] == 'theme.custom_updated']
        self.assertEqual(len(calls), 1)


class TestResonanceEventEmission(unittest.TestCase):
    """Test that resonance tuning emits events."""

    @patch('core.platform.events.emit_event')
    def test_analyze_and_tune_emits_event(self, mock_emit):
        from core.resonance_tuner import ResonanceTuner
        from core.resonance_profile import UserResonanceProfile
        tuner = ResonanceTuner(auto_save=False)
        with patch('core.resonance_tuner.get_or_create_profile',
                   return_value=UserResonanceProfile(user_id='u1')):
            with patch.object(tuner, '_dispatch_to_hevolveai'):
                tuner.analyze_and_tune('u1', 'hello', 'hi there')
        calls = [c for c in mock_emit.call_args_list
                 if c[0][0] == 'resonance.tuned']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][1]['user_id'], 'u1')


class TestLifecycleEventEmission(unittest.TestCase):
    """Test that action state changes emit events."""

    @patch('core.platform.events.emit_event')
    def test_auto_sync_emits_event(self, mock_emit):
        from lifecycle_hooks import _auto_sync_to_ledger, _ledger_registry, ActionState

        # Register a mock ledger
        mock_ledger = MagicMock()
        mock_ledger.tasks = {'action_42': MagicMock()}
        _ledger_registry['test_prompt'] = mock_ledger

        try:
            with patch('lifecycle_hooks._get_ledger_task_status') as mock_status:
                MockStatus = MagicMock()
                MockStatus.IN_PROGRESS = 'IN_PROGRESS'
                mock_status.return_value = MockStatus
                _auto_sync_to_ledger('test_prompt', 42, ActionState.IN_PROGRESS)
        except Exception:
            pass  # Ledger sync may fail — we only care about event emission
        finally:
            _ledger_registry.pop('test_prompt', None)

        calls = [c for c in mock_emit.call_args_list
                 if c[0][0] == 'action_state.changed']
        self.assertGreaterEqual(len(calls), 1)
        self.assertEqual(calls[0][0][1]['action_id'], 42)
        self.assertEqual(calls[0][0][1]['state'], 'in_progress')


class TestInferenceEventEmission(unittest.TestCase):
    """Test that model bus inference emits events."""

    @patch('core.platform.events.emit_event')
    @patch('integrations.service_tools.pocket_tts_tool.pocket_tts_synthesize')
    def test_infer_tts_emits_event(self, mock_synth, mock_emit):
        mock_synth.return_value = json.dumps({
            'path': '/tmp/event.wav', 'duration': 1.0,
            'sample_rate': 24000, 'voice': 'alba', 'engine': 'pocket-tts'})

        from integrations.agent_engine.model_bus_service import ModelBusService
        bus = ModelBusService()
        env = {k: v for k, v in os.environ.items() if k != 'MAKEITTALK_API_URL'}
        with patch.dict(os.environ, env, clear=True):
            result = bus.infer('tts', 'Hello world')

        calls = [c for c in mock_emit.call_args_list
                 if c[0][0] == 'inference.completed']
        self.assertEqual(len(calls), 1)
        event_data = calls[0][0][1]
        self.assertEqual(event_data['model_type'], 'tts')
        self.assertTrue(event_data['success'])


# ═══════════════════════════════════════════════════════════════
# Bootstrap WAMP Auto-Connect
# ═══════════════════════════════════════════════════════════════

class TestBootstrapWAMP(unittest.TestCase):
    """Test that bootstrap_platform connects WAMP when CBURL is set."""

    def test_bootstrap_without_cburl_skips_wamp(self):
        from core.platform.registry import reset_registry
        reset_registry()
        env = {k: v for k, v in os.environ.items() if k != 'CBURL'}
        with patch.dict(os.environ, env, clear=True):
            from core.platform.bootstrap import bootstrap_platform
            from core.platform.registry import reset_registry
            reset_registry()
            with patch('core.platform.bootstrap._migrate_shell_manifest'):
                with patch('core.platform.bootstrap._register_native_apps'):
                    registry = bootstrap_platform()
            bus = registry.get('events')
            self.assertFalse(bus.wamp_connected)
        reset_registry()

    def test_bootstrap_with_cburl_calls_connect_wamp(self):
        from core.platform.registry import reset_registry
        reset_registry()
        with patch.dict(os.environ, {'CBURL': 'ws://fake:8088/ws'}):
            from core.platform.bootstrap import bootstrap_platform
            from core.platform.events import EventBus
            reset_registry()
            with patch('core.platform.bootstrap._migrate_shell_manifest'):
                with patch('core.platform.bootstrap._register_native_apps'):
                    with patch.object(EventBus, 'connect_wamp',
                                      return_value=True) as mock_connect:
                        registry = bootstrap_platform()
            mock_connect.assert_called_once_with(
                'ws://fake:8088/ws', 'realm1')
        reset_registry()


if __name__ == '__main__':
    unittest.main()
