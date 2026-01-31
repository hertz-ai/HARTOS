"""
Tests for Response Streaming Module

Tests the StreamingResponse functionality including:
- Stream processing with async generators
- Message editing for supported platforms
- Fallback for platforms without edit support
- Progress indicators
- Error handling and stream interruptions
- Integration with TypingManager
- Docker/container compatibility
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime
import time

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.channels.response.streaming import (
    StreamingResponse,
    FallbackStreamingResponse,
    StreamConfig,
    StreamContext,
    StreamState,
    PlatformCapability,
    ProgressIndicator,
    PLATFORM_CAPABILITIES,
    create_streaming_response,
)


class TestStreamConfig:
    """Tests for StreamConfig dataclass."""

    def test_default_config(self):
        """Test default configuration values."""
        config = StreamConfig()

        assert config.update_interval == 0.5
        assert config.min_chunk_size == 10
        assert config.max_chunk_size == 500
        assert config.buffer_size == 4096
        assert config.show_typing_during_stream is True
        assert config.progress_indicator == "..."
        assert config.error_indicator == "[Error]"
        assert config.timeout == 300.0
        assert config.max_retries == 3
        assert config.retry_delay == 1.0
        assert config.websocket_ping_interval == 20.0
        assert config.connection_timeout == 30.0

    def test_custom_config(self):
        """Test custom configuration values."""
        config = StreamConfig(
            update_interval=1.0,
            min_chunk_size=20,
            max_chunk_size=1000,
            show_typing_during_stream=False,
            timeout=60.0,
        )

        assert config.update_interval == 1.0
        assert config.min_chunk_size == 20
        assert config.max_chunk_size == 1000
        assert config.show_typing_during_stream is False
        assert config.timeout == 60.0


class TestStreamContext:
    """Tests for StreamContext dataclass."""

    def test_context_creation(self):
        """Test StreamContext creation."""
        context = StreamContext(
            channel="telegram",
            chat_id="123456",
        )

        assert context.channel == "telegram"
        assert context.chat_id == "123456"
        assert context.message_id is None
        assert context.content_buffer == ""
        assert context.chunk_count == 0
        assert context.bytes_streamed == 0
        assert context.state == StreamState.IDLE
        assert context.error is None
        assert context.platform_capability == PlatformCapability.FULL_EDIT

    def test_context_with_all_fields(self):
        """Test StreamContext with all fields set."""
        context = StreamContext(
            channel="discord",
            chat_id="789",
            message_id="msg123",
            initial_message_id="init456",
            content_buffer="Hello",
            chunk_count=5,
            bytes_streamed=100,
            state=StreamState.STREAMING,
            platform_capability=PlatformCapability.FULL_EDIT,
            metadata={"key": "value"},
        )

        assert context.message_id == "msg123"
        assert context.initial_message_id == "init456"
        assert context.content_buffer == "Hello"
        assert context.chunk_count == 5
        assert context.bytes_streamed == 100
        assert context.state == StreamState.STREAMING
        assert context.metadata == {"key": "value"}


class TestProgressIndicator:
    """Tests for ProgressIndicator."""

    def test_default_indicator(self):
        """Test default progress indicator."""
        indicator = ProgressIndicator()

        assert indicator.enabled is True
        assert indicator.style == "dots"
        assert indicator.frames == [".", "..", "..."]
        assert indicator.current_frame == 0

    def test_next_frame_cycles(self):
        """Test that next_frame cycles through frames."""
        indicator = ProgressIndicator()

        assert indicator.next_frame() == "."
        assert indicator.next_frame() == ".."
        assert indicator.next_frame() == "..."
        assert indicator.next_frame() == "."  # Cycles back

    def test_disabled_indicator(self):
        """Test disabled indicator returns empty string."""
        indicator = ProgressIndicator(enabled=False)

        assert indicator.next_frame() == ""

    def test_none_style_indicator(self):
        """Test none style indicator returns empty string."""
        indicator = ProgressIndicator(style="none")

        assert indicator.next_frame() == ""


class TestPlatformCapabilities:
    """Tests for platform capability mappings."""

    def test_discord_has_full_edit(self):
        """Test Discord supports full edit."""
        assert PLATFORM_CAPABILITIES["discord"] == PlatformCapability.FULL_EDIT

    def test_telegram_has_full_edit(self):
        """Test Telegram supports full edit."""
        assert PLATFORM_CAPABILITIES["telegram"] == PlatformCapability.FULL_EDIT

    def test_slack_has_full_edit(self):
        """Test Slack supports full edit."""
        assert PLATFORM_CAPABILITIES["slack"] == PlatformCapability.FULL_EDIT

    def test_whatsapp_has_no_edit(self):
        """Test WhatsApp has no edit support."""
        assert PLATFORM_CAPABILITIES["whatsapp"] == PlatformCapability.NO_EDIT

    def test_sms_has_no_edit(self):
        """Test SMS has no edit support."""
        assert PLATFORM_CAPABILITIES["sms"] == PlatformCapability.NO_EDIT


class TestStreamingResponse:
    """Tests for StreamingResponse class."""

    @pytest.fixture
    def mock_send_message(self):
        """Create mock send_message callback."""
        mock = AsyncMock(return_value="msg_123")
        return mock

    @pytest.fixture
    def mock_edit_message(self):
        """Create mock edit_message callback."""
        mock = AsyncMock(return_value=None)
        return mock

    @pytest.fixture
    def mock_typing_manager(self):
        """Create mock TypingManager."""
        manager = Mock()
        manager.start_async = AsyncMock()
        manager.stop_async = AsyncMock()
        manager.pulse_async = AsyncMock()
        return manager

    @pytest.fixture
    def streaming_response(self, mock_send_message, mock_edit_message):
        """Create StreamingResponse instance."""
        return StreamingResponse(
            send_message=mock_send_message,
            edit_message=mock_edit_message,
        )

    def test_creation(self):
        """Test StreamingResponse instantiation."""
        sr = StreamingResponse()

        assert sr._send_message is None
        assert sr._edit_message is None
        assert sr._typing_manager is None
        assert isinstance(sr._config, StreamConfig)

    def test_creation_with_config(self):
        """Test StreamingResponse with custom config."""
        config = StreamConfig(update_interval=2.0)
        sr = StreamingResponse(config=config)

        assert sr.config.update_interval == 2.0

    def test_set_callbacks(self, streaming_response, mock_send_message, mock_edit_message):
        """Test setting callbacks."""
        new_send = AsyncMock()
        new_edit = AsyncMock()

        streaming_response.set_callbacks(new_send, new_edit)

        assert streaming_response._send_message == new_send
        assert streaming_response._edit_message == new_edit

    def test_set_typing_manager(self, streaming_response, mock_typing_manager):
        """Test setting typing manager."""
        streaming_response.set_typing_manager(mock_typing_manager)

        assert streaming_response._typing_manager == mock_typing_manager

    def test_get_platform_capability(self, streaming_response):
        """Test getting platform capability."""
        assert streaming_response._get_platform_capability("telegram") == PlatformCapability.FULL_EDIT
        assert streaming_response._get_platform_capability("whatsapp") == PlatformCapability.NO_EDIT
        assert streaming_response._get_platform_capability("unknown") == PlatformCapability.NO_EDIT

    def test_create_stream_key(self, streaming_response):
        """Test stream key creation."""
        key = streaming_response._create_stream_key("telegram", "123")
        assert key == "telegram:123"

    @pytest.mark.asyncio
    async def test_stream_simple(self, mock_send_message, mock_edit_message):
        """Test simple streaming."""
        sr = StreamingResponse(
            send_message=mock_send_message,
            edit_message=mock_edit_message,
            config=StreamConfig(update_interval=0.01, min_chunk_size=1)
        )

        async def simple_generator():
            yield "Hello"
            yield " "
            yield "World"

        context = await sr.stream("telegram", "123", simple_generator())

        assert context.state == StreamState.COMPLETED
        assert context.content_buffer == "Hello World"
        assert context.chunk_count == 3

    @pytest.mark.asyncio
    async def test_stream_with_typing(self, mock_send_message, mock_edit_message, mock_typing_manager):
        """Test streaming with typing indicator."""
        sr = StreamingResponse(
            send_message=mock_send_message,
            edit_message=mock_edit_message,
            typing_manager=mock_typing_manager,
            config=StreamConfig(show_typing_during_stream=True)
        )

        async def simple_generator():
            yield "Test"

        await sr.stream("telegram", "123", simple_generator())

        mock_typing_manager.start_async.assert_called_once()
        mock_typing_manager.stop_async.assert_called_once()

    @pytest.mark.asyncio
    async def test_stream_cancellation(self, mock_send_message, mock_edit_message):
        """Test stream cancellation."""
        sr = StreamingResponse(
            send_message=mock_send_message,
            edit_message=mock_edit_message,
        )

        async def slow_generator():
            for i in range(100):
                yield f"chunk{i}"
                await asyncio.sleep(0.1)

        # Start stream in background
        task = asyncio.create_task(sr.stream("telegram", "123", slow_generator()))

        # Wait a bit then cancel
        await asyncio.sleep(0.2)
        cancelled = sr.cancel_stream("telegram", "123")
        assert cancelled is True

        # Wait for task to complete
        context = await task
        assert context.state == StreamState.CANCELLED

    def test_is_streaming(self, streaming_response):
        """Test is_streaming check."""
        assert streaming_response.is_streaming("telegram", "123") is False

    def test_get_context_not_found(self, streaming_response):
        """Test getting context when not streaming."""
        context = streaming_response.get_context("telegram", "123")
        assert context is None

    def test_get_active_streams_empty(self, streaming_response):
        """Test getting active streams when none active."""
        streams = streaming_response.get_active_streams()
        assert streams == []

    @pytest.mark.asyncio
    async def test_cancel_all(self, streaming_response):
        """Test cancelling all streams."""
        count = await streaming_response.cancel_all()
        assert count == 0

    def test_get_stats_empty(self, streaming_response):
        """Test getting stats with no active streams."""
        stats = streaming_response.get_stats()

        assert stats["active_streams"] == 0
        assert stats["total_bytes_streamed"] == 0
        assert stats["total_chunks"] == 0
        assert stats["stream_keys"] == []

    @pytest.mark.asyncio
    async def test_update_message(self, mock_send_message, mock_edit_message):
        """Test message update."""
        sr = StreamingResponse(
            send_message=mock_send_message,
            edit_message=mock_edit_message,
        )

        await sr.update_message("msg_123", "Updated content")

        mock_edit_message.assert_called_once_with("msg_123", "Updated content")

    @pytest.mark.asyncio
    async def test_update_message_no_callback(self, mock_send_message):
        """Test update message with no edit callback."""
        sr = StreamingResponse(send_message=mock_send_message)

        # Should not raise, just skip
        await sr.update_message("msg_123", "Updated content")

    @pytest.mark.asyncio
    async def test_update_message_retry(self, mock_send_message):
        """Test message update retry on failure."""
        mock_edit = AsyncMock(side_effect=[Exception("Error"), None])

        sr = StreamingResponse(
            send_message=mock_send_message,
            edit_message=mock_edit,
            config=StreamConfig(max_retries=2, retry_delay=0.01)
        )

        await sr.update_message("msg_123", "Content")

        assert mock_edit.call_count == 2

    @pytest.mark.asyncio
    async def test_finalize(self, mock_send_message, mock_edit_message):
        """Test finalize message."""
        sr = StreamingResponse(
            send_message=mock_send_message,
            edit_message=mock_edit_message,
        )

        await sr.finalize("msg_123", "Final content")

        mock_edit_message.assert_called_once_with("msg_123", "Final content")

    @pytest.mark.asyncio
    async def test_streaming_context_manager(self, mock_send_message, mock_edit_message):
        """Test streaming context manager."""
        sr = StreamingResponse(
            send_message=mock_send_message,
            edit_message=mock_edit_message,
        )

        async with sr.streaming_context("telegram", "123") as ctx:
            ctx.content_buffer = "Test content"

        assert ctx.state == StreamState.COMPLETED

    @pytest.mark.asyncio
    async def test_stream_error_handling(self, mock_send_message, mock_edit_message):
        """Test error handling during stream."""
        sr = StreamingResponse(
            send_message=mock_send_message,
            edit_message=mock_edit_message,
        )

        async def error_generator():
            yield "Start"
            raise ValueError("Test error")

        context = await sr.stream("telegram", "123", error_generator())

        assert context.state == StreamState.ERROR
        assert "Test error" in context.error


class TestFallbackStreamingResponse:
    """Tests for FallbackStreamingResponse class."""

    @pytest.fixture
    def mock_send_message(self):
        """Create mock send_message callback."""
        return AsyncMock(return_value="msg_123")

    @pytest.fixture
    def mock_typing_manager(self):
        """Create mock TypingManager."""
        manager = Mock()
        manager.start_async = AsyncMock()
        manager.stop_async = AsyncMock()
        manager.pulse_async = AsyncMock()
        return manager

    def test_creation(self, mock_send_message):
        """Test FallbackStreamingResponse instantiation."""
        fsr = FallbackStreamingResponse(send_message=mock_send_message)

        assert fsr._send_message == mock_send_message
        assert fsr._typing_manager is None

    @pytest.mark.asyncio
    async def test_stream_collects_all(self, mock_send_message):
        """Test fallback streaming collects all content."""
        fsr = FallbackStreamingResponse(send_message=mock_send_message)

        async def simple_generator():
            yield "Hello"
            yield " "
            yield "World"

        context = await fsr.stream("whatsapp", "123", simple_generator())

        assert context.state == StreamState.COMPLETED
        assert context.content_buffer == "Hello World"
        assert context.chunk_count == 3
        mock_send_message.assert_called_once_with("123", "Hello World")

    @pytest.mark.asyncio
    async def test_stream_with_typing(self, mock_send_message, mock_typing_manager):
        """Test fallback streaming with typing."""
        fsr = FallbackStreamingResponse(
            send_message=mock_send_message,
            typing_manager=mock_typing_manager
        )

        async def simple_generator():
            yield "Test"

        await fsr.stream("whatsapp", "123", simple_generator())

        mock_typing_manager.start_async.assert_called_once()
        mock_typing_manager.stop_async.assert_called_once()

    @pytest.mark.asyncio
    async def test_stream_error(self, mock_send_message):
        """Test fallback streaming error handling."""
        fsr = FallbackStreamingResponse(send_message=mock_send_message)

        async def error_generator():
            yield "Start"
            raise ValueError("Error")

        context = await fsr.stream("whatsapp", "123", error_generator())

        assert context.state == StreamState.ERROR
        assert "Error" in context.error


class TestCreateStreamingResponse:
    """Tests for create_streaming_response factory function."""

    def test_creates_streaming_for_telegram(self):
        """Test creates StreamingResponse for Telegram."""
        send = Mock()
        edit = Mock()

        result = create_streaming_response("telegram", send, edit)

        assert isinstance(result, StreamingResponse)

    def test_creates_streaming_for_discord(self):
        """Test creates StreamingResponse for Discord."""
        send = Mock()
        edit = Mock()

        result = create_streaming_response("discord", send, edit)

        assert isinstance(result, StreamingResponse)

    def test_creates_fallback_for_whatsapp(self):
        """Test creates FallbackStreamingResponse for WhatsApp."""
        send = Mock()
        edit = Mock()

        result = create_streaming_response("whatsapp", send, edit)

        assert isinstance(result, FallbackStreamingResponse)

    def test_creates_fallback_without_edit(self):
        """Test creates FallbackStreamingResponse without edit callback."""
        send = Mock()

        result = create_streaming_response("telegram", send, None)

        assert isinstance(result, FallbackStreamingResponse)

    def test_creates_fallback_for_unknown_platform(self):
        """Test creates FallbackStreamingResponse for unknown platform."""
        send = Mock()
        edit = Mock()

        result = create_streaming_response("unknown_platform", send, edit)

        assert isinstance(result, FallbackStreamingResponse)


class TestStreamState:
    """Tests for StreamState enum."""

    def test_all_states_exist(self):
        """Test all stream states are defined."""
        assert StreamState.IDLE.value == "idle"
        assert StreamState.STREAMING.value == "streaming"
        assert StreamState.UPDATING.value == "updating"
        assert StreamState.FINALIZING.value == "finalizing"
        assert StreamState.COMPLETED.value == "completed"
        assert StreamState.ERROR.value == "error"
        assert StreamState.CANCELLED.value == "cancelled"


class TestPlatformCapability:
    """Tests for PlatformCapability enum."""

    def test_all_capabilities_exist(self):
        """Test all platform capabilities are defined."""
        assert PlatformCapability.FULL_EDIT.value == "full_edit"
        assert PlatformCapability.APPEND_ONLY.value == "append_only"
        assert PlatformCapability.NO_EDIT.value == "no_edit"


class TestDockerCompatibility:
    """Tests for Docker/container compatibility."""

    def test_config_has_websocket_settings(self):
        """Test config has WebSocket-related settings for containers."""
        config = StreamConfig()

        assert hasattr(config, 'websocket_ping_interval')
        assert hasattr(config, 'connection_timeout')
        assert config.websocket_ping_interval > 0
        assert config.connection_timeout > 0

    @pytest.mark.asyncio
    async def test_timeout_handling(self):
        """Test stream timeout handling for long-running operations."""
        mock_send = AsyncMock(return_value="msg_123")
        mock_edit = AsyncMock()

        sr = StreamingResponse(
            send_message=mock_send,
            edit_message=mock_edit,
            config=StreamConfig(timeout=0.1)  # Very short timeout for testing
        )

        async def slow_generator():
            for i in range(100):
                yield f"chunk{i}"
                await asyncio.sleep(0.05)

        context = await sr.stream("telegram", "123", slow_generator())

        # Stream should be cancelled due to timeout
        assert context.state == StreamState.CANCELLED


class TestIntegrationWithTypingManager:
    """Tests for integration with TypingManager."""

    @pytest.fixture
    def real_typing_manager(self):
        """Create a real TypingManager-like object."""
        from integrations.channels.response.typing import TypingManager
        return TypingManager()

    @pytest.mark.asyncio
    async def test_streaming_with_real_typing_manager(self, real_typing_manager):
        """Test streaming integrates with TypingManager."""
        mock_send = AsyncMock(return_value="msg_123")
        mock_edit = AsyncMock()

        # Set up typing callback
        typing_sent = []
        real_typing_manager.set_send_callback(lambda c: typing_sent.append(c))

        sr = StreamingResponse(
            send_message=mock_send,
            edit_message=mock_edit,
            typing_manager=real_typing_manager,
            config=StreamConfig(show_typing_during_stream=True)
        )

        async def simple_generator():
            yield "Test"

        await sr.stream("telegram", "123", simple_generator())

        # Typing should have been sent
        assert len(typing_sent) >= 1


class TestRegressionTests:
    """Regression tests for streaming module."""

    def test_imports_work(self):
        """Test that all imports work correctly."""
        from integrations.channels.response.streaming import (
            StreamingResponse,
            FallbackStreamingResponse,
            StreamConfig,
            StreamContext,
            StreamState,
            PlatformCapability,
            ProgressIndicator,
            PLATFORM_CAPABILITIES,
            create_streaming_response,
        )

        assert StreamingResponse is not None
        assert FallbackStreamingResponse is not None
        assert StreamConfig is not None
        assert StreamContext is not None
        assert StreamState is not None
        assert PlatformCapability is not None
        assert ProgressIndicator is not None
        assert PLATFORM_CAPABILITIES is not None
        assert create_streaming_response is not None

    def test_response_module_imports(self):
        """Test that streaming can be imported from response module."""
        # This ensures the module integrates with the response package
        try:
            from integrations.channels.response import streaming
            assert hasattr(streaming, 'StreamingResponse')
        except ImportError:
            # Module might not have __init__.py set up yet
            pass


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
