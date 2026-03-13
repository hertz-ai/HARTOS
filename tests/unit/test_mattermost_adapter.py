"""
Tests for Mattermost Channel Adapter

Tests the Mattermost adapter functionality including:
- WebSocket connection
- REST API operations
- Message conversion
- Slash commands
- Interactive messages
- File attachments
- Thread support
- Error handling and reconnection
"""

import pytest
import asyncio
import json
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.channels.base import (
    ChannelConfig,
    ChannelStatus,
    Message,
    MessageType,
    MediaAttachment,
    SendResult,
)


class TestMattermostAdapter:
    """Tests for MattermostAdapter."""

    @pytest.fixture
    def mock_websockets(self):
        """Create mock websockets module."""
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=json.dumps({
            "status": "OK",
            "seq_reply": 1,
        }))
        mock_ws.send = AsyncMock()
        mock_ws.close = AsyncMock()
        mock_ws.ping = AsyncMock()

        with patch.dict('sys.modules', {
            'websockets': MagicMock(
                connect=AsyncMock(return_value=mock_ws),
                exceptions=MagicMock(ConnectionClosed=Exception),
            ),
            'websockets.exceptions': MagicMock(ConnectionClosed=Exception),
        }):
            yield mock_ws

    @pytest.fixture
    def mock_aiohttp(self):
        """Create mock aiohttp session."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "id": "user123",
            "username": "testbot",
            "email": "bot@example.com",
        })
        mock_response.text = AsyncMock(return_value="")
        mock_response.read = AsyncMock(return_value=b"file content")

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=None),
        ))
        mock_session.post = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=None),
        ))
        mock_session.put = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=None),
        ))
        mock_session.delete = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=None),
        ))
        mock_session.close = AsyncMock()

        with patch('aiohttp.ClientSession', return_value=mock_session):
            yield mock_session

    def test_adapter_creation(self, mock_websockets, mock_aiohttp):
        """Test MattermostAdapter instantiation."""
        from integrations.channels.extensions.mattermost_adapter import (
            MattermostAdapter,
            MattermostConfig,
        )

        config = MattermostConfig(
            server_url="https://mattermost.example.com",
            personal_access_token="test-token",
            bot_username="testbot",
        )
        adapter = MattermostAdapter(config)

        assert adapter.name == "mattermost"
        assert adapter.status == ChannelStatus.DISCONNECTED
        assert adapter.mm_config.server_url == "https://mattermost.example.com"

    def test_api_url_construction(self, mock_websockets, mock_aiohttp):
        """Test API URL construction."""
        from integrations.channels.extensions.mattermost_adapter import (
            MattermostAdapter,
            MattermostConfig,
        )

        config = MattermostConfig(
            server_url="https://mattermost.example.com",
            personal_access_token="test-token",
        )
        adapter = MattermostAdapter(config)

        assert adapter._api_url == "https://mattermost.example.com/api/v4/"
        assert adapter._ws_url == "wss://mattermost.example.com/api/v4/websocket"

    def test_message_handler_registration(self, mock_websockets, mock_aiohttp):
        """Test message handler registration."""
        from integrations.channels.extensions.mattermost_adapter import (
            MattermostAdapter,
            MattermostConfig,
        )

        config = MattermostConfig(
            server_url="https://mattermost.example.com",
            personal_access_token="test-token",
        )
        adapter = MattermostAdapter(config)

        handler_called = False

        async def test_handler(msg):
            nonlocal handler_called
            handler_called = True

        adapter.on_message(test_handler)
        assert len(adapter._message_handlers) == 1


class TestMattermostMessageConversion:
    """Tests for Mattermost message conversion."""

    def test_message_structure(self):
        """Test that Message can hold Mattermost-specific data."""
        msg = Message(
            id="post123",
            channel="mattermost",
            sender_id="user456",
            sender_name="testuser",
            chat_id="channel789",
            text="Hello from Mattermost!",
            is_group=True,
            is_bot_mentioned=True,
            raw={
                "team_id": "team123",
                "channel_name": "town-square",
                "channel_display_name": "Town Square",
                "props": {"key": "value"},
            },
        )

        assert msg.channel == "mattermost"
        assert msg.is_group
        assert msg.is_bot_mentioned
        assert msg.raw["channel_name"] == "town-square"

    def test_media_attachment(self):
        """Test media attachment handling."""
        media = MediaAttachment(
            type=MessageType.IMAGE,
            file_id="file123",
            file_name="image.png",
            file_size=1024,
            mime_type="image/png",
        )

        assert media.type == MessageType.IMAGE
        assert media.file_id == "file123"

    def test_thread_message(self):
        """Test thread reply message."""
        msg = Message(
            id="post456",
            channel="mattermost",
            sender_id="user789",
            chat_id="channel123",
            text="Thread reply",
            reply_to_id="post123",  # Root post ID
            raw={
                "root_id": "post123",
            },
        )

        assert msg.reply_to_id == "post123"


class TestInteractiveMessage:
    """Tests for Mattermost interactive messages."""

    def test_interactive_message_creation(self):
        """Test InteractiveMessage builder."""
        from integrations.channels.extensions.mattermost_adapter import InteractiveMessage

        interactive = InteractiveMessage(text="Choose an option:")
        interactive.add_attachment(
            fallback="Options",
            color="#0076B4",
            title="Options Menu",
        )

        result = interactive.to_dict()
        assert result["message"] == "Choose an option:"
        assert "attachments" in result["props"]

    def test_interactive_message_with_buttons(self):
        """Test InteractiveMessage with action buttons."""
        from integrations.channels.extensions.mattermost_adapter import InteractiveMessage

        interactive = InteractiveMessage(text="Click a button:")
        interactive.add_attachment(fallback="Buttons")
        interactive.add_button(
            name="Approve",
            integration_url="https://example.com/webhook",
            context={"action": "approve"},
            style="success",
        )
        interactive.add_button(
            name="Reject",
            integration_url="https://example.com/webhook",
            context={"action": "reject"},
            style="danger",
        )

        result = interactive.to_dict()
        actions = result["props"]["attachments"][0]["actions"]
        assert len(actions) == 2
        assert actions[0]["name"] == "Approve"
        assert actions[0]["style"] == "success"

    def test_interactive_message_with_select_menu(self):
        """Test InteractiveMessage with select menu."""
        from integrations.channels.extensions.mattermost_adapter import InteractiveMessage

        interactive = InteractiveMessage(text="Select an option:")
        interactive.add_attachment(fallback="Select Menu")
        interactive.add_select_menu(
            name="Priority",
            integration_url="https://example.com/webhook",
            options=[
                {"text": "High", "value": "high"},
                {"text": "Medium", "value": "medium"},
                {"text": "Low", "value": "low"},
            ],
        )

        result = interactive.to_dict()
        actions = result["props"]["attachments"][0]["actions"]
        assert len(actions) == 1
        assert actions[0]["type"] == "select"
        assert len(actions[0]["options"]) == 3


class TestSlashCommands:
    """Tests for Mattermost slash commands."""

    @pytest.fixture
    def mock_deps(self):
        """Create mock dependencies."""
        mock_ws = AsyncMock()
        with patch.dict('sys.modules', {
            'websockets': MagicMock(
                connect=AsyncMock(return_value=mock_ws),
                exceptions=MagicMock(ConnectionClosed=Exception),
            ),
            'websockets.exceptions': MagicMock(ConnectionClosed=Exception),
        }):
            yield

    def test_slash_command_registration(self, mock_deps):
        """Test slash command handler registration."""
        from integrations.channels.extensions.mattermost_adapter import (
            MattermostAdapter,
            MattermostConfig,
        )

        config = MattermostConfig(
            server_url="https://mattermost.example.com",
            personal_access_token="test-token",
            enable_slash_commands=True,
        )
        adapter = MattermostAdapter(config)

        async def help_handler(**kwargs):
            return {"text": "Help message"}

        adapter.register_slash_command(
            trigger="help",
            description="Show help message",
            handler=help_handler,
        )

        assert "help" in adapter._slash_commands
        assert adapter._slash_commands["help"].description == "Show help message"

    @pytest.mark.asyncio
    async def test_slash_command_execution(self, mock_deps):
        """Test slash command execution."""
        from integrations.channels.extensions.mattermost_adapter import (
            MattermostAdapter,
            MattermostConfig,
        )

        config = MattermostConfig(
            server_url="https://mattermost.example.com",
            personal_access_token="test-token",
        )
        adapter = MattermostAdapter(config)

        handler_result = {"text": "Command executed!"}

        async def test_handler(**kwargs):
            return handler_result

        adapter.register_slash_command(
            trigger="test",
            description="Test command",
            handler=test_handler,
        )

        result = await adapter.handle_slash_command(
            command="test",
            text="arg1 arg2",
            user_id="user123",
            channel_id="channel456",
            trigger_id="trigger789",
        )

        assert result == handler_result


class TestMattermostFactoryFunction:
    """Tests for Mattermost adapter factory function."""

    @pytest.fixture
    def mock_deps(self):
        """Create mock dependencies."""
        with patch.dict('sys.modules', {
            'websockets': MagicMock(
                exceptions=MagicMock(ConnectionClosed=Exception),
            ),
            'websockets.exceptions': MagicMock(ConnectionClosed=Exception),
        }):
            yield

    def test_factory_with_params(self, mock_deps):
        """Test factory function with parameters."""
        from integrations.channels.extensions.mattermost_adapter import (
            create_mattermost_adapter,
        )

        adapter = create_mattermost_adapter(
            server_url="https://mm.example.com",
            personal_access_token="token123",
            bot_username="mybot",
        )

        assert adapter.mm_config.server_url == "https://mm.example.com"
        assert adapter.mm_config.personal_access_token == "token123"
        assert adapter.mm_config.bot_username == "mybot"

    def test_factory_with_env_vars(self, mock_deps):
        """Test factory function with environment variables."""
        from integrations.channels.extensions.mattermost_adapter import (
            create_mattermost_adapter,
        )

        with patch.dict(os.environ, {
            "MATTERMOST_SERVER_URL": "https://env-mm.example.com",
            "MATTERMOST_TOKEN": "env-token",
            "MATTERMOST_BOT_USERNAME": "envbot",
        }):
            adapter = create_mattermost_adapter()

            assert adapter.mm_config.server_url == "https://env-mm.example.com"
            assert adapter.mm_config.personal_access_token == "env-token"
            assert adapter.mm_config.bot_username == "envbot"

    def test_factory_missing_params_raises(self, mock_deps):
        """Test factory function raises on missing required params."""
        from integrations.channels.extensions.mattermost_adapter import (
            create_mattermost_adapter,
        )

        with pytest.raises(ValueError, match="server URL required"):
            create_mattermost_adapter(personal_access_token="token")

        with pytest.raises(ValueError, match="access token required"):
            create_mattermost_adapter(server_url="https://example.com")


class TestMattermostConfiguration:
    """Tests for Mattermost configuration."""

    @pytest.fixture
    def mock_deps(self):
        """Create mock dependencies."""
        with patch.dict('sys.modules', {
            'websockets': MagicMock(
                exceptions=MagicMock(ConnectionClosed=Exception),
            ),
            'websockets.exceptions': MagicMock(ConnectionClosed=Exception),
        }):
            yield

    def test_default_config(self, mock_deps):
        """Test default configuration values."""
        from integrations.channels.extensions.mattermost_adapter import MattermostConfig

        config = MattermostConfig()

        assert config.enable_slash_commands is True
        assert config.enable_interactive_messages is True
        assert config.enable_file_attachments is True
        assert config.enable_threads is True
        assert config.reconnect_delay == 5.0
        assert config.max_reconnect_attempts == 10

    def test_custom_config(self, mock_deps):
        """Test custom configuration values."""
        from integrations.channels.extensions.mattermost_adapter import MattermostConfig

        config = MattermostConfig(
            server_url="https://custom.mm.com",
            personal_access_token="custom-token",
            enable_slash_commands=False,
            reconnect_delay=10.0,
            max_reconnect_attempts=5,
        )

        assert config.server_url == "https://custom.mm.com"
        assert config.enable_slash_commands is False
        assert config.reconnect_delay == 10.0
        assert config.max_reconnect_attempts == 5


class TestMattermostReactions:
    """Tests for Mattermost reaction support."""

    def test_reaction_data_structure(self):
        """Test reaction data in message."""
        msg = Message(
            id="post123",
            channel="mattermost",
            sender_id="user456",
            chat_id="channel789",
            text="React to this!",
            raw={
                "reactions": {
                    "+1": ["user1", "user2"],
                    "smile": ["user3"],
                },
            },
        )

        reactions = msg.raw.get("reactions", {})
        assert "+1" in reactions
        assert len(reactions["+1"]) == 2


class TestMattermostThreads:
    """Tests for Mattermost thread support."""

    def test_thread_root_detection(self):
        """Test detecting thread root message."""
        # Root message has no root_id
        root_msg = Message(
            id="post123",
            channel="mattermost",
            sender_id="user456",
            chat_id="channel789",
            text="Start of thread",
            reply_to_id=None,
        )

        # Reply message has root_id
        reply_msg = Message(
            id="post456",
            channel="mattermost",
            sender_id="user789",
            chat_id="channel789",
            text="Thread reply",
            reply_to_id="post123",
        )

        assert root_msg.reply_to_id is None
        assert reply_msg.reply_to_id == "post123"


class TestMattermostDockerCompatibility:
    """Tests for Docker compatibility."""

    @pytest.fixture
    def mock_deps(self):
        """Create mock dependencies."""
        with patch.dict('sys.modules', {
            'websockets': MagicMock(
                exceptions=MagicMock(ConnectionClosed=Exception),
            ),
            'websockets.exceptions': MagicMock(ConnectionClosed=Exception),
        }):
            yield

    def test_env_var_configuration(self, mock_deps):
        """Test configuration via environment variables for Docker."""
        from integrations.channels.extensions.mattermost_adapter import (
            create_mattermost_adapter,
        )

        env_vars = {
            "MATTERMOST_SERVER_URL": "https://docker-mm.example.com",
            "MATTERMOST_TOKEN": "docker-token",
            "MATTERMOST_BOT_USERNAME": "dockerbot",
        }

        with patch.dict(os.environ, env_vars):
            adapter = create_mattermost_adapter()

            assert adapter.mm_config.server_url == "https://docker-mm.example.com"
            assert adapter.mm_config.personal_access_token == "docker-token"
            assert adapter.mm_config.bot_username == "dockerbot"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
