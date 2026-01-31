"""
Discord User Account Adapter

Implements Discord user account (self-bot) integration.
Based on HevolveBot extension patterns.

WARNING: Self-bots violate Discord's Terms of Service and may result in account termination.
This adapter is provided for educational purposes and internal/private use only.

Features:
- User account authentication
- Access to all servers/DMs
- Full message history
- Docker-compatible
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import aiohttp
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime
from dataclasses import dataclass, field

try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

from ..base import (
    ChannelAdapter,
    ChannelConfig,
    ChannelStatus,
    Message,
    MessageType,
    SendResult,
    ChannelConnectionError,
    ChannelSendError,
)

logger = logging.getLogger(__name__)


@dataclass
class DiscordUserConfig(ChannelConfig):
    """Discord user account configuration."""
    user_token: str = ""  # User account token (NOT bot token)
    receive_own_messages: bool = False
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    api_base: str = "https://discord.com/api/v10"
    heartbeat_interval: float = 41.25

    @classmethod
    def from_env(cls) -> "DiscordUserConfig":
        """Create config from environment variables."""
        return cls(
            user_token=os.getenv("DISCORD_USER_TOKEN", ""),
        )


class DiscordUserAdapter(ChannelAdapter):
    """Discord user account adapter (self-bot)."""

    channel_type = "discord_user"

    @property
    def name(self) -> str:
        """Get adapter name."""
        return self.channel_type

    def __init__(self, config: DiscordUserConfig):
        if not HAS_WEBSOCKETS:
            raise ImportError("websockets is required for DiscordUserAdapter")

        super().__init__(config)
        self.config: DiscordUserConfig = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[Any] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._connected = False
        self._message_handlers: List[Callable] = []
        self._sequence: Optional[int] = None
        self._session_id: Optional[str] = None
        self._user_id: Optional[str] = None
        self._guilds: Dict[str, Dict] = {}
        self._channels: Dict[str, Dict] = {}

    def _get_headers(self) -> Dict[str, str]:
        """Get headers for API requests."""
        return {
            "Authorization": self.config.user_token,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

    async def connect(self) -> bool:
        """Connect to Discord as user."""
        try:
            self._session = aiohttp.ClientSession()

            # Verify token
            async with self._session.get(
                f"{self.config.api_base}/users/@me",
                headers=self._get_headers()
            ) as resp:
                if resp.status != 200:
                    raise ChannelConnectionError("Invalid user token")

                user_data = await resp.json()
                self._user_id = user_data["id"]
                logger.info(f"Authenticated as {user_data['username']}#{user_data['discriminator']}")

            # Connect to gateway
            self._ws_task = asyncio.create_task(self._gateway_loop())

            # Wait for ready
            for _ in range(100):
                if self._session_id:
                    break
                await asyncio.sleep(0.1)

            if not self._session_id:
                raise ChannelConnectionError("Failed to establish gateway session")

            self._connected = True
            self._status = ChannelStatus.CONNECTED
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Discord: {e}")
            self._status = ChannelStatus.ERROR
            raise ChannelConnectionError(str(e))

    async def disconnect(self) -> None:
        """Disconnect from Discord."""
        self._connected = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

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
        logger.info("Disconnected from Discord user account")

    async def _gateway_loop(self) -> None:
        """Main gateway WebSocket loop."""
        while self._connected or not self._session_id:
            try:
                async with websockets.connect(self.config.gateway_url) as ws:
                    self._ws = ws

                    # Receive Hello
                    hello = json.loads(await ws.recv())
                    if hello["op"] == 10:
                        interval = hello["d"]["heartbeat_interval"] / 1000
                        self._heartbeat_task = asyncio.create_task(
                            self._heartbeat_loop(interval)
                        )

                    # Send Identify
                    await self._send_identify()

                    # Message loop
                    async for message in ws:
                        await self._handle_gateway_message(json.loads(message))

            except websockets.exceptions.ConnectionClosed:
                if not self._connected:
                    break
                logger.warning("Gateway disconnected, reconnecting...")
                await asyncio.sleep(5)

            except Exception as e:
                logger.error(f"Gateway error: {e}")
                if not self._connected:
                    break
                await asyncio.sleep(5)

    async def _heartbeat_loop(self, interval: float) -> None:
        """Send heartbeats to keep connection alive."""
        while True:
            try:
                await asyncio.sleep(interval)
                if self._ws:
                    await self._ws.send(json.dumps({
                        "op": 1,
                        "d": self._sequence
                    }))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")

    async def _send_identify(self) -> None:
        """Send identify payload."""
        identify = {
            "op": 2,
            "d": {
                "token": self.config.user_token,
                "properties": {
                    "$os": "linux",
                    "$browser": "chrome",
                    "$device": "desktop"
                },
                "presence": {
                    "status": "online",
                    "since": 0,
                    "afk": False
                }
            }
        }
        await self._ws.send(json.dumps(identify))

    async def _handle_gateway_message(self, data: Dict[str, Any]) -> None:
        """Handle gateway message."""
        op = data.get("op")
        event = data.get("t")
        payload = data.get("d")

        if data.get("s"):
            self._sequence = data["s"]

        if op == 0:  # Dispatch
            if event == "READY":
                self._session_id = payload["session_id"]
                self._user_id = payload["user"]["id"]

                # Cache guilds
                for guild in payload.get("guilds", []):
                    self._guilds[guild["id"]] = guild

                logger.info("Discord gateway ready")

            elif event == "MESSAGE_CREATE":
                await self._handle_message(payload)

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Handle incoming message."""
        try:
            # Skip own messages unless configured
            if data.get("author", {}).get("id") == self._user_id:
                if not self.config.receive_own_messages:
                    return

            message = self._parse_message(data)
            if message:
                for handler in self._message_handlers:
                    asyncio.create_task(handler(message))

        except Exception as e:
            logger.error(f"Error handling message: {e}")

    def _parse_message(self, data: Dict[str, Any]) -> Optional[Message]:
        """Parse Discord message to unified Message."""
        try:
            author = data.get("author", {})

            # Determine message type
            msg_type = MessageType.TEXT
            if data.get("attachments"):
                attachment = data["attachments"][0]
                content_type = attachment.get("content_type", "")
                if "image" in content_type:
                    msg_type = MessageType.IMAGE
                elif "video" in content_type:
                    msg_type = MessageType.VIDEO
                elif "audio" in content_type:
                    msg_type = MessageType.AUDIO
                else:
                    msg_type = MessageType.FILE

            return Message(
                id=data.get("id", ""),
                channel=self.channel_type,
                chat_id=data.get("channel_id", ""),
                sender_id=author.get("id", ""),
                sender_name=author.get("username", ""),
                text=data.get("content", ""),
                timestamp=datetime.fromisoformat(
                    data.get("timestamp", datetime.now().isoformat()).replace("Z", "+00:00")
                ),
                message_type=msg_type,
                reply_to=data.get("referenced_message", {}).get("id") if data.get("referenced_message") else None,
                metadata={
                    "guild_id": data.get("guild_id"),
                    "discriminator": author.get("discriminator"),
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
        url = f"{self.config.api_base}/channels/{chat_id}/messages"

        payload: Dict[str, Any] = {"content": text}

        if reply_to:
            payload["message_reference"] = {"message_id": reply_to}

        try:
            async with self._session.post(
                url,
                json=payload,
                headers=self._get_headers()
            ) as resp:
                if resp.status not in (200, 201):
                    error = await resp.text()
                    raise ChannelSendError(f"Failed to send: {error}")

                data = await resp.json()
                return SendResult(
                    success=True,
                    message_id=data["id"],
                    timestamp=datetime.now()
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
        url = f"{self.config.api_base}/channels/{chat_id}/messages/{message_id}"

        try:
            async with self._session.patch(
                url,
                json={"content": text},
                headers=self._get_headers()
            ) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"Failed to edit message: {e}")
            return False

    async def delete_message(self, chat_id: str, message_id: str, **kwargs) -> bool:
        """Delete a message."""
        url = f"{self.config.api_base}/channels/{chat_id}/messages/{message_id}"

        try:
            async with self._session.delete(url, headers=self._get_headers()) as resp:
                return resp.status == 204
        except Exception as e:
            logger.error(f"Failed to delete message: {e}")
            return False

    async def send_typing(self, chat_id: str, **kwargs) -> None:
        """Send typing indicator."""
        url = f"{self.config.api_base}/channels/{chat_id}/typing"
        try:
            await self._session.post(url, headers=self._get_headers())
        except Exception as e:
            logger.debug(f"Failed to send typing: {e}")

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get channel information."""
        url = f"{self.config.api_base}/channels/{chat_id}"

        try:
            async with self._session.get(url, headers=self._get_headers()) as resp:
                if resp.status != 200:
                    return None

                data = await resp.json()
                return {
                    "id": data["id"],
                    "name": data.get("name", "DM"),
                    "type": data.get("type"),
                    "guild_id": data.get("guild_id"),
                }
        except Exception as e:
            logger.error(f"Failed to get channel info: {e}")
            return None


def create_discord_user_adapter(
    user_token: Optional[str] = None,
    **kwargs
) -> DiscordUserAdapter:
    """Factory function to create a Discord user adapter."""
    config = DiscordUserConfig(
        user_token=user_token or os.getenv("DISCORD_USER_TOKEN", ""),
        **kwargs
    )
    return DiscordUserAdapter(config)
