"""
Slack Channel Adapter

Implements Slack messaging using the Bolt framework with Socket Mode.
Supports workspaces, channels, threads, and rich formatting.

Features:
- Text messages with mrkdwn formatting
- File uploads
- Threads
- Reactions
- Slash commands
- Interactive components (buttons, modals)
- Socket Mode (no public URL needed)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, List, Dict, Any
from datetime import datetime

try:
    from slack_bolt.async_app import AsyncApp
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_sdk.web.async_client import AsyncWebClient
    from slack_sdk.errors import SlackApiError
    HAS_SLACK = True
except ImportError:
    HAS_SLACK = False

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
from .room_capable import RoomCapableAdapter, UnsupportedRoomError

logger = logging.getLogger(__name__)


class SlackAdapter(ChannelAdapter, RoomCapableAdapter):
    """
    Slack messaging adapter using Bolt framework with Socket Mode.

    Usage:
        config = ChannelConfig(
            token="xoxb-bot-token",
            extra={"app_token": "xapp-app-token"}
        )
        adapter = SlackAdapter(config)
        adapter.on_message(my_handler)
        await adapter.start()
    """

    def __init__(self, config: ChannelConfig):
        if not HAS_SLACK:
            raise ImportError(
                "slack-bolt not installed. "
                "Install with: pip install slack-bolt slack-sdk"
            )

        super().__init__(config)
        self._app: Optional[AsyncApp] = None
        self._handler: Optional[AsyncSocketModeHandler] = None
        self._client: Optional[AsyncWebClient] = None
        self._bot_user_id: Optional[str] = None
        self._bot_name: Optional[str] = None
        self._app_token: str = config.extra.get("app_token", "")

    @property
    def name(self) -> str:
        return "slack"

    async def connect(self) -> bool:
        """Connect to Slack using Socket Mode."""
        if not self.config.token:
            logger.error("Slack bot token not provided")
            return False

        if not self._app_token:
            logger.error("Slack app token not provided (required for Socket Mode)")
            return False

        try:
            # Create Bolt app
            self._app = AsyncApp(token=self.config.token)
            self._client = self._app.client

            # Get bot info
            auth_response = await self._client.auth_test()
            self._bot_user_id = auth_response.get("user_id")
            self._bot_name = auth_response.get("user")
            logger.info(f"Slack connected as @{self._bot_name}")

            # Register event handlers
            self._register_handlers()

            # Start Socket Mode handler
            self._handler = AsyncSocketModeHandler(self._app, self._app_token)
            asyncio.create_task(self._handler.start_async())

            self.status = ChannelStatus.CONNECTED
            return True

        except SlackApiError as e:
            logger.error(f"Failed to connect to Slack: {e}")
            self.status = ChannelStatus.ERROR
            return False
        except Exception as e:
            logger.error(f"Slack connection error: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Disconnect from Slack."""
        if self._handler:
            try:
                await self._handler.close_async()
            except Exception as e:
                logger.error(f"Error disconnecting from Slack: {e}")
            finally:
                self._handler = None
                self._app = None
                self._client = None
                self.status = ChannelStatus.DISCONNECTED

    def _register_handlers(self) -> None:
        """Register Slack event handlers."""
        if not self._app:
            return

        @self._app.event("message")
        async def handle_message(event: Dict, say):
            # Ignore bot messages
            if event.get("bot_id") or event.get("subtype") == "bot_message":
                return

            # Ignore message edits and deletes
            if event.get("subtype") in ("message_changed", "message_deleted"):
                return

            message = self._convert_message(event)
            await self._dispatch_message(message)

        @self._app.event("app_mention")
        async def handle_mention(event: Dict, say):
            # Handle direct mentions separately if needed
            message = self._convert_message(event)
            message.is_bot_mentioned = True
            await self._dispatch_message(message)

        @self._app.event("reaction_added")
        async def handle_reaction(event: Dict):
            # Can be used for reaction-based workflows
            logger.debug(f"Reaction added: {event.get('reaction')} by {event.get('user')}")

    def _convert_message(self, event: Dict[str, Any]) -> Message:
        """Convert Slack event to unified Message format."""
        # Check if bot is mentioned
        is_mentioned = False
        text = event.get("text", "")
        if self._bot_user_id and f"<@{self._bot_user_id}>" in text:
            is_mentioned = True
            # Remove mention from text
            text = text.replace(f"<@{self._bot_user_id}>", "").strip()

        # Process attachments/files
        media = []
        files = event.get("files", [])
        for file in files:
            file_type = file.get("filetype", "")
            if file_type in ("png", "jpg", "jpeg", "gif", "webp"):
                media_type = MessageType.IMAGE
            elif file_type in ("mp4", "mov", "avi", "webm"):
                media_type = MessageType.VIDEO
            elif file_type in ("mp3", "wav", "ogg", "m4a"):
                media_type = MessageType.AUDIO
            else:
                media_type = MessageType.DOCUMENT

            media.append(MediaAttachment(
                type=media_type,
                url=file.get("url_private"),
                file_id=file.get("id"),
                file_name=file.get("name"),
                file_size=file.get("size"),
                mime_type=file.get("mimetype"),
            ))

        # Determine if this is in a channel/group or DM
        channel_type = event.get("channel_type", "")
        is_group = channel_type in ("channel", "group")

        return Message(
            id=event.get("ts", ""),
            channel=self.name,
            sender_id=event.get("user", ""),
            sender_name=None,  # Would need to fetch from users.info
            chat_id=event.get("channel", ""),
            text=text,
            media=media,
            reply_to_id=event.get("thread_ts"),
            timestamp=datetime.fromtimestamp(float(event.get("ts", "0").split(".")[0])),
            is_group=is_group,
            is_bot_mentioned=is_mentioned,
            raw=event,
        )

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        media: Optional[List[MediaAttachment]] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Send a message to a Slack channel."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            # Build message payload
            payload: Dict[str, Any] = {
                "channel": chat_id,
                "text": text,
            }

            # Handle thread reply
            if reply_to:
                payload["thread_ts"] = reply_to

            # Handle buttons/blocks
            if buttons:
                payload["blocks"] = self._build_blocks(text, buttons)

            # Handle media (file upload)
            if media and len(media) > 0:
                return await self._send_with_media(chat_id, text, media, reply_to)

            # Send text message
            response = await self._client.chat_postMessage(**payload)

            return SendResult(
                success=True,
                message_id=response.get("ts"),
                raw=dict(response.data),
            )

        except SlackApiError as e:
            if e.response.get("error") == "ratelimited":
                retry_after = int(e.response.headers.get("Retry-After", 60))
                raise ChannelRateLimitError(retry_after=retry_after)
            logger.error(f"Failed to send Slack message: {e}")
            return SendResult(success=False, error=str(e))
        except Exception as e:
            logger.error(f"Failed to send Slack message: {e}")
            return SendResult(success=False, error=str(e))

    async def _send_with_media(
        self,
        chat_id: str,
        text: str,
        media: List[MediaAttachment],
        reply_to: Optional[str],
    ) -> SendResult:
        """Send message with file attachment."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        first_media = media[0]

        try:
            upload_args: Dict[str, Any] = {
                "channels": chat_id,
                "initial_comment": text,
            }

            if reply_to:
                upload_args["thread_ts"] = reply_to

            if first_media.file_path:
                upload_args["file"] = first_media.file_path
            elif first_media.url:
                # Download and upload
                upload_args["content"] = first_media.url

            if first_media.file_name:
                upload_args["filename"] = first_media.file_name

            response = await self._client.files_upload_v2(**upload_args)

            file_info = response.get("file", {})
            return SendResult(
                success=True,
                message_id=file_info.get("id"),
                raw=dict(response.data),
            )

        except SlackApiError as e:
            logger.error(f"Failed to upload Slack file: {e}")
            return SendResult(success=False, error=str(e))

    def _build_blocks(self, text: str, buttons: List[Dict]) -> List[Dict]:
        """Build Slack blocks with buttons."""
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            }
        ]

        # Add button actions
        actions = []
        for btn in buttons:
            if btn.get("url"):
                actions.append({
                    "type": "button",
                    "text": {"type": "plain_text", "text": btn["text"]},
                    "url": btn["url"],
                })
            else:
                actions.append({
                    "type": "button",
                    "text": {"type": "plain_text", "text": btn["text"]},
                    "action_id": btn.get("callback_data", btn["text"]),
                    "value": btn.get("callback_data", btn["text"]),
                })

        if actions:
            blocks.append({
                "type": "actions",
                "elements": actions[:5],  # Slack limits to 5 buttons per block
            })

        return blocks

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Edit an existing Slack message."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            payload: Dict[str, Any] = {
                "channel": chat_id,
                "ts": message_id,
                "text": text,
            }

            if buttons:
                payload["blocks"] = self._build_blocks(text, buttons)

            response = await self._client.chat_update(**payload)

            return SendResult(
                success=True,
                message_id=response.get("ts"),
                raw=dict(response.data),
            )

        except SlackApiError as e:
            logger.error(f"Failed to edit Slack message: {e}")
            return SendResult(success=False, error=str(e))

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete a Slack message."""
        if not self._client:
            return False

        try:
            await self._client.chat_delete(channel=chat_id, ts=message_id)
            return True
        except SlackApiError as e:
            logger.error(f"Failed to delete Slack message: {e}")
            return False

    async def send_typing(self, chat_id: str) -> None:
        """Slack doesn't have a typing indicator API for bots."""
        # Slack bots cannot send typing indicators
        pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a Slack channel."""
        if not self._client:
            return None

        try:
            response = await self._client.conversations_info(channel=chat_id)
            channel = response.get("channel", {})
            return {
                "id": channel.get("id"),
                "name": channel.get("name"),
                "is_channel": channel.get("is_channel"),
                "is_group": channel.get("is_group"),
                "is_im": channel.get("is_im"),
                "is_private": channel.get("is_private"),
                "topic": channel.get("topic", {}).get("value"),
                "purpose": channel.get("purpose", {}).get("value"),
            }
        except SlackApiError as e:
            logger.error(f"Failed to get Slack channel info: {e}")
            return None

    async def add_reaction(self, chat_id: str, message_id: str, emoji: str) -> bool:
        """Add a reaction to a message."""
        if not self._client:
            return False

        try:
            # Remove colons if present
            emoji = emoji.strip(":")
            await self._client.reactions_add(
                channel=chat_id,
                timestamp=message_id,
                name=emoji,
            )
            return True
        except SlackApiError as e:
            logger.error(f"Failed to add Slack reaction: {e}")
            return False

    async def get_user_info(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a Slack user."""
        if not self._client:
            return None

        try:
            response = await self._client.users_info(user=user_id)
            user = response.get("user", {})
            return {
                "id": user.get("id"),
                "name": user.get("name"),
                "real_name": user.get("real_name"),
                "display_name": user.get("profile", {}).get("display_name"),
                "email": user.get("profile", {}).get("email"),
                "is_bot": user.get("is_bot"),
            }
        except SlackApiError as e:
            logger.error(f"Failed to get Slack user info: {e}")
            return None

    async def open_dm(self, user_id: str) -> Optional[str]:
        """Open a DM channel with a user."""
        if not self._client:
            return None

        try:
            response = await self._client.conversations_open(users=user_id)
            return response.get("channel", {}).get("id")
        except SlackApiError as e:
            logger.error(f"Failed to open Slack DM: {e}")
            return None

    # ─── UNIF-G2: RoomCapableAdapter implementation ──────────────────

    async def join_room(self, room_id: str,
                        role: str = 'participant') -> bool:
        """Join a Slack channel as the bot's presence.

        Slack semantics: ``conversations.join`` works on PUBLIC channels
        only.  For private channels, the bot must be invited by an admin
        — there's no programmatic self-invite.  We treat
        "already a member" as success.

        IM (DM) channel ids → ``UnsupportedRoomError`` (DMs aren't
        rooms — use ``send_message`` for 1:1).
        """
        if not self._client:
            return False
        if not room_id:
            return False
        # Slack channel ids: C* = public, G* = private/group, D* = DM,
        # MP* = multi-party DM.  Reject DMs.
        if room_id.startswith('D') or room_id.startswith('MP'):
            raise UnsupportedRoomError(
                "Slack DMs are not rooms — use send_message for 1:1.")
        try:
            await self._client.conversations_join(channel=room_id)
            logger.info(
                "Slack.join_room: channel %s joined (role=%s)",
                room_id, role)
            return True
        except SlackApiError as e:
            err = (e.response or {}).get('error', '') \
                if hasattr(e, 'response') else ''
            # already_in_channel: idempotent success.
            if err == 'already_in_channel':
                return True
            # method_not_supported_for_channel_type: private channels
            # can only be joined via invite; treat as graceful False so
            # Join_External_Room can surface "ask an admin to invite the
            # bot to this private channel" to the user.
            if err in ('method_not_supported_for_channel_type',
                       'channel_not_found',
                       'is_archived',
                       'missing_scope'):
                logger.info(
                    "Slack.join_room: refused for %s (%s)",
                    room_id, err)
                return False
            logger.error(
                "Slack.join_room: API error for %s: %s",
                room_id, e)
            return False
        except Exception as e:
            logger.error(
                "Slack.join_room: unexpected error for %s: %s",
                room_id, e)
            return False

    async def leave_room(self, room_id: str) -> bool:
        """Leave a Slack channel.  Idempotent on already-absent."""
        if not self._client:
            return False
        if not room_id:
            return False
        if room_id.startswith('D') or room_id.startswith('MP'):
            # Can't "leave" a DM.  Caller should treat as no-op.
            return False
        try:
            await self._client.conversations_leave(channel=room_id)
            logger.info("Slack.leave_room: channel %s left", room_id)
            return True
        except SlackApiError as e:
            err = (e.response or {}).get('error', '') \
                if hasattr(e, 'response') else ''
            if err in ('not_in_channel', 'channel_not_found'):
                # Already absent — idempotent semantics.
                return True
            logger.error(
                "Slack.leave_room: API error for %s: %s", room_id, e)
            return False
        except Exception as e:
            logger.error(
                "Slack.leave_room: unexpected error for %s: %s",
                room_id, e)
            return False

    async def list_room_members(self, room_id: str) -> List[Dict[str, Any]]:
        """List members of a Slack channel.

        Uses ``conversations.members`` to get user-id list, then
        ``users.info`` for display names.  Skips the bot itself.
        Truncated to first page (~100) — full pagination is
        platform-side overhead the agent doesn't typically need.
        """
        if not self._client or not room_id:
            return []
        try:
            response = await self._client.conversations_members(
                channel=room_id)
            user_ids = response.get('members', []) or []
        except SlackApiError as e:
            logger.error(
                "Slack.list_room_members: API error for %s: %s",
                room_id, e)
            return []
        except Exception as e:
            logger.error(
                "Slack.list_room_members: unexpected error for %s: %s",
                room_id, e)
            return []

        result: List[Dict[str, Any]] = []
        for uid in user_ids:
            if uid == self._bot_user_id:
                continue
            try:
                info = await self._client.users_info(user=uid)
                user = info.get('user', {}) or {}
                result.append({
                    'id': uid,
                    'display_name': (user.get('profile', {}) or {}).get(
                        'display_name') or user.get('real_name') or
                        user.get('name') or uid,
                    'is_bot': bool(user.get('is_bot', False)),
                })
            except Exception:
                # Single-user info failure shouldn't drop the whole
                # list — synthesize a minimal entry.
                result.append({'id': uid, 'display_name': uid,
                               'is_bot': False})
        return result


def create_slack_adapter(
    bot_token: str = None,
    app_token: str = None,
    **kwargs
) -> SlackAdapter:
    """
    Factory function to create Slack adapter.

    Args:
        bot_token: Bot token (xoxb-...) or set SLACK_BOT_TOKEN env var
        app_token: App token (xapp-...) or set SLACK_APP_TOKEN env var
        **kwargs: Additional config options

    Returns:
        Configured SlackAdapter
    """
    bot_token = bot_token or os.getenv("SLACK_BOT_TOKEN")
    app_token = app_token or os.getenv("SLACK_APP_TOKEN")

    if not bot_token:
        raise ValueError("Slack bot token required")
    if not app_token:
        raise ValueError("Slack app token required for Socket Mode")

    config = ChannelConfig(
        token=bot_token,
        extra={"app_token": app_token, **kwargs},
    )
    return SlackAdapter(config)
