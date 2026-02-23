"""
Tests for iMessage Channel Adapter

Tests the iMessage adapter functionality including:
- BlueBubbles API integration
- Message conversion
- Tapbacks (reactions)
- Group chats
- Attachments
- Error handling
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime
import json

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


class TestIMessageAdapter:
    """Tests for IMessageAdapter."""

    @pytest.fixture
    def mock_aiohttp(self):
        """Create mock aiohttp module."""
        mock_session = MagicMock()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"data": {}})
        mock_response.text = AsyncMock(return_value="")

        mock_session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_response)))
        mock_session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_response)))
        mock_session.close = AsyncMock()

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            with patch('aiohttp.ClientSession', return_value=mock_session):
                yield mock_session

    @pytest.fixture
    def imessage_config(self):
        """Create iMessage adapter config."""
        return ChannelConfig(
            token="test_password",
            extra={"api_url": "http://localhost:1234"}
        )

    def test_adapter_creation(self, mock_aiohttp, imessage_config):
        """Test IMessageAdapter instantiation."""
        from integrations.channels.imessage_adapter import IMessageAdapter

        adapter = IMessageAdapter(imessage_config)

        assert adapter.name == "imessage"
        assert adapter.status == ChannelStatus.DISCONNECTED
        assert adapter._password == "test_password"
        assert adapter._api_url == "http://localhost:1234"

    def test_message_handler_registration(self, mock_aiohttp, imessage_config):
        """Test message handler registration."""
        from integrations.channels.imessage_adapter import IMessageAdapter

        adapter = IMessageAdapter(imessage_config)

        handler_called = False

        async def test_handler(msg):
            nonlocal handler_called
            handler_called = True

        adapter.on_message(test_handler)
        assert len(adapter._message_handlers) == 1


class TestIMessageConversion:
    """Tests for iMessage message conversion."""

    @pytest.fixture
    def adapter(self):
        """Create adapter for testing."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            from integrations.channels.imessage_adapter import IMessageAdapter

            config = ChannelConfig(
                token="test_password",
                extra={"api_url": "http://localhost:1234"}
            )
            return IMessageAdapter(config)

    def test_direct_message_conversion(self, adapter):
        """Test direct message conversion."""
        msg_data = {
            "guid": "msg-guid-123",
            "text": "Hello from iMessage!",
            "is_from_me": False,
            "date_created": 1699000000000,
            "handle": {
                "address": "+1987654321",
                "displayName": "John Doe",
            },
            "chat": {
                "guid": "chat-guid-456",
                "style": 45,  # Direct message
            },
            "attachments": [],
        }

        message = adapter._convert_message(msg_data)

        assert message is not None
        assert message.id == "msg-guid-123"
        assert message.channel == "imessage"
        assert message.sender_id == "+1987654321"
        assert message.sender_name == "John Doe"
        assert message.text == "Hello from iMessage!"
        assert message.is_group is False

    def test_group_message_conversion(self, adapter):
        """Test group message conversion."""
        msg_data = {
            "guid": "msg-guid-789",
            "text": "Hello group!",
            "is_from_me": False,
            "date_created": 1699000000000,
            "handle": {
                "address": "+1987654321",
                "displayName": "John",
            },
            "chat": {
                "guid": "chat-group-guid",
                "style": 43,  # Group chat
            },
            "attachments": [],
        }

        message = adapter._convert_message(msg_data)

        assert message is not None
        assert message.is_group is True
        assert message.chat_id == "chat-group-guid"

    def test_attachment_conversion(self, adapter):
        """Test message with attachments."""
        msg_data = {
            "guid": "msg-guid-att",
            "text": "Check this out",
            "is_from_me": False,
            "date_created": 1699000000000,
            "handle": {
                "address": "+1987654321",
            },
            "chat": {
                "guid": "chat-guid",
                "style": 45,
            },
            "attachments": [
                {
                    "guid": "att-guid-1",
                    "transfer_name": "photo.jpg",
                    "mime_type": "image/jpeg",
                    "total_bytes": 12345,
                }
            ],
        }

        message = adapter._convert_message(msg_data)

        assert message is not None
        assert message.has_media
        assert len(message.media) == 1
        assert message.media[0].type == MessageType.IMAGE
        assert message.media[0].file_name == "photo.jpg"

    def test_skip_own_messages(self, adapter):
        """Test that own messages are skipped."""
        msg_data = {
            "guid": "msg-guid",
            "text": "My own message",
            "is_from_me": True,
            "date_created": 1699000000000,
            "handle": {},
            "chat": {"guid": "chat-guid", "style": 45},
            "attachments": [],
        }

        message = adapter._convert_message(msg_data)
        assert message is None

    def test_media_type_detection(self, adapter):
        """Test media type detection."""
        assert adapter._get_media_type("image/jpeg") == MessageType.IMAGE
        assert adapter._get_media_type("image/png") == MessageType.IMAGE
        assert adapter._get_media_type("video/mp4") == MessageType.VIDEO
        assert adapter._get_media_type("video/quicktime") == MessageType.VIDEO
        assert adapter._get_media_type("audio/mpeg") == MessageType.AUDIO
        assert adapter._get_media_type("application/pdf") == MessageType.DOCUMENT


class TestIMessageSending:
    """Tests for iMessage sending."""

    @pytest.fixture
    def mock_session(self):
        """Create mock aiohttp session."""
        session = MagicMock()
        response = AsyncMock()
        response.status = 200
        response.json = AsyncMock(return_value={"data": {"guid": "sent-msg-guid"}})

        session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response)))
        session.close = AsyncMock()

        return session

    @pytest.mark.asyncio
    async def test_send_text_message(self, mock_session):
        """Test sending text message."""
        from integrations.channels.imessage_adapter import IMessageAdapter

        config = ChannelConfig(token="test_password", extra={"api_url": "http://localhost:1234"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = IMessageAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_message(
                chat_id="chat-guid-123",
                text="Hello!",
            )

            assert result.success
            assert result.message_id == "sent-msg-guid"
            mock_session.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_with_reply(self, mock_session):
        """Test sending message as reply."""
        from integrations.channels.imessage_adapter import IMessageAdapter

        config = ChannelConfig(token="test_password", extra={"api_url": "http://localhost:1234"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = IMessageAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_message(
                chat_id="chat-guid-123",
                text="Reply to this",
                reply_to="original-msg-guid",
            )

            assert result.success

    @pytest.mark.asyncio
    async def test_send_not_connected(self):
        """Test sending when not connected."""
        from integrations.channels.imessage_adapter import IMessageAdapter

        config = ChannelConfig(token="test_password", extra={"api_url": "http://localhost:1234"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = IMessageAdapter(config)
            # Don't set _session

            result = await adapter.send_message(
                chat_id="chat-guid-123",
                text="Hello!",
            )

            assert not result.success
            assert "Not connected" in result.error


class TestIMessageTapbacks:
    """Tests for iMessage tapbacks (reactions)."""

    @pytest.fixture
    def mock_session(self):
        """Create mock aiohttp session."""
        session = MagicMock()
        response = AsyncMock()
        response.status = 200

        session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response)))
        return session

    @pytest.mark.asyncio
    async def test_send_love_tapback(self, mock_session):
        """Test sending love tapback."""
        from integrations.channels.imessage_adapter import IMessageAdapter

        config = ChannelConfig(token="test_password", extra={"api_url": "http://localhost:1234"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = IMessageAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_tapback(
                chat_id="chat-guid",
                message_id="msg-guid",
                tapback="love",
            )

            assert result is True
            mock_session.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_like_tapback(self, mock_session):
        """Test sending like tapback."""
        from integrations.channels.imessage_adapter import IMessageAdapter

        config = ChannelConfig(token="test_password", extra={"api_url": "http://localhost:1234"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = IMessageAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_tapback(
                chat_id="chat-guid",
                message_id="msg-guid",
                tapback="like",
            )

            assert result is True

    @pytest.mark.asyncio
    async def test_send_emoji_mapped_tapback(self, mock_session):
        """Test sending tapback with emoji-style name."""
        from integrations.channels.imessage_adapter import IMessageAdapter

        config = ChannelConfig(token="test_password", extra={"api_url": "http://localhost:1234"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = IMessageAdapter(config)
            adapter._session = mock_session

            # 'heart' should map to 'love'
            result = await adapter.send_tapback(
                chat_id="chat-guid",
                message_id="msg-guid",
                tapback="heart",
            )

            assert result is True

    @pytest.mark.asyncio
    async def test_invalid_tapback_type(self, mock_session):
        """Test sending invalid tapback type."""
        from integrations.channels.imessage_adapter import IMessageAdapter

        config = ChannelConfig(token="test_password", extra={"api_url": "http://localhost:1234"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = IMessageAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_tapback(
                chat_id="chat-guid",
                message_id="msg-guid",
                tapback="invalid_type",
            )

            assert result is False


class TestIMessageTyping:
    """Tests for iMessage typing indicators."""

    @pytest.fixture
    def mock_session(self):
        """Create mock aiohttp session."""
        session = MagicMock()
        response = AsyncMock()
        response.status = 200

        session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response)))
        return session

    @pytest.mark.asyncio
    async def test_send_typing(self, mock_session):
        """Test sending typing indicator."""
        from integrations.channels.imessage_adapter import IMessageAdapter

        config = ChannelConfig(token="test_password", extra={"api_url": "http://localhost:1234"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = IMessageAdapter(config)
            adapter._session = mock_session

            await adapter.send_typing("chat-guid")

            mock_session.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_typing(self, mock_session):
        """Test stopping typing indicator."""
        from integrations.channels.imessage_adapter import IMessageAdapter

        config = ChannelConfig(token="test_password", extra={"api_url": "http://localhost:1234"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = IMessageAdapter(config)
            adapter._session = mock_session

            await adapter.stop_typing("chat-guid")

            mock_session.post.assert_called_once()


class TestIMessageGroups:
    """Tests for iMessage group operations."""

    @pytest.fixture
    def mock_session(self):
        """Create mock aiohttp session."""
        session = MagicMock()
        response = AsyncMock()
        response.status = 200
        response.json = AsyncMock(return_value={"data": {"guid": "new-group-guid"}})

        session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response)))
        session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response)))
        return session

    @pytest.mark.asyncio
    async def test_create_group(self, mock_session):
        """Test creating group chat."""
        from integrations.channels.imessage_adapter import IMessageAdapter

        config = ChannelConfig(token="test_password", extra={"api_url": "http://localhost:1234"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = IMessageAdapter(config)
            adapter._session = mock_session

            result = await adapter.create_group(
                participants=["+1987654321", "+1555555555"],
                name="Test Group",
            )

            assert result == "new-group-guid"

    @pytest.mark.asyncio
    async def test_get_chat_info(self, mock_session):
        """Test getting chat info."""
        mock_session.get.return_value = AsyncMock(
            __aenter__=AsyncMock(return_value=AsyncMock(
                status=200,
                json=AsyncMock(return_value={
                    "data": {
                        "guid": "chat-guid",
                        "displayName": "Test Chat",
                        "style": 45,
                        "participants": [{"address": "+1234567890"}],
                    }
                })
            ))
        )

        from integrations.channels.imessage_adapter import IMessageAdapter

        config = ChannelConfig(token="test_password", extra={"api_url": "http://localhost:1234"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = IMessageAdapter(config)
            adapter._session = mock_session

            result = await adapter.get_chat_info("chat-guid")

            assert result is not None
            assert result["id"] == "chat-guid"
            assert result["display_name"] == "Test Chat"


class TestIMessageFactory:
    """Tests for iMessage adapter factory."""

    def test_factory_with_params(self):
        """Test factory function with parameters."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            from integrations.channels.imessage_adapter import create_imessage_adapter

            adapter = create_imessage_adapter(
                password="my_password",
                api_url="http://my-mac:1234",
            )

            assert adapter.name == "imessage"
            assert adapter._password == "my_password"
            assert adapter._api_url == "http://my-mac:1234"

    def test_factory_with_env_vars(self):
        """Test factory function with environment variables."""
        with patch.dict(os.environ, {
            "BLUEBUBBLES_PASSWORD": "env_password",
            "BLUEBUBBLES_URL": "http://env-mac:1234",
        }):
            with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
                from integrations.channels.imessage_adapter import create_imessage_adapter

                adapter = create_imessage_adapter()

                assert adapter._password == "env_password"
                assert adapter._api_url == "http://env-mac:1234"

    def test_factory_missing_password(self):
        """Test factory function without password."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("BLUEBUBBLES_PASSWORD", None)

            with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
                from integrations.channels.imessage_adapter import create_imessage_adapter

                with pytest.raises(ValueError, match="password required"):
                    create_imessage_adapter()


class TestIMessageReadReceipts:
    """Tests for iMessage read receipts."""

    @pytest.fixture
    def mock_session(self):
        """Create mock aiohttp session."""
        session = MagicMock()
        response = AsyncMock()
        response.status = 200

        session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response)))
        return session

    @pytest.mark.asyncio
    async def test_mark_read(self, mock_session):
        """Test marking chat as read."""
        from integrations.channels.imessage_adapter import IMessageAdapter

        config = ChannelConfig(token="test_password", extra={"api_url": "http://localhost:1234"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = IMessageAdapter(config)
            adapter._session = mock_session

            result = await adapter.mark_read("chat-guid")

            assert result is True
            mock_session.post.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
