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
        assert info["id"] == "sess-1"
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


def _ws_binary_frame(file_name=None, file_data=b"payload-bytes", *, metadata=None):
    """Build a WebSocket binary upload frame the way a browser client would.

    Wire format consumed by ``_handle_ws_binary``:
        [4-byte big-endian metadata length][metadata JSON][raw file bytes]
    """
    if metadata is None:
        metadata = {} if file_name is None else {"file_name": file_name}
    meta_bytes = json.dumps(metadata).encode()
    return len(meta_bytes).to_bytes(4, "big") + meta_bytes + file_data


class TestWebAdapterBinaryUpload:
    """Behavioural tests for ``_handle_ws_binary`` (WebSocket file uploads).

    Exercises the REAL adapter method against a REAL temp upload directory,
    mocking only the WebSocket boundary so we can capture the confirmation /
    error frames the adapter emits. Focus: the path-traversal escape and the
    malformed-frame degrade paths the happy-path suite never touched.
    """

    def _make_adapter(self, tmp_path):
        import integrations.channels.web_adapter as wa

        if not wa.HAS_AIOHTTP:
            pytest.skip("aiohttp not installed; WebAdapter cannot be constructed")

        upload_dir = tmp_path / "uploads"
        config = ChannelConfig(extra={"port": 8765, "upload_dir": str(upload_dir)})
        adapter = wa.WebAdapter(config)
        # connect() would create this; we don't start a server in unit tests.
        upload_dir.mkdir(parents=True, exist_ok=True)
        return adapter

    def _connected_session(self, adapter, sid="sess-bin", uid="user-bin"):
        """Register a session with a mock websocket so we can capture frames."""
        from integrations.channels.web_adapter import WebSession

        session = WebSession(session_id=sid, user_id=uid)
        ws = MagicMock()
        ws.send_json = AsyncMock()
        session.websockets.add(ws)
        adapter._sessions[sid] = session
        return session, ws

    @staticmethod
    def _sent_frames(ws):
        return [call.args[0] for call in ws.send_json.call_args_list]

    @pytest.mark.asyncio
    async def test_binary_upload_happy_path(self, tmp_path):
        """A well-formed upload lands inside upload_dir and confirms."""
        adapter = self._make_adapter(tmp_path)
        session, ws = self._connected_session(adapter)

        payload = b"%PDF-1.4 fake report bytes"
        frame = _ws_binary_frame("report.pdf", payload)

        await adapter._handle_ws_binary(session, frame)

        written = list(adapter._upload_dir.glob("*_report.pdf"))
        assert len(written) == 1, "file should be stored under upload_dir"
        assert written[0].read_bytes() == payload

        frames = self._sent_frames(ws)
        assert len(frames) == 1
        assert frames[0]["type"] == "upload_complete"
        assert frames[0]["file_name"] == "report.pdf"
        assert frames[0]["size"] == len(payload)

    @pytest.mark.asyncio
    async def test_binary_upload_path_traversal_is_contained(self, tmp_path):
        """SECURITY: a crafted '../' file_name must NOT escape upload_dir.

        Regression guard for the arbitrary-file-write over an unauthenticated
        WebSocket. On Windows the OS normalises '..' lexically, so the raw
        ``f"{uuid}_{file_name}"`` join wrote OUTSIDE upload_dir; on POSIX the
        same name raised ENOENT and stored nothing. Either way the untrusted
        name must be sanitised to a bare basename kept inside upload_dir.
        """
        adapter = self._make_adapter(tmp_path)
        session, ws = self._connected_session(adapter)

        payload = b"PWNED-CONTENTS"
        frame = _ws_binary_frame("../../../pwned.txt", payload)

        before = {p for p in tmp_path.rglob("*") if p.is_file()}
        await adapter._handle_ws_binary(session, frame)
        after = {p for p in tmp_path.rglob("*") if p.is_file()}

        upload_dir = adapter._upload_dir.resolve()
        new_files = after - before
        escaped = [p for p in new_files if not p.resolve().is_relative_to(upload_dir)]
        assert not escaped, f"upload escaped upload_dir to: {escaped}"

        # Positive side: the payload is stored, sanitised, inside upload_dir.
        stored = list(adapter._upload_dir.glob("*_pwned.txt"))
        assert len(stored) == 1, "sanitised file should be kept inside upload_dir"
        assert stored[0].read_bytes() == payload

    @pytest.mark.asyncio
    async def test_binary_upload_backslash_traversal_is_contained(self, tmp_path):
        """SECURITY: Windows-style backslash traversal is sanitised too."""
        adapter = self._make_adapter(tmp_path)
        session, ws = self._connected_session(adapter)

        frame = _ws_binary_frame("..\\..\\..\\pwn_bs.txt", b"bs-bytes")

        before = {p for p in tmp_path.rglob("*") if p.is_file()}
        await adapter._handle_ws_binary(session, frame)
        after = {p for p in tmp_path.rglob("*") if p.is_file()}

        upload_dir = adapter._upload_dir.resolve()
        escaped = [p for p in (after - before) if not p.resolve().is_relative_to(upload_dir)]
        assert not escaped, f"backslash upload escaped upload_dir to: {escaped}"

        stored = list(adapter._upload_dir.glob("*_pwn_bs.txt"))
        assert len(stored) == 1

    @pytest.mark.asyncio
    async def test_binary_upload_absolute_path_is_contained(self, tmp_path):
        """SECURITY: an absolute-looking name stays inside upload_dir."""
        adapter = self._make_adapter(tmp_path)
        session, ws = self._connected_session(adapter)

        frame = _ws_binary_frame("/etc/cron.d/evil", b"cronjob")

        before = {p for p in tmp_path.rglob("*") if p.is_file()}
        await adapter._handle_ws_binary(session, frame)
        after = {p for p in tmp_path.rglob("*") if p.is_file()}

        upload_dir = adapter._upload_dir.resolve()
        escaped = [p for p in (after - before) if not p.resolve().is_relative_to(upload_dir)]
        assert not escaped, f"absolute-path upload escaped upload_dir to: {escaped}"

        # basename 'evil' is what survives sanitisation
        stored = list(adapter._upload_dir.glob("*_evil"))
        assert len(stored) == 1

    @pytest.mark.asyncio
    async def test_binary_upload_missing_file_name_defaults(self, tmp_path):
        """Metadata with no file_name falls back to the 'upload' default."""
        adapter = self._make_adapter(tmp_path)
        session, ws = self._connected_session(adapter)

        frame = _ws_binary_frame(metadata={"unrelated": "x"}, file_data=b"abc")

        await adapter._handle_ws_binary(session, frame)

        stored = list(adapter._upload_dir.glob("*_upload"))
        assert len(stored) == 1
        assert stored[0].read_bytes() == b"abc"

    @pytest.mark.asyncio
    async def test_binary_upload_dotdot_only_name_is_contained(self, tmp_path):
        """A file_name that is purely '..' must not become a directory ref."""
        adapter = self._make_adapter(tmp_path)
        session, ws = self._connected_session(adapter)

        frame = _ws_binary_frame("..", b"xyz")

        before = {p for p in tmp_path.rglob("*") if p.is_file()}
        await adapter._handle_ws_binary(session, frame)
        after = {p for p in tmp_path.rglob("*") if p.is_file()}

        upload_dir = adapter._upload_dir.resolve()
        new_files = after - before
        escaped = [p for p in new_files if not p.resolve().is_relative_to(upload_dir)]
        assert not escaped, f"'..' name escaped upload_dir to: {escaped}"
        # Something was still stored (fell back to a safe default name).
        assert len(new_files) == 1

    @pytest.mark.asyncio
    async def test_binary_upload_invalid_json_metadata_degrades(self, tmp_path):
        """Non-JSON metadata → no crash, upload_error emitted, nothing written."""
        adapter = self._make_adapter(tmp_path)
        session, ws = self._connected_session(adapter)

        bad_meta = b"not-json-at-all"
        frame = len(bad_meta).to_bytes(4, "big") + bad_meta + b"filebytes"

        before = {p for p in tmp_path.rglob("*") if p.is_file()}
        await adapter._handle_ws_binary(session, frame)  # must not raise
        after = {p for p in tmp_path.rglob("*") if p.is_file()}

        assert after == before, "malformed frame must not write any file"
        frames = self._sent_frames(ws)
        assert frames and frames[-1]["type"] == "upload_error"

    @pytest.mark.asyncio
    async def test_binary_upload_empty_frame_degrades(self, tmp_path):
        """An empty binary frame degrades cleanly to upload_error."""
        adapter = self._make_adapter(tmp_path)
        session, ws = self._connected_session(adapter)

        before = {p for p in tmp_path.rglob("*") if p.is_file()}
        await adapter._handle_ws_binary(session, b"")  # must not raise
        after = {p for p in tmp_path.rglob("*") if p.is_file()}

        assert after == before
        frames = self._sent_frames(ws)
        assert frames and frames[-1]["type"] == "upload_error"

    @pytest.mark.asyncio
    async def test_binary_upload_truncated_metadata_length_degrades(self, tmp_path):
        """A declared length longer than the buffer degrades to upload_error."""
        adapter = self._make_adapter(tmp_path)
        session, ws = self._connected_session(adapter)

        # Claim 9999 bytes of metadata but supply only a few.
        frame = (9999).to_bytes(4, "big") + b'{"file_name"'  # truncated JSON

        before = {p for p in tmp_path.rglob("*") if p.is_file()}
        await adapter._handle_ws_binary(session, frame)  # must not raise
        after = {p for p in tmp_path.rglob("*") if p.is_file()}

        assert after == before
        frames = self._sent_frames(ws)
        assert frames and frames[-1]["type"] == "upload_error"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
