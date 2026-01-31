"""
Tests for Discord Channel Adapter

Tests the Discord adapter functionality including:
- Message conversion
- Send/receive operations
- Embed building
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


class TestDiscordAdapter:
    """Tests for DiscordAdapter."""

    @pytest.fixture
    def mock_discord(self):
        """Create mock discord modules."""
        mock_intents = MagicMock()
        mock_intents.default.return_value = mock_intents

        mock_bot = MagicMock()
        mock_bot.user = MagicMock()
        mock_bot.user.id = 123456789
        mock_bot.user.name = "TestBot"
        mock_bot.user.discriminator = "1234"

        with patch.dict('sys.modules', {
            'discord': MagicMock(
                Intents=mock_intents,
                ui=MagicMock(),
                Embed=MagicMock(),
                File=MagicMock(),
                ButtonStyle=MagicMock(),
            ),
            'discord.ext': MagicMock(),
            'discord.ext.commands': MagicMock(Bot=MagicMock(return_value=mock_bot)),
        }):
            yield mock_bot

    def test_adapter_creation(self, mock_discord):
        """Test DiscordAdapter instantiation."""
        from integrations.channels.discord_adapter import DiscordAdapter

        config = ChannelConfig(token="test_token")
        adapter = DiscordAdapter(config)

        assert adapter.name == "discord"
        assert adapter.status == ChannelStatus.DISCONNECTED

    def test_message_handler_registration(self, mock_discord):
        """Test message handler registration."""
        from integrations.channels.discord_adapter import DiscordAdapter

        config = ChannelConfig(token="test_token")
        adapter = DiscordAdapter(config)

        handler_called = False

        async def test_handler(msg):
            nonlocal handler_called
            handler_called = True

        adapter.on_message(test_handler)
        assert len(adapter._message_handlers) == 1


class TestDiscordMessageConversion:
    """Tests for Discord message conversion."""

    def test_message_structure(self):
        """Test that Message can hold Discord-specific data."""
        msg = Message(
            id="123456789",
            channel="discord",
            sender_id="987654321",
            sender_name="TestUser",
            chat_id="111222333",
            text="Hello from Discord!",
            is_group=True,
            is_bot_mentioned=True,
            raw={
                "guild_id": "444555666",
                "guild_name": "Test Server",
                "channel_name": "general",
            },
        )

        assert msg.channel == "discord"
        assert msg.is_group
        assert msg.is_bot_mentioned
        assert msg.raw["guild_name"] == "Test Server"

    def test_media_attachment(self):
        """Test media attachment handling."""
        media = MediaAttachment(
            type=MessageType.IMAGE,
            url="https://cdn.discord.com/attachments/123/456/image.png",
            file_name="image.png",
            file_size=1024,
            mime_type="image/png",
        )

        assert media.type == MessageType.IMAGE
        assert media.url.startswith("https://cdn.discord.com")


class TestDiscordFlaskIntegration:
    """Tests for Discord-Flask integration."""

    def test_discord_registration(self):
        """Test Discord adapter registration with Flask integration."""
        from integrations.channels.flask_integration import FlaskChannelIntegration

        integration = FlaskChannelIntegration()

        # Verify registry exists
        assert integration.registry is not None
        assert len(integration.registry.list_channels()) == 0

    @patch('integrations.channels.flask_integration.requests.post')
    def test_discord_message_routing(self, mock_post):
        """Test that Discord messages route to agent API."""
        from integrations.channels.flask_integration import FlaskChannelIntegration

        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"response": "Hello from agent!"}

        integration = FlaskChannelIntegration()

        message = Message(
            id="123456789",
            channel="discord",
            sender_id="987654321",
            sender_name="TestUser",
            chat_id="111222333",
            text="Hello",
            is_group=True,
            raw={"guild_name": "Test Server"},
        )

        response = integration._handle_message(message)

        assert response == "Hello from agent!"

        # Verify payload includes Discord context
        call_args = mock_post.call_args
        payload = call_args[1]["json"]
        assert payload["channel_context"]["channel"] == "discord"
        assert payload["channel_context"]["is_group"] is True


class TestRegressionWithTelegram:
    """
    Regression tests to ensure Discord doesn't break Telegram.
    """

    def test_both_adapters_import(self):
        """Test that both adapters can be imported."""
        from integrations.channels.telegram_adapter import TelegramAdapter
        from integrations.channels.discord_adapter import DiscordAdapter

        assert TelegramAdapter is not None
        assert DiscordAdapter is not None

    def test_registry_handles_multiple_adapters(self):
        """Test that registry can handle both adapters."""
        from integrations.channels.registry import ChannelRegistry

        registry = ChannelRegistry()

        # Mock adapters
        telegram_mock = Mock()
        telegram_mock.name = "telegram"
        telegram_mock.on_message = Mock()
        telegram_mock.get_status.return_value = ChannelStatus.CONNECTED

        discord_mock = Mock()
        discord_mock.name = "discord"
        discord_mock.on_message = Mock()
        discord_mock.get_status.return_value = ChannelStatus.CONNECTED

        registry.register(telegram_mock)
        registry.register(discord_mock)

        assert "telegram" in registry.list_channels()
        assert "discord" in registry.list_channels()
        assert len(registry.list_channels()) == 2

    def test_channel_isolation(self):
        """Test that channels don't interfere with each other."""
        from integrations.channels.flask_integration import FlaskChannelIntegration

        integration = FlaskChannelIntegration()

        # Set different sessions for different channels
        integration.set_user_session("telegram", "user1", 100, 200)
        integration.set_user_session("discord", "user1", 300, 400)

        assert integration._user_sessions[("telegram", "user1")] == (100, 200)
        assert integration._user_sessions[("discord", "user1")] == (300, 400)

    def test_base_imports_unchanged(self):
        """Test that base channel imports still work."""
        from integrations.channels import (
            ChannelAdapter,
            ChannelStatus,
            Message,
            MessageType,
            ChannelRegistry,
        )

        assert ChannelAdapter is not None
        assert ChannelStatus is not None
        assert Message is not None
        assert MessageType is not None
        assert ChannelRegistry is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
