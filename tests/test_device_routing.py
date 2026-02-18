"""
Tests for Cross-Device Agent Communication - DeviceBinding extensions,
FleetCommand types, and DeviceRoutingService.
"""
import json
import os
import sys
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')


# ─── DeviceBinding Model Tests ───

class TestDeviceBindingModel:
    """Verify form_factor + capabilities_json columns and to_dict()."""

    def _make_binding(self, **kwargs):
        from integrations.social.models import DeviceBinding
        return DeviceBinding(
            id='test-id',
            user_id='user-1',
            device_id='dev-1',
            device_name='My Phone',
            platform='android',
            form_factor=kwargs.get('form_factor', 'phone'),
            capabilities_json=kwargs.get('capabilities_json', '{}'),
        )

    def test_form_factor_column_exists(self):
        from integrations.social.models import DeviceBinding
        assert hasattr(DeviceBinding, 'form_factor')

    def test_capabilities_json_column_exists(self):
        from integrations.social.models import DeviceBinding
        assert hasattr(DeviceBinding, 'capabilities_json')

    def test_default_form_factor(self):
        b = self._make_binding()
        assert b.form_factor == 'phone'

    def test_capabilities_property_parses_json(self):
        b = self._make_binding(capabilities_json='{"tts": true, "mic": true}')
        assert b.capabilities == {'tts': True, 'mic': True}

    def test_capabilities_property_handles_empty(self):
        b = self._make_binding(capabilities_json='{}')
        assert b.capabilities == {}

    def test_capabilities_property_handles_invalid_json(self):
        b = self._make_binding(capabilities_json='not-json')
        assert b.capabilities == {}

    def test_capabilities_property_handles_none(self):
        b = self._make_binding(capabilities_json=None)
        assert b.capabilities == {}

    def test_to_dict_includes_form_factor(self):
        b = self._make_binding(form_factor='watch')
        d = b.to_dict()
        assert d['form_factor'] == 'watch'

    def test_to_dict_includes_capabilities(self):
        b = self._make_binding(capabilities_json='{"tts": true}')
        d = b.to_dict()
        assert d['capabilities'] == {'tts': True}


# ─── Schema Migration Tests ───

class TestSchemaMigrationV27:
    """Verify SCHEMA_VERSION bumped to 27."""

    def test_schema_version_at_least_27(self):
        from integrations.social.migrations import SCHEMA_VERSION
        assert SCHEMA_VERSION >= 27


# ─── FleetCommand Types Tests ───

class TestFleetCommandTypes:
    """Verify tts_stream and agent_consent command types."""

    def test_tts_stream_in_valid_types(self):
        from integrations.social.fleet_command import VALID_COMMAND_TYPES
        assert 'tts_stream' in VALID_COMMAND_TYPES

    def test_agent_consent_in_valid_types(self):
        from integrations.social.fleet_command import VALID_COMMAND_TYPES
        assert 'agent_consent' in VALID_COMMAND_TYPES

    def test_execute_tts_stream(self):
        from integrations.social.fleet_command import FleetCommandService
        result = FleetCommandService.execute_command('tts_stream', {
            'text': 'Hello world',
            'voice': 'default',
            'lang': 'en',
        })
        assert result['success'] is True
        assert 'TTS queued' in result['message']
        # Clean up
        os.environ.pop('HEVOLVE_TTS_PENDING', None)

    def test_execute_tts_stream_empty_text(self):
        from integrations.social.fleet_command import FleetCommandService
        result = FleetCommandService.execute_command('tts_stream', {'text': ''})
        assert result['success'] is False

    def test_execute_tts_stream_with_relay(self):
        from integrations.social.fleet_command import FleetCommandService
        result = FleetCommandService.execute_command('tts_stream', {
            'text': 'Hello',
            'relay_to_device_id': 'watch-123',
        })
        assert result['success'] is True
        assert 'relay' in result['message']
        pending = json.loads(os.environ.pop('HEVOLVE_TTS_PENDING', '{}'))
        assert pending['relay_to_device_id'] == 'watch-123'

    def test_execute_agent_consent(self):
        from integrations.social.fleet_command import FleetCommandService
        result = FleetCommandService.execute_command('agent_consent', {
            'action': 'send_email',
            'agent_id': 'agent-1',
            'description': 'Send weekly digest',
        })
        assert result['success'] is True
        assert 'send_email' in result['message']
        pending = json.loads(os.environ.pop('HEVOLVE_CONSENT_PENDING', '{}'))
        assert pending['action'] == 'send_email'

    def test_execute_agent_consent_empty_action(self):
        from integrations.social.fleet_command import FleetCommandService
        result = FleetCommandService.execute_command('agent_consent', {})
        assert result['success'] is False


# ─── DeviceRoutingService Tests ───

class TestDeviceRoutingService:
    """Test routing logic for TTS and consent."""

    def _make_mock_device(self, device_id, form_factor, capabilities, **kwargs):
        """Create a mock DeviceBinding object."""
        mock = MagicMock()
        mock.device_id = device_id
        mock.form_factor = form_factor
        mock.capabilities_json = json.dumps(capabilities)
        mock.is_active = True
        mock.last_sync_at = kwargs.get('last_sync_at', datetime.utcnow())

        # Mock the capabilities property
        type(mock).capabilities = property(lambda self: capabilities)
        mock.to_dict.return_value = {
            'id': f'id-{device_id}',
            'user_id': 'user-1',
            'device_id': device_id,
            'form_factor': form_factor,
            'capabilities': capabilities,
        }
        return mock

    def test_get_user_device_map(self):
        from integrations.social.device_routing_service import DeviceRoutingService

        phone = self._make_mock_device('phone-1', 'phone', {'tts': True})
        watch = self._make_mock_device('watch-1', 'watch', {'tts': False})

        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = [phone, watch]

        result = DeviceRoutingService.get_user_device_map(db, 'user-1')
        assert len(result) == 2

    def test_pick_device_selects_phone_over_desktop(self):
        from integrations.social.device_routing_service import DeviceRoutingService

        phone = self._make_mock_device('phone-1', 'phone', {'tts': True})
        desktop = self._make_mock_device('desk-1', 'desktop', {'tts': True})

        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = [desktop, phone]

        result = DeviceRoutingService.pick_device(db, 'user-1', 'tts')
        assert result['device_id'] == 'phone-1'

    def test_pick_device_skips_non_capable(self):
        from integrations.social.device_routing_service import DeviceRoutingService

        watch = self._make_mock_device('watch-1', 'watch', {'tts': False, 'mic': True})

        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = [watch]

        result = DeviceRoutingService.pick_device(db, 'user-1', 'tts')
        assert result is None

    def test_pick_device_returns_none_no_devices(self):
        from integrations.social.device_routing_service import DeviceRoutingService

        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = []

        result = DeviceRoutingService.pick_device(db, 'user-1', 'tts')
        assert result is None

    @patch('integrations.social.device_routing_service.FleetCommandService')
    def test_route_tts_to_phone(self, MockFleet):
        from integrations.social.device_routing_service import DeviceRoutingService

        phone = self._make_mock_device('phone-1', 'phone', {'tts': True})
        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = [phone]

        result = DeviceRoutingService.route_tts(db, 'user-1', 'Hello')
        assert result['success'] is True
        assert result['device_id'] == 'phone-1'
        assert result['method'] == 'fleet_command'
        MockFleet.push_command.assert_called_once()

    @patch('integrations.social.device_routing_service.FleetCommandService')
    def test_route_tts_relay_to_watch(self, MockFleet):
        from integrations.social.device_routing_service import DeviceRoutingService

        phone = self._make_mock_device('phone-1', 'phone', {'tts': True})
        watch = self._make_mock_device('watch-1', 'watch', {'tts': False})
        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = [phone, watch]

        result = DeviceRoutingService.route_tts(db, 'user-1', 'Hello from agent')
        assert result['success'] is True
        assert result['device_id'] == 'phone-1'
        assert result['relay_to'] == 'watch-1'

        # Verify fleet command includes relay
        call_args = MockFleet.push_command.call_args
        params = call_args[0][3]  # 4th positional arg is params
        assert params['relay_to_device_id'] == 'watch-1'

    def test_route_tts_no_devices(self):
        from integrations.social.device_routing_service import DeviceRoutingService

        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = []

        result = DeviceRoutingService.route_tts(db, 'user-1', 'Hello')
        assert result['success'] is False
        assert 'No devices' in result['error']

    @patch('integrations.social.device_routing_service.NotificationService')
    def test_route_tts_fallback_notification(self, MockNotif):
        from integrations.social.device_routing_service import DeviceRoutingService

        # Device with no TTS capability
        watch = self._make_mock_device('watch-1', 'watch', {'tts': False})
        db = MagicMock()
        db.query.return_value.filter_by.return_value.all.return_value = [watch]

        result = DeviceRoutingService.route_tts(db, 'user-1', 'Hello')
        assert result['success'] is True
        assert result['method'] == 'notification_fallback'
        MockNotif.create.assert_called_once()

    @patch('integrations.social.device_routing_service.FleetCommandService')
    @patch('integrations.social.device_routing_service.NotificationService')
    def test_request_consent(self, MockNotif, MockFleet):
        from integrations.social.device_routing_service import DeviceRoutingService

        phone = self._make_mock_device('phone-1', 'phone', {'tts': True})
        db = MagicMock()
        db.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = [phone]
        MockFleet.push_command.return_value = {'id': 42}

        result = DeviceRoutingService.request_consent(
            db, 'user-1', 'send_email', 'agent-1', 'Send weekly digest',
        )
        assert result['success'] is True
        assert result['command_id'] == 42
        assert result['device_id'] == 'phone-1'
        MockNotif.create.assert_called_once()
        MockFleet.push_command.assert_called_once()

    @patch('integrations.social.device_routing_service.NotificationService')
    def test_request_consent_no_devices(self, MockNotif):
        from integrations.social.device_routing_service import DeviceRoutingService

        db = MagicMock()
        db.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = []

        result = DeviceRoutingService.request_consent(
            db, 'user-1', 'send_email', 'agent-1',
        )
        assert result['success'] is True
        assert result['method'] == 'notification_only'
        # Still creates a notification
        MockNotif.create.assert_called_once()

    @patch('integrations.social.device_routing_service.FleetCommandService')
    @patch('integrations.social.device_routing_service.NotificationService')
    def test_request_consent_prefers_phone(self, MockNotif, MockFleet):
        from integrations.social.device_routing_service import DeviceRoutingService

        desktop = self._make_mock_device('desk-1', 'desktop', {'tts': True})
        phone = self._make_mock_device('phone-1', 'phone', {'tts': True})
        db = MagicMock()
        db.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = [desktop, phone]
        MockFleet.push_command.return_value = {'id': 99}

        result = DeviceRoutingService.request_consent(
            db, 'user-1', 'speak', 'agent-1',
        )
        assert result['device_id'] == 'phone-1'
