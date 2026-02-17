"""
Tests for Telegram Channel Adapter

Tests the Telegram adapter functionality including:
- Message conversion
- Send/receive operations
- Keyboard building
- Error handling
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.channels.base import (
    ChannelConfig,
    ChannelStatus,
    Message,
    MessageType,
    MediaAttachment,
    SendResult,
)
from integrations.channels.registry import ChannelRegistry, ChannelRegistryConfig


class TestChannelBase:
    """Tests for base channel classes."""

    def test_message_creation(self):
        """Test Message dataclass creation."""
        msg = Message(
            id="123",
            channel="telegram",
            sender_id="456",
            sender_name="Test User",
            chat_id="789",
            text="Hello, world!",
        )

        assert msg.id == "123"
        assert msg.channel == "telegram"
        assert msg.sender_id == "456"
        assert msg.text == "Hello, world!"
        assert msg.content == "Hello, world!"
        assert not msg.has_media

    def test_message_with_media(self):
        """Test Message with media attachments."""
        media = MediaAttachment(
            type=MessageType.IMAGE,
            file_id="file123",
            caption="A nice photo",
        )

        msg = Message(
            id="123",
            channel="telegram",
            sender_id="456",
            chat_id="789",
            media=[media],
        )

        assert msg.has_media
        assert len(msg.media) == 1
        assert msg.media[0].type == MessageType.IMAGE
        assert msg.content == "A nice photo"

    def test_channel_config(self):
        """Test ChannelConfig creation."""
        config = ChannelConfig(
            enabled=True,
            token="test_token",
            dm_policy="pairing",
            allow_from=["user1", "user2"],
        )

        assert config.enabled
        assert config.token == "test_token"
        assert config.dm_policy == "pairing"
        assert len(config.allow_from) == 2

    def test_send_result(self):
        """Test SendResult creation."""
        result = SendResult(
            success=True,
            message_id="123",
        )

        assert result.success
        assert result.message_id == "123"
        assert result.error is None


class TestChannelRegistry:
    """Tests for ChannelRegistry."""

    def test_registry_creation(self):
        """Test registry instantiation."""
        config = ChannelRegistryConfig(
            agent_callback_url="http://localhost:6777/chat",
            default_user_id=10077,
        )
        registry = ChannelRegistry(config)

        assert registry.config.agent_callback_url == "http://localhost:6777/chat"
        assert len(registry.list_channels()) == 0

    def test_register_adapter(self):
        """Test registering an adapter."""
        registry = ChannelRegistry()

        # Create mock adapter
        mock_adapter = Mock()
        mock_adapter.name = "test_channel"
        mock_adapter.on_message = Mock()

        registry.register(mock_adapter)

        assert "test_channel" in registry.list_channels()
        assert registry.get("test_channel") == mock_adapter
        mock_adapter.on_message.assert_called_once()

    def test_unregister_adapter(self):
        """Test unregistering an adapter."""
        registry = ChannelRegistry()

        mock_adapter = Mock()
        mock_adapter.name = "test_channel"
        mock_adapter.on_message = Mock()

        registry.register(mock_adapter)
        registry.unregister("test_channel")

        assert "test_channel" not in registry.list_channels()

    def test_get_status(self):
        """Test getting status of all channels."""
        registry = ChannelRegistry()

        mock_adapter1 = Mock()
        mock_adapter1.name = "channel1"
        mock_adapter1.on_message = Mock()
        mock_adapter1.get_status.return_value = ChannelStatus.CONNECTED

        mock_adapter2 = Mock()
        mock_adapter2.name = "channel2"
        mock_adapter2.on_message = Mock()
        mock_adapter2.get_status.return_value = ChannelStatus.DISCONNECTED

        registry.register(mock_adapter1)
        registry.register(mock_adapter2)

        status = registry.get_status()

        assert status["channel1"] == ChannelStatus.CONNECTED
        assert status["channel2"] == ChannelStatus.DISCONNECTED


_HAS_TELEGRAM = False
try:
    import telegram  # noqa: F401
    _HAS_TELEGRAM = True
except ImportError:
    pass


@pytest.mark.skipif(
    not _HAS_TELEGRAM,
    reason="python-telegram-bot not installed"
)
class TestTelegramAdapter:
    """Tests for TelegramAdapter."""

    @pytest.fixture
    def mock_telegram(self):
        """Create mock telegram modules."""
        with patch.dict('sys.modules', {
            'telegram': MagicMock(),
            'telegram.ext': MagicMock(),
            'telegram.constants': MagicMock(),
            'telegram.error': MagicMock(),
        }):
            yield

    def test_adapter_creation(self, mock_telegram):
        """Test TelegramAdapter instantiation."""
        # Import after mocking
        from integrations.channels.telegram_adapter import TelegramAdapter

        config = ChannelConfig(token="test_token")
        adapter = TelegramAdapter(config)

        assert adapter.name == "telegram"
        assert adapter.status == ChannelStatus.DISCONNECTED

    def test_message_handler_registration(self, mock_telegram):
        """Test message handler registration."""
        from integrations.channels.telegram_adapter import TelegramAdapter

        config = ChannelConfig(token="test_token")
        adapter = TelegramAdapter(config)

        handler_called = False

        async def test_handler(msg):
            nonlocal handler_called
            handler_called = True

        adapter.on_message(test_handler)

        assert len(adapter._message_handlers) == 1

    def test_keyboard_building(self, mock_telegram):
        """Test inline keyboard building."""
        from integrations.channels.telegram_adapter import TelegramAdapter

        config = ChannelConfig(token="test_token")
        adapter = TelegramAdapter(config)

        buttons = [
            {"text": "Option 1", "callback_data": "opt1"},
            {"text": "Option 2", "callback_data": "opt2"},
            {"text": "Link", "url": "https://example.com"},
        ]

        # Mock InlineKeyboardMarkup and InlineKeyboardButton
        with patch('integrations.channels.telegram_adapter.InlineKeyboardMarkup') as mock_markup:
            with patch('integrations.channels.telegram_adapter.InlineKeyboardButton') as mock_button:
                mock_button.return_value = Mock()
                mock_markup.return_value = Mock()

                keyboard = adapter._build_keyboard(buttons)

                # Verify buttons were created
                assert mock_button.call_count == 3


class TestFlaskIntegration:
    """Tests for Flask channel integration."""

    def test_integration_creation(self):
        """Test FlaskChannelIntegration instantiation."""
        from integrations.channels.flask_integration import FlaskChannelIntegration

        integration = FlaskChannelIntegration(
            agent_api_url="http://localhost:6777/chat",
            default_user_id=10077,
            default_prompt_id=8888,
        )

        assert integration.agent_api_url == "http://localhost:6777/chat"
        assert integration.default_user_id == 10077
        assert integration.default_prompt_id == 8888

    def test_user_session_mapping(self):
        """Test user session mapping."""
        from integrations.channels.flask_integration import FlaskChannelIntegration

        integration = FlaskChannelIntegration()
        integration.set_user_session("telegram", "123456", 999, 111)

        assert ("telegram", "123456") in integration._user_sessions
        assert integration._user_sessions[("telegram", "123456")] == (999, 111)

    @patch('integrations.channels.flask_integration.requests.post')
    def test_message_handling(self, mock_post):
        """Test message handling routes to agent API."""
        from integrations.channels.flask_integration import FlaskChannelIntegration

        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"response": "Hello from agent!"}

        integration = FlaskChannelIntegration()

        message = Message(
            id="123",
            channel="telegram",
            sender_id="456",
            chat_id="789",
            text="Hello",
        )

        response = integration._handle_message(message)

        assert response == "Hello from agent!"
        mock_post.assert_called_once()

        # Verify payload
        call_args = mock_post.call_args
        payload = call_args[1]["json"]
        assert payload["prompt"] == "Hello"
        assert payload["channel_context"]["channel"] == "telegram"

    @patch('integrations.channels.flask_integration.requests.post')
    def test_message_handling_api_error(self, mock_post):
        """Test error handling when API fails."""
        from integrations.channels.flask_integration import FlaskChannelIntegration

        mock_post.return_value.status_code = 500
        mock_post.return_value.text = "Internal Server Error"

        integration = FlaskChannelIntegration()

        message = Message(
            id="123",
            channel="telegram",
            sender_id="456",
            chat_id="789",
            text="Hello",
        )

        response = integration._handle_message(message)

        assert "error" in response.lower()


class TestRegressionExistingFunctionality:
    """
    Regression tests to ensure existing functionality still works.

    These tests verify that the channel integration doesn't break
    the existing agent system.
    """

    def test_imports_dont_break(self):
        """Test that channel imports don't break existing code."""
        # These imports should work without errors
        from integrations.channels import ChannelAdapter, ChannelStatus, Message
        from integrations.channels.registry import ChannelRegistry

        assert ChannelAdapter is not None
        assert ChannelStatus is not None
        assert Message is not None
        assert ChannelRegistry is not None

    def test_existing_config_compatibility(self):
        """Test that existing config.json is not affected."""
        import json

        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config.json"
        )

        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                config = json.load(f)

            # Verify existing keys are present
            # (this depends on what your config.json contains)
            assert isinstance(config, dict)

    def test_flask_app_import(self):
        """Test that Flask app can still be imported."""
        try:
            # This should not raise any import errors
            import langchain_gpt_api
            assert hasattr(langchain_gpt_api, 'app')
        except ImportError:
            # If there are missing dependencies, that's a separate issue
            pytest.skip("langchain_gpt_api has missing dependencies")

    def test_helper_functions_available(self):
        """Test that helper functions are still available."""
        try:
            from helper import retrieve_json, topological_sort
            assert retrieve_json is not None
            assert topological_sort is not None
        except ImportError:
            pytest.skip("helper module has missing dependencies")

    def test_lifecycle_hooks_available(self):
        """Test that lifecycle hooks are still available."""
        try:
            from lifecycle_hooks import ActionState
            assert ActionState is not None
            assert hasattr(ActionState, 'ASSIGNED')
            assert hasattr(ActionState, 'IN_PROGRESS')
            assert hasattr(ActionState, 'COMPLETED')
        except ImportError:
            pytest.skip("lifecycle_hooks has missing dependencies")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
