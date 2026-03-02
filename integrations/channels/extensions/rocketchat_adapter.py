"""
Rocket.Chat Channel Adapter

Implements Rocket.Chat messaging integration using REST API and Realtime API.
Based on HevolveBot extension patterns for Rocket.Chat.

Features:
- REST API for CRUD operations
- Realtime API (WebSocket) for live messaging
- Direct messages and channels
- File attachments
- Reactions and threads
- Slash commands
- User mentions
- Docker-compatible configuration
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import hashlib
try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urljoin

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

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


@dataclass
class RocketChatConfig(ChannelConfig):
    """Rocket.Chat-specific configuration."""
    server_url: str = ""
    username: str = ""
    password: str = ""
    auth_token: str = ""
    user_id: str = ""
    enable_realtime: bool = True
    enable_file_attachments: bool = True
    enable_threads: bool = True
    enable_reactions: bool = True
    reconnect_delay: float = 5.0
    max_reconnect_attempts: int = 10
    websocket_timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "RocketChatConfig":
        """Create config from environment variables (Docker-compatible)."""
        return cls(
            server_url=os.getenv("ROCKETCHAT_URL", ""),
            username=os.getenv("ROCKETCHAT_USERNAME", ""),
            password=os.getenv("ROCKETCHAT_PASSWORD", ""),
            auth_token=os.getenv("ROCKETCHAT_AUTH_TOKEN", ""),
            user_id=os.getenv("ROCKETCHAT_USER_ID", ""),
        )


@dataclass
class RocketChatRoom:
    """Rocket.Chat room information."""
    id: str
    name: str
    type: str  # c=channel, p=private, d=direct
    topic: Optional[str] = None
    description: Optional[str] = None
    user_count: int = 0
    read_only: bool = False
    archived: bool = False


@dataclass
class RocketChatUser:
    """Rocket.Chat user information."""
    id: str
    username: str
    name: Optional[str] = None
    email: Optional[str] = None
    status: str = "offline"
    roles: List[str] = field(default_factory=list)


@dataclass
class RocketChatMessage:
    """Rocket.Chat message structure."""
    id: str
    room_id: str
    text: str
    user: RocketChatUser
    timestamp: datetime
    updated_at: Optional[datetime] = None
    thread_id: Optional[str] = None
    reactions: Dict[str, List[str]] = field(default_factory=dict)
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    mentions: List[str] = field(default_factory=list)


class RocketChatAdapter(ChannelAdapter):
    """Rocket.Chat channel adapter with REST and Realtime API support."""

    channel_type = "rocketchat"

    @property
    def name(self) -> str:
        """Get adapter name."""
        return self.channel_type

    def __init__(self, config: RocketChatConfig):
        super().__init__(config)
        self.config: RocketChatConfig = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[Any] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._auth_token: str = config.auth_token
        self._user_id: str = config.user_id
        self._connected = False
        self._reconnect_count = 0
        self._message_handlers: List[Callable] = []
        self._rooms_cache: Dict[str, RocketChatRoom] = {}
        self._users_cache: Dict[str, RocketChatUser] = {}
        self._ddp_session_id: Optional[str] = None
        self._msg_id_counter = 0

    @property
    def base_url(self) -> str:
        """Get base API URL."""
        return urljoin(self.config.server_url, "/api/v1/")

    @property
    def ws_url(self) -> str:
        """Get WebSocket URL for Realtime API."""
        url = self.config.server_url.replace("http://", "ws://").replace("https://", "wss://")
        return urljoin(url, "/websocket")

    def _get_headers(self) -> Dict[str, str]:
        """Get headers for API requests."""
        headers = {"Content-Type": "application/json"}
        if self._auth_token and self._user_id:
            headers["X-Auth-Token"] = self._auth_token
            headers["X-User-Id"] = self._user_id
        return headers

    async def connect(self) -> bool:
        """Connect to Rocket.Chat."""
        try:
            self._session = aiohttp.ClientSession()

            # Authenticate if needed
            if not self._auth_token:
                await self._authenticate()

            # Verify connection
            if not await self._verify_connection():
                raise ChannelConnectionError("Failed to verify Rocket.Chat connection")

            # Start Realtime API if enabled
            if self.config.enable_realtime and HAS_WEBSOCKETS:
                self._ws_task = asyncio.create_task(self._realtime_loop())

            self._connected = True
            self._status = ChannelStatus.CONNECTED
            logger.info("Connected to Rocket.Chat")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Rocket.Chat: {e}")
            self._status = ChannelStatus.ERROR
            raise ChannelConnectionError(str(e))

    async def disconnect(self) -> None:
        """Disconnect from Rocket.Chat."""
        self._connected = False

        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            await self._ws.close()
            self._ws = None

        if self._session:
            await self._session.close()
            self._session = None

        self._status = ChannelStatus.DISCONNECTED
        logger.info("Disconnected from Rocket.Chat")

    async def _authenticate(self) -> None:
        """Authenticate with username/password."""
        url = urljoin(self.base_url, "login")
        payload = {
            "user": self.config.username,
            "password": self.config.password
        }

        async with self._session.post(url, json=payload) as resp:
            if resp.status != 200:
                raise ChannelConnectionError("Authentication failed")

            data = await resp.json()
            if data.get("status") != "success":
                raise ChannelConnectionError("Authentication failed")

            self._auth_token = data["data"]["authToken"]
            self._user_id = data["data"]["userId"]
            logger.info(f"Authenticated as user {self._user_id}")

    async def _verify_connection(self) -> bool:
        """Verify API connection."""
        url = urljoin(self.base_url, "me")
        async with self._session.get(url, headers=self._get_headers()) as resp:
            return resp.status == 200

    async def _realtime_loop(self) -> None:
        """Main loop for Realtime API WebSocket."""
        while self._connected:
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=25,
                    ping_timeout=self.config.websocket_timeout
                ) as ws:
                    self._ws = ws
                    self._reconnect_count = 0

                    # Connect to DDP
                    await self._ddp_connect()

                    # Login via DDP
                    await self._ddp_login()

                    # Subscribe to messages
                    await self._subscribe_to_messages()

                    # Message receive loop
                    async for message in ws:
                        await self._handle_ws_message(message)

            except (ConnectionClosed, asyncio.TimeoutError) as e:
                if not self._connected:
                    break

                self._reconnect_count += 1
                if self._reconnect_count >= self.config.max_reconnect_attempts:
                    logger.error("Max reconnection attempts reached")
                    self._status = ChannelStatus.ERROR
                    break

                delay = self.config.reconnect_delay * self._reconnect_count
                logger.warning(f"WebSocket disconnected, reconnecting in {delay}s...")
                await asyncio.sleep(delay)

            except Exception as e:
                logger.error(f"Realtime API error: {e}")
                if not self._connected:
                    break
                await asyncio.sleep(self.config.reconnect_delay)

    def _next_msg_id(self) -> str:
        """Generate next DDP message ID."""
        self._msg_id_counter += 1
        return str(self._msg_id_counter)

    async def _ddp_connect(self) -> None:
        """Connect to DDP protocol."""
        msg = {
            "msg": "connect",
            "version": "1",
            "support": ["1"]
        }
        await self._ws.send(json.dumps(msg))

        # Wait for connect response
        response = await self._ws.recv()
        data = json.loads(response)
        if data.get("msg") == "connected":
            self._ddp_session_id = data.get("session")
            logger.debug(f"DDP connected, session: {self._ddp_session_id}")

    async def _ddp_login(self) -> None:
        """Login via DDP with resume token."""
        msg = {
            "msg": "method",
            "method": "login",
            "id": self._next_msg_id(),
            "params": [{"resume": self._auth_token}]
        }
        await self._ws.send(json.dumps(msg))

    async def _subscribe_to_messages(self) -> None:
        """Subscribe to message stream."""
        msg = {
            "msg": "sub",
            "id": self._next_msg_id(),
            "name": "stream-room-messages",
            "params": ["__my_messages__", False]
        }
        await self._ws.send(json.dumps(msg))

    async def _handle_ws_message(self, raw_message: str) -> None:
        """Handle incoming WebSocket message."""
        try:
            data = json.loads(raw_message)

            # Handle ping
            if data.get("msg") == "ping":
                await self._ws.send(json.dumps({"msg": "pong"}))
                return

            # Handle message stream
            if data.get("msg") == "changed" and data.get("collection") == "stream-room-messages":
                fields = data.get("fields", {})
                args = fields.get("args", [])

                for msg_data in args:
                    message = self._parse_message(msg_data)
                    if message:
                        for handler in self._message_handlers:
                            asyncio.create_task(handler(message))

        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON in WebSocket message")
        except Exception as e:
            logger.error(f"Error handling WebSocket message: {e}")

    def _parse_message(self, data: Dict[str, Any]) -> Optional[Message]:
        """Parse Rocket.Chat message to unified Message."""
        try:
            user_data = data.get("u", {})

            # Skip bot's own messages
            if user_data.get("_id") == self._user_id:
                return None

            return Message(
                id=data.get("_id", ""),
                channel=self.channel_type,
                chat_id=data.get("rid", ""),
                sender_id=user_data.get("_id", ""),
                sender_name=user_data.get("username", ""),
                text=data.get("msg", ""),
                timestamp=datetime.fromisoformat(
                    data.get("ts", {}).get("$date", datetime.now().isoformat())
                ) if isinstance(data.get("ts"), dict) else datetime.now(),
                message_type=MessageType.TEXT,
                reply_to=data.get("tmid"),
                metadata={
                    "mentions": data.get("mentions", []),
                    "channels": data.get("channels", []),
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
        """Send a message to a room."""
        url = urljoin(self.base_url, "chat.postMessage")

        payload: Dict[str, Any] = {
            "roomId": chat_id,
            "text": text
        }

        # Thread support
        if reply_to and self.config.enable_threads:
            payload["tmid"] = reply_to

        # Attachments
        attachments = kwargs.get("attachments", [])
        if attachments:
            payload["attachments"] = attachments

        try:
            async with self._session.post(
                url,
                json=payload,
                headers=self._get_headers()
            ) as resp:
                if resp.status == 429:
                    raise ChannelRateLimitError("Rate limited")

                if resp.status != 200:
                    data = await resp.json()
                    raise ChannelSendError(data.get("error", "Send failed"))

                data = await resp.json()
                if not data.get("success"):
                    raise ChannelSendError(data.get("error", "Send failed"))

                msg = data.get("message", {})
                return SendResult(
                    success=True,
                    message_id=msg.get("_id", ""),
                    timestamp=datetime.now()
                )

        except (ChannelSendError, ChannelRateLimitError):
            raise
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
        url = urljoin(self.base_url, "chat.update")

        payload = {
            "roomId": chat_id,
            "msgId": message_id,
            "text": text
        }

        try:
            async with self._session.post(
                url,
                json=payload,
                headers=self._get_headers()
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                return data.get("success", False)
        except Exception as e:
            logger.error(f"Failed to edit message: {e}")
            return False

    async def delete_message(self, chat_id: str, message_id: str, **kwargs) -> bool:
        """Delete a message."""
        url = urljoin(self.base_url, "chat.delete")

        payload = {
            "roomId": chat_id,
            "msgId": message_id
        }

        try:
            async with self._session.post(
                url,
                json=payload,
                headers=self._get_headers()
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                return data.get("success", False)
        except Exception as e:
            logger.error(f"Failed to delete message: {e}")
            return False

    async def send_typing(self, chat_id: str, **kwargs) -> None:
        """Send typing indicator."""
        if self._ws and self._ddp_session_id:
            msg = {
                "msg": "method",
                "method": "stream-notify-room",
                "id": self._next_msg_id(),
                "params": [f"{chat_id}/typing", self.config.username, True]
            }
            try:
                await self._ws.send(json.dumps(msg))
            except Exception as e:
                logger.debug(f"Failed to send typing indicator: {e}")

    async def add_reaction(self, chat_id: str, message_id: str, emoji: str) -> bool:
        """Add reaction to a message."""
        if not self.config.enable_reactions:
            return False

        url = urljoin(self.base_url, "chat.react")
        payload = {
            "messageId": message_id,
            "emoji": emoji,
            "shouldReact": True
        }

        try:
            async with self._session.post(
                url,
                json=payload,
                headers=self._get_headers()
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                return data.get("success", False)
        except Exception as e:
            logger.error(f"Failed to add reaction: {e}")
            return False

    async def remove_reaction(self, chat_id: str, message_id: str, emoji: str) -> bool:
        """Remove reaction from a message."""
        url = urljoin(self.base_url, "chat.react")
        payload = {
            "messageId": message_id,
            "emoji": emoji,
            "shouldReact": False
        }

        try:
            async with self._session.post(
                url,
                json=payload,
                headers=self._get_headers()
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                return data.get("success", False)
        except Exception as e:
            logger.error(f"Failed to remove reaction: {e}")
            return False

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get room information."""
        # Check cache
        if chat_id in self._rooms_cache:
            room = self._rooms_cache[chat_id]
            return {
                "id": room.id,
                "name": room.name,
                "type": room.type,
                "topic": room.topic,
                "user_count": room.user_count,
            }

        url = urljoin(self.base_url, f"rooms.info?roomId={chat_id}")

        try:
            async with self._session.get(url, headers=self._get_headers()) as resp:
                if resp.status != 200:
                    return None

                data = await resp.json()
                if not data.get("success"):
                    return None

                room_data = data.get("room", {})
                room = RocketChatRoom(
                    id=room_data.get("_id", ""),
                    name=room_data.get("name", ""),
                    type=room_data.get("t", ""),
                    topic=room_data.get("topic"),
                    description=room_data.get("description"),
                    user_count=room_data.get("usersCount", 0),
                    read_only=room_data.get("ro", False),
                    archived=room_data.get("archived", False),
                )

                self._rooms_cache[chat_id] = room

                return {
                    "id": room.id,
                    "name": room.name,
                    "type": room.type,
                    "topic": room.topic,
                    "user_count": room.user_count,
                }
        except Exception as e:
            logger.error(f"Failed to get room info: {e}")
            return None

    async def upload_file(
        self,
        chat_id: str,
        file_path: str,
        description: Optional[str] = None
    ) -> Optional[str]:
        """Upload a file to a room."""
        if not self.config.enable_file_attachments:
            return None

        url = urljoin(self.base_url, f"rooms.upload/{chat_id}")

        try:
            data = aiohttp.FormData()
            data.add_field(
                "file",
                open(file_path, "rb"),
                filename=os.path.basename(file_path)
            )
            if description:
                data.add_field("description", description)

            headers = self._get_headers()
            del headers["Content-Type"]  # Let aiohttp set it

            async with self._session.post(url, data=data, headers=headers) as resp:
                if resp.status != 200:
                    return None

                result = await resp.json()
                if result.get("success"):
                    return result.get("message", {}).get("_id")
                return None

        except Exception as e:
            logger.error(f"Failed to upload file: {e}")
            return None

    async def create_direct_message(self, username: str) -> Optional[str]:
        """Create a direct message room with a user."""
        url = urljoin(self.base_url, "im.create")
        payload = {"username": username}

        try:
            async with self._session.post(
                url,
                json=payload,
                headers=self._get_headers()
            ) as resp:
                if resp.status != 200:
                    return None

                data = await resp.json()
                if data.get("success"):
                    return data.get("room", {}).get("_id")
                return None
        except Exception as e:
            logger.error(f"Failed to create DM: {e}")
            return None

    async def get_thread_messages(
        self,
        chat_id: str,
        thread_id: str,
        limit: int = 50
    ) -> List[RocketChatMessage]:
        """Get messages in a thread."""
        url = urljoin(
            self.base_url,
            f"chat.getThreadMessages?tmid={thread_id}&count={limit}"
        )

        try:
            async with self._session.get(url, headers=self._get_headers()) as resp:
                if resp.status != 200:
                    return []

                data = await resp.json()
                if not data.get("success"):
                    return []

                messages = []
                for msg in data.get("messages", []):
                    user_data = msg.get("u", {})
                    messages.append(RocketChatMessage(
                        id=msg.get("_id", ""),
                        room_id=msg.get("rid", ""),
                        text=msg.get("msg", ""),
                        user=RocketChatUser(
                            id=user_data.get("_id", ""),
                            username=user_data.get("username", ""),
                            name=user_data.get("name"),
                        ),
                        timestamp=datetime.now(),
                        thread_id=thread_id,
                    ))

                return messages
        except Exception as e:
            logger.error(f"Failed to get thread messages: {e}")
            return []


def create_rocketchat_adapter(
    server_url: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    auth_token: Optional[str] = None,
    user_id: Optional[str] = None,
    **kwargs
) -> RocketChatAdapter:
    """Factory function to create a Rocket.Chat adapter.

    Args:
        server_url: Rocket.Chat server URL
        username: Username for authentication
        password: Password for authentication
        auth_token: Pre-existing auth token (optional)
        user_id: Pre-existing user ID (optional)
        **kwargs: Additional config options

    Returns:
        Configured RocketChatAdapter instance
    """
    config = RocketChatConfig(
        server_url=server_url or os.getenv("ROCKETCHAT_URL", ""),
        username=username or os.getenv("ROCKETCHAT_USERNAME", ""),
        password=password or os.getenv("ROCKETCHAT_PASSWORD", ""),
        auth_token=auth_token or os.getenv("ROCKETCHAT_AUTH_TOKEN", ""),
        user_id=user_id or os.getenv("ROCKETCHAT_USER_ID", ""),
        **kwargs
    )
    return RocketChatAdapter(config)
