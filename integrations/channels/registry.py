"""
Channel Registry

Manages all channel adapters and provides unified access.
Handles routing messages to/from the agent system.
"""

import asyncio
import logging
from typing import Dict, Optional, Callable, Any, List
from dataclasses import dataclass, field
from core.port_registry import get_port

from .base import (
    ChannelAdapter,
    ChannelConfig,
    ChannelStatus,
    Message,
    SendResult,
    MediaAttachment,
)

logger = logging.getLogger(__name__)


@dataclass
class ChannelRegistryConfig:
    """Configuration for channel registry."""
    agent_callback_url: str = None
    default_user_id: int = 10077
    default_prompt_id: int = 8888
    enable_create_mode: bool = False

    def __post_init__(self):
        if self.agent_callback_url is None:
            self.agent_callback_url = f"http://localhost:{get_port('backend')}/chat"


class ChannelRegistry:
    """
    Central registry for all messaging channel adapters.

    Provides:
    - Unified message routing
    - Channel lifecycle management
    - Agent integration
    """

    def __init__(self, config: ChannelRegistryConfig = None):
        self.config = config or ChannelRegistryConfig()
        self._adapters: Dict[str, ChannelAdapter] = {}
        self._agent_handler: Optional[Callable] = None
        self._running = False

    def register(self, adapter: ChannelAdapter) -> None:
        """
        Register a channel adapter.

        Args:
            adapter: Channel adapter instance
        """
        if adapter.name in self._adapters:
            logger.warning(f"Replacing existing adapter for {adapter.name}")

        self._adapters[adapter.name] = adapter

        # Set up message routing
        adapter.on_message(self._route_to_agent)

        logger.info(f"Registered channel adapter: {adapter.name}")

    def unregister(self, channel_name: str) -> None:
        """Unregister a channel adapter."""
        if channel_name in self._adapters:
            del self._adapters[channel_name]
            logger.info(f"Unregistered channel adapter: {channel_name}")

    def get(self, channel_name: str) -> Optional[ChannelAdapter]:
        """Get adapter by name."""
        return self._adapters.get(channel_name)

    def list_channels(self) -> List[str]:
        """List all registered channel names."""
        return list(self._adapters.keys())

    def get_status(self) -> Dict[str, ChannelStatus]:
        """Get status of all channels."""
        return {name: adapter.get_status() for name, adapter in self._adapters.items()}

    def set_agent_handler(self, handler: Callable[[Message], str]) -> None:
        """
        Set the agent handler function.

        This function receives messages and returns agent responses.

        Args:
            handler: Async function (Message) -> str (response)
        """
        self._agent_handler = handler

    async def _route_to_agent(self, message: Message) -> None:
        """
        Route incoming message to agent and send response.

        This is the core integration point between channels and the agent system.
        """
        if not self._agent_handler:
            logger.warning("No agent handler set, ignoring message")
            return

        adapter = self._adapters.get(message.channel)
        if not adapter:
            logger.error(f"No adapter found for channel: {message.channel}")
            return

        try:
            # Send typing indicator
            await adapter.send_typing(message.chat_id)

            # Get response from agent
            response = self._agent_handler(message)
            if asyncio.iscoroutine(response):
                response = await response

            if response:
                # Send response back to channel
                await adapter.send_message(
                    chat_id=message.chat_id,
                    text=response,
                    reply_to=message.id,
                )

        except Exception as e:
            logger.error(f"Error routing message to agent: {e}")
            # Optionally send error message to user
            try:
                await adapter.send_message(
                    chat_id=message.chat_id,
                    text="Sorry, I encountered an error processing your message.",
                    reply_to=message.id,
                )
            except:
                pass

    async def send_to_channel(
        self,
        channel: str,
        chat_id: str,
        text: str,
        **kwargs
    ) -> SendResult:
        """
        Send a message to a specific channel.

        Args:
            channel: Channel name (telegram, discord, etc.)
            chat_id: Target chat ID
            text: Message text
            **kwargs: Additional arguments (media, buttons, etc.)
        """
        adapter = self._adapters.get(channel)
        if not adapter:
            return SendResult(success=False, error=f"Unknown channel: {channel}")

        if not adapter.is_running():
            return SendResult(success=False, error=f"Channel {channel} not connected")

        return await adapter.send_message(chat_id, text, **kwargs)

    async def broadcast(
        self,
        text: str,
        channels: Optional[List[str]] = None,
        chat_ids: Optional[Dict[str, str]] = None,
    ) -> Dict[str, SendResult]:
        """
        Broadcast message to multiple channels.

        Args:
            text: Message text
            channels: List of channels to broadcast to (all if None)
            chat_ids: Dict of channel -> chat_id mappings

        Returns:
            Dict of channel -> SendResult
        """
        results = {}
        target_channels = channels or list(self._adapters.keys())

        for channel in target_channels:
            if channel not in self._adapters:
                results[channel] = SendResult(success=False, error="Unknown channel")
                continue

            chat_id = chat_ids.get(channel) if chat_ids else None
            if not chat_id:
                results[channel] = SendResult(success=False, error="No chat_id for channel")
                continue

            results[channel] = await self.send_to_channel(channel, chat_id, text)

        return results

    async def start_all(self) -> None:
        """Start all registered channel adapters."""
        self._running = True

        tasks = [adapter.start() for adapter in self._adapters.values()]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Log status
        for name, adapter in self._adapters.items():
            status = adapter.get_status()
            if status == ChannelStatus.CONNECTED:
                logger.info(f"Channel {name} started successfully")
            else:
                logger.error(f"Channel {name} failed to start: {status}")

    async def stop_all(self) -> None:
        """Stop all channel adapters."""
        self._running = False

        tasks = [adapter.stop() for adapter in self._adapters.values()]
        await asyncio.gather(*tasks, return_exceptions=True)

        logger.info("All channels stopped")

    def is_running(self) -> bool:
        """Check if registry is running."""
        return self._running


# Global registry instance
_registry: Optional[ChannelRegistry] = None


def get_registry() -> ChannelRegistry:
    """Get or create the global channel registry."""
    global _registry
    if _registry is None:
        _registry = ChannelRegistry()
    return _registry


def set_registry(registry: ChannelRegistry) -> None:
    """Set the global channel registry."""
    global _registry
    _registry = registry
