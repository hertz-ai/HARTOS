"""Tests for Remote Desktop Service Manager — engine lifecycle management."""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


class TestEngineService(unittest.TestCase):
    """Test EngineService lifecycle for a single engine."""

    def test_detect_installed_engine(self):
        from integrations.remote_desktop.service_manager import EngineService, EngineState
        svc = EngineService('rustdesk')
        mock_bridge = MagicMock()
        mock_bridge.available = True
        with patch.object(svc, '_get_bridge', return_value=mock_bridge):
            self.assertTrue(svc.detect())
            self.assertEqual(svc.info.state, EngineState.INSTALLED)

    def test_detect_missing_engine(self):
        from integrations.remote_desktop.service_manager import EngineService, EngineState
        svc = EngineService('rustdesk')
        mock_bridge = MagicMock()
        mock_bridge.available = False
        with patch.object(svc, '_get_bridge', return_value=mock_bridge):
            self.assertFalse(svc.detect())
            self.assertEqual(svc.info.state, EngineState.NOT_INSTALLED)

    def test_start_engine_success(self):
        from integrations.remote_desktop.service_manager import EngineService, EngineState
        svc = EngineService('rustdesk')
        mock_bridge = MagicMock()
        mock_bridge.available = True
        mock_bridge.start_service.return_value = True
        with patch.object(svc, '_get_bridge', return_value=mock_bridge):
            self.assertTrue(svc.start())
            self.assertEqual(svc.info.state, EngineState.RUNNING)
            self.assertTrue(svc.info.healthy)

    def test_start_engine_not_installed(self):
        from integrations.remote_desktop.service_manager import EngineService, EngineState
        svc = EngineService('rustdesk')
        mock_bridge = MagicMock()
        mock_bridge.available = False
        with patch.object(svc, '_get_bridge', return_value=mock_bridge):
            self.assertFalse(svc.start())
            self.assertEqual(svc.info.state, EngineState.NOT_INSTALLED)

    def test_stop_engine(self):
        from integrations.remote_desktop.service_manager import EngineService, EngineState
        svc = EngineService('rustdesk')
        svc.info.state = EngineState.RUNNING
        mock_bridge = MagicMock()
        with patch.object(svc, '_get_bridge', return_value=mock_bridge):
            self.assertTrue(svc.stop())
            self.assertEqual(svc.info.state, EngineState.STOPPED)
            self.assertFalse(svc.info.healthy)

    def test_restart_increments_count(self):
        from integrations.remote_desktop.service_manager import EngineService
        svc = EngineService('rustdesk')
        mock_bridge = MagicMock()
        mock_bridge.available = True
        mock_bridge.start_service.return_value = True
        with patch.object(svc, '_get_bridge', return_value=mock_bridge):
            with patch('time.sleep'):
                svc.restart()
                self.assertEqual(svc.info.restart_count, 1)

    def test_get_status_dict(self):
        from integrations.remote_desktop.service_manager import EngineService
        svc = EngineService('sunshine')
        mock_bridge = MagicMock()
        mock_bridge.available = True
        mock_bridge.get_install_command.return_value = 'brew install sunshine'
        with patch.object(svc, '_get_bridge', return_value=mock_bridge):
            status = svc.get_status()
            self.assertEqual(status['engine'], 'sunshine')
            self.assertIn('installed', status)
            self.assertIn('running', status)
            self.assertIn('healthy', status)

    def test_install_command_delegates_to_bridge(self):
        from integrations.remote_desktop.service_manager import EngineService
        svc = EngineService('rustdesk')
        mock_bridge = MagicMock()
        mock_bridge.get_install_command.return_value = 'winget install RustDesk'
        with patch.object(svc, '_get_bridge', return_value=mock_bridge):
            cmd = svc.install_command()
            self.assertIn('winget', cmd)

    def test_moonlight_on_demand(self):
        """Moonlight is on-demand — start() should succeed without start_service."""
        from integrations.remote_desktop.service_manager import EngineService
        svc = EngineService('moonlight')
        mock_bridge = MagicMock(spec=['available'])
        mock_bridge.available = True
        with patch.object(svc, '_get_bridge', return_value=mock_bridge):
            # Moonlight bridge has no start_service
            self.assertTrue(svc.start())


class TestServiceManager(unittest.TestCase):
    """Test ServiceManager multi-engine lifecycle."""

    def test_singleton_pattern(self):
        import integrations.remote_desktop.service_manager as sm_mod
        sm_mod._service_manager = None
        sm1 = sm_mod.get_service_manager()
        sm2 = sm_mod.get_service_manager()
        self.assertIs(sm1, sm2)
        sm_mod._service_manager = None

    def test_all_engines_registered(self):
        from integrations.remote_desktop.service_manager import ServiceManager
        sm = ServiceManager()
        self.assertIn('rustdesk', sm._engines)
        self.assertIn('sunshine', sm._engines)
        self.assertIn('moonlight', sm._engines)

    def test_ensure_native_always_ready(self):
        from integrations.remote_desktop.service_manager import ServiceManager
        sm = ServiceManager()
        ready, msg = sm.ensure_engine('native')
        self.assertTrue(ready)
        self.assertIn('always available', msg)

    def test_ensure_unknown_engine(self):
        from integrations.remote_desktop.service_manager import ServiceManager
        sm = ServiceManager()
        ready, msg = sm.ensure_engine('nonexistent')
        self.assertFalse(ready)
        self.assertIn('Unknown', msg)

    def test_get_all_status_includes_native(self):
        from integrations.remote_desktop.service_manager import ServiceManager
        sm = ServiceManager()
        status = sm.get_all_status()
        self.assertIn('native', status)
        self.assertTrue(status['native']['running'])

    def test_stop_all(self):
        from integrations.remote_desktop.service_manager import ServiceManager, EngineState
        sm = ServiceManager()
        # Simulate running engine
        sm._engines['rustdesk'].info.state = EngineState.RUNNING
        mock_bridge = MagicMock()
        with patch.object(sm._engines['rustdesk'], '_get_bridge', return_value=mock_bridge):
            sm.stop_all()
            self.assertEqual(sm._engines['rustdesk'].info.state, EngineState.STOPPED)

    def test_register_with_watchdog_no_watchdog(self):
        from integrations.remote_desktop.service_manager import ServiceManager
        sm = ServiceManager()
        # Patch get_watchdog at the source so the lazy import inside
        # register_with_watchdog() picks up the mock regardless of
        # whether security.node_watchdog was already imported by other tests.
        with patch('security.node_watchdog.get_watchdog', return_value=None):
            result = sm.register_with_watchdog()
            self.assertFalse(result)

    def test_health_check_all(self):
        from integrations.remote_desktop.service_manager import ServiceManager
        sm = ServiceManager()
        results = sm.health_check_all()
        self.assertIn('native', results)
        self.assertTrue(results['native'])


if __name__ == '__main__':
    unittest.main()
