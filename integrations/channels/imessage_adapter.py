"""
iMessage Channel Adapter

Implements iMessage messaging using BlueBubbles API.
Designed for Docker-compatible deployments with cross-platform access.

Features:
- BlueBubbles API integration (cross-platform iMessage access)
- Group chats
- Tapbacks (reactions)
- Attachments
- Read receipts
- Typing indicators

Requirements:
- BlueBubbles server running on a Mac (https://bluebubbles.app/)
- API access configured

Note: BlueBubbles requires a Mac running as a server to relay iMessages.
This adapter connects to that Mac's BlueBubbles API from Docker/Linux.
"""

from __future__ import annotations

import asyncio
import logging
import os
import base64
import mimetypes
from typing import Optional, List, Dict, Any
from datetime import datetime
from pathlib import Path

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

try:
    import socketio
    HAS_SOCKETIO = True
except ImportError:
    HAS_SOCKETIO = False

from .base import (
    ChannelAdapter,
    ChannelConfig,
    ChannelStatus,
    Message,
    MessageType,
    MediaAttachment,
    SendResult,
    ChannelConnectionError,
    ChannelSendError,
    ChannelRateLimitError,
)

logger = logging.getLogger(__name__)


# Tapback reaction mappings
TAPBACK_MAP = {
    "love": 2000,
    "like": 2001,
    "dislike": 2002,
    "laugh": 2003,
    "emphasize": 2004,
    "question": 2005,
}

TAPBACK_EMOJI_MAP = {
    "heart": "love",
    "thumbsup": "like",
    "thumbsdown": "dislike",
    "haha": "laugh",
    "!!": "emphasize",
    "?": "question",
}


class IMessageAdapter(ChannelAdapter):
    """
    iMessage adapter using BlueBubbles API.

    Usage:
        config = ChannelConfig(
            token="your_bluebubbles_password",
            extra={
                "api_url": "http://your-mac:1234",
            }
        )
        adapter = IMessageAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()
    """

    def __init__(self, config: ChannelConfig):
        if not HAS_AIOHTTP:
            raise ImportError(
                "aiohttp not installed. "
                "Install with: pip install aiohttp"
            )

        super().__init__(config)
        self._password = config.token
        self._api_url = config.extra.get("api_url", "http://localhost:1234")
        self._session: Optional[aiohttp.ClientSession] = None
        self._sio: Optional[Any] = None
        self._running = False
        self._reconnect_delay = 5
        self._max_reconnect_delay = 300
        self._last_message_guid: Optional[str] = None

    @property
    def name(self) -> str:
        return "imessage"

    async def connect(self) -> bool:
        """Connect to BlueBubbles API."""
        if not self._password:
            logger.error("BlueBubbles password not provided")
            return False

        try:
            # Create session with auth
            self._session = aiohttp.ClientSession(
                headers={"Authorization": self._password}
            )

            # Verify API connection
            async with self._session.get(
                f"{self._api_url}/api/v1/server/info"
            ) as response:
                if response.status != 200:
                    logger.error("BlueBubbles API not available")
                    return False

                info = await response.json()
                logger.info(f"Connected to BlueBubbles v{info.get('data', {}).get('server_version', 'unknown')}")

            # Set up Socket.IO for real-time messages
            if HAS_SOCKETIO:
                await self._setup_socketio()
            else:
                # Fall back to polling
                logger.warning("python-socketio not installed, using polling mode")
                asyncio.create_task(self._poll_messages())

            self._running = True
            self.status = ChannelStatus.CONNECTED
            return True

        except aiohttp.ClientError as e:
            logger.error(f"Failed to connect to BlueBubbles: {e}")
            self.status = ChannelStatus.ERROR
            return False
        except Exception as e:
            logger.error(f"BlueBubbles connection error: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def _setup_socketio(self) -> None:
        """Set up Socket.IO connection for real-time messages."""
        try:
            self._sio = socketio.AsyncClient()

            @self._sio.event
            async def connect():
                logger.info("Socket.IO connected to BlueBubbles")
                # Subscribe to new messages
                await self._sio.emit("subscribe", {"topic": "new-message"})

            @self._sio.event
            async def disconnect():
                logger.warning("Socket.IO disconnected from BlueBubbles")
                if self._running:
                    asyncio.create_task(self._reconnect_socketio())

            @self._sio.on("new-message")
            async def on_new_message(data):
                message = self._convert_message(data)
                if message:
                    await self._dispatch_message(message)

            @self._sio.on("message-send-error")
            async def on_send_error(data):
                logger.error(f"Message send error: {data}")

            # Connect with auth
            await self._sio.connect(
                self._api_url,
                auth={"password": self._password},
                transports=["websocket", "polling"],
            )

        except Exception as e:
            logger.error(f"Failed to setup Socket.IO: {e}")
            # Fall back to polling
            asyncio.create_task(self._poll_messages())

    async def _reconnect_socketio(self) -> None:
        """Attempt to reconnect Socket.IO."""
        delay = self._reconnect_delay

        while self._running:
            try:
                await asyncio.sleep(delay)
                await self._sio.connect(
                    self._api_url,
                    auth={"password": self._password},
                )
                break
            except Exception as e:
                logger.error(f"Socket.IO reconnection failed: {e}")
                delay = min(delay * 2, self._max_reconnect_delay)

    async def _poll_messages(self) -> None:
        """Poll for new messages (fallback when Socket.IO unavailable)."""
        reconnect_delay = self._reconnect_delay

        while self._running:
            try:
                params = {"limit": 50, "sort": "DESC"}
                if self._last_message_guid:
                    params["after"] = self._last_message_guid

                async with self._session.get(
                    f"{self._api_url}/api/v1/message",
                    params=params,
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        messages = data.get("data", [])

                        # Process in chronological order
                        for msg_data in reversed(messages):
                            # Skip sent messages (is_from_me)
                            if msg_data.get("is_from_me"):
                                continue

                            message = self._convert_message(msg_data)
                            if message:
                                await self._dispatch_message(message)
                                self._last_message_guid = msg_data.get("guid")

                        reconnect_delay = self._reconnect_delay

                await asyncio.sleep(2)  # Poll interval

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Polling error: {e}")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, self._max_reconnect_delay)

    async def disconnect(self) -> None:
        """Disconnect from BlueBubbles API."""
        self._running = False

        if self._sio and self._sio.connected:
            await self._sio.disconnect()
            self._sio = None

        if self._session:
            await self._session.close()
            self._session = None

        self.status = ChannelStatus.DISCONNECTED
        logger.info("Disconnected from BlueBubbles")

    def _convert_message(self, msg_data: Dict[str, Any]) -> Optional[Message]:
        """Convert BlueBubbles message to unified Message format."""
        # Skip system messages or empty messages
        if not msg_data.get("text") and not msg_data.get("attachments"):
            return None

        # Skip messages from self
        if msg_data.get("is_from_me"):
            return None

        handle = msg_data.get("handle", {})
        chat = msg_data.get("chat", {}) or msg_data.get("chats", [{}])[0] if msg_data.get("chats") else {}

        # Get sender info
        sender_id = handle.get("address", "") or handle.get("id", "")
        sender_name = handle.get("displayName") or sender_id

        # Determine chat type
        is_group = chat.get("style") == 43  # Group chat style

        # Get chat ID (GUID)
        chat_id = chat.get("guid", "") or msg_data.get("chatGuid", "")

        # Process attachments
        media = []
        for att in msg_data.get("attachments", []):
            media_type = self._get_media_type(att.get("mime_type", ""))
            media.append(MediaAttachment(
                type=media_type,
                file_id=att.get("guid"),
                file_name=att.get("transfer_name"),
                mime_type=att.get("mime_type"),
                file_size=att.get("total_bytes"),
            ))

        # Parse timestamp
        timestamp = msg_data.get("date_created")
        if isinstance(timestamp, (int, float)):
            # BlueBubbles uses milliseconds
            timestamp = datetime.fromtimestamp(timestamp / 1000)
        else:
            timestamp = datetime.now()

        return Message(
            id=msg_data.get("guid", ""),
            channel=self.name,
            sender_id=sender_id,
            sender_name=sender_name,
            chat_id=chat_id,
            text=msg_data.get("text", ""),
            media=media,
            reply_to_id=msg_data.get("thread_origin_guid"),
            timestamp=timestamp,
            is_group=is_group,
            is_bot_mentioned=False,  # iMessage doesn't have @mentions
            raw=msg_data,
        )

    def _get_media_type(self, mime_type: str) -> MessageType:
        """Get MessageType from MIME type."""
        if mime_type.startswith("image/"):
            return MessageType.IMAGE
        elif mime_type.startswith("video/"):
            return MessageType.VIDEO
        elif mime_type.startswith("audio/"):
            return MessageType.AUDIO
        else:
            return MessageType.DOCUMENT

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        media: Optional[List[MediaAttachment]] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send an iMessage."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            payload = {
                "chatGuid": chat_id,
                "message": text,
                "method": "private-api",  # Use private API for better delivery
            }

            # Handle reply/thread
            if reply_to:
                payload["selectedMessageGuid"] = reply_to

            # Handle attachments
            if media and len(media) > 0:
                return await self._send_with_attachments(chat_id, text, media, reply_to)

            async with self._session.post(
                f"{self._api_url}/api/v1/message/text",
                json=payload,
            ) as response:
                if response.status in (200, 201):
                    data = await response.json()
                    return SendResult(
                        success=True,
                        message_id=data.get("data", {}).get("guid", ""),
                        raw=data,
                    )
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to send iMessage: {error_text}")
                    return SendResult(success=False, error=error_text)

        except Exception as e:
            logger.error(f"Error sending iMessage: {e}")
            return SendResult(success=False, error=str(e))

    async def _send_with_attachments(
        self,
        chat_id: str,
        text: str,
        media: List[MediaAttachment],
        reply_to: Optional[str],
    ) -> SendResult:
        """Send message with attachments."""
        try:
            # BlueBubbles uses multipart form for attachments
            data = aiohttp.FormData()
            data.add_field("chatGuid", chat_id)
            if text:
                data.add_field("message", text)
            if reply_to:
                data.add_field("selectedMessageGuid", reply_to)

            for idx, m in enumerate(media):
                if m.file_path and Path(m.file_path).exists():
                    path = Path(m.file_path)
                    content = path.read_bytes()
                    data.add_field(
                        f"attachment",
                        content,
                        filename=m.file_name or path.name,
                        content_type=m.mime_type or mimetypes.guess_type(str(path))[0],
                    )
                elif m.url:
                    # Download and attach
                    async with self._session.get(m.url) as response:
                        if response.status == 200:
                            content = await response.read()
                            data.add_field(
                                f"attachment",
                                content,
                                filename=m.file_name or "attachment",
                                content_type=m.mime_type or response.content_type,
                            )

            async with self._session.post(
                f"{self._api_url}/api/v1/message/attachment",
                data=data,
            ) as response:
                if response.status in (200, 201):
                    result = await response.json()
                    return SendResult(
                        success=True,
                        message_id=result.get("data", {}).get("guid", ""),
                        raw=result,
                    )
                else:
                    error_text = await response.text()
                    return SendResult(success=False, error=error_text)

        except Exception as e:
            logger.error(f"Failed to send attachment: {e}")
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Edit an existing message (requires iOS 16+)."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            payload = {
                "editedMessage": text,
                "backwardsCompatMessage": f"[Edited] {text}",
            }

            async with self._session.post(
                f"{self._api_url}/api/v1/message/{message_id}/edit",
                json=payload,
            ) as response:
                if response.status in (200, 201):
                    data = await response.json()
                    return SendResult(
                        success=True,
                        message_id=message_id,
                        raw=data,
                    )
                else:
                    # Fall back to sending edit indicator
                    return await self.send_message(chat_id, f"[Edit] {text}")

        except Exception as e:
            logger.error(f"Failed to edit message: {e}")
            return await self.send_message(chat_id, f"[Edit] {text}")

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Unsend a message (requires iOS 16+)."""
        if not self._session:
            return False

        try:
            async with self._session.post(
                f"{self._api_url}/api/v1/message/{message_id}/unsend"
            ) as response:
                return response.status in (200, 201, 204)

        except Exception as e:
            logger.error(f"Failed to unsend message: {e}")
            return False

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        if not self._session:
            return

        try:
            await self._session.post(
                f"{self._api_url}/api/v1/chat/{chat_id}/typing",
                json={"status": True},
            )
        except Exception as e:
            logger.debug(f"Failed to send typing indicator: {e}")

    async def stop_typing(self, chat_id: str) -> None:
        """Stop typing indicator."""
        if not self._session:
            return

        try:
            await self._session.post(
                f"{self._api_url}/api/v1/chat/{chat_id}/typing",
                json={"status": False},
            )
        except Exception:
            pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a chat."""
        if not self._session:
            return None

        try:
            async with self._session.get(
                f"{self._api_url}/api/v1/chat/{chat_id}"
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    chat = data.get("data", {})
                    return {
                        "id": chat.get("guid"),
                        "type": "group" if chat.get("style") == 43 else "direct",
                        "display_name": chat.get("displayName"),
                        "participants": [
                            p.get("address") for p in chat.get("participants", [])
                        ],
                    }

        except Exception as e:
            logger.error(f"Failed to get chat info: {e}")

        return None

    async def send_tapback(
        self,
        chat_id: str,
        message_id: str,
        tapback: str,
        remove: bool = False,
    ) -> bool:
        """
        Send a tapback (reaction) to a message.

        Args:
            chat_id: Chat GUID
            message_id: Message GUID to react to
            tapback: Tapback type (love, like, dislike, laugh, emphasize, question)
            remove: Whether to remove the tapback
        """
        if not self._session:
            return False

        # Map emoji-style names to tapback names
        tapback = TAPBACK_EMOJI_MAP.get(tapback, tapback)

        if tapback not in TAPBACK_MAP:
            logger.error(f"Invalid tapback type: {tapback}")
            return False

        try:
            payload = {
                "selectedMessageGuid": message_id,
                "reaction": TAPBACK_MAP[tapback] + (1000 if remove else 0),
            }

            async with self._session.post(
                f"{self._api_url}/api/v1/message/react",
                json=payload,
            ) as response:
                return response.status in (200, 201)

        except Exception as e:
            logger.error(f"Failed to send tapback: {e}")
            return False

    async def mark_read(self, chat_id: str) -> bool:
        """Mark chat as read."""
        if not self._session:
            return False

        try:
            async with self._session.post(
                f"{self._api_url}/api/v1/chat/{chat_id}/read"
            ) as response:
                return response.status in (200, 201, 204)

        except Exception as e:
            logger.error(f"Failed to mark chat as read: {e}")
            return False

    async def create_group(
        self,
        participants: List[str],
        name: Optional[str] = None,
    ) -> Optional[str]:
        """Create a new group chat."""
        if not self._session:
            return None

        try:
            payload = {
                "addresses": participants,
            }
            if name:
                payload["name"] = name

            async with self._session.post(
                f"{self._api_url}/api/v1/chat/new",
                json=payload,
            ) as response:
                if response.status in (200, 201):
                    data = await response.json()
                    return data.get("data", {}).get("guid")

        except Exception as e:
            logger.error(f"Failed to create group: {e}")

        return None

    async def download_attachment(
        self,
        attachment_id: str,
        destination: str,
    ) -> bool:
        """Download an attachment."""
        if not self._session:
            return False

        try:
            async with self._session.get(
                f"{self._api_url}/api/v1/attachment/{attachment_id}/download"
            ) as response:
                if response.status == 200:
                    content = await response.read()
                    Path(destination).write_bytes(content)
                    return True

        except Exception as e:
            logger.error(f"Failed to download attachment: {e}")

        return False


def create_imessage_adapter(
    password: str = None,
    api_url: str = None,
    **kwargs
) -> IMessageAdapter:
    """
    Factory function to create iMessage adapter.

    Args:
        password: BlueBubbles server password (or set BLUEBUBBLES_PASSWORD env var)
        api_url: BlueBubbles API URL (or set BLUEBUBBLES_URL env var)
        **kwargs: Additional config options

    Returns:
        Configured IMessageAdapter
    """
    password = password or os.getenv("BLUEBUBBLES_PASSWORD")
    if not password:
        raise ValueError("BlueBubbles password required")

    api_url = api_url or os.getenv("BLUEBUBBLES_URL", "http://localhost:1234")

    config = ChannelConfig(
        token=password,
        extra={"api_url": api_url, **kwargs.get("extra", {})},
        **{k: v for k, v in kwargs.items() if k != "extra"},
    )
    return IMessageAdapter(config)
