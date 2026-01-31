"""
Twitch Channel Adapter

Implements Twitch chat integration with IRC and Helix API.
Based on HevolveBot extension patterns for Twitch.

Features:
- IRC chat integration (TMI.js compatible)
- Whispers (private messages)
- Chat commands with prefix support
- Bits/Cheers events
- Channel point redemptions
- Emote handling
- Subscriber detection
- VIP/Moderator detection
- Raid/Host events
- EventSub webhooks
- Reconnection with exponential backoff
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import re
import ssl
import time
from typing import Optional, List, Dict, Any, Callable, Set
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum

try:
    import aiohttp
    import websockets
    HAS_TWITCH = True
except ImportError:
    HAS_TWITCH = False

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

# Twitch IRC server
TWITCH_IRC_URL = "wss://irc-ws.chat.twitch.tv:443"
TWITCH_IRC_HOST = "irc.chat.twitch.tv"
TWITCH_IRC_PORT = 6697

# Twitch Helix API
TWITCH_HELIX_URL = "https://api.twitch.tv/helix"
TWITCH_AUTH_URL = "https://id.twitch.tv/oauth2/token"


class TwitchUserType(Enum):
    """Twitch user type."""
    NORMAL = "normal"
    VIP = "vip"
    MODERATOR = "mod"
    BROADCASTER = "broadcaster"
    SUBSCRIBER = "subscriber"


@dataclass
class TwitchConfig(ChannelConfig):
    """Twitch-specific configuration."""
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    refresh_token: str = ""
    bot_username: str = ""
    channels: List[str] = field(default_factory=list)
    command_prefix: str = "!"
    enable_whispers: bool = True
    enable_bits: bool = True
    enable_channel_points: bool = True
    enable_eventsub: bool = False
    eventsub_callback_url: str = ""
    eventsub_secret: str = ""
    reconnect_attempts: int = 5
    reconnect_delay: float = 1.0


@dataclass
class TwitchUser:
    """Twitch user information."""
    id: str
    login: str
    display_name: str
    user_type: TwitchUserType = TwitchUserType.NORMAL
    is_subscriber: bool = False
    is_mod: bool = False
    is_vip: bool = False
    is_broadcaster: bool = False
    badges: Dict[str, str] = field(default_factory=dict)
    color: Optional[str] = None


@dataclass
class TwitchBitsEvent:
    """Bits/Cheer event."""
    user: TwitchUser
    channel: str
    bits: int
    message: str
    timestamp: datetime


@dataclass
class TwitchRedemptionEvent:
    """Channel point redemption event."""
    user: TwitchUser
    channel: str
    reward_id: str
    reward_title: str
    user_input: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)


class TwitchAdapter(ChannelAdapter):
    """
    Twitch chat adapter with IRC and Helix API integration.

    Usage:
        config = TwitchConfig(
            client_id="your-client-id",
            client_secret="your-client-secret",
            access_token="your-oauth-token",
            bot_username="your_bot",
            channels=["channel1", "channel2"],
        )
        adapter = TwitchAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()
    """

    def __init__(self, config: TwitchConfig):
        if not HAS_TWITCH:
            raise ImportError(
                "websockets and aiohttp not installed. "
                "Install with: pip install websockets aiohttp"
            )

        super().__init__(config)
        self.twitch_config: TwitchConfig = config
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._read_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._joined_channels: Set[str] = set()
        self._command_handlers: Dict[str, Callable] = {}
        self._bits_handlers: List[Callable] = []
        self._redemption_handlers: List[Callable] = []
        self._reconnect_count: int = 0
        self._last_message_time: Dict[str, float] = {}
        self._user_cache: Dict[str, TwitchUser] = {}

    @property
    def name(self) -> str:
        return "twitch"

    async def connect(self) -> bool:
        """Connect to Twitch IRC."""
        if not self.twitch_config.access_token:
            logger.error("Twitch access token required")
            return False

        if not self.twitch_config.bot_username:
            logger.error("Twitch bot username required")
            return False

        try:
            # Create aiohttp session for API calls
            self._session = aiohttp.ClientSession()

            # Connect to IRC
            self._ws = await websockets.connect(
                TWITCH_IRC_URL,
                ssl=ssl.create_default_context(),
            )

            # Authenticate
            await self._authenticate()

            # Request capabilities
            await self._request_capabilities()

            # Join channels
            for channel in self.twitch_config.channels:
                await self.join_channel(channel)

            # Start read loop
            self._read_task = asyncio.create_task(self._read_loop())

            # Start ping loop
            self._ping_task = asyncio.create_task(self._ping_loop())

            self.status = ChannelStatus.CONNECTED
            self._reconnect_count = 0
            logger.info(f"Twitch connected as {self.twitch_config.bot_username}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Twitch: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect from Twitch IRC."""
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            await self._ws.close()
            self._ws = None

        if self._session:
            await self._session.close()
            self._session = None

        self._joined_channels.clear()
        self.status = ChannelStatus.DISCONNECTED

    async def _authenticate(self) -> None:
        """Authenticate with Twitch IRC."""
        if not self._ws:
            return

        # OAuth token format
        oauth_token = self.twitch_config.access_token
        if not oauth_token.startswith("oauth:"):
            oauth_token = f"oauth:{oauth_token}"

        await self._ws.send(f"PASS {oauth_token}")
        await self._ws.send(f"NICK {self.twitch_config.bot_username}")

    async def _request_capabilities(self) -> None:
        """Request Twitch IRC capabilities."""
        if not self._ws:
            return

        # Request tags, commands, and membership capabilities
        await self._ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands twitch.tv/membership")

    async def join_channel(self, channel: str) -> bool:
        """Join a Twitch channel."""
        if not self._ws:
            return False

        channel = channel.lower()
        if not channel.startswith("#"):
            channel = f"#{channel}"

        try:
            await self._ws.send(f"JOIN {channel}")
            self._joined_channels.add(channel)
            logger.info(f"Joined Twitch channel: {channel}")
            return True
        except Exception as e:
            logger.error(f"Failed to join channel {channel}: {e}")
            return False

    async def leave_channel(self, channel: str) -> bool:
        """Leave a Twitch channel."""
        if not self._ws:
            return False

        channel = channel.lower()
        if not channel.startswith("#"):
            channel = f"#{channel}"

        try:
            await self._ws.send(f"PART {channel}")
            self._joined_channels.discard(channel)
            logger.info(f"Left Twitch channel: {channel}")
            return True
        except Exception as e:
            logger.error(f"Failed to leave channel {channel}: {e}")
            return False

    async def _read_loop(self) -> None:
        """Read messages from IRC connection."""
        while self._ws and self.status == ChannelStatus.CONNECTED:
            try:
                raw_message = await self._ws.recv()
                await self._handle_raw_message(raw_message)

            except websockets.ConnectionClosed:
                logger.warning("Twitch IRC connection closed")
                await self._handle_disconnect()
                break

            except asyncio.CancelledError:
                break

            except Exception as e:
                logger.error(f"Error reading Twitch message: {e}")

    async def _ping_loop(self) -> None:
        """Send periodic PINGs to keep connection alive."""
        while self._ws and self.status == ChannelStatus.CONNECTED:
            try:
                await asyncio.sleep(60)
                if self._ws:
                    await self._ws.send("PING :tmi.twitch.tv")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ping error: {e}")

    async def _handle_disconnect(self) -> None:
        """Handle disconnection with reconnection logic."""
        if self._reconnect_count < self.twitch_config.reconnect_attempts:
            self._reconnect_count += 1
            delay = self.twitch_config.reconnect_delay * (2 ** (self._reconnect_count - 1))
            logger.info(f"Reconnecting to Twitch in {delay}s (attempt {self._reconnect_count})")

            await asyncio.sleep(delay)
            await self.connect()
        else:
            self.status = ChannelStatus.ERROR
            logger.error("Max reconnection attempts reached")

    async def _handle_raw_message(self, raw: str) -> None:
        """Parse and handle raw IRC message."""
        for line in raw.strip().split("\r\n"):
            if not line:
                continue

            # Handle PING
            if line.startswith("PING"):
                await self._ws.send(line.replace("PING", "PONG"))
                continue

            # Parse IRC message
            parsed = self._parse_irc_message(line)
            if not parsed:
                continue

            command = parsed.get("command")

            if command == "PRIVMSG":
                await self._handle_privmsg(parsed)
            elif command == "WHISPER":
                await self._handle_whisper(parsed)
            elif command == "USERNOTICE":
                await self._handle_usernotice(parsed)
            elif command == "CLEARCHAT":
                await self._handle_clearchat(parsed)
            elif command == "CLEARMSG":
                await self._handle_clearmsg(parsed)

    def _parse_irc_message(self, raw: str) -> Optional[Dict[str, Any]]:
        """Parse IRC message into components."""
        tags = {}
        prefix = None
        command = None
        params = []

        idx = 0

        # Parse tags
        if raw.startswith("@"):
            space_idx = raw.index(" ")
            tag_str = raw[1:space_idx]
            for tag in tag_str.split(";"):
                if "=" in tag:
                    key, value = tag.split("=", 1)
                    tags[key] = value.replace("\\s", " ").replace("\\n", "\n")
                else:
                    tags[tag] = True
            idx = space_idx + 1

        # Parse prefix
        if raw[idx] == ":":
            space_idx = raw.index(" ", idx)
            prefix = raw[idx + 1:space_idx]
            idx = space_idx + 1

        # Parse command
        try:
            space_idx = raw.index(" ", idx)
            command = raw[idx:space_idx]
            idx = space_idx + 1
        except ValueError:
            command = raw[idx:]
            return {"tags": tags, "prefix": prefix, "command": command, "params": []}

        # Parse params
        while idx < len(raw):
            if raw[idx] == ":":
                params.append(raw[idx + 1:])
                break
            else:
                try:
                    space_idx = raw.index(" ", idx)
                    params.append(raw[idx:space_idx])
                    idx = space_idx + 1
                except ValueError:
                    params.append(raw[idx:])
                    break

        return {
            "tags": tags,
            "prefix": prefix,
            "command": command,
            "params": params,
        }

    async def _handle_privmsg(self, parsed: Dict[str, Any]) -> None:
        """Handle PRIVMSG (chat message)."""
        tags = parsed["tags"]
        prefix = parsed["prefix"]
        params = parsed["params"]

        if len(params) < 2:
            return

        channel = params[0]
        text = params[1]

        # Extract user info
        username = prefix.split("!")[0] if "!" in prefix else prefix
        user = self._parse_user_from_tags(tags, username)

        # Check for bits
        if "bits" in tags and self.twitch_config.enable_bits:
            bits = int(tags["bits"])
            await self._handle_bits(user, channel, bits, text)

        # Create message
        message = Message(
            id=tags.get("id", str(int(time.time() * 1000))),
            channel=self.name,
            sender_id=tags.get("user-id", username),
            sender_name=user.display_name,
            chat_id=channel,
            text=text,
            timestamp=datetime.now(),
            is_group=True,
            is_bot_mentioned=self.twitch_config.bot_username.lower() in text.lower(),
            raw={
                "tags": tags,
                "user": user,
                "emotes": tags.get("emotes", ""),
            },
        )

        # Check for command
        if text.startswith(self.twitch_config.command_prefix):
            await self._handle_command(message, user)
        else:
            await self._dispatch_message(message)

    async def _handle_whisper(self, parsed: Dict[str, Any]) -> None:
        """Handle WHISPER (private message)."""
        if not self.twitch_config.enable_whispers:
            return

        tags = parsed["tags"]
        prefix = parsed["prefix"]
        params = parsed["params"]

        if len(params) < 2:
            return

        text = params[1]
        username = prefix.split("!")[0] if "!" in prefix else prefix
        user = self._parse_user_from_tags(tags, username)

        message = Message(
            id=tags.get("message-id", str(int(time.time() * 1000))),
            channel=self.name,
            sender_id=tags.get("user-id", username),
            sender_name=user.display_name,
            chat_id=f"whisper:{username}",
            text=text,
            timestamp=datetime.now(),
            is_group=False,
            raw={
                "tags": tags,
                "user": user,
                "is_whisper": True,
            },
        )

        await self._dispatch_message(message)

    async def _handle_usernotice(self, parsed: Dict[str, Any]) -> None:
        """Handle USERNOTICE (subs, raids, etc.)."""
        tags = parsed["tags"]
        msg_id = tags.get("msg-id")

        if msg_id == "sub" or msg_id == "resub":
            logger.info(f"Subscription: {tags.get('display-name')}")
        elif msg_id == "raid":
            logger.info(f"Raid from {tags.get('display-name')} with {tags.get('msg-param-viewerCount')} viewers")

    async def _handle_clearchat(self, parsed: Dict[str, Any]) -> None:
        """Handle CLEARCHAT (timeout/ban)."""
        tags = parsed["tags"]
        params = parsed["params"]

        if len(params) >= 2:
            logger.info(f"User {params[1]} was timed out/banned")

    async def _handle_clearmsg(self, parsed: Dict[str, Any]) -> None:
        """Handle CLEARMSG (message deleted)."""
        tags = parsed["tags"]
        logger.info(f"Message deleted: {tags.get('target-msg-id')}")

    def _parse_user_from_tags(self, tags: Dict[str, Any], username: str) -> TwitchUser:
        """Parse TwitchUser from IRC tags."""
        badges = {}
        if "badges" in tags and tags["badges"]:
            for badge in tags["badges"].split(","):
                if "/" in badge:
                    name, version = badge.split("/", 1)
                    badges[name] = version

        user_type = TwitchUserType.NORMAL
        is_broadcaster = "broadcaster" in badges
        is_mod = "moderator" in badges or tags.get("mod") == "1"
        is_vip = "vip" in badges
        is_subscriber = "subscriber" in badges or tags.get("subscriber") == "1"

        if is_broadcaster:
            user_type = TwitchUserType.BROADCASTER
        elif is_mod:
            user_type = TwitchUserType.MODERATOR
        elif is_vip:
            user_type = TwitchUserType.VIP
        elif is_subscriber:
            user_type = TwitchUserType.SUBSCRIBER

        return TwitchUser(
            id=tags.get("user-id", username),
            login=username,
            display_name=tags.get("display-name", username),
            user_type=user_type,
            is_subscriber=is_subscriber,
            is_mod=is_mod,
            is_vip=is_vip,
            is_broadcaster=is_broadcaster,
            badges=badges,
            color=tags.get("color"),
        )

    async def _handle_command(self, message: Message, user: TwitchUser) -> None:
        """Handle chat command."""
        text = message.text[len(self.twitch_config.command_prefix):]
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if command in self._command_handlers:
            handler = self._command_handlers[command]
            try:
                await handler(message, user, args)
            except Exception as e:
                logger.error(f"Command handler error: {e}")
        else:
            # Dispatch as regular message
            await self._dispatch_message(message)

    async def _handle_bits(
        self,
        user: TwitchUser,
        channel: str,
        bits: int,
        message: str,
    ) -> None:
        """Handle bits/cheer event."""
        event = TwitchBitsEvent(
            user=user,
            channel=channel,
            bits=bits,
            message=message,
            timestamp=datetime.now(),
        )

        for handler in self._bits_handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Bits handler error: {e}")

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        media: Optional[List[MediaAttachment]] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send a message to a Twitch channel."""
        if not self._ws:
            return SendResult(success=False, error="Not connected")

        try:
            channel = chat_id
            if not channel.startswith("#"):
                channel = f"#{channel}"

            # Handle whispers
            if chat_id.startswith("whisper:"):
                username = chat_id.replace("whisper:", "")
                return await self.send_whisper(username, text)

            # Rate limiting (20 messages per 30 seconds for normal users)
            now = time.time()
            if channel in self._last_message_time:
                elapsed = now - self._last_message_time[channel]
                if elapsed < 1.5:  # Simple rate limit
                    await asyncio.sleep(1.5 - elapsed)

            # Send with reply if specified
            if reply_to:
                await self._ws.send(f"@reply-parent-msg-id={reply_to} PRIVMSG {channel} :{text}")
            else:
                await self._ws.send(f"PRIVMSG {channel} :{text}")

            self._last_message_time[channel] = time.time()
            return SendResult(success=True)

        except Exception as e:
            logger.error(f"Failed to send Twitch message: {e}")
            return SendResult(success=False, error=str(e))

    async def send_whisper(self, username: str, text: str) -> SendResult:
        """Send a whisper (private message)."""
        if not self._ws:
            return SendResult(success=False, error="Not connected")

        if not self.twitch_config.enable_whispers:
            return SendResult(success=False, error="Whispers disabled")

        try:
            # Whispers are sent via PRIVMSG to #jtv (legacy) or API
            # Modern approach uses Helix API
            if self._session and self.twitch_config.client_id:
                return await self._send_whisper_api(username, text)
            else:
                # Legacy IRC whisper (may not work)
                await self._ws.send(f"PRIVMSG #jtv :/w {username} {text}")
                return SendResult(success=True)

        except Exception as e:
            logger.error(f"Failed to send whisper: {e}")
            return SendResult(success=False, error=str(e))

    async def _send_whisper_api(self, username: str, text: str) -> SendResult:
        """Send whisper via Helix API."""
        if not self._session:
            return SendResult(success=False, error="No session")

        try:
            # Get user ID
            user_id = await self._get_user_id(username)
            if not user_id:
                return SendResult(success=False, error="User not found")

            # Get bot user ID
            bot_id = await self._get_user_id(self.twitch_config.bot_username)
            if not bot_id:
                return SendResult(success=False, error="Bot user ID not found")

            headers = {
                "Authorization": f"Bearer {self.twitch_config.access_token}",
                "Client-Id": self.twitch_config.client_id,
                "Content-Type": "application/json",
            }

            params = {
                "from_user_id": bot_id,
                "to_user_id": user_id,
            }

            data = {"message": text}

            async with self._session.post(
                f"{TWITCH_HELIX_URL}/whispers",
                headers=headers,
                params=params,
                json=data,
            ) as resp:
                if resp.status == 204:
                    return SendResult(success=True)
                else:
                    error = await resp.text()
                    return SendResult(success=False, error=error)

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def _get_user_id(self, username: str) -> Optional[str]:
        """Get user ID from username via Helix API."""
        if username in self._user_cache:
            return self._user_cache[username].id

        if not self._session:
            return None

        try:
            headers = {
                "Authorization": f"Bearer {self.twitch_config.access_token}",
                "Client-Id": self.twitch_config.client_id,
            }

            async with self._session.get(
                f"{TWITCH_HELIX_URL}/users",
                headers=headers,
                params={"login": username},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("data"):
                        user_data = data["data"][0]
                        return user_data["id"]

        except Exception as e:
            logger.error(f"Failed to get user ID: {e}")

        return None

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """
        Edit a Twitch message.
        Note: Twitch doesn't support message editing.
        """
        logger.warning("Twitch doesn't support message editing")
        return SendResult(success=False, error="Not supported")

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete a Twitch message (requires mod privileges)."""
        if not self._ws:
            return False

        try:
            channel = chat_id
            if not channel.startswith("#"):
                channel = f"#{channel}"

            await self._ws.send(f"PRIVMSG {channel} :/delete {message_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to delete message: {e}")
            return False

    async def send_typing(self, chat_id: str) -> None:
        """
        Send typing indicator.
        Note: Twitch doesn't support typing indicators.
        """
        pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a Twitch channel."""
        if not self._session:
            return None

        channel = chat_id.lstrip("#")

        try:
            headers = {
                "Authorization": f"Bearer {self.twitch_config.access_token}",
                "Client-Id": self.twitch_config.client_id,
            }

            async with self._session.get(
                f"{TWITCH_HELIX_URL}/channels",
                headers=headers,
                params={"broadcaster_login": channel},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("data"):
                        channel_data = data["data"][0]
                        return {
                            "id": channel_data["broadcaster_id"],
                            "name": channel_data["broadcaster_name"],
                            "title": channel_data["title"],
                            "game": channel_data.get("game_name"),
                            "language": channel_data.get("broadcaster_language"),
                        }

        except Exception as e:
            logger.error(f"Failed to get channel info: {e}")

        return None

    # Twitch-specific methods

    def register_command(
        self,
        command: str,
        handler: Callable[[Message, TwitchUser, str], Any],
    ) -> None:
        """Register a chat command handler."""
        self._command_handlers[command.lower()] = handler

    def on_bits(self, handler: Callable[[TwitchBitsEvent], Any]) -> None:
        """Register a bits/cheer event handler."""
        self._bits_handlers.append(handler)

    def on_redemption(self, handler: Callable[[TwitchRedemptionEvent], Any]) -> None:
        """Register a channel point redemption handler."""
        self._redemption_handlers.append(handler)

    async def timeout_user(
        self,
        channel: str,
        username: str,
        duration: int,
        reason: str = "",
    ) -> bool:
        """Timeout a user (requires mod privileges)."""
        if not self._ws:
            return False

        try:
            if not channel.startswith("#"):
                channel = f"#{channel}"

            cmd = f"/timeout {username} {duration}"
            if reason:
                cmd += f" {reason}"

            await self._ws.send(f"PRIVMSG {channel} :{cmd}")
            return True

        except Exception as e:
            logger.error(f"Failed to timeout user: {e}")
            return False

    async def ban_user(
        self,
        channel: str,
        username: str,
        reason: str = "",
    ) -> bool:
        """Ban a user (requires mod privileges)."""
        if not self._ws:
            return False

        try:
            if not channel.startswith("#"):
                channel = f"#{channel}"

            cmd = f"/ban {username}"
            if reason:
                cmd += f" {reason}"

            await self._ws.send(f"PRIVMSG {channel} :{cmd}")
            return True

        except Exception as e:
            logger.error(f"Failed to ban user: {e}")
            return False

    async def unban_user(self, channel: str, username: str) -> bool:
        """Unban a user (requires mod privileges)."""
        if not self._ws:
            return False

        try:
            if not channel.startswith("#"):
                channel = f"#{channel}"

            await self._ws.send(f"PRIVMSG {channel} :/unban {username}")
            return True

        except Exception as e:
            logger.error(f"Failed to unban user: {e}")
            return False

    async def clear_chat(self, channel: str) -> bool:
        """Clear chat (requires mod privileges)."""
        if not self._ws:
            return False

        try:
            if not channel.startswith("#"):
                channel = f"#{channel}"

            await self._ws.send(f"PRIVMSG {channel} :/clear")
            return True

        except Exception as e:
            logger.error(f"Failed to clear chat: {e}")
            return False

    async def set_slow_mode(self, channel: str, seconds: int) -> bool:
        """Set slow mode (requires mod privileges)."""
        if not self._ws:
            return False

        try:
            if not channel.startswith("#"):
                channel = f"#{channel}"

            if seconds > 0:
                await self._ws.send(f"PRIVMSG {channel} :/slow {seconds}")
            else:
                await self._ws.send(f"PRIVMSG {channel} :/slowoff")
            return True

        except Exception as e:
            logger.error(f"Failed to set slow mode: {e}")
            return False

    async def announce(self, channel: str, message: str, color: str = "") -> bool:
        """Send an announcement (requires mod privileges)."""
        if not self._ws:
            return False

        try:
            if not channel.startswith("#"):
                channel = f"#{channel}"

            cmd = f"/announce {message}"
            if color:
                cmd = f"/announce{color} {message}"

            await self._ws.send(f"PRIVMSG {channel} :{cmd}")
            return True

        except Exception as e:
            logger.error(f"Failed to send announcement: {e}")
            return False

    async def refresh_token(self) -> bool:
        """Refresh OAuth token using refresh token."""
        if not self.twitch_config.refresh_token:
            return False

        if not self._session:
            return False

        try:
            data = {
                "grant_type": "refresh_token",
                "refresh_token": self.twitch_config.refresh_token,
                "client_id": self.twitch_config.client_id,
                "client_secret": self.twitch_config.client_secret,
            }

            async with self._session.post(TWITCH_AUTH_URL, data=data) as resp:
                if resp.status == 200:
                    token_data = await resp.json()
                    self.twitch_config.access_token = token_data["access_token"]
                    if "refresh_token" in token_data:
                        self.twitch_config.refresh_token = token_data["refresh_token"]
                    logger.info("Twitch token refreshed")
                    return True
                else:
                    logger.error(f"Failed to refresh token: {await resp.text()}")
                    return False

        except Exception as e:
            logger.error(f"Token refresh error: {e}")
            return False


def create_twitch_adapter(
    client_id: str = None,
    client_secret: str = None,
    access_token: str = None,
    bot_username: str = None,
    channels: List[str] = None,
    **kwargs
) -> TwitchAdapter:
    """
    Factory function to create Twitch adapter.

    Args:
        client_id: Twitch app client ID (or set TWITCH_CLIENT_ID env var)
        client_secret: Twitch app client secret (or set TWITCH_CLIENT_SECRET env var)
        access_token: OAuth access token (or set TWITCH_ACCESS_TOKEN env var)
        bot_username: Bot's Twitch username (or set TWITCH_BOT_USERNAME env var)
        channels: List of channels to join (or set TWITCH_CHANNELS env var, comma-separated)
        **kwargs: Additional config options

    Returns:
        Configured TwitchAdapter
    """
    client_id = client_id or os.getenv("TWITCH_CLIENT_ID")
    client_secret = client_secret or os.getenv("TWITCH_CLIENT_SECRET")
    access_token = access_token or os.getenv("TWITCH_ACCESS_TOKEN")
    bot_username = bot_username or os.getenv("TWITCH_BOT_USERNAME")

    if channels is None:
        channels_env = os.getenv("TWITCH_CHANNELS", "")
        channels = [c.strip() for c in channels_env.split(",") if c.strip()]

    if not access_token:
        raise ValueError("Twitch access token required")
    if not bot_username:
        raise ValueError("Twitch bot username required")

    config = TwitchConfig(
        client_id=client_id or "",
        client_secret=client_secret or "",
        access_token=access_token,
        bot_username=bot_username,
        channels=channels,
        **kwargs
    )
    return TwitchAdapter(config)
