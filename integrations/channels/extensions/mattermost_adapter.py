"""
Mattermost Channel Adapter

Implements Mattermost messaging integration using WebSocket for real-time
and REST API for operations.
Based on HevolveBot extension patterns for Mattermost.

Features:
- WebSocket API for real-time messaging
- REST API for operations
- Slash commands
- Interactive messages (buttons, menus)
- File attachments
- Thread support
- Reactions
- Direct messages and channels
- Reconnection logic
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
class MattermostConfig(ChannelConfig):
    """Mattermost-specific configuration."""
    server_url: str = ""
    personal_access_token: str = ""
    bot_username: str = ""
    team_id: Optional[str] = None
    enable_slash_commands: bool = True
    enable_interactive_messages: bool = True
    enable_file_attachments: bool = True
    enable_threads: bool = True
    reconnect_delay: float = 5.0
    max_reconnect_attempts: int = 10
    websocket_timeout: float = 30.0


@dataclass
class MattermostChannel:
    """Mattermost channel information."""
    id: str
    name: str
    display_name: str
    team_id: str
    type: str  # O=public, P=private, D=direct, G=group
    header: Optional[str] = None
    purpose: Optional[str] = None
    member_count: int = 0


@dataclass
class MattermostUser:
    """Mattermost user information."""
    id: str
    username: str
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    nickname: Optional[str] = None
    position: Optional[str] = None


@dataclass
class InteractiveMessage:
    """Interactive message builder for Mattermost."""
    text: str = ""
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    props: Dict[str, Any] = field(default_factory=dict)

    def add_attachment(
        self,
        fallback: str,
        color: str = "#0076B4",
        pretext: str = "",
        text: str = "",
        author_name: str = "",
        title: str = "",
        title_link: str = "",
        fields: Optional[List[Dict[str, Any]]] = None,
        image_url: str = "",
        thumb_url: str = "",
        footer: str = "",
        actions: Optional[List[Dict[str, Any]]] = None,
    ) -> 'InteractiveMessage':
        """Add an attachment to the message."""
        attachment = {
            "fallback": fallback,
            "color": color,
        }
        if pretext:
            attachment["pretext"] = pretext
        if text:
            attachment["text"] = text
        if author_name:
            attachment["author_name"] = author_name
        if title:
            attachment["title"] = title
        if title_link:
            attachment["title_link"] = title_link
        if fields:
            attachment["fields"] = fields
        if image_url:
            attachment["image_url"] = image_url
        if thumb_url:
            attachment["thumb_url"] = thumb_url
        if footer:
            attachment["footer"] = footer
        if actions:
            attachment["actions"] = actions

        self.attachments.append(attachment)
        return self

    def add_button(
        self,
        name: str,
        integration_url: str,
        context: Dict[str, Any] = None,
        style: str = "default",
    ) -> 'InteractiveMessage':
        """Add a button action to the last attachment."""
        if not self.attachments:
            self.add_attachment(fallback=name)

        action = {
            "id": name.lower().replace(" ", "_"),
            "name": name,
            "integration": {
                "url": integration_url,
                "context": context or {},
            },
            "style": style,  # default, primary, success, danger, warning
        }

        if "actions" not in self.attachments[-1]:
            self.attachments[-1]["actions"] = []

        self.attachments[-1]["actions"].append(action)
        return self

    def add_select_menu(
        self,
        name: str,
        integration_url: str,
        options: List[Dict[str, str]],
        context: Dict[str, Any] = None,
    ) -> 'InteractiveMessage':
        """Add a select menu to the last attachment."""
        if not self.attachments:
            self.add_attachment(fallback=name)

        action = {
            "id": name.lower().replace(" ", "_"),
            "name": name,
            "type": "select",
            "integration": {
                "url": integration_url,
                "context": context or {},
            },
            "options": options,  # [{"text": "Option 1", "value": "opt1"}, ...]
        }

        if "actions" not in self.attachments[-1]:
            self.attachments[-1]["actions"] = []

        self.attachments[-1]["actions"].append(action)
        return self

    def to_dict(self) -> Dict[str, Any]:
        """Convert to Mattermost message format."""
        result = {}
        if self.text:
            result["message"] = self.text
        if self.attachments:
            result["props"] = {"attachments": self.attachments}
        if self.props:
            result["props"] = {**result.get("props", {}), **self.props}
        return result


@dataclass
class SlashCommand:
    """Slash command definition."""
    trigger: str
    description: str
    hint: str = ""
    handler: Optional[Callable] = None


class MattermostAdapter(ChannelAdapter):
    """
    Mattermost messaging adapter with WebSocket and REST API support.

    Usage:
        config = MattermostConfig(
            server_url="https://mattermost.example.com",
            personal_access_token="your-token",
            bot_username="mybot",
        )
        adapter = MattermostAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()
    """

    def __init__(self, config: MattermostConfig):
        if not HAS_WEBSOCKETS:
            raise ImportError(
                "websockets not installed. "
                "Install with: pip install websockets aiohttp"
            )

        super().__init__(config)
        self.mm_config: MattermostConfig = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._user_id: Optional[str] = None
        self._channels: Dict[str, MattermostChannel] = {}
        self._users: Dict[str, MattermostUser] = {}
        self._slash_commands: Dict[str, SlashCommand] = {}
        self._interactive_handlers: Dict[str, Callable] = {}
        self._reconnect_attempts: int = 0
        self._running: bool = False

    @property
    def name(self) -> str:
        return "mattermost"

    @property
    def _api_url(self) -> str:
        """Get API base URL."""
        return urljoin(self.mm_config.server_url, "/api/v4/")

    @property
    def _ws_url(self) -> str:
        """Get WebSocket URL."""
        base = self.mm_config.server_url.replace("https://", "wss://").replace("http://", "ws://")
        return urljoin(base, "/api/v4/websocket")

    def _get_headers(self) -> Dict[str, str]:
        """Get API request headers."""
        return {
            "Authorization": f"Bearer {self.mm_config.personal_access_token}",
            "Content-Type": "application/json",
        }

    async def connect(self) -> bool:
        """Connect to Mattermost server."""
        if not self.mm_config.server_url:
            logger.error("Mattermost server URL not provided")
            return False

        if not self.mm_config.personal_access_token:
            logger.error("Mattermost personal access token not provided")
            return False

        try:
            # Create HTTP session
            self._session = aiohttp.ClientSession(headers=self._get_headers())

            # Verify token and get user info
            user_info = await self._api_get("users/me")
            if not user_info:
                logger.error("Failed to authenticate with Mattermost")
                return False

            self._user_id = user_info.get("id")
            logger.info(f"Mattermost authenticated as: {user_info.get('username')}")

            # Start WebSocket connection
            self._running = True
            self._ws_task = asyncio.create_task(self._websocket_loop())

            self.status = ChannelStatus.CONNECTED
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Mattermost: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect from Mattermost server."""
        self._running = False

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

        self._channels.clear()
        self._users.clear()
        self.status = ChannelStatus.DISCONNECTED

    async def _api_get(self, endpoint: str) -> Optional[Dict[str, Any]]:
        """Make GET request to Mattermost API."""
        if not self._session:
            return None

        try:
            url = urljoin(self._api_url, endpoint)
            async with self._session.get(url) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 429:
                    raise ChannelRateLimitError()
                else:
                    logger.error(f"API GET {endpoint} failed: {response.status}")
                    return None
        except ChannelRateLimitError:
            raise
        except Exception as e:
            logger.error(f"API GET {endpoint} error: {e}")
            return None

    async def _api_post(
        self,
        endpoint: str,
        data: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Make POST request to Mattermost API."""
        if not self._session:
            return None

        try:
            url = urljoin(self._api_url, endpoint)
            async with self._session.post(url, json=data) as response:
                if response.status in (200, 201):
                    return await response.json()
                elif response.status == 429:
                    raise ChannelRateLimitError()
                else:
                    error_text = await response.text()
                    logger.error(f"API POST {endpoint} failed: {response.status} - {error_text}")
                    return None
        except ChannelRateLimitError:
            raise
        except Exception as e:
            logger.error(f"API POST {endpoint} error: {e}")
            return None

    async def _api_put(
        self,
        endpoint: str,
        data: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Make PUT request to Mattermost API."""
        if not self._session:
            return None

        try:
            url = urljoin(self._api_url, endpoint)
            async with self._session.put(url, json=data) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 429:
                    raise ChannelRateLimitError()
                else:
                    logger.error(f"API PUT {endpoint} failed: {response.status}")
                    return None
        except ChannelRateLimitError:
            raise
        except Exception as e:
            logger.error(f"API PUT {endpoint} error: {e}")
            return None

    async def _api_delete(self, endpoint: str) -> bool:
        """Make DELETE request to Mattermost API."""
        if not self._session:
            return False

        try:
            url = urljoin(self._api_url, endpoint)
            async with self._session.delete(url) as response:
                return response.status in (200, 204)
        except Exception as e:
            logger.error(f"API DELETE {endpoint} error: {e}")
            return False

    async def _websocket_loop(self) -> None:
        """WebSocket connection loop with reconnection logic."""
        while self._running:
            try:
                await self._connect_websocket()
                await self._listen_websocket()
            except ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}")
                if self._running:
                    await self._handle_reconnect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                if self._running:
                    await self._handle_reconnect()

    async def _connect_websocket(self) -> None:
        """Connect to WebSocket."""
        extra_headers = {
            "Authorization": f"Bearer {self.mm_config.personal_access_token}"
        }

        self._ws = await websockets.connect(
            self._ws_url,
            extra_headers=extra_headers,
            ping_interval=20,
            ping_timeout=self.mm_config.websocket_timeout,
        )

        # Send authentication challenge response
        auth_msg = {
            "seq": 1,
            "action": "authentication_challenge",
            "data": {
                "token": self.mm_config.personal_access_token
            }
        }
        await self._ws.send(json.dumps(auth_msg))

        # Wait for auth response
        response = await self._ws.recv()
        auth_response = json.loads(response)

        if auth_response.get("status") == "OK":
            logger.info("Mattermost WebSocket authenticated")
            self._reconnect_attempts = 0
            self.status = ChannelStatus.CONNECTED
        else:
            raise ChannelConnectionError("WebSocket authentication failed")

    async def _listen_websocket(self) -> None:
        """Listen for WebSocket messages."""
        while self._ws and self._running:
            try:
                message = await asyncio.wait_for(
                    self._ws.recv(),
                    timeout=self.mm_config.websocket_timeout
                )
                data = json.loads(message)
                await self._handle_ws_event(data)
            except asyncio.TimeoutError:
                # Send ping to keep connection alive
                if self._ws:
                    await self._ws.ping()

    async def _handle_reconnect(self) -> None:
        """Handle reconnection with backoff."""
        self._reconnect_attempts += 1

        if self._reconnect_attempts > self.mm_config.max_reconnect_attempts:
            logger.error("Max reconnection attempts reached")
            self.status = ChannelStatus.ERROR
            self._running = False
            return

        delay = min(
            self.mm_config.reconnect_delay * (2 ** self._reconnect_attempts),
            60.0
        )
        logger.info(f"Reconnecting in {delay}s (attempt {self._reconnect_attempts})")
        self.status = ChannelStatus.CONNECTING
        await asyncio.sleep(delay)

    async def _handle_ws_event(self, data: Dict[str, Any]) -> None:
        """Handle WebSocket event."""
        event_type = data.get("event")

        if event_type == "posted":
            await self._handle_posted_event(data)
        elif event_type == "post_edited":
            await self._handle_post_edited_event(data)
        elif event_type == "post_deleted":
            await self._handle_post_deleted_event(data)
        elif event_type == "reaction_added":
            await self._handle_reaction_event(data, added=True)
        elif event_type == "reaction_removed":
            await self._handle_reaction_event(data, added=False)
        elif event_type == "typing":
            pass  # Ignore typing events
        elif event_type == "channel_viewed":
            pass  # Ignore channel viewed events

    async def _handle_posted_event(self, data: Dict[str, Any]) -> None:
        """Handle new post event."""
        try:
            post_data = json.loads(data.get("data", {}).get("post", "{}"))

            # Ignore own messages
            if post_data.get("user_id") == self._user_id:
                return

            # Convert to unified message
            message = await self._convert_message(post_data)
            await self._dispatch_message(message)

        except Exception as e:
            logger.error(f"Error handling posted event: {e}")

    async def _handle_post_edited_event(self, data: Dict[str, Any]) -> None:
        """Handle post edited event."""
        logger.debug(f"Post edited: {data}")

    async def _handle_post_deleted_event(self, data: Dict[str, Any]) -> None:
        """Handle post deleted event."""
        logger.debug(f"Post deleted: {data}")

    async def _handle_reaction_event(
        self,
        data: Dict[str, Any],
        added: bool,
    ) -> None:
        """Handle reaction added/removed event."""
        reaction_data = data.get("data", {})
        logger.debug(f"Reaction {'added' if added else 'removed'}: {reaction_data}")

    async def _convert_message(self, post_data: Dict[str, Any]) -> Message:
        """Convert Mattermost post to unified Message format."""
        user_id = post_data.get("user_id", "")
        channel_id = post_data.get("channel_id", "")

        # Get user info
        user = await self._get_user(user_id)
        sender_name = user.username if user else user_id

        # Get channel info
        channel = await self._get_channel(channel_id)
        is_group = channel.type in ("O", "P", "G") if channel else True

        # Check for bot mention
        text = post_data.get("message", "")
        is_mentioned = f"@{self.mm_config.bot_username}" in text

        # Process file attachments
        media = []
        file_ids = post_data.get("file_ids", [])
        if file_ids:
            for file_id in file_ids:
                file_info = await self._api_get(f"files/{file_id}/info")
                if file_info:
                    media_type = MessageType.DOCUMENT
                    mime_type = file_info.get("mime_type", "")
                    if mime_type.startswith("image/"):
                        media_type = MessageType.IMAGE
                    elif mime_type.startswith("video/"):
                        media_type = MessageType.VIDEO
                    elif mime_type.startswith("audio/"):
                        media_type = MessageType.AUDIO

                    media.append(MediaAttachment(
                        type=media_type,
                        file_id=file_id,
                        file_name=file_info.get("name"),
                        mime_type=mime_type,
                        file_size=file_info.get("size"),
                    ))

        # Get thread info
        reply_to_id = post_data.get("root_id") or None

        return Message(
            id=post_data.get("id", ""),
            channel=self.name,
            sender_id=user_id,
            sender_name=sender_name,
            chat_id=channel_id,
            text=text,
            media=media,
            reply_to_id=reply_to_id,
            timestamp=datetime.fromtimestamp(post_data.get("create_at", 0) / 1000),
            is_group=is_group,
            is_bot_mentioned=is_mentioned,
            raw={
                "team_id": channel.team_id if channel else None,
                "channel_name": channel.name if channel else None,
                "channel_display_name": channel.display_name if channel else None,
                "props": post_data.get("props", {}),
                "metadata": post_data.get("metadata", {}),
            },
        )

    async def _get_user(self, user_id: str) -> Optional[MattermostUser]:
        """Get user information (cached)."""
        if user_id in self._users:
            return self._users[user_id]

        user_data = await self._api_get(f"users/{user_id}")
        if user_data:
            user = MattermostUser(
                id=user_data.get("id"),
                username=user_data.get("username"),
                email=user_data.get("email"),
                first_name=user_data.get("first_name"),
                last_name=user_data.get("last_name"),
                nickname=user_data.get("nickname"),
                position=user_data.get("position"),
            )
            self._users[user_id] = user
            return user
        return None

    async def _get_channel(self, channel_id: str) -> Optional[MattermostChannel]:
        """Get channel information (cached)."""
        if channel_id in self._channels:
            return self._channels[channel_id]

        channel_data = await self._api_get(f"channels/{channel_id}")
        if channel_data:
            channel = MattermostChannel(
                id=channel_data.get("id"),
                name=channel_data.get("name"),
                display_name=channel_data.get("display_name"),
                team_id=channel_data.get("team_id"),
                type=channel_data.get("type"),
                header=channel_data.get("header"),
                purpose=channel_data.get("purpose"),
            )
            self._channels[channel_id] = channel
            return channel
        return None

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        media: Optional[List[MediaAttachment]] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send a message to a Mattermost channel."""
        try:
            post_data = {
                "channel_id": chat_id,
                "message": text,
            }

            # Add thread reply
            if reply_to and self.mm_config.enable_threads:
                post_data["root_id"] = reply_to

            # Build interactive message if buttons provided
            if buttons and self.mm_config.enable_interactive_messages:
                interactive = self._build_interactive_message(text, buttons)
                post_data.update(interactive.to_dict())

            # Handle file attachments
            file_ids = []
            if media and self.mm_config.enable_file_attachments:
                for m in media:
                    if m.file_path:
                        file_id = await self._upload_file(chat_id, m.file_path)
                        if file_id:
                            file_ids.append(file_id)

            if file_ids:
                post_data["file_ids"] = file_ids

            result = await self._api_post("posts", post_data)

            if result:
                return SendResult(
                    success=True,
                    message_id=result.get("id"),
                    raw=result,
                )
            else:
                return SendResult(success=False, error="Failed to send message")

        except Exception as e:
            logger.error(f"Failed to send Mattermost message: {e}")
            return SendResult(success=False, error=str(e))

    def _build_interactive_message(
        self,
        text: str,
        buttons: List[Dict],
    ) -> InteractiveMessage:
        """Build interactive message with buttons."""
        interactive = InteractiveMessage(text=text)
        interactive.add_attachment(fallback=text)

        for btn in buttons:
            if btn.get("url"):
                # URL button - use markdown link in text
                interactive.text += f"\n[{btn['text']}]({btn['url']})"
            else:
                # Action button
                callback_data = btn.get("callback_data", btn["text"])
                webhook_url = btn.get("webhook_url", "")
                if webhook_url:
                    interactive.add_button(
                        name=btn["text"],
                        integration_url=webhook_url,
                        context={"action": callback_data},
                        style=btn.get("style", "default"),
                    )

        return interactive

    async def _upload_file(
        self,
        channel_id: str,
        file_path: str,
    ) -> Optional[str]:
        """Upload a file to Mattermost."""
        if not self._session:
            return None

        try:
            url = urljoin(self._api_url, "files")

            with open(file_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("channel_id", channel_id)
                data.add_field(
                    "files",
                    f,
                    filename=os.path.basename(file_path),
                )

                async with self._session.post(url, data=data) as response:
                    if response.status == 201:
                        result = await response.json()
                        file_infos = result.get("file_infos", [])
                        if file_infos:
                            return file_infos[0].get("id")
            return None

        except Exception as e:
            logger.error(f"Failed to upload file: {e}")
            return None

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Edit an existing Mattermost message."""
        try:
            post_data = {
                "id": message_id,
                "message": text,
            }

            if buttons and self.mm_config.enable_interactive_messages:
                interactive = self._build_interactive_message(text, buttons)
                post_data.update(interactive.to_dict())

            result = await self._api_put(f"posts/{message_id}", post_data)

            if result:
                return SendResult(success=True, message_id=message_id)
            else:
                return SendResult(success=False, error="Failed to edit message")

        except Exception as e:
            logger.error(f"Failed to edit Mattermost message: {e}")
            return SendResult(success=False, error=str(e))

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete a Mattermost message."""
        return await self._api_delete(f"posts/{message_id}")

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator via WebSocket."""
        if self._ws and self._user_id:
            try:
                typing_msg = {
                    "action": "user_typing",
                    "seq": 2,
                    "data": {
                        "channel_id": chat_id,
                        "parent_id": "",
                    }
                }
                await self._ws.send(json.dumps(typing_msg))
            except Exception:
                pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a Mattermost channel."""
        channel = await self._get_channel(chat_id)
        if channel:
            return {
                "id": channel.id,
                "name": channel.name,
                "display_name": channel.display_name,
                "team_id": channel.team_id,
                "type": channel.type,
                "header": channel.header,
                "purpose": channel.purpose,
            }
        return None

    # Mattermost-specific methods

    def register_slash_command(
        self,
        trigger: str,
        description: str,
        handler: Callable,
        hint: str = "",
    ) -> None:
        """Register a slash command handler."""
        if not self.mm_config.enable_slash_commands:
            return

        self._slash_commands[trigger] = SlashCommand(
            trigger=trigger,
            description=description,
            hint=hint,
            handler=handler,
        )

    async def handle_slash_command(
        self,
        command: str,
        text: str,
        user_id: str,
        channel_id: str,
        trigger_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Handle incoming slash command from webhook."""
        if command in self._slash_commands:
            cmd = self._slash_commands[command]
            if cmd.handler:
                return await cmd.handler(
                    command=command,
                    text=text,
                    user_id=user_id,
                    channel_id=channel_id,
                    trigger_id=trigger_id,
                )
        return None

    def register_interactive_handler(
        self,
        action_id: str,
        handler: Callable,
    ) -> None:
        """Register an interactive message action handler."""
        self._interactive_handlers[action_id] = handler

    async def handle_interactive_action(
        self,
        action_id: str,
        context: Dict[str, Any],
        user_id: str,
        channel_id: str,
        post_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Handle interactive message action from webhook."""
        if action_id in self._interactive_handlers:
            handler = self._interactive_handlers[action_id]
            return await handler(
                action_id=action_id,
                context=context,
                user_id=user_id,
                channel_id=channel_id,
                post_id=post_id,
            )
        return None

    async def send_interactive_message(
        self,
        chat_id: str,
        interactive: InteractiveMessage,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """Send an interactive message with attachments."""
        try:
            post_data = {
                "channel_id": chat_id,
                **interactive.to_dict(),
            }

            if reply_to:
                post_data["root_id"] = reply_to

            result = await self._api_post("posts", post_data)

            if result:
                return SendResult(success=True, message_id=result.get("id"))
            else:
                return SendResult(success=False, error="Failed to send message")

        except Exception as e:
            logger.error(f"Failed to send interactive message: {e}")
            return SendResult(success=False, error=str(e))

    async def add_reaction(
        self,
        chat_id: str,
        message_id: str,
        emoji_name: str,
    ) -> bool:
        """Add a reaction to a message."""
        try:
            result = await self._api_post("reactions", {
                "user_id": self._user_id,
                "post_id": message_id,
                "emoji_name": emoji_name,
            })
            return result is not None
        except Exception as e:
            logger.error(f"Failed to add reaction: {e}")
            return False

    async def remove_reaction(
        self,
        chat_id: str,
        message_id: str,
        emoji_name: str,
    ) -> bool:
        """Remove a reaction from a message."""
        try:
            return await self._api_delete(
                f"users/{self._user_id}/posts/{message_id}/reactions/{emoji_name}"
            )
        except Exception as e:
            logger.error(f"Failed to remove reaction: {e}")
            return False

    async def send_thread_reply(
        self,
        chat_id: str,
        root_id: str,
        text: str,
        media: Optional[List[MediaAttachment]] = None,
    ) -> SendResult:
        """Send a reply in a thread."""
        return await self.send_message(
            chat_id=chat_id,
            text=text,
            reply_to=root_id,
            media=media,
        )

    async def get_thread_posts(
        self,
        chat_id: str,
        root_id: str,
    ) -> List[Dict[str, Any]]:
        """Get all posts in a thread."""
        try:
            result = await self._api_get(f"posts/{root_id}/thread")
            if result:
                posts = result.get("posts", {})
                order = result.get("order", [])
                return [posts[post_id] for post_id in order if post_id in posts]
            return []
        except Exception as e:
            logger.error(f"Failed to get thread posts: {e}")
            return []

    async def create_direct_channel(self, user_id: str) -> Optional[str]:
        """Create or get direct message channel with a user."""
        try:
            result = await self._api_post("channels/direct", [self._user_id, user_id])
            if result:
                return result.get("id")
            return None
        except Exception as e:
            logger.error(f"Failed to create direct channel: {e}")
            return None

    async def get_channel_members(self, channel_id: str) -> List[MattermostUser]:
        """Get members of a channel."""
        try:
            result = await self._api_get(f"channels/{channel_id}/members")
            if result:
                users = []
                for member in result:
                    user = await self._get_user(member.get("user_id"))
                    if user:
                        users.append(user)
                return users
            return []
        except Exception as e:
            logger.error(f"Failed to get channel members: {e}")
            return []

    async def download_file(self, file_id: str) -> Optional[bytes]:
        """Download a file by ID."""
        if not self._session:
            return None

        try:
            url = urljoin(self._api_url, f"files/{file_id}")
            async with self._session.get(url) as response:
                if response.status == 200:
                    return await response.read()
            return None
        except Exception as e:
            logger.error(f"Failed to download file: {e}")
            return None


def create_mattermost_adapter(
    server_url: str = None,
    personal_access_token: str = None,
    bot_username: str = None,
    **kwargs
) -> MattermostAdapter:
    """
    Factory function to create Mattermost adapter.

    Args:
        server_url: Mattermost server URL (or set MATTERMOST_SERVER_URL env var)
        personal_access_token: Personal access token (or set MATTERMOST_TOKEN env var)
        bot_username: Bot username (or set MATTERMOST_BOT_USERNAME env var)
        **kwargs: Additional config options

    Returns:
        Configured MattermostAdapter
    """
    server_url = server_url or os.getenv("MATTERMOST_SERVER_URL")
    personal_access_token = personal_access_token or os.getenv("MATTERMOST_TOKEN")
    bot_username = bot_username or os.getenv("MATTERMOST_BOT_USERNAME", "bot")

    if not server_url:
        raise ValueError("Mattermost server URL required")
    if not personal_access_token:
        raise ValueError("Mattermost personal access token required")

    config = MattermostConfig(
        server_url=server_url,
        personal_access_token=personal_access_token,
        bot_username=bot_username,
        **kwargs
    )
    return MattermostAdapter(config)
