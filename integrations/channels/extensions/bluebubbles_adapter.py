"""
BlueBubbles Channel Adapter

Implements BlueBubbles iMessage bridge integration.
Based on SantaClaw extension patterns for cross-platform messaging.

Features:
- iMessage sending/receiving via BlueBubbles server
- Attachments (images, videos, files)
- Reactions (tapbacks)
- Read receipts
- Typing indicators
- Group chats
- Rich link previews
- Message effects
- Socket.IO real-time events
- Reconnection with exponential backoff
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import time
from typing import Optional, List, Dict, Any, Callable, Set
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum

try:
    import aiohttp
    import socketio
    HAS_BLUEBUBBLES = True
except ImportError:
    HAS_BLUEBUBBLES = False

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
    ChannelRateLimitError,
)

logger = logging.getLogger(__name__)


class TapbackType(Enum):
    """iMessage tapback (reaction) types."""
    LOVE = "love"
    LIKE = "like"
    DISLIKE = "dislike"
    LAUGH = "laugh"
    EMPHASIZE = "emphasize"
    QUESTION = "question"


class MessageEffect(Enum):
    """iMessage bubble and screen effects."""
    SLAM = "com.apple.MobileSMS.expressivesend.impact"
    LOUD = "com.apple.MobileSMS.expressivesend.loud"
    GENTLE = "com.apple.MobileSMS.expressivesend.gentle"
    INVISIBLE_INK = "com.apple.MobileSMS.expressivesend.invisibleink"
    ECHO = "com.apple.messages.effect.CKEchoEffect"
    SPOTLIGHT = "com.apple.messages.effect.CKSpotlightEffect"
    BALLOONS = "com.apple.messages.effect.CKHappyBirthdayEffect"
    CONFETTI = "com.apple.messages.effect.CKConfettiEffect"
    HEART = "com.apple.messages.effect.CKHeartEffect"
    LASERS = "com.apple.messages.effect.CKLasersEffect"
    FIREWORKS = "com.apple.messages.effect.CKFireworksEffect"
    CELEBRATION = "com.apple.messages.effect.CKSparklesEffect"


@dataclass
class BlueBubblesConfig(ChannelConfig):
    """BlueBubbles-specific configuration."""
    server_url: str = ""
    password: str = ""
    enable_read_receipts: bool = True
    enable_typing_indicators: bool = True
    enable_reactions: bool = True
    enable_effects: bool = True
    private_api_enabled: bool = False  # Requires Private API helper
    socket_reconnect: bool = True
    reconnect_attempts: int = 5
    reconnect_delay: float = 1.0


@dataclass
class BlueBubblesChat:
    """BlueBubbles chat (conversation) information."""
    guid: str
    display_name: Optional[str] = None
    participants: List[str] = field(default_factory=list)
    is_group: bool = False
    is_imessage: bool = True
    last_message: Optional[str] = None


@dataclass
class BlueBubblesAttachment:
    """Attachment information."""
    guid: str
    filename: str
    mime_type: str
    transfer_name: str
    total_bytes: int
    is_sticker: bool = False
    hide_attachment: bool = False


class BlueBubblesAdapter(ChannelAdapter):
    """
    BlueBubbles iMessage bridge adapter.

    Requires a running BlueBubbles server on a Mac.

    Usage:
        config = BlueBubblesConfig(
            server_url="http://192.168.1.100:1234",
            password="your-server-password",
        )
        adapter = BlueBubblesAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()
    """

    def __init__(self, config: BlueBubblesConfig):
        if not HAS_BLUEBUBBLES:
            raise ImportError(
                "aiohttp and python-socketio not installed. "
                "Install with: pip install aiohttp python-socketio"
            )

        super().__init__(config)
        self.bb_config: BlueBubblesConfig = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._sio: Optional[socketio.AsyncClient] = None
        self._chats: Dict[str, BlueBubblesChat] = {}
        self._reaction_handlers: List[Callable] = []
        self._typing_handlers: List[Callable] = []
        self._reconnect_count: int = 0
        self._connected: bool = False

    @property
    def name(self) -> str:
        return "bluebubbles"

    async def connect(self) -> bool:
        """Connect to BlueBubbles server."""
        if not self.bb_config.server_url:
            logger.error("BlueBubbles server URL required")
            return False

        if not self.bb_config.password:
            logger.error("BlueBubbles password required")
            return False

        try:
            # Create HTTP session
            self._session = aiohttp.ClientSession()

            # Verify connection
            server_info = await self._get_server_info()
            if not server_info:
                logger.error("Failed to connect to BlueBubbles server")
                self.status = ChannelStatus.ERROR
                return False

            logger.info(f"BlueBubbles server: v{server_info.get('server_version', 'unknown')}")

            # Connect Socket.IO
            await self._connect_socket()

            # Load initial chats
            await self._load_chats()

            self.status = ChannelStatus.CONNECTED
            self._connected = True
            self._reconnect_count = 0
            logger.info("BlueBubbles connected successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to BlueBubbles: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect from BlueBubbles server."""
        self._connected = False

        if self._sio:
            await self._sio.disconnect()
            self._sio = None

        if self._session:
            await self._session.close()
            self._session = None

        self._chats.clear()
        self.status = ChannelStatus.DISCONNECTED

    async def _get_server_info(self) -> Optional[Dict[str, Any]]:
        """Get BlueBubbles server information."""
        if not self._session:
            return None

        try:
            url = f"{self.bb_config.server_url}/api/v1/server/info"
            params = {"password": self.bb_config.password}

            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", {})
                else:
                    logger.error(f"Server info request failed: {resp.status}")

        except Exception as e:
            logger.error(f"Failed to get server info: {e}")

        return None

    async def _connect_socket(self) -> None:
        """Connect to Socket.IO for real-time events."""
        self._sio = socketio.AsyncClient(reconnection=self.bb_config.socket_reconnect)

        @self._sio.event
        async def connect():
            logger.info("Socket.IO connected")
            self._connected = True

        @self._sio.event
        async def disconnect():
            logger.warning("Socket.IO disconnected")
            self._connected = False
            if self.bb_config.socket_reconnect:
                await self._handle_disconnect()

        @self._sio.on("new-message")
        async def on_new_message(data):
            await self._handle_new_message(data)

        @self._sio.on("updated-message")
        async def on_updated_message(data):
            await self._handle_updated_message(data)

        @self._sio.on("typing-indicator")
        async def on_typing(data):
            await self._handle_typing_indicator(data)

        @self._sio.on("group-name-change")
        async def on_group_change(data):
            await self._handle_group_change(data)

        # Connect with authentication
        url = self.bb_config.server_url
        await self._sio.connect(
            url,
            auth={"password": self.bb_config.password},
            transports=["websocket"],
        )

    async def _handle_disconnect(self) -> None:
        """Handle Socket.IO disconnection with reconnection."""
        if self._reconnect_count < self.bb_config.reconnect_attempts:
            self._reconnect_count += 1
            delay = self.bb_config.reconnect_delay * (2 ** (self._reconnect_count - 1))

            logger.info(f"Reconnecting to BlueBubbles in {delay}s")
            await asyncio.sleep(delay)
            await self.connect()

    async def _load_chats(self) -> None:
        """Load all chats from server."""
        if not self._session:
            return

        try:
            url = f"{self.bb_config.server_url}/api/v1/chat/query"
            params = {
                "password": self.bb_config.password,
                "limit": 100,
                "offset": 0,
                "with": "lastMessage,participants",
            }

            async with self._session.post(url, params=params, json={}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for chat_data in data.get("data", []):
                        chat = self._parse_chat(chat_data)
                        self._chats[chat.guid] = chat

                    logger.info(f"Loaded {len(self._chats)} chats")

        except Exception as e:
            logger.error(f"Failed to load chats: {e}")

    def _parse_chat(self, data: Dict[str, Any]) -> BlueBubblesChat:
        """Parse chat data from API response."""
        participants = []
        for p in data.get("participants", []):
            handle = p.get("address") or p.get("id", "")
            if handle:
                participants.append(handle)

        return BlueBubblesChat(
            guid=data.get("guid", ""),
            display_name=data.get("displayName"),
            participants=participants,
            is_group=len(participants) > 1,
            is_imessage=data.get("service", "iMessage") == "iMessage",
            last_message=data.get("lastMessage", {}).get("text"),
        )

    async def _handle_new_message(self, data: Dict[str, Any]) -> None:
        """Handle incoming message from Socket.IO."""
        try:
            msg_data = data if isinstance(data, dict) else json.loads(data)

            # Skip sent messages (from this device)
            if msg_data.get("isFromMe"):
                return

            # Convert to unified message
            message = await self._convert_message(msg_data)
            if message:
                await self._dispatch_message(message)

        except Exception as e:
            logger.error(f"Error handling new message: {e}")

    async def _handle_updated_message(self, data: Dict[str, Any]) -> None:
        """Handle message update (reactions, read receipts)."""
        try:
            msg_data = data if isinstance(data, dict) else json.loads(data)

            # Check for tapback (reaction)
            if msg_data.get("associatedMessageType") and self.bb_config.enable_reactions:
                await self._handle_tapback(msg_data)

        except Exception as e:
            logger.error(f"Error handling message update: {e}")

    async def _handle_tapback(self, data: Dict[str, Any]) -> None:
        """Handle tapback (reaction) event."""
        for handler in self._reaction_handlers:
            try:
                result = handler(data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Reaction handler error: {e}")

    async def _handle_typing_indicator(self, data: Dict[str, Any]) -> None:
        """Handle typing indicator event."""
        if not self.bb_config.enable_typing_indicators:
            return

        for handler in self._typing_handlers:
            try:
                result = handler(data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Typing handler error: {e}")

    async def _handle_group_change(self, data: Dict[str, Any]) -> None:
        """Handle group name change event."""
        chat_guid = data.get("chatGuid")
        new_name = data.get("newName")

        if chat_guid in self._chats:
            self._chats[chat_guid].display_name = new_name
            logger.info(f"Group name changed: {new_name}")

    async def _convert_message(self, data: Dict[str, Any]) -> Optional[Message]:
        """Convert BlueBubbles message to unified Message format."""
        try:
            # Get chat info
            chat_guid = data.get("chats", [{}])[0].get("guid") if data.get("chats") else ""

            # Get sender
            handle = data.get("handle", {})
            sender_id = handle.get("address") or handle.get("id", "")

            # Get text
            text = data.get("text", "")
            subject = data.get("subject")
            if subject:
                text = f"[Subject: {subject}] {text}"

            # Handle attachments
            media = []
            for att_data in data.get("attachments", []):
                attachment = self._parse_attachment(att_data)
                if attachment:
                    media_type = self._get_media_type(attachment.mime_type)
                    media.append(MediaAttachment(
                        type=media_type,
                        file_id=attachment.guid,
                        file_name=attachment.filename,
                        mime_type=attachment.mime_type,
                        file_size=attachment.total_bytes,
                    ))

            # Check if group
            chat = self._chats.get(chat_guid)
            is_group = chat.is_group if chat else False

            return Message(
                id=data.get("guid", str(int(time.time() * 1000))),
                channel=self.name,
                sender_id=sender_id,
                sender_name=sender_id,  # BlueBubbles doesn't provide names easily
                chat_id=chat_guid,
                text=text,
                media=media,
                timestamp=datetime.fromtimestamp(data.get("dateCreated", 0) / 1000) if data.get("dateCreated") else datetime.now(),
                is_group=is_group,
                raw={
                    "service": data.get("service"),
                    "is_imessage": data.get("service") == "iMessage",
                    "effect": data.get("expressiveSendStyleId"),
                    "thread_origin_guid": data.get("threadOriginatorGuid"),
                },
            )

        except Exception as e:
            logger.error(f"Error converting message: {e}")
            return None

    def _parse_attachment(self, data: Dict[str, Any]) -> Optional[BlueBubblesAttachment]:
        """Parse attachment data."""
        try:
            return BlueBubblesAttachment(
                guid=data.get("guid", ""),
                filename=data.get("filename", ""),
                mime_type=data.get("mimeType", "application/octet-stream"),
                transfer_name=data.get("transferName", ""),
                total_bytes=data.get("totalBytes", 0),
                is_sticker=data.get("isSticker", False),
                hide_attachment=data.get("hideAttachment", False),
            )
        except Exception:
            return None

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
        """Send a message via BlueBubbles."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            # Check if sending to phone number (new conversation)
            if chat_id.startswith("+") or "@" in chat_id:
                return await self._send_to_address(chat_id, text, media)

            # Send to existing chat
            return await self._send_to_chat(chat_id, text, reply_to, media)

        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return SendResult(success=False, error=str(e))

    async def _send_to_address(
        self,
        address: str,
        text: str,
        media: Optional[List[MediaAttachment]] = None,
    ) -> SendResult:
        """Send message to a phone number or email."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            url = f"{self.bb_config.server_url}/api/v1/message/text"
            params = {"password": self.bb_config.password}

            data = {
                "chatGuid": f"iMessage;-;{address}",
                "message": text,
            }

            async with self._session.post(url, params=params, json=data) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    msg_data = result.get("data", {})
                    return SendResult(
                        success=True,
                        message_id=msg_data.get("guid"),
                    )
                else:
                    error = await resp.text()
                    return SendResult(success=False, error=error)

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def _send_to_chat(
        self,
        chat_guid: str,
        text: str,
        reply_to: Optional[str] = None,
        media: Optional[List[MediaAttachment]] = None,
    ) -> SendResult:
        """Send message to existing chat."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            url = f"{self.bb_config.server_url}/api/v1/message/text"
            params = {"password": self.bb_config.password}

            data = {
                "chatGuid": chat_guid,
                "message": text,
            }

            # Add reply
            if reply_to:
                data["selectedMessageGuid"] = reply_to

            # Handle attachments
            if media and len(media) > 0:
                # Send attachments separately
                for m in media:
                    await self._send_attachment(chat_guid, m)

            async with self._session.post(url, params=params, json=data) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    msg_data = result.get("data", {})
                    return SendResult(
                        success=True,
                        message_id=msg_data.get("guid"),
                    )
                else:
                    error = await resp.text()
                    return SendResult(success=False, error=error)

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def _send_attachment(
        self,
        chat_guid: str,
        media: MediaAttachment,
    ) -> SendResult:
        """Send attachment to chat."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        if not media.file_path and not media.url:
            return SendResult(success=False, error="No file source")

        try:
            url = f"{self.bb_config.server_url}/api/v1/message/attachment"
            params = {"password": self.bb_config.password}

            # Prepare form data
            form = aiohttp.FormData()
            form.add_field("chatGuid", chat_guid)

            if media.file_path:
                with open(media.file_path, "rb") as f:
                    form.add_field(
                        "attachment",
                        f.read(),
                        filename=media.file_name or "attachment",
                        content_type=media.mime_type or "application/octet-stream",
                    )
            elif media.url:
                # Download and re-upload
                async with self._session.get(media.url) as dl_resp:
                    if dl_resp.status == 200:
                        content = await dl_resp.read()
                        form.add_field(
                            "attachment",
                            content,
                            filename=media.file_name or "attachment",
                            content_type=media.mime_type or "application/octet-stream",
                        )

            async with self._session.post(url, params=params, data=form) as resp:
                if resp.status == 200:
                    return SendResult(success=True)
                else:
                    error = await resp.text()
                    return SendResult(success=False, error=error)

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """
        Edit an iMessage.
        Note: Requires Private API and iOS 16+.
        """
        if not self.bb_config.private_api_enabled:
            return SendResult(success=False, error="Private API not enabled")

        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            url = f"{self.bb_config.server_url}/api/v1/message/{message_id}/edit"
            params = {"password": self.bb_config.password}

            data = {"editedMessage": text}

            async with self._session.post(url, params=params, json=data) as resp:
                if resp.status == 200:
                    return SendResult(success=True, message_id=message_id)
                else:
                    error = await resp.text()
                    return SendResult(success=False, error=error)

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """
        Unsend an iMessage.
        Note: Requires Private API and iOS 16+.
        """
        if not self.bb_config.private_api_enabled:
            logger.warning("Private API required for unsend")
            return False

        if not self._session:
            return False

        try:
            url = f"{self.bb_config.server_url}/api/v1/message/{message_id}/unsend"
            params = {"password": self.bb_config.password}

            async with self._session.post(url, params=params) as resp:
                return resp.status == 200

        except Exception as e:
            logger.error(f"Failed to unsend message: {e}")
            return False

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        if not self.bb_config.enable_typing_indicators:
            return

        if not self.bb_config.private_api_enabled:
            return

        if not self._session:
            return

        try:
            url = f"{self.bb_config.server_url}/api/v1/chat/{chat_id}/typing"
            params = {"password": self.bb_config.password}

            await self._session.post(url, params=params)

        except Exception:
            pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a chat."""
        # Check cache
        if chat_id in self._chats:
            chat = self._chats[chat_id]
            return {
                "guid": chat.guid,
                "display_name": chat.display_name,
                "participants": chat.participants,
                "is_group": chat.is_group,
                "is_imessage": chat.is_imessage,
            }

        # Fetch from API
        if not self._session:
            return None

        try:
            url = f"{self.bb_config.server_url}/api/v1/chat/{chat_id}"
            params = {"password": self.bb_config.password}

            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    chat = self._parse_chat(data.get("data", {}))
                    self._chats[chat.guid] = chat
                    return {
                        "guid": chat.guid,
                        "display_name": chat.display_name,
                        "participants": chat.participants,
                        "is_group": chat.is_group,
                        "is_imessage": chat.is_imessage,
                    }

        except Exception as e:
            logger.error(f"Failed to get chat info: {e}")

        return None

    # BlueBubbles-specific methods

    def on_reaction(self, handler: Callable[[Dict[str, Any]], Any]) -> None:
        """Register a tapback (reaction) handler."""
        self._reaction_handlers.append(handler)

    def on_typing(self, handler: Callable[[Dict[str, Any]], Any]) -> None:
        """Register a typing indicator handler."""
        self._typing_handlers.append(handler)

    async def send_tapback(
        self,
        chat_id: str,
        message_id: str,
        tapback: TapbackType,
    ) -> SendResult:
        """Send a tapback (reaction) to a message."""
        if not self.bb_config.enable_reactions:
            return SendResult(success=False, error="Reactions disabled")

        if not self.bb_config.private_api_enabled:
            return SendResult(success=False, error="Private API required")

        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            url = f"{self.bb_config.server_url}/api/v1/message/{message_id}/react"
            params = {"password": self.bb_config.password}

            data = {
                "chatGuid": chat_id,
                "reaction": tapback.value,
            }

            async with self._session.post(url, params=params, json=data) as resp:
                if resp.status == 200:
                    return SendResult(success=True)
                else:
                    error = await resp.text()
                    return SendResult(success=False, error=error)

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_with_effect(
        self,
        chat_id: str,
        text: str,
        effect: MessageEffect,
    ) -> SendResult:
        """Send a message with a bubble or screen effect."""
        if not self.bb_config.enable_effects:
            return SendResult(success=False, error="Effects disabled")

        if not self.bb_config.private_api_enabled:
            return SendResult(success=False, error="Private API required")

        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            url = f"{self.bb_config.server_url}/api/v1/message/text"
            params = {"password": self.bb_config.password}

            data = {
                "chatGuid": chat_id,
                "message": text,
                "effectId": effect.value,
            }

            async with self._session.post(url, params=params, json=data) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    msg_data = result.get("data", {})
                    return SendResult(
                        success=True,
                        message_id=msg_data.get("guid"),
                    )
                else:
                    error = await resp.text()
                    return SendResult(success=False, error=error)

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def mark_read(self, chat_id: str) -> bool:
        """Mark chat as read."""
        if not self.bb_config.enable_read_receipts:
            return False

        if not self.bb_config.private_api_enabled:
            return False

        if not self._session:
            return False

        try:
            url = f"{self.bb_config.server_url}/api/v1/chat/{chat_id}/read"
            params = {"password": self.bb_config.password}

            async with self._session.post(url, params=params) as resp:
                return resp.status == 200

        except Exception:
            return False

    async def get_attachment(self, attachment_guid: str) -> Optional[bytes]:
        """Download attachment content."""
        if not self._session:
            return None

        try:
            url = f"{self.bb_config.server_url}/api/v1/attachment/{attachment_guid}/download"
            params = {"password": self.bb_config.password}

            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    return await resp.read()

        except Exception as e:
            logger.error(f"Failed to download attachment: {e}")

        return None

    async def create_group(
        self,
        participants: List[str],
        name: Optional[str] = None,
    ) -> Optional[str]:
        """Create a new group chat."""
        if not self._session:
            return None

        try:
            url = f"{self.bb_config.server_url}/api/v1/chat/new"
            params = {"password": self.bb_config.password}

            data = {
                "participants": participants,
            }

            if name:
                data["displayName"] = name

            async with self._session.post(url, params=params, json=data) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    chat_data = result.get("data", {})
                    return chat_data.get("guid")

        except Exception as e:
            logger.error(f"Failed to create group: {e}")

        return None

    async def rename_group(self, chat_id: str, new_name: str) -> bool:
        """Rename a group chat."""
        if not self.bb_config.private_api_enabled:
            return False

        if not self._session:
            return False

        try:
            url = f"{self.bb_config.server_url}/api/v1/chat/{chat_id}/name"
            params = {"password": self.bb_config.password}

            data = {"name": new_name}

            async with self._session.patch(url, params=params, json=data) as resp:
                if resp.status == 200:
                    if chat_id in self._chats:
                        self._chats[chat_id].display_name = new_name
                    return True

        except Exception as e:
            logger.error(f"Failed to rename group: {e}")

        return False

    async def add_participant(self, chat_id: str, address: str) -> bool:
        """Add participant to group."""
        if not self.bb_config.private_api_enabled:
            return False

        if not self._session:
            return False

        try:
            url = f"{self.bb_config.server_url}/api/v1/chat/{chat_id}/participant"
            params = {"password": self.bb_config.password}

            data = {"address": address}

            async with self._session.post(url, params=params, json=data) as resp:
                return resp.status == 200

        except Exception:
            return False

    async def remove_participant(self, chat_id: str, address: str) -> bool:
        """Remove participant from group."""
        if not self.bb_config.private_api_enabled:
            return False

        if not self._session:
            return False

        try:
            url = f"{self.bb_config.server_url}/api/v1/chat/{chat_id}/participant/{address}"
            params = {"password": self.bb_config.password}

            async with self._session.delete(url, params=params) as resp:
                return resp.status == 200

        except Exception:
            return False


def create_bluebubbles_adapter(
    server_url: str = None,
    password: str = None,
    **kwargs
) -> BlueBubblesAdapter:
    """
    Factory function to create BlueBubbles adapter.

    Args:
        server_url: BlueBubbles server URL (or set BLUEBUBBLES_SERVER_URL env var)
        password: Server password (or set BLUEBUBBLES_PASSWORD env var)
        **kwargs: Additional config options

    Returns:
        Configured BlueBubblesAdapter
    """
    server_url = server_url or os.getenv("BLUEBUBBLES_SERVER_URL")
    password = password or os.getenv("BLUEBUBBLES_PASSWORD")

    if not server_url:
        raise ValueError("BlueBubbles server URL required")
    if not password:
        raise ValueError("BlueBubbles password required")

    config = BlueBubblesConfig(
        server_url=server_url,
        password=password,
        **kwargs
    )
    return BlueBubblesAdapter(config)
