"""
Base Channel Adapter Interface

Defines the contract for all messaging channel adapters.
Ported from HevolveBot's ChannelMessagingAdapter pattern.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Optional, List, Dict, Any, Union
import asyncio
import logging

logger = logging.getLogger(__name__)


class MessageType(Enum):
    """Type of message content."""
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    LOCATION = "location"
    CONTACT = "contact"
    STICKER = "sticker"
    VOICE = "voice"


class ChannelStatus(Enum):
    """Channel connection status."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    RATE_LIMITED = "rate_limited"


@dataclass
class MediaAttachment:
    """Media attachment in a message."""
    type: MessageType
    url: Optional[str] = None
    file_path: Optional[str] = None
    file_id: Optional[str] = None  # Platform-specific file ID
    mime_type: Optional[str] = None
    file_name: Optional[str] = None
    file_size: Optional[int] = None
    caption: Optional[str] = None


@dataclass
class Message:
    """Unified message format across all channels."""
    id: str
    channel: str  # telegram, discord, slack, etc.
    sender_id: str
    sender_name: Optional[str] = None
    chat_id: str = ""  # Group/channel ID or same as sender for DMs
    text: Optional[str] = None
    media: List[MediaAttachment] = field(default_factory=list)
    reply_to_id: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
    is_group: bool = False
    is_bot_mentioned: bool = False
    raw: Optional[Dict[str, Any]] = None  # Original platform message

    @property
    def has_media(self) -> bool:
        return len(self.media) > 0

    @property
    def content(self) -> str:
        """Get text content or media caption."""
        if self.text:
            return self.text
        for m in self.media:
            if m.caption:
                return m.caption
        return ""


@dataclass
class SendResult:
    """Result of sending a message."""
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None


@dataclass
class ChannelConfig:
    """Configuration for a channel adapter."""
    enabled: bool = True
    token: Optional[str] = None
    webhook_url: Optional[str] = None
    dm_policy: str = "pairing"  # pairing, open, closed
    allow_from: List[str] = field(default_factory=list)
    require_mention_in_groups: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)


class ChannelAdapter(ABC):
    """
    Base class for all channel adapters.

    Implements the adapter pattern for unified messaging across platforms.
    Each platform (Telegram, Discord, etc.) extends this class.
    """

    def __init__(self, config: ChannelConfig):
        self.config = config
        self.status = ChannelStatus.DISCONNECTED
        self._message_handlers: List[Callable] = []
        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    @abstractmethod
    def name(self) -> str:
        """Channel name identifier."""
        pass

    @abstractmethod
    async def connect(self) -> bool:
        """
        Connect to the messaging platform.
        Returns True if connection successful.
        """
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the platform."""
        pass

    @abstractmethod
    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        media: Optional[List[MediaAttachment]] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """
        Send a message to a chat.

        Args:
            chat_id: Target chat/user ID
            text: Message text
            reply_to: Message ID to reply to
            media: Media attachments
            buttons: Interactive buttons/keyboard

        Returns:
            SendResult with success status and message ID
        """
        pass

    @abstractmethod
    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Edit an existing message."""
        pass

    @abstractmethod
    async def delete_message(
        self,
        chat_id: str,
        message_id: str,
    ) -> bool:
        """Delete a message."""
        pass

    @abstractmethod
    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        pass

    @abstractmethod
    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a chat."""
        pass

    def on_message(self, handler: Callable[[Message], Any]) -> None:
        """
        Register a message handler.

        Handler will be called for each incoming message.
        """
        self._message_handlers.append(handler)

    async def _dispatch_message(self, message: Message) -> None:
        """Dispatch message to all registered handlers."""
        for handler in self._message_handlers:
            try:
                result = handler(message)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Error in message handler: {e}")

    def get_status(self) -> ChannelStatus:
        """Get current connection status."""
        return self.status

    async def start(self) -> None:
        """Start the channel adapter (begin receiving messages)."""
        if self._running:
            return

        self._running = True
        connected = await self.connect()

        if connected:
            self.status = ChannelStatus.CONNECTED
            logger.info(f"{self.name} channel connected")
        else:
            self.status = ChannelStatus.ERROR
            logger.error(f"{self.name} channel failed to connect")

    async def stop(self) -> None:
        """Stop the channel adapter."""
        self._running = False
        await self.disconnect()
        self.status = ChannelStatus.DISCONNECTED
        logger.info(f"{self.name} channel disconnected")

    def is_running(self) -> bool:
        """Check if adapter is running."""
        return self._running and self.status == ChannelStatus.CONNECTED


class ChannelError(Exception):
    """Base exception for channel errors."""
    pass


class ChannelConnectionError(ChannelError):
    """Error connecting to channel."""
    pass


class ChannelSendError(ChannelError):
    """Error sending message."""
    pass


class ChannelRateLimitError(ChannelError):
    """Rate limit exceeded."""
    def __init__(self, retry_after: Optional[int] = None):
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after {retry_after}s" if retry_after else "Rate limited")
