"""
Telegram User Account Adapter

Implements Telegram user account (not bot) integration using Telethon.
Based on HevolveBot extension patterns.

This allows using a regular Telegram user account as a messaging channel,
which can access groups/channels that bots cannot.

Features:
- User account authentication
- Access to all groups/channels
- Send as user (not bot)
- Full message history access
- Docker-compatible
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime
from dataclasses import dataclass, field

try:
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
    from telethon.tl.types import User, Chat, Channel
    HAS_TELETHON = True
except ImportError:
    HAS_TELETHON = False

from ..base import (
    ChannelAdapter,
    ChannelConfig,
    ChannelStatus,
    Message,
    MessageType,
    MediaAttachment,
    SendResult,
    ChannelConnectionError,
    ChannelSendError,
)

logger = logging.getLogger(__name__)


@dataclass
class TelegramUserConfig(ChannelConfig):
    """Telegram user account configuration."""
    api_id: int = 0
    api_hash: str = ""
    session_string: str = ""  # Telethon session string
    phone_number: str = ""    # For initial auth
    proxy: Optional[Dict[str, Any]] = None
    receive_own_messages: bool = False

    @classmethod
    def from_env(cls) -> "TelegramUserConfig":
        """Create config from environment variables."""
        return cls(
            api_id=int(os.getenv("TELEGRAM_API_ID", "0")),
            api_hash=os.getenv("TELEGRAM_API_HASH", ""),
            session_string=os.getenv("TELEGRAM_SESSION", ""),
            phone_number=os.getenv("TELEGRAM_PHONE", ""),
        )


class TelegramUserAdapter(ChannelAdapter):
    """Telegram user account adapter using Telethon."""

    channel_type = "telegram_user"

    @property
    def name(self) -> str:
        """Get adapter name."""
        return self.channel_type

    def __init__(self, config: TelegramUserConfig):
        if not HAS_TELETHON:
            raise ImportError("telethon is required for TelegramUserAdapter")

        super().__init__(config)
        self.config: TelegramUserConfig = config
        self._client: Optional[TelegramClient] = None
        self._connected = False
        self._message_handlers: List[Callable] = []
        self._me: Optional[User] = None

    async def connect(self) -> bool:
        """Connect to Telegram as user."""
        try:
            # Create client with session string
            session = StringSession(self.config.session_string) if self.config.session_string else StringSession()

            self._client = TelegramClient(
                session,
                self.config.api_id,
                self.config.api_hash,
                proxy=self.config.proxy
            )

            await self._client.start(phone=self.config.phone_number)

            # Get current user info
            self._me = await self._client.get_me()

            # Register message handler
            @self._client.on(events.NewMessage)
            async def handler(event):
                await self._handle_message(event)

            self._connected = True
            self._status = ChannelStatus.CONNECTED
            logger.info(f"Connected to Telegram as {self._me.username or self._me.first_name}")

            # Save session string for future use
            if not self.config.session_string:
                new_session = self._client.session.save()
                logger.info(f"Session string (save this): {new_session}")

            return True

        except Exception as e:
            logger.error(f"Failed to connect to Telegram: {e}")
            self._status = ChannelStatus.ERROR
            raise ChannelConnectionError(str(e))

    async def disconnect(self) -> None:
        """Disconnect from Telegram."""
        self._connected = False

        if self._client:
            await self._client.disconnect()
            self._client = None

        self._status = ChannelStatus.DISCONNECTED
        logger.info("Disconnected from Telegram user account")

    async def _handle_message(self, event) -> None:
        """Handle incoming message event."""
        try:
            # Skip own messages unless configured otherwise
            if event.out and not self.config.receive_own_messages:
                return

            message = await self._parse_message(event)
            if message:
                for handler in self._message_handlers:
                    asyncio.create_task(handler(message))

        except Exception as e:
            logger.error(f"Error handling message: {e}")

    async def _parse_message(self, event) -> Optional[Message]:
        """Parse Telethon event to unified Message."""
        try:
            sender = await event.get_sender()
            chat = await event.get_chat()

            sender_name = ""
            if sender:
                if hasattr(sender, 'username') and sender.username:
                    sender_name = sender.username
                elif hasattr(sender, 'first_name'):
                    sender_name = sender.first_name or ""

            chat_id = str(event.chat_id)

            # Determine message type
            msg_type = MessageType.TEXT
            attachments = []

            if event.photo:
                msg_type = MessageType.IMAGE
            elif event.video:
                msg_type = MessageType.VIDEO
            elif event.voice or event.audio:
                msg_type = MessageType.AUDIO
            elif event.document:
                msg_type = MessageType.FILE

            return Message(
                id=str(event.id),
                channel=self.channel_type,
                chat_id=chat_id,
                sender_id=str(sender.id) if sender else "",
                sender_name=sender_name,
                text=event.text or "",
                timestamp=event.date or datetime.now(),
                message_type=msg_type,
                reply_to=str(event.reply_to_msg_id) if event.reply_to_msg_id else None,
                attachments=attachments,
                metadata={
                    "chat_type": type(chat).__name__.lower(),
                    "out": event.out,
                }
            )
        except Exception as e:
            logger.error(f"Error parsing message: {e}")
            return None

    def on_message(self, handler: Callable) -> None:
        """Register message handler."""
        self._message_handlers.append(handler)

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        **kwargs
    ) -> SendResult:
        """Send a message."""
        try:
            entity = await self._client.get_entity(int(chat_id))

            result = await self._client.send_message(
                entity,
                text,
                reply_to=int(reply_to) if reply_to else None,
                parse_mode=kwargs.get("parse_mode", "markdown"),
            )

            return SendResult(
                success=True,
                message_id=str(result.id),
                timestamp=result.date or datetime.now()
            )

        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            raise ChannelSendError(str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        **kwargs
    ) -> bool:
        """Edit a message."""
        try:
            entity = await self._client.get_entity(int(chat_id))
            await self._client.edit_message(
                entity,
                int(message_id),
                text
            )
            return True
        except Exception as e:
            logger.error(f"Failed to edit message: {e}")
            return False

    async def delete_message(self, chat_id: str, message_id: str, **kwargs) -> bool:
        """Delete a message."""
        try:
            entity = await self._client.get_entity(int(chat_id))
            await self._client.delete_messages(entity, [int(message_id)])
            return True
        except Exception as e:
            logger.error(f"Failed to delete message: {e}")
            return False

    async def send_typing(self, chat_id: str, **kwargs) -> None:
        """Send typing action."""
        try:
            entity = await self._client.get_entity(int(chat_id))
            await self._client.send_typing(entity)
        except Exception as e:
            logger.debug(f"Failed to send typing: {e}")

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get chat information."""
        try:
            entity = await self._client.get_entity(int(chat_id))

            info = {
                "id": str(entity.id),
                "type": type(entity).__name__.lower(),
            }

            if hasattr(entity, 'title'):
                info["title"] = entity.title
            if hasattr(entity, 'username'):
                info["username"] = entity.username
            if hasattr(entity, 'first_name'):
                info["name"] = f"{entity.first_name or ''} {entity.last_name or ''}".strip()

            return info
        except Exception as e:
            logger.error(f"Failed to get chat info: {e}")
            return None

    async def send_file(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        **kwargs
    ) -> Optional[str]:
        """Send a file."""
        try:
            entity = await self._client.get_entity(int(chat_id))
            result = await self._client.send_file(
                entity,
                file_path,
                caption=caption,
            )
            return str(result.id)
        except Exception as e:
            logger.error(f"Failed to send file: {e}")
            return None

    def get_session_string(self) -> str:
        """Get current session string for persistence."""
        if self._client:
            return self._client.session.save()
        return ""


def create_telegram_user_adapter(
    api_id: Optional[int] = None,
    api_hash: Optional[str] = None,
    session_string: Optional[str] = None,
    **kwargs
) -> TelegramUserAdapter:
    """Factory function to create a Telegram user adapter."""
    config = TelegramUserConfig(
        api_id=api_id or int(os.getenv("TELEGRAM_API_ID", "0")),
        api_hash=api_hash or os.getenv("TELEGRAM_API_HASH", ""),
        session_string=session_string or os.getenv("TELEGRAM_SESSION", ""),
        **kwargs
    )
    return TelegramUserAdapter(config)
