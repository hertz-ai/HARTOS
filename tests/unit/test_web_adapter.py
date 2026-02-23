"""
Tests for Web/Browser Channel Adapter

Tests the Web adapter functionality including:
- WebSocket connections
- REST API endpoints
- Session management
- File upload/download
- Typing indicators
- Read receipts
- Multi-tab support
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timedelta
import json
import uuid

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


class TestWebAdapter:
    """Tests for WebAdapter."""

    @pytest.fixture
    def mock_aiohttp(self):
        """Create mock aiohttp module."""
        mock_web = MagicMock()

        with patch.dict('sys.modules', {
            'aiohttp': MagicMock(),
            'aiohttp.web': mock_web,
        }):
            yield mock_web

    @pytest.fixture
    def web_config(self):
        """Create Web adapter config."""
        return ChannelConfig(
            extra={
                "host": "0.0.0.0",
                "port": 8765,
                "upload_dir": "/tmp/test_uploads",
                "cors_origins": ["*"],
            }
        )

    def test_adapter_creation(self, mock_aiohttp, web_config):
        """Test WebAdapter instantiation."""
        from integrations.channels.web_adapter import WebAdapter

        adapter = WebAdapter(web_config)

        assert adapter.name == "web"
        assert adapter.status == ChannelStatus.DISCONNECTED
        assert adapter._host == "0.0.0.0"
        assert adapter._port == 8765

    def test_message_handler_registration(self, mock_aiohttp, web_config):
        """Test message handler registration."""
        from integrations.channels.web_adapter import WebAdapter

        adapter = WebAdapter(web_config)

        handler_called = False

        async def test_handler(msg):
            nonlocal handler_called
            handler_called = True

        adapter.on_message(test_handler)
        assert len(adapter._message_handlers) == 1


class TestWebSession:
    """Tests for WebSession dataclass."""

    def test_session_creation(self):
        """Test WebSession creation."""
        from integrations.channels.web_adapter import WebSession

        session = WebSession(
            session_id="sess-123",
            user_id="user-456",
            user_name="John Doe",
        )

        assert session.session_id == "sess-123"
        assert session.user_id == "user-456"
        assert session.user_name == "John Doe"
        assert not session.is_connected
        assert isinstance(session.connected_at, datetime)

    def test_session_connected(self):
        """Test session connection status."""
        from integrations.channels.web_adapter import WebSession

        session = WebSession(
            session_id="sess-123",
            user_id="user-456",
        )

        # Initially not connected
        assert not session.is_connected

        # Add a websocket
        mock_ws = MagicMock()
        session.websockets.add(mock_ws)

        assert session.is_connected

    def test_session_touch(self):
        """Test session activity update."""
        from integrations.channels.web_adapter import WebSession

        session = WebSession(
            session_id="sess-123",
            user_id="user-456",
        )

        old_activity = session.last_activity
        session.touch()

        assert session.last_activity >= old_activity


class TestPendingMessage:
    """Tests for PendingMessage dataclass."""

    def test_pending_message_creation(self):
        """Test PendingMessage creation."""
        from integrations.channels.web_adapter import PendingMessage

        msg = PendingMessage(
            id="msg-123",
            session_id="sess-456",
            data={"type": "message", "text": "Hello!"},
        )

        assert msg.id == "msg-123"
        assert msg.session_id == "sess-456"
        assert msg.data["text"] == "Hello!"
        assert isinstance(msg.created_at, datetime)
        assert msg.expires_at > msg.created_at


class TestWebAdapterSessions:
    """Tests for session management."""

    @pytest.fixture
    def adapter(self):
        """Create adapter for testing."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock(), 'aiohttp.web': MagicMock()}):
            from integrations.channels.web_adapter import WebAdapter, WebSession

            config = ChannelConfig(extra={"port": 8765})
            adapter = WebAdapter(config)

            # Add some test sessions
            adapter._sessions["sess-1"] = WebSession(
                session_id="sess-1",
                user_id="user-1",
                user_name="Alice",
            )
            adapter._sessions["sess-2"] = WebSession(
                session_id="sess-2",
                user_id="user-2",
                user_name="Bob",
            )

            return adapter

    def test_get_active_sessions(self, adapter):
        """Test getting active sessions."""
        sessions = adapter.get_active_sessions()

        assert len(sessions) == 2
        assert any(s["user_name"] == "Alice" for s in sessions)
        assert any(s["user_name"] == "Bob" for s in sessions)

    @pytest.mark.asyncio
    async def test_get_chat_info(self, adapter):
        """Test getting chat/session info."""
        info = await adapter.get_chat_info("sess-1")

        assert info is not None
        assert info["session_id"] == "sess-1"
        assert info["user_name"] == "Alice"
        assert info["type"] == "web"

    @pytest.mark.asyncio
    async def test_get_chat_info_not_found(self, adapter):
        """Test getting info for non-existent session."""
        info = await adapter.get_chat_info("non-existent")

        assert info is None


class TestWebAdapterMessaging:
    """Tests for message sending."""

    @pytest.fixture
    def adapter(self):
        """Create adapter with mock session."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock(), 'aiohttp.web': MagicMock()}):
            from integrations.channels.web_adapter import WebAdapter, WebSession

            config = ChannelConfig(extra={"port": 8765})
            adapter = WebAdapter(config)

            # Create session with mock websocket
            session = WebSession(
                session_id="sess-1",
                user_id="user-1",
            )
            mock_ws = MagicMock()
            mock_ws.send_json = AsyncMock()
            session.websockets.add(mock_ws)

            adapter._sessions["sess-1"] = session

            return adapter

    @pytest.mark.asyncio
    async def test_send_message(self, adapter):
        """Test sending message to connected session."""
        result = await adapter.send_message(
            chat_id="sess-1",
            text="Hello!",
        )

        assert result.success
        assert result.message_id is not None
        assert result.raw["delivered"] is True

    @pytest.mark.asyncio
    async def test_send_with_media(self, adapter):
        """Test sending message with attachments."""
        result = await adapter.send_message(
            chat_id="sess-1",
            text="Check this out",
            media=[
                MediaAttachment(
                    type=MessageType.IMAGE,
                    file_id="file-123",
                    file_name="photo.jpg",
                )
            ],
        )

        assert result.success

    @pytest.mark.asyncio
    async def test_send_with_buttons(self, adapter):
        """Test sending message with buttons."""
        result = await adapter.send_message(
            chat_id="sess-1",
            text="Choose an option",
            buttons=[
                {"text": "Option 1", "callback_data": "opt1"},
                {"text": "Option 2", "callback_data": "opt2"},
            ],
        )

        assert result.success

    @pytest.mark.asyncio
    async def test_send_to_offline_session(self):
        """Test sending to offline session (should queue)."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock(), 'aiohttp.web': MagicMock()}):
            from integrations.channels.web_adapter import WebAdapter, WebSession

            config = ChannelConfig(extra={"port": 8765})
            adapter = WebAdapter(config)

            # Create offline session (no websockets)
            session = WebSession(
                session_id="sess-offline",
                user_id="user-offline",
            )
            adapter._sessions["sess-offline"] = session

            result = await adapter.send_message(
                chat_id="sess-offline",
                text="Queued message",
            )

            assert result.success
            assert result.raw["delivered"] is False
            assert result.raw["queued"] is True
            assert "sess-offline" in adapter._pending_messages

    @pytest.mark.asyncio
    async def test_edit_message(self, adapter):
        """Test editing message."""
        result = await adapter.edit_message(
            chat_id="sess-1",
            message_id="msg-123",
            text="Edited text",
        )

        assert result.success
        assert result.message_id == "msg-123"

    @pytest.mark.asyncio
    async def test_delete_message(self, adapter):
        """Test deleting message."""
        result = await adapter.delete_message(
            chat_id="sess-1",
            message_id="msg-123",
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_send_typing(self, adapter):
        """Test sending typing indicator."""
        # Should not raise
        await adapter.send_typing("sess-1")


class TestWebAdapterReadReceipts:
    """Tests for read receipts."""

    @pytest.fixture
    def adapter(self):
        """Create adapter for testing."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock(), 'aiohttp.web': MagicMock()}):
            from integrations.channels.web_adapter import WebAdapter

            config = ChannelConfig(extra={"port": 8765})
            adapter = WebAdapter(config)

            # Add some read receipts
            adapter._read_receipts["msg-1"] = {"sess-1", "sess-2"}
            adapter._read_receipts["msg-2"] = {"sess-1"}

            return adapter

    def test_get_read_receipts(self, adapter):
        """Test getting read receipts for a message."""
        receipts = adapter.get_read_receipts("msg-1")

        assert len(receipts) == 2
        assert "sess-1" in receipts
        assert "sess-2" in receipts

    def test_get_read_receipts_empty(self, adapter):
        """Test getting receipts for unread message."""
        receipts = adapter.get_read_receipts("msg-unread")

        assert len(receipts) == 0


class TestWebAdapterMessageHandling:
    """Tests for incoming message handling."""

    @pytest.fixture
    def adapter(self):
        """Create adapter for testing."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock(), 'aiohttp.web': MagicMock()}):
            from integrations.channels.web_adapter import WebAdapter, WebSession

            config = ChannelConfig(extra={"port": 8765})
            adapter = WebAdapter(config)

            return adapter

    @pytest.mark.asyncio
    async def test_handle_text_message(self, adapter):
        """Test handling incoming text message."""
        from integrations.channels.web_adapter import WebSession

        messages_received = []

        async def handler(msg):
            messages_received.append(msg)

        adapter.on_message(handler)

        session = WebSession(
            session_id="sess-1",
            user_id="user-1",
            user_name="Test User",
        )

        data = json.dumps({
            "type": "message",
            "text": "Hello from browser!",
        })

        await adapter._handle_ws_message(session, data)

        assert len(messages_received) == 1
        assert messages_received[0].text == "Hello from browser!"
        assert messages_received[0].sender_id == "user-1"
        assert messages_received[0].channel == "web"

    @pytest.mark.asyncio
    async def test_handle_message_with_attachments(self, adapter):
        """Test handling message with attachments."""
        from integrations.channels.web_adapter import WebSession

        messages_received = []

        async def handler(msg):
            messages_received.append(msg)

        adapter.on_message(handler)

        session = WebSession(
            session_id="sess-1",
            user_id="user-1",
        )

        data = json.dumps({
            "type": "message",
            "text": "Check this file",
            "attachments": [
                {
                    "type": "document",
                    "file_id": "file-123",
                    "file_name": "report.pdf",
                }
            ],
        })

        await adapter._handle_ws_message(session, data)

        assert len(messages_received) == 1
        assert messages_received[0].has_media
        assert len(messages_received[0].media) == 1

    @pytest.mark.asyncio
    async def test_handle_typing_event(self, adapter):
        """Test handling typing indicator."""
        from integrations.channels.web_adapter import WebSession

        session = WebSession(
            session_id="sess-1",
            user_id="user-1",
        )

        data = json.dumps({"type": "typing"})

        await adapter._handle_ws_message(session, data)

        assert "sess-1" in adapter._typing_status

    @pytest.mark.asyncio
    async def test_handle_read_event(self, adapter):
        """Test handling read receipt."""
        from integrations.channels.web_adapter import WebSession

        session = WebSession(
            session_id="sess-1",
            user_id="user-1",
        )

        data = json.dumps({
            "type": "read",
            "message_ids": ["msg-1", "msg-2"],
        })

        await adapter._handle_ws_message(session, data)

        assert "msg-1" in adapter._read_receipts
        assert "sess-1" in adapter._read_receipts["msg-1"]

    @pytest.mark.asyncio
    async def test_handle_ping(self, adapter):
        """Test handling ping message."""
        from integrations.channels.web_adapter import WebSession

        session = WebSession(
            session_id="sess-1",
            user_id="user-1",
        )

        # Add mock websocket
        mock_ws = MagicMock()
        mock_ws.send_json = AsyncMock()
        session.websockets.add(mock_ws)
        adapter._sessions["sess-1"] = session

        data = json.dumps({"type": "ping"})

        await adapter._handle_ws_message(session, data)

        # Should send pong
        mock_ws.send_json.assert_called()
        call_args = mock_ws.send_json.call_args[0][0]
        assert call_args["type"] == "pong"


class TestWebAdapterBroadcast:
    """Tests for broadcasting to sessions."""

    @pytest.fixture
    def adapter_with_sessions(self):
        """Create adapter with multiple sessions."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock(), 'aiohttp.web': MagicMock()}):
            from integrations.channels.web_adapter import WebAdapter, WebSession

            config = ChannelConfig(extra={"port": 8765})
            adapter = WebAdapter(config)

            # Create multiple sessions
            for i in range(3):
                session = WebSession(
                    session_id=f"sess-{i}",
                    user_id=f"user-{i}",
                    user_name=f"User {i}",
                )
                mock_ws = MagicMock()
                mock_ws.send_json = AsyncMock()
                session.websockets.add(mock_ws)
                adapter._sessions[f"sess-{i}"] = session

            return adapter

    @pytest.mark.asyncio
    async def test_broadcast_typing(self, adapter_with_sessions):
        """Test broadcasting typing indicator."""
        await adapter_with_sessions._broadcast_typing("sess-0", "User 0")

        # Other sessions should receive typing
        for session_id in ["sess-1", "sess-2"]:
            session = adapter_with_sessions._sessions[session_id]
            ws = list(session.websockets)[0]
            ws.send_json.assert_called()


class TestWebAdapterFactory:
    """Tests for Web adapter factory."""

    def test_factory_with_params(self):
        """Test factory with parameters."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock(), 'aiohttp.web': MagicMock()}):
            from integrations.channels.web_adapter import create_web_adapter

            adapter = create_web_adapter(
                host="127.0.0.1",
                port=9999,
            )

            assert adapter.name == "web"
            assert adapter._host == "127.0.0.1"
            assert adapter._port == 9999

    def test_factory_with_env_vars(self):
        """Test factory with environment variables."""
        with patch.dict(os.environ, {
            "WEB_ADAPTER_HOST": "0.0.0.0",
            "WEB_ADAPTER_PORT": "8888",
        }):
            with patch.dict('sys.modules', {'aiohttp': MagicMock(), 'aiohttp.web': MagicMock()}):
                from integrations.channels.web_adapter import create_web_adapter

                adapter = create_web_adapter()

                assert adapter._host == "0.0.0.0"
                assert adapter._port == 8888

    def test_factory_default_values(self):
        """Test factory with default values."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("WEB_ADAPTER_HOST", None)
            os.environ.pop("WEB_ADAPTER_PORT", None)

            with patch.dict('sys.modules', {'aiohttp': MagicMock(), 'aiohttp.web': MagicMock()}):
                from integrations.channels.web_adapter import create_web_adapter

                adapter = create_web_adapter()

                assert adapter._host == "0.0.0.0"
                assert adapter._port == 8765


class TestWebAdapterCleanup:
    """Tests for session cleanup."""

    @pytest.fixture
    def adapter_with_old_session(self):
        """Create adapter with expired session."""
        with patch.dict('sys.modules', {'aiohttp': MagicMock(), 'aiohttp.web': MagicMock()}):
            from integrations.channels.web_adapter import WebAdapter, WebSession, PendingMessage

            config = ChannelConfig(extra={"port": 8765, "session_timeout": 60})
            adapter = WebAdapter(config)

            # Create old session
            old_session = WebSession(
                session_id="old-sess",
                user_id="old-user",
            )
            old_session.last_activity = datetime.now() - timedelta(hours=2)
            adapter._sessions["old-sess"] = old_session

            # Create current session
            current_session = WebSession(
                session_id="current-sess",
                user_id="current-user",
            )
            mock_ws = MagicMock()
            current_session.websockets.add(mock_ws)
            adapter._sessions["current-sess"] = current_session

            # Add expired pending message
            adapter._pending_messages["old-sess"] = [
                PendingMessage(
                    id="old-msg",
                    session_id="old-sess",
                    data={"text": "old"},
                    expires_at=datetime.now() - timedelta(hours=1),
                )
            ]

            return adapter

    def test_session_timeout_detection(self, adapter_with_old_session):
        """Test that old sessions are detected."""
        # The old session should be identified for cleanup
        now = datetime.now()
        timeout = timedelta(seconds=60)

        old_session = adapter_with_old_session._sessions["old-sess"]
        current_session = adapter_with_old_session._sessions["current-sess"]

        assert not old_session.is_connected
        assert (now - old_session.last_activity) > timeout

        assert current_session.is_connected


class TestWebAdapterIntegration:
    """Integration tests for Web adapter."""

    def test_message_round_trip_structure(self):
        """Test message structure for round-trip."""
        from integrations.channels.base import Message, MessageType

        # Create message as it would be received
        msg = Message(
            id="msg-123",
            channel="web",
            sender_id="user-1",
            sender_name="Test User",
            chat_id="sess-1",
            text="Hello from web!",
            is_group=False,
            raw={"type": "message", "text": "Hello from web!"},
        )

        assert msg.id == "msg-123"
        assert msg.channel == "web"
        assert msg.content == "Hello from web!"

    def test_send_result_structure(self):
        """Test SendResult structure."""
        from integrations.channels.base import SendResult

        result = SendResult(
            success=True,
            message_id="sent-msg-123",
            raw={"delivered": True, "queued": False},
        )

        assert result.success
        assert result.message_id == "sent-msg-123"
        assert result.raw["delivered"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
