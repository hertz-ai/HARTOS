"""
Tests for Google Chat Channel Adapter

Tests the Google Chat adapter functionality including:
- Webhook handling
- Card message building
- Slash commands
- Thread support
- Space operations
- Error handling
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime
import json

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


class TestGoogleChatAdapter:
    """Tests for GoogleChatAdapter."""

    @pytest.fixture
    def mock_aiohttp(self):
        """Create mock aiohttp module."""
        mock_session = MagicMock()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={})
        mock_response.text = AsyncMock(return_value="")

        mock_session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_response)))
        mock_session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_response)))
        mock_session.close = AsyncMock()

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            with patch('aiohttp.ClientSession', return_value=mock_session):
                yield mock_session

    @pytest.fixture
    def webhook_config(self):
        """Create webhook-based config."""
        return ChannelConfig(
            webhook_url="https://chat.googleapis.com/v1/spaces/XXX/messages?key=YYY&token=ZZZ"
        )

    def test_adapter_creation_webhook(self, mock_aiohttp, webhook_config):
        """Test GoogleChatAdapter instantiation with webhook."""
        from integrations.channels.google_chat_adapter import GoogleChatAdapter

        adapter = GoogleChatAdapter(webhook_config)

        assert adapter.name == "google_chat"
        assert adapter.status == ChannelStatus.DISCONNECTED
        assert adapter._webhook_url is not None
        assert adapter._use_api is False

    def test_message_handler_registration(self, mock_aiohttp, webhook_config):
        """Test message handler registration."""
        from integrations.channels.google_chat_adapter import GoogleChatAdapter

        adapter = GoogleChatAdapter(webhook_config)

        handler_called = False

        async def test_handler(msg):
            nonlocal handler_called
            handler_called = True

        adapter.on_message(test_handler)
        assert len(adapter._message_handlers) == 1


class TestGoogleChatWebhook:
    """Tests for Google Chat webhook handling."""

    @pytest.fixture
    def adapter(self):
        """Create adapter for testing."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            from integrations.channels.google_chat_adapter import GoogleChatAdapter

            config = ChannelConfig(
                webhook_url="https://chat.googleapis.com/v1/spaces/XXX/messages"
            )
            adapter = GoogleChatAdapter(config)
            adapter._session = MagicMock()
            return adapter

    @pytest.mark.asyncio
    async def test_handle_added_to_space(self, adapter):
        """Test handling ADDED_TO_SPACE event."""
        data = {
            "type": "ADDED_TO_SPACE",
            "space": {
                "name": "spaces/ABC123",
                "displayName": "Test Space",
            },
            "user": {
                "name": "users/123",
                "displayName": "John Doe",
            },
        }

        response = await adapter.handle_webhook(data)

        assert response is not None
        assert "text" in response
        assert "Thanks for adding me" in response["text"]

    @pytest.mark.asyncio
    async def test_handle_removed_from_space(self, adapter):
        """Test handling REMOVED_FROM_SPACE event."""
        data = {
            "type": "REMOVED_FROM_SPACE",
            "space": {
                "name": "spaces/ABC123",
                "displayName": "Test Space",
            },
        }

        response = await adapter.handle_webhook(data)

        assert response is None

    @pytest.mark.asyncio
    async def test_handle_message_event(self, adapter):
        """Test handling MESSAGE event."""
        messages_received = []

        async def handler(msg):
            messages_received.append(msg)

        adapter.on_message(handler)

        data = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/ABC/messages/123",
                "text": "Hello bot!",
                "sender": {
                    "name": "users/456",
                    "displayName": "John Doe",
                },
            },
            "space": {
                "name": "spaces/ABC",
                "type": "ROOM",
            },
        }

        response = await adapter.handle_webhook(data)

        assert len(messages_received) == 1
        assert messages_received[0].text == "Hello bot!"
        assert messages_received[0].sender_name == "John Doe"

    @pytest.mark.asyncio
    async def test_handle_card_clicked(self, adapter):
        """Test handling CARD_CLICKED event."""
        messages_received = []

        async def handler(msg):
            messages_received.append(msg)

        adapter.on_message(handler)

        data = {
            "type": "CARD_CLICKED",
            "action": {
                "actionMethodName": "button_action",
                "parameters": [
                    {"key": "param1", "value": "value1"},
                ],
            },
            "user": {
                "name": "users/456",
                "displayName": "John Doe",
            },
            "space": {
                "name": "spaces/ABC",
            },
            "eventTime": "2023-11-01T12:00:00Z",
        }

        response = await adapter.handle_webhook(data)

        assert len(messages_received) == 1
        assert "[button:button_action]" in messages_received[0].text
        assert messages_received[0].raw["action"] == "button_action"


class TestGoogleChatMessageConversion:
    """Tests for Google Chat message conversion."""

    @pytest.fixture
    def adapter(self):
        """Create adapter for testing."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            from integrations.channels.google_chat_adapter import GoogleChatAdapter

            config = ChannelConfig(
                webhook_url="https://chat.googleapis.com/v1/spaces/XXX/messages"
            )
            return GoogleChatAdapter(config)

    def test_direct_message_conversion(self, adapter):
        """Test DM conversion."""
        data = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/ABC/messages/123",
                "text": "Hello!",
                "sender": {
                    "name": "users/456",
                    "displayName": "Jane Doe",
                },
            },
            "space": {
                "name": "spaces/ABC",
                "type": "DM",
            },
        }

        message = adapter._convert_message(data)

        assert message is not None
        assert message.channel == "google_chat"
        assert message.text == "Hello!"
        assert message.is_group is False

    def test_room_message_conversion(self, adapter):
        """Test room/space message conversion."""
        data = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/ABC/messages/123",
                "text": "Hello room!",
                "sender": {
                    "name": "users/456",
                    "displayName": "Jane Doe",
                },
            },
            "space": {
                "name": "spaces/ABC",
                "type": "ROOM",
            },
        }

        message = adapter._convert_message(data)

        assert message is not None
        assert message.is_group is True

    def test_message_with_thread(self, adapter):
        """Test message with thread."""
        data = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/ABC/messages/123",
                "text": "Thread reply",
                "sender": {
                    "name": "users/456",
                    "displayName": "Jane Doe",
                },
                "thread": {
                    "name": "spaces/ABC/threads/789",
                },
            },
            "space": {
                "name": "spaces/ABC",
                "type": "ROOM",
            },
        }

        message = adapter._convert_message(data)

        assert message is not None
        assert message.reply_to_id == "spaces/ABC/threads/789"

    def test_message_with_mention(self, adapter):
        """Test message with bot mention."""
        data = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/ABC/messages/123",
                "text": "@bot help",
                "sender": {
                    "name": "users/456",
                    "displayName": "Jane Doe",
                },
                "annotations": [
                    {
                        "type": "USER_MENTION",
                        "userMention": {
                            "type": "BOT",
                        },
                    },
                ],
            },
            "space": {
                "name": "spaces/ABC",
                "type": "ROOM",
            },
        }

        message = adapter._convert_message(data)

        assert message is not None
        assert message.is_bot_mentioned is True


class TestGoogleChatCardBuilding:
    """Tests for Google Chat card message building."""

    @pytest.fixture
    def adapter(self):
        """Create adapter for testing."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            from integrations.channels.google_chat_adapter import GoogleChatAdapter

            config = ChannelConfig(
                webhook_url="https://chat.googleapis.com/v1/spaces/XXX/messages"
            )
            return GoogleChatAdapter(config)

    def test_build_card_with_text(self, adapter):
        """Test building card with text."""
        buttons = []

        card = adapter._build_card("Hello from card!", buttons)

        assert "sections" in card
        assert len(card["sections"]) > 0
        assert "widgets" in card["sections"][0]

    def test_build_card_with_url_button(self, adapter):
        """Test building card with URL button."""
        buttons = [
            {"text": "Visit Site", "url": "https://example.com"},
        ]

        card = adapter._build_card("Click the button", buttons)

        assert "sections" in card
        widgets = card["sections"][0]["widgets"]

        # Should have text and buttons
        button_widget = None
        for widget in widgets:
            if "buttons" in widget:
                button_widget = widget
                break

        assert button_widget is not None
        assert len(button_widget["buttons"]) == 1
        assert button_widget["buttons"][0]["textButton"]["onClick"]["openLink"]["url"] == "https://example.com"

    def test_build_card_with_callback_button(self, adapter):
        """Test building card with callback button."""
        buttons = [
            {"text": "Click Me", "callback_data": "action_1"},
        ]

        card = adapter._build_card("Click the button", buttons)

        widgets = card["sections"][0]["widgets"]
        button_widget = None
        for widget in widgets:
            if "buttons" in widget:
                button_widget = widget
                break

        assert button_widget is not None
        assert button_widget["buttons"][0]["textButton"]["onClick"]["action"]["actionMethodName"] == "action_1"

    def test_build_card_v2(self, adapter):
        """Test building Cards v2 format."""
        sections = [
            {
                "header": "Section 1",
                "widgets": [
                    {"text": "Some text"},
                    {"buttons": [{"text": "Click", "url": "https://example.com"}]},
                ],
            },
        ]

        card = adapter.build_card_v2("Test Card", sections)

        assert "cardsV2" in card
        assert len(card["cardsV2"]) == 1
        assert card["cardsV2"][0]["card"]["header"]["title"] == "Test Card"


class TestGoogleChatSending:
    """Tests for Google Chat message sending."""

    @pytest.fixture
    def mock_session(self):
        """Create mock aiohttp session."""
        session = MagicMock()
        response = AsyncMock()
        response.status = 200
        response.json = AsyncMock(return_value={"name": "spaces/ABC/messages/456"})

        session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response)))
        session.close = AsyncMock()

        return session

    @pytest.mark.asyncio
    async def test_send_via_webhook(self, mock_session):
        """Test sending message via webhook."""
        from integrations.channels.google_chat_adapter import GoogleChatAdapter

        config = ChannelConfig(
            webhook_url="https://chat.googleapis.com/v1/spaces/XXX/messages?key=YYY"
        )

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = GoogleChatAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_message(
                chat_id="spaces/ABC",
                text="Hello!",
            )

            assert result.success
            assert result.message_id == "spaces/ABC/messages/456"
            mock_session.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_with_buttons(self, mock_session):
        """Test sending message with buttons."""
        from integrations.channels.google_chat_adapter import GoogleChatAdapter

        config = ChannelConfig(
            webhook_url="https://chat.googleapis.com/v1/spaces/XXX/messages?key=YYY"
        )

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = GoogleChatAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_message(
                chat_id="spaces/ABC",
                text="Choose an option",
                buttons=[
                    {"text": "Option 1", "callback_data": "opt1"},
                    {"text": "Option 2", "callback_data": "opt2"},
                ],
            )

            assert result.success

    @pytest.mark.asyncio
    async def test_send_with_thread(self, mock_session):
        """Test sending message in thread."""
        from integrations.channels.google_chat_adapter import GoogleChatAdapter

        config = ChannelConfig(
            webhook_url="https://chat.googleapis.com/v1/spaces/XXX/messages?key=YYY"
        )

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = GoogleChatAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_message(
                chat_id="spaces/ABC",
                text="Thread reply",
                reply_to="spaces/ABC/threads/789",
            )

            assert result.success


class TestGoogleChatSlashCommands:
    """Tests for Google Chat slash commands."""

    @pytest.fixture
    def adapter(self):
        """Create adapter for testing."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            from integrations.channels.google_chat_adapter import GoogleChatAdapter

            config = ChannelConfig(
                webhook_url="https://chat.googleapis.com/v1/spaces/XXX/messages"
            )
            adapter = GoogleChatAdapter(config)
            adapter._session = MagicMock()
            return adapter

    @pytest.mark.asyncio
    async def test_register_slash_command(self, adapter):
        """Test registering slash command."""
        async def help_handler(msg):
            return {"text": "Help text"}

        adapter.register_slash_command("help", help_handler, "Show help")

        assert "help" in adapter._slash_commands
        assert adapter._slash_commands["help"]["description"] == "Show help"

    @pytest.mark.asyncio
    async def test_slash_command_execution(self, adapter):
        """Test slash command execution."""
        async def greet_handler(msg):
            return {"text": f"Hello, {msg.sender_name}!"}

        adapter.register_slash_command("greet", greet_handler)

        data = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/ABC/messages/123",
                "text": "/greet",
                "slashCommand": {
                    "commandId": "greet",
                },
                "sender": {
                    "name": "users/456",
                    "displayName": "John",
                },
            },
            "space": {
                "name": "spaces/ABC",
                "type": "ROOM",
            },
        }

        response = await adapter.handle_webhook(data)

        assert response is not None
        assert "Hello, John!" in response["text"]


class TestGoogleChatFactory:
    """Tests for Google Chat adapter factory."""

    def test_factory_with_webhook(self):
        """Test factory with webhook URL."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            from integrations.channels.google_chat_adapter import create_google_chat_adapter

            adapter = create_google_chat_adapter(
                webhook_url="https://chat.googleapis.com/v1/spaces/XXX/messages?key=YYY"
            )

            assert adapter.name == "google_chat"
            assert adapter._webhook_url is not None
            assert adapter._use_api is False

    def test_factory_with_env_vars(self):
        """Test factory with environment variables."""
        with patch.dict(os.environ, {
            "GOOGLE_CHAT_WEBHOOK": "https://chat.googleapis.com/v1/spaces/ENV/messages?key=ENV",
        }):
            with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
                from integrations.channels.google_chat_adapter import create_google_chat_adapter

                adapter = create_google_chat_adapter()

                assert adapter._webhook_url == "https://chat.googleapis.com/v1/spaces/ENV/messages?key=ENV"

    def test_factory_missing_config(self):
        """Test factory without required config."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GOOGLE_CHAT_WEBHOOK", None)
            os.environ.pop("GOOGLE_CHAT_SA_FILE", None)

            with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
                from integrations.channels.google_chat_adapter import create_google_chat_adapter

                with pytest.raises(ValueError, match="Either webhook_url or service_account_file required"):
                    create_google_chat_adapter()


class TestGoogleChatAPIMode:
    """Tests for Google Chat API mode (with service account)."""

    @pytest.fixture
    def mock_google_api(self):
        """Mock Google API client."""
        mock_service = MagicMock()
        mock_spaces = MagicMock()
        mock_messages = MagicMock()

        mock_service.spaces.return_value = mock_spaces
        mock_spaces.messages.return_value = mock_messages

        with patch.dict('sys.modules', {
            'google.oauth2': MagicMock(),
            'google.oauth2.service_account': MagicMock(),
            'googleapiclient.discovery': MagicMock(build=MagicMock(return_value=mock_service)),
        }):
            yield mock_service

    def test_api_mode_detection(self):
        """Test API mode is detected when service account provided."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            from integrations.channels.google_chat_adapter import GoogleChatAdapter

            config = ChannelConfig(
                extra={"service_account_file": "/path/to/creds.json"}
            )
            adapter = GoogleChatAdapter(config)

            assert adapter._use_api is True


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
