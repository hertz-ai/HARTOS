"""
Matrix Protocol Channel Adapter

Implements Matrix messaging with end-to-end encryption support.
Based on HevolveBot extension patterns for Matrix.

Features:
- End-to-end encryption (E2EE) using Olm/Megolm
- Room management (create, join, invite, leave)
- Reactions
- Thread support (MSC3440)
- Read receipts
- Typing indicators
- Media upload/download
- Device verification
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime
from dataclasses import dataclass, field

try:
    from nio import (
        AsyncClient,
        AsyncClientConfig,
        LoginResponse,
        RoomMessageText,
        RoomMessageMedia,
        RoomMemberEvent,
        InviteMemberEvent,
        MatrixRoom,
        Event,
        SyncResponse,
        UploadResponse,
        RoomCreateResponse,
        JoinResponse,
        RoomSendResponse,
        RoomResolveAliasResponse,
        ToDeviceEvent,
        KeyVerificationEvent,
        MegolmEvent,
    )
    from nio.store import SqliteStore
    HAS_MATRIX = True
except ImportError:
    HAS_MATRIX = False

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
class MatrixConfig(ChannelConfig):
    """Matrix-specific configuration."""
    homeserver_url: str = "https://matrix.org"
    user_id: str = ""
    device_id: Optional[str] = None
    device_name: str = "HevolveBotClient"
    store_path: str = "./matrix_store"
    enable_e2ee: bool = True
    auto_join_rooms: bool = True
    trust_own_devices: bool = True
    verification_emoji: bool = True


@dataclass
class MatrixRoom:
    """Matrix room information."""
    room_id: str
    name: Optional[str] = None
    topic: Optional[str] = None
    is_encrypted: bool = False
    member_count: int = 0
    is_direct: bool = False


@dataclass
class ThreadInfo:
    """Thread information for Matrix threads (MSC3440)."""
    root_event_id: str
    latest_event_id: Optional[str] = None
    reply_count: int = 0


class MatrixAdapter(ChannelAdapter):
    """
    Matrix protocol messaging adapter with E2EE support.

    Usage:
        config = MatrixConfig(
            homeserver_url="https://matrix.org",
            user_id="@bot:matrix.org",
            token="access_token",
            enable_e2ee=True,
        )
        adapter = MatrixAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()
    """

    def __init__(self, config: MatrixConfig):
        if not HAS_MATRIX:
            raise ImportError(
                "matrix-nio not installed. "
                "Install with: pip install matrix-nio[e2e]"
            )

        super().__init__(config)
        self.matrix_config: MatrixConfig = config
        self._client: Optional[AsyncClient] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._rooms: Dict[str, MatrixRoom] = {}
        self._threads: Dict[str, ThreadInfo] = {}
        self._reaction_handlers: List[Callable] = []
        self._verified_devices: set = set()

    @property
    def name(self) -> str:
        return "matrix"

    async def connect(self) -> bool:
        """Connect to Matrix homeserver with optional E2EE setup."""
        if not self.matrix_config.homeserver_url:
            logger.error("Matrix homeserver URL not provided")
            return False

        try:
            # Configure client
            client_config = AsyncClientConfig(
                max_limit_exceeded=0,
                max_timeouts=0,
                store_sync_tokens=True,
                encryption_enabled=self.matrix_config.enable_e2ee,
            )

            # Initialize store for E2EE
            store = None
            if self.matrix_config.enable_e2ee:
                os.makedirs(self.matrix_config.store_path, exist_ok=True)
                store = SqliteStore(
                    self.matrix_config.user_id,
                    self.matrix_config.device_id or "HEVOLVEBOT",
                    self.matrix_config.store_path,
                )

            # Create client
            self._client = AsyncClient(
                homeserver=self.matrix_config.homeserver_url,
                user=self.matrix_config.user_id,
                device_id=self.matrix_config.device_id,
                store_path=self.matrix_config.store_path if store else None,
                config=client_config,
            )

            # Login or use token
            if self.matrix_config.token:
                self._client.access_token = self.matrix_config.token
                self._client.user_id = self.matrix_config.user_id
                if self.matrix_config.device_id:
                    self._client.device_id = self.matrix_config.device_id
            else:
                logger.error("Matrix access token required")
                return False

            # Setup E2EE if enabled
            if self.matrix_config.enable_e2ee:
                await self._setup_encryption()

            # Register event callbacks
            self._register_callbacks()

            # Start sync
            self._sync_task = asyncio.create_task(self._sync_forever())

            self.status = ChannelStatus.CONNECTED
            logger.info(f"Matrix connected as {self.matrix_config.user_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Matrix: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect from Matrix homeserver."""
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass

        if self._client:
            await self._client.close()
            self._client = None

        self.status = ChannelStatus.DISCONNECTED

    async def _setup_encryption(self) -> None:
        """Setup end-to-end encryption."""
        if not self._client:
            return

        # Trust own devices if configured
        if self.matrix_config.trust_own_devices:
            await self._trust_own_devices()

        logger.info("Matrix E2EE initialized")

    async def _trust_own_devices(self) -> None:
        """Trust all devices belonging to the bot user."""
        if not self._client:
            return

        try:
            # Get own devices
            devices = await self._client.devices()
            if hasattr(devices, 'devices'):
                for device in devices.devices:
                    self._verified_devices.add(device.device_id)
        except Exception as e:
            logger.warning(f"Could not trust own devices: {e}")

    def _register_callbacks(self) -> None:
        """Register Matrix event callbacks."""
        if not self._client:
            return

        # Message events
        self._client.add_event_callback(
            self._handle_room_message,
            RoomMessageText
        )

        # Room invites
        self._client.add_event_callback(
            self._handle_invite,
            InviteMemberEvent
        )

        # Encrypted messages (after decryption)
        if self.matrix_config.enable_e2ee:
            self._client.add_event_callback(
                self._handle_megolm_event,
                MegolmEvent
            )

    async def _sync_forever(self) -> None:
        """Continuously sync with homeserver."""
        while self._client and self.status == ChannelStatus.CONNECTED:
            try:
                sync_response = await self._client.sync(
                    timeout=30000,
                    full_state=True,
                )

                if isinstance(sync_response, SyncResponse):
                    # Update room list
                    for room_id, room in sync_response.rooms.join.items():
                        await self._update_room_info(room_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Matrix sync error: {e}")
                await asyncio.sleep(5)

    async def _handle_room_message(
        self,
        room: Any,
        event: RoomMessageText
    ) -> None:
        """Handle incoming room messages."""
        # Ignore own messages
        if event.sender == self.matrix_config.user_id:
            return

        # Convert to unified message
        message = self._convert_message(room, event)
        await self._dispatch_message(message)

    async def _handle_invite(self, room: Any, event: InviteMemberEvent) -> None:
        """Handle room invite events."""
        if not self.matrix_config.auto_join_rooms:
            return

        if event.state_key == self.matrix_config.user_id:
            try:
                await self._client.join(room.room_id)
                logger.info(f"Auto-joined room: {room.room_id}")
            except Exception as e:
                logger.error(f"Failed to join room: {e}")

    async def _handle_megolm_event(self, room: Any, event: MegolmEvent) -> None:
        """Handle decrypted Megolm events."""
        # This is called after successful decryption
        if hasattr(event, 'decrypted'):
            if event.sender == self.matrix_config.user_id:
                return

            message = self._convert_message(room, event)
            await self._dispatch_message(message)

    async def _update_room_info(self, room_id: str) -> None:
        """Update cached room information."""
        if not self._client:
            return

        try:
            room = self._client.rooms.get(room_id)
            if room:
                self._rooms[room_id] = MatrixRoom(
                    room_id=room_id,
                    name=room.display_name if hasattr(room, 'display_name') else None,
                    topic=room.topic if hasattr(room, 'topic') else None,
                    is_encrypted=room.encrypted if hasattr(room, 'encrypted') else False,
                    member_count=room.member_count if hasattr(room, 'member_count') else 0,
                    is_direct=room.is_direct if hasattr(room, 'is_direct') else False,
                )
        except Exception as e:
            logger.warning(f"Could not update room info: {e}")

    def _convert_message(self, room: Any, event: Any) -> Message:
        """Convert Matrix event to unified Message format."""
        # Extract text content
        text = ""
        if hasattr(event, 'body'):
            text = event.body
        elif hasattr(event, 'content') and isinstance(event.content, dict):
            text = event.content.get('body', '')

        # Check for reply/thread
        reply_to_id = None
        if hasattr(event, 'content') and isinstance(event.content, dict):
            relates_to = event.content.get('m.relates_to', {})
            if 'm.in_reply_to' in relates_to:
                reply_to_id = relates_to['m.in_reply_to'].get('event_id')
            elif relates_to.get('rel_type') == 'm.thread':
                reply_to_id = relates_to.get('event_id')

        # Determine if group
        is_group = True
        room_info = self._rooms.get(room.room_id)
        if room_info and room_info.is_direct:
            is_group = False

        # Check for bot mention
        is_mentioned = False
        if self.matrix_config.user_id in text:
            is_mentioned = True

        return Message(
            id=event.event_id,
            channel=self.name,
            sender_id=event.sender,
            sender_name=room.user_name(event.sender) if hasattr(room, 'user_name') else event.sender,
            chat_id=room.room_id,
            text=text,
            reply_to_id=reply_to_id,
            timestamp=datetime.fromtimestamp(event.server_timestamp / 1000) if hasattr(event, 'server_timestamp') else datetime.now(),
            is_group=is_group,
            is_bot_mentioned=is_mentioned,
            raw={
                'event_type': type(event).__name__,
                'room_id': room.room_id,
                'encrypted': room_info.is_encrypted if room_info else False,
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
        """Send a message to a Matrix room."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            # Build message content
            content = {
                'msgtype': 'm.text',
                'body': text,
            }

            # Add formatted body (HTML)
            if '<' in text and '>' in text:
                content['format'] = 'org.matrix.custom.html'
                content['formatted_body'] = text

            # Add reply relation
            if reply_to:
                content['m.relates_to'] = {
                    'm.in_reply_to': {
                        'event_id': reply_to
                    }
                }

            # Handle media
            if media and len(media) > 0:
                return await self._send_media(chat_id, text, media[0], reply_to)

            # Send message
            response = await self._client.room_send(
                room_id=chat_id,
                message_type='m.room.message',
                content=content,
            )

            if isinstance(response, RoomSendResponse):
                return SendResult(
                    success=True,
                    message_id=response.event_id,
                )
            else:
                return SendResult(
                    success=False,
                    error=str(response),
                )

        except Exception as e:
            logger.error(f"Failed to send Matrix message: {e}")
            return SendResult(success=False, error=str(e))

    async def _send_media(
        self,
        chat_id: str,
        caption: str,
        media: MediaAttachment,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """Send media message to Matrix room."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            # Upload media first
            if media.file_path:
                with open(media.file_path, 'rb') as f:
                    file_data = f.read()

                upload_response = await self._client.upload(
                    data_provider=file_data,
                    content_type=media.mime_type or 'application/octet-stream',
                    filename=media.file_name,
                )

                if isinstance(upload_response, UploadResponse):
                    mxc_url = upload_response.content_uri
                else:
                    return SendResult(success=False, error="Upload failed")
            elif media.url:
                mxc_url = media.url
            else:
                return SendResult(success=False, error="No media source")

            # Determine message type
            msgtype = 'm.file'
            if media.type == MessageType.IMAGE:
                msgtype = 'm.image'
            elif media.type == MessageType.VIDEO:
                msgtype = 'm.video'
            elif media.type == MessageType.AUDIO:
                msgtype = 'm.audio'

            # Build content
            content = {
                'msgtype': msgtype,
                'body': caption or media.file_name or 'attachment',
                'url': mxc_url,
            }

            if media.mime_type:
                content['info'] = {'mimetype': media.mime_type}

            if reply_to:
                content['m.relates_to'] = {
                    'm.in_reply_to': {'event_id': reply_to}
                }

            # Send
            response = await self._client.room_send(
                room_id=chat_id,
                message_type='m.room.message',
                content=content,
            )

            if isinstance(response, RoomSendResponse):
                return SendResult(success=True, message_id=response.event_id)
            else:
                return SendResult(success=False, error=str(response))

        except Exception as e:
            logger.error(f"Failed to send Matrix media: {e}")
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Edit an existing Matrix message."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            content = {
                'msgtype': 'm.text',
                'body': f'* {text}',
                'm.new_content': {
                    'msgtype': 'm.text',
                    'body': text,
                },
                'm.relates_to': {
                    'rel_type': 'm.replace',
                    'event_id': message_id,
                },
            }

            response = await self._client.room_send(
                room_id=chat_id,
                message_type='m.room.message',
                content=content,
            )

            if isinstance(response, RoomSendResponse):
                return SendResult(success=True, message_id=response.event_id)
            else:
                return SendResult(success=False, error=str(response))

        except Exception as e:
            logger.error(f"Failed to edit Matrix message: {e}")
            return SendResult(success=False, error=str(e))

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Redact a Matrix message."""
        if not self._client:
            return False

        try:
            response = await self._client.room_redact(
                room_id=chat_id,
                event_id=message_id,
                reason="Deleted by bot",
            )
            return True
        except Exception as e:
            logger.error(f"Failed to delete Matrix message: {e}")
            return False

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        if self._client:
            try:
                await self._client.room_typing(
                    room_id=chat_id,
                    typing_state=True,
                    timeout=30000,
                )
            except Exception:
                pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a Matrix room."""
        room_info = self._rooms.get(chat_id)
        if room_info:
            return {
                'room_id': room_info.room_id,
                'name': room_info.name,
                'topic': room_info.topic,
                'encrypted': room_info.is_encrypted,
                'member_count': room_info.member_count,
                'is_direct': room_info.is_direct,
            }
        return None

    # Matrix-specific methods

    async def create_room(
        self,
        name: str,
        topic: Optional[str] = None,
        invite: Optional[List[str]] = None,
        is_direct: bool = False,
        encrypted: bool = True,
    ) -> Optional[str]:
        """Create a new Matrix room."""
        if not self._client:
            return None

        try:
            initial_state = []
            if encrypted and self.matrix_config.enable_e2ee:
                initial_state.append({
                    'type': 'm.room.encryption',
                    'content': {'algorithm': 'm.megolm.v1.aes-sha2'},
                })

            response = await self._client.room_create(
                name=name,
                topic=topic,
                invite=invite or [],
                is_direct=is_direct,
                initial_state=initial_state,
            )

            if isinstance(response, RoomCreateResponse):
                return response.room_id
            return None

        except Exception as e:
            logger.error(f"Failed to create room: {e}")
            return None

    async def join_room(self, room_id_or_alias: str) -> bool:
        """Join a Matrix room."""
        if not self._client:
            return False

        try:
            response = await self._client.join(room_id_or_alias)
            return isinstance(response, JoinResponse)
        except Exception as e:
            logger.error(f"Failed to join room: {e}")
            return False

    async def leave_room(self, room_id: str) -> bool:
        """Leave a Matrix room."""
        if not self._client:
            return False

        try:
            await self._client.room_leave(room_id)
            return True
        except Exception as e:
            logger.error(f"Failed to leave room: {e}")
            return False

    async def invite_user(self, room_id: str, user_id: str) -> bool:
        """Invite a user to a Matrix room."""
        if not self._client:
            return False

        try:
            await self._client.room_invite(room_id, user_id)
            return True
        except Exception as e:
            logger.error(f"Failed to invite user: {e}")
            return False

    async def add_reaction(
        self,
        chat_id: str,
        message_id: str,
        emoji: str,
    ) -> bool:
        """Add a reaction to a message."""
        if not self._client:
            return False

        try:
            content = {
                'm.relates_to': {
                    'rel_type': 'm.annotation',
                    'event_id': message_id,
                    'key': emoji,
                }
            }

            await self._client.room_send(
                room_id=chat_id,
                message_type='m.reaction',
                content=content,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to add reaction: {e}")
            return False

    async def send_thread_reply(
        self,
        chat_id: str,
        thread_root_id: str,
        text: str,
    ) -> SendResult:
        """Send a reply in a thread (MSC3440)."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            content = {
                'msgtype': 'm.text',
                'body': text,
                'm.relates_to': {
                    'rel_type': 'm.thread',
                    'event_id': thread_root_id,
                    'is_falling_back': True,
                    'm.in_reply_to': {
                        'event_id': thread_root_id
                    }
                }
            }

            response = await self._client.room_send(
                room_id=chat_id,
                message_type='m.room.message',
                content=content,
            )

            if isinstance(response, RoomSendResponse):
                return SendResult(success=True, message_id=response.event_id)
            else:
                return SendResult(success=False, error=str(response))

        except Exception as e:
            logger.error(f"Failed to send thread reply: {e}")
            return SendResult(success=False, error=str(e))

    async def send_read_receipt(self, chat_id: str, message_id: str) -> bool:
        """Send read receipt for a message."""
        if not self._client:
            return False

        try:
            await self._client.room_read_markers(
                room_id=chat_id,
                fully_read_event=message_id,
                read_event=message_id,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send read receipt: {e}")
            return False

    async def verify_device(self, user_id: str, device_id: str) -> bool:
        """Verify a device for E2EE."""
        if not self._client or not self.matrix_config.enable_e2ee:
            return False

        try:
            await self._client.verify_device(user_id, device_id)
            self._verified_devices.add(f"{user_id}:{device_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to verify device: {e}")
            return False


def create_matrix_adapter(
    homeserver_url: str = None,
    user_id: str = None,
    token: str = None,
    **kwargs
) -> MatrixAdapter:
    """
    Factory function to create Matrix adapter.

    Args:
        homeserver_url: Matrix homeserver URL (or set MATRIX_HOMESERVER_URL env var)
        user_id: Bot user ID (or set MATRIX_USER_ID env var)
        token: Access token (or set MATRIX_ACCESS_TOKEN env var)
        **kwargs: Additional config options

    Returns:
        Configured MatrixAdapter
    """
    homeserver_url = homeserver_url or os.getenv("MATRIX_HOMESERVER_URL", "https://matrix.org")
    user_id = user_id or os.getenv("MATRIX_USER_ID")
    token = token or os.getenv("MATRIX_ACCESS_TOKEN")

    if not user_id:
        raise ValueError("Matrix user ID required")
    if not token:
        raise ValueError("Matrix access token required")

    config = MatrixConfig(
        homeserver_url=homeserver_url,
        user_id=user_id,
        token=token,
        **kwargs
    )
    return MatrixAdapter(config)
