"""
AckManager - Manages acknowledgment reactions for messages.

Provides emoji reactions to acknowledge message receipt, processing status,
completion, and errors.
"""

import asyncio
import threading
import time
from typing import Optional, Callable, Any, List, Set
from dataclasses import dataclass, field
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class AckState(Enum):
    """Acknowledgment states."""
    NONE = "none"
    RECEIVED = "received"
    PROCESSING = "processing"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass
class AckConfig:
    """Configuration for acknowledgment reactions."""
    received_emoji: str = "\u2705"  # Check mark
    processing_emoji: str = "\u23f3"  # Hourglass
    complete_emoji: str = "\u2714\ufe0f"  # Heavy check mark
    error_emoji: str = "\u274c"  # Cross mark
    thinking_emoji: str = "\U0001f914"  # Thinking face
    queued_emoji: str = "\U0001f4cb"  # Clipboard

    auto_remove_on_complete: bool = True
    auto_remove_delay: float = 2.0  # Seconds to wait before auto-remove
    remove_previous_on_transition: bool = True


@dataclass
class AckContext:
    """Context for an acknowledgment session."""
    message_id: str
    channel_id: str
    current_state: AckState = AckState.NONE
    reactions_added: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)


class AckManager:
    """
    Manages acknowledgment reactions for messages.

    Provides methods to add/remove emoji reactions indicating
    message processing status.
    """

    def __init__(
        self,
        add_reaction: Optional[Callable[[str, str, str], Any]] = None,
        remove_reaction: Optional[Callable[[str, str, str], Any]] = None,
        config: Optional[AckConfig] = None
    ):
        """
        Initialize the AckManager.

        Args:
            add_reaction: Callback to add reaction. Takes (channel_id, message_id, emoji).
            remove_reaction: Callback to remove reaction. Takes (channel_id, message_id, emoji).
            config: Optional configuration for ack behavior.
        """
        self._add_reaction = add_reaction
        self._remove_reaction = remove_reaction
        self._config = config or AckConfig()
        self._contexts: dict[str, AckContext] = {}  # keyed by message_id
        self._lock = threading.Lock()
        self._async_lock: Optional[asyncio.Lock] = None

    @property
    def config(self) -> AckConfig:
        """Get the ack configuration."""
        return self._config

    @property
    def received_emoji(self) -> str:
        """Get the received emoji."""
        return self._config.received_emoji

    @property
    def processing_emoji(self) -> str:
        """Get the processing emoji."""
        return self._config.processing_emoji

    @property
    def complete_emoji(self) -> str:
        """Get the complete emoji."""
        return self._config.complete_emoji

    @property
    def error_emoji(self) -> str:
        """Get the error emoji."""
        return self._config.error_emoji

    def set_callbacks(
        self,
        add_reaction: Callable[[str, str, str], Any],
        remove_reaction: Callable[[str, str, str], Any]
    ) -> None:
        """Set the callback functions for adding/removing reactions."""
        self._add_reaction = add_reaction
        self._remove_reaction = remove_reaction

    def set_emojis(
        self,
        received: Optional[str] = None,
        processing: Optional[str] = None,
        complete: Optional[str] = None,
        error: Optional[str] = None
    ) -> None:
        """Update emoji configuration."""
        if received is not None:
            self._config.received_emoji = received
        if processing is not None:
            self._config.processing_emoji = processing
        if complete is not None:
            self._config.complete_emoji = complete
        if error is not None:
            self._config.error_emoji = error

    def _get_async_lock(self) -> asyncio.Lock:
        """Get or create the async lock."""
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    def _get_or_create_context(self, message_id: str, channel_id: str) -> AckContext:
        """Get existing context or create new one."""
        if message_id not in self._contexts:
            self._contexts[message_id] = AckContext(
                message_id=message_id,
                channel_id=channel_id
            )
        return self._contexts[message_id]

    def _add_reaction_internal(self, channel_id: str, message_id: str, emoji: str) -> bool:
        """Internal method to add a reaction."""
        if self._add_reaction:
            try:
                self._add_reaction(channel_id, message_id, emoji)
                return True
            except Exception as e:
                logger.warning(f"Failed to add reaction {emoji}: {e}")
                return False
        return False

    def _remove_reaction_internal(self, channel_id: str, message_id: str, emoji: str) -> bool:
        """Internal method to remove a reaction."""
        if self._remove_reaction:
            try:
                self._remove_reaction(channel_id, message_id, emoji)
                return True
            except Exception as e:
                logger.warning(f"Failed to remove reaction {emoji}: {e}")
                return False
        return False

    def ack_received(self, channel_id: str, message_id: str) -> AckContext:
        """
        Acknowledge that a message was received.

        Args:
            channel_id: The channel containing the message.
            message_id: The message to acknowledge.

        Returns:
            AckContext for the acknowledgment session.
        """
        with self._lock:
            context = self._get_or_create_context(message_id, channel_id)

            # Remove previous reactions if configured
            if self._config.remove_previous_on_transition:
                self._remove_all_reactions(context)

            # Add received emoji
            emoji = self._config.received_emoji
            if self._add_reaction_internal(channel_id, message_id, emoji):
                context.reactions_added.append(emoji)

            context.current_state = AckState.RECEIVED
            context.last_updated = time.time()

            logger.debug(f"Acked received for message {message_id}")
            return context

    def ack_processing(self, channel_id: str, message_id: str) -> AckContext:
        """
        Acknowledge that a message is being processed.

        Args:
            channel_id: The channel containing the message.
            message_id: The message being processed.

        Returns:
            AckContext for the acknowledgment session.
        """
        with self._lock:
            context = self._get_or_create_context(message_id, channel_id)

            # Remove previous reactions if configured
            if self._config.remove_previous_on_transition:
                self._remove_all_reactions(context)

            # Add processing emoji
            emoji = self._config.processing_emoji
            if self._add_reaction_internal(channel_id, message_id, emoji):
                context.reactions_added.append(emoji)

            context.current_state = AckState.PROCESSING
            context.last_updated = time.time()

            logger.debug(f"Acked processing for message {message_id}")
            return context

    def ack_complete(self, channel_id: str, message_id: str,
                     auto_remove: Optional[bool] = None) -> AckContext:
        """
        Acknowledge that processing is complete.

        Args:
            channel_id: The channel containing the message.
            message_id: The completed message.
            auto_remove: Override auto-remove setting for this call.

        Returns:
            AckContext for the acknowledgment session.
        """
        with self._lock:
            context = self._get_or_create_context(message_id, channel_id)

            # Remove previous reactions if configured
            if self._config.remove_previous_on_transition:
                self._remove_all_reactions(context)

            # Add complete emoji
            emoji = self._config.complete_emoji
            if self._add_reaction_internal(channel_id, message_id, emoji):
                context.reactions_added.append(emoji)

            context.current_state = AckState.COMPLETE
            context.last_updated = time.time()

            # Auto-remove if configured
            should_remove = auto_remove if auto_remove is not None else self._config.auto_remove_on_complete
            if should_remove:
                self._schedule_removal(context)

            logger.debug(f"Acked complete for message {message_id}")
            return context

    def ack_error(self, channel_id: str, message_id: str) -> AckContext:
        """
        Acknowledge that an error occurred.

        Args:
            channel_id: The channel containing the message.
            message_id: The message that errored.

        Returns:
            AckContext for the acknowledgment session.
        """
        with self._lock:
            context = self._get_or_create_context(message_id, channel_id)

            # Remove previous reactions if configured
            if self._config.remove_previous_on_transition:
                self._remove_all_reactions(context)

            # Add error emoji
            emoji = self._config.error_emoji
            if self._add_reaction_internal(channel_id, message_id, emoji):
                context.reactions_added.append(emoji)

            context.current_state = AckState.ERROR
            context.last_updated = time.time()

            logger.debug(f"Acked error for message {message_id}")
            return context

    def ack_queued(self, channel_id: str, message_id: str) -> AckContext:
        """
        Acknowledge that a message is queued for processing.

        Args:
            channel_id: The channel containing the message.
            message_id: The queued message.

        Returns:
            AckContext for the acknowledgment session.
        """
        with self._lock:
            context = self._get_or_create_context(message_id, channel_id)

            # Add queued emoji
            emoji = self._config.queued_emoji
            if self._add_reaction_internal(channel_id, message_id, emoji):
                context.reactions_added.append(emoji)

            context.last_updated = time.time()

            logger.debug(f"Acked queued for message {message_id}")
            return context

    def ack_thinking(self, channel_id: str, message_id: str) -> AckContext:
        """
        Show thinking indicator on a message.

        Args:
            channel_id: The channel containing the message.
            message_id: The message being thought about.

        Returns:
            AckContext for the acknowledgment session.
        """
        with self._lock:
            context = self._get_or_create_context(message_id, channel_id)

            # Add thinking emoji
            emoji = self._config.thinking_emoji
            if self._add_reaction_internal(channel_id, message_id, emoji):
                context.reactions_added.append(emoji)

            context.last_updated = time.time()

            logger.debug(f"Acked thinking for message {message_id}")
            return context

    def remove_acks(self, channel_id: str, message_id: str) -> int:
        """
        Remove all acknowledgment reactions from a message.

        Args:
            channel_id: The channel containing the message.
            message_id: The message to clear reactions from.

        Returns:
            Number of reactions removed.
        """
        with self._lock:
            context = self._contexts.get(message_id)
            if not context:
                return 0

            count = self._remove_all_reactions(context)
            context.current_state = AckState.NONE

            # Clean up context
            del self._contexts[message_id]

            logger.debug(f"Removed {count} acks from message {message_id}")
            return count

    def _remove_all_reactions(self, context: AckContext) -> int:
        """Remove all reactions from a context."""
        count = 0
        for emoji in list(context.reactions_added):
            if self._remove_reaction_internal(context.channel_id, context.message_id, emoji):
                count += 1
        context.reactions_added.clear()
        return count

    def _schedule_removal(self, context: AckContext) -> None:
        """Schedule automatic removal of reactions."""
        def remove_after_delay():
            time.sleep(self._config.auto_remove_delay)
            self.remove_acks(context.channel_id, context.message_id)

        thread = threading.Thread(target=remove_after_delay, daemon=True)
        thread.start()

    def get_state(self, message_id: str) -> AckState:
        """Get the current acknowledgment state for a message."""
        with self._lock:
            context = self._contexts.get(message_id)
            return context.current_state if context else AckState.NONE

    def get_context(self, message_id: str) -> Optional[AckContext]:
        """Get the acknowledgment context for a message."""
        with self._lock:
            return self._contexts.get(message_id)

    def get_active_messages(self) -> List[str]:
        """Get list of messages with active acknowledgments."""
        with self._lock:
            return list(self._contexts.keys())

    def clear_all(self) -> int:
        """
        Clear all acknowledgments.

        Returns:
            Number of messages cleared.
        """
        with self._lock:
            count = len(self._contexts)
            for context in list(self._contexts.values()):
                self._remove_all_reactions(context)
            self._contexts.clear()
            return count

    # Async variants
    async def ack_received_async(self, channel_id: str, message_id: str) -> AckContext:
        """Async version of ack_received()."""
        async with self._get_async_lock():
            return self.ack_received(channel_id, message_id)

    async def ack_processing_async(self, channel_id: str, message_id: str) -> AckContext:
        """Async version of ack_processing()."""
        async with self._get_async_lock():
            return self.ack_processing(channel_id, message_id)

    async def ack_complete_async(self, channel_id: str, message_id: str,
                                  auto_remove: Optional[bool] = None) -> AckContext:
        """Async version of ack_complete()."""
        async with self._get_async_lock():
            return self.ack_complete(channel_id, message_id, auto_remove)

    async def ack_error_async(self, channel_id: str, message_id: str) -> AckContext:
        """Async version of ack_error()."""
        async with self._get_async_lock():
            return self.ack_error(channel_id, message_id)

    async def remove_acks_async(self, channel_id: str, message_id: str) -> int:
        """Async version of remove_acks()."""
        async with self._get_async_lock():
            return self.remove_acks(channel_id, message_id)

    def transition_state(self, channel_id: str, message_id: str,
                         new_state: AckState) -> AckContext:
        """
        Transition a message to a new acknowledgment state.

        Args:
            channel_id: The channel containing the message.
            message_id: The message to transition.
            new_state: The new state to transition to.

        Returns:
            Updated AckContext.
        """
        state_methods = {
            AckState.RECEIVED: self.ack_received,
            AckState.PROCESSING: self.ack_processing,
            AckState.COMPLETE: self.ack_complete,
            AckState.ERROR: self.ack_error,
            AckState.NONE: lambda c, m: self.remove_acks(c, m) or self._get_or_create_context(m, c)
        }

        method = state_methods.get(new_state)
        if method:
            return method(channel_id, message_id)
        raise ValueError(f"Unknown state: {new_state}")

    def get_stats(self) -> dict:
        """Get statistics about acknowledgments."""
        with self._lock:
            state_counts = {}
            for context in self._contexts.values():
                state = context.current_state.value
                state_counts[state] = state_counts.get(state, 0) + 1

            return {
                "total_tracked": len(self._contexts),
                "state_counts": state_counts,
                "messages": list(self._contexts.keys())
            }
