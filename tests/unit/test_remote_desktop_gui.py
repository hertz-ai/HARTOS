"""Tests for Remote Desktop Phase 4 — GUI panel, shell manifest, panel data."""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


# ═══════════════════════════════════════════════════════════════
# Shell Manifest Tests
# ═══════════════════════════════════════════════════════════════

class TestShellManifest(unittest.TestCase):
    """Verify remote_desktop panel is registered in the shell manifest."""

    def test_remote_desktop_in_system_panels(self):
        from integrations.agent_engine.shell_manifest import SYSTEM_PANELS
        self.assertIn('remote_desktop', SYSTEM_PANELS)

    def test_remote_desktop_panel_structure(self):
        from integrations.agent_engine.shell_manifest import SYSTEM_PANELS
        panel = SYSTEM_PANELS['remote_desktop']
        self.assertEqual(panel['title'], 'Remote Desktop')
        self.assertEqual(panel['icon'], 'connected_tv')
        self.assertEqual(panel['group'], 'System')
        self.assertIsInstance(panel['default_size'], list)
        self.assertEqual(len(panel['default_size']), 2)

    def test_remote_desktop_apis(self):
        from integrations.agent_engine.shell_manifest import SYSTEM_PANELS
        apis = SYSTEM_PANELS['remote_desktop']['apis']
        self.assertIn('/api/remote-desktop/status', apis)
        self.assertIn('/api/remote-desktop/engines', apis)
        self.assertIn('/api/remote-desktop/sessions', apis)

    def test_remote_desktop_in_get_all_panels(self):
        from integrations.agent_engine.shell_manifest import get_all_panels
        panels = get_all_panels()
        self.assertIn('remote_desktop', panels)


# ═══════════════════════════════════════════════════════════════
# Panel Data Tests
# ═══════════════════════════════════════════════════════════════

class TestPanelData(unittest.TestCase):
    """Test the panel data aggregation module."""

    def test_get_panel_data_structure(self):
        from integrations.remote_desktop.gui.panel import get_panel_data
        with patch('integrations.remote_desktop.device_id.get_device_id',
                   return_value='abcdef1234567890'), \
             patch('integrations.remote_desktop.engine_selector.get_all_status',
                   return_value={'engines': {'native': {'available': True}},
                                 'install_recommendations': []}), \
             patch('integrations.remote_desktop.session_manager.get_session_manager') as mock_sm:
            mock_sm.return_value.get_active_sessions.return_value = []

            data = get_panel_data()
            self.assertEqual(data['device_id'], 'abcdef1234567890')
            self.assertEqual(data['formatted_id'], 'abc-def-123')
            self.assertIn('native', data['engines'])
            self.assertEqual(data['sessions'], [])
            self.assertIsInstance(data['install_recommendations'], list)

    def test_get_panel_data_graceful_degradation(self):
        """Panel data should return defaults if modules fail."""
        from integrations.remote_desktop.gui.panel import get_panel_data
        # Even if sub-imports fail, should return a valid dict with defaults
        with patch('integrations.remote_desktop.device_id.get_device_id',
                   side_effect=Exception('no key')):
            data = get_panel_data()
            # device_id should be None when it fails
            self.assertIsNone(data['device_id'])
            # engines should still have native fallback
            self.assertIn('engines', data)
            self.assertIn('sessions', data)

    def test_panel_js_exists(self):
        from integrations.remote_desktop.gui.panel import PANEL_JS
        self.assertIsInstance(PANEL_JS, str)
        self.assertIn('loadRemoteDesktopPanel', PANEL_JS)
        self.assertIn('Device ID', PANEL_JS)
        self.assertIn('Engines', PANEL_JS)


# ═══════════════════════════════════════════════════════════════
# LiquidUI Integration Tests
# ═══════════════════════════════════════════════════════════════

class TestLiquidUIIntegration(unittest.TestCase):
    """Verify the remote desktop panel is wired into liquid_ui_service.py."""

    def test_loadRemoteDesktopPanel_in_dispatcher(self):
        """Verify loadRemoteDesktopPanel is called for remote_desktop panel ID."""
        import integrations.agent_engine.liquid_ui_service as lui
        # Read the module source to check for the dispatcher wiring
        import inspect
        source = inspect.getsource(lui)
        self.assertIn("id==='remote_desktop'", source)
        self.assertIn('loadRemoteDesktopPanel', source)

    def test_loadRemoteDesktopPanel_js_function_exists(self):
        """Verify the JS function is defined in liquid_ui_service.py."""
        import integrations.agent_engine.liquid_ui_service as lui
        import inspect
        source = inspect.getsource(lui)
        self.assertIn('function loadRemoteDesktopPanel', source)


if __name__ == '__main__':
    unittest.main()
