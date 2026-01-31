"""
Nextcloud Talk Channel Adapter

Implements Nextcloud Talk messaging integration using REST API
and WebSocket for real-time communication.
Based on HevolveBot extension patterns for Nextcloud Talk.

Features:
- REST API integration
- WebSocket for real-time messaging
- File sharing integration with Nextcloud Files
- Reactions support
- Room/conversation management
- Participants management
- Rich object sharing
- Polls support
- Reconnection logic
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
import aiohttp
import hashlib
import hmac
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urljoin, quote
from enum import Enum

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


class ConversationType(Enum):
    """Nextcloud Talk conversation types."""
    ONE_TO_ONE = 1
    GROUP = 2
    PUBLIC = 3
    CHANGELOG = 4
    FORMER_ONE_TO_ONE = 5


class ParticipantType(Enum):
    """Participant types in a conversation."""
    OWNER = 1
    MODERATOR = 2
    USER = 3
    GUEST = 4
    USER_SELF_JOINED = 5
    GUEST_MODERATOR = 6


class MessageActorType(Enum):
    """Types of message actors."""
    USERS = "users"
    GUESTS = "guests"
    BOTS = "bots"
    BRIDGED = "bridged"


@dataclass
class NextcloudConfig(ChannelConfig):
    """Nextcloud Talk-specific configuration."""
    server_url: str = ""
    username: str = ""
    password: str = ""
    app_password: Optional[str] = None  # Recommended over password
    enable_file_sharing: bool = True
    enable_reactions: bool = True
    enable_polls: bool = True
    poll_interval: float = 2.0  # For long-polling fallback
    reconnect_delay: float = 5.0
    max_reconnect_attempts: int = 10
    verify_ssl: bool = True


@dataclass
class NextcloudConversation:
    """Nextcloud Talk conversation/room information."""
    token: str
    name: str
    display_name: str
    type: ConversationType
    participant_type: ParticipantType
    read_only: bool = False
    has_password: bool = False
    has_call: bool = False
    unread_messages: int = 0
    last_activity: Optional[datetime] = None
    description: Optional[str] = None


@dataclass
class NextcloudParticipant:
    """Participant in a conversation."""
    attendee_id: int
    actor_type: str
    actor_id: str
    display_name: str
    participant_type: ParticipantType
    last_ping: Optional[datetime] = None
    in_call: bool = False
    session_ids: List[str] = field(default_factory=list)


@dataclass
class NextcloudMessage:
    """Nextcloud Talk message representation."""
    id: int
    token: str
    actor_type: str
    actor_id: str
    actor_display_name: str
    message: str
    timestamp: datetime
    message_type: str = "comment"  # comment, system, command
    is_replyable: bool = True
    reference_id: Optional[str] = None
    parent_id: Optional[int] = None
    reactions: Dict[str, int] = field(default_factory=dict)
    message_parameters: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RichObjectParameter:
    """Rich object parameter for message sharing."""
    type: str  # file, deck-card, talk-poll, etc.
    id: str
    name: str
    extra: Dict[str, Any] = field(default_factory=dict)


class NextcloudAdapter(ChannelAdapter):
    """
    Nextcloud Talk messaging adapter with REST API and file sharing.

    Usage:
        config = NextcloudConfig(
            server_url="https://nextcloud.example.com",
            username="bot",
            app_password="xxxxx-xxxxx-xxxxx-xxxxx-xxxxx",
        )
        adapter = NextcloudAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()
    """

    def __init__(self, config: NextcloudConfig):
        super().__init__(config)
        self.nc_config: NextcloudConfig = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._user_id: Optional[str] = None
        self._conversations: Dict[str, NextcloudConversation] = {}
        self._participants_cache: Dict[str, List[NextcloudParticipant]] = {}
        self._last_known_message: Dict[str, int] = {}
        self._reconnect_attempts: int = 0
        self._running: bool = False
        self._reaction_handlers: List[Callable] = []

    @property
    def name(self) -> str:
        return "nextcloud"

    @property
    def _api_url(self) -> str:
        """Get OCS API base URL."""
        return urljoin(self.nc_config.server_url, "/ocs/v2.php/apps/spreed/api/v4/")

    @property
    def _dav_url(self) -> str:
        """Get WebDAV base URL for file operations."""
        return urljoin(self.nc_config.server_url, f"/remote.php/dav/files/{self.nc_config.username}/")

    def _get_headers(self) -> Dict[str, str]:
        """Get API request headers with Basic Auth."""
        import base64

        password = self.nc_config.app_password or self.nc_config.password
        auth_string = f"{self.nc_config.username}:{password}"
        auth_bytes = base64.b64encode(auth_string.encode()).decode()

        return {
            "Authorization": f"Basic {auth_bytes}",
            "OCS-APIRequest": "true",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def connect(self) -> bool:
        """Connect to Nextcloud Talk server."""
        if not self.nc_config.server_url:
            logger.error("Nextcloud server URL not provided")
            return False

        if not self.nc_config.username:
            logger.error("Nextcloud username not provided")
            return False

        password = self.nc_config.app_password or self.nc_config.password
        if not password:
            logger.error("Nextcloud password or app password not provided")
            return False

        try:
            # Create HTTP session
            ssl_context = None if self.nc_config.verify_ssl else False
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            self._session = aiohttp.ClientSession(
                headers=self._get_headers(),
                connector=connector,
            )

            # Verify authentication by getting user info
            user_info = await self._api_get("../../../cloud/user")
            if not user_info or "ocs" not in user_info:
                logger.error("Failed to authenticate with Nextcloud")
                return False

            self._user_id = user_info["ocs"]["data"].get("id")
            logger.info(f"Nextcloud authenticated as: {self._user_id}")

            # Load conversations
            await self._load_conversations()

            # Start polling for messages
            self._running = True
            self._poll_task = asyncio.create_task(self._poll_loop())

            self.status = ChannelStatus.CONNECTED
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Nextcloud: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect from Nextcloud server."""
        self._running = False

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if self._session:
            await self._session.close()
            self._session = None

        self._conversations.clear()
        self._participants_cache.clear()
        self._last_known_message.clear()
        self.status = ChannelStatus.DISCONNECTED

    async def _api_get(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Make GET request to Nextcloud API."""
        if not self._session:
            return None

        try:
            url = urljoin(self._api_url, endpoint)
            async with self._session.get(url, params=params) as response:
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
        data: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Make POST request to Nextcloud API."""
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
        data: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Make PUT request to Nextcloud API."""
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
        """Make DELETE request to Nextcloud API."""
        if not self._session:
            return False

        try:
            url = urljoin(self._api_url, endpoint)
            async with self._session.delete(url) as response:
                return response.status in (200, 204)
        except Exception as e:
            logger.error(f"API DELETE {endpoint} error: {e}")
            return False

    async def _load_conversations(self) -> None:
        """Load all conversations the bot is part of."""
        result = await self._api_get("room")
        if result and "ocs" in result:
            for conv_data in result["ocs"]["data"]:
                conv = self._parse_conversation(conv_data)
                self._conversations[conv.token] = conv

                # Initialize last known message ID
                if conv_data.get("lastMessage"):
                    self._last_known_message[conv.token] = conv_data["lastMessage"].get("id", 0)

        logger.info(f"Loaded {len(self._conversations)} conversations")

    def _parse_conversation(self, data: Dict[str, Any]) -> NextcloudConversation:
        """Parse conversation data from API response."""
        return NextcloudConversation(
            token=data.get("token", ""),
            name=data.get("name", ""),
            display_name=data.get("displayName", ""),
            type=ConversationType(data.get("type", 2)),
            participant_type=ParticipantType(data.get("participantType", 3)),
            read_only=data.get("readOnly", 0) == 1,
            has_password=data.get("hasPassword", False),
            has_call=data.get("hasCall", False),
            unread_messages=data.get("unreadMessages", 0),
            last_activity=datetime.fromtimestamp(data["lastActivity"]) if data.get("lastActivity") else None,
            description=data.get("description"),
        )

    async def _poll_loop(self) -> None:
        """Poll for new messages from all conversations."""
        while self._running:
            try:
                for token in list(self._conversations.keys()):
                    await self._poll_conversation(token)

                # Also check for new conversations
                await self._load_conversations()

                await asyncio.sleep(self.nc_config.poll_interval)
                self._reconnect_attempts = 0

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Poll error: {e}")
                self._reconnect_attempts += 1
                if self._reconnect_attempts > self.nc_config.max_reconnect_attempts:
                    self.status = ChannelStatus.ERROR
                    break
                await asyncio.sleep(self.nc_config.reconnect_delay)

    async def _poll_conversation(self, token: str) -> None:
        """Poll for new messages in a specific conversation."""
        last_id = self._last_known_message.get(token, 0)

        # Get messages since last known
        params = {
            "lookIntoFuture": 1,
            "limit": 100,
            "setReadMarker": 0,
        }
        if last_id > 0:
            params["lastKnownMessageId"] = last_id

        result = await self._api_get(f"chat/{token}", params)

        if result and "ocs" in result:
            messages = result["ocs"]["data"]
            for msg_data in messages:
                # Skip own messages
                if msg_data.get("actorId") == self._user_id:
                    continue

                # Skip system messages unless relevant
                if msg_data.get("messageType") == "system":
                    continue

                # Convert and dispatch
                message = self._convert_message(token, msg_data)
                await self._dispatch_message(message)

                # Update last known message ID
                msg_id = msg_data.get("id", 0)
                if msg_id > self._last_known_message.get(token, 0):
                    self._last_known_message[token] = msg_id

    def _convert_message(self, token: str, msg_data: Dict[str, Any]) -> Message:
        """Convert Nextcloud Talk message to unified Message format."""
        conv = self._conversations.get(token)
        is_group = conv.type != ConversationType.ONE_TO_ONE if conv else True

        # Parse message parameters (mentions, files, etc.)
        message_text = msg_data.get("message", "")
        message_params = msg_data.get("messageParameters", {})

        # Process file attachments
        media = []
        for param_name, param_data in message_params.items():
            if param_data.get("type") == "file":
                media_type = MessageType.DOCUMENT
                mime_type = param_data.get("mimetype", "")
                if mime_type.startswith("image/"):
                    media_type = MessageType.IMAGE
                elif mime_type.startswith("video/"):
                    media_type = MessageType.VIDEO
                elif mime_type.startswith("audio/"):
                    media_type = MessageType.AUDIO

                media.append(MediaAttachment(
                    type=media_type,
                    file_id=str(param_data.get("id")),
                    file_name=param_data.get("name"),
                    mime_type=mime_type,
                    file_size=param_data.get("size"),
                    url=param_data.get("link"),
                ))

        # Check for bot mention
        is_mentioned = False
        for param_name, param_data in message_params.items():
            if param_data.get("type") == "user" and param_data.get("id") == self._user_id:
                is_mentioned = True
                break

        # Get reply-to ID
        reply_to_id = None
        parent = msg_data.get("parent")
        if parent:
            reply_to_id = str(parent.get("id"))

        return Message(
            id=str(msg_data.get("id", "")),
            channel=self.name,
            sender_id=msg_data.get("actorId", ""),
            sender_name=msg_data.get("actorDisplayName", ""),
            chat_id=token,
            text=message_text,
            media=media,
            reply_to_id=reply_to_id,
            timestamp=datetime.fromtimestamp(msg_data.get("timestamp", 0)),
            is_group=is_group,
            is_bot_mentioned=is_mentioned,
            raw={
                "message_type": msg_data.get("messageType"),
                "actor_type": msg_data.get("actorType"),
                "reference_id": msg_data.get("referenceId"),
                "reactions": msg_data.get("reactions", {}),
                "message_parameters": message_params,
                "conversation_name": conv.display_name if conv else None,
            },
        )

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        media: Optional[List[MediaAttachment]] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send a message to a Nextcloud Talk conversation."""
        try:
            data = {
                "message": text,
                "actorDisplayName": self.nc_config.username,
            }

            # Add reply reference
            if reply_to:
                data["replyTo"] = int(reply_to)

            # Handle file attachments
            if media and self.nc_config.enable_file_sharing:
                for m in media:
                    file_result = await self._share_file(chat_id, m)
                    if not file_result.success:
                        logger.warning(f"Failed to share file: {file_result.error}")

            result = await self._api_post(f"chat/{chat_id}", data)

            if result and "ocs" in result:
                msg_data = result["ocs"]["data"]
                return SendResult(
                    success=True,
                    message_id=str(msg_data.get("id")),
                    raw=msg_data,
                )
            else:
                return SendResult(success=False, error="Failed to send message")

        except Exception as e:
            logger.error(f"Failed to send Nextcloud message: {e}")
            return SendResult(success=False, error=str(e))

    async def _share_file(
        self,
        token: str,
        media: MediaAttachment,
    ) -> SendResult:
        """Share a file from Nextcloud Files to a conversation."""
        if not self._session:
            return SendResult(success=False, error="Not connected")

        try:
            # If file path provided, upload to Nextcloud first
            if media.file_path:
                upload_result = await self._upload_file(media.file_path)
                if not upload_result:
                    return SendResult(success=False, error="Failed to upload file")
                file_path = upload_result
            elif media.file_id:
                file_path = media.file_id
            else:
                return SendResult(success=False, error="No file source")

            # Share file to conversation
            data = {
                "shareType": 10,  # Share to Talk room
                "shareWith": token,
                "path": file_path,
            }

            share_url = urljoin(self.nc_config.server_url, "/ocs/v2.php/apps/files_sharing/api/v1/shares")
            async with self._session.post(share_url, json=data) as response:
                if response.status in (200, 201):
                    return SendResult(success=True)
                else:
                    error_text = await response.text()
                    return SendResult(success=False, error=f"Share failed: {error_text}")

        except Exception as e:
            logger.error(f"Failed to share file: {e}")
            return SendResult(success=False, error=str(e))

    async def _upload_file(self, file_path: str) -> Optional[str]:
        """Upload a file to Nextcloud Files."""
        if not self._session:
            return None

        try:
            file_name = os.path.basename(file_path)
            upload_path = f"Talk/{file_name}"
            url = urljoin(self._dav_url, upload_path)

            with open(file_path, "rb") as f:
                async with self._session.put(url, data=f) as response:
                    if response.status in (201, 204):
                        return f"/{upload_path}"
                    else:
                        logger.error(f"File upload failed: {response.status}")
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
        """Edit an existing Nextcloud Talk message."""
        try:
            data = {"message": text}
            result = await self._api_put(f"chat/{chat_id}/{message_id}", data)

            if result and "ocs" in result:
                return SendResult(success=True, message_id=message_id)
            else:
                return SendResult(success=False, error="Failed to edit message")

        except Exception as e:
            logger.error(f"Failed to edit Nextcloud message: {e}")
            return SendResult(success=False, error=str(e))

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete a Nextcloud Talk message."""
        return await self._api_delete(f"chat/{chat_id}/{message_id}")

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator (not supported in Nextcloud Talk)."""
        # Nextcloud Talk doesn't have a typing indicator API
        pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a Nextcloud Talk conversation."""
        conv = self._conversations.get(chat_id)
        if not conv:
            # Try to fetch from API
            result = await self._api_get(f"room/{chat_id}")
            if result and "ocs" in result:
                conv = self._parse_conversation(result["ocs"]["data"])
                self._conversations[chat_id] = conv

        if conv:
            return {
                "token": conv.token,
                "name": conv.name,
                "display_name": conv.display_name,
                "type": conv.type.name,
                "read_only": conv.read_only,
                "has_call": conv.has_call,
                "unread_messages": conv.unread_messages,
                "description": conv.description,
            }
        return None

    # Nextcloud Talk-specific methods

    async def create_conversation(
        self,
        room_type: int = 2,  # 2 = group, 3 = public
        invite: Optional[str] = None,
        room_name: str = "",
    ) -> Optional[str]:
        """Create a new conversation."""
        try:
            data = {"roomType": room_type}
            if invite:
                data["invite"] = invite
            if room_name:
                data["roomName"] = room_name

            result = await self._api_post("room", data)
            if result and "ocs" in result:
                conv_data = result["ocs"]["data"]
                conv = self._parse_conversation(conv_data)
                self._conversations[conv.token] = conv
                return conv.token
            return None

        except Exception as e:
            logger.error(f"Failed to create conversation: {e}")
            return None

    async def add_participant(
        self,
        token: str,
        user_id: str,
    ) -> bool:
        """Add a participant to a conversation."""
        try:
            data = {
                "newParticipant": user_id,
                "source": "users",
            }
            result = await self._api_post(f"room/{token}/participants", data)
            return result is not None
        except Exception as e:
            logger.error(f"Failed to add participant: {e}")
            return False

    async def remove_participant(
        self,
        token: str,
        attendee_id: int,
    ) -> bool:
        """Remove a participant from a conversation."""
        return await self._api_delete(f"room/{token}/attendees/{attendee_id}")

    async def get_participants(self, token: str) -> List[NextcloudParticipant]:
        """Get participants of a conversation."""
        try:
            result = await self._api_get(f"room/{token}/participants")
            if result and "ocs" in result:
                participants = []
                for p_data in result["ocs"]["data"]:
                    participant = NextcloudParticipant(
                        attendee_id=p_data.get("attendeeId"),
                        actor_type=p_data.get("actorType"),
                        actor_id=p_data.get("actorId"),
                        display_name=p_data.get("displayName"),
                        participant_type=ParticipantType(p_data.get("participantType", 3)),
                        in_call=p_data.get("inCall", 0) > 0,
                        session_ids=p_data.get("sessionIds", []),
                    )
                    participants.append(participant)
                self._participants_cache[token] = participants
                return participants
            return []
        except Exception as e:
            logger.error(f"Failed to get participants: {e}")
            return []

    async def set_conversation_name(self, token: str, name: str) -> bool:
        """Set conversation display name."""
        try:
            result = await self._api_put(f"room/{token}", {"roomName": name})
            if result and "ocs" in result:
                if token in self._conversations:
                    self._conversations[token].display_name = name
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to set conversation name: {e}")
            return False

    async def set_conversation_description(self, token: str, description: str) -> bool:
        """Set conversation description."""
        try:
            result = await self._api_put(f"room/{token}/description", {"description": description})
            return result is not None
        except Exception as e:
            logger.error(f"Failed to set conversation description: {e}")
            return False

    async def leave_conversation(self, token: str) -> bool:
        """Leave a conversation."""
        result = await self._api_delete(f"room/{token}/participants/self")
        if result:
            self._conversations.pop(token, None)
            self._last_known_message.pop(token, None)
        return result

    async def delete_conversation(self, token: str) -> bool:
        """Delete a conversation (must be moderator)."""
        result = await self._api_delete(f"room/{token}")
        if result:
            self._conversations.pop(token, None)
            self._last_known_message.pop(token, None)
        return result

    async def add_reaction(
        self,
        chat_id: str,
        message_id: str,
        reaction: str,
    ) -> bool:
        """Add a reaction to a message."""
        if not self.nc_config.enable_reactions:
            return False

        try:
            result = await self._api_post(
                f"reaction/{chat_id}/{message_id}",
                {"reaction": reaction}
            )
            return result is not None
        except Exception as e:
            logger.error(f"Failed to add reaction: {e}")
            return False

    async def remove_reaction(
        self,
        chat_id: str,
        message_id: str,
        reaction: str,
    ) -> bool:
        """Remove a reaction from a message."""
        if not self.nc_config.enable_reactions:
            return False

        return await self._api_delete(f"reaction/{chat_id}/{message_id}?reaction={quote(reaction)}")

    async def get_reactions(
        self,
        chat_id: str,
        message_id: str,
    ) -> Dict[str, List[str]]:
        """Get reactions on a message."""
        try:
            result = await self._api_get(f"reaction/{chat_id}/{message_id}")
            if result and "ocs" in result:
                return result["ocs"]["data"]
            return {}
        except Exception as e:
            logger.error(f"Failed to get reactions: {e}")
            return {}

    async def create_poll(
        self,
        chat_id: str,
        question: str,
        options: List[str],
        result_mode: int = 0,  # 0 = public, 1 = hidden
        max_votes: int = 0,  # 0 = unlimited
    ) -> SendResult:
        """Create a poll in a conversation."""
        if not self.nc_config.enable_polls:
            return SendResult(success=False, error="Polls disabled")

        try:
            data = {
                "question": question,
                "options": options,
                "resultMode": result_mode,
                "maxVotes": max_votes,
            }

            result = await self._api_post(f"poll/{chat_id}", data)
            if result and "ocs" in result:
                poll_data = result["ocs"]["data"]
                return SendResult(
                    success=True,
                    message_id=str(poll_data.get("id")),
                    raw=poll_data,
                )
            return SendResult(success=False, error="Failed to create poll")

        except Exception as e:
            logger.error(f"Failed to create poll: {e}")
            return SendResult(success=False, error=str(e))

    async def vote_poll(
        self,
        chat_id: str,
        poll_id: int,
        option_ids: List[int],
    ) -> bool:
        """Vote on a poll."""
        try:
            result = await self._api_post(
                f"poll/{chat_id}/{poll_id}",
                {"optionIds": option_ids}
            )
            return result is not None
        except Exception as e:
            logger.error(f"Failed to vote on poll: {e}")
            return False

    async def close_poll(self, chat_id: str, poll_id: int) -> bool:
        """Close a poll."""
        return await self._api_delete(f"poll/{chat_id}/{poll_id}")

    async def share_rich_object(
        self,
        chat_id: str,
        object_type: str,
        object_id: str,
        meta_data: Dict[str, Any],
        reference_id: Optional[str] = None,
    ) -> SendResult:
        """Share a rich object (deck card, location, etc.) to a conversation."""
        try:
            data = {
                "objectType": object_type,
                "objectId": object_id,
                "metaData": json.dumps(meta_data),
            }
            if reference_id:
                data["referenceId"] = reference_id

            result = await self._api_post(f"chat/{chat_id}/share", data)
            if result and "ocs" in result:
                msg_data = result["ocs"]["data"]
                return SendResult(
                    success=True,
                    message_id=str(msg_data.get("id")),
                    raw=msg_data,
                )
            return SendResult(success=False, error="Failed to share rich object")

        except Exception as e:
            logger.error(f"Failed to share rich object: {e}")
            return SendResult(success=False, error=str(e))

    async def set_read_marker(self, chat_id: str, message_id: int) -> bool:
        """Mark messages as read up to a specific message."""
        try:
            result = await self._api_post(
                f"chat/{chat_id}/read",
                {"lastReadMessage": message_id}
            )
            return result is not None
        except Exception as e:
            logger.error(f"Failed to set read marker: {e}")
            return False

    async def download_file(self, file_path: str) -> Optional[bytes]:
        """Download a file from Nextcloud Files."""
        if not self._session:
            return None

        try:
            url = urljoin(self._dav_url, file_path.lstrip("/"))
            async with self._session.get(url) as response:
                if response.status == 200:
                    return await response.read()
            return None
        except Exception as e:
            logger.error(f"Failed to download file: {e}")
            return None

    def on_reaction(self, handler: Callable) -> None:
        """Register a handler for reaction events."""
        self._reaction_handlers.append(handler)


def create_nextcloud_adapter(
    server_url: str = None,
    username: str = None,
    app_password: str = None,
    **kwargs
) -> NextcloudAdapter:
    """
    Factory function to create Nextcloud Talk adapter.

    Args:
        server_url: Nextcloud server URL (or set NEXTCLOUD_URL env var)
        username: Username (or set NEXTCLOUD_USERNAME env var)
        app_password: App password (or set NEXTCLOUD_APP_PASSWORD env var)
        **kwargs: Additional config options

    Returns:
        Configured NextcloudAdapter
    """
    server_url = server_url or os.getenv("NEXTCLOUD_URL")
    username = username or os.getenv("NEXTCLOUD_USERNAME")
    app_password = app_password or os.getenv("NEXTCLOUD_APP_PASSWORD")

    if not server_url:
        raise ValueError("Nextcloud server URL required")
    if not username:
        raise ValueError("Nextcloud username required")
    if not app_password:
        raise ValueError("Nextcloud app password required")

    config = NextcloudConfig(
        server_url=server_url,
        username=username,
        app_password=app_password,
        **kwargs
    )
    return NextcloudAdapter(config)
