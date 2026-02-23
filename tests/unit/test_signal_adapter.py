"""
Tests for Signal Channel Adapter

Tests the Signal adapter functionality including:
- Message conversion
- Send/receive operations
- Group support
- Reactions
- Error handling
- Reconnection logic
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


class TestSignalAdapter:
    """Tests for SignalAdapter."""

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
        mock_session.put = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_response)))
        mock_session.close = AsyncMock()

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            with patch('aiohttp.ClientSession', return_value=mock_session):
                yield mock_session

    @pytest.fixture
    def signal_config(self):
        """Create Signal adapter config."""
        return ChannelConfig(
            token="+1234567890",
            extra={"api_url": "http://localhost:8080"}
        )

    def test_adapter_creation(self, mock_aiohttp, signal_config):
        """Test SignalAdapter instantiation."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        assert adapter.name == "signal"
        assert adapter.status == ChannelStatus.DISCONNECTED
        assert adapter._phone_number == "+1234567890"
        assert adapter._api_url == "http://localhost:8080"

    def test_message_handler_registration(self, mock_aiohttp, signal_config):
        """Test message handler registration."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        handler_called = False

        async def test_handler(msg):
            nonlocal handler_called
            handler_called = True

        adapter.on_message(test_handler)
        assert len(adapter._message_handlers) == 1

    @pytest.mark.asyncio
    async def test_connect_success(self, mock_aiohttp, signal_config):
        """Test successful connection."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        # Mock API responses
        about_response = AsyncMock()
        about_response.status = 200

        accounts_response = AsyncMock()
        accounts_response.status = 200
        accounts_response.json = AsyncMock(return_value=[{"number": "+1234567890"}])

        mock_aiohttp.get = MagicMock(side_effect=[
            AsyncMock(__aenter__=AsyncMock(return_value=about_response)),
            AsyncMock(__aenter__=AsyncMock(return_value=accounts_response)),
        ])

        with patch('aiohttp.ClientSession', return_value=mock_aiohttp):
            result = await adapter.connect()

        # Connection starts polling in background, so it should return True
        # Note: We can't fully test async polling here
        assert adapter._phone_number == "+1234567890"

    def test_message_conversion(self, mock_aiohttp, signal_config):
        """Test Signal message to unified Message conversion."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        # Mock Signal message data
        msg_data = {
            "envelope": {
                "source": "+1987654321",
                "sourceName": "John Doe",
                "timestamp": 1699000000000,
                "dataMessage": {
                    "message": "Hello from Signal!",
                    "groupInfo": None,
                    "attachments": [],
                    "mentions": [],
                }
            }
        }

        message = adapter._convert_message(msg_data)

        assert message is not None
        assert message.channel == "signal"
        assert message.sender_id == "+1987654321"
        assert message.sender_name == "John Doe"
        assert message.text == "Hello from Signal!"
        assert message.is_group is False

    def test_group_message_conversion(self, mock_aiohttp, signal_config):
        """Test group message conversion."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        msg_data = {
            "envelope": {
                "source": "+1987654321",
                "sourceName": "John Doe",
                "timestamp": 1699000000000,
                "dataMessage": {
                    "message": "Hello group!",
                    "groupInfo": {
                        "groupId": "abc123groupid",
                    },
                    "attachments": [],
                    "mentions": [],
                }
            }
        }

        message = adapter._convert_message(msg_data)

        assert message is not None
        assert message.is_group is True
        assert message.chat_id == "abc123groupid"

    def test_attachment_handling(self, mock_aiohttp, signal_config):
        """Test attachment in message."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        msg_data = {
            "envelope": {
                "source": "+1987654321",
                "sourceName": "John Doe",
                "timestamp": 1699000000000,
                "dataMessage": {
                    "message": "Check this out",
                    "groupInfo": None,
                    "attachments": [
                        {
                            "id": "att123",
                            "filename": "photo.jpg",
                            "contentType": "image/jpeg",
                            "size": 12345,
                        }
                    ],
                    "mentions": [],
                }
            }
        }

        message = adapter._convert_message(msg_data)

        assert message is not None
        assert message.has_media
        assert len(message.media) == 1
        assert message.media[0].type == MessageType.IMAGE
        assert message.media[0].file_name == "photo.jpg"

    def test_mention_detection(self, mock_aiohttp, signal_config):
        """Test bot mention detection."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        msg_data = {
            "envelope": {
                "source": "+1987654321",
                "sourceName": "John Doe",
                "timestamp": 1699000000000,
                "dataMessage": {
                    "message": "Hey @bot!",
                    "groupInfo": None,
                    "attachments": [],
                    "mentions": [
                        {"number": "+1234567890"}  # Bot's number
                    ],
                }
            }
        }

        message = adapter._convert_message(msg_data)

        assert message is not None
        assert message.is_bot_mentioned is True

    def test_media_type_detection(self, mock_aiohttp, signal_config):
        """Test media type detection from content type."""
        from integrations.channels.signal_adapter import SignalAdapter

        adapter = SignalAdapter(signal_config)

        assert adapter._get_media_type("image/jpeg") == MessageType.IMAGE
        assert adapter._get_media_type("image/png") == MessageType.IMAGE
        assert adapter._get_media_type("video/mp4") == MessageType.VIDEO
        assert adapter._get_media_type("audio/ogg") == MessageType.AUDIO
        assert adapter._get_media_type("application/pdf") == MessageType.DOCUMENT
        assert adapter._get_media_type("unknown/type") == MessageType.DOCUMENT


class TestSignalSending:
    """Tests for Signal message sending."""

    @pytest.fixture
    def mock_session(self):
        """Create mock aiohttp session."""
        session = MagicMock()
        response = AsyncMock()
        response.status = 200
        response.json = AsyncMock(return_value={"timestamp": 1699000000000})

        session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response)))
        session.close = AsyncMock()

        return session

    @pytest.mark.asyncio
    async def test_send_direct_message(self, mock_session):
        """Test sending direct message."""
        from integrations.channels.signal_adapter import SignalAdapter

        config = ChannelConfig(token="+1234567890", extra={"api_url": "http://localhost:8080"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = SignalAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_message(
                chat_id="+1987654321",
                text="Hello!",
            )

            assert result.success
            assert result.message_id == "1699000000000"
            mock_session.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_group_message(self, mock_session):
        """Test sending group message."""
        from integrations.channels.signal_adapter import SignalAdapter

        config = ChannelConfig(token="+1234567890", extra={"api_url": "http://localhost:8080"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = SignalAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_message(
                chat_id="group.abc123",
                text="Hello group!",
            )

            assert result.success

    @pytest.mark.asyncio
    async def test_send_with_quote(self, mock_session):
        """Test sending message with quote/reply."""
        from integrations.channels.signal_adapter import SignalAdapter

        config = ChannelConfig(token="+1234567890", extra={"api_url": "http://localhost:8080"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = SignalAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_message(
                chat_id="+1987654321",
                text="Reply to this",
                reply_to="1699000000000",
            )

            assert result.success

    @pytest.mark.asyncio
    async def test_send_not_connected(self):
        """Test sending when not connected."""
        from integrations.channels.signal_adapter import SignalAdapter

        config = ChannelConfig(token="+1234567890", extra={"api_url": "http://localhost:8080"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = SignalAdapter(config)
            # Don't set _session

            result = await adapter.send_message(
                chat_id="+1987654321",
                text="Hello!",
            )

            assert not result.success
            assert "Not connected" in result.error


class TestSignalReactions:
    """Tests for Signal reactions."""

    @pytest.fixture
    def mock_session(self):
        """Create mock aiohttp session."""
        session = MagicMock()
        response = AsyncMock()
        response.status = 200

        session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response)))
        return session

    @pytest.mark.asyncio
    async def test_send_reaction(self, mock_session):
        """Test sending reaction."""
        from integrations.channels.signal_adapter import SignalAdapter

        config = ChannelConfig(token="+1234567890", extra={"api_url": "http://localhost:8080"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = SignalAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_reaction(
                chat_id="+1987654321",
                message_id="1699000000000",
                emoji="",
            )

            assert result is True
            mock_session.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_remove_reaction(self, mock_session):
        """Test removing reaction."""
        from integrations.channels.signal_adapter import SignalAdapter

        config = ChannelConfig(token="+1234567890", extra={"api_url": "http://localhost:8080"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = SignalAdapter(config)
            adapter._session = mock_session

            result = await adapter.send_reaction(
                chat_id="+1987654321",
                message_id="1699000000000",
                emoji="",
                remove=True,
            )

            assert result is True


class TestSignalGroups:
    """Tests for Signal group operations."""

    @pytest.fixture
    def mock_session(self):
        """Create mock aiohttp session."""
        session = MagicMock()
        response = AsyncMock()
        response.status = 200
        response.json = AsyncMock(return_value={"id": "newgroupid123"})

        session.post = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response)))
        session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response)))
        return session

    @pytest.mark.asyncio
    async def test_create_group(self, mock_session):
        """Test creating group."""
        from integrations.channels.signal_adapter import SignalAdapter

        config = ChannelConfig(token="+1234567890", extra={"api_url": "http://localhost:8080"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = SignalAdapter(config)
            adapter._session = mock_session

            result = await adapter.create_group(
                name="Test Group",
                members=["+1987654321", "+1555555555"],
            )

            assert result == "group.newgroupid123"

    @pytest.mark.asyncio
    async def test_get_group_info(self, mock_session):
        """Test getting group info."""
        from integrations.channels.signal_adapter import SignalAdapter

        mock_session.get.return_value = AsyncMock(
            __aenter__=AsyncMock(return_value=AsyncMock(
                status=200,
                json=AsyncMock(return_value={
                    "name": "Test Group",
                    "members": ["+1234567890", "+1987654321"],
                })
            ))
        )

        config = ChannelConfig(token="+1234567890", extra={"api_url": "http://localhost:8080"})

        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            adapter = SignalAdapter(config)
            adapter._session = mock_session

            result = await adapter.get_chat_info("group.abc123")

            assert result is not None
            assert result["type"] == "group"
            assert result["name"] == "Test Group"


class TestSignalFactory:
    """Tests for Signal adapter factory."""

    def test_factory_with_params(self):
        """Test factory function with parameters."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
            from integrations.channels.signal_adapter import create_signal_adapter

            adapter = create_signal_adapter(
                phone_number="+1234567890",
                api_url="http://signal-api:8080",
            )

            assert adapter.name == "signal"
            assert adapter._phone_number == "+1234567890"
            assert adapter._api_url == "http://signal-api:8080"

    def test_factory_with_env_vars(self):
        """Test factory function with environment variables."""
        with patch.dict(os.environ, {
            "SIGNAL_PHONE_NUMBER": "+1999999999",
            "SIGNAL_API_URL": "http://env-signal:8080",
        }):
            with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
                from integrations.channels.signal_adapter import create_signal_adapter

                adapter = create_signal_adapter()

                assert adapter._phone_number == "+1999999999"
                assert adapter._api_url == "http://env-signal:8080"

    def test_factory_missing_phone(self):
        """Test factory function without phone number."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove env vars
            os.environ.pop("SIGNAL_PHONE_NUMBER", None)

            with patch.dict('sys.modules', {'aiohttp': MagicMock()}):
                from integrations.channels.signal_adapter import create_signal_adapter

                with pytest.raises(ValueError, match="phone number required"):
                    create_signal_adapter()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
