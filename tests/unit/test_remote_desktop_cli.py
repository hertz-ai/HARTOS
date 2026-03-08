"""Tests for Remote Desktop Phase 5 — CLI commands + API endpoints."""
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


# ═══════════════════════════════════════════════════════════════
# CLI Tests (Click runner)
# ═══════════════════════════════════════════════════════════════

class TestRemoteDesktopCLI(unittest.TestCase):
    """Test hart remote-desktop CLI commands."""

    def setUp(self):
        from click.testing import CliRunner
        from hart_cli import hart
        self.runner = CliRunner()
        self.hart = hart

    def test_remote_desktop_id(self):
        with patch('integrations.remote_desktop.device_id.get_device_id',
                   return_value='abcdef1234567890'):
            result = self.runner.invoke(self.hart, ['remote-desktop', 'id'])
            self.assertEqual(result.exit_code, 0)
            self.assertIn('abc-def-123', result.output)

    def test_remote_desktop_id_json(self):
        with patch('integrations.remote_desktop.device_id.get_device_id',
                   return_value='abcdef1234567890'):
            result = self.runner.invoke(self.hart, ['--json', 'remote-desktop', 'id'])
            self.assertEqual(result.exit_code, 0)
            data = json.loads(result.output)
            self.assertEqual(data['device_id'], 'abcdef1234567890')

    def test_remote_desktop_status(self):
        mock_status = {
            'engines': {
                'rustdesk': {'available': False},
                'sunshine': {'available': False},
                'moonlight': {'available': False},
                'native': {'available': True},
            },
            'install_recommendations': [],
        }
        with patch('integrations.remote_desktop.engine_selector.get_all_status',
                   return_value=mock_status):
            result = self.runner.invoke(self.hart, ['remote-desktop', 'status'])
            self.assertEqual(result.exit_code, 0)
            self.assertIn('native', result.output)
            self.assertIn('available', result.output)

    def test_remote_desktop_status_json(self):
        mock_status = {
            'engines': {'native': {'available': True}},
            'install_recommendations': [],
        }
        with patch('integrations.remote_desktop.engine_selector.get_all_status',
                   return_value=mock_status):
            result = self.runner.invoke(self.hart, ['--json', 'remote-desktop', 'status'])
            self.assertEqual(result.exit_code, 0)
            data = json.loads(result.output)
            self.assertIn('engines', data)

    def test_remote_desktop_sessions_empty(self):
        with patch('integrations.remote_desktop.session_manager.get_session_manager') as mock_sm:
            mock_sm.return_value.get_active_sessions.return_value = []
            result = self.runner.invoke(self.hart, ['remote-desktop', 'sessions'])
            self.assertEqual(result.exit_code, 0)
            self.assertIn('No active sessions', result.output)

    def test_remote_desktop_install_rustdesk(self):
        result = self.runner.invoke(self.hart, ['remote-desktop', 'install', 'rustdesk'])
        self.assertEqual(result.exit_code, 0)
        self.assertIn('rustdesk', result.output.lower())

    def test_remote_desktop_install_sunshine(self):
        result = self.runner.invoke(self.hart, ['remote-desktop', 'install', 'sunshine'])
        self.assertEqual(result.exit_code, 0)
        self.assertIn('sunshine', result.output.lower())

    def test_remote_desktop_install_moonlight(self):
        result = self.runner.invoke(self.hart, ['remote-desktop', 'install', 'moonlight'])
        self.assertEqual(result.exit_code, 0)
        self.assertIn('moonlight', result.output.lower())

    def test_remote_desktop_disconnect_requires_id_or_all(self):
        with patch('integrations.remote_desktop.session_manager.get_session_manager'):
            result = self.runner.invoke(self.hart, ['remote-desktop', 'disconnect'])
            self.assertNotEqual(result.exit_code, 0)

    def test_remote_desktop_host(self):
        with patch('integrations.remote_desktop.device_id.get_device_id',
                   return_value='abcdef1234567890'), \
             patch('integrations.remote_desktop.session_manager.get_session_manager') as mock_sm:
            mock_sm.return_value.generate_otp.return_value = 'A1B2C3'
            result = self.runner.invoke(self.hart, ['remote-desktop', 'host',
                                                     '--engine', 'native'])
            self.assertEqual(result.exit_code, 0)
            self.assertIn('A1B2C3', result.output)
            self.assertIn('abc-def-123', result.output)

    def test_remote_desktop_host_json(self):
        with patch('integrations.remote_desktop.device_id.get_device_id',
                   return_value='abcdef1234567890'), \
             patch('integrations.remote_desktop.session_manager.get_session_manager') as mock_sm:
            mock_sm.return_value.generate_otp.return_value = 'X9Y8Z7'
            result = self.runner.invoke(self.hart, ['--json', 'remote-desktop', 'host',
                                                     '--engine', 'native'])
            self.assertEqual(result.exit_code, 0)
            data = json.loads(result.output)
            self.assertEqual(data['password'], 'X9Y8Z7')
            self.assertEqual(data['device_id'], 'abcdef1234567890')


# ═══════════════════════════════════════════════════════════════
# API Endpoint Tests (Flask test client)
# ═══════════════════════════════════════════════════════════════

class TestRemoteDesktopAPI(unittest.TestCase):
    """Test /api/remote-desktop/* Flask endpoints."""

    @classmethod
    def setUpClass(cls):
        # Import Flask app
        from langchain_gpt_api import app
        app.config['TESTING'] = True
        cls.app = app
        cls.client = app.test_client()

    def test_status_endpoint(self):
        with patch('integrations.remote_desktop.engine_selector.get_all_status',
                   return_value={'engines': {'native': {'available': True}},
                                 'install_recommendations': []}), \
             patch('integrations.remote_desktop.device_id.get_device_id',
                   return_value='abcdef1234567890'), \
             patch('integrations.remote_desktop.session_manager.get_session_manager') as mock_sm:
            mock_sm.return_value.get_active_sessions.return_value = []
            resp = self.client.get('/api/remote-desktop/status')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertIn('device_id', data)
            self.assertIn('engines', data)
            self.assertEqual(data['formatted_id'], 'abc-def-123')

    def test_host_endpoint(self):
        with patch('integrations.remote_desktop.device_id.get_device_id',
                   return_value='abcdef1234567890'), \
             patch('integrations.remote_desktop.session_manager.get_session_manager') as mock_sm:
            mock_sm.return_value.generate_otp.return_value = 'OTP123'
            resp = self.client.post('/api/remote-desktop/host',
                                    json={'engine': 'native'})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data['password'], 'OTP123')
            self.assertEqual(data['device_id'], 'abcdef1234567890')

    def test_connect_endpoint_missing_fields(self):
        resp = self.client.post('/api/remote-desktop/connect', json={})
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertIn('error', data)

    def test_connect_endpoint_no_engine(self):
        resp = self.client.post('/api/remote-desktop/connect',
                                json={'device_id': '123', 'password': 'abc',
                                      'engine': 'native'})
        # native engine doesn't have a connect method in the API,
        # so falls through to 503
        self.assertEqual(resp.status_code, 503)

    def test_sessions_endpoint(self):
        with patch('integrations.remote_desktop.session_manager.get_session_manager') as mock_sm:
            mock_sm.return_value.get_active_sessions.return_value = []
            resp = self.client.get('/api/remote-desktop/sessions')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data['sessions'], [])

    def test_disconnect_endpoint(self):
        with patch('integrations.remote_desktop.session_manager.get_session_manager') as mock_sm:
            mock_sm.return_value.disconnect_session.return_value = None
            resp = self.client.post('/api/remote-desktop/disconnect/test_session_123',
                                    json={})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data['disconnected'], 'test_session_123')

    def test_engines_endpoint(self):
        mock_status = {
            'engines': {'native': {'available': True}},
            'install_recommendations': [],
        }
        with patch('integrations.remote_desktop.engine_selector.get_all_status',
                   return_value=mock_status), \
             patch('integrations.remote_desktop.engine_selector.get_available_engines',
                   return_value=[MagicMock(value='native')]):
            resp = self.client.get('/api/remote-desktop/engines')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertIn('available', data)
            self.assertIn('native', data['available'])

    def test_select_engine_endpoint(self):
        with patch('integrations.remote_desktop.engine_selector.select_engine') as mock_sel, \
             patch('integrations.remote_desktop.engine_selector.reset_cache'):
            mock_sel.return_value = MagicMock(value='rustdesk')
            resp = self.client.post('/api/remote-desktop/select-engine',
                                    json={'use_case': 'general', 'role': 'viewer'})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data['engine'], 'rustdesk')

    def test_select_engine_with_preference(self):
        with patch('integrations.remote_desktop.engine_selector.select_engine') as mock_sel, \
             patch('integrations.remote_desktop.engine_selector.reset_cache'):
            mock_sel.return_value = MagicMock(value='sunshine')
            resp = self.client.post('/api/remote-desktop/select-engine',
                                    json={'use_case': 'gaming', 'role': 'host',
                                          'prefer': 'sunshine'})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data['engine'], 'sunshine')


if __name__ == '__main__':
    unittest.main()
