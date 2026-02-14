"""
Tests for Nextcloud Talk Channel Adapter

Tests the Nextcloud Talk adapter functionality including:
- REST API integration
- Message conversion
- File sharing integration
- Reactions support
- Room/conversation management
- Participants management
- Polls
- Error handling and reconnection
"""

import pytest
import asyncio
import json
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


class TestNextcloudAdapter:
    """Tests for NextcloudAdapter."""

    @pytest.fixture
    def mock_aiohttp(self):
        """Create mock aiohttp session."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "ocs": {
                "meta": {"status": "ok"},
                "data": {
                    "id": "testuser",
                    "displayname": "Test User",
                },
            }
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
            with patch('aiohttp.TCPConnector'):
                yield mock_session

    def test_adapter_creation(self, mock_aiohttp):
        """Test NextcloudAdapter instantiation."""
        from integrations.channels.extensions.nextcloud_adapter import (
            NextcloudAdapter,
            NextcloudConfig,
        )

        config = NextcloudConfig(
            server_url="https://nextcloud.example.com",
            username="bot",
            app_password="xxxxx-xxxxx-xxxxx",
        )
        adapter = NextcloudAdapter(config)

        assert adapter.name == "nextcloud"
        assert adapter.status == ChannelStatus.DISCONNECTED
        assert adapter.nc_config.server_url == "https://nextcloud.example.com"

    def test_api_url_construction(self, mock_aiohttp):
        """Test API URL construction."""
        from integrations.channels.extensions.nextcloud_adapter import (
            NextcloudAdapter,
            NextcloudConfig,
        )

        config = NextcloudConfig(
            server_url="https://nextcloud.example.com",
            username="bot",
            app_password="xxxxx",
        )
        adapter = NextcloudAdapter(config)

        assert "ocs/v2.php/apps/spreed/api/v4" in adapter._api_url
        assert "remote.php/dav/files/bot" in adapter._dav_url

    def test_message_handler_registration(self, mock_aiohttp):
        """Test message handler registration."""
        from integrations.channels.extensions.nextcloud_adapter import (
            NextcloudAdapter,
            NextcloudConfig,
        )

        config = NextcloudConfig(
            server_url="https://nextcloud.example.com",
            username="bot",
            app_password="xxxxx",
        )
        adapter = NextcloudAdapter(config)

        handler_called = False

        async def test_handler(msg):
            nonlocal handler_called
            handler_called = True

        adapter.on_message(test_handler)
        assert len(adapter._message_handlers) == 1


class TestNextcloudMessageConversion:
    """Tests for Nextcloud Talk message conversion."""

    def test_message_structure(self):
        """Test that Message can hold Nextcloud Talk-specific data."""
        msg = Message(
            id="123",
            channel="nextcloud",
            sender_id="user456",
            sender_name="Test User",
            chat_id="abc123token",
            text="Hello from Nextcloud!",
            is_group=True,
            is_bot_mentioned=False,
            raw={
                "message_type": "comment",
                "actor_type": "users",
                "conversation_name": "General Chat",
                "reactions": {":+1:": 3, ":smile:": 1},
            },
        )

        assert msg.channel == "nextcloud"
        assert msg.is_group
        assert msg.raw["message_type"] == "comment"
        assert msg.raw["reactions"][":+1:"] == 3

    def test_media_attachment_from_file(self):
        """Test media attachment from file sharing."""
        media = MediaAttachment(
            type=MessageType.IMAGE,
            file_id="12345",
            file_name="photo.jpg",
            file_size=2048,
            mime_type="image/jpeg",
            url="https://nextcloud.example.com/remote.php/dav/files/user/photo.jpg",
        )

        assert media.type == MessageType.IMAGE
        assert media.file_id == "12345"
        assert media.url is not None

    def test_reply_message(self):
        """Test reply message structure."""
        msg = Message(
            id="456",
            channel="nextcloud",
            sender_id="user789",
            chat_id="roomtoken",
            text="This is a reply",
            reply_to_id="123",  # Parent message ID
        )

        assert msg.reply_to_id == "123"


class TestConversationTypes:
    """Tests for Nextcloud Talk conversation types."""

    def test_conversation_types(self):
        """Test ConversationType enum values."""
        from integrations.channels.extensions.nextcloud_adapter import ConversationType

        assert ConversationType.ONE_TO_ONE.value == 1
        assert ConversationType.GROUP.value == 2
        assert ConversationType.PUBLIC.value == 3
        assert ConversationType.CHANGELOG.value == 4

    def test_participant_types(self):
        """Test ParticipantType enum values."""
        from integrations.channels.extensions.nextcloud_adapter import ParticipantType

        assert ParticipantType.OWNER.value == 1
        assert ParticipantType.MODERATOR.value == 2
        assert ParticipantType.USER.value == 3
        assert ParticipantType.GUEST.value == 4


class TestNextcloudConversation:
    """Tests for Nextcloud conversation data structures."""

    def test_conversation_creation(self):
        """Test NextcloudConversation dataclass."""
        from integrations.channels.extensions.nextcloud_adapter import (
            NextcloudConversation,
            ConversationType,
            ParticipantType,
        )

        conv = NextcloudConversation(
            token="abc123",
            name="general",
            display_name="General Chat",
            type=ConversationType.GROUP,
            participant_type=ParticipantType.USER,
            read_only=False,
            has_call=True,
            unread_messages=5,
            description="Main chat room",
        )

        assert conv.token == "abc123"
        assert conv.type == ConversationType.GROUP
        assert conv.participant_type == ParticipantType.USER
        assert conv.has_call is True
        assert conv.unread_messages == 5

    def test_participant_creation(self):
        """Test NextcloudParticipant dataclass."""
        from integrations.channels.extensions.nextcloud_adapter import (
            NextcloudParticipant,
            ParticipantType,
        )

        participant = NextcloudParticipant(
            attendee_id=123,
            actor_type="users",
            actor_id="user456",
            display_name="John Doe",
            participant_type=ParticipantType.MODERATOR,
            in_call=True,
        )

        assert participant.attendee_id == 123
        assert participant.actor_type == "users"
        assert participant.participant_type == ParticipantType.MODERATOR
        assert participant.in_call is True


class TestNextcloudReactions:
    """Tests for Nextcloud Talk reaction support."""

    def test_reaction_data_in_message(self):
        """Test reaction data structure in message."""
        msg = Message(
            id="123",
            channel="nextcloud",
            sender_id="user456",
            chat_id="roomtoken",
            text="React to this!",
            raw={
                "reactions": {
                    ":+1:": 5,
                    ":heart:": 3,
                    ":tada:": 2,
                },
            },
        )

        reactions = msg.raw.get("reactions", {})
        assert ":+1:" in reactions
        assert reactions[":+1:"] == 5
        assert sum(reactions.values()) == 10


class TestNextcloudPolls:
    """Tests for Nextcloud Talk poll support."""

    @pytest.fixture
    def mock_aiohttp(self):
        """Create mock aiohttp session."""
        mock_session = MagicMock()
        mock_session.close = AsyncMock()

        with patch('aiohttp.ClientSession', return_value=mock_session):
            with patch('aiohttp.TCPConnector'):
                yield mock_session

    def test_poll_configuration(self, mock_aiohttp):
        """Test poll configuration option."""
        from integrations.channels.extensions.nextcloud_adapter import (
            NextcloudAdapter,
            NextcloudConfig,
        )

        config = NextcloudConfig(
            server_url="https://nextcloud.example.com",
            username="bot",
            app_password="xxxxx",
            enable_polls=True,
        )
        adapter = NextcloudAdapter(config)

        assert adapter.nc_config.enable_polls is True


class TestNextcloudFileSharing:
    """Tests for Nextcloud file sharing integration."""

    @pytest.fixture
    def mock_aiohttp(self):
        """Create mock aiohttp session."""
        mock_session = MagicMock()
        mock_session.close = AsyncMock()

        with patch('aiohttp.ClientSession', return_value=mock_session):
            with patch('aiohttp.TCPConnector'):
                yield mock_session

    def test_file_sharing_configuration(self, mock_aiohttp):
        """Test file sharing configuration."""
        from integrations.channels.extensions.nextcloud_adapter import (
            NextcloudAdapter,
            NextcloudConfig,
        )

        config = NextcloudConfig(
            server_url="https://nextcloud.example.com",
            username="bot",
            app_password="xxxxx",
            enable_file_sharing=True,
        )
        adapter = NextcloudAdapter(config)

        assert adapter.nc_config.enable_file_sharing is True

    def test_dav_url_construction(self, mock_aiohttp):
        """Test WebDAV URL construction for file operations."""
        from integrations.channels.extensions.nextcloud_adapter import (
            NextcloudAdapter,
            NextcloudConfig,
        )

        config = NextcloudConfig(
            server_url="https://nextcloud.example.com",
            username="botuser",
            app_password="xxxxx",
        )
        adapter = NextcloudAdapter(config)

        expected = "https://nextcloud.example.com/remote.php/dav/files/botuser/"
        assert adapter._dav_url == expected


class TestNextcloudFactoryFunction:
    """Tests for Nextcloud adapter factory function."""

    def test_factory_with_params(self):
        """Test factory function with parameters."""
        from integrations.channels.extensions.nextcloud_adapter import (
            create_nextcloud_adapter,
        )

        adapter = create_nextcloud_adapter(
            server_url="https://nc.example.com",
            username="testbot",
            app_password="test-app-password",
        )

        assert adapter.nc_config.server_url == "https://nc.example.com"
        assert adapter.nc_config.username == "testbot"
        assert adapter.nc_config.app_password == "test-app-password"

    def test_factory_with_env_vars(self):
        """Test factory function with environment variables."""
        from integrations.channels.extensions.nextcloud_adapter import (
            create_nextcloud_adapter,
        )

        with patch.dict(os.environ, {
            "NEXTCLOUD_URL": "https://env-nc.example.com",
            "NEXTCLOUD_USERNAME": "envuser",
            "NEXTCLOUD_APP_PASSWORD": "env-app-password",
        }):
            adapter = create_nextcloud_adapter()

            assert adapter.nc_config.server_url == "https://env-nc.example.com"
            assert adapter.nc_config.username == "envuser"
            assert adapter.nc_config.app_password == "env-app-password"

    def test_factory_missing_params_raises(self):
        """Test factory function raises on missing required params."""
        from integrations.channels.extensions.nextcloud_adapter import (
            create_nextcloud_adapter,
        )

        with pytest.raises(ValueError, match="server URL required"):
            create_nextcloud_adapter(username="bot", app_password="pass")

        with pytest.raises(ValueError, match="username required"):
            create_nextcloud_adapter(server_url="https://example.com", app_password="pass")

        with pytest.raises(ValueError, match="app password required"):
            create_nextcloud_adapter(server_url="https://example.com", username="bot")


class TestNextcloudConfiguration:
    """Tests for Nextcloud configuration."""

    def test_default_config(self):
        """Test default configuration values."""
        from integrations.channels.extensions.nextcloud_adapter import NextcloudConfig

        config = NextcloudConfig()

        assert config.enable_file_sharing is True
        assert config.enable_reactions is True
        assert config.enable_polls is True
        assert config.poll_interval == 2.0
        assert config.reconnect_delay == 5.0
        assert config.max_reconnect_attempts == 10
        assert config.verify_ssl is True

    def test_custom_config(self):
        """Test custom configuration values."""
        from integrations.channels.extensions.nextcloud_adapter import NextcloudConfig

        config = NextcloudConfig(
            server_url="https://custom.nc.com",
            username="custombot",
            app_password="custom-pass",
            enable_reactions=False,
            poll_interval=5.0,
            verify_ssl=False,
        )

        assert config.server_url == "https://custom.nc.com"
        assert config.enable_reactions is False
        assert config.poll_interval == 5.0
        assert config.verify_ssl is False


class TestNextcloudRichObjects:
    """Tests for Nextcloud Talk rich object sharing."""

    def test_rich_object_parameter(self):
        """Test RichObjectParameter dataclass."""
        from integrations.channels.extensions.nextcloud_adapter import RichObjectParameter

        rich_obj = RichObjectParameter(
            type="deck-card",
            id="12345",
            name="Task Card",
            extra={
                "boardname": "Project Board",
                "stackname": "In Progress",
            },
        )

        assert rich_obj.type == "deck-card"
        assert rich_obj.id == "12345"
        assert rich_obj.extra["boardname"] == "Project Board"

    def test_file_rich_object(self):
        """Test file as rich object."""
        from integrations.channels.extensions.nextcloud_adapter import RichObjectParameter

        file_obj = RichObjectParameter(
            type="file",
            id="67890",
            name="document.pdf",
            extra={
                "mimetype": "application/pdf",
                "size": 1024,
                "path": "/Documents/document.pdf",
            },
        )

        assert file_obj.type == "file"
        assert file_obj.extra["mimetype"] == "application/pdf"


class TestNextcloudDockerCompatibility:
    """Tests for Docker compatibility."""

    def test_env_var_configuration(self):
        """Test configuration via environment variables for Docker."""
        from integrations.channels.extensions.nextcloud_adapter import (
            create_nextcloud_adapter,
        )

        env_vars = {
            "NEXTCLOUD_URL": "https://docker-nc.example.com",
            "NEXTCLOUD_USERNAME": "dockerbot",
            "NEXTCLOUD_APP_PASSWORD": "docker-app-password",
        }

        with patch.dict(os.environ, env_vars):
            adapter = create_nextcloud_adapter()

            assert adapter.nc_config.server_url == "https://docker-nc.example.com"
            assert adapter.nc_config.username == "dockerbot"
            assert adapter.nc_config.app_password == "docker-app-password"


class TestNextcloudReconnection:
    """Tests for reconnection logic."""

    def test_reconnection_config(self):
        """Test reconnection configuration."""
        from integrations.channels.extensions.nextcloud_adapter import NextcloudConfig

        config = NextcloudConfig(
            server_url="https://nc.example.com",
            username="bot",
            app_password="pass",
            reconnect_delay=10.0,
            max_reconnect_attempts=5,
        )

        assert config.reconnect_delay == 10.0
        assert config.max_reconnect_attempts == 5


class TestNextcloudRegressionWithOtherAdapters:
    """Regression tests to ensure Nextcloud doesn't break other adapters."""

    def test_both_adapters_import(self):
        """Test that both Mattermost and Nextcloud adapters can be imported."""
        from integrations.channels.extensions.mattermost_adapter import MattermostAdapter
        from integrations.channels.extensions.nextcloud_adapter import NextcloudAdapter

        assert MattermostAdapter is not None
        assert NextcloudAdapter is not None

    def test_extensions_init_imports(self):
        """Test that extensions __init__ exports all adapters."""
        from integrations.channels.extensions import (
            MattermostAdapter,
            MattermostConfig,
            NextcloudAdapter,
            NextcloudConfig,
            create_mattermost_adapter,
            create_nextcloud_adapter,
        )

        assert MattermostAdapter is not None
        assert MattermostConfig is not None
        assert NextcloudAdapter is not None
        assert NextcloudConfig is not None
        assert create_mattermost_adapter is not None
        assert create_nextcloud_adapter is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
