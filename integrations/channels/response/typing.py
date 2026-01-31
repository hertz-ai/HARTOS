"""
TypingManager - Manages typing indicators for chat channels.

Provides start/stop/pulse methods and a typing() context manager
for indicating that the bot is actively processing/typing a response.
"""

import asyncio
import threading
import time
from contextlib import contextmanager, asynccontextmanager
from typing import Optional, Callable, Any, Union
from dataclasses import dataclass, field
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class TypingState(Enum):
    """States for typing indicator."""
    IDLE = "idle"
    TYPING = "typing"
    PULSING = "pulsing"


@dataclass
class TypingConfig:
    """Configuration for typing indicator behavior."""
    pulse_interval: float = 5.0  # Seconds between pulses
    auto_stop_timeout: float = 30.0  # Auto-stop after this duration
    min_pulse_duration: float = 0.5  # Minimum duration for a pulse
    max_retries: int = 3  # Max retries on failure


@dataclass
class TypingContext:
    """Context information for a typing session."""
    channel_id: str
    user_id: Optional[str] = None
    message_id: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    pulse_count: int = 0
    state: TypingState = TypingState.IDLE


class TypingManager:
    """
    Manages typing indicators for chat channels.

    Supports both synchronous and asynchronous operations,
    with automatic pulse maintenance and timeout handling.
    """

    def __init__(
        self,
        send_typing: Optional[Callable[[str], Any]] = None,
        config: Optional[TypingConfig] = None
    ):
        """
        Initialize the TypingManager.

        Args:
            send_typing: Callback function to send typing indicator to channel.
                        Takes channel_id as argument.
            config: Optional configuration for typing behavior.
        """
        self._send_typing = send_typing
        self._config = config or TypingConfig()
        self._active_contexts: dict[str, TypingContext] = {}
        self._pulse_tasks: dict[str, Union[asyncio.Task, threading.Thread]] = {}
        self._lock = threading.Lock()
        self._async_lock: Optional[asyncio.Lock] = None
        self._stopped_channels: set[str] = set()

    @property
    def config(self) -> TypingConfig:
        """Get the typing configuration."""
        return self._config

    def set_send_callback(self, callback: Callable[[str], Any]) -> None:
        """Set the callback function for sending typing indicators."""
        self._send_typing = callback

    def _get_async_lock(self) -> asyncio.Lock:
        """Get or create the async lock."""
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    def start(self, channel_id: str, user_id: Optional[str] = None,
              message_id: Optional[str] = None) -> TypingContext:
        """
        Start a typing indicator for a channel.

        Args:
            channel_id: The channel to show typing in.
            user_id: Optional user ID for context.
            message_id: Optional message ID being responded to.

        Returns:
            TypingContext for the started session.
        """
        with self._lock:
            # Stop existing if any
            if channel_id in self._active_contexts:
                self._stop_internal(channel_id)

            # Remove from stopped channels if present
            self._stopped_channels.discard(channel_id)

            context = TypingContext(
                channel_id=channel_id,
                user_id=user_id,
                message_id=message_id,
                state=TypingState.TYPING
            )
            self._active_contexts[channel_id] = context

            # Send initial typing indicator
            self._send_indicator(channel_id)

            logger.debug(f"Started typing indicator for channel {channel_id}")
            return context

    def stop(self, channel_id: str) -> bool:
        """
        Stop the typing indicator for a channel.

        Args:
            channel_id: The channel to stop typing in.

        Returns:
            True if a typing session was stopped, False if none was active.
        """
        with self._lock:
            return self._stop_internal(channel_id)

    def _stop_internal(self, channel_id: str) -> bool:
        """Internal stop without lock (must be called with lock held)."""
        if channel_id not in self._active_contexts:
            return False

        context = self._active_contexts.pop(channel_id)
        context.state = TypingState.IDLE

        # Mark as stopped
        self._stopped_channels.add(channel_id)

        # Cancel pulse task if any
        if channel_id in self._pulse_tasks:
            task = self._pulse_tasks.pop(channel_id)
            if isinstance(task, asyncio.Task):
                task.cancel()
            elif isinstance(task, threading.Thread):
                # Thread will check stopped_channels and exit
                pass

        logger.debug(f"Stopped typing indicator for channel {channel_id}")
        return True

    def pulse(self, channel_id: str) -> bool:
        """
        Send a single typing pulse for a channel.

        This refreshes the typing indicator without starting a new session.

        Args:
            channel_id: The channel to pulse typing in.

        Returns:
            True if pulse was sent, False if no active session.
        """
        with self._lock:
            context = self._active_contexts.get(channel_id)
            if context is None:
                return False

            context.pulse_count += 1
            context.state = TypingState.PULSING
            self._send_indicator(channel_id)
            context.state = TypingState.TYPING

            logger.debug(f"Pulsed typing indicator for channel {channel_id} "
                        f"(pulse #{context.pulse_count})")
            return True

    def _send_indicator(self, channel_id: str) -> None:
        """Send the typing indicator via callback."""
        if self._send_typing:
            try:
                self._send_typing(channel_id)
            except Exception as e:
                logger.warning(f"Failed to send typing indicator: {e}")

    def is_typing(self, channel_id: str) -> bool:
        """Check if typing is active for a channel."""
        with self._lock:
            return channel_id in self._active_contexts

    def get_context(self, channel_id: str) -> Optional[TypingContext]:
        """Get the typing context for a channel."""
        with self._lock:
            return self._active_contexts.get(channel_id)

    def get_active_channels(self) -> list[str]:
        """Get list of channels with active typing indicators."""
        with self._lock:
            return list(self._active_contexts.keys())

    def stop_all(self) -> int:
        """
        Stop all active typing indicators.

        Returns:
            Number of indicators stopped.
        """
        with self._lock:
            count = len(self._active_contexts)
            channels = list(self._active_contexts.keys())
            for channel_id in channels:
                self._stop_internal(channel_id)
            return count

    @contextmanager
    def typing(self, channel_id: str, user_id: Optional[str] = None,
               message_id: Optional[str] = None, auto_pulse: bool = False):
        """
        Context manager for typing indicator.

        Automatically starts typing on enter and stops on exit.

        Args:
            channel_id: The channel to show typing in.
            user_id: Optional user ID for context.
            message_id: Optional message ID being responded to.
            auto_pulse: Whether to automatically pulse during the context.

        Yields:
            The TypingContext for the session.

        Example:
            with manager.typing("channel123") as ctx:
                # Do processing...
                pass
            # Typing automatically stopped
        """
        context = self.start(channel_id, user_id, message_id)
        pulse_thread = None

        try:
            if auto_pulse:
                pulse_thread = self._start_pulse_thread(channel_id)
            yield context
        finally:
            if pulse_thread:
                # Thread will stop when channel is removed from active
                pass
            self.stop(channel_id)

    def _start_pulse_thread(self, channel_id: str) -> threading.Thread:
        """Start a background thread for auto-pulsing."""
        def pulse_loop():
            while True:
                time.sleep(self._config.pulse_interval)
                if channel_id in self._stopped_channels:
                    break
                with self._lock:
                    if channel_id not in self._active_contexts:
                        break
                if not self.pulse(channel_id):
                    break

        thread = threading.Thread(target=pulse_loop, daemon=True)
        thread.start()
        with self._lock:
            self._pulse_tasks[channel_id] = thread
        return thread

    # Async variants
    async def start_async(self, channel_id: str, user_id: Optional[str] = None,
                          message_id: Optional[str] = None) -> TypingContext:
        """Async version of start()."""
        async with self._get_async_lock():
            return self.start(channel_id, user_id, message_id)

    async def stop_async(self, channel_id: str) -> bool:
        """Async version of stop()."""
        async with self._get_async_lock():
            return self.stop(channel_id)

    async def pulse_async(self, channel_id: str) -> bool:
        """Async version of pulse()."""
        async with self._get_async_lock():
            return self.pulse(channel_id)

    @asynccontextmanager
    async def typing_async(self, channel_id: str, user_id: Optional[str] = None,
                           message_id: Optional[str] = None, auto_pulse: bool = False):
        """
        Async context manager for typing indicator.

        Args:
            channel_id: The channel to show typing in.
            user_id: Optional user ID for context.
            message_id: Optional message ID being responded to.
            auto_pulse: Whether to automatically pulse during the context.

        Yields:
            The TypingContext for the session.
        """
        context = await self.start_async(channel_id, user_id, message_id)
        pulse_task = None

        try:
            if auto_pulse:
                pulse_task = asyncio.create_task(
                    self._pulse_loop_async(channel_id)
                )
                with self._lock:
                    self._pulse_tasks[channel_id] = pulse_task
            yield context
        finally:
            if pulse_task:
                pulse_task.cancel()
                try:
                    await pulse_task
                except asyncio.CancelledError:
                    pass
            await self.stop_async(channel_id)

    async def _pulse_loop_async(self, channel_id: str) -> None:
        """Async pulse loop for auto-pulsing."""
        try:
            while True:
                await asyncio.sleep(self._config.pulse_interval)
                if not await self.pulse_async(channel_id):
                    break
        except asyncio.CancelledError:
            pass

    def get_typing_duration(self, channel_id: str) -> Optional[float]:
        """Get how long typing has been active for a channel."""
        with self._lock:
            context = self._active_contexts.get(channel_id)
            if context:
                return time.time() - context.started_at
            return None

    def get_stats(self) -> dict:
        """Get statistics about typing indicators."""
        with self._lock:
            total_pulses = sum(
                ctx.pulse_count for ctx in self._active_contexts.values()
            )
            return {
                "active_count": len(self._active_contexts),
                "total_pulses": total_pulses,
                "channels": list(self._active_contexts.keys())
            }
