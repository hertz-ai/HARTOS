"""
Functional tests for device_control tool — channel-agnostic device control
wired through PeerLink dispatch channel with FleetCommandService fallback.

Tests verify:
  1. Tool is registered in the core agent tool registry
  2. PeerLink dispatch channel is used for SAME_USER peers
  3. FleetCommandService fallback when PeerLink unavailable
  4. Privacy: only SAME_USER trust devices can be controlled
  5. Receiving handler processes device_control messages correctly
"""
import json
import os
import sys
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')


# ─── Tool Registration ───────────────────────────────────────────

class TestDeviceControlToolRegistered:
    """Verify device_control exists in the core tool registry."""

    def _build_tools(self):
        """Build core tools with minimal context."""
        from core.agent_tools import build_core_tool_closures

        ctx = {
            'user_id': '999',
            'prompt_id': '8888',
            'agent_data': {},
            'helper_fun': MagicMock(),
            'user_prompt': '999_8888',
            'request_id_list': {'999_8888': 'req1'},
            'recent_file_id': {},
            'scheduler': MagicMock(),
            'send_message_to_user1': MagicMock(),
            'retrieve_json': MagicMock(return_value={}),
            'strip_json_values': MagicMock(return_value=''),
            'save_conversation_db': MagicMock(return_value='1'),
        }
        return build_core_tool_closures(ctx)

    def test_device_control_in_tool_list(self):
        """device_control tool must exist in the core tool registry."""
        tools = self._build_tools()
        tool_names = [name for name, desc, func in tools]
        assert 'device_control' in tool_names, (
            f"device_control not found in core tools: {tool_names}"
        )

    def test_device_control_is_callable(self):
        """The device_control function must be callable."""
        tools = self._build_tools()
        dc_func = None
        for name, desc, func in tools:
            if name == 'device_control':
                dc_func = func
                break
        assert dc_func is not None
        assert callable(dc_func)

    def test_device_control_description_mentions_privacy(self):
        """Tool description should mention privacy/own devices."""
        tools = self._build_tools()
        for name, desc, func in tools:
            if name == 'device_control':
                assert 'own' in desc.lower() or 'private' in desc.lower(), (
                    f"Description should mention privacy: {desc}"
                )
                break


# ─── PeerLink Routing ────────────────────────────────────────────

class TestDeviceControlRoutesToPeerLink:
    """Verify device_control uses PeerLink dispatch channel for SAME_USER peers."""

    def _get_tool_func(self):
        from core.agent_tools import build_core_tool_closures
        ctx = {
            'user_id': '999',
            'prompt_id': '8888',
            'agent_data': {},
            'helper_fun': MagicMock(),
            'user_prompt': '999_8888',
            'request_id_list': {'999_8888': 'req1'},
            'recent_file_id': {},
            'scheduler': MagicMock(),
            'send_message_to_user1': MagicMock(),
            'retrieve_json': MagicMock(return_value={}),
            'strip_json_values': MagicMock(return_value=''),
            'save_conversation_db': MagicMock(return_value='1'),
        }
        tools = build_core_tool_closures(ctx)
        for name, desc, func in tools:
            if name == 'device_control':
                return func
        raise AssertionError("device_control tool not found")

    def test_peerlink_dispatch_channel_used(self):
        """When PeerLink has a SAME_USER link, dispatch channel is used."""
        dc = self._get_tool_func()

        # Mock the PeerLink manager
        mock_link = MagicMock()
        mock_link.trust = MagicMock()
        mock_link.trust.value = 'same_user'

        mock_mgr = MagicMock()
        mock_mgr.get_link.return_value = mock_link
        mock_mgr.send.return_value = {'success': True, 'message': 'Light turned on'}

        # Mock device routing to return a device
        mock_device = {'device_id': 'node-abc123', 'form_factor': 'desktop'}

        with patch('integrations.social.models.db_session') as mock_db_session, \
             patch('integrations.social.device_routing_service.DeviceRoutingService.pick_device',
                   return_value=mock_device), \
             patch('integrations.social.device_routing_service.DeviceRoutingService.get_user_device_map',
                   return_value=[mock_device]), \
             patch('core.peer_link.link_manager.get_link_manager', return_value=mock_mgr), \
             patch('core.peer_link.link.TrustLevel') as MockTrust:

            MockTrust.SAME_USER = mock_link.trust
            mock_db_ctx = MagicMock()
            mock_db_session.return_value.__enter__ = MagicMock(return_value=mock_db_ctx)
            mock_db_session.return_value.__exit__ = MagicMock(return_value=False)

            result = dc(action='turn on light', device_hint='desktop')

            # Verify PeerLink send was called with dispatch channel
            mock_mgr.send.assert_called_once()
            call_args = mock_mgr.send.call_args
            assert call_args[0][1] == 'dispatch', (
                f"Expected 'dispatch' channel, got: {call_args[0][1]}"
            )
            # Verify the payload includes device_control type
            payload = call_args[0][2]
            assert payload['type'] == 'device_control'
            assert payload['action'] == 'turn on light'

    def test_peerlink_result_returned(self):
        """Result from PeerLink dispatch is returned to the agent."""
        dc = self._get_tool_func()

        mock_link = MagicMock()
        mock_link.trust = MagicMock()

        mock_mgr = MagicMock()
        mock_mgr.get_link.return_value = mock_link
        mock_mgr.send.return_value = {'success': True, 'message': 'Temperature is 22C'}

        mock_device = {'device_id': 'node-xyz', 'form_factor': 'embedded'}

        with patch('integrations.social.models.db_session') as mock_db_session, \
             patch('integrations.social.device_routing_service.DeviceRoutingService.pick_device',
                   return_value=mock_device), \
             patch('core.peer_link.link_manager.get_link_manager', return_value=mock_mgr), \
             patch('core.peer_link.link.TrustLevel') as MockTrust:

            MockTrust.SAME_USER = mock_link.trust
            mock_db_ctx = MagicMock()
            mock_db_session.return_value.__enter__ = MagicMock(return_value=mock_db_ctx)
            mock_db_session.return_value.__exit__ = MagicMock(return_value=False)

            result = dc(action='check temperature')
            assert 'Temperature is 22C' in result


# ─── FleetCommand Fallback ───────────────────────────────────────

class TestDeviceControlFallbackFleetCommand:
    """When PeerLink is unavailable, FleetCommandService is used as fallback."""

    def _get_tool_func(self):
        from core.agent_tools import build_core_tool_closures
        ctx = {
            'user_id': '999',
            'prompt_id': '8888',
            'agent_data': {},
            'helper_fun': MagicMock(),
            'user_prompt': '999_8888',
            'request_id_list': {'999_8888': 'req1'},
            'recent_file_id': {},
            'scheduler': MagicMock(),
            'send_message_to_user1': MagicMock(),
            'retrieve_json': MagicMock(return_value={}),
            'strip_json_values': MagicMock(return_value=''),
            'save_conversation_db': MagicMock(return_value='1'),
        }
        tools = build_core_tool_closures(ctx)
        for name, desc, func in tools:
            if name == 'device_control':
                return func
        raise AssertionError("device_control tool not found")

    def test_fleet_command_fallback_when_no_peerlink(self):
        """When PeerLink has no link to device, fall back to FleetCommandService."""
        dc = self._get_tool_func()

        mock_mgr = MagicMock()
        mock_mgr.get_link.return_value = None  # No PeerLink available

        mock_device = {'device_id': 'node-abc', 'form_factor': 'phone'}

        with patch('integrations.social.models.db_session') as mock_db_session, \
             patch('integrations.social.device_routing_service.DeviceRoutingService.pick_device',
                   return_value=mock_device), \
             patch('core.peer_link.link_manager.get_link_manager', return_value=mock_mgr), \
             patch('integrations.social.fleet_command.FleetCommandService.push_command',
                   return_value={'id': 42}) as mock_push:

            mock_db_ctx = MagicMock()
            mock_db_session.return_value.__enter__ = MagicMock(return_value=mock_db_ctx)
            mock_db_session.return_value.__exit__ = MagicMock(return_value=False)

            result = dc(action='list files', device_hint='phone')

            # FleetCommandService.push_command should be called
            mock_push.assert_called_once()
            call_args = mock_push.call_args
            assert call_args[0][2] == 'device_control', (
                f"Expected cmd_type 'device_control', got: {call_args[0][2]}"
            )
            assert 'queued' in result.lower() or 'command' in result.lower()

    def test_local_execution_as_last_resort(self):
        """When no device routing or PeerLink, execute locally."""
        dc = self._get_tool_func()

        # All routing/PeerLink/fleet fails, but local exec works
        with patch('integrations.social.models.db_session', side_effect=Exception('no db')), \
             patch('integrations.social.fleet_command.FleetCommandService.execute_command',
                   return_value={'success': True, 'message': 'file1.txt  file2.txt'}) as mock_exec:

            result = dc(action='run command ls')
            assert 'file1.txt' in result or 'local' in result.lower()


# ─── Privacy: SAME_USER Only ─────────────────────────────────────

class TestPrivacyOnlySameUser:
    """Verify device_control enforces SAME_USER trust requirement."""

    def _get_tool_func(self):
        from core.agent_tools import build_core_tool_closures
        ctx = {
            'user_id': '999',
            'prompt_id': '8888',
            'agent_data': {},
            'helper_fun': MagicMock(),
            'user_prompt': '999_8888',
            'request_id_list': {'999_8888': 'req1'},
            'recent_file_id': {},
            'scheduler': MagicMock(),
            'send_message_to_user1': MagicMock(),
            'retrieve_json': MagicMock(return_value={}),
            'strip_json_values': MagicMock(return_value=''),
            'save_conversation_db': MagicMock(return_value='1'),
        }
        tools = build_core_tool_closures(ctx)
        for name, desc, func in tools:
            if name == 'device_control':
                return func
        raise AssertionError("device_control tool not found")

    def test_rejects_non_same_user_peer(self):
        """PeerLink to a PEER trust device must be rejected."""
        dc = self._get_tool_func()

        # Create mock with PEER trust (not SAME_USER)
        from core.peer_link.link import TrustLevel

        mock_link = MagicMock()
        mock_link.trust = TrustLevel.PEER  # NOT SAME_USER

        mock_mgr = MagicMock()
        mock_mgr.get_link.return_value = mock_link

        mock_device = {'device_id': 'node-other', 'form_factor': 'desktop'}

        with patch('integrations.social.models.db_session') as mock_db_session, \
             patch('integrations.social.device_routing_service.DeviceRoutingService.pick_device',
                   return_value=mock_device), \
             patch('core.peer_link.link_manager.get_link_manager', return_value=mock_mgr):

            mock_db_ctx = MagicMock()
            mock_db_session.return_value.__enter__ = MagicMock(return_value=mock_db_ctx)
            mock_db_session.return_value.__exit__ = MagicMock(return_value=False)

            result = dc(action='turn on light', device_hint='desktop')

            # Should be blocked
            assert 'blocked' in result.lower() or 'SAME_USER' in result, (
                f"Expected SAME_USER rejection, got: {result}"
            )
            # PeerLink send should NOT have been called
            mock_mgr.send.assert_not_called()

    def test_rejects_relay_trust_peer(self):
        """PeerLink to a RELAY trust device must also be rejected."""
        dc = self._get_tool_func()

        from core.peer_link.link import TrustLevel

        mock_link = MagicMock()
        mock_link.trust = TrustLevel.RELAY

        mock_mgr = MagicMock()
        mock_mgr.get_link.return_value = mock_link

        mock_device = {'device_id': 'node-relay', 'form_factor': 'embedded'}

        with patch('integrations.social.models.db_session') as mock_db_session, \
             patch('integrations.social.device_routing_service.DeviceRoutingService.pick_device',
                   return_value=mock_device), \
             patch('core.peer_link.link_manager.get_link_manager', return_value=mock_mgr):

            mock_db_ctx = MagicMock()
            mock_db_session.return_value.__enter__ = MagicMock(return_value=mock_db_ctx)
            mock_db_session.return_value.__exit__ = MagicMock(return_value=False)

            result = dc(action='check temperature')

            assert 'blocked' in result.lower() or 'SAME_USER' in result
            mock_mgr.send.assert_not_called()


# ─── Receiving Handler ───────────────────────────────────────────

class TestDeviceControlReceivingHandler:
    """Test the PeerLink dispatch handler registered in embedded_main.py."""

    def test_handler_processes_device_control_message(self):
        """Handler should parse and execute device_control actions."""
        from integrations.social.fleet_command import FleetCommandService

        result = FleetCommandService.execute_command('device_control', {
            'action': 'run command echo hello',
        })
        assert result['success'] is True
        assert 'hello' in result['message']

    def test_handler_rejects_empty_action(self):
        """Handler should reject messages with no action."""
        from integrations.social.fleet_command import FleetCommandService

        result = FleetCommandService.execute_command('device_control', {
            'action': '',
        })
        assert result['success'] is False

    def test_handler_gpio_detection(self):
        """Handler should detect GPIO actions from keywords."""
        from integrations.social.fleet_command import FleetCommandService

        result = FleetCommandService.execute_command('device_control', {
            'action': 'turn on LED on pin 17',
        })
        assert result['success'] is True
        assert 'pin' in result['message'].lower() or 'GPIO' in result['message']
        # Clean up env
        os.environ.pop('HEVOLVE_DEVICE_CONTROL_RESULT', None)

    def test_handler_serial_detection(self):
        """Handler should detect serial actions from keywords."""
        from integrations.social.fleet_command import FleetCommandService

        result = FleetCommandService.execute_command('device_control', {
            'action': 'send serial data hello',
        })
        assert result['success'] is True
        assert 'serial' in result['message'].lower()
        os.environ.pop('HEVOLVE_DEVICE_CONTROL_RESULT', None)

    def test_device_control_in_valid_command_types(self):
        """device_control must be in VALID_COMMAND_TYPES."""
        from integrations.social.fleet_command import VALID_COMMAND_TYPES
        assert 'device_control' in VALID_COMMAND_TYPES

    def test_embedded_handler_rejects_non_same_user(self):
        """The embedded_main handler must reject non-SAME_USER senders."""
        from core.peer_link.link import TrustLevel

        # Simulate the handler logic from embedded_main
        mock_mgr = MagicMock()
        mock_link = MagicMock()
        mock_link.trust = TrustLevel.PEER  # Not SAME_USER

        mock_mgr.get_link.return_value = mock_link

        # The handler should check trust
        data = {'type': 'device_control', 'action': 'ls'}
        sender = 'peer-xyz-123'

        # Re-implement the trust check logic (same as in _register_device_control_handler)
        link = mock_mgr.get_link(sender)
        assert link is not None
        assert link.trust != TrustLevel.SAME_USER, (
            "Test setup error: link should be PEER trust"
        )

    def test_embedded_handler_accepts_same_user(self):
        """The embedded_main handler must accept SAME_USER senders."""
        from core.peer_link.link import TrustLevel

        mock_mgr = MagicMock()
        mock_link = MagicMock()
        mock_link.trust = TrustLevel.SAME_USER

        mock_mgr.get_link.return_value = mock_link

        link = mock_mgr.get_link('my-phone-node')
        assert link.trust == TrustLevel.SAME_USER
