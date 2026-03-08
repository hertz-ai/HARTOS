"""Tests for Remote Desktop Phase 6 — Agent tools, recipe hooks, crossbar wiring."""
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


# ═══════════════════════════════════════════════════════════════
# Agent Tool Tests
# ═══════════════════════════════════════════════════════════════

class TestBuildRemoteDesktopTools(unittest.TestCase):
    """Test build_remote_desktop_tools() returns proper tool tuples."""

    def test_builds_expected_tools(self):
        from integrations.remote_desktop.agent_tools import build_remote_desktop_tools
        ctx = {'user_id': 'test_user', 'prompt_id': '0'}
        tools = build_remote_desktop_tools(ctx)
        self.assertIsInstance(tools, list)
        self.assertGreaterEqual(len(tools), 7)
        names = [t[0] for t in tools]
        self.assertIn('offer_remote_help', names)
        self.assertIn('request_screen_view', names)
        self.assertIn('remote_execute_action', names)
        self.assertIn('remote_screenshot', names)
        self.assertIn('remote_transfer_file', names)
        self.assertIn('get_remote_sessions', names)
        self.assertIn('disconnect_remote', names)

    def test_tool_tuple_format(self):
        from integrations.remote_desktop.agent_tools import build_remote_desktop_tools
        ctx = {'user_id': 'test_user'}
        tools = build_remote_desktop_tools(ctx)
        for name, desc, func in tools:
            self.assertIsInstance(name, str)
            self.assertIsInstance(desc, str)
            self.assertTrue(callable(func))


class TestOfferRemoteHelp(unittest.TestCase):
    """Test the offer_remote_help tool function."""

    def test_offer_help_returns_device_info(self):
        from integrations.remote_desktop.agent_tools import build_remote_desktop_tools
        ctx = {'user_id': 'test_user'}
        tools = build_remote_desktop_tools(ctx)
        offer_fn = dict((t[0], t[2]) for t in tools)['offer_remote_help']

        with patch('integrations.remote_desktop.device_id.get_device_id',
                   return_value='abcdef1234567890'), \
             patch('integrations.remote_desktop.session_manager.get_session_manager') as mock_sm:
            mock_sm.return_value.generate_otp.return_value = 'ABC123'
            result = offer_fn(allow_control=True)
            self.assertIn('abc-def-123', result)
            self.assertIn('ABC123', result)
            self.assertIn('full_control', result)

    def test_offer_help_view_only(self):
        from integrations.remote_desktop.agent_tools import build_remote_desktop_tools
        ctx = {'user_id': 'test_user'}
        tools = build_remote_desktop_tools(ctx)
        offer_fn = dict((t[0], t[2]) for t in tools)['offer_remote_help']

        with patch('integrations.remote_desktop.device_id.get_device_id',
                   return_value='abcdef1234567890'), \
             patch('integrations.remote_desktop.session_manager.get_session_manager') as mock_sm:
            mock_sm.return_value.generate_otp.return_value = 'XYZ789'
            result = offer_fn(allow_control=False)
            self.assertIn('view_only', result)


class TestGetRemoteSessions(unittest.TestCase):
    """Test the get_remote_sessions tool function."""

    def test_no_sessions(self):
        from integrations.remote_desktop.agent_tools import build_remote_desktop_tools
        ctx = {'user_id': 'test_user'}
        tools = build_remote_desktop_tools(ctx)
        sessions_fn = dict((t[0], t[2]) for t in tools)['get_remote_sessions']

        with patch('integrations.remote_desktop.session_manager.get_session_manager') as mock_sm:
            mock_sm.return_value.get_active_sessions.return_value = []
            result = sessions_fn()
            self.assertIn('No active', result)


class TestDisconnectRemote(unittest.TestCase):
    """Test the disconnect_remote tool function."""

    def test_disconnect_all(self):
        from integrations.remote_desktop.agent_tools import build_remote_desktop_tools
        ctx = {'user_id': 'test_user'}
        tools = build_remote_desktop_tools(ctx)
        disconnect_fn = dict((t[0], t[2]) for t in tools)['disconnect_remote']

        with patch('integrations.remote_desktop.session_manager.get_session_manager') as mock_sm:
            mock_sm.return_value.get_active_sessions.return_value = []
            result = disconnect_fn()
            self.assertIn('Disconnected 0', result)


# ═══════════════════════════════════════════════════════════════
# Recipe Hook Tests
# ═══════════════════════════════════════════════════════════════

class TestRecipeBridge(unittest.TestCase):
    """Test RemoteDesktopRecipeBridge."""

    def test_capture_session_as_recipe(self):
        from integrations.remote_desktop.recipe_hooks import RemoteDesktopRecipeBridge
        bridge = RemoteDesktopRecipeBridge()
        actions = [
            {'type': 'click', 'x': 100, 'y': 200},
            {'type': 'type', 'text': 'hello world'},
            {'type': 'key', 'key': 'enter'},
        ]
        recipe = bridge.capture_session_as_recipe('session_abc', actions)
        self.assertEqual(recipe['recipe_type'], 'remote_desktop')
        self.assertEqual(recipe['step_count'], 3)
        self.assertEqual(len(recipe['steps']), 3)
        self.assertEqual(recipe['steps'][0]['action_type'], 'click')
        self.assertEqual(recipe['steps'][1]['action_type'], 'type')

    def test_recording_flow(self):
        from integrations.remote_desktop.recipe_hooks import RemoteDesktopRecipeBridge
        bridge = RemoteDesktopRecipeBridge()
        bridge.start_recording('session_123')
        bridge.record_action({'type': 'click', 'x': 50, 'y': 50})
        bridge.record_action({'type': 'type', 'text': 'test'})
        actions = bridge.stop_recording()
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0]['action']['type'], 'click')

    def test_not_recording_by_default(self):
        from integrations.remote_desktop.recipe_hooks import RemoteDesktopRecipeBridge
        bridge = RemoteDesktopRecipeBridge()
        bridge.record_action({'type': 'click', 'x': 50, 'y': 50})
        actions = bridge.stop_recording()
        self.assertEqual(len(actions), 0)

    def test_replay_empty_recipe(self):
        from integrations.remote_desktop.recipe_hooks import RemoteDesktopRecipeBridge
        bridge = RemoteDesktopRecipeBridge()
        result = bridge.replay_recipe_on_device({'steps': []})
        self.assertTrue(result['success'])
        self.assertEqual(result['steps_executed'], 0)

    def test_replay_recipe_local(self):
        from integrations.remote_desktop.recipe_hooks import RemoteDesktopRecipeBridge
        bridge = RemoteDesktopRecipeBridge()
        recipe = {
            'steps': [
                {'step_id': 1, 'action_type': 'click', 'parameters': {'x': 100, 'y': 200}},
            ]
        }
        with patch('integrations.remote_desktop.input_handler.InputHandler') as mock_cls:
            mock_handler = MagicMock()
            mock_handler.handle_input_event.return_value = {'success': True}
            mock_cls.return_value = mock_handler
            result = bridge.replay_recipe_on_device(recipe, delay=0)
            self.assertEqual(result['steps_executed'], 1)
            mock_handler.handle_input_event.assert_called_once()


class TestDescribeAction(unittest.TestCase):
    """Test _describe_action helper."""

    def test_describe_click(self):
        from integrations.remote_desktop.recipe_hooks import _describe_action
        self.assertIn('Click', _describe_action({'type': 'click', 'x': 10, 'y': 20}))

    def test_describe_type(self):
        from integrations.remote_desktop.recipe_hooks import _describe_action
        self.assertIn('Type', _describe_action({'type': 'type', 'text': 'hello'}))

    def test_describe_key(self):
        from integrations.remote_desktop.recipe_hooks import _describe_action
        self.assertIn('Key', _describe_action({'type': 'key', 'key': 'enter'}))

    def test_describe_scroll(self):
        from integrations.remote_desktop.recipe_hooks import _describe_action
        self.assertIn('Scroll', _describe_action({'type': 'scroll', 'delta_y': -3}))


class TestRecipeBridgeSingleton(unittest.TestCase):
    def test_singleton(self):
        from integrations.remote_desktop.recipe_hooks import get_recipe_bridge
        b1 = get_recipe_bridge()
        b2 = get_recipe_bridge()
        self.assertIs(b1, b2)


# ═══════════════════════════════════════════════════════════════
# Agent Tools Registration Tests
# ═══════════════════════════════════════════════════════════════

class TestRegisterRemoteDesktopTools(unittest.TestCase):
    """Test that tools register correctly on mock AutoGen agents."""

    def test_register_on_mock_agents(self):
        from integrations.remote_desktop.agent_tools import (
            build_remote_desktop_tools, register_remote_desktop_tools,
        )
        ctx = {'user_id': 'test'}
        tools = build_remote_desktop_tools(ctx)

        helper = MagicMock()
        executor = MagicMock()
        # register_for_llm returns a decorator
        helper.register_for_llm.return_value = lambda f: f
        executor.register_for_execution.return_value = lambda f: f

        register_remote_desktop_tools(tools, helper, executor)

        # Should have been called for each tool
        self.assertEqual(helper.register_for_llm.call_count, len(tools))
        self.assertEqual(executor.register_for_execution.call_count, len(tools))


class TestCoreAgentToolsIntegration(unittest.TestCase):
    """Test that core/agent_tools.py can import remote desktop tools."""

    def test_register_if_available(self):
        from core.agent_tools import register_remote_desktop_tools_if_available
        ctx = {'user_id': 'test'}
        helper = MagicMock()
        executor = MagicMock()
        helper.register_for_llm.return_value = lambda f: f
        executor.register_for_execution.return_value = lambda f: f

        # Should not raise
        register_remote_desktop_tools_if_available(ctx, helper, executor)
        self.assertGreaterEqual(helper.register_for_llm.call_count, 7)


if __name__ == '__main__':
    unittest.main()
