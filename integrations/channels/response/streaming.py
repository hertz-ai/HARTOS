"""
StreamingResponse - Manages streaming LLM responses to chat platforms.

Provides support for streaming responses with message editing for platforms
that support it (Discord, Telegram, Slack), with fallback for those that don't.
Designed to work properly in Docker/containerized environments.
"""

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional, Callable, Any, AsyncGenerator, Dict, List, Union
from dataclasses import dataclass, field
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class StreamState(Enum):
    """States for streaming response."""
    IDLE = "idle"
    STREAMING = "streaming"
    UPDATING = "updating"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"


class PlatformCapability(Enum):
    """Platform message editing capabilities."""
    FULL_EDIT = "full_edit"  # Can edit messages (Discord, Telegram, Slack)
    APPEND_ONLY = "append_only"  # Can only append new messages
    NO_EDIT = "no_edit"  # No edit support, fallback to single message


@dataclass
class StreamConfig:
    """Configuration for streaming behavior."""
    update_interval: float = 0.5  # Seconds between updates
    min_chunk_size: int = 10  # Minimum characters before update
    max_chunk_size: int = 500  # Maximum characters per update
    buffer_size: int = 4096  # Buffer size for stream reading
    show_typing_during_stream: bool = True
    progress_indicator: str = "..."  # Appended during streaming
    error_indicator: str = "[Error]"
    timeout: float = 300.0  # Max stream duration in seconds
    max_retries: int = 3  # Retries on update failure
    retry_delay: float = 1.0  # Delay between retries
    # Docker/container-specific settings
    websocket_ping_interval: float = 20.0  # Keep-alive for WebSocket
    connection_timeout: float = 30.0  # Connection establishment timeout


@dataclass
class StreamContext:
    """Context information for a streaming session."""
    channel: str
    chat_id: str
    message_id: Optional[str] = None
    initial_message_id: Optional[str] = None  # Original user message
    content_buffer: str = ""
    chunk_count: int = 0
    bytes_streamed: int = 0
    started_at: float = field(default_factory=time.time)
    last_update_at: float = field(default_factory=time.time)
    state: StreamState = StreamState.IDLE
    error: Optional[str] = None
    platform_capability: PlatformCapability = PlatformCapability.FULL_EDIT
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProgressIndicator:
    """Progress indicator configuration."""
    enabled: bool = True
    style: str = "dots"  # dots, spinner, percentage, none
    frames: List[str] = field(default_factory=lambda: [".", "..", "..."])
    current_frame: int = 0

    def next_frame(self) -> str:
        """Get the next animation frame."""
        if not self.enabled or self.style == "none":
            return ""
        frame = self.frames[self.current_frame]
        self.current_frame = (self.current_frame + 1) % len(self.frames)
        return frame


# Platform capability mappings
PLATFORM_CAPABILITIES: Dict[str, PlatformCapability] = {
    "discord": PlatformCapability.FULL_EDIT,
    "telegram": PlatformCapability.FULL_EDIT,
    "slack": PlatformCapability.FULL_EDIT,
    "whatsapp": PlatformCapability.NO_EDIT,
    "sms": PlatformCapability.NO_EDIT,
    "matrix": PlatformCapability.FULL_EDIT,
    "teams": PlatformCapability.FULL_EDIT,
    "line": PlatformCapability.NO_EDIT,
    "web": PlatformCapability.FULL_EDIT,
    "api": PlatformCapability.APPEND_ONLY,
}


class StreamingResponse:
    """
    Manages streaming LLM responses to chat platforms.

    Supports:
    - Streaming with message editing for supported platforms
    - Chunked updates with configurable interval
    - Fallback for platforms without edit support
    - Progress indicators during streaming
    - Error handling for stream interruptions
    - Integration with TypingManager for typing indicators
    - Docker/container-friendly WebSocket handling
    """

    def __init__(
        self,
        send_message: Optional[Callable[[str, str], Any]] = None,
        edit_message: Optional[Callable[[str, str, str], Any]] = None,
        typing_manager: Optional[Any] = None,
        config: Optional[StreamConfig] = None
    ):
        """
        Initialize the StreamingResponse manager.

        Args:
            send_message: Callback to send message. Takes (chat_id, text) -> message_id.
            edit_message: Callback to edit message. Takes (chat_id, message_id, text).
            typing_manager: Optional TypingManager instance for showing typing.
            config: Optional configuration for streaming behavior.
        """
        self._send_message = send_message
        self._edit_message = edit_message
        self._typing_manager = typing_manager
        self._config = config or StreamConfig()
        self._active_streams: Dict[str, StreamContext] = {}
        self._lock = threading.Lock()
        self._async_lock: Optional[asyncio.Lock] = None
        self._cancelled_streams: set = set()

    @property
    def config(self) -> StreamConfig:
        """Get the streaming configuration."""
        return self._config

    def set_callbacks(
        self,
        send_message: Callable[[str, str], Any],
        edit_message: Optional[Callable[[str, str, str], Any]] = None
    ) -> None:
        """Set the callback functions for sending/editing messages."""
        self._send_message = send_message
        self._edit_message = edit_message

    def set_typing_manager(self, typing_manager: Any) -> None:
        """Set the typing manager for showing typing during stream."""
        self._typing_manager = typing_manager

    def _get_async_lock(self) -> asyncio.Lock:
        """Get or create the async lock."""
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    def _get_platform_capability(self, channel: str) -> PlatformCapability:
        """Get the capability for a platform."""
        return PLATFORM_CAPABILITIES.get(
            channel.lower(),
            PlatformCapability.NO_EDIT
        )

    def _create_stream_key(self, channel: str, chat_id: str) -> str:
        """Create a unique key for a stream context."""
        return f"{channel}:{chat_id}"

    async def stream(
        self,
        channel: str,
        chat_id: str,
        generator: AsyncGenerator[str, None],
        initial_message_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> StreamContext:
        """
        Stream content from an async generator to a chat.

        Args:
            channel: The channel name (telegram, discord, etc.).
            chat_id: The chat/conversation ID.
            generator: Async generator yielding content chunks.
            initial_message_id: Optional message ID being responded to.
            metadata: Optional metadata to attach to context.

        Returns:
            StreamContext with final state and content.
        """
        stream_key = self._create_stream_key(channel, chat_id)

        # Cancel any existing stream for this chat
        await self._cancel_existing_stream(stream_key)

        # Create stream context
        context = StreamContext(
            channel=channel,
            chat_id=chat_id,
            initial_message_id=initial_message_id,
            state=StreamState.STREAMING,
            platform_capability=self._get_platform_capability(channel),
            metadata=metadata or {}
        )

        async with self._get_async_lock():
            self._active_streams[stream_key] = context
            self._cancelled_streams.discard(stream_key)

        try:
            # Start typing indicator if available
            if self._typing_manager and self._config.show_typing_during_stream:
                await self._start_typing(channel, chat_id)

            # Send initial placeholder message for platforms with edit support
            if context.platform_capability == PlatformCapability.FULL_EDIT:
                context.message_id = await self._send_initial_message(
                    chat_id,
                    self._config.progress_indicator
                )

            # Process the stream
            await self._process_stream(context, generator, stream_key)

            # Finalize
            if context.state != StreamState.CANCELLED:
                await self.finalize(context.message_id, context.content_buffer)
                context.state = StreamState.COMPLETED

        except asyncio.CancelledError:
            context.state = StreamState.CANCELLED
            logger.info(f"Stream cancelled for {stream_key}")
        except Exception as e:
            context.state = StreamState.ERROR
            context.error = str(e)
            logger.error(f"Stream error for {stream_key}: {e}")
            await self._handle_stream_error(context, e)
        finally:
            # Stop typing indicator
            if self._typing_manager and self._config.show_typing_during_stream:
                await self._stop_typing(channel, chat_id)

            # Clean up
            async with self._get_async_lock():
                self._active_streams.pop(stream_key, None)

        return context

    async def _process_stream(
        self,
        context: StreamContext,
        generator: AsyncGenerator[str, None],
        stream_key: str
    ) -> None:
        """Process chunks from the generator."""
        last_update_time = time.time()
        pending_content = ""
        progress = ProgressIndicator()
        timeout_task = asyncio.create_task(
            self._stream_timeout_watcher(stream_key, self._config.timeout)
        )

        try:
            async for chunk in generator:
                # Check for cancellation
                if stream_key in self._cancelled_streams:
                    context.state = StreamState.CANCELLED
                    break

                # Accumulate content
                context.content_buffer += chunk
                pending_content += chunk
                context.chunk_count += 1
                context.bytes_streamed += len(chunk.encode('utf-8'))

                # Check if we should update
                current_time = time.time()
                time_elapsed = current_time - last_update_time
                content_size = len(pending_content)

                should_update = (
                    time_elapsed >= self._config.update_interval and
                    content_size >= self._config.min_chunk_size
                ) or content_size >= self._config.max_chunk_size

                if should_update and context.message_id:
                    context.state = StreamState.UPDATING

                    # Add progress indicator
                    display_content = context.content_buffer
                    if progress.enabled:
                        display_content += progress.next_frame()

                    await self.update_message(
                        context.message_id,
                        display_content
                    )

                    last_update_time = current_time
                    pending_content = ""
                    context.last_update_at = current_time
                    context.state = StreamState.STREAMING

                    # Pulse typing if needed
                    if self._typing_manager:
                        await self._pulse_typing(context.channel, context.chat_id)

        finally:
            timeout_task.cancel()
            try:
                await timeout_task
            except asyncio.CancelledError:
                pass

    async def _stream_timeout_watcher(
        self,
        stream_key: str,
        timeout: float
    ) -> None:
        """Watch for stream timeout and cancel if exceeded."""
        await asyncio.sleep(timeout)
        logger.warning(f"Stream timeout reached for {stream_key}")
        self._cancelled_streams.add(stream_key)

    async def _cancel_existing_stream(self, stream_key: str) -> None:
        """Cancel an existing stream for the same chat."""
        async with self._get_async_lock():
            if stream_key in self._active_streams:
                self._cancelled_streams.add(stream_key)
                logger.info(f"Cancelled existing stream for {stream_key}")

    async def update_message(
        self,
        message_id: str,
        content: str
    ) -> None:
        """
        Update an existing message with new content.

        Args:
            message_id: The message ID to update.
            content: The new content to set.
        """
        if not self._edit_message:
            logger.debug("No edit_message callback set, skipping update")
            return

        retries = 0
        while retries < self._config.max_retries:
            try:
                result = self._edit_message(message_id, content)
                if asyncio.iscoroutine(result):
                    await result
                return
            except Exception as e:
                retries += 1
                if retries >= self._config.max_retries:
                    logger.error(f"Failed to update message after {retries} retries: {e}")
                    raise
                logger.warning(f"Update retry {retries}/{self._config.max_retries}: {e}")
                await asyncio.sleep(self._config.retry_delay)

    async def finalize(
        self,
        message_id: Optional[str],
        final_content: str
    ) -> None:
        """
        Finalize a streaming message with the complete content.

        Args:
            message_id: The message ID to finalize (or None for new message).
            final_content: The final complete content.
        """
        if message_id and self._edit_message:
            # Update existing message with final content (no progress indicator)
            await self.update_message(message_id, final_content)
        elif not message_id and self._send_message:
            # Send as new message if no message_id
            await self._send_initial_message(None, final_content)

    async def _send_initial_message(
        self,
        chat_id: Optional[str],
        content: str
    ) -> Optional[str]:
        """Send the initial placeholder message."""
        if not self._send_message:
            return None

        try:
            result = self._send_message(chat_id, content)
            if asyncio.iscoroutine(result):
                result = await result
            # Expect result to be message_id or dict with message_id
            if isinstance(result, str):
                return result
            elif isinstance(result, dict):
                return result.get('message_id') or result.get('id')
            return str(result) if result else None
        except Exception as e:
            logger.error(f"Failed to send initial message: {e}")
            return None

    async def _handle_stream_error(
        self,
        context: StreamContext,
        error: Exception
    ) -> None:
        """Handle stream errors by sending error indicator."""
        error_content = context.content_buffer
        if error_content:
            error_content += f"\n\n{self._config.error_indicator}"
        else:
            error_content = f"{self._config.error_indicator} {str(error)}"

        if context.message_id:
            try:
                await self.update_message(context.message_id, error_content)
            except Exception as e:
                logger.error(f"Failed to send error indicator: {e}")

    async def _start_typing(self, channel: str, chat_id: str) -> None:
        """Start typing indicator via TypingManager."""
        if self._typing_manager:
            try:
                if hasattr(self._typing_manager, 'start_async'):
                    await self._typing_manager.start_async(chat_id)
                else:
                    self._typing_manager.start(chat_id)
            except Exception as e:
                logger.debug(f"Failed to start typing: {e}")

    async def _stop_typing(self, channel: str, chat_id: str) -> None:
        """Stop typing indicator via TypingManager."""
        if self._typing_manager:
            try:
                if hasattr(self._typing_manager, 'stop_async'):
                    await self._typing_manager.stop_async(chat_id)
                else:
                    self._typing_manager.stop(chat_id)
            except Exception as e:
                logger.debug(f"Failed to stop typing: {e}")

    async def _pulse_typing(self, channel: str, chat_id: str) -> None:
        """Pulse typing indicator to keep it active."""
        if self._typing_manager:
            try:
                if hasattr(self._typing_manager, 'pulse_async'):
                    await self._typing_manager.pulse_async(chat_id)
                else:
                    self._typing_manager.pulse(chat_id)
            except Exception as e:
                logger.debug(f"Failed to pulse typing: {e}")

    def cancel_stream(self, channel: str, chat_id: str) -> bool:
        """
        Cancel an active stream.

        Args:
            channel: The channel name.
            chat_id: The chat ID.

        Returns:
            True if a stream was cancelled, False if none was active.
        """
        stream_key = self._create_stream_key(channel, chat_id)
        with self._lock:
            if stream_key in self._active_streams:
                self._cancelled_streams.add(stream_key)
                return True
            return False

    def is_streaming(self, channel: str, chat_id: str) -> bool:
        """Check if streaming is active for a chat."""
        stream_key = self._create_stream_key(channel, chat_id)
        with self._lock:
            return stream_key in self._active_streams

    def get_context(self, channel: str, chat_id: str) -> Optional[StreamContext]:
        """Get the streaming context for a chat."""
        stream_key = self._create_stream_key(channel, chat_id)
        with self._lock:
            return self._active_streams.get(stream_key)

    def get_active_streams(self) -> List[str]:
        """Get list of active stream keys."""
        with self._lock:
            return list(self._active_streams.keys())

    async def cancel_all(self) -> int:
        """
        Cancel all active streams.

        Returns:
            Number of streams cancelled.
        """
        async with self._get_async_lock():
            count = len(self._active_streams)
            for stream_key in self._active_streams.keys():
                self._cancelled_streams.add(stream_key)
            return count

    @asynccontextmanager
    async def streaming_context(
        self,
        channel: str,
        chat_id: str,
        initial_message_id: Optional[str] = None
    ):
        """
        Async context manager for streaming sessions.

        Usage:
            async with streaming.streaming_context("telegram", "123") as ctx:
                async for chunk in llm_response:
                    ctx.content_buffer += chunk
                    await streaming.update_message(ctx.message_id, ctx.content_buffer)

        Args:
            channel: The channel name.
            chat_id: The chat ID.
            initial_message_id: Optional message being responded to.

        Yields:
            StreamContext for the session.
        """
        stream_key = self._create_stream_key(channel, chat_id)

        context = StreamContext(
            channel=channel,
            chat_id=chat_id,
            initial_message_id=initial_message_id,
            state=StreamState.STREAMING,
            platform_capability=self._get_platform_capability(channel)
        )

        async with self._get_async_lock():
            self._active_streams[stream_key] = context

        try:
            # Start typing
            if self._typing_manager and self._config.show_typing_during_stream:
                await self._start_typing(channel, chat_id)

            # Send initial message for platforms with edit support
            if context.platform_capability == PlatformCapability.FULL_EDIT:
                context.message_id = await self._send_initial_message(
                    chat_id,
                    self._config.progress_indicator
                )

            yield context

            # Finalize
            if context.state != StreamState.CANCELLED and context.content_buffer:
                await self.finalize(context.message_id, context.content_buffer)
                context.state = StreamState.COMPLETED

        except Exception as e:
            context.state = StreamState.ERROR
            context.error = str(e)
            await self._handle_stream_error(context, e)
            raise
        finally:
            # Stop typing
            if self._typing_manager and self._config.show_typing_during_stream:
                await self._stop_typing(channel, chat_id)

            # Clean up
            async with self._get_async_lock():
                self._active_streams.pop(stream_key, None)

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about streaming responses."""
        with self._lock:
            total_bytes = sum(
                ctx.bytes_streamed for ctx in self._active_streams.values()
            )
            total_chunks = sum(
                ctx.chunk_count for ctx in self._active_streams.values()
            )
            return {
                "active_streams": len(self._active_streams),
                "total_bytes_streamed": total_bytes,
                "total_chunks": total_chunks,
                "stream_keys": list(self._active_streams.keys())
            }


class FallbackStreamingResponse:
    """
    Fallback streaming handler for platforms without message editing.

    Collects all content and sends as a single message at the end.
    """

    def __init__(
        self,
        send_message: Callable[[str, str], Any],
        typing_manager: Optional[Any] = None,
        config: Optional[StreamConfig] = None
    ):
        """
        Initialize the fallback streaming handler.

        Args:
            send_message: Callback to send message. Takes (chat_id, text).
            typing_manager: Optional TypingManager instance.
            config: Optional configuration.
        """
        self._send_message = send_message
        self._typing_manager = typing_manager
        self._config = config or StreamConfig()

    async def stream(
        self,
        channel: str,
        chat_id: str,
        generator: AsyncGenerator[str, None],
        initial_message_id: Optional[str] = None
    ) -> StreamContext:
        """
        Collect stream content and send as single message.

        Args:
            channel: The channel name.
            chat_id: The chat ID.
            generator: Async generator yielding content chunks.
            initial_message_id: Optional message being responded to.

        Returns:
            StreamContext with collected content.
        """
        context = StreamContext(
            channel=channel,
            chat_id=chat_id,
            initial_message_id=initial_message_id,
            state=StreamState.STREAMING,
            platform_capability=PlatformCapability.NO_EDIT
        )

        try:
            # Start typing
            if self._typing_manager:
                if hasattr(self._typing_manager, 'start_async'):
                    await self._typing_manager.start_async(chat_id)
                else:
                    self._typing_manager.start(chat_id)

            # Collect all content
            async for chunk in generator:
                context.content_buffer += chunk
                context.chunk_count += 1
                context.bytes_streamed += len(chunk.encode('utf-8'))

                # Pulse typing periodically
                if self._typing_manager and context.chunk_count % 10 == 0:
                    if hasattr(self._typing_manager, 'pulse_async'):
                        await self._typing_manager.pulse_async(chat_id)
                    else:
                        self._typing_manager.pulse(chat_id)

            # Send final message
            if context.content_buffer:
                result = self._send_message(chat_id, context.content_buffer)
                if asyncio.iscoroutine(result):
                    result = await result
                if isinstance(result, str):
                    context.message_id = result
                elif isinstance(result, dict):
                    context.message_id = result.get('message_id') or result.get('id')

            context.state = StreamState.COMPLETED

        except Exception as e:
            context.state = StreamState.ERROR
            context.error = str(e)
            logger.error(f"Fallback stream error: {e}")
        finally:
            # Stop typing
            if self._typing_manager:
                if hasattr(self._typing_manager, 'stop_async'):
                    await self._typing_manager.stop_async(chat_id)
                else:
                    self._typing_manager.stop(chat_id)

        return context


def create_streaming_response(
    channel: str,
    send_message: Callable[[str, str], Any],
    edit_message: Optional[Callable[[str, str, str], Any]] = None,
    typing_manager: Optional[Any] = None,
    config: Optional[StreamConfig] = None
) -> Union[StreamingResponse, FallbackStreamingResponse]:
    """
    Factory function to create appropriate streaming response handler.

    Args:
        channel: The channel name.
        send_message: Callback to send message.
        edit_message: Optional callback to edit message.
        typing_manager: Optional TypingManager instance.
        config: Optional configuration.

    Returns:
        StreamingResponse or FallbackStreamingResponse based on platform capability.
    """
    capability = PLATFORM_CAPABILITIES.get(
        channel.lower(),
        PlatformCapability.NO_EDIT
    )

    if capability == PlatformCapability.FULL_EDIT and edit_message:
        return StreamingResponse(
            send_message=send_message,
            edit_message=edit_message,
            typing_manager=typing_manager,
            config=config
        )
    else:
        return FallbackStreamingResponse(
            send_message=send_message,
            typing_manager=typing_manager,
            config=config
        )
