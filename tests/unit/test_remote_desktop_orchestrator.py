"""Tests for Remote Desktop Orchestrator — unified coordinator."""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


class TestOrchestratorLifecycle(unittest.TestCase):
    """Test orchestrator startup/shutdown."""

    def test_singleton_pattern(self):
        import integrations.remote_desktop.orchestrator as orch_mod
        orch_mod._orchestrator = None
        o1 = orch_mod.get_orchestrator()
        o2 = orch_mod.get_orchestrator()
        self.assertIs(o1, o2)
        orch_mod._orchestrator = None

    def test_startup(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        mock_sm = MagicMock()
        mock_sm.start_all_available.return_value = {'native': {'running': True}}
        mock_sm.register_with_watchdog.return_value = True
        with patch('integrations.remote_desktop.service_manager.get_service_manager',
                   return_value=mock_sm):
            result = orch.startup()
            self.assertEqual(result['status'], 'started')
            self.assertTrue(orch._started)

    def test_startup_idempotent(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        orch._started = True
        result = orch.startup()
        # Returns status when already started
        self.assertIn('started', result)

    def test_shutdown(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        orch._started = True
        with patch('integrations.remote_desktop.service_manager.get_service_manager') as mock_sm:
            orch.shutdown()
            self.assertFalse(orch._started)


class TestOrchestratorHosting(unittest.TestCase):
    """Test host operations via orchestrator."""

    def _make_orchestrator(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        return RemoteDesktopOrchestrator()

    @patch('integrations.remote_desktop.service_manager.get_service_manager')
    @patch('integrations.remote_desktop.device_id.get_device_id', return_value='abc123def456')
    @patch('integrations.remote_desktop.device_id.format_device_id', return_value='abc-123-def')
    @patch('integrations.remote_desktop.session_manager.get_session_manager')
    def test_start_hosting_native(self, mock_sm_fn, mock_fmt, mock_did, mock_svc_fn):
        orch = self._make_orchestrator()

        mock_svc = MagicMock()
        mock_svc.ensure_engine.return_value = (True, 'native ready')
        mock_svc_fn.return_value = mock_svc

        mock_sm = MagicMock()
        mock_sm.generate_otp.return_value = 'ab1234'
        mock_session = MagicMock()
        mock_session.session_id = 'sess-001'
        mock_sm.create_session.return_value = mock_session
        mock_sm_fn.return_value = mock_sm

        with patch.object(orch, '_resolve_engine', return_value='native'), \
             patch.object(orch, '_audit'):
            result = orch.start_hosting()
            self.assertEqual(result['status'], 'hosting')
            self.assertEqual(result['device_id'], 'abc123def456')
            self.assertEqual(result['password'], 'ab1234')
            self.assertEqual(result['engine'], 'native')

    def test_start_hosting_no_device_id(self):
        orch = self._make_orchestrator()
        with patch('integrations.remote_desktop.device_id.get_device_id',
                   side_effect=Exception('no key')):
            result = orch.start_hosting()
            self.assertEqual(result['status'], 'error')

    def test_stop_hosting(self):
        orch = self._make_orchestrator()
        orch._active_sessions = {'s1': {'device_id': 'abc'}}
        with patch.object(orch, '_disconnect_one', return_value=True) as mock_dc:
            orch.stop_hosting()
            mock_dc.assert_called_once_with('s1')


class TestOrchestratorConnect(unittest.TestCase):
    """Test viewer connection operations."""

    def _make_orchestrator(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        return RemoteDesktopOrchestrator()

    @patch('integrations.remote_desktop.service_manager.get_service_manager')
    @patch('integrations.remote_desktop.device_id.get_device_id', return_value='viewer-001')
    @patch('integrations.remote_desktop.session_manager.get_session_manager')
    def test_connect_native(self, mock_sm_fn, mock_did, mock_svc_fn):
        orch = self._make_orchestrator()

        mock_svc = MagicMock()
        mock_svc.ensure_engine.return_value = (True, 'ready')
        mock_svc_fn.return_value = mock_svc

        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.session_id = 'sess-002'
        mock_sm.create_session.return_value = mock_session
        mock_sm_fn.return_value = mock_sm

        with patch.object(orch, '_resolve_engine', return_value='native'), \
             patch.object(orch, '_authenticate', return_value=(True, 'ok')), \
             patch.object(orch, '_connect_native', return_value={'status': 'connected'}), \
             patch.object(orch, '_start_clipboard_bridge', return_value=True), \
             patch.object(orch, '_audit'):
            result = orch.connect('host-001', 'pw123')
            self.assertEqual(result['status'], 'connected')
            self.assertEqual(result['engine'], 'native')
            self.assertIn('sess-002', result['session_id'])

    def test_connect_auth_failed(self):
        orch = self._make_orchestrator()
        mock_svc = MagicMock()
        mock_svc.ensure_engine.return_value = (True, 'ok')
        with patch('integrations.remote_desktop.service_manager.get_service_manager',
                   return_value=mock_svc), \
             patch.object(orch, '_resolve_engine', return_value='native'), \
             patch.object(orch, '_authenticate', return_value=(False, 'bad password')):
            result = orch.connect('host-001', 'wrong')
            self.assertEqual(result['status'], 'auth_failed')


class TestOrchestratorSmartConnect(unittest.TestCase):
    """Test AI-native smart_connect."""

    def test_smart_connect_file_transfer(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        with patch.object(orch, 'connect', return_value={'status': 'connected'}) as mock_conn:
            orch.smart_connect('dev-1', 'pw', context={'intent': 'file_transfer'})
            call_kwargs = mock_conn.call_args[1]
            self.assertEqual(call_kwargs['mode'], 'file_transfer')
            self.assertEqual(call_kwargs['use_case'], 'file_transfer')

    def test_smart_connect_gaming(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        with patch.object(orch, 'connect', return_value={'status': 'connected'}) as mock_conn:
            orch.smart_connect('dev-1', 'pw', context={'intent': 'gaming'})
            call_kwargs = mock_conn.call_args[1]
            self.assertEqual(call_kwargs['use_case'], 'gaming')

    def test_smart_connect_observe_mode(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        with patch.object(orch, 'connect', return_value={'status': 'connected'}) as mock_conn:
            orch.smart_connect('dev-1', 'pw', context={'intent': 'observe'})
            call_kwargs = mock_conn.call_args[1]
            self.assertEqual(call_kwargs['mode'], 'view_only')


class TestOrchestratorEngineSwitch(unittest.TestCase):
    """Test mid-session engine switching."""

    def test_switch_engine_success(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        orch._active_sessions = {
            'sess-1': {
                'engine': 'rustdesk',
                'remote_device_id': 'dev-1',
                'mode': 'full_control',
                'user_id': 'u1',
                'gui': True,
            }
        }
        mock_svc = MagicMock()
        mock_svc.ensure_engine.return_value = (True, 'ok')
        with patch('integrations.remote_desktop.service_manager.get_service_manager',
                   return_value=mock_svc), \
             patch.object(orch, '_disconnect_engine'), \
             patch.object(orch, '_connect_moonlight', return_value={'status': 'connected'}), \
             patch.object(orch, '_audit'):
            result = orch.switch_engine('sess-1', 'moonlight')
            self.assertEqual(result['status'], 'switched')
            self.assertEqual(result['old_engine'], 'rustdesk')
            self.assertEqual(result['new_engine'], 'moonlight')

    def test_switch_same_engine_no_change(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        orch._active_sessions = {'s1': {'engine': 'rustdesk'}}
        result = orch.switch_engine('s1', 'rustdesk')
        self.assertEqual(result['status'], 'no_change')

    def test_switch_session_not_found(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        result = orch.switch_engine('nonexistent', 'moonlight')
        self.assertEqual(result['status'], 'error')


class TestOrchestratorStatus(unittest.TestCase):
    """Test orchestrator status reporting."""

    def test_get_status_empty(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        with patch('integrations.remote_desktop.service_manager.get_service_manager') as mock_sm:
            mock_sm.return_value.get_all_status.return_value = {}
            with patch('integrations.remote_desktop.device_id.get_device_id',
                       return_value='dev-1'), \
                 patch('integrations.remote_desktop.device_id.format_device_id',
                       return_value='dev-1-fmt'):
                status = orch.get_status()
                self.assertEqual(status['active_session_count'], 0)
                self.assertEqual(status['device_id'], 'dev-1')

    def test_get_sessions(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        orch._active_sessions = {'s1': {'engine': 'rustdesk'}}
        sessions = orch.get_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]['engine'], 'rustdesk')

    def test_disconnect_all(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        orch._active_sessions = {'s1': {'engine': 'a'}, 's2': {'engine': 'b'}}
        with patch.object(orch, '_disconnect_one', return_value=True) as mock_dc:
            orch.disconnect()
            self.assertEqual(mock_dc.call_count, 2)


if __name__ == '__main__':
    unittest.main()
