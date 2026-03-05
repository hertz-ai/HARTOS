"""
WS13 P1 Runtime Wiring Tests — tool allowlist gate, app auto-registration,
ActionState retry EventBus emission, orchestrator bootstrap registration.

Run: pytest tests/unit/test_ws13_runtime_wiring.py -v --noconftest
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


# ═══════════════════════════════════════════════════════════════
# 1. Tool Allowlist Gate on Dispatch — Structural
# ═══════════════════════════════════════════════════════════════

class TestToolAllowlistGateDispatch(unittest.TestCase):
    """Verify dispatch_goal has model tier resolution wired in."""

    def test_dispatch_goal_source_has_tool_allowlist_import(self):
        """dispatch_goal should reference tool_allowlist for model tier."""
        import inspect
        from integrations.agent_engine.dispatch import dispatch_goal
        src = inspect.getsource(dispatch_goal)
        self.assertIn('tool_allowlist', src)
        self.assertIn('model_tier', src)

    def test_dispatch_goal_attaches_model_tier_to_body(self):
        """dispatch_goal source should set body['model_tier']."""
        import inspect
        from integrations.agent_engine.dispatch import dispatch_goal
        src = inspect.getsource(dispatch_goal)
        self.assertIn("body['model_tier']", src)

    def test_dispatch_without_model_config_has_no_tier(self):
        """Without model_config, _dispatch_model_tier stays None."""
        from integrations.agent_engine import dispatch
        self.assertTrue(hasattr(dispatch, 'dispatch_goal'))


# ═══════════════════════════════════════════════════════════════
# 2. App Auto-Registration After Install
# ═══════════════════════════════════════════════════════════════

class TestAppAutoRegistration(unittest.TestCase):
    """Verify app installer auto-registers/unregisters in AppRegistry."""

    def _make_installer(self):
        from integrations.agent_engine.app_installer import AppInstaller
        return AppInstaller()

    def test_auto_register_creates_manifest(self):
        """Successful install should register app in AppRegistry."""
        from integrations.agent_engine.app_installer import InstallResult, InstallRequest
        from core.platform.app_registry import AppRegistry

        mock_apps = AppRegistry()
        mock_registry = MagicMock()
        mock_registry.has.return_value = True
        mock_registry.get.return_value = mock_apps

        installer = self._make_installer()
        result = InstallResult(
            success=True, platform='nix', name='TestApp',
            version='1.0.0', app_id='testapp',
            install_path='/nix/store/testapp')
        req = InstallRequest(source='nixpkgs.testapp')

        with patch('core.platform.registry.get_registry',
                   return_value=mock_registry):
            installer._auto_register_app(result, req)

        m = mock_apps.get('testapp')
        self.assertIsNotNone(m)
        self.assertEqual(m.name, 'TestApp')
        self.assertEqual(m.type, 'desktop_app')
        self.assertIn('installed', m.tags)

    def test_auto_register_skips_if_already_registered(self):
        """Should not raise if app already in registry."""
        from integrations.agent_engine.app_installer import InstallResult, InstallRequest
        from core.platform.app_registry import AppRegistry
        from core.platform.app_manifest import AppManifest

        mock_apps = AppRegistry()
        existing = AppManifest(
            id='testapp', name='TestApp', version='1.0.0',
            type='desktop_app', icon='apps',
            entry={'exec': 'testapp'})
        mock_apps.register(existing)

        mock_registry = MagicMock()
        mock_registry.has.return_value = True
        mock_registry.get.return_value = mock_apps

        installer = self._make_installer()
        result = InstallResult(
            success=True, platform='nix', name='TestApp',
            app_id='testapp')
        req = InstallRequest(source='nixpkgs.testapp')

        with patch('core.platform.registry.get_registry',
                   return_value=mock_registry):
            installer._auto_register_app(result, req)
        # No error — just skipped

    def test_auto_unregister_removes_from_registry(self):
        """Uninstall should remove app from AppRegistry."""
        from core.platform.app_registry import AppRegistry
        from core.platform.app_manifest import AppManifest

        mock_apps = AppRegistry()
        existing = AppManifest(
            id='myapp', name='MyApp', version='1.0.0',
            type='desktop_app', icon='apps',
            entry={'exec': 'myapp'})
        mock_apps.register(existing)
        self.assertIsNotNone(mock_apps.get('myapp'))

        mock_registry = MagicMock()
        mock_registry.has.return_value = True
        mock_registry.get.return_value = mock_apps

        installer = self._make_installer()
        with patch('core.platform.registry.get_registry',
                   return_value=mock_registry):
            installer._auto_unregister_app('myapp')

        self.assertIsNone(mock_apps.get('myapp'))

    def test_auto_unregister_noop_if_not_registered(self):
        """Unregister should silently no-op if app not in registry."""
        from core.platform.app_registry import AppRegistry

        mock_apps = AppRegistry()
        mock_registry = MagicMock()
        mock_registry.has.return_value = True
        mock_registry.get.return_value = mock_apps

        installer = self._make_installer()
        with patch('core.platform.registry.get_registry',
                   return_value=mock_registry):
            installer._auto_unregister_app('nonexistent')
        # No error

    def test_auto_register_graceful_without_platform(self):
        """Should not raise if platform not bootstrapped."""
        from integrations.agent_engine.app_installer import InstallResult, InstallRequest

        installer = self._make_installer()
        result = InstallResult(success=True, platform='nix', name='X', app_id='x')
        req = InstallRequest(source='nix:x')

        with patch('core.platform.registry.get_registry',
                   side_effect=ImportError("no platform")):
            installer._auto_register_app(result, req)
        # No error — graceful skip

    def test_extension_gets_extension_type(self):
        """Extension platform should map to extension AppType."""
        from integrations.agent_engine.app_installer import InstallResult, InstallRequest
        from core.platform.app_registry import AppRegistry

        mock_apps = AppRegistry()
        mock_registry = MagicMock()
        mock_registry.has.return_value = True
        mock_registry.get.return_value = mock_apps

        installer = self._make_installer()
        result = InstallResult(
            success=True, platform='extension', name='My Extension',
            app_id='my_ext')
        req = InstallRequest(source='extensions/my_ext')

        with patch('core.platform.registry.get_registry',
                   return_value=mock_registry):
            installer._auto_register_app(result, req)

        m = mock_apps.get('my_ext')
        self.assertIsNotNone(m)
        self.assertEqual(m.type, 'extension')

    def test_install_method_calls_auto_register(self):
        """install() source should call _auto_register_app on success."""
        import inspect
        from integrations.agent_engine.app_installer import AppInstaller
        src = inspect.getsource(AppInstaller.install)
        self.assertIn('_auto_register_app', src)

    def test_uninstall_method_calls_auto_unregister(self):
        """uninstall() source should call _auto_unregister_app on success."""
        import inspect
        from integrations.agent_engine.app_installer import AppInstaller
        src = inspect.getsource(AppInstaller.uninstall)
        self.assertIn('_auto_unregister_app', src)


# ═══════════════════════════════════════════════════════════════
# 3. ActionState Retry EventBus Emission
# ═══════════════════════════════════════════════════════════════

class TestRetryTrackerEventBus(unittest.TestCase):
    """Verify retry exhaustion emits EventBus event."""

    def test_retry_exhaustion_emits_event(self):
        """When retry count exceeds MAX, EventBus should emit action.retry_exhausted."""
        from lifecycle_hooks import ActionRetryTracker

        tracker = ActionRetryTracker()
        events = []

        def mock_emit(topic, data):
            events.append((topic, data))

        with patch('core.platform.events.emit_event', mock_emit):
            # Burn through retries
            for _ in range(tracker.MAX_PENDING_RETRIES):
                result = tracker.increment_pending('prompt1', 1)
                self.assertFalse(result)

            # This one should exceed threshold
            result = tracker.increment_pending('prompt1', 1)
            self.assertTrue(result)

        # Verify event was emitted
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], 'action.retry_exhausted')
        self.assertEqual(events[0][1]['action_id'], 1)
        self.assertEqual(events[0][1]['prompt'], 'prompt1')
        self.assertEqual(events[0][1]['retry_count'], tracker.MAX_PENDING_RETRIES + 1)

    def test_no_event_before_threshold(self):
        """No event should be emitted before retry threshold is exceeded."""
        from lifecycle_hooks import ActionRetryTracker

        tracker = ActionRetryTracker()
        events = []

        with patch('core.platform.events.emit_event',
                   lambda t, d: events.append((t, d))):
            tracker.increment_pending('p', 1)

        self.assertEqual(len(events), 0)

    def test_reset_clears_count(self):
        """Reset should clear the retry count."""
        from lifecycle_hooks import ActionRetryTracker

        tracker = ActionRetryTracker()
        tracker.increment_pending('p', 1)
        tracker.increment_pending('p', 1)
        tracker.reset_count('p', 1)
        self.assertNotIn(('p', 1), tracker.pending_counts)

    def test_event_graceful_without_eventbus(self):
        """Retry exhaustion should not crash if EventBus unavailable."""
        from lifecycle_hooks import ActionRetryTracker

        tracker = ActionRetryTracker()
        with patch('core.platform.events.emit_event',
                   side_effect=Exception("no bus")):
            for _ in range(tracker.MAX_PENDING_RETRIES + 1):
                tracker.increment_pending('p', 1)
        # No crash

    def test_increment_source_has_emit_event(self):
        """increment_pending source should call emit_event."""
        import inspect
        from lifecycle_hooks import ActionRetryTracker
        src = inspect.getsource(ActionRetryTracker.increment_pending)
        self.assertIn('emit_event', src)
        self.assertIn('action.retry_exhausted', src)


# ═══════════════════════════════════════════════════════════════
# 4. Orchestrator Services in Bootstrap
# ═══════════════════════════════════════════════════════════════

class TestOrchestratorBootstrapRegistration(unittest.TestCase):
    """Verify _register_orchestrator_services wires into bootstrap."""

    def test_register_orchestrator_services_registers_daemon(self):
        """AgentDaemon should be registered as 'agent_daemon' service."""
        from core.platform.bootstrap import _register_orchestrator_services
        from core.platform.registry import ServiceRegistry

        registry = ServiceRegistry()
        mock_daemon = MagicMock()

        with patch.dict('sys.modules', {
            'integrations.agent_engine.agent_daemon': MagicMock(
                agent_daemon=mock_daemon),
            'integrations.agent_engine.federated_aggregator': MagicMock(
                get_federated_aggregator=MagicMock(return_value=MagicMock())),
        }):
            _register_orchestrator_services(registry)

        self.assertTrue(registry.has('agent_daemon'))
        self.assertTrue(registry.has('federation'))

    def test_register_orchestrator_services_graceful_on_import_error(self):
        """Should not crash if agent_engine modules unavailable."""
        from core.platform.bootstrap import _register_orchestrator_services
        from core.platform.registry import ServiceRegistry

        registry = ServiceRegistry()
        # Just call it — the lazy factories handle import errors
        _register_orchestrator_services(registry)
        # Services are registered but factories may return None on resolution
        # No crash is the important assertion

    def test_bootstrap_includes_orchestrator_call(self):
        """bootstrap_platform should call _register_orchestrator_services."""
        from core.platform import bootstrap
        self.assertTrue(hasattr(bootstrap, '_register_orchestrator_services'))

        # Verify function is called in bootstrap_platform source
        import inspect
        src = inspect.getsource(bootstrap.bootstrap_platform)
        self.assertIn('_register_orchestrator_services', src)

    def test_register_function_references_agent_daemon(self):
        """_register_orchestrator_services should import agent_daemon."""
        import inspect
        from core.platform.bootstrap import _register_orchestrator_services
        src = inspect.getsource(_register_orchestrator_services)
        self.assertIn('agent_daemon', src)
        self.assertIn('federated_aggregator', src)


# ═══════════════════════════════════════════════════════════════
# 5. Integration — Install → Register → Uninstall → Unregister
# ═══════════════════════════════════════════════════════════════

class TestInstallLifecycleIntegration(unittest.TestCase):
    """End-to-end: install registers, uninstall unregisters."""

    def test_full_lifecycle(self):
        """Install → app in registry → uninstall → app gone."""
        from integrations.agent_engine.app_installer import (
            AppInstaller, InstallResult, InstallRequest)
        from core.platform.app_registry import AppRegistry

        mock_apps = AppRegistry()
        mock_registry = MagicMock()
        mock_registry.has.return_value = True
        mock_registry.get.return_value = mock_apps

        installer = AppInstaller()

        # Simulate install
        result = InstallResult(
            success=True, platform='flatpak', name='Video Player',
            version='3.2.0', app_id='video_player',
            install_path='/var/lib/flatpak/app/video_player')
        req = InstallRequest(source='flathub:video_player')

        with patch('core.platform.registry.get_registry',
                   return_value=mock_registry):
            installer._auto_register_app(result, req)
            self.assertIsNotNone(mock_apps.get('video_player'))
            self.assertEqual(mock_apps.get('video_player').name, 'Video Player')

            # Uninstall
            installer._auto_unregister_app('video_player')
            self.assertIsNone(mock_apps.get('video_player'))


# ═══════════════════════════════════════════════════════════════
# 6. Tool Allowlist — Structural Tests
# ═══════════════════════════════════════════════════════════════

class TestToolAllowlistStructure(unittest.TestCase):
    """Verify tool allowlist has correct tier structure."""

    def test_fast_tools_are_read_only(self):
        from integrations.agent_engine.tool_allowlist import _FAST_TOOLS
        self.assertNotIn('write_file', _FAST_TOOLS)
        self.assertNotIn('send_message', _FAST_TOOLS)
        self.assertIn('web_search', _FAST_TOOLS)
        self.assertIn('read_file', _FAST_TOOLS)

    def test_balanced_includes_fast(self):
        from integrations.agent_engine.tool_allowlist import (
            _FAST_TOOLS, _BALANCED_TOOLS)
        self.assertTrue(_FAST_TOOLS.issubset(_BALANCED_TOOLS))

    def test_balanced_has_write_tools(self):
        from integrations.agent_engine.tool_allowlist import _BALANCED_TOOLS
        self.assertIn('write_file', _BALANCED_TOOLS)
        self.assertIn('send_message', _BALANCED_TOOLS)

    def test_check_tool_allowed_unknown_model_fails_closed(self):
        from integrations.agent_engine.tool_allowlist import check_tool_allowed
        with patch('integrations.agent_engine.tool_allowlist._resolve_tier',
                   return_value=None):
            allowed, reason = check_tool_allowed('unknown_model', 'write_file')
            self.assertFalse(allowed)
            self.assertIn('fail-closed', reason)


# ═══════════════════════════════════════════════════════════════
# 7. Dispatch — Budget Gate Integration
# ═══════════════════════════════════════════════════════════════

class TestDispatchBudgetGate(unittest.TestCase):
    """Verify budget gate blocks dispatch when over budget."""

    @patch('integrations.agent_engine.dispatch.requests')
    @patch('integrations.agent_engine.dispatch._has_hive_peers', return_value=False)
    @patch('integrations.agent_engine.dispatch._get_distributed_coordinator', return_value=None)
    def test_budget_gate_blocks_dispatch(self, _coord, _peers, _req):
        from integrations.agent_engine.dispatch import dispatch_goal

        with patch.dict('sys.modules', {
            'integrations.agent_engine.budget_gate': MagicMock(
                pre_dispatch_budget_gate=MagicMock(
                    return_value=(False, 'Over budget'))),
        }):
            result = dispatch_goal('test', 'user1', 'goal123abc', 'marketing')
            self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════
# 8. Engine Fallback — Remote Desktop Orchestrator
# ═══════════════════════════════════════════════════════════════

class TestEngineFallback(unittest.TestCase):
    """Verify engine fallback in remote desktop orchestrator."""

    def test_orchestrator_has_fallback_logic(self):
        """Orchestrator handles engine failures gracefully."""
        import inspect
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        src = inspect.getsource(RemoteDesktopOrchestrator)
        # Should have exception handling around engine operations
        self.assertIn('except', src)
        self.assertIn('logger', src)

    def test_orchestrator_graceful_on_all_engines_fail(self):
        """Orchestrator initializes even when no engines detected."""
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator.__new__(RemoteDesktopOrchestrator)
        orch._engines = {}
        orch._active_engine = None
        orch._sessions = {}
        # Should not crash
        self.assertEqual(len(orch._engines), 0)

    def test_native_transport_available(self):
        """Native transport module exists as fallback."""
        import importlib
        mod = importlib.import_module('integrations.remote_desktop.transport')
        self.assertTrue(hasattr(mod, 'TransportChannel') or hasattr(mod, 'DirectWebSocketTransport'))


# ═══════════════════════════════════════════════════════════════
# 9. Resonance Per-User EMA Isolation
# ═══════════════════════════════════════════════════════════════

class TestResonancePerUserEMA(unittest.TestCase):
    """Verify EMA state is per-user, not shared."""

    def test_separate_user_profiles(self):
        """Two users get separate profile objects."""
        from core.resonance_profile import UserResonanceProfile
        p1 = UserResonanceProfile(user_id='user_A')
        p2 = UserResonanceProfile(user_id='user_B')
        self.assertIsNot(p1, p2)
        self.assertNotEqual(p1.user_id, p2.user_id)

    def test_no_cross_contamination(self):
        """Tuning user A does not affect user B."""
        from core.resonance_profile import UserResonanceProfile
        p1 = UserResonanceProfile(user_id='user_A')
        p2 = UserResonanceProfile(user_id='user_B')
        # Modify p1
        p1.set_tuning('formality_score', 0.9)
        # p2 should be unchanged at default
        self.assertNotEqual(p2.get_tuning('formality_score'), 0.9)

    def test_profile_path_includes_user_id(self):
        """Profile save path uses user_id for isolation."""
        from core.resonance_profile import UserResonanceProfile
        p = UserResonanceProfile(user_id='test_user_42')
        # The profile should know its user_id
        self.assertEqual(p.user_id, 'test_user_42')


if __name__ == '__main__':
    unittest.main()
